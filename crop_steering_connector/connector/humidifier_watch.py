"""The humidifier watch: the connector's own off for a humidifier the Cropilot turned on, when the Cropilot stops
answering.

A humidifier enters when a Cropilot `power_on` on a `humidifier` entity with `device_class: humidifier` reaches
Home Assistant: the connector cannot tell the round from the platform's manual toggle, and turning off a
humidifier the platform switched on fails closed. It leaves when a Cropilot `power_off` on it succeeds, when a read
of `off` follows the watch's own off answered with a 2xx, or when HA no longer has the entity. With no off under way
a read of `off` also takes it out, silently: 30 s after the `power_on` once a read showed it `on`, and 15 minutes
after it when none ever did, because a slow humidifier reads `off` long after the call. A humidifier the person
turns on is never watched, with one exception: inside those 900 s an entry never read `on` is still watched, so the
person's `on` reads as the Cropilot's and the humidifier stays in.

Every watched humidifier is turned off on a stop line (a 401 on any endpoint, a refused pairing: the connector's
halt), on a new pairing (before the first claim) and after 20 minutes with no 2xx answer from any Cropilot endpoint.
A `power_on` still in flight when the connector halts enters with the stop line's off already on it. Like the pump
watch it runs in its own daemon thread with its own HA client and never calls the Cropilot; its only HA service is
`humidifier.turn_off`, only on a watched entity. The watched set, the silence timer and the events live in the
humidifier file, so a restart resumes from it before any Cropilot call.

Since 0.2.17 the same watch turns back on a dehumidifier the Cropilot turned off so the humidifier could work. It enters
when a Cropilot `power_off` flagged `restore_on` (at the claim item's level) reaches HA on a `humidifier` of class
`dehumidifier` or on a plug, with a lease of 1500 s from the off. Every claim answer that carries `restore_on_leases`
is the whole list for the connection: it sets the lease of each entry it lists to the server's `until`, read as `until`
minus the same answer's `server_time` on this process's own clocks, and an entry it does not list leaves with no on
(the person's off or on, the device out of its role or of the list); an answer without the key changes nothing, so
with a server that stops renewing every lease runs out. The watch turns the entry on (`lease_on`) when its lease runs
out on either clock, after 20 minutes with no 2xx (`silence_on`), on the halt (`stop_on`) and on a new pairing
(`pair_on`), never before 600 s after the off and with at least 15 s between two of its ons. Once one of these is
decided, it goes on until the entity reads `on`: only a Cropilot command takes it back. An entry also leaves on a
Cropilot `power_on` that succeeded, on a Cropilot `power_off` without the flag, on a read of `on` (30 s after the off
once a read showed it `off`, 15 minutes after it when none did) and when HA no longer has the entity. Its only HA
services are `humidifier.turn_on` and `switch.turn_on` (`LOCAL_RESTORE_SERVICES`, actions.py), only on an entry; a
`humidifier` that no longer reads as a dehumidifier leaves with no on. A restart keeps the entries, each lease never
longer than 1500 s from the restart and the whole 600 s owed again from it. The events (`stop_on`, `pair_on`,
`silence_on`, `lease_on` and `on_failed`) share the humidifier's queue and its `humidifier_events`.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from . import messages
from .actions import DEHUMIDIFIER_DEVICE_CLASS, LOCAL_RESTORE_SERVICES
from .clients import TIMEOUT_SECONDS, HomeAssistantClient, json_body
from .context import Clock
from .humidifier_store import (
    HUMIDIFIER_ENTITY_RE,
    MAX_EVENTS,
    MAX_WATCHED,
    OFF_KINDS,
    ON_FOR_OFF,
    RESTORE_ENTITY_RE,
    HumidifierEvent,
    HumidifierFile,
    HumidifierStore,
    Restore,
    Watched,
)
from .light_store import parse_instant
from .transport import Transport, TransportError

SILENCE_SECONDS = 1200.0
LAST_OK_WRITE_STEP_SECONDS = 60.0
# A read of `off` this soon after the power_on is HA lagging behind its own call, not the person's off.
ON_GRACE_SECONDS = 30.0
# A humidifier never read `on` waits this long for it: above the 10-minute round, whose `power_on` renews `since`,
# and below the 20 minutes of silence, so such an entry leaves by itself before the silence's off.
NEVER_ON_SECONDS = 900.0
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
# The dehumidifier's re-on (0.2.17): the server's lease (HumidifierEpisodeOff::LEASE_SECONDS), the 10-minute
# protection and the gap between two compressor starts of the connection.
LEASE_SECONDS = 1500.0
PROTECTION_SECONDS = 600.0
RESTORE_GAP_SECONDS = 15.0
# A dehumidifier never read `off` waits this long before a read of `on` counts as the person's on.
NEVER_OFF_SECONDS = 900.0
# A renewed lease is written only when it moves by this much; a restart reads it at most this far off.
LEASE_WRITE_STEP_SECONDS = 60.0
MAX_LEASES = 200
RESTORE_CLOSING_LINES = {
    "stop_on": messages.RESTORE_STOP_ON,
    "pair_on": messages.RESTORE_PAIR_ON,
    "silence_on": messages.RESTORE_SILENCE_ON,
    "lease_on": messages.RESTORE_LEASE_ON,
}
LEASE_KEYS = frozenset({"entity_id", "until"})


def parse_leases(raw: Any) -> dict[str, float] | None:
    """`restore_on_leases` of a claim answer, by entity: the `until` of each; None when anything in it is out of the
    contract (not a list, more than 200 items, an item without exactly `entity_id` and `until`, an entity twice), and
    then the answer changes nothing, so the leases run out."""
    if not isinstance(raw, list) or len(raw) > MAX_LEASES:
        return None
    leases: dict[str, float] = {}
    for item in raw:
        if not isinstance(item, dict) or set(item) != LEASE_KEYS:
            return None
        entity_id, until = item["entity_id"], parse_instant(item["until"])
        if not isinstance(entity_id, str) or RESTORE_ENTITY_RE.fullmatch(entity_id) is None or entity_id in leases:
            return None
        if until is None:
            return None
        leases[entity_id] = until
    return leases


@dataclass
class _Try:
    """The retry schedule of one off in memory (5 s for a minute, then 60 s); a restart starts it over.

    `tried_at` is written before the call, so it proves nothing alone; `answered` says HA gave the last try a 2xx."""

    tries: int = 0
    first_try_at: float | None = None
    next_try_at: float | None = None
    tried_at: float = 0.0
    answered: bool = False
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
        # The re-on's entries (0.2.17), and what of them the file cannot keep: the retries, the reads, the 600 s and
        # the lease on the monotonic clock, and the lease on the wall clock as this process last set it.
        self._restore = content.restore
        self._restore_tries: dict[str, _Try] = {}
        self._restore_last_read: dict[str, float] = {}
        self._restore_missing: dict[str, _Missing] = {}
        self._protect_until: dict[str, float] = {}
        self._lease_mono: dict[str, float] = {}
        self._lease_wall: dict[str, float] = {}
        self._last_on_call = float("-inf")
        for entity_id, item in content.restore.items():
            self._resume(entity_id, item)
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

    def restore_entered(self, entity_id: str, command_id: str) -> None:
        """A Cropilot `power_off` flagged `restore_on` reached HA on a dehumidifier: it enters, or starts over, with
        the lease and the 600 s counted from now. As with a `power_on` of a humidifier, a halt the trigger may have
        missed marks it for the stop line's on at once."""
        if not RESTORE_ENTITY_RE.fullmatch(entity_id):
            return
        with self._lock:
            halted = self._halt is not None and self._halt.is_set()
            now, mono = self._clock.time(), self._clock.monotonic()
            self._forget_restore(entity_id)
            self._restore[entity_id] = Restore(
                off_at=now, lease_until=now + LEASE_SECONDS, command_id=command_id, cut="stop_on" if halted else None
            )
            self._protect_until[entity_id] = mono + PROTECTION_SECONDS
            self._lease_mono[entity_id] = mono + LEASE_SECONDS
            self._lease_wall[entity_id] = now + LEASE_SECONDS
            self._persist()
        self._wake.set()

    def restore_left(self, entity_id: str) -> None:
        """A Cropilot `power_on` on it succeeded, or a Cropilot `power_off` without the flag ran on it: the Cropilot's
        own command decides now, and the entry leaves with no on."""
        with self._lock:
            if entity_id not in self._restore:
                return
            self._forget_restore(entity_id)
            self._persist()

    def leases(self, raw: Any, server_time: Any, received: float) -> None:
        """A claim answer that carries `restore_on_leases`, with its `server_time` and the monotonic instant it came:
        the whole list for the connection. An entry whose on is already decided keeps it; an answer this version
        cannot read, or one without a `server_time`, changes nothing."""
        listed = parse_leases(raw)
        moment = parse_instant(server_time)
        if listed is None or moment is None:
            return
        with self._lock:
            # The wall instant of the answer, so a slow pass between it and here never lengthens a lease.
            arrived = self._clock.time() - (self._clock.monotonic() - received)
            changed = False
            for entity_id, item in sorted(self._restore.items()):
                if item.cut is not None:
                    continue
                until = listed.get(entity_id)
                if until is None:
                    self._forget_restore(entity_id)
                    changed = True
                    continue
                left = min(max(until - moment, 0.0), LEASE_SECONDS)
                self._lease_mono[entity_id] = received + left
                self._lease_wall[entity_id] = arrived + left
                if abs(arrived + left - item.lease_until) >= LEASE_WRITE_STEP_SECONDS:
                    self._restore[entity_id] = replace(item, lease_until=arrived + left)
                    changed = True
            if changed:
                self._persist()
        self._wake.set()

    def server_answered(self) -> None:
        """A 2xx from any Cropilot endpoint; the file follows when the instant moves by 60 s or more."""
        now = self._clock.time()
        with self._lock:
            self._last_ok_at = now
            self._last_ok_mono = self._clock.monotonic()
            if self._last_ok_written is None or abs(now - self._last_ok_written) >= LAST_OK_WRITE_STEP_SECONDS:
                self._persist()

    def paired(self) -> None:
        """A new pairing succeeded: every watched humidifier is turned off before the first claim, and every
        dehumidifier of the re-on is turned back on once its 600 s have passed."""
        self.trigger("pair_off")

    def trigger(self, kind: str) -> None:
        """Mark every watched humidifier for an off of `kind`, and every dehumidifier of the re-on for the on that
        answers it, and run a pass at once, in the caller's thread.

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

    def restoring(self) -> dict[str, Restore]:
        """The dehumidifiers the re-on follows."""
        with self._lock:
            return dict(self._restore)

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
            self._expire_leases()
            due = self._due()
            due_restore = self._due_restore()
        visits = [(self._watch, entity_id) for entity_id in due]
        visits += [(self._watch_restore, entity_id) for entity_id in due_restore]
        for visit, entity_id in visits:
            if self._preempt.is_set():
                # A trigger waits for this pass: it runs a whole one itself.
                return
            try:
                visit(entity_id)
            except Exception as error:  # noqa: BLE001 - one device's defect must not skip the others
                self._log_error(error)

    def _silent(self) -> bool:
        if not self._watched and not self._restore:
            return False
        # With no 2xx proven nothing shows the Cropilot is answering: fail closed.
        if self._last_ok_at is None or self._last_ok_mono is None:
            return True
        # The longer of the two clocks: a wall clock set back cannot stretch the silence, nor a paused VM, whose
        # monotonic clock stands still, hide it.
        silence = max(self._clock.monotonic() - self._last_ok_mono, self._clock.time() - self._last_ok_at)
        return silence >= SILENCE_SECONDS

    def _mark(self, kind: str) -> bool:
        """Under the lock: give every watched humidifier without an off this one, and every dehumidifier of the re-on
        without an on the one that answers it; True when any is watched."""
        changed = False
        for entity_id, item in self._watched.items():
            if item.cut is None:
                self._watched[entity_id] = replace(item, cut=kind)
                changed = True
        on = ON_FOR_OFF[kind]
        for entity_id, entry in self._restore.items():
            if entry.cut is None:
                self._restore[entity_id] = replace(entry, cut=on)
                changed = True
        if changed:
            self._persist()
        return bool(self._watched) or bool(self._restore)

    def _due(self) -> list[str]:
        now = self._clock.monotonic()
        return [
            entity_id
            for entity_id, item in sorted(self._watched.items())
            if item.cut is not None or now - self._last_read.get(entity_id, float("-inf")) >= POLL_IDLE_SECONDS
        ]

    def _cadence(self) -> float:
        with self._lock:
            mono = self._clock.monotonic()
            hot = any(item.cut is not None for item in self._watched.values()) or any(
                self._restore_ready(entity_id, item, mono) for entity_id, item in self._restore.items()
            )
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
            response = self._ha.post("/services/humidifier/turn_off", {"entity_id": entity_id})
        except TransportError:
            # Not answered: the next read finds the try unanswered, and the retry follows.
            return
        if not 200 <= response.status < 300:
            return
        with self._lock:
            item = self._watched.get(entity_id)
            attempt = self._tries.get(entity_id)
            if item is not None and attempt is not None and (item.command_id, item.cut) == decided:
                attempt.answered = True

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
        if state == "on" and not item.seen_on:
            item = replace(item, seen_on=True)
            self._watched[entity_id] = item
            self._persist()
        attempt = self._tries.get(entity_id)
        if state == "off":
            # Also with an off under way: acting on HA's lag would drop a humidifier that is about to read on. An
            # on in the future is a wall clock set back, which must not hold back that off.
            elapsed = self._clock.time() - item.since
            wait = NEVER_ON_SECONDS if item.cut is None and not item.seen_on else ON_GRACE_SECONDS
            if elapsed < wait and (item.cut is None or elapsed >= 0):
                return None
            if item.cut is None:
                self._forget(entity_id)
                self._persist()
                return None
            if attempt is not None and attempt.answered:
                self._leave(entity_id, item, attempt)
                return None
        if item.cut is None:
            return None
        attempt = self._tries.setdefault(entity_id, _Try())
        now = self._clock.monotonic()
        if attempt.awaiting_check:
            # The read after a try is neither off nor backed by a 2xx: that try failed.
            attempt.awaiting_check = False
            if attempt.tries >= OFF_FAILED_AFTER_TRIES and not attempt.off_failed_sent:
                attempt.off_failed_sent = True
                self._queue(entity_id, "off_failed", self._clock.time())
            elapsed = now - (attempt.first_try_at if attempt.first_try_at is not None else now)
            interval = RETRY_FAST_SECONDS if elapsed < RETRY_FAST_FOR_SECONDS else RETRY_SLOW_SECONDS
            attempt.next_try_at = now + interval
            line = messages.HUMIDIFIER_OFF_UNCONFIRMED if state == "off" else messages.HUMIDIFIER_OFF_RETRY
            self._log.warning(line.format(entity=entity_id, seconds=round(interval)))
            return None
        if attempt.next_try_at is not None and now < attempt.next_try_at:
            return None
        attempt.tries += 1
        attempt.first_try_at = now if attempt.first_try_at is None else attempt.first_try_at
        attempt.tried_at = self._clock.time()
        attempt.answered = False
        attempt.awaiting_check = True
        return item.command_id, item.cut

    def _leave(self, entity_id: str, item: Watched, attempt: _Try) -> None:
        """It reads off after a try HA answered with a 2xx: that off is reported, at that try."""
        self._forget(entity_id)
        self._queue(entity_id, item.cut, attempt.tried_at)
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
        self._cap_restore()
        content = HumidifierFile(
            watched=dict(self._watched),
            last_ok_at=self._last_ok_at,
            events=list(self._events),
            restore=dict(self._restore),
        )
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

    # The dehumidifier's re-on (0.2.17), under the lock unless said otherwise

    def _resume(self, entity_id: str, item: Restore) -> None:
        """An entry read from the file: the whole 600 s owed again from now, and the lease left, at most 1500 s from
        now. The wall clock cannot tell how long ago the off was: one jumping forward across the restart (an HA
        without a clock battery syncing NTP) would cut the protection short, so the on waits instead; one set back
        never stretches the lease beyond 1500 s."""
        now, mono = self._clock.time(), self._clock.monotonic()
        self._protect_until[entity_id] = mono + PROTECTION_SECONDS
        left = min(max(item.lease_until - now, 0.0), LEASE_SECONDS)
        self._lease_mono[entity_id] = mono + left
        self._lease_wall[entity_id] = now + left

    def _expire_leases(self) -> None:
        """An entry with no on decided whose lease ran out on either clock gets `lease_on`."""
        mono, now = self._clock.monotonic(), self._clock.time()
        changed = False
        for entity_id, item in list(self._restore.items()):
            if item.cut is not None:
                continue
            lease_mono = self._lease_mono.get(entity_id, float("-inf"))
            if mono >= lease_mono or now >= self._lease_wall.get(entity_id, float("-inf")):
                self._restore[entity_id] = replace(item, cut="lease_on")
                changed = True
        if changed:
            self._persist()

    def _restore_ready(self, entity_id: str, item: Restore, mono: float) -> bool:
        """The entry's on is decided and may go now, or its call waits for the read that checks it."""
        if item.cut is None or mono < self._protect_until.get(entity_id, float("-inf")):
            return False
        attempt = self._restore_tries.get(entity_id)
        if attempt is not None and attempt.awaiting_check:
            return True
        if attempt is not None and attempt.next_try_at is not None and mono < attempt.next_try_at:
            return False
        return mono >= self._last_on_call + RESTORE_GAP_SECONDS

    def _due_restore(self) -> list[str]:
        mono = self._clock.monotonic()
        return [
            entity_id
            for entity_id, item in sorted(self._restore.items())
            if self._restore_ready(entity_id, item, mono)
            or mono - self._restore_last_read.get(entity_id, float("-inf")) >= POLL_IDLE_SECONDS
        ]

    def _watch_restore(self, entity_id: str) -> None:
        """Outside the lock: one read of a dehumidifier of the re-on, the rule, and the on it decided."""
        state, device_class = self._read_restore(entity_id)
        with self._lock:
            self._restore_last_read[entity_id] = self._clock.monotonic()
            if self._restore_gone(entity_id, state):
                return
            decided = self._apply_restore(entity_id, None if isinstance(state, _NotFound) else state, device_class)
        if decided is not None:
            self._turn_on(entity_id, decided)

    def _read_restore(self, entity_id: str) -> tuple[str | _NotFound | None, str | None]:
        """The entity's state and its `device_class`, as `_read` reads the state; no class when HA gives none."""
        try:
            response = self._ha.get(f"/states/{entity_id}")
        except TransportError:
            return None, None
        if response.status == 404:
            return NOT_FOUND, None
        payload = json_body(response) if response.status == 200 else None
        if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
            return None, None
        attributes = payload.get("attributes")
        device_class = attributes.get("device_class") if isinstance(attributes, dict) else None
        return payload["state"], device_class if isinstance(device_class, str) else None

    def _turn_on(self, entity_id: str, decided: tuple[str, str]) -> None:
        """Outside the lock: the service call of the on `_apply_restore` decided, checked again first."""
        service = LOCAL_RESTORE_SERVICES.get(entity_id.split(".", 1)[0])
        if service is None or not RESTORE_ENTITY_RE.fullmatch(entity_id):
            raise ValueError("the watch turns on only a dehumidifier or its plug")
        with self._lock:
            item = self._restore.get(entity_id)
            if item is None or (item.command_id, item.cut) != decided:
                # A Cropilot command or an answer's list came after the decision: it wins, and no on is sent.
                return
        try:
            response = self._ha.post(f"/services/{service}", {"entity_id": entity_id})
        except TransportError:
            return
        if not 200 <= response.status < 300:
            return
        with self._lock:
            item = self._restore.get(entity_id)
            attempt = self._restore_tries.get(entity_id)
            if item is not None and attempt is not None and (item.command_id, item.cut) == decided:
                attempt.answered = True

    def _restore_gone(self, entity_id: str, state: str | _NotFound | None) -> bool:
        """`_gone` for an entry of the re-on: True when HA no longer has it and the entry left, with no on."""
        if not isinstance(state, _NotFound):
            self._restore_missing.pop(entity_id, None)
            return False
        if entity_id not in self._restore:
            return True
        now = self._clock.monotonic()
        missing = self._restore_missing.setdefault(entity_id, _Missing(reads=0, since=now))
        missing.reads += 1
        if missing.reads < GONE_AFTER_READS or now - missing.since < GONE_AFTER_SECONDS:
            return False
        self._forget_restore(entity_id)
        self._persist()
        self._log.warning(messages.RESTORE_GONE.format(entity=entity_id))
        return True

    def _apply_restore(self, entity_id: str, state: str | None, device_class: str | None) -> tuple[str, str] | None:
        """`_apply` turned around, for one read of an entry of the re-on: the command id and the on to call now, or
        None."""
        item = self._restore.get(entity_id)
        if item is None:
            return None
        if entity_id.startswith("humidifier.") and state in ("on", "off") and device_class != DEHUMIDIFIER_DEVICE_CLASS:
            # Never turn on what may now be a humidifier.
            self._forget_restore(entity_id)
            self._persist()
            self._log.warning(messages.RESTORE_NOT_DEHUMIDIFIER.format(entity=entity_id))
            return None
        if state == "off" and not item.seen_off:
            item = replace(item, seen_off=True)
            self._restore[entity_id] = item
            self._persist()
        attempt = self._restore_tries.get(entity_id)
        if state == "on":
            # A read of `on` this soon after the off is HA's lag; one later is the person's on, or the watch's own.
            elapsed = self._clock.time() - item.off_at
            wait = NEVER_OFF_SECONDS if item.cut is None and not item.seen_off else ON_GRACE_SECONDS
            if elapsed < wait and (item.cut is None or elapsed >= 0):
                return None
            if item.cut is None:
                self._forget_restore(entity_id)
                self._persist()
                return None
            if attempt is not None and attempt.answered:
                self._forget_restore(entity_id)
                self._queue(entity_id, item.cut, attempt.tried_at)
                self._log.warning(RESTORE_CLOSING_LINES[item.cut].format(entity=entity_id))
                return None
        mono = self._clock.monotonic()
        if item.cut is None or mono < self._protect_until.get(entity_id, float("-inf")):
            return None
        attempt = self._restore_tries.setdefault(entity_id, _Try())
        if attempt.awaiting_check:
            # The read after a try is neither on nor backed by a 2xx: that try failed.
            attempt.awaiting_check = False
            if attempt.tries >= OFF_FAILED_AFTER_TRIES and not attempt.off_failed_sent:
                attempt.off_failed_sent = True
                self._queue(entity_id, "on_failed", self._clock.time())
            elapsed = mono - (attempt.first_try_at if attempt.first_try_at is not None else mono)
            # The first minute's retries keep the 15 s between two ons, so the line says the real wait.
            interval = RESTORE_GAP_SECONDS if elapsed < RETRY_FAST_FOR_SECONDS else RETRY_SLOW_SECONDS
            attempt.next_try_at = mono + interval
            line = messages.RESTORE_ON_UNCONFIRMED if state == "on" else messages.RESTORE_ON_RETRY
            self._log.warning(line.format(entity=entity_id, seconds=round(interval)))
            return None
        if attempt.next_try_at is not None and mono < attempt.next_try_at:
            return None
        if mono < self._last_on_call + RESTORE_GAP_SECONDS:
            return None
        self._last_on_call = mono
        attempt.tries += 1
        attempt.first_try_at = mono if attempt.first_try_at is None else attempt.first_try_at
        attempt.tried_at = self._clock.time()
        attempt.answered = False
        attempt.awaiting_check = True
        return item.command_id, item.cut

    def _cap_restore(self) -> None:
        """The file reader refuses more than MAX_WATCHED entries, so the oldest offs go before a write."""
        excess = len(self._restore) - MAX_WATCHED
        if excess <= 0:
            return
        for entity_id in sorted(self._restore, key=lambda key: (self._restore[key].off_at, key))[:excess]:
            self._forget_restore(entity_id)
        self._log.warning(messages.RESTORE_WATCHED_DROPPED.format(count=excess))

    def _forget_restore(self, entity_id: str) -> None:
        for table in (
            self._restore,
            self._restore_tries,
            self._restore_last_read,
            self._restore_missing,
            self._protect_until,
            self._lease_mono,
            self._lease_wall,
        ):
            table.pop(entity_id, None)

    def _log_error(self, error: Exception) -> None:
        name = type(error).__name__
        now = self._clock.monotonic()
        with self._lock:
            last = self._error_logged.get(name)
            if last is not None and now - last < ERROR_LOG_INTERVAL_SECONDS:
                return
            self._error_logged[name] = now
        self._log.error(messages.UNEXPECTED_ERROR.format(where=WHERE, error=name))
