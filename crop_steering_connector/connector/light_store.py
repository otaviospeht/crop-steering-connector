"""The light controller's schedules and its file, `/data/light_control.json`: mode 0600, written atomically, out of HA
backups.

A schedule is what the Cropilot sends in the `light` marker of a light plug's allowlist entry: absolute instants
(`at`, `state`), strictly ascending and at least 30 minutes apart, and the `valid_until` that ends it. The marker
also pins the plug's HA device (`device_id`) and says whether the plug answers only through the cloud (`cloud_only`).
`parse_marker` and the file reader apply the same bounds, so the file never holds a schedule an answer could not.

The file holds, per light plug (at most 32), the schedule, the pinned device, `cloud_only`, the last transition
applied (`applied`: null before the first, `void` after the clock went back) and the «on» armed ahead (`armed`); the
controller's last time, and the events not yet accepted by the Cropilot. An event keeps the instant of its end here;
the report turns it into an age.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Union

from .devices import DEVICE_ID_RE
from .pump_store import EVENT_ID_RE

LIGHT_FILE_NAME = "light_control.json"
CORRUPT_SUFFIX = ".corrupt"
FILE_VERSION = 1
MAX_ENTITIES = 32
MAX_EVENTS = 20
MAX_TRANSITIONS = 20
MIN_GAP_SECONDS = 1800.0
# `valid_until` at most 9 days after the first instant: the period in force plus the 7 days of the horizon.
MAX_SPAN_SECONDS = 9 * 86400.0
MAX_FILE_BYTES = 262_144
MAX_INSTANT_TEXT = 64
MAX_ATTEMPTS = 99
MAX_EVENT_AGE_SECONDS = 2_592_000
LIGHT_ENTITY_RE = re.compile(r"^switch\.[a-z0-9_]{1,200}$")
STATES = ("on", "off")
EVENT_KINDS = frozenset({"switched", "night_off", "failed", "expired"})
VOID = "void"
MARKER_KEYS = frozenset({"device_id", "cloud_only", "transitions", "valid_until"})
TRANSITION_KEYS = frozenset({"at", "state"})


class InvalidLightFile(ValueError):
    """The file exists but is not a light file this version wrote."""


def utc_iso(moment: float) -> str:
    """An instant as the contract writes it: ISO 8601, whole seconds, `+00:00`."""
    return datetime.fromtimestamp(int(moment), tz=timezone.utc).isoformat()


def parse_instant(value: Any) -> float | None:
    """An ISO 8601 instant with an offset, or None: one without an offset is never read as local time."""
    if not isinstance(value, str) or len(value) > MAX_INSTANT_TEXT:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    try:
        instant = moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None
    return instant if math.isfinite(instant) else None


@dataclass(frozen=True)
class Transition:
    at: float
    state: str


@dataclass(frozen=True)
class Schedule:
    transitions: tuple[Transition, ...]
    valid_until: float

    def current(self, moment: float) -> Transition | None:
        """The last transition at or before `moment`, or None when every instant is later."""
        found = None
        for transition in self.transitions:
            if transition.at > moment:
                break
            found = transition
        return found

    def has(self, transition: Transition) -> bool:
        return transition in self.transitions

    def on_at(self, at: float) -> bool:
        return Transition(at, "on") in self.transitions

    def first_on_after(self, moment: float) -> float | None:
        return next((item.at for item in self.transitions if item.state == "on" and item.at > moment), None)

    def on_after(self, at: float) -> bool:
        """Whether an «on» follows the instant `at` in this schedule: the dark period that `at` opens ends."""
        return self.first_on_after(at) is not None


@dataclass(frozen=True)
class Marker:
    device_id: str
    cloud_only: bool
    schedule: Schedule


Applied = Union[Transition, str, None]


@dataclass
class HeldLight:
    """One light plug's schedule as the controller holds it."""

    schedule: Schedule
    device_id: str
    cloud_only: bool
    applied: Applied = None
    armed: float | None = None


@dataclass(frozen=True)
class LightEvent:
    event_id: str
    entity_id: str
    kind: str
    state: str
    transition_at: float
    attempts: int
    ended_at: float

    def wire(self, now: float) -> dict:
        """The event as the report sends it: its end as an age, never an instant, so a wrong clock moves nothing."""
        return {
            "event_id": self.event_id,
            "entity_id": self.entity_id,
            "kind": self.kind,
            "state": self.state,
            "transition_at": utc_iso(self.transition_at),
            "attempts": self.attempts,
            "ended_seconds_ago": min(MAX_EVENT_AGE_SECONDS, max(0, int(now - self.ended_at))),
        }


@dataclass
class LightFile:
    entities: dict[str, HeldLight] = field(default_factory=dict)
    last_time: float | None = None
    events: list[LightEvent] = field(default_factory=list)


def _is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def valid_schedule(transitions: tuple[Transition, ...], valid_until: float) -> bool:
    """The bounds of a schedule: 1 to 20 transitions, each `on` or `off`, ascending at least 1800 s apart, none after
    `valid_until`, and `valid_until` at most 9 days after the first."""
    if not 1 <= len(transitions) <= MAX_TRANSITIONS:
        return False
    if any(item.state not in STATES for item in transitions):
        return False
    if any(later.at - earlier.at < MIN_GAP_SECONDS for earlier, later in zip(transitions, transitions[1:])):
        return False
    return transitions[-1].at <= valid_until <= transitions[0].at + MAX_SPAN_SECONDS


def parse_marker(raw: Any) -> Marker | None:
    """The `light` marker of an allowlist entry, or None when anything in it is out of the contract."""
    if not isinstance(raw, dict) or set(raw) != MARKER_KEYS:
        return None
    device_id, cloud_only, items = raw["device_id"], raw["cloud_only"], raw["transitions"]
    if not isinstance(device_id, str) or not DEVICE_ID_RE.fullmatch(device_id):
        return None
    if not isinstance(cloud_only, bool):
        return None
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_TRANSITIONS:
        return None
    transitions = []
    for item in items:
        if not isinstance(item, dict) or set(item) != TRANSITION_KEYS or item["state"] not in STATES:
            return None
        at = parse_instant(item["at"])
        if at is None:
            return None
        transitions.append(Transition(at, item["state"]))
    valid_until = parse_instant(raw["valid_until"])
    if valid_until is None or not valid_schedule(tuple(transitions), valid_until):
        return None
    return Marker(device_id, cloud_only, Schedule(tuple(transitions), valid_until))


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise InvalidLightFile(what)


def _transition(raw: Any) -> Transition:
    _require(isinstance(raw, dict) and set(raw) == TRANSITION_KEYS, "transition")
    _require(_is_number(raw["at"]) and raw["state"] in STATES, "transition value")
    return Transition(float(raw["at"]), raw["state"])


def _held(raw: Any) -> HeldLight:
    _require(isinstance(raw, dict), "entity")
    schedule = raw.get("schedule")
    _require(isinstance(schedule, dict) and isinstance(schedule.get("transitions"), list), "schedule")
    transitions = tuple(_transition(item) for item in schedule["transitions"])
    valid_until = schedule.get("valid_until")
    _require(_is_number(valid_until) and valid_schedule(transitions, float(valid_until)), "schedule bounds")
    device_id, cloud_only = raw.get("device_id"), raw.get("cloud_only")
    _require(isinstance(device_id, str) and DEVICE_ID_RE.fullmatch(device_id) is not None, "device_id")
    _require(isinstance(cloud_only, bool), "cloud_only")
    applied_raw = raw.get("applied")
    applied: Applied = None
    if applied_raw == VOID:
        applied = VOID
    elif applied_raw is not None:
        applied = _transition(applied_raw)
    armed = raw.get("armed")
    _require(armed is None or _is_number(armed), "armed")
    return HeldLight(
        schedule=Schedule(transitions, float(valid_until)),
        device_id=device_id,
        cloud_only=cloud_only,
        applied=applied,
        armed=None if armed is None else float(armed),
    )


def _event(raw: Any) -> LightEvent:
    _require(isinstance(raw, dict), "event")
    event_id, entity_id, attempts = raw.get("event_id"), raw.get("entity_id"), raw.get("attempts")
    _require(isinstance(event_id, str) and EVENT_ID_RE.fullmatch(event_id) is not None, "event id")
    _require(isinstance(entity_id, str) and LIGHT_ENTITY_RE.fullmatch(entity_id) is not None, "event entity")
    _require(raw.get("kind") in EVENT_KINDS and raw.get("state") in STATES, "event kind")
    _require(not isinstance(attempts, bool) and isinstance(attempts, int) and 0 <= attempts <= MAX_ATTEMPTS, "attempts")
    _require(_is_number(raw.get("transition_at")) and _is_number(raw.get("ended_at")), "event instants")
    return LightEvent(
        event_id, entity_id, raw["kind"], raw["state"], float(raw["transition_at"]), attempts, float(raw["ended_at"])
    )


def parse(document: Any) -> LightFile:
    _require(isinstance(document, dict) and document.get("version") == FILE_VERSION, "version")
    entities, events, last_time = document.get("entities"), document.get("events"), document.get("last_time")
    _require(isinstance(entities, dict) and len(entities) <= MAX_ENTITIES, "entities")
    _require(isinstance(events, list) and len(events) <= MAX_EVENTS, "events")
    _require(last_time is None or _is_number(last_time), "last_time")
    held = {}
    for entity_id, raw in entities.items():
        _require(LIGHT_ENTITY_RE.fullmatch(entity_id) is not None, "entity id")
        held[entity_id] = _held(raw)
    return LightFile(
        entities=held,
        last_time=None if last_time is None else float(last_time),
        events=[_event(item) for item in events],
    )


def _transition_doc(transition: Transition) -> dict:
    return {"at": transition.at, "state": transition.state}


def serialize(content: LightFile) -> dict:
    entities = {}
    for entity_id, held in content.entities.items():
        applied = held.applied
        entities[entity_id] = {
            "schedule": {
                "transitions": [_transition_doc(item) for item in held.schedule.transitions],
                "valid_until": held.schedule.valid_until,
            },
            "device_id": held.device_id,
            "cloud_only": held.cloud_only,
            "applied": _transition_doc(applied) if isinstance(applied, Transition) else applied,
            "armed": held.armed,
        }
    return {
        "version": FILE_VERSION,
        "entities": entities,
        "last_time": content.last_time,
        "events": [
            {
                "event_id": event.event_id,
                "entity_id": event.entity_id,
                "kind": event.kind,
                "state": event.state,
                "transition_at": event.transition_at,
                "attempts": event.attempts,
                "ended_at": event.ended_at,
            }
            for event in content.events
        ],
    }


class LightStore:
    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir
        self._path = data_dir / LIGHT_FILE_NAME

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> LightFile | None:
        """The stored content; an empty one when there is no file, and None when it cannot be read or is invalid."""
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return LightFile()
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
        """Keep an unreadable file as `light_control.json.corrupt` (over an older copy) before a write replaces it."""
        with contextlib.suppress(OSError):
            os.replace(self._path, self._path.with_name(LIGHT_FILE_NAME + CORRUPT_SUFFIX))

    def write(self, content: LightFile) -> None:
        """Write through a private temporary file in the same folder, then rename it over the old one."""
        data = json.dumps(serialize(content), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        descriptor, temporary = tempfile.mkstemp(prefix=f"{LIGHT_FILE_NAME}.", suffix=".tmp", dir=self._dir)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
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
