"""The device map: the HA device of each state the report may send, read from HA's template API.

The connector renders only `DEVICE_TEMPLATE`, `IDENTITY_TEMPLATE` and `INFO_TEMPLATE`, constants built here from the
report's own filter: the server never sends a template, so it can never make the connector read the rest of the house.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping

from . import messages
from .clients import json_body
from .context import Context
from .device_info import FIELDS, REGISTRY_ATTRIBUTES, DeviceInfo, clean_device_info
from .entities import BATTERY_DEVICE_CLASS, NUMBER_DEVICE_CLASS, PROBLEM_DEVICE_CLASS, RELAY_STATUS_LAST, SENSOR_DEVICE_CLASSES
from .transport import TransportError

DEVICE_MAP_REFRESH_SECONDS = 600.0
# The gap read (`fill_gaps`): under the shortest report interval (55 s), so the report right after any read may read
# again, while the early reports, 15 s apart, never read twice within it.
MISSING_REREAD_SECONDS = 50.0
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_DEVICE_NAME = 120
# Every device class of these leaves: a switch of an AC's or a dehumidifier's own device is a compressor plug. A valve
# (0.2.12) may be a pump. A light (0.2.14) may take the Light role.
ANY_CLASS_DOMAINS = ("climate", "humidifier", "light", "switch", "valve")
# The identity of a device: each identifier pair HA gives, (integration domain, id), leaves as the domain and the
# SHA-256 of this prefix, the domain, "|" and the id. The prefix names the scheme version; the raw id never leaves.
IDENTITY_PREFIX = "crop-steering:device-identity:v1|"
IDENTITY_DOMAIN_RE = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_IDENTITY_ID = 255
MAX_IDENTITY_PAIRS_PER_DEVICE = 4

Identity = tuple[tuple[str, str], ...]


def _sources() -> str:
    sensor_classes = json.dumps(sorted(SENSOR_DEVICE_CLASSES | {BATTERY_DEVICE_CLASS}))
    number_class = json.dumps(NUMBER_DEVICE_CLASS)
    sources = [f"(states.{domain} | list)" for domain in ANY_CLASS_DOMAINS]
    sources.append(f"(states.sensor | selectattr('attributes.device_class', 'in', {sensor_classes}) | list)")
    sources.append(f"(states.number | selectattr('attributes.device_class', 'eq', {number_class}) | list)")
    # A problem binary sensor (0.2.12) is mapped so the report keeps only those of a humidifier's own device.
    sources.append(f"(states.binary_sensor | selectattr('attributes.device_class', 'eq', {json.dumps(PROBLEM_DEVICE_CLASS)}) | list)")
    # The selects that may be a plug's power-on state: with one last-state option; the report checks the rest.
    for option in sorted(RELAY_STATUS_LAST):
        sources.append(f"(states.select | selectattr('attributes.options', 'defined') | selectattr('attributes.options', 'contains', {json.dumps(option)}) | list)")
    return " + ".join(sources)


def _device_template() -> str:
    # Filtered inside the template because the answer to a POST, HA's included, is capped at 1 MiB (SERVER_MAX_BYTES),
    # while /states may reach 16 MiB. `device_attr` raises a TemplateError on a non-text id, so it only runs behind
    # `if d`: one entity with no device must never fail the whole render. The battery sensors are mapped too, so their
    # level can ride on the entities of the same device (`report_entities`); they never leave as entities.
    return (
        "{%- set ns = namespace(pairs=[]) -%}"
        "{%- for s in " + _sources() + " -%}"
        "{%- set d = device_id(s.entity_id) -%}"
        "{%- set ns.pairs = ns.pairs + [(s.entity_id, "
        "[d, (device_attr(d, 'name_by_user') or device_attr(d, 'name')) if d else none])] -%}"
        "{%- endfor -%}"
        "{{ dict(ns.pairs) | tojson }}"
    )


def _identity_template() -> str:
    # The same sources as the device map, so it never reads a device the report would not send, and its own render:
    # a failure here never costs the device map. `identifiers` is a set of (domain, id) tuples, and `tojson` fails
    # the whole render on a set, so it goes through `list` first. No `sort`: two ids of one domain with different
    # types would fail it, and the connector sorts the hashed pairs anyway. No domain is compared here.
    return (
        "{%- set ns = namespace(pairs=[]) -%}"
        "{%- for s in " + _sources() + " -%}"
        "{%- set d = device_id(s.entity_id) -%}"
        "{%- if d -%}"
        "{%- set ns.pairs = ns.pairs + [(d, (device_attr(d, 'identifiers') or []) | list)] -%}"
        "{%- endif -%}"
        "{%- endfor -%}"
        "{{ dict(ns.pairs) | tojson }}"
    )


def _registry_values(device: str) -> str:
    return "[" + ", ".join(f"device_attr({device}, '{attribute}')" for attribute in REGISTRY_ATTRIBUTES) + "]"


def _info_template() -> str:
    # 0.2.13, rendered only while the server asks (`device_info_wanted`): the same sources, behind `if d`, and its own
    # render, so a failure never costs the device map or the identities. One template for every HA version: HA's
    # `device_attr` answers none for an attribute the device entry lacks (`model_id` before HA 2024.8,
    # `parent_device_id` before 2026.11), and the values only leave through `tojson`, so a none is null, never the
    # text «None». Since HA 2026.11, each channel of a multi-channel Smart Life plug sits on a child device with the
    # four values null; then they come from its parent, whose id never leaves the template. `device_attr` raises on a
    # non-text id, so the parent is read only behind `p is string`.
    return (
        "{%- set ns = namespace(pairs=[]) -%}"
        "{%- for s in " + _sources() + " -%}"
        "{%- set d = device_id(s.entity_id) -%}"
        "{%- if d -%}"
        "{%- set own = " + _registry_values("d") + " -%}"
        "{%- set p = device_attr(d, 'parent_device_id') -%}"
        "{%- if (own | reject('none') | list) or p is not string -%}"
        "{%- set ns.pairs = ns.pairs + [(d, own)] -%}"
        "{%- else -%}"
        "{%- set ns.pairs = ns.pairs + [(d, " + _registry_values("p") + ")] -%}"
        "{%- endif -%}"
        "{%- endif -%}"
        "{%- endfor -%}"
        "{{ dict(ns.pairs) | tojson }}"
    )


DEVICE_TEMPLATE = _device_template()
IDENTITY_TEMPLATE = _identity_template()
INFO_TEMPLATE = _info_template()


@dataclass(frozen=True)
class DeviceMap:
    """`devices`: entity id → HA device id, None for an entity with no device; `names`: device id → its name;
    `dropped`: the entities whose entry had a bad device id, which a new read would drop again; `identities`: device
    id → its hashed pairs, from the last good identity read of the process (empty before the first); `info`: device id
    → its cleaned device info, from the last good info read while the server asks, or None (no key in the report)."""

    devices: Mapping[str, str | None]
    names: Mapping[str, str]
    dropped: frozenset[str] = frozenset()
    identities: Mapping[str, Identity] = field(default_factory=dict)
    info: Mapping[str, DeviceInfo] | None = None

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


def identity_hash(domain: str, identifier: str) -> str:
    """The lowercase hex SHA-256 of the prefix, the domain, "|" and the id, over UTF-8."""
    return hashlib.sha256(f"{IDENTITY_PREFIX}{domain}|{identifier}".encode("utf-8")).hexdigest()


def _hashed_pair(pair: object) -> tuple[str, str] | None:
    """`(domain, hash)` for a valid pair: 2 strings, the domain by its pattern and an id of 1 to 255 characters."""
    if not isinstance(pair, list) or len(pair) != 2:
        return None
    domain, identifier = pair
    if not isinstance(domain, str) or not IDENTITY_DOMAIN_RE.fullmatch(domain):
        return None
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= MAX_IDENTITY_ID:
        return None
    try:
        return domain, identity_hash(domain, identifier)
    except UnicodeEncodeError:
        # A lone surrogate from a JSON escape: not text HA could hold, so not a pair.
        return None


def parse_identities(payload: object) -> dict[str, Identity] | None:
    """The identity template's answer, or None when its shape is wrong.

    A device leaves no entry when it has no pair, more than 4 or any invalid one: a dropped or cut pair could hide a
    second pair of one domain. An entry with a bad device id is dropped alone, as in the device map.
    """
    if not isinstance(payload, dict):
        return None
    identities: dict[str, Identity] = {}
    for device_id, pairs in payload.items():
        if not isinstance(pairs, list):
            return None
        if not DEVICE_ID_RE.fullmatch(device_id) or not 1 <= len(pairs) <= MAX_IDENTITY_PAIRS_PER_DEVICE:
            continue
        hashed = [_hashed_pair(pair) for pair in pairs]
        if all(item is not None for item in hashed):
            identities[device_id] = tuple(sorted(hashed))
    return identities


def parse_device_info(payload: object) -> dict[str, DeviceInfo] | None:
    """The info template's answer, or None when its shape is wrong: each entry a list of the four values.

    Every value is cleaned (`clean_device_value`), so a device whose four are null still has its entry, with four
    nulls. An entry with a bad device id is dropped alone, as in the device map.
    """
    if not isinstance(payload, dict):
        return None
    infos: dict[str, DeviceInfo] = {}
    for device_id, values in payload.items():
        if not isinstance(values, list) or len(values) != len(FIELDS):
            return None
        if DEVICE_ID_RE.fullmatch(device_id):
            infos[device_id] = clean_device_info(values)
    return infos


class DeviceMapReader:
    """The last good map, read at the first report and then at most every 600 s; a failure never stops a report.

    It is the only cache the connector keeps of what Home Assistant holds, and the one a sync drops (`invalidate`).
    Between two reads, an entity the map never saw makes the next report read it again (`fill_gaps`). Every good map
    read is followed by an identity read; a failed one keeps the identities of the last good one, in memory only.
    While the server asks for the device info (`want_info`), the identity read is followed by an info read, which
    keeps the last good info the same way.
    """

    def __init__(self, context: Context) -> None:
        self._context = context
        self._map: DeviceMap | None = None
        self._read_at: float | None = None
        self._failing = False
        self._invalidated = False
        self._good_reads = 0
        self._identities: dict[str, Identity] = {}
        self._identity_failure_logged = False
        self._info_wanted = False
        self._info: dict[str, DeviceInfo] | None = None
        self._info_failure_logged = False

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

    @property
    def info_wanted(self) -> bool:
        return self._info_wanted

    def want_info(self, wanted: bool) -> None:
        """The server's `device_info_wanted` of the last accepted answer. False drops the info held in memory at once,
        so no report carries it again; a flip to true invalidates the map, so the next report reads the map, the
        identities and the info, and carries the info from that read on."""
        if not wanted:
            self._info_wanted = False
            self._info = None
            if self._map is not None and self._map.info is not None:
                self._map = replace(self._map, info=None)
            return
        if not self._info_wanted:
            self._info_wanted = True
            self.invalidate()

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
        self._refresh_identities()
        if self._info_wanted:
            self._refresh_info()
        self._map = replace(fresh, identities=self._identities, info=self._info)

    def _refresh_identities(self) -> None:
        # The sync ack and `good_reads` follow the device map alone; this read only decides which identities ride on it.
        identities, detail = self._read_identities()
        if identities is not None:
            self._identities = identities
        elif not self._identity_failure_logged:
            self._identity_failure_logged = True
            self._context.log.warning(messages.DEVICE_IDENTITIES_FAILED.format(detail=detail))

    def _refresh_info(self) -> None:
        info, detail = self._read_info()
        if info is not None:
            self._info = info
        elif not self._info_failure_logged:
            self._info_failure_logged = True
            self._context.log.warning(messages.DEVICE_INFO_FAILED.format(detail=detail))

    def _read(self) -> DeviceMap | None:
        try:
            response = self._context.ha.post("/template", {"template": DEVICE_TEMPLATE})
        except TransportError:
            return None
        if response.status != 200:
            return None
        return parse_device_map(json_body(response))

    def _read_identities(self) -> tuple[dict[str, Identity] | None, str]:
        """The identities and an empty detail, or None and a detail with no id and no template output."""
        try:
            response = self._context.ha.post("/template", {"template": IDENTITY_TEMPLATE})
        except TransportError as error:
            return None, error.kind
        if response.status != 200:
            return None, f"HTTP {response.status}"
        identities = parse_identities(json_body(response))
        return (identities, "") if identities is not None else (None, "resposta inválida")

    def _read_info(self) -> tuple[dict[str, DeviceInfo] | None, str]:
        """The device info and an empty detail, or None and a detail with no value and no template output."""
        try:
            response = self._context.ha.post("/template", {"template": INFO_TEMPLATE})
        except TransportError as error:
            return None, error.kind
        if response.status != 200:
            return None, f"HTTP {response.status}"
        info = parse_device_info(json_body(response))
        return (info, "") if info is not None else (None, "resposta inválida")
