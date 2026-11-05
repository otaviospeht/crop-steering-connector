"""The humidifier watch: the connector's own off for a humidifier the Cropilot turned on, when the Cropilot stops
answering.

A humidifier enters when a Cropilot `power_on` on a `humidifier` entity with `device_class: humidifier` reaches
Home Assistant: the connector cannot tell the round from the platform's manual toggle, and turning off a
humidifier the platform switched on fails closed. It leaves when a Cropilot `power_off` on it succeeds, when the
watch reads it `off`, when the watch's own off succeeds, or when HA no longer has the entity. A humidifier the
person turns on is never watched.

Every watched humidifier is turned off on a stop line (a 401 on any endpoint, a refused pairing: the connector's
halt), on a new pairing (before the first claim) and after 20 minutes with no 2xx answer from any Cropilot endpoint.
A `power_on` still in flight when the connector halts enters with the stop line's off already on it. Like the pump
watch it runs in its own daemon thread with its own HA client and never calls the Cropilot; its only HA service is
`humidifier.turn_off`, only on a watched entity. The watched set, the silence timer and the events live in the
humidifier file, so a restart resumes from it before any Cropilot call.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from . import messages
from .clients import TIMEOUT_SECONDS, HomeAssistantClient, json_body
from .context import Clock
from .humidifier_store import (
    HUMIDIFIER_ENTITY_RE,
    MAX_EVENTS,
    MAX_WATCHED,
    OFF_KINDS,
    HumidifierEvent,
    HumidifierFile,
    HumidifierStore,
    Watched,
)
from .transport import Transport, TransportError

SILENCE_SECONDS = 1200.0
LAST_OK_WRITE_STEP_SECONDS = 60.0
# A read of `off` this soon after the power_on is HA lagging behind its own call, not the person's off.
ON_GRACE_SECONDS = 30.0
POLL_HOT_SECONDS = 1.0
POLL_IDLE_SECONDS = 5.0
RETRY_FAST_SECONDS = 5.0
RETRY_FAST_FOR_SECONDS = 60.0
RETRY_SLOW_SECONDS = 60.0
OFF_FAILED_AFTER_TRIES = 3
# HA answers 404 for an entity it has not loaded yet while it starts, so a humidifier leaves only after 5 reads in
# a row that HA does not know it, spread over 10 minutes.
GONE_AFTER_READS = 5
GONE_AFTER_SECONDS = 600.0
HALT_POLL_SECONDS = 1.0
# A trigger waits for the pass under way to finish its HA call, bounded by the client's timeout.
TRIGGER_WAIT_SECONDS = TIMEOUT_SECONDS + 5.0
ERROR_LOG_INTERVAL_SECONDS = 60.0
WHERE = "umidificador"
CLOSING_LINES = {
    "stop_off": messages.HUMIDIFIER_STOP_OFF,
    "pair_off": messages.HUMIDIFIER_PAIR_OFF,
    "silence_off": messages.HUMIDIFIER_SILENCE_OFF,
}


@dataclass
class _Try:
    """The retry schedule of one off in memory (5 s for a minute, then 60 s); a restart starts it over."""

    tries: int = 0
    first_try_at: float | None = None
    next_try_at: float | None = None
    tried_at: float | None = None
    awaiting_check: bool = False
    off_failed_sent: bool = False


@dataclass
class _Missing:
    """The 404 reads in a row of one watched humidifier, since the first one (monotonic)."""

    reads: int
    since: float


class _NotFound:
    """A read HA answered 404: it has no such entity, maybe only not yet."""


NOT_FOUND = _NotFound()


class HumidifierWatch:
    def __init__(
        self,
        *,
        data_dir: Path,
        transport: Transport,
        supervisor_token: str,
        clock: Clock,
        log: logging.Logger,
    ) -> None:
        self._store = HumidifierStore(data_dir)
        # Its own event, never set: the connector's halt is a trigger of this watch, never its end.
        self._ha = HomeAssistantClient(transport, supervisor_token, threading.Event())
        self._clock = clock
        self._log = log
        self._lock = threading.RLock()
        self._pass_lock = threading.Lock()
        self._wake = threading.Event()
        self._preempt = threading.Event()
        self._retired = threading.Event()
        self._halt: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._halt_thread: threading.Thread | None = None
        self._tries: dict[str, _Try] = {}
        self._last_read: dict[str, float] = {}
        self._missing: dict[str, _Missing] = {}
        self._error_logged: dict[str, float] = {}
        content = self._store.read()
        if content is None:
            self._store.set_aside()
            log.warning(messages.HUMIDIFIER_FILE_UNREADABLE)
            content = HumidifierFile()
        self._watched = content.watched
        self._last_ok_at = content.last_ok_at
        self._last_ok_written = content.last_ok_at
        self._events = content.events
        # The silence is also counted on the monotonic clock, which the file cannot keep: it starts as far back as
        # the file says. A 2xx stored in the future means the wall clock went back across the restart; it is
        # clamped to now, and the silence stays unproven (fail closed) until the next 2xx.
        self._last_ok_mono: float | None = None
        now = clock.time()
        if content.last_ok_at is not None:
            if content.last_ok_at > now:
                self._last_ok_at = now
            else:
                self._last_ok_mono = clock.monotonic() - (now - content.last_ok_at)

    # The threads

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="umidificador", daemon=True)
        self._thread.start()

    def follow_halt(self, halt: threading.Event) -> None:
        """Turn every watched humidifier off the moment the connector halts on a stop line."""
        self._halt = halt

        def follow() -> None:
            while not self._retired.is_set():
                if halt.wait(HALT_POLL_SECONDS):
                    self.trigger("stop_off")
                    return

        self._halt_thread = threading.Thread(target=follow, name="umidificador-parada", daemon=True)
        self._halt_thread.start()

    def retire(self) -> None:
        """Only tests call this; in the app the loops have no exit and end with the process."""
        self._retired.set()
        self._wake.set()
        for thread in (self._thread, self._halt_thread):
            if thread is not None:
                thread.join(5)

    def _run(self) -> None:
        while not self._retired.is_set():
            self._wake.clear()
            delay = self.step()
            self._clock.wait(self._wake, delay)

    def step(self) -> float:
        """One pass; returns the seconds until the next one, the normal cadence even after an error."""
        with self._pass_lock:
            self._guarded_pass()
        return self._cadence()

    def _guarded_pass(self) -> None:
        try:
            self._pass()
        except Exception as error:  # noqa: BLE001 - the watch must survive any defect of one pass
            self._log_error(error)

    # Calls from the command loop, the transport and the start step

    def turned_on(self, entity_id: str, command_id: str) -> None:
        """A Cropilot `power_on` on a humidifier-class humidifier reached HA."""
        if not HUMIDIFIER_ENTITY_RE.fullmatch(entity_id):
            return
        with self._lock:
            # A command that reached HA passed the Cropilot's start just now: an off still under way from an
            # earlier pairing or silence no longer applies to it. A halt does: its one trigger may have run while
            # this call was in flight, and no Cropilot power_off comes after it, so this on goes off at once. The
            # lock orders this with that trigger: a halt read as clear here means the trigger marks this entry.
            halted = self._halt is not None and self._halt.is_set()
            self._watched[entity_id] = Watched(
                since=self._clock.time(), command_id=command_id, cut="stop_off" if halted else None
            )
            self._tries.pop(entity_id, None)
            self._missing.pop(entity_id, None)
            self._persist()
        self._wake.set()

    def turned_off(self, entity_id: str) -> None:
        """A Cropilot `power_off` on it succeeded: the Cropilot itself turned it off."""
        with self._lock:
            if entity_id not in self._watched:
                return
            self._forget(entity_id)
            self._persist()

    def server_answered(self) -> None:
        """A 2xx from any Cropilot endpoint; the file follows when the instant moves by 60 s or more."""
        now = self._clock.time()
        with self._lock:
            self._last_ok_at = now
            self._last_ok_mono = self._clock.monotonic()
            if self._last_ok_written is None or abs(now - self._last_ok_written) >= LAST_OK_WRITE_STEP_SECONDS:
                self._persist()

    def paired(self) -> None:
        """A new pairing succeeded: every watched humidifier is turned off before the first claim."""
        self.trigger("pair_off")

    def trigger(self, kind: str) -> None:
        """Mark every watched humidifier for an off of `kind` and run a pass at once, in the caller's thread.

        A pass under way stops before its next HA call, so the trigger waits for one HA call at most (the client's
        timeout bounds it). Past TRIGGER_WAIT_SECONDS it gives up the wait, and the watch's thread runs the off at
        once after the pass that holds it."""
        if kind not in OFF_KINDS:
            raise ValueError("unknown off")
        with self._lock:
            if not self._mark(kind):
                return
        self._preempt.set()
        acquired = self._pass_lock.acquire(timeout=TRIGGER_WAIT_SECONDS)
        self._preempt.clear()
        if acquired:
            try:
                self._guarded_pass()
            finally:
                self._pass_lock.release()
        self._wake.set()

    def watched(self) -> dict[str, Watched]:
        with self._lock:
            return dict(self._watched)

    def last_ok_at(self) -> float | None:
        with self._lock:
            return self._last_ok_at

    def events_waiting(self) -> bool:
        with self._lock:
            return bool(self._events)

    def snapshot_events(self) -> list[HumidifierEvent]:
        with self._lock:
            return list(self._events)

    def drop_events(self, ids: Iterable[str]) -> None:
        sent = set(ids)
        with self._lock:
            kept = [event for event in self._events if event.event_id not in sent]
            if len(kept) != len(self._events):
                self._events = kept
                self._persist()

    # One pass

    def _pass(self) -> None:
        with self._lock:
            if self._silent():
                self._mark("silence_off")
            due = self._due()
        for entity_id in due:
            if self._preempt.is_set():
                # A trigger waits for this pass: it runs a whole one itself.
                return
            try:
                self._watch(entity_id)
            except Exception as error:  # noqa: BLE001 - one humidifier's defect must not skip the others
                self._log_error(error)

    def _silent(self) -> bool:
        if not self._watched:
            return False
        # With no 2xx proven nothing shows the Cropilot is answering: fail closed.
        if self._last_ok_at is None or self._last_ok_mono is None:
            return True
        # The longer of the two clocks: a wall clock set back cannot stretch the silence, nor a paused VM, whose
        # monotonic clock stands still, hide it.
        silence = max(self._clock.monotonic() - self._last_ok_mono, self._clock.time() - self._last_ok_at)
        return silence >= SILENCE_SECONDS

    def _mark(self, kind: str) -> bool:
        """Under the lock: give every watched humidifier without an off this one; True when any is watched."""
        changed = False
        for entity_id, item in self._watched.items():
            if item.cut is None:
                self._watched[entity_id] = replace(item, cut=kind)
                changed = True
        if changed:
            self._persist()
        return bool(self._watched)

    def _due(self) -> list[str]:
        now = self._clock.monotonic()
        return [
            entity_id
            for entity_id, item in sorted(self._watched.items())
            if item.cut is not None or now - self._last_read.get(entity_id, float("-inf")) >= POLL_IDLE_SECONDS
        ]

    def _cadence(self) -> float:
        with self._lock:
            hot = any(item.cut is not None for item in self._watched.values())
        return POLL_HOT_SECONDS if hot else POLL_IDLE_SECONDS

    def _watch(self, entity_id: str) -> None:
        state = self._read(entity_id)
        with self._lock:
            self._last_read[entity_id] = self._clock.monotonic()
            if self._gone(entity_id, state):
                return
            decided = self._apply(entity_id, None if isinstance(state, _NotFound) else state)
        if decided is not None:
            self._turn_off(entity_id, decided)

    def _read(self, entity_id: str) -> str | _NotFound | None:
        """The entity's state; NOT_FOUND on a 404; None when HA did not answer with one (read as «maybe on»)."""
        try:
            response = self._ha.get(f"/states/{entity_id}")
        except TransportError:
            return None
        if response.status == 404:
            return NOT_FOUND
        payload = json_body(response) if response.status == 200 else None
        if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
            return None
        return payload["state"]

    def _turn_off(self, entity_id: str, decided: tuple[str, str]) -> None:
        """Call `humidifier.turn_off` for the off `_apply` decided: `decided` is the command id and the off it saw."""
        if not HUMIDIFIER_ENTITY_RE.fullmatch(entity_id):
            raise ValueError("the watch turns off only a humidifier")
        with self._lock:
            item = self._watched.get(entity_id)
            if item is None or (item.command_id, item.cut) != decided:
                # A Cropilot power_on or power_off came after the decision: that command wins, and no off is sent.
                return
        try:
            self._ha.post("/services/humidifier/turn_off", {"entity_id": entity_id})
        except TransportError:
            # The next read finds it still on, and the retry follows.
            return

    # The rule, under the lock

    def _gone(self, entity_id: str, state: str | _NotFound | None) -> bool:
        """Count the 404 reads in a row; True when HA no longer has the humidifier and the watch let it go."""
        if not isinstance(state, _NotFound):
            self._missing.pop(entity_id, None)
            return False
        if entity_id not in self._watched:
            return True
        now = self._clock.monotonic()
        missing = self._missing.setdefault(entity_id, _Missing(reads=0, since=now))
        missing.reads += 1
        if missing.reads < GONE_AFTER_READS or now - missing.since < GONE_AFTER_SECONDS:
            return False
        self._forget(entity_id)
        self._persist()
        self._log.warning(messages.HUMIDIFIER_GONE.format(entity=entity_id))
        return True

    def _apply(self, entity_id: str, state: str | None) -> tuple[str, str] | None:
        """Update one watched humidifier for one read. When the watch must call `humidifier.turn_off` now, the
        command id and the off it saw, which the call checks again; None otherwise."""
        item = self._watched.get(entity_id)
        if item is None:
            return None
        attempt = self._tries.get(entity_id)
        if state == "off":
            # Also with an off under way: leaving on HA's lag would drop a humidifier that is about to read on. An
            # on in the future is a wall clock set back, which must not hold back that off's event.
            elapsed = self._clock.time() - item.since
            if elapsed < ON_GRACE_SECONDS and (item.cut is None or elapsed >= 0):
                return None
            self._leave(entity_id, item, attempt)
            return None
        if item.cut is None:
            return None
        attempt = self._tries.setdefault(entity_id, _Try())
        now = self._clock.monotonic()
        if attempt.awaiting_check:
            # The read after a try is still not off: that try failed.
            attempt.awaiting_check = False
            if attempt.tries >= OFF_FAILED_AFTER_TRIES and not attempt.off_failed_sent:
                attempt.off_failed_sent = True
                self._queue(entity_id, "off_failed", self._clock.time())
            elapsed = now - (attempt.first_try_at if attempt.first_try_at is not None else now)
            interval = RETRY_FAST_SECONDS if elapsed < RETRY_FAST_FOR_SECONDS else RETRY_SLOW_SECONDS
            attempt.next_try_at = now + interval
            self._log.warning(messages.HUMIDIFIER_OFF_RETRY.format(entity=entity_id, seconds=round(interval)))
            return None
        if attempt.next_try_at is not None and now < attempt.next_try_at:
            return None
        attempt.tries += 1
        attempt.first_try_at = now if attempt.first_try_at is None else attempt.first_try_at
        attempt.tried_at = self._clock.time()
        attempt.awaiting_check = True
        return item.command_id, item.cut

    def _leave(self, entity_id: str, item: Watched, attempt: _Try | None) -> None:
        """It reads off. With an off under way that off is reported, at its last try or, when this process has not
        tried yet (a restart, or an off read first), now: a counted off on a humidifier that is off fails closed.
        Without one it was someone else's off, which is not reported."""
        self._forget(entity_id)
        if item.cut is None:
            self._persist()
            return
        tried_at = attempt.tried_at if attempt is not None else None
        self._queue(entity_id, item.cut, tried_at if tried_at is not None else self._clock.time())
        self._log.warning(CLOSING_LINES[item.cut].format(entity=entity_id))

    def _queue(self, entity_id: str, kind: str, ended_at: float) -> None:
        self._events.append(HumidifierEvent(str(uuid.uuid4()), entity_id, kind, ended_at))
        dropped = len(self._events) - MAX_EVENTS
        if dropped > 0:
            self._events = self._events[dropped:]
            self._log.warning(messages.HUMIDIFIER_EVENTS_DROPPED.format(count=dropped))
        self._persist()

    def _persist(self) -> None:
        self._cap_watched()
        content = HumidifierFile(watched=dict(self._watched), last_ok_at=self._last_ok_at, events=list(self._events))
        try:
            self._store.write(content)
        except OSError as error:
            # The state stays in memory and the next change writes it again; the off never waits for the disk.
            self._log_error(error)
            return
        self._last_ok_written = self._last_ok_at

    def _cap_watched(self) -> None:
        """The file reader refuses more than MAX_WATCHED humidifiers, so the oldest ons go before a write."""
        excess = len(self._watched) - MAX_WATCHED
        if excess <= 0:
            return
        for entity_id in sorted(self._watched, key=lambda key: (self._watched[key].since, key))[:excess]:
            self._forget(entity_id)
        self._log.warning(messages.HUMIDIFIER_WATCHED_DROPPED.format(count=excess))

    def _forget(self, entity_id: str) -> None:
        self._watched.pop(entity_id, None)
        self._tries.pop(entity_id, None)
        self._last_read.pop(entity_id, None)
        self._missing.pop(entity_id, None)

    def _log_error(self, error: Exception) -> None:
        name = type(error).__name__
        now = self._clock.monotonic()
        with self._lock:
            last = self._error_logged.get(name)
            if last is not None and now - last < ERROR_LOG_INTERVAL_SECONDS:
                return
            self._error_logged[name] = now
        self._log.error(messages.UNEXPECTED_ERROR.format(where=WHERE, error=name))
