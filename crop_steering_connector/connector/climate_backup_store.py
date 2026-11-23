"""The climate reserve's schedule parser and its file, `/data/climate_backup.json`: mode 0600, written atomically, out
of HA backups, at most 262144 bytes, set aside when unreadable.

A schedule is what the Cropilot sends in the `climate_backup` key of an air conditioner's or an in-loop dehumidifier's
allowlist entry (0.2.17): the device's HA device (the pin), its kind, `valid_until`, `takeover_seconds`, the air
conditioner's `mode_exit` and the light transitions of the next 24 hours, each with the period's target and whether it
lights the plants. `parse_schedule` refuses the whole key when anything in it is out of the contract.

The file keeps only the events the Cropilot has not acknowledged: the schedule lives in memory, and after a restart
nothing runs before a contact, which brings the schedule again. An event keeps the instant of its end here; the report
turns it into an age.
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
from typing import Any, Union

from .devices import DEVICE_ID_RE
from .light_store import parse_instant, utc_iso
from .pump_store import EVENT_ID_RE

CLIMATE_FILE_NAME = "climate_backup.json"
CORRUPT_SUFFIX = ".corrupt"
FILE_VERSION = 1
MAX_FILE_BYTES = 262_144
MAX_EVENTS = 256
MAX_EVENT_AGE_SECONDS = 2_592_000
MAX_ATTEMPTS = 99
# The devices one answer may give a schedule to.
MAX_DEVICES = 32

# The parser's bounds: the server sends a takeover of 1200 s and the light transitions of 24 hours.
TAKEOVER_RANGE = (600, 3600)
MAX_STEPS = 8
MIN_STEP_GAP_SECONDS = 1800.0
HORIZON_SECONDS = 86_400.0 + 60.0
HUMIDITY_RANGE = (0, 100)

AIR_CONDITIONER = "air_conditioner"
DEHUMIDIFIER = "dehumidifier"
KIND_ENTITY_RE = {
    AIR_CONDITIONER: re.compile(r"^climate\.[a-z0-9_]{1,200}$"),
    DEHUMIDIFIER: re.compile(r"^humidifier\.[a-z0-9_]{1,200}$"),
}
CLIMATE_ENTITY_RE = re.compile(r"^(climate|humidifier)\.[a-z0-9_]{1,200}$")
SCHEDULE_KEYS = frozenset({"device_id", "kind", "valid_until", "takeover_seconds", "mode_exit", "steps"})
STEP_KEYS = frozenset({"at", "target", "lights_on"})
MODE_EXIT_KEYS = frozenset({"to", "from", "after"})
COOL = "cool"
EXIT_FROM = frozenset({"dry", "heat"})

EVENT_KINDS = frozenset({"applied", "skipped", "failed", "expired"})
REASONS = frozenset({"mode", "state", "invalid_value", "no_anchor", "clock_skew", "unavailable", "service_error"})
TARGET_TEMPERATURE = "target_temperature"
TARGET_HUMIDITY = "target_humidity"
OPERATION_MODE = "operation_mode"
CAPABILITIES = frozenset({TARGET_TEMPERATURE, TARGET_HUMIDITY, OPERATION_MODE})

Value = Union[int, float, str, None]


class InvalidClimateFile(ValueError):
    """The file exists but is not a climate reserve file this version wrote."""


def _is_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int)


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


# The schedule


@dataclass(frozen=True)
class Step:
    """A light transition: from `at`, the period's target, and whether the plants have light in it."""

    at: float
    target: int | float
    lights_on: bool


@dataclass(frozen=True)
class ModeExit:
    """The platform's own `dry` or `heat` the air conditioner may leave for `cool`, never before `after`."""

    source: str
    after: float


@dataclass(frozen=True)
class ClimateSchedule:
    device_id: str
    kind: str
    valid_until: float
    takeover_seconds: int
    mode_exit: ModeExit | None
    steps: tuple[Step, ...]

    @property
    def capability(self) -> str:
        return TARGET_TEMPERATURE if self.kind == AIR_CONDITIONER else TARGET_HUMIDITY

    def in_force(self, moment: float) -> Step:
        """The last step at or before `moment`; the first one when every step is later (the parser never allows it)."""
        found = self.steps[0]
        for step in self.steps:
            if step.at > moment:
                break
            found = step
        return found

    def next_of(self, step: Step) -> Step | None:
        """The step that ends `step`'s period, or None for the last one."""
        later = [item for item in self.steps if item.at > step.at]
        return later[0] if later else None


def parse_schedule(entity_id: str, raw: Any, server_time: float | None) -> ClimateSchedule | None:
    """The `climate_backup` key of an allowlist entry, or None when anything in it is out of the contract: the exact
    keys, a kind that matches the entity's domain, the pin by its pattern, `takeover_seconds` from 600 to 3600,
    `valid_until` after the answer's `server_time` and at most 24 h + 60 s after it, `mode_exit` null or, on an air
    conditioner only, `{to: cool, from: dry|heat, after}`, and 1 to 8 steps strictly ascending at least 1800 s apart,
    the first in force at `server_time` and every one before `valid_until`, each with a target on the kind's scale (a
    number on an air conditioner, an integer 0 to 100 on a dehumidifier) and a boolean `lights_on`."""
    if server_time is None or not isinstance(raw, dict) or set(raw) != SCHEDULE_KEYS:
        return None
    kind = raw["kind"]
    pattern = KIND_ENTITY_RE.get(kind) if isinstance(kind, str) else None
    if pattern is None or not isinstance(entity_id, str) or pattern.fullmatch(entity_id) is None:
        return None
    device_id, takeover = raw["device_id"], raw["takeover_seconds"]
    if not isinstance(device_id, str) or DEVICE_ID_RE.fullmatch(device_id) is None:
        return None
    if not _is_int(takeover) or not TAKEOVER_RANGE[0] <= takeover <= TAKEOVER_RANGE[1]:
        return None
    valid_until = parse_instant(raw["valid_until"])
    if valid_until is None or not server_time < valid_until <= server_time + HORIZON_SECONDS:
        return None
    exit_ok, mode_exit = _mode_exit(raw["mode_exit"], kind)
    steps = _steps(raw["steps"], kind)
    if not exit_ok or steps is None:
        return None
    if steps[0].at > server_time or steps[-1].at >= valid_until:
        return None
    return ClimateSchedule(device_id, kind, valid_until, takeover, mode_exit, steps)


def _mode_exit(raw: Any, kind: str) -> tuple[bool, ModeExit | None]:
    if raw is None:
        return True, None
    if kind != AIR_CONDITIONER or not isinstance(raw, dict) or set(raw) != MODE_EXIT_KEYS:
        return False, None
    after = parse_instant(raw["after"])
    if raw["to"] != COOL or raw["from"] not in EXIT_FROM or after is None:
        return False, None
    return True, ModeExit(raw["from"], after)


def _steps(raw: Any, kind: str) -> tuple[Step, ...] | None:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_STEPS:
        return None
    steps: list[Step] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != STEP_KEYS or not isinstance(item["lights_on"], bool):
            return None
        at, target = parse_instant(item["at"]), item["target"]
        if at is None:
            return None
        if kind == AIR_CONDITIONER and not _is_number(target):
            return None
        if kind == DEHUMIDIFIER and not (_is_int(target) and HUMIDITY_RANGE[0] <= target <= HUMIDITY_RANGE[1]):
            return None
        steps.append(Step(at, target, item["lights_on"]))
    if any(later.at - earlier.at < MIN_STEP_GAP_SECONDS for earlier, later in zip(steps, steps[1:])):
        return None
    return tuple(steps)


# The events and the file


@dataclass(frozen=True)
class ClimateEvent:
    event_id: str
    entity_id: str
    step_at: float
    kind: str
    reason: str | None
    capability: str
    value: Value
    attempts: int
    ended_at: float

    def wire(self, now: float) -> dict:
        """The event as the report sends it: its end as an age, never an instant, and the `at` of its step."""
        return {
            "event_id": self.event_id,
            "entity_id": self.entity_id,
            "step_at": utc_iso(self.step_at),
            "kind": self.kind,
            "reason": self.reason,
            "capability": self.capability,
            "value": self.value,
            "attempts": self.attempts,
            "ended_age_seconds": min(MAX_EVENT_AGE_SECONDS, max(0, int(now - self.ended_at))),
        }


@dataclass
class ClimateFile:
    events: list[ClimateEvent] = field(default_factory=list)


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise InvalidClimateFile(what)


def _is_value(value: Any) -> bool:
    return value is None or value == COOL or _is_number(value)


def _event(raw: Any) -> ClimateEvent:
    _require(isinstance(raw, dict), "event")
    event_id, entity_id, kind, reason = raw.get("event_id"), raw.get("entity_id"), raw.get("kind"), raw.get("reason")
    _require(isinstance(event_id, str) and EVENT_ID_RE.fullmatch(event_id) is not None, "event id")
    _require(isinstance(entity_id, str) and CLIMATE_ENTITY_RE.fullmatch(entity_id) is not None, "event entity")
    _require(kind in EVENT_KINDS, "event kind")
    _require(reason is None or reason in REASONS, "event reason")
    _require(raw.get("capability") in CAPABILITIES and _is_value(raw.get("value")), "event value")
    attempts = raw.get("attempts")
    _require(_is_int(attempts) and 0 <= attempts <= MAX_ATTEMPTS, "event attempts")
    _require(_is_number(raw.get("step_at")) and _is_number(raw.get("ended_at")), "event instants")
    return ClimateEvent(
        event_id=event_id,
        entity_id=entity_id,
        step_at=float(raw["step_at"]),
        kind=kind,
        reason=reason,
        capability=raw["capability"],
        value=raw["value"],
        attempts=attempts,
        ended_at=float(raw["ended_at"]),
    )


def parse(document: Any) -> ClimateFile:
    _require(isinstance(document, dict) and document.get("version") == FILE_VERSION, "version")
    events = document.get("events")
    _require(isinstance(events, list) and len(events) <= MAX_EVENTS, "events")
    return ClimateFile(events=[_event(item) for item in events])


def encode(content: ClimateFile) -> bytes:
    document = {
        "version": FILE_VERSION,
        "events": [
            {
                "event_id": event.event_id,
                "entity_id": event.entity_id,
                "step_at": event.step_at,
                "kind": event.kind,
                "reason": event.reason,
                "capability": event.capability,
                "value": event.value,
                "attempts": event.attempts,
                "ended_at": event.ended_at,
            }
            for event in content.events
        ],
    }
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


class ClimateStore:
    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir
        self._path = data_dir / CLIMATE_FILE_NAME

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> ClimateFile | None:
        """The stored content; an empty one when there is no file, and None when it cannot be read or is invalid."""
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return ClimateFile()
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
        """Keep an unreadable file as `climate_backup.json.corrupt` (over an older copy) before a write replaces it."""
        with contextlib.suppress(OSError):
            os.replace(self._path, self._path.with_name(CLIMATE_FILE_NAME + CORRUPT_SUFFIX))

    def write(self, data: bytes) -> None:
        """Write the encoded content through a private temporary file in the same folder, then rename it over the old
        one."""
        descriptor, temporary = tempfile.mkstemp(prefix=f"{CLIMATE_FILE_NAME}.", suffix=".tmp", dir=self._dir)
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
