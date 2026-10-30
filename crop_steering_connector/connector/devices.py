"""The device map: the HA device of each state the report may send, read from HA's template API.

The connector renders only `DEVICE_TEMPLATE`, a constant built here from the report's own filter: the server never
sends a template, so it can never make the connector read the rest of the house.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping

from . import messages
from .clients import json_body
from .context import Context
from .entities import NUMBER_DEVICE_CLASS, SENSOR_DEVICE_CLASSES
from .transport import TransportError

DEVICE_MAP_REFRESH_SECONDS = 600.0
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_DEVICE_NAME = 120
# Every device class of these leaves: a switch of an AC's or a dehumidifier's own device is a compressor plug.
ANY_CLASS_DOMAINS = ("climate", "humidifier", "switch")


def _device_template() -> str:
    # Filtered inside the template because the answer to a POST, HA's included, is capped at 1 MiB (SERVER_MAX_BYTES),
    # while /states may reach 16 MiB. `device_attr` raises a TemplateError on a non-text id, so it only runs behind
    # `if d`: one entity with no device must never fail the whole render.
    sensor_classes = json.dumps(sorted(SENSOR_DEVICE_CLASSES))
    number_class = json.dumps(NUMBER_DEVICE_CLASS)
    sources = [f"(states.{domain} | list)" for domain in ANY_CLASS_DOMAINS]
    sources.append(f"(states.sensor | selectattr('attributes.device_class', 'in', {sensor_classes}) | list)")
    sources.append(f"(states.number | selectattr('attributes.device_class', 'eq', {number_class}) | list)")
    return (
        "{%- set ns = namespace(pairs=[]) -%}"
        "{%- for s in " + " + ".join(sources) + " -%}"
        "{%- set d = device_id(s.entity_id) -%}"
        "{%- set ns.pairs = ns.pairs + [(s.entity_id, "
        "[d, (device_attr(d, 'name_by_user') or device_attr(d, 'name')) if d else none])] -%}"
        "{%- endfor -%}"
        "{{ dict(ns.pairs) | tojson }}"
    )


DEVICE_TEMPLATE = _device_template()


@dataclass(frozen=True)
class DeviceMap:
    """`devices`: entity id → HA device id, None for an entity with no device; `names`: device id → its name."""

    devices: Mapping[str, str | None]
    names: Mapping[str, str]


def clean_name(value: object) -> str | None:
    """The same cleaning as the server's: control and format characters out, trimmed, 120 cut."""
    if not isinstance(value, str):
        return None
    name = "".join(char for char in value if unicodedata.category(char) not in ("Cc", "Cf")).strip()
    return name[:MAX_DEVICE_NAME] or None


def parse_device_map(payload: object) -> DeviceMap | None:
    """The template's answer, or None when its shape is wrong; an entry with a bad device id is dropped alone."""
    if not isinstance(payload, dict):
        return None
    devices: dict[str, str | None] = {}
    names: dict[str, str] = {}
    for entity_id, entry in payload.items():
        if not isinstance(entry, list) or len(entry) != 2:
            return None
        device_id, name = entry
        if device_id is not None and not (isinstance(device_id, str) and DEVICE_ID_RE.fullmatch(device_id)):
            continue
        devices[entity_id] = device_id
        cleaned = clean_name(name)
        if device_id is not None and cleaned is not None:
            names.setdefault(device_id, cleaned)
    return DeviceMap(devices, names)


class DeviceMapReader:
    """The last good map, read at the first report and then at most every 600 s; a failure never stops a report.

    It is the only cache the connector keeps of what Home Assistant holds, and the one a sync drops (`invalidate`).
    """

    def __init__(self, context: Context) -> None:
        self._context = context
        self._map: DeviceMap | None = None
        self._read_at: float | None = None
        self._failing = False
        self._invalidated = False

    @property
    def invalidated(self) -> bool:
        """True from `invalidate()` until the next `current()` reads the map."""
        return self._invalidated

    def invalidate(self) -> None:
        """A sync: the cached map is no longer current, so the next `current()` reads it whatever its age, and the
        600 s count restarts there.

        The last good map still serves when that read fails, so a failed read never strips the report of its device
        keys.
        """
        self._invalidated = True

    def current(self) -> DeviceMap | None:
        """The map for this report, read first when due or invalidated; None until one read succeeds."""
        now = self._context.clock.monotonic()
        if self._invalidated or self._read_at is None or now - self._read_at >= DEVICE_MAP_REFRESH_SECONDS:
            self._invalidated = False
            self._read_at = now
            self._refresh()
        return self._map

    def _refresh(self) -> None:
        fresh = self._read()
        if fresh is None:
            if not self._failing:
                self._failing = True
                self._context.log.warning(messages.DEVICE_NAMES_FAILED)
            return
        self._failing = False
        self._map = fresh

    def _read(self) -> DeviceMap | None:
        try:
            response = self._context.ha.post("/template", {"template": DEVICE_TEMPLATE})
        except TransportError:
            return None
        if response.status != 200:
            return None
        return parse_device_map(json_body(response))
