"""The pump watch: the connector's own off for a pump that stays on past its pulse or turns on
outside a Cropilot shot.

It runs in its own daemon thread, with its own HA client bound to its own event, and its own loop: never the
connector's `halt` or `_loop`, so a revoked pairing (a 401), the other stop lines and an unreachable Cropilot
never stop it. It never calls the Cropilot. Its only HA service is `switch.turn_off`, only on a `switch` of the
pump list or of an open cycle, or `valve.close_valve` for a valve pump (0.2.12): the single HA call the
connector makes without the Cropilot's start of a command. The list, the open cycles and the events live in the pump file, so a restart resumes from it before
any Cropilot call.

Since 0.2.16 the backup watering (pump_backup.py) starts a pulse of its own with `backup_pulse_starting`, keyed by the
pulse's `irrigation_shots` key instead of a command id. The watch cuts it as a Cropilot shot (the dose, `max_on`, the
boot rule, never an `outside_off`), and a restart in the middle of it is always a `boot_off`. Such a cycle never
becomes a `pump_events` item: its end and its `off_failed` go to the backup watering's sink, before the pump file is
written, so a crash between the two writes repeats the end, which the sink ignores, and never loses it.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from . import messages
from .clients import HomeAssistantClient, json_body
from .context import Clock
from .pump_store import (
    MAX_EVENTS,
    PUMP_ENTITY_RE,
    BackupKey,
    Cycle,
    PumpEvent,
    PumpFile,
    PumpStore,
    is_max_on,
    off_state,
)
from .transport import Transport, TransportError

# (entity id, backup key, kind, on seconds or None, ended at, attempts): a backup cycle's end or `off_failed`.
BackupSink = Callable[[str, BackupKey, str, "int | None", float, int], None]

POLL_HOT_SECONDS = 1.0
POLL_IDLE_SECONDS = 5.0
ATTACH_WINDOW_SECONDS = 30.0
RETRY_FAST_SECONDS = 5.0
RETRY_FAST_FOR_SECONDS = 60.0
RETRY_SLOW_SECONDS = 60.0
OFF_FAILED_AFTER_TRIES = 3
ERROR_LOG_INTERVAL_SECONDS = 60.0
UNAVAILABLE_STATES = frozenset({"unavailable", "unknown"})
OFF_SERVICES = {"switch": "/services/switch/turn_off", "valve": "/services/valve/close_valve"}
# A valve stays out of the dose cut, so out of the backup watering: only a plug takes a backup pulse.
BACKUP_PUMP_PREFIX = "switch."
WHERE = "bomba"
# The watch's off at the dose closes the shot as a normal end.
DOSE_CUT = "ended"
# A valve's motor takes a while to close: `closing` after a try is that try working, until this grace runs out.
VALVE_CLOSING_STATE = "closing"
VALVE_CLOSING_GRACE_SECONDS = 30.0
CLOSING_LINES = {
    "forced_off": messages.PUMP_FORCED_OFF,
    "outside_off": messages.PUMP_OUTSIDE_OFF,
    "boot_off": messages.PUMP_BOOT_OFF,
}


@dataclass
class _InFlight:
    """A `pulse_on`, or a backup pulse, from just before its HA call until 30 s after its success (the attach
    window)."""

    command_id: str | None
    max_on_seconds: int
    until: float
    dose_seconds: int | None = None
    backup_key: BackupKey | None = None

    def same(self, command_id: str | None, backup_key: BackupKey | None) -> bool:
        return (self.command_id, self.backup_key) == (command_id, backup_key)


@dataclass
class _Cut:
    """The off of one cycle in memory: its closing kind and the retry schedule (5 s for a minute, then 60 s)."""

    kind: str
    first_try_at: float | None = None
    next_try_at: float | None = None
    awaiting_check: bool = False
    tried_at: float | None = None


def _closing(entity_id: str, state: str) -> bool:
    return entity_id.startswith("valve.") and state == VALVE_CLOSING_STATE


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
        self._backup_sink: BackupSink | None = None
        # The backup watering's answer to «a slot of yours is open on this pump», its gaps included.
        self._backup_slot_open: Callable[[str], bool] | None = None
        # A backup cycle that ended before the backup watering attached, handed over at the attach.
        self._backup_waiting: list[tuple[str, BackupKey, str, int | None, float, int]] = []
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
        """The pump entries of an accepted report answer; only `switch` and `valve` entities become pumps."""
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

    def pulse_starting(self, entity_id: str, command_id: str, max_on_seconds: int, dose_seconds: int | None = None) -> None:
        """The shot's `max_on_seconds` is the pulse the Cropilot holds now (its start refuses an older one), so it
        replaces the list's, which may still be the one before a pulse change until the next report answer."""
        with self._lock:
            until = self._clock.monotonic() + ATTACH_WINDOW_SECONDS
            self._in_flight[entity_id] = _InFlight(command_id, max_on_seconds, until, dose_seconds)
            if entity_id in self._pumps and self._pumps[entity_id] != max_on_seconds:
                self._pumps[entity_id] = max_on_seconds
                self._persist()
        self._wake.set()

    def pulse_succeeded(self, entity_id: str, command_id: str | None, backup_key: BackupKey | None = None) -> None:
        with self._lock:
            flight = self._in_flight.get(entity_id)
            if flight is not None and flight.same(command_id, backup_key):
                flight.until = self._clock.monotonic() + ATTACH_WINDOW_SECONDS

    def pulse_failed(self, entity_id: str, command_id: str | None, backup_key: BackupKey | None = None) -> None:
        with self._lock:
            flight = self._in_flight.get(entity_id)
            if flight is not None and flight.same(command_id, backup_key):
                del self._in_flight[entity_id]

    # Calls from the backup watering (pump_backup.py), never under its lock

    def attach_backup(self, sink: BackupSink, slot_open: Callable[[str], bool] | None = None) -> None:
        """The backup watering's sink, called under this watch's lock, and its slot check, called outside it; a backup
        end queued before goes to the sink now."""
        with self._lock:
            self._backup_sink = sink
            self._backup_slot_open = slot_open
            waiting, self._backup_waiting = self._backup_waiting, []
            for item in waiting:
                self._hand_backup(*item)

    def backup_busy(self, entity_id: str) -> bool:
        """A backup pulse is under way on the pump, in its attach window or open as a cycle, or the backup watering has
        a slot open on it, the gaps between its pulses included: a Cropilot `pulse_on` is refused until the slot ends."""
        now = self._clock.monotonic()
        with self._lock:
            cycle = self._cycles.get(entity_id)
            flight = self._in_flight.get(entity_id)
            if cycle is not None and cycle.backup_key is not None:
                return True
            if flight is not None and flight.backup_key is not None and now <= flight.until:
                return True
            slot_open = self._backup_slot_open
        return slot_open is not None and slot_open(entity_id)

    def listed_max_on(self, entity_id: str) -> int | None:
        """The pump's `max_on_seconds` in the last list, or None when it is not listed or has none."""
        with self._lock:
            return self._pumps.get(entity_id)

    def busy(self, entity_id: str) -> bool:
        """A cycle is open on the pump, or a shot or a backup pulse is about to reach it."""
        now = self._clock.monotonic()
        with self._lock:
            flight = self._in_flight.get(entity_id)
            return entity_id in self._cycles or (flight is not None and now <= flight.until)

    def backup_pending(self, entity_id: str, key: BackupKey) -> bool:
        """The backup pulse of `key` is still in its attach window or open as a cycle."""
        now = self._clock.monotonic()
        with self._lock:
            cycle = self._cycles.get(entity_id)
            if cycle is not None and cycle.backup_key == key:
                return True
            flight = self._in_flight.get(entity_id)
            return flight is not None and flight.backup_key == key and now <= flight.until

    def backup_pulse_starting(self, entity_id: str, key: BackupKey, max_on_seconds: int, dose_seconds: int) -> bool:
        """Just before the backup's HA call: True when the watch now follows the pulse. False, with nothing changed,
        when the pump is not on the list with this `max_on_seconds` or a cycle or another shot is under way on it."""
        now = self._clock.monotonic()
        with self._lock:
            flight = self._in_flight.get(entity_id)
            if (
                not PUMP_ENTITY_RE.fullmatch(entity_id)
                or not entity_id.startswith(BACKUP_PUMP_PREFIX)
                or not self.booted.is_set()
                or self._pumps.get(entity_id) != max_on_seconds
                or not is_max_on(max_on_seconds)
                or entity_id in self._cycles
                or (flight is not None and now <= flight.until)
            ):
                return False
            self._in_flight[entity_id] = _InFlight(
                None, max_on_seconds, now + ATTACH_WINDOW_SECONDS, dose_seconds, backup_key=key
            )
        self._wake.set()
        return True

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
            raise ValueError("the watch turns off only a switch or a valve")
        try:
            self._ha.post(OFF_SERVICES[entity_id.split(".", 1)[0]], {"entity_id": entity_id})
        except TransportError:
            # The next read finds it still on, and the retry follows.
            return

    # The rule, under the lock

    def _apply(self, entity_id: str, state: str, changed_at: float) -> bool:
        """Update the cycle for one read; True when the watch must call `switch.turn_off` now."""
        cycle = self._cycles.get(entity_id)
        boot = entity_id in self._boot_pending
        if state == off_state(entity_id):
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
            # A Cropilot shot inside its limit resumes; a backup pulse never does: the restart ends its slot.
            within = cycle.command_id is not None and cycle.tries == 0
            if not (within and self._clock.time() - cycle.started_at < self._limit(entity_id, cycle)):
                self._cuts[entity_id] = _Cut("boot_off")
        elapsed = self._clock.time() - cycle.started_at
        past_limit = elapsed >= self._limit(entity_id, cycle)
        if past_limit and self._cuts.get(entity_id, _Cut("")).kind == DOSE_CUT:
            # The dose off did not take and the pump ran on to its limit: from here it is a forced off.
            self._cuts[entity_id].kind = "forced_off"
        if entity_id not in self._cuts and past_limit:
            self._cuts[entity_id] = _Cut("forced_off" if cycle.in_shot else "outside_off")
        if entity_id not in self._cuts and cycle.dose_seconds is not None and elapsed >= cycle.dose_seconds:
            # The dose is reached: the shot ends here, and the pump's own timer was only the backup.
            self._cuts[entity_id] = _Cut(DOSE_CUT)
        cut = self._cuts.get(entity_id)
        return cut is not None and self._next_try(entity_id, cycle, cut, state)

    def _open(self, entity_id: str, started_at: float, boot: bool) -> Cycle:
        flight = self._in_flight.pop(entity_id, None)
        if flight is not None and self._clock.monotonic() <= flight.until:
            cycle = Cycle(
                started_at=started_at,
                command_id=flight.command_id,
                limit_seconds=flight.max_on_seconds,
                dose_seconds=flight.dose_seconds,
                backup_key=flight.backup_key,
            )
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

    def _next_try(self, entity_id: str, cycle: Cycle, cut: _Cut, state: str) -> bool:
        now = self._clock.monotonic()
        tried_at = cut.tried_at
        if cut.awaiting_check and tried_at is not None and _closing(entity_id, state):
            if now - tried_at < VALVE_CLOSING_GRACE_SECONDS:
                return False
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
        cut.tried_at = now
        cut.awaiting_check = True
        self._persist()
        return True

    def _close(self, entity_id: str, cycle: Cycle, off_at: float) -> None:
        cut = self._cuts.pop(entity_id, None)
        if cut is not None:
            # The dose off that needed a retry was not a normal end.
            kind = "forced_off" if cut.kind == DOSE_CUT and cycle.tries > 1 else cut.kind
        elif cycle.in_shot:
            kind = "forced_off" if cycle.tries else "ended"
        else:
            kind = "outside_off"
        on_seconds = max(0, round(off_at - cycle.started_at))
        del self._cycles[entity_id]
        flight = self._in_flight.get(entity_id)
        if flight is not None and flight.same(cycle.command_id, cycle.backup_key):
            del self._in_flight[entity_id]
        self._queue(entity_id, kind, cycle, on_seconds, off_at)
        if kind in CLOSING_LINES:
            self._log.warning(CLOSING_LINES[kind].format(entity=entity_id))

    def _queue(self, entity_id: str, kind: str, cycle: Cycle, on_seconds: int | None, ended_at: float) -> None:
        if cycle.backup_key is not None:
            # The backup watering's sink writes its file first; the pump file follows, so its end is never lost.
            self._hand_backup(entity_id, cycle.backup_key, kind, on_seconds, ended_at, cycle.tries)
            self._persist()
            return
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

    def _hand_backup(
        self, entity_id: str, key: BackupKey, kind: str, on_seconds: int | None, ended_at: float, attempts: int
    ) -> None:
        sink = self._backup_sink
        if sink is None:
            self._backup_waiting = [*self._backup_waiting, (entity_id, key, kind, on_seconds, ended_at, attempts)][
                -MAX_EVENTS:
            ]
            return
        try:
            sink(entity_id, key, kind, on_seconds, ended_at, attempts)
        except Exception as error:  # noqa: BLE001 - the off never waits for the backup watering's bookkeeping
            self._log_error(error)

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
