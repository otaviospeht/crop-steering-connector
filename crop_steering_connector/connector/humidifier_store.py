"""The humidifier watch's file, `/data/humidifier_watch.json`: mode 0600, written atomically, out of HA backups.

It holds the humidifiers the Cropilot turned on (each with the off it is waiting for, if any), the instant of the
last 2xx answer from the Cropilot and the events not yet accepted by it, so a restart resumes the watch, the
silence timer included, before any Cropilot call. An event keeps the instant of its end here; the report turns it
into an age.
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
EVENT_KINDS = OFF_KINDS | {"off_failed"}
HUMIDIFIER_ENTITY_RE = re.compile(r"^humidifier\.[a-z0-9_]{1,200}$")


class InvalidHumidifierFile(ValueError):
    """The file exists but is not a humidifier file this version wrote."""


@dataclass(frozen=True)
class Watched:
    """A humidifier a Cropilot `power_on` turned on: when, by which command, and the off it waits for (or None)."""

    since: float
    command_id: str
    cut: str | None = None


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
    events: list[HumidifierEvent] = field(default_factory=list)


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
        command_id, cut = item.get("command_id"), item.get("cut")
        _require(_is_instant(item.get("since")), "watched since")
        _require(isinstance(command_id, str) and COMMAND_ID_RE.fullmatch(command_id) is not None, "watched command_id")
        _require(cut is None or cut in OFF_KINDS, "watched cut")
        watched[entity_id] = Watched(since=float(item["since"]), command_id=command_id, cut=cut)
    return watched


def _events(raw: Any) -> list[HumidifierEvent]:
    _require(isinstance(raw, list) and len(raw) <= MAX_EVENTS, "events")
    events: list[HumidifierEvent] = []
    for item in raw:
        _require(isinstance(item, dict), "event")
        event_id = item.get("event_id")
        _require(isinstance(event_id, str) and EVENT_ID_RE.fullmatch(event_id) is not None, "event id")
        _require(_is_entity(item.get("entity_id")), "event entity")
        _require(item.get("kind") in EVENT_KINDS, "event kind")
        _require(_is_instant(item.get("ended_at")), "event ended_at")
        events.append(HumidifierEvent(event_id, item["entity_id"], item["kind"], float(item["ended_at"])))
    return events


def parse(document: Any) -> HumidifierFile:
    _require(isinstance(document, dict) and document.get("version") == FILE_VERSION, "version")
    last_ok_at = document.get("last_ok_at")
    _require(last_ok_at is None or _is_instant(last_ok_at), "last_ok_at")
    return HumidifierFile(
        watched=_watched(document.get("watched")),
        last_ok_at=None if last_ok_at is None else float(last_ok_at),
        events=_events(document.get("events")),
    )


def serialize(content: HumidifierFile) -> dict:
    return {
        "version": FILE_VERSION,
        "watched": {
            entity_id: {"since": item.since, "command_id": item.command_id, "cut": item.cut}
            for entity_id, item in content.watched.items()
        },
        "last_ok_at": content.last_ok_at,
        "events": [
            {"event_id": event.event_id, "entity_id": event.entity_id, "kind": event.kind, "ended_at": event.ended_at}
            for event in content.events
        ],
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
