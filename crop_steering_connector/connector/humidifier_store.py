"""The humidifier watch's file, `/data/humidifier_watch.json`: mode 0600, written atomically, out of HA backups.

It holds the humidifiers the Cropilot turned on (each with the off it is waiting for, if any), the instant of the
last 2xx answer from the Cropilot and the events not yet accepted by it, so a restart resumes the watch, the
silence timer included, before any Cropilot call. An event keeps the instant of its end here; the report turns it
into an age.

Since 0.2.17 the file also holds, under `restore`, the dehumidifiers the Cropilot turned off for a humidifier episode
(each with the instant of the off, its lease and the re-on it is waiting for, if any) and the events of their re-ons.
The version stays 1: a 0.2.16 file has no `restore` and reads as none, and a 0.2.16 connector reads a 0.2.17 file
without it, its humidifiers and their events intact, because the re-on's entries and events live only there.
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

from .pump_store import COMMAND_ID_RE, EVENT_ID_RE

HUMIDIFIER_FILE_NAME = "humidifier_watch.json"
CORRUPT_SUFFIX = ".corrupt"
FILE_VERSION = 1
MAX_EVENTS = 20
MAX_WATCHED = 200
MAX_FILE_BYTES = 262_144
OFF_KINDS = frozenset({"stop_off", "pair_off", "silence_off"})
OFF_EVENT_KINDS = OFF_KINDS | {"off_failed"}
# The dehumidifier's re-on (0.2.17): the lease run out, then the three triggers the humidifier's off shares.
ON_KINDS = frozenset({"stop_on", "pair_on", "silence_on", "lease_on"})
ON_EVENT_KINDS = ON_KINDS | {"on_failed"}
EVENT_KINDS = OFF_EVENT_KINDS | ON_EVENT_KINDS
# The on that answers each off trigger of the humidifier watch.
ON_FOR_OFF = {"stop_off": "stop_on", "pair_off": "pair_on", "silence_off": "silence_on"}
HUMIDIFIER_ENTITY_RE = re.compile(r"^humidifier\.[a-z0-9_]{1,200}$")
# A dehumidifier the re-on follows: a `humidifier` of class dehumidifier, or a plug in the Desumidificador role.
RESTORE_ENTITY_RE = re.compile(r"^(humidifier|switch)\.[a-z0-9_]{1,200}$")
# Every entity a `humidifier_events` item may name.
EVENT_ENTITY_RE = RESTORE_ENTITY_RE


class InvalidHumidifierFile(ValueError):
    """The file exists but is not a humidifier file this version wrote."""


@dataclass(frozen=True)
class Watched:
    """A humidifier a Cropilot `power_on` turned on: when, by which command, the off it waits for (or None) and
    whether a read of this `power_on` has already shown it `on`. A file of 0.2.8 has no `seen_on`: read as False."""

    since: float
    command_id: str
    cut: str | None = None
    seen_on: bool = False


@dataclass(frozen=True)
class Restore:
    """A dehumidifier a Cropilot `power_off` flagged `restore_on` turned off (0.2.17): the wall instant of the off, the
    wall instant its lease runs out, by which command, the re-on it waits for (or None) and whether a read of this off
    has already shown it `off`."""

    off_at: float
    lease_until: float
    command_id: str
    cut: str | None = None
    seen_off: bool = False


@dataclass(frozen=True)
class HumidifierEvent:
    event_id: str
    entity_id: str
    kind: str
    ended_at: float

    def wire(self, now: float) -> dict:
        """The event as the report sends it: an age, never an instant, so a wrong clock here moves nothing."""
        return {
            "event_id": self.event_id,
            "entity_id": self.entity_id,
            "kind": self.kind,
            "ended_seconds_ago": max(0, int(now - self.ended_at)),
        }


@dataclass
class HumidifierFile:
    watched: dict[str, Watched] = field(default_factory=dict)
    last_ok_at: float | None = None
    # Both watches' events in one queue, oldest first; the file keeps the re-on's apart, under `restore`.
    events: list[HumidifierEvent] = field(default_factory=list)
    restore: dict[str, Restore] = field(default_factory=dict)


def _is_instant(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise InvalidHumidifierFile(what)


def _is_entity(value: Any) -> bool:
    return isinstance(value, str) and HUMIDIFIER_ENTITY_RE.fullmatch(value) is not None


def _watched(raw: Any) -> dict[str, Watched]:
    _require(isinstance(raw, dict) and len(raw) <= MAX_WATCHED, "watched")
    watched: dict[str, Watched] = {}
    for entity_id, item in raw.items():
        _require(_is_entity(entity_id) and isinstance(item, dict), "watched entity")
        command_id, cut, seen_on = item.get("command_id"), item.get("cut"), item.get("seen_on", False)
        _require(_is_instant(item.get("since")), "watched since")
        _require(isinstance(command_id, str) and COMMAND_ID_RE.fullmatch(command_id) is not None, "watched command_id")
        _require(cut is None or cut in OFF_KINDS, "watched cut")
        _require(isinstance(seen_on, bool), "watched seen_on")
        watched[entity_id] = Watched(since=float(item["since"]), command_id=command_id, cut=cut, seen_on=seen_on)
    return watched


def _events(raw: Any, kinds: frozenset[str], entity_re: re.Pattern) -> list[HumidifierEvent]:
    _require(isinstance(raw, list) and len(raw) <= MAX_EVENTS, "events")
    events: list[HumidifierEvent] = []
    for item in raw:
        _require(isinstance(item, dict), "event")
        event_id, entity_id = item.get("event_id"), item.get("entity_id")
        _require(isinstance(event_id, str) and EVENT_ID_RE.fullmatch(event_id) is not None, "event id")
        _require(isinstance(entity_id, str) and entity_re.fullmatch(entity_id) is not None, "event entity")
        _require(item.get("kind") in kinds, "event kind")
        _require(_is_instant(item.get("ended_at")), "event ended_at")
        events.append(HumidifierEvent(event_id, entity_id, item["kind"], float(item["ended_at"])))
    return events


def _restore(raw: Any) -> tuple[dict[str, Restore], list[HumidifierEvent]]:
    """The `restore` key of a 0.2.17 file; a file without it has none."""
    if raw is None:
        return {}, []
    _require(isinstance(raw, dict), "restore")
    items = raw.get("watched")
    _require(isinstance(items, dict) and len(items) <= MAX_WATCHED, "restore watched")
    restore: dict[str, Restore] = {}
    for entity_id, item in items.items():
        _require(isinstance(entity_id, str) and RESTORE_ENTITY_RE.fullmatch(entity_id) is not None, "restore entity")
        _require(isinstance(item, dict), "restore item")
        command_id, cut, seen_off = item.get("command_id"), item.get("cut"), item.get("seen_off")
        _require(_is_instant(item.get("off_at")) and _is_instant(item.get("lease_until")), "restore instants")
        _require(isinstance(command_id, str) and COMMAND_ID_RE.fullmatch(command_id) is not None, "restore command_id")
        _require(cut is None or cut in ON_KINDS, "restore cut")
        _require(isinstance(seen_off, bool), "restore seen_off")
        restore[entity_id] = Restore(
            off_at=float(item["off_at"]),
            lease_until=float(item["lease_until"]),
            command_id=command_id,
            cut=cut,
            seen_off=seen_off,
        )
    return restore, _events(raw.get("events"), ON_EVENT_KINDS, RESTORE_ENTITY_RE)


def parse(document: Any) -> HumidifierFile:
    _require(isinstance(document, dict) and document.get("version") == FILE_VERSION, "version")
    last_ok_at = document.get("last_ok_at")
    _require(last_ok_at is None or _is_instant(last_ok_at), "last_ok_at")
    restore, restore_events = _restore(document.get("restore"))
    events = _events(document.get("events"), OFF_EVENT_KINDS, HUMIDIFIER_ENTITY_RE) + restore_events
    # One queue again, oldest first; a stable sort keeps the written order of equal instants.
    events.sort(key=lambda event: event.ended_at)
    return HumidifierFile(
        watched=_watched(document.get("watched")),
        last_ok_at=None if last_ok_at is None else float(last_ok_at),
        events=events[-MAX_EVENTS:],
        restore=restore,
    )


def _event_document(event: HumidifierEvent) -> dict:
    return {"event_id": event.event_id, "entity_id": event.entity_id, "kind": event.kind, "ended_at": event.ended_at}


def serialize(content: HumidifierFile) -> dict:
    return {
        "version": FILE_VERSION,
        "watched": {
            entity_id: {"since": item.since, "command_id": item.command_id, "cut": item.cut, "seen_on": item.seen_on}
            for entity_id, item in content.watched.items()
        },
        "last_ok_at": content.last_ok_at,
        "events": [_event_document(event) for event in content.events if event.kind in OFF_EVENT_KINDS],
        "restore": {
            "watched": {
                entity_id: {
                    "off_at": item.off_at,
                    "lease_until": item.lease_until,
                    "command_id": item.command_id,
                    "cut": item.cut,
                    "seen_off": item.seen_off,
                }
                for entity_id, item in content.restore.items()
            },
            "events": [_event_document(event) for event in content.events if event.kind in ON_EVENT_KINDS],
        },
    }


class HumidifierStore:
    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir
        self._path = data_dir / HUMIDIFIER_FILE_NAME

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> HumidifierFile | None:
        """The stored content; an empty one when there is no file, and None when it cannot be read or is invalid."""
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return HumidifierFile()
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
        """Keep an unreadable file as `humidifier_watch.json.corrupt` (over an older copy) before a write replaces it."""
        with contextlib.suppress(OSError):
            os.replace(self._path, self._path.with_name(HUMIDIFIER_FILE_NAME + CORRUPT_SUFFIX))

    def write(self, content: HumidifierFile) -> None:
        """Write through a private temporary file in the same folder, then rename it over the old one."""
        data = json.dumps(serialize(content), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        descriptor, temporary = tempfile.mkstemp(prefix=f"{HUMIDIFIER_FILE_NAME}.", suffix=".tmp", dir=self._dir)
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
