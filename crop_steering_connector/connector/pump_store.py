"""The pump watch's file, `/data/pump_watch.json`: mode 0600, written atomically, out of HA backups.

It holds the pump list of the last report answer, the open cycles and the events not yet accepted by the
Cropilot, so the watch resumes from it after a restart, a 401 or with the Cropilot unreachable. An event keeps
the instant of its end here; the report turns it into an age.
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

PUMP_FILE_NAME = "pump_watch.json"
FILE_VERSION = 1
MAX_EVENTS = 20
MAX_FILE_BYTES = 262_144
EVENT_KINDS = frozenset({"ended", "forced_off", "outside_off", "boot_off", "off_failed"})
PUMP_ENTITY_RE = re.compile(r"^switch\.[a-z0-9_]{1,200}$")
COMMAND_ID_RE = re.compile(r"^[0-9A-Za-z]{26}$")
EVENT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
MAX_ON_SECONDS_RANGE = (20, 915)


class InvalidPumpFile(ValueError):
    """The file exists but is not a pump file this version wrote."""


@dataclass
class Cycle:
    """An open on of a pump: from its first `on` read until an `off` read."""

    started_at: float
    command_id: str | None
    limit_seconds: int | None
    tries: int = 0
    off_failed_sent: bool = False


@dataclass(frozen=True)
class PumpEvent:
    id: str
    entity_id: str
    kind: str
    command_id: str | None
    on_seconds: int | None
    ended_at: float
    attempts: int

    def wire(self, now: float) -> dict:
        """The event as the report sends it: an age, never an instant, so a wrong clock here moves nothing."""
        return {
            "id": self.id,
            "entity_id": self.entity_id,
            "kind": self.kind,
            "command_id": self.command_id,
            "on_seconds": self.on_seconds,
            "ended_age_seconds": max(0, int(now - self.ended_at)),
            "attempts": self.attempts,
        }


@dataclass
class PumpFile:
    pumps: dict[str, int | None] = field(default_factory=dict)
    cycles: dict[str, Cycle] = field(default_factory=dict)
    events: list[PumpEvent] = field(default_factory=list)


def is_max_on(value: Any) -> bool:
    low, high = MAX_ON_SECONDS_RANGE
    return not isinstance(value, bool) and isinstance(value, int) and low <= value <= high


def _is_count(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _is_instant(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _optional_command_id(value: Any) -> bool:
    return value is None or (isinstance(value, str) and COMMAND_ID_RE.fullmatch(value) is not None)


def _require(condition: bool, what: str) -> None:
    if not condition:
        raise InvalidPumpFile(what)


def _pumps(raw: Any) -> dict[str, int | None]:
    _require(isinstance(raw, list), "pumps")
    pumps: dict[str, int | None] = {}
    for item in raw:
        _require(isinstance(item, dict), "pump")
        entity_id, max_on = item.get("entity_id"), item.get("max_on_seconds")
        _require(isinstance(entity_id, str) and PUMP_ENTITY_RE.fullmatch(entity_id) is not None, "pump entity")
        _require(max_on is None or is_max_on(max_on), "pump max_on_seconds")
        pumps[entity_id] = max_on
    return pumps


def _cycles(raw: Any) -> dict[str, Cycle]:
    _require(isinstance(raw, dict), "cycles")
    cycles: dict[str, Cycle] = {}
    for entity_id, item in raw.items():
        _require(PUMP_ENTITY_RE.fullmatch(entity_id) is not None and isinstance(item, dict), "cycle")
        limit = item.get("limit_seconds")
        _require(_is_instant(item.get("started_at")), "cycle started_at")
        _require(_optional_command_id(item.get("command_id")), "cycle command_id")
        _require(limit is None or (_is_count(limit) and limit <= MAX_ON_SECONDS_RANGE[1]), "cycle limit_seconds")
        _require(_is_count(item.get("tries")), "cycle tries")
        _require(isinstance(item.get("off_failed_sent"), bool), "cycle off_failed_sent")
        cycles[entity_id] = Cycle(
            started_at=float(item["started_at"]),
            command_id=item["command_id"],
            limit_seconds=limit,
            tries=item["tries"],
            off_failed_sent=item["off_failed_sent"],
        )
    return cycles


def _events(raw: Any) -> list[PumpEvent]:
    _require(isinstance(raw, list) and len(raw) <= MAX_EVENTS, "events")
    events: list[PumpEvent] = []
    for item in raw:
        _require(isinstance(item, dict), "event")
        on_seconds = item.get("on_seconds")
        _require(isinstance(item.get("id"), str) and EVENT_ID_RE.fullmatch(item["id"]) is not None, "event id")
        _require(isinstance(item.get("entity_id"), str) and PUMP_ENTITY_RE.fullmatch(item["entity_id"]) is not None, "event entity")
        _require(item.get("kind") in EVENT_KINDS, "event kind")
        _require(_optional_command_id(item.get("command_id")), "event command_id")
        _require(on_seconds is None or _is_count(on_seconds), "event on_seconds")
        _require(_is_instant(item.get("ended_at")), "event ended_at")
        _require(_is_count(item.get("attempts")), "event attempts")
        events.append(
            PumpEvent(
                id=item["id"],
                entity_id=item["entity_id"],
                kind=item["kind"],
                command_id=item["command_id"],
                on_seconds=on_seconds,
                ended_at=float(item["ended_at"]),
                attempts=item["attempts"],
            )
        )
    return events


def parse(document: Any) -> PumpFile:
    _require(isinstance(document, dict) and document.get("version") == FILE_VERSION, "version")
    return PumpFile(
        pumps=_pumps(document.get("pumps")),
        cycles=_cycles(document.get("cycles")),
        events=_events(document.get("events")),
    )


def serialize(content: PumpFile) -> dict:
    return {
        "version": FILE_VERSION,
        "pumps": [{"entity_id": entity_id, "max_on_seconds": max_on} for entity_id, max_on in content.pumps.items()],
        "cycles": {
            entity_id: {
                "started_at": cycle.started_at,
                "command_id": cycle.command_id,
                "limit_seconds": cycle.limit_seconds,
                "tries": cycle.tries,
                "off_failed_sent": cycle.off_failed_sent,
            }
            for entity_id, cycle in content.cycles.items()
        },
        "events": [
            {
                "id": event.id,
                "entity_id": event.entity_id,
                "kind": event.kind,
                "command_id": event.command_id,
                "on_seconds": event.on_seconds,
                "ended_at": event.ended_at,
                "attempts": event.attempts,
            }
            for event in content.events
        ],
    }


class PumpStore:
    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir
        self._path = data_dir / PUMP_FILE_NAME

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> PumpFile | None:
        """The stored content; an empty one when there is no file, and None when it cannot be read or is invalid."""
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return PumpFile()
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

    def write(self, content: PumpFile) -> None:
        """Write through a private temporary file in the same folder, then rename it over the old one."""
        data = json.dumps(serialize(content), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        descriptor, temporary = tempfile.mkstemp(prefix=f"{PUMP_FILE_NAME}.", suffix=".tmp", dir=self._dir)
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
