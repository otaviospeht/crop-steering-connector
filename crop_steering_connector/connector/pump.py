"""The pump watch: the connector's own off for a pump that stays on past its pulse or turns on
outside a Cropilot shot.

It runs in its own daemon thread, with its own HA client bound to its own event, and its own loop: never the
connector's `halt` or `_loop`, so a revoked pairing (a 401), the other stop lines and an unreachable Cropilot
never stop it. It never calls the Cropilot. Its only HA service is `switch.turn_off`, only on a `switch` of the
pump list or of an open cycle: the single HA call the connector makes without the Cropilot's start of a
command. The list, the open cycles and the events live in the pump file, so a restart resumes from it before
any Cropilot call.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Iterable

from . import messages
from .clients import HomeAssistantClient, json_body
from .context import Clock
from .pump_store import MAX_EVENTS, PUMP_ENTITY_RE, Cycle, PumpEvent, PumpFile, PumpStore, is_max_on
from .transport import Transport, TransportError

POLL_HOT_SECONDS = 1.0
POLL_IDLE_SECONDS = 5.0
ATTACH_WINDOW_SECONDS = 30.0
RETRY_FAST_SECONDS = 5.0
RETRY_FAST_FOR_SECONDS = 60.0
RETRY_SLOW_SECONDS = 60.0
OFF_FAILED_AFTER_TRIES = 3
ERROR_LOG_INTERVAL_SECONDS = 60.0
UNAVAILABLE_STATES = frozenset({"unavailable", "unknown"})
WHERE = "bomba"
CLOSING_LINES = {
    "forced_off": messages.PUMP_FORCED_OFF,
    "outside_off": messages.PUMP_OUTSIDE_OFF,
    "boot_off": messages.PUMP_BOOT_OFF,
}


@dataclass
class _InFlight:
    """A `pulse_on` from just before its HA call until 30 s after its success (the attach window)."""

    command_id: str
    max_on_seconds: int
    until: float


@dataclass
class _Cut:
    """The off of one cycle in memory: its closing kind and the retry schedule (5 s for a minute, then 60 s)."""

    kind: str
    first_try_at: float | None = None
    next_try_at: float | None = None
    awaiting_check: bool = False


def _instant(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


class PumpWatch:
    def __init__(
        self,
        *,
        data_dir: Path,
        transport: Transport,
        supervisor_token: str,
        clock: Clock,
        log: logging.Logger,
    ) -> None:
        self._store = PumpStore(data_dir)
        # Its own event, never set: the connector's halt must not silence the watch.
        self._ha = HomeAssistantClient(transport, supervisor_token, threading.Event())
        self._clock = clock
        self._log = log
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._retired = threading.Event()
        self.booted = threading.Event()
        self._thread: threading.Thread | None = None
        self._ha_running = False
        self._first_list = True
        self._in_flight: dict[str, _InFlight] = {}
        self._cuts: dict[str, _Cut] = {}
        self._last_read: dict[str, float] = {}
        self._error_logged: dict[str, float] = {}
        content = self._store.read()
        if content is None:
            log.warning(messages.PUMP_FILE_UNREADABLE)
            content = PumpFile()
        self._pumps = content.pumps
        self._cycles = content.cycles
        self._events = content.events
        # The first state read of each pump since this process started applies the boot rule.
        self._boot_pending = set(self._pumps) | set(self._cycles)

    # The thread

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="bomba", daemon=True)
        self._thread.start()

    def retire(self) -> None:
        """Only tests call this; in the app the loop has no exit and ends with the process."""
        self._retired.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(5)

    def _run(self) -> None:
        while not self._retired.is_set():
            self._wake.clear()
            delay = self.step()
            self._clock.wait(self._wake, delay)

    def step(self) -> float:
        """One pass; returns the seconds until the next one, the normal cadence even after an error."""
        try:
            self._pass()
        except Exception as error:  # noqa: BLE001 - the watch must survive any defect of one pass
            self._log_error(error)
        return self._cadence()

    # Calls from the report and command loops

    def replace_list(self, pumps: Iterable[tuple[str, int | None]]) -> None:
        """The pump entries of an accepted report answer; only `switch` entities become pumps."""
        listed = {
            entity_id: max_on if is_max_on(max_on) else None
            for entity_id, max_on in pumps
            if PUMP_ENTITY_RE.fullmatch(entity_id)
        }
        with self._lock:
            if self._first_list:
                self._first_list = False
                self._boot_pending |= {entity_id for entity_id in listed if entity_id not in self._last_read}
            self._boot_pending &= set(listed) | set(self._cycles)
            if listed != self._pumps:
                self._pumps = listed
                self._persist()

    def is_pump(self, entity_id: str) -> bool:
        with self._lock:
            return entity_id in self._pumps

    def pulse_starting(self, entity_id: str, command_id: str, max_on_seconds: int) -> None:
        with self._lock:
            until = self._clock.monotonic() + ATTACH_WINDOW_SECONDS
            self._in_flight[entity_id] = _InFlight(command_id, max_on_seconds, until)
        self._wake.set()

    def pulse_succeeded(self, entity_id: str, command_id: str) -> None:
        with self._lock:
            flight = self._in_flight.get(entity_id)
            if flight is not None and flight.command_id == command_id:
                flight.until = self._clock.monotonic() + ATTACH_WINDOW_SECONDS

    def pulse_failed(self, entity_id: str, command_id: str) -> None:
        with self._lock:
            flight = self._in_flight.get(entity_id)
            if flight is not None and flight.command_id == command_id:
                del self._in_flight[entity_id]

    def events_waiting(self) -> bool:
        with self._lock:
            return bool(self._events)

    def snapshot_events(self) -> list[PumpEvent]:
        with self._lock:
            return list(self._events)

    def drop_events(self, ids: Iterable[str]) -> None:
        sent = set(ids)
        with self._lock:
            kept = [event for event in self._events if event.id not in sent]
            if len(kept) != len(self._events):
                self._events = kept
                self._persist()

    # One pass

    def _pass(self) -> None:
        if not self._ha_running:
            if not self._read_running():
                return
            self._ha_running = True
        for entity_id in self._due():
            try:
                self._watch(entity_id)
            except Exception as error:  # noqa: BLE001 - one pump's defect must not skip the others
                self._log_error(error)
        self.booted.set()

    def _read_running(self) -> bool:
        try:
            response = self._ha.get("/config")
        except TransportError:
            return False
        payload = json_body(response) if response.status == 200 else None
        return isinstance(payload, dict) and payload.get("state") == "RUNNING"

    def _hot(self, entity_id: str, now: float) -> bool:
        flight = self._in_flight.get(entity_id)
        return entity_id in self._cycles or (flight is not None and now <= flight.until)

    def _due(self) -> list[str]:
        now = self._clock.monotonic()
        with self._lock:
            for entity_id in [key for key, flight in self._in_flight.items() if now > flight.until]:
                del self._in_flight[entity_id]
            watched = sorted(set(self._pumps) | set(self._cycles))
            return [
                entity_id
                for entity_id in watched
                if self._hot(entity_id, now) or now - self._last_read.get(entity_id, float("-inf")) >= POLL_IDLE_SECONDS
            ]

    def _cadence(self) -> float:
        now = self._clock.monotonic()
        with self._lock:
            hot = any(self._hot(entity_id, now) for entity_id in set(self._cycles) | set(self._in_flight))
        return POLL_HOT_SECONDS if hot else POLL_IDLE_SECONDS

    def _watch(self, entity_id: str) -> None:
        read = self._read(entity_id)
        if read is None:
            return
        state, changed_at = read
        with self._lock:
            self._last_read[entity_id] = self._clock.monotonic()
            turn_off = self._apply(entity_id, state, changed_at)
        if turn_off:
            self._turn_off(entity_id)

    def _read(self, entity_id: str) -> tuple[str, float] | None:
        """The state and HA's `last_changed` (the read time when missing or in the future), or None."""
        try:
            response = self._ha.get(f"/states/{entity_id}")
        except TransportError:
            return None
        now = self._clock.time()
        if response.status == 404:
            return "unavailable", now
        payload = json_body(response) if response.status == 200 else None
        if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
            return None
        changed = _instant(payload.get("last_changed"))
        return payload["state"], changed if changed is not None and changed <= now else now

    def _turn_off(self, entity_id: str) -> None:
        if not PUMP_ENTITY_RE.fullmatch(entity_id):
            raise ValueError("the watch turns off only a switch")
        try:
            self._ha.post("/services/switch/turn_off", {"entity_id": entity_id})
        except TransportError:
            # The next read finds it still on, and the retry follows.
            return

    # The rule, under the lock

    def _apply(self, entity_id: str, state: str, changed_at: float) -> bool:
        """Update the cycle for one read; True when the watch must call `switch.turn_off` now."""
        cycle = self._cycles.get(entity_id)
        boot = entity_id in self._boot_pending
        if state == "off":
            self._boot_pending.discard(entity_id)
            if cycle is not None:
                self._close(entity_id, cycle, changed_at)
            return False
        if cycle is None:
            if state in UNAVAILABLE_STATES or entity_id not in self._pumps:
                return False
            self._boot_pending.discard(entity_id)
            cycle = self._open(entity_id, changed_at, boot)
        elif boot:
            self._boot_pending.discard(entity_id)
            within = cycle.command_id is not None and cycle.tries == 0
            if not (within and self._clock.time() - cycle.started_at < self._limit(entity_id, cycle)):
                self._cuts[entity_id] = _Cut("boot_off")
        if entity_id not in self._cuts and self._clock.time() - cycle.started_at >= self._limit(entity_id, cycle):
            self._cuts[entity_id] = _Cut("forced_off" if cycle.command_id is not None else "outside_off")
        cut = self._cuts.get(entity_id)
        return cut is not None and self._next_try(entity_id, cycle, cut)

    def _open(self, entity_id: str, started_at: float, boot: bool) -> Cycle:
        flight = self._in_flight.pop(entity_id, None)
        if flight is not None and self._clock.monotonic() <= flight.until:
            listed = self._pumps.get(entity_id)
            limit = flight.max_on_seconds if listed is None else min(flight.max_on_seconds, listed)
            cycle = Cycle(started_at=started_at, command_id=flight.command_id, limit_seconds=limit)
        else:
            # Outside a shot the limit is zero: cut at the first read.
            cycle = Cycle(started_at=started_at, command_id=None, limit_seconds=0)
            self._cuts[entity_id] = _Cut("boot_off" if boot else "outside_off")
        self._cycles[entity_id] = cycle
        self._persist()
        return cycle

    def _limit(self, entity_id: str, cycle: Cycle) -> int:
        if cycle.limit_seconds is not None:
            return cycle.limit_seconds
        return self._pumps.get(entity_id) or 0

    def _next_try(self, entity_id: str, cycle: Cycle, cut: _Cut) -> bool:
        now = self._clock.monotonic()
        if cut.awaiting_check:
            # The read after a try is still not off: that try failed.
            cut.awaiting_check = False
            if cycle.tries >= OFF_FAILED_AFTER_TRIES and not cycle.off_failed_sent:
                cycle.off_failed_sent = True
                self._queue(entity_id, "off_failed", cycle, None, self._clock.time())
            elapsed = now - (cut.first_try_at if cut.first_try_at is not None else now)
            interval = RETRY_FAST_SECONDS if elapsed < RETRY_FAST_FOR_SECONDS else RETRY_SLOW_SECONDS
            cut.next_try_at = now + interval
            self._log.warning(messages.PUMP_OFF_RETRY.format(entity=entity_id, seconds=round(interval)))
            return False
        if cut.next_try_at is not None and now < cut.next_try_at:
            return False
        cycle.tries += 1
        cut.first_try_at = now if cut.first_try_at is None else cut.first_try_at
        cut.awaiting_check = True
        self._persist()
        return True

    def _close(self, entity_id: str, cycle: Cycle, off_at: float) -> None:
        cut = self._cuts.pop(entity_id, None)
        if cut is not None:
            kind = cut.kind
        elif cycle.command_id is not None:
            kind = "forced_off" if cycle.tries else "ended"
        else:
            kind = "outside_off"
        on_seconds = max(0, round(off_at - cycle.started_at))
        del self._cycles[entity_id]
        flight = self._in_flight.get(entity_id)
        if flight is not None and flight.command_id == cycle.command_id:
            del self._in_flight[entity_id]
        self._queue(entity_id, kind, cycle, on_seconds, off_at)
        if kind in CLOSING_LINES:
            self._log.warning(CLOSING_LINES[kind].format(entity=entity_id))

    def _queue(self, entity_id: str, kind: str, cycle: Cycle, on_seconds: int | None, ended_at: float) -> None:
        self._events.append(
            PumpEvent(
                id=str(uuid.uuid4()),
                entity_id=entity_id,
                kind=kind,
                command_id=cycle.command_id,
                on_seconds=on_seconds,
                ended_at=ended_at,
                attempts=cycle.tries,
            )
        )
        dropped = len(self._events) - MAX_EVENTS
        if dropped > 0:
            self._events = self._events[dropped:]
            self._log.warning(messages.PUMP_EVENTS_DROPPED.format(count=dropped))
        self._persist()

    def _persist(self) -> None:
        content = PumpFile(
            pumps=dict(self._pumps),
            cycles={entity_id: replace(cycle) for entity_id, cycle in self._cycles.items()},
            events=list(self._events),
        )
        try:
            self._store.write(content)
        except OSError as error:
            # The state stays in memory and the next change writes it again; the off never waits for the disk.
            self._log_error(error)

    def _log_error(self, error: Exception) -> None:
        name = type(error).__name__
        now = self._clock.monotonic()
        with self._lock:
            last = self._error_logged.get(name)
            if last is not None and now - last < ERROR_LOG_INTERVAL_SECONDS:
                return
            self._error_logged[name] = now
        self._log.error(messages.UNEXPECTED_ERROR.format(where=WHERE, error=name))
