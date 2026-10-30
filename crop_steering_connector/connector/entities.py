"""The report: which HA states leave the house, with which attributes, and how it fits the limits."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any, Iterable, Mapping

ENTITY_ID_RE = re.compile(r"^(climate|humidifier|number|sensor|switch)\.[a-z0-9_]{1,200}$")
# The plug meters leave beside the climate sensors; current, voltage and the rest stay home.
SENSOR_DEVICE_CLASSES = frozenset({"temperature", "humidity", "power", "energy"})
# A plug's own pulse duration; no other `number` leaves the house.
NUMBER_DEVICE_CLASS = "duration"
KEPT_ATTRIBUTES = (
    "friendly_name",
    "device_class",
    "unit_of_measurement",
    "state_class",
    "hvac_modes",
    "supported_features",
    "current_temperature",
    "current_humidity",
    "humidity",
    # The dehumidifier's writable range. An odd step still leaves as it is: the server closes the
    # target on it, and dropping it here would read as absent.
    "min_humidity",
    "max_humidity",
    "target_humidity_step",
    "temperature",
    "min_temp",
    "max_temp",
    "target_temp_step",
)
# Only a `number` keeps its range: the dehumidifier's `mode` and the like stay home.
NUMBER_ATTRIBUTES = ("min", "max", "step", "mode")
MAX_REPORT_BYTES = 61440
MAX_REPORT_ENTITIES = 200
MAX_AGE_SECONDS = 2_592_000
# Beside every `climate`, the sensors the refresh (refresh.py) asks HA to write again.
REFRESHED_SENSOR_CLASSES = frozenset({"temperature", "humidity"})
# A climate's value: the readings the server takes from it, not its target, mode or fan.
CLIMATE_READINGS = ("current_temperature", "current_humidity")
# The first dropped when the report must shrink; the allowlisted entities go only when nothing else is left.
DROP_ORDER = ("number", "switch", "sensor", "humidifier", "climate")


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def _json_safe(value: Any) -> bool:
    if value is None or isinstance(value, (bool, str)):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_safe(item) for item in value)
    return False


def _instant(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.timestamp()


def _clamped_age(now: float, moment: float) -> int:
    return int(min(MAX_AGE_SECONDS, max(0.0, now - moment)))


def is_refreshed(state: Any) -> bool:
    """A `climate`, or a temperature or humidity `sensor`, by HA's own state: the kinds the refresh may ask for."""
    if not isinstance(state, dict):
        return False
    entity_id = state.get("entity_id")
    if not isinstance(entity_id, str) or not ENTITY_ID_RE.fullmatch(entity_id):
        return False
    domain = _domain(entity_id)
    if domain == "climate":
        return True
    if domain != "sensor":
        return False
    attributes = state.get("attributes")
    device_class = attributes.get("device_class") if isinstance(attributes, dict) else None
    return isinstance(device_class, str) and device_class in REFRESHED_SENSOR_CLASSES


def written_age_seconds(state: dict, now: float) -> int | None:
    """Seconds since HA last wrote this state (`last_reported`, both on the VM clock), or None without it."""
    for key in ("last_reported", "last_updated", "last_changed"):
        moment = _instant(state.get(key))
        if moment is not None:
            return _clamped_age(now, moment)
    return None


def value_age_seconds(state: dict, now: float, changed_at: float | None = None) -> int | None:
    """Seconds since the value of a refreshed kind last changed, or None for another kind or without the time.

    Some integrations answer `update_entity` by writing their cached value again, which moves `last_reported` and
    keeps a dead sensor fresh. The report sends this age beside the written one so the app can flag a value that has
    not changed; the reading itself keeps its written age and is never dropped for it. `last_updated`, not
    `last_changed`: a climate's reading is an attribute (`current_temperature`), and only `last_updated` moves with
    an attribute; `last_changed` follows the hvac mode alone. A climate's `changed_at` (`ClimateReadings`) replaces
    `last_updated`, which also moves with its target or mode; the key still needs a `last_updated`.
    """
    if not is_refreshed(state):
        return None
    changed = _instant(state.get("last_updated"))
    if changed is None:
        return None
    return _clamped_age(now, changed if changed_at is None else changed_at)


def _readings(state: dict) -> tuple:
    attributes = state.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}
    return tuple(attributes.get(key) for key in CLIMATE_READINGS)


class ClimateReadings:
    """When each climate's readings last changed, as this process saw them in /states.

    HA keeps no time per attribute, and a climate's `last_updated` moves with any of them: the Cropilot's own
    `set_temperature` made a stuck reading look fresh. At a climate's first sight, and whenever a reading differs
    from the last /states, the change time is `last_updated`; while the readings stay equal, the earlier time stands.
    """

    def __init__(self) -> None:
        self._seen: dict[str, tuple[tuple, float]] = {}

    def observe(self, states: Iterable[Any]) -> dict[str, float]:
        """Entity id → change time for every climate of these states with a `last_updated`; the others are forgotten."""
        seen: dict[str, tuple[tuple, float]] = {}
        for state in states:
            if not is_refreshed(state) or _domain(state["entity_id"]) != "climate":
                continue
            entity_id = state["entity_id"]
            updated = _instant(state.get("last_updated"))
            if updated is None:
                continue
            readings = _readings(state)
            previous = self._seen.get(entity_id)
            same = previous is not None and previous[0] == readings
            seen[entity_id] = (readings, min(previous[1], updated) if same else updated)
        self._seen = seen
        return {entity_id: changed_at for entity_id, (_, changed_at) in seen.items()}


def report_entity(
    state: Any,
    now: float,
    devices: Mapping[str, str | None] | None = None,
    value_changed: Mapping[str, float] | None = None,
) -> dict | None:
    """One HA state as the report sends it, or None when it does not leave the house.

    With a device map, an entity in it carries `device_id`, null when it has no device in HA; one absent
    from it carries no key, so the server keeps the stored id: a null there would clear a plug's compressor
    protection until the next map read. `value_changed` is `ClimateReadings.observe` of the same states.
    """
    if not isinstance(state, dict):
        return None
    entity_id = state.get("entity_id")
    if not isinstance(entity_id, str) or not ENTITY_ID_RE.fullmatch(entity_id):
        return None
    raw_attributes = state.get("attributes")
    if not isinstance(raw_attributes, dict):
        raw_attributes = {}
    domain = _domain(entity_id)
    if domain == "sensor" and raw_attributes.get("device_class") not in SENSOR_DEVICE_CLASSES:
        return None
    if domain == "number" and raw_attributes.get("device_class") != NUMBER_DEVICE_CLASS:
        return None
    value = state.get("state")
    if not isinstance(value, str):
        return None
    age = written_age_seconds(state, now)
    if age is None:
        return None
    kept = KEPT_ATTRIBUTES + NUMBER_ATTRIBUTES if domain == "number" else KEPT_ATTRIBUTES
    attributes = {key: raw_attributes[key] for key in kept if key in raw_attributes and _json_safe(raw_attributes[key])}
    entity: dict = {"entity_id": entity_id, "state": value, "age_seconds": age}
    value_age = value_age_seconds(state, now, value_changed.get(entity_id) if value_changed is not None else None)
    if value_age is not None:
        entity["value_age_seconds"] = value_age
    if devices is not None and entity_id in devices:
        entity["device_id"] = devices[entity_id]
    entity["attributes"] = attributes
    return entity


def report_entities(
    states: Iterable[Any],
    now: float,
    devices: Mapping[str, str | None] | None = None,
    value_changed: Mapping[str, float] | None = None,
) -> list[dict]:
    selected: dict[str, dict] = {}
    for state in states:
        entity = report_entity(state, now, devices, value_changed)
        if entity is not None:
            selected[entity["entity_id"]] = entity
    return [selected[key] for key in sorted(selected)]


def _encode(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _drop_rank(entity_id: str, allowlisted: bool) -> tuple:
    # Sorted ascending, the first item is the first to go.
    return (allowlisted, DROP_ORDER.index(_domain(entity_id)), tuple(-ord(char) for char in entity_id))


def _body(envelope: dict, entities: list[dict], devices: dict[str, str] | None) -> dict:
    body = {**envelope, "entities": entities}
    if devices is not None:
        body["devices"] = devices
    return body


def _named_device(entity: dict, device_names: Mapping[str, str]) -> str | None:
    device_id = entity.get("device_id")
    return device_id if isinstance(device_id, str) and device_id in device_names else None


def build_report(
    envelope: dict,
    entities: list[dict],
    allowlisted: frozenset[str],
    restriction: int = 0,
    max_bytes: int = MAX_REPORT_BYTES,
    max_entities: int = MAX_REPORT_ENTITIES,
    device_names: Mapping[str, str] | None = None,
) -> tuple[bytes, int, int]:
    """Encode the report within the limits; returns (body, entities sent, entities left out).

    `restriction` 0 sends everything that fits; 1 only the allowlisted entities (after a 413); every step
    above halves the allowlisted entities again. With `device_names` (a device map was read) the
    body carries `devices`, right after `entities`: each named device of the entities sent, once. A device's
    entry counts while one kept entity points to it and leaves with the last one.
    """
    candidates = list(entities)
    if restriction >= 1:
        candidates = [entity for entity in candidates if entity["entity_id"] in allowlisted]
    target = len(candidates)
    for _ in range(max(0, restriction - 1)):
        target //= 2
    names = device_names if device_names is not None else {}
    sizes = {entity["entity_id"]: len(_encode(entity)) for entity in candidates}
    device_of = {entity["entity_id"]: _named_device(entity, names) for entity in candidates}
    holders: dict[str, int] = {}
    for device_id in device_of.values():
        if device_id is not None:
            holders[device_id] = holders.get(device_id, 0) + 1
    # `"<id>":"<name>"`, the encoded one-entry object without its braces.
    device_sizes = {device_id: len(_encode({device_id: names[device_id]})) - 2 for device_id in holders}
    base = len(_encode(_body(envelope, [], {} if device_names is not None else None)))
    kept = {entity["entity_id"] for entity in candidates}
    total = base + sum(sizes.values()) + max(0, len(kept) - 1)
    total += sum(device_sizes.values()) + max(0, len(holders) - 1)
    for entity_id in sorted(kept, key=lambda item: _drop_rank(item, item in allowlisted)):
        if len(kept) <= min(target, max_entities) and total <= max_bytes:
            break
        kept.discard(entity_id)
        total -= sizes[entity_id] + (1 if kept else 0)
        device_id = device_of[entity_id]
        if device_id is not None:
            holders[device_id] -= 1
            if holders[device_id] == 0:
                del holders[device_id]
                total -= device_sizes[device_id] + (1 if holders else 0)
    sent = [entity for entity in candidates if entity["entity_id"] in kept]
    devices = None
    if device_names is not None:
        devices = {}
        for entity in sent:
            device_id = device_of[entity["entity_id"]]
            if device_id is not None and device_id not in devices:
                devices[device_id] = names[device_id]
    body = _encode(_body(envelope, sent, devices))
    if len(body) > max_bytes:
        raise ValueError("report envelope does not fit the size limit")
    return body, len(sent), len(entities) - len(sent)
