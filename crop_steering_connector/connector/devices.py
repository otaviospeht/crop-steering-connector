"""The device map: the HA device of each state the report may send, read from HA's template API.

The connector renders only `DEVICE_TEMPLATE`, a constant built here from the report's own filter: the server never
sends a template, so it can never make the connector read the rest of the house.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping

from . import messages
from .clients import json_body
from .context import Context
from .entities import BATTERY_DEVICE_CLASS, NUMBER_DEVICE_CLASS, SENSOR_DEVICE_CLASSES
from .transport import TransportError

DEVICE_MAP_REFRESH_SECONDS = 600.0
# The gap read (`fill_gaps`): under the shortest report interval (55 s), so the report right after any read may read
# again, while the early reports, 15 s apart, never read twice within it.
MISSING_REREAD_SECONDS = 50.0
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_DEVICE_NAME = 120
# Every device class of these leaves: a switch of an AC's or a dehumidifier's own device is a compressor plug.
ANY_CLASS_DOMAINS = ("climate", "humidifier", "switch")


def _device_template() -> str:
    # Filtered inside the template because the answer to a POST, HA's included, is capped at 1 MiB (SERVER_MAX_BYTES),
    # while /states may reach 16 MiB. `device_attr` raises a TemplateError on a non-text id, so it only runs behind
    # `if d`: one entity with no device must never fail the whole render. The battery sensors are mapped too, so their
    # level can ride on the entities of the same device (`report_entities`); they never leave as entities.
    sensor_classes = json.dumps(sorted(SENSOR_DEVICE_CLASSES | {BATTERY_DEVICE_CLASS}))
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
    """`devices`: entity id → HA device id, None for an entity with no device; `names`: device id → its name;
    `dropped`: the entities whose entry had a bad device id, which a new read would drop again."""

    devices: Mapping[str, str | None]
    names: Mapping[str, str]
    dropped: frozenset[str] = frozenset()

    def holds(self, entity_id: str) -> bool:
        """Whether this read saw the entity, kept or dropped: an entity it never saw appeared after it."""
        return entity_id in self.devices or entity_id in self.dropped


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
    dropped: set[str] = set()
    for entity_id, entry in payload.items():
        if not isinstance(entry, list) or len(entry) != 2:
            return None
        device_id, name = entry
        if device_id is not None and not (isinstance(device_id, str) and DEVICE_ID_RE.fullmatch(device_id)):
            dropped.add(entity_id)
            continue
        devices[entity_id] = device_id
        cleaned = clean_name(name)
        if device_id is not None and cleaned is not None:
            names.setdefault(device_id, cleaned)
    return DeviceMap(devices, names, frozenset(dropped))


class DeviceMapReader:
    """The last good map, read at the first report and then at most every 600 s; a failure never stops a report.

    It is the only cache the connector keeps of what Home Assistant holds, and the one a sync drops (`invalidate`).
    Between two reads, an entity the map never saw makes the next report read it again (`fill_gaps`).
    """

    def __init__(self, context: Context) -> None:
        self._context = context
        self._map: DeviceMap | None = None
        self._read_at: float | None = None
        self._failing = False
        self._invalidated = False
        self._good_reads = 0

    @property
    def invalidated(self) -> bool:
        """True from `invalidate()` until the next `current()` reads the map."""
        return self._invalidated

    @property
    def good_reads(self) -> int:
        """How many reads have succeeded since the process started: a sync is honoured once one more has."""
        return self._good_reads

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

    def fill_gaps(self, entity_ids: Iterable[str]) -> DeviceMap | None:
        """The map read again now when one of these entities (those the report is about to send) appeared after the
        last read, that read succeeded and it began `MISSING_REREAD_SECONDS` ago or more; else the map as it stands.

        A new device's first report then carries its `device_id` and its name, so the server never stores its entities
        apart until the 600 s read. A failing read is left to that cadence, as after a sync, and the 600 s count
        restarts at a read made here.
        """
        known = self._map
        if known is None or self._failing or self._read_at is None:
            return known
        if self._context.clock.monotonic() - self._read_at < MISSING_REREAD_SECONDS:
            return known
        if all(known.holds(entity_id) for entity_id in entity_ids):
            return known
        self._read_at = self._context.clock.monotonic()
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
        self._good_reads += 1
        self._map = fresh

    def _read(self) -> DeviceMap | None:
        try:
            response = self._context.ha.post("/template", {"template": DEVICE_TEMPLATE})
        except TransportError:
            return None
        if response.status != 200:
            return None
        return parse_device_map(json_body(response))
