"""The backup watering's schedule parser and its file, `/data/pump_backup.json`: mode 0600, written atomically, out of HA
backups, at most 262144 bytes, set aside when unreadable.

A schedule is what the Cropilot sends in the `pump_backup` key of a pump's allowlist entry: the pump's HA device (the
pin), `valid_until`, `takeover_seconds`, the plug's own pulse (`plug_pulse`), the grid fingerprint of each light day and
the automatic slots of the next 24 hours, each with the dose of each pulse. `parse_schedule` refuses the whole key when
anything in it is out of the contract.

The file keeps only what a restart needs: the slots decided in the last 48 hours (run or skipped, so no slot runs
twice, also across restarts), the pulse in flight of each pump (its key, its start and its dose, with what its event
echoes) and the events the Cropilot has not acknowledged. The schedules are never written: after a restart nothing
runs before a contact, and that contact brings the schedule again. An event keeps the instant of its end here; the
report turns it into an age.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .devices import DEVICE_ID_RE
from .light_store import parse_instant, utc_iso
from .pump_store import (
    EVENT_ID_RE,
    MAX_ON_SECONDS_RANGE,
    MAX_PULSE_INDEX,
    MAX_SLOT_INDEX,
    BackupKey,
    is_light_date,
    is_max_on,
)

BACKUP_FILE_NAME = "pump_backup.json"
CORRUPT_SUFFIX = ".corrupt"
FILE_VERSION = 1
MAX_FILE_BYTES = 262_144
MAX_EVENTS = 256
MAX_DECIDED = 2048
DECIDED_KEEP_SECONDS = 48 * 3600.0
MAX_EVENT_AGE_SECONDS = 2_592_000
MAX_ATTEMPTS = 99
# The pumps one answer may give a schedule to; the report's `pump_backup` is sized for them.
MAX_PUMPS = 32

# The parser's bounds, sized for 24 hours of slots.
MAX_SLOTS = 64
MAX_PULSES_PER_SLOT = 6
MIN_PULSE_SECONDS = 5
# Every dose stays below the plug's pulse, P = max_on_seconds - 15: at most P - 1.
DOSE_BELOW_MAX_ON_SECONDS = 16
PLUG_PULSE_BELOW_MAX_ON_SECONDS = 15
MAX_SCHEDULE_SECONDS = 14_400
MIN_SLOT_GAP_SECONDS = 120.0
TAKEOVER_RANGE = (300, 1800)
HORIZON_SECONDS = 86_400.0 + 60.0
MAX_DAYS = 8

# Only a plug is a backup pump: a valve stays out of the dose cut, so out of the backup.
BACKUP_PUMP_RE = re.compile(r"^switch\.[a-z0-9_]{1,200}$")
NUMBER_ENTITY_RE = re.compile(r"^number\.[a-z0-9_]{1,200}$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16}$")
SCHEDULE_KEYS = frozenset({"device_id", "valid_until", "takeover_seconds", "plug_pulse", "days", "slots"})
PLUG_PULSE_KEYS = frozenset({"entity_id", "seconds"})
DAY_KEYS = frozenset({"light_date", "fingerprint"})
SLOT_KEYS = frozenset({"light_date", "slot_index", "slot_at", "pulses"})

# The watch's ends of a backup pulse, and the backup's own outcomes.
WATCH_KINDS = frozenset({"ended", "forced_off", "boot_off", "off_failed"})
EVENT_KINDS = WATCH_KINDS | {"failed", "skipped"}
FAILED_REASONS = frozenset({"ha_rejected", "ha_unreachable", "ha_error"})
SKIPPED_REASONS = frozenset({"late", "pulse_mismatch", "clock_skew", "no_anchor", "pump_busy"})


class InvalidBackupFile(ValueError):
    """The file exists but is not a backup file this version wrote."""


def _is_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int)


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


# The schedule


@dataclass(frozen=True)
class Slot:
    light_date: str
    slot_index: int
    slot_at: float
    pulses: tuple[int, ...]

    def key(self, pulse_index: int) -> BackupKey:
        return (self.light_date, self.slot_index, pulse_index)


@dataclass(frozen=True)
class BackupSchedule:
    device_id: str
    valid_until: float
    takeover_seconds: int
    plug_entity_id: str
    plug_seconds: int
    max_on_seconds: int
    days: tuple[tuple[str, str], ...]
    slots: tuple[Slot, ...]

    def fingerprint(self, light_date: str) -> str:
        return dict(self.days)[light_date]


def parse_schedule(raw: Any, max_on_seconds: Any, server_time: float | None) -> BackupSchedule | None:
    """The `pump_backup` key of a pump entry, or None when anything in it is out of the contract: the exact keys, the
    pin by its pattern, `takeover_seconds` from 300 to 1800, `plug_pulse` a `number` with P = max_on_seconds - 15,
    `days` one fingerprint per light day covering every slot, 1 to 64 slots strictly ascending at least 120 s apart
    with a unique key, 1 to 6 pulses each from 5 s to max_on_seconds - 16, at most 14400 s in all, and `valid_until`
    after the answer's `server_time`, not before the last slot and at most 24 h + 60 s after the answer."""
    if server_time is None or not is_max_on(max_on_seconds):
        return None
    if not isinstance(raw, dict) or set(raw) != SCHEDULE_KEYS:
        return None
    device_id, takeover, plug = raw["device_id"], raw["takeover_seconds"], raw["plug_pulse"]
    if not isinstance(device_id, str) or DEVICE_ID_RE.fullmatch(device_id) is None:
        return None
    if not _is_int(takeover) or not TAKEOVER_RANGE[0] <= takeover <= TAKEOVER_RANGE[1]:
        return None
    if not isinstance(plug, dict) or set(plug) != PLUG_PULSE_KEYS:
        return None
    plug_entity, plug_seconds = plug["entity_id"], plug["seconds"]
    if not isinstance(plug_entity, str) or NUMBER_ENTITY_RE.fullmatch(plug_entity) is None:
        return None
    if not _is_int(plug_seconds) or plug_seconds != max_on_seconds - PLUG_PULSE_BELOW_MAX_ON_SECONDS:
        return None
    days = _days(raw["days"])
    slots = _slots(raw["slots"], max_on_seconds)
    if days is None or slots is None:
        return None
    if not {slot.light_date for slot in slots} <= {light_date for light_date, _ in days}:
        return None
    valid_until = parse_instant(raw["valid_until"])
    if valid_until is None or not server_time < valid_until <= server_time + HORIZON_SECONDS:
        return None
    if slots[-1].slot_at > valid_until:
        return None
    return BackupSchedule(
        device_id=device_id,
        valid_until=valid_until,
        takeover_seconds=takeover,
        plug_entity_id=plug_entity,
        plug_seconds=plug_seconds,
        max_on_seconds=max_on_seconds,
        days=days,
        slots=slots,
    )


def _days(raw: Any) -> tuple[tuple[str, str], ...] | None:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_DAYS:
        return None
    days = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != DAY_KEYS:
            return None
        light_date, fingerprint = item["light_date"], item["fingerprint"]
        if not is_light_date(light_date) or not isinstance(fingerprint, str) or not FINGERPRINT_RE.fullmatch(fingerprint):
            return None
        days.append((light_date, fingerprint))
    dates = [light_date for light_date, _ in days]
    if any(later <= earlier for earlier, later in zip(dates, dates[1:])):
        return None
    return tuple(days)


def _slots(raw: Any, max_on_seconds: int) -> tuple[Slot, ...] | None:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SLOTS:
        return None
    highest = max_on_seconds - DOSE_BELOW_MAX_ON_SECONDS
    slots: list[Slot] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != SLOT_KEYS:
            return None
        light_date, slot_index, pulses = item["light_date"], item["slot_index"], item["pulses"]
        if not is_light_date(light_date) or not _is_int(slot_index) or not 0 <= slot_index <= MAX_SLOT_INDEX:
            return None
        if not isinstance(pulses, list) or not 1 <= len(pulses) <= MAX_PULSES_PER_SLOT:
            return None
        if not all(_is_int(dose) and MIN_PULSE_SECONDS <= dose <= highest for dose in pulses):
            return None
        slot_at = parse_instant(item["slot_at"])
        if slot_at is None:
            return None
        slots.append(Slot(light_date, slot_index, slot_at, tuple(pulses)))
    if any(later.slot_at - earlier.slot_at < MIN_SLOT_GAP_SECONDS for earlier, later in zip(slots, slots[1:])):
        return None
    if len({(slot.light_date, slot.slot_index) for slot in slots}) != len(slots):
        return None
    if sum(sum(slot.pulses) for slot in slots) > MAX_SCHEDULE_SECONDS:
        return None
    return tuple(slots)


# The file


@dataclass(frozen=True)
class InFlight:
    """A backup pulse from just before its HA call until the watch's end of it."""

    light_date: str
    slot_index: int
    pulse_index: int
    slot_at: float
    pulse_count: int
    fingerprint: str
    dose_seconds: int
    started_at: float

    @property
    def key(self) -> BackupKey:
        return (self.light_date, self.slot_index, self.pulse_index)


@dataclass(frozen=True)
class BackupEvent:
    event_id: str
    entity_id: str
    light_date: str
    slot_index: int
    slot_at: float
    pulse_index: int
    pulse_count: int
    fingerprint: str
    kind: str
    reason: str | None
    dose_seconds: int
    on_seconds: int | None
    ended_at: float
    attempts: int

    def wire(self, now: float) -> dict:
        """The event as the report sends it: its end as an age, never an instant, and the `slot_at` it received."""
        return {
            "event_id": self.event_id,
            "entity_id": self.entity_id,
            "light_date": self.light_date,
            "slot_index": self.slot_index,
            "slot_at": utc_iso(self.slot_at),
            "pulse_index": self.pulse_index,
            "pulse_count": self.pulse_count,
            "fingerprint": self.fingerprint,
            "kind": self.kind,
            "reason": self.reason,
            "dose_seconds": self.dose_seconds,
            "on_seconds": self.on_seconds,
            "ended_age_seconds": min(MAX_EVENT_AGE_SECONDS, max(0, int(now - self.ended_at))),
            "attempts": self.attempts,
        }


@dataclass
class BackupFile:
    # Per pump, the slots decided: (light date, slot index) -> slot_at.
    decided: dict[str, dict[tuple[str, int], float]] = field(default_factory=dict)
    in_flight: dict[str, InFlight] = field(default_factory=dict)
    events: list[BackupEvent] = field(default_factory=list)


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise InvalidBackupFile(what)


def _decided(raw: Any) -> dict[str, dict[tuple[str, int], float]]:
    _require(isinstance(raw, dict), "decided")
    decided: dict[str, dict[tuple[str, int], float]] = {}
    count = 0
    for entity_id, items in raw.items():
        _require(BACKUP_PUMP_RE.fullmatch(entity_id) is not None and isinstance(items, list), "decided entity")
        slots: dict[tuple[str, int], float] = {}
        for item in items:
            _require(isinstance(item, list) and len(item) == 3, "decided slot")
            light_date, slot_index, slot_at = item
            _require(is_light_date(light_date), "decided light_date")
            _require(_is_int(slot_index) and 0 <= slot_index <= MAX_SLOT_INDEX, "decided slot_index")
            _require(_is_number(slot_at), "decided slot_at")
            slots[(light_date, slot_index)] = float(slot_at)
        count += len(slots)
        decided[entity_id] = slots
    _require(count <= MAX_DECIDED, "decided count")
    return decided


def _key_fields(raw: dict) -> None:
    _require(is_light_date(raw.get("light_date")), "light_date")
    slot_index, pulse_index = raw.get("slot_index"), raw.get("pulse_index")
    _require(_is_int(slot_index) and 0 <= slot_index <= MAX_SLOT_INDEX, "slot_index")
    _require(_is_int(pulse_index) and 0 <= pulse_index <= MAX_PULSE_INDEX, "pulse_index")
    pulse_count = raw.get("pulse_count")
    _require(_is_int(pulse_count) and pulse_index < pulse_count <= MAX_PULSES_PER_SLOT, "pulse_count")
    _require(_is_number(raw.get("slot_at")), "slot_at")
    fingerprint = raw.get("fingerprint")
    _require(isinstance(fingerprint, str) and FINGERPRINT_RE.fullmatch(fingerprint) is not None, "fingerprint")
    dose = raw.get("dose_seconds")
    highest = MAX_ON_SECONDS_RANGE[1] - DOSE_BELOW_MAX_ON_SECONDS
    _require(_is_int(dose) and MIN_PULSE_SECONDS <= dose <= highest, "dose_seconds")


def _in_flight(raw: Any) -> dict[str, InFlight]:
    _require(isinstance(raw, dict) and len(raw) <= MAX_PUMPS, "in_flight")
    flights: dict[str, InFlight] = {}
    for entity_id, item in raw.items():
        _require(BACKUP_PUMP_RE.fullmatch(entity_id) is not None and isinstance(item, dict), "in_flight entity")
        _key_fields(item)
        _require(_is_number(item.get("started_at")), "started_at")
        flights[entity_id] = InFlight(
            light_date=item["light_date"],
            slot_index=item["slot_index"],
            pulse_index=item["pulse_index"],
            slot_at=float(item["slot_at"]),
            pulse_count=item["pulse_count"],
            fingerprint=item["fingerprint"],
            dose_seconds=item["dose_seconds"],
            started_at=float(item["started_at"]),
        )
    return flights


def _event(raw: Any) -> BackupEvent:
    _require(isinstance(raw, dict), "event")
    event_id, entity_id, kind, reason = raw.get("event_id"), raw.get("entity_id"), raw.get("kind"), raw.get("reason")
    _require(isinstance(event_id, str) and EVENT_ID_RE.fullmatch(event_id) is not None, "event id")
    _require(isinstance(entity_id, str) and BACKUP_PUMP_RE.fullmatch(entity_id) is not None, "event entity")
    _key_fields(raw)
    _require(kind in EVENT_KINDS, "event kind")
    if kind == "failed":
        _require(reason in FAILED_REASONS, "event reason")
    elif kind == "skipped":
        _require(reason in SKIPPED_REASONS, "event reason")
    else:
        _require(reason is None, "event reason")
    on_seconds, attempts = raw.get("on_seconds"), raw.get("attempts")
    _require(on_seconds is None or (_is_int(on_seconds) and on_seconds >= 0), "event on_seconds")
    _require(_is_int(attempts) and 0 <= attempts <= MAX_ATTEMPTS, "event attempts")
    _require(_is_number(raw.get("ended_at")), "event ended_at")
    return BackupEvent(
        event_id=event_id,
        entity_id=entity_id,
        light_date=raw["light_date"],
        slot_index=raw["slot_index"],
        slot_at=float(raw["slot_at"]),
        pulse_index=raw["pulse_index"],
        pulse_count=raw["pulse_count"],
        fingerprint=raw["fingerprint"],
        kind=kind,
        reason=reason,
        dose_seconds=raw["dose_seconds"],
        on_seconds=on_seconds,
        ended_at=float(raw["ended_at"]),
        attempts=attempts,
    )


def parse(document: Any) -> BackupFile:
    _require(isinstance(document, dict) and document.get("version") == FILE_VERSION, "version")
    events = document.get("events")
    _require(isinstance(events, list) and len(events) <= MAX_EVENTS, "events")
    return BackupFile(
        decided=_decided(document.get("decided")),
        in_flight=_in_flight(document.get("in_flight")),
        events=[_event(item) for item in events],
    )


def _flight_document(flight: InFlight) -> dict:
    return {
        "light_date": flight.light_date,
        "slot_index": flight.slot_index,
        "pulse_index": flight.pulse_index,
        "slot_at": flight.slot_at,
        "pulse_count": flight.pulse_count,
        "fingerprint": flight.fingerprint,
        "dose_seconds": flight.dose_seconds,
        "started_at": flight.started_at,
    }


def _event_document(event: BackupEvent) -> dict:
    return {
        "event_id": event.event_id,
        "entity_id": event.entity_id,
        "light_date": event.light_date,
        "slot_index": event.slot_index,
        "slot_at": event.slot_at,
        "pulse_index": event.pulse_index,
        "pulse_count": event.pulse_count,
        "fingerprint": event.fingerprint,
        "kind": event.kind,
        "reason": event.reason,
        "dose_seconds": event.dose_seconds,
        "on_seconds": event.on_seconds,
        "ended_at": event.ended_at,
        "attempts": event.attempts,
    }


def serialize(content: BackupFile) -> dict:
    return {
        "version": FILE_VERSION,
        "decided": {
            entity_id: [[light_date, slot_index, slot_at] for (light_date, slot_index), slot_at in sorted(slots.items())]
            for entity_id, slots in sorted(content.decided.items())
            if slots
        },
        "in_flight": {entity_id: _flight_document(flight) for entity_id, flight in sorted(content.in_flight.items())},
        "events": [_event_document(event) for event in content.events],
    }


def encode(content: BackupFile) -> bytes:
    return json.dumps(serialize(content), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


class BackupStore:
    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir
        self._path = data_dir / BACKUP_FILE_NAME

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> BackupFile | None:
        """The stored content; an empty one when there is no file, and None when it cannot be read or is invalid."""
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return BackupFile()
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        if stat.S_IMODE(info.st_mode) != 0o600:
            with contextlib.suppress(OSError):
                os.chmod(self._path, 0o600)
        try:
            with self._path.open("rb") as handle:
                raw = handle.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                return None
            return parse(json.loads(raw.decode("utf-8")))
        except (OSError, UnicodeDecodeError, ValueError):
            return None

    def set_aside(self) -> None:
        """Keep an unreadable file as `pump_backup.json.corrupt` (over an older copy) before a write replaces it."""
        with contextlib.suppress(OSError):
            os.replace(self._path, self._path.with_name(BACKUP_FILE_NAME + CORRUPT_SUFFIX))

    def write(self, data: bytes) -> None:
        """Write the encoded content through a private temporary file in the same folder, then rename it over the old
        one."""
        descriptor, temporary = tempfile.mkstemp(prefix=f"{BACKUP_FILE_NAME}.", suffix=".tmp", dir=self._dir)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
        with contextlib.suppress(OSError):
            directory = os.open(self._dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
