"""The light controller: the connector switches a light plug at the instants of the phase schedule the Cropilot sends,
with no timer on the plug, in a space where the person turned that on.

The server decides and the connector executes. Each accepted report answer carries, on the light plug's allowlist
entry, the marker `light`: the instants of the next 7 days, the plug's HA device and whether the plug answers only
through the cloud (light_store.py). The controller keeps the last valid schedule in its file and switches the plug
within seconds of each instant, locally, also with the Cropilot out of reach, until the schedule's `valid_until`;
then it stops with the light as it is. Only an explicit `"light": null` in an answer with `light_control_supported`
stops a plug before that; a missing key, an entity missing from the list and an answer without the flag stop nothing.
No stop sends a last off.

Edge-triggered, like a timer: each transition is applied once, retried until the plug reads it. An off in force
applies at once. An on applies only when nothing was applied yet for the plug, or when it is the on armed ahead
(seen in the schedule before its instant) and crossed by the clock: a schedule that changes in the middle of a period
never lights the plant, and the light waits for the next on. A manual change stands until the next transition,
except a light on in the dark period, which the night guard turns off after 15 minutes on, at most 3 times a night.

Time. From the first contact of the process (an accepted answer with the flag and a `server_time`), the controller's
time is the last `server_time` plus the monotonic clock since it, for the rest of the process, and it holds every
action while that time and the wall clock are more than 300 s apart. Nothing acts in the first 10 minutes of a
process before a contact. From then until the first contact the wall clock drives the rule: the off and the night
guard on every plug, and the on only on a plug the server marked `cloud_only`, after that plug has answered for 10
minutes in a row (read every 30 s from the end of those first 10 minutes): such a plug answers only with the internet
up, and the wait gives NTP time to set the clock. A plug that answers without the internet turns on only after the
first contact.

A plug is switched only while the last good map read of this process gives it the HA device of its marker; another
device drops its schedule. The only HA services are `switch.turn_on` and `switch.turn_off` (`LOCAL_LIGHT_SERVICES`,
in actions.py), body exactly `{entity_id}`. Like the watches it runs in its own daemon thread with
its own HA client, starts before any Cropilot call, never calls the Cropilot and never calls the Supervisor. Every
halt of the connector and every new pairing drop every schedule, with the lights as they are.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import messages
from .actions import LOCAL_LIGHT_SERVICES
from .clients import HomeAssistantClient, json_body
from .context import Clock
from .light_store import (
    LIGHT_ENTITY_RE,
    MAX_ATTEMPTS,
    MAX_ENTITIES,
    MAX_EVENTS,
    STATES,
    VOID,
    HeldLight,
    LightEvent,
    LightFile,
    LightStore,
    Transition,
    parse_instant,
    parse_marker,
    utc_iso,
)
from .transport import Transport, TransportError

STARTING = "starting"
BEFORE_CONTACT = "before_contact"
ANCHORED = "anchored"
PASS_SECONDS = 15.0
MIN_PASS_SECONDS = 1.0
STARTING_SECONDS = 600.0
LOCK_SECONDS = 600.0
LOCK_READ_SECONDS = 30.0
WAIT_READ_SECONDS = 30.0
CHECK_AFTER_SECONDS = 10.0
# The gap from one try to the next: 10 s, 60 s, then 300 s while the transition stands.
RETRY_DELAYS = (10.0, 60.0, 300.0)
FAILED_AFTER_TRIES = 3
SKEW_SECONDS = 300.0
JUMP_BACK_SECONDS = 300.0
GUARD_START_SECONDS = 900.0
GUARD_ON_SECONDS = 900.0
GUARD_READ_SECONDS = 60.0
GUARD_MAX_OFFS = 3
CAP_CALLS = 20
CAP_WINDOW_SECONDS = 3600.0
LOG_INTERVAL_SECONDS = 3600.0
# `last_time` rides on every write of the file; alone, at most once a minute while a schedule is held.
LAST_TIME_WRITE_SECONDS = 60.0
REPORT_KEYS_MAX_BYTES = 16384
UNAVAILABLE_STATES = frozenset({"unavailable", "unknown"})
HALT_POLL_SECONDS = 1.0
ERROR_LOG_INTERVAL_SECONDS = 60.0
VERBS = {"on": "acender", "off": "apagar"}
WHERE = "iluminação"


@dataclass
class _Pending:
    """The transition the rule applies, in memory: the next read (before a try, after it or while waiting)."""

    transition: Transition
    tries: int = 0
    next_at: float = 0.0
    called_at: float = 0.0
    awaiting_check: bool = False
    failed_sent: bool = False


@dataclass
class _Guard:
    """The night guard of one dark period, named by its off."""

    off_at: float
    on_since: float | None = None
    offs: int = 0
    attempts: int = 0
    check_at: float | None = None
    read_at: float = float("-inf")


@dataclass
class _Live:
    pending: _Pending | None = None
    guard: _Guard | None = None
    calls: list[float] = field(default_factory=list)
    capped_at: float | None = None


@dataclass(frozen=True)
class _Plan:
    """What one visit reads the plug for: the pending transition, the night guard, or only the lock."""

    device_id: str
    pending: Transition | None
    guard: bool


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _when(moment: float) -> str:
    return datetime.fromtimestamp(int(moment), tz=timezone.utc).strftime("%d/%m/%Y %H:%M UTC")


def markers_of(entries: Iterable[Any]) -> dict[str, Any]:
    """The `light` key of every allowlist entry that has one, by entity: an object, null or anything else."""
    return {
        entry["entity_id"]: entry["light"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("entity_id"), str) and "light" in entry
    }


def compressor_switches(states: Iterable[Any], devices: Mapping[str, str | None] | None) -> frozenset[str]:
    """The switches whose HA device also holds a climate or a humidifier in these states: compressor plugs."""
    if devices is None:
        return frozenset()
    ids = [item["entity_id"] for item in states if isinstance(item, dict) and isinstance(item.get("entity_id"), str)]
    holders = {devices.get(entity_id) for entity_id in ids if entity_id.split(".", 1)[0] in ("climate", "humidifier")}
    holders.discard(None)
    return frozenset(item for item in ids if item.startswith("switch.") and devices.get(item) in holders)


def report_keys(
    schedules: Sequence[tuple[str, float]],
    events: Sequence[LightEvent],
    now: float,
    max_bytes: int = REPORT_KEYS_MAX_BYTES,
) -> tuple[list[dict], list[dict], list[LightEvent]]:
    """`light_control`, whole, and the oldest `light_events` that fit beside it in `max_bytes` encoded; returns the
    two lists and the events sent, so the rest stays queued for the next report."""
    control = [{"entity_id": entity_id, "valid_until": utc_iso(until)} for entity_id, until in schedules]
    wires: list[dict] = []
    kept: list[LightEvent] = []
    for event in events:
        wire = event.wire(now)
        if len(_encode({"light_control": control, "light_events": [*wires, wire]})) > max_bytes:
            break
        wires.append(wire)
        kept.append(event)
    return control, wires, kept


class LightControl:
    def __init__(
        self,
        *,
        data_dir: Path,
        transport: Transport,
        supervisor_token: str,
        clock: Clock,
        log: logging.Logger,
    ) -> None:
        self._store = LightStore(data_dir)
        # Its own event, never set: the connector's halt is a trigger of this controller, never its end.
        self._ha = HomeAssistantClient(transport, supervisor_token, threading.Event())
        self._clock = clock
        self._log = log
        self._lock = threading.RLock()
        self._pass_lock = threading.Lock()
        self._wake = threading.Event()
        self._retired = threading.Event()
        self._thread: threading.Thread | None = None
        self._halt_thread: threading.Thread | None = None
        self._started = clock.monotonic()
        self._anchor: tuple[float, float] | None = None
        self._device_of: dict[str, str | None] | None = None
        self._pumps: frozenset[str] = frozenset()
        self._compressors: frozenset[str] = frozenset()
        self._live: dict[str, _Live] = {}
        # The lock on the on before the first contact, per plug for the process: its first answering read.
        self._answering: dict[str, float | None] = {}
        self._lock_read_at: dict[str, float] = {}
        self._logged: dict[tuple[str, str], float] = {}
        self._skew_logged: float | None = None
        self._error_logged: dict[str, float] = {}
        content = self._store.read()
        if content is None:
            self._store.set_aside()
            log.warning(messages.LIGHT_FILE_UNREADABLE)
            content = LightFile()
        self._held = content.entities
        self._events = content.events
        self._last_time = content.last_time
        self._written_at = self._started
        with self._lock:
            # At load only the wall clock can expire a schedule: dropping is the safe side.
            self._expire(clock.time(), None)

    # The threads

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="iluminacao", daemon=True)
        self._thread.start()

    def follow_halt(self, halt: threading.Event) -> None:
        """Drop every schedule the moment the connector halts on a stop line or an idle line of the start."""

        def follow() -> None:
            while not self._retired.is_set():
                if halt.wait(HALT_POLL_SECONDS):
                    self.stop()
                    return

        self._halt_thread = threading.Thread(target=follow, name="iluminacao-parada", daemon=True)
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
            try:
                self._pass()
            except Exception as error:  # noqa: BLE001 - the controller must survive any defect of one pass
                self._log_error(error)
        return self._cadence()

    # Calls from the report loop and the start step

    def replace(
        self,
        markers: Mapping[str, Any],
        supported: bool,
        server_time: Any,
        compressors: Iterable[str],
        pumps: Iterable[str],
        device_of: Mapping[str, str | None] | None,
    ) -> None:
        """An accepted report answer: its light markers by entity (an object, null, or absent), its
        `light_control_supported` and `server_time`, the switches known to be compressor plugs, the pump list and
        the HA device of each entity in the last good map read."""
        received = self._clock.monotonic()
        contact = parse_instant(server_time) if supported is True else None
        with self._lock:
            self._pumps = frozenset(pumps)
            self._compressors = frozenset(compressors)
            changed = self._take_map(device_of) if device_of is not None else False
            for entity_id, raw in markers.items():
                if raw is not None:
                    changed = self._accept(entity_id, raw) or changed
                elif supported is True and entity_id in self._held:
                    self._forget(entity_id)
                    changed = True
            if contact is not None:
                self._anchor = (contact, received)
            if changed:
                self._persist()
        self._wake.set()

    def devices(self, device_of: Mapping[str, str | None]) -> None:
        """A good map read: the HA device of each entity, the one the device check reads from now on."""
        with self._lock:
            if self._take_map(device_of):
                self._persist()
        self._wake.set()

    def paired(self) -> None:
        """A new pairing succeeded: every schedule goes before the first claim; the first accepted answer of this
        pairing brings them back as new entities."""
        with self._lock:
            self._drop_all()

    def stop(self) -> None:
        """A halt: every schedule goes, from memory and the file, and the lights stay as they are."""
        with self._lock:
            held = bool(self._held)
            self._drop_all()
        if held:
            self._log.warning(messages.LIGHT_STOPPED)

    def schedules(self) -> list[tuple[str, float]]:
        """The schedules held, as the report's `light_control` lists them: entity and `valid_until`, by entity."""
        with self._lock:
            return [(entity_id, self._held[entity_id].schedule.valid_until) for entity_id in sorted(self._held)]

    def events_waiting(self) -> bool:
        with self._lock:
            return bool(self._events)

    def snapshot_events(self) -> list[LightEvent]:
        with self._lock:
            return list(self._events)

    def drop_events(self, ids: Iterable[str]) -> None:
        sent = set(ids)
        with self._lock:
            kept = [event for event in self._events if event.event_id not in sent]
            if len(kept) != len(self._events):
                self._events = kept
                self._persist()

    # The answer, under the lock

    def _accept(self, entity_id: str, raw: Any) -> bool:
        """One marker of an accepted answer; True when the held schedules changed. A refused marker keeps the last
        valid schedule of the entity until its `valid_until`."""
        if not LIGHT_ENTITY_RE.fullmatch(entity_id):
            return False
        held = self._held.get(entity_id)
        marker = parse_marker(raw)
        if marker is None:
            line = messages.LIGHT_SCHEDULE_INVALID_NONE_HELD
            if held is not None:
                line = messages.LIGHT_SCHEDULE_INVALID.format(entity=entity_id, data=_when(held.schedule.valid_until))
            self._log_once("invalid", entity_id, line)
            return False
        if entity_id in self._pumps or entity_id in self._compressors:
            self._log_once("refused", entity_id, messages.LIGHT_REFUSED_PLUG.format(entity=entity_id))
            return False
        device = self._device_of.get(entity_id) if self._device_of is not None else None
        if device is None:
            self._log_once("unknown", entity_id, messages.LIGHT_DEVICE_UNKNOWN.format(entity=entity_id))
            return False
        if device != marker.device_id:
            self._log_once("mismatch", entity_id, messages.LIGHT_DEVICE_MISMATCH.format(entity=entity_id))
            return False
        if held is None or held.device_id != marker.device_id:
            # A new entity, or the plug pinned anew: nothing applied yet.
            if held is None and len(self._held) >= MAX_ENTITIES:
                return False
            self._forget(entity_id)
            self._held[entity_id] = HeldLight(marker.schedule, marker.device_id, marker.cloud_only)
            return True
        if (held.schedule, held.cloud_only) == (marker.schedule, marker.cloud_only):
            return False
        held.schedule = marker.schedule
        held.cloud_only = marker.cloud_only
        return True

    def _take_map(self, device_of: Mapping[str, str | None]) -> bool:
        """Keep the map; drop every schedule whose entity the map gives to another HA device."""
        self._device_of = dict(device_of)
        changed = False
        for entity_id in sorted(self._held):
            device = self._device_of.get(entity_id)
            if isinstance(device, str) and device != self._held[entity_id].device_id:
                self._forget(entity_id)
                self._log.warning(messages.LIGHT_DEVICE_CHANGED.format(entity=entity_id))
                changed = True
        return changed

    def _drop_all(self) -> None:
        if not self._held:
            return
        for entity_id in list(self._held):
            self._forget(entity_id)
        self._persist()

    def _forget(self, entity_id: str) -> None:
        self._held.pop(entity_id, None)
        self._live.pop(entity_id, None)

    # One pass

    def _pass(self) -> None:
        now, wall = self._clock.monotonic(), self._clock.time()
        with self._lock:
            mode, moment = self._mode(now, wall)
            self._expire(wall, moment)
            if moment is None:
                return
            self._jump(moment)
            self._last_time = moment
            if self._held and now - self._written_at >= LAST_TIME_WRITE_SECONDS:
                self._persist()
            if mode == ANCHORED and not self._acting(mode, moment, wall):
                self._log_skew(now)
            entities = sorted(self._held)
        for entity_id in entities:
            try:
                self._visit(entity_id)
            except Exception as error:  # noqa: BLE001 - one plug's defect must not skip the others
                self._log_error(error)

    def _mode(self, now: float, wall: float) -> tuple[str, float | None]:
        """The mode and the controller's time `t` (None while Starting)."""
        if self._anchor is not None:
            server_time, received = self._anchor
            return ANCHORED, server_time + (now - received)
        if now - self._started < STARTING_SECONDS:
            return STARTING, None
        return BEFORE_CONTACT, wall

    @staticmethod
    def _acting(mode: str, moment: float, wall: float) -> bool:
        if mode == BEFORE_CONTACT:
            return True
        return mode == ANCHORED and abs(moment - wall) <= SKEW_SECONDS

    def _expire(self, wall: float, moment: float | None) -> None:
        """Past `valid_until` by `t` or by the wall clock, whichever comes first, a schedule is dropped."""
        for entity_id in sorted(self._held):
            held = self._held[entity_id]
            until = held.schedule.valid_until
            if wall < until and (moment is None or moment < until):
                continue
            last = held.schedule.current(until)
            self._forget(entity_id)
            self._queue(entity_id, "expired", last.state if last is not None else "off", until, 0)
            self._log.warning(messages.LIGHT_EXPIRED.format(entity=entity_id))

    def _jump(self, moment: float) -> None:
        """`t` more than 300 s before the last pass: every transition applied becomes void."""
        if self._last_time is None or moment >= self._last_time - JUMP_BACK_SECONDS or not self._held:
            return
        for held in self._held.values():
            if held.applied is not None:
                held.applied = VOID
        self._log.warning(messages.LIGHT_CLOCK_BACK)
        self._persist()

    def _visit(self, entity_id: str) -> None:
        """One plug: at most one read and at most one call, each outside the lock."""
        with self._lock:
            plan = self._plan(entity_id)
        if plan is None:
            return
        state = self._read(entity_id)
        with self._lock:
            call = self._after_read(entity_id, plan, state)
        if call is None:
            return
        running = self._running()
        with self._lock:
            if not self._still_planned(entity_id, plan):
                return
            if not running:
                self._wait(entity_id)
                return
        self._call(entity_id, call[0])
        with self._lock:
            self._after_call(entity_id, plan, call[1])

    def _plan(self, entity_id: str) -> _Plan | None:
        held = self._held.get(entity_id)
        if held is None:
            return None
        now, wall = self._clock.monotonic(), self._clock.time()
        mode, moment = self._mode(now, wall)
        if moment is None:
            return None
        live = self._live.setdefault(entity_id, _Live())
        lock_due = (
            mode == BEFORE_CONTACT
            and held.cloud_only
            and now - self._lock_read_at.get(entity_id, float("-inf")) >= LOCK_READ_SECONDS
        )
        pending_due = guard_due = False
        if self._acting(mode, moment, wall) and self._device_ok(entity_id, held):
            target = self._rule(held, moment)
            if target is None:
                live.pending = None
            elif live.pending is None or live.pending.transition != target:
                # A newer transition replaces a pending one; every try reads the plug first.
                live.pending = _Pending(target, next_at=now)
            pending_due = live.pending is not None and now >= live.pending.next_at
            guard_due = live.pending is None and self._guard_due(held, live, moment, now)
        if not (lock_due or pending_due or guard_due):
            return None
        return _Plan(held.device_id, live.pending.transition if pending_due and live.pending else None, guard_due)

    def _rule(self, held: HeldLight, moment: float) -> Transition | None:
        """The crossing of the armed on, then the table: the transition to apply now, or None."""
        schedule = held.schedule
        current = schedule.current(moment)
        applied = held.applied
        armed = held.armed
        if armed is not None and isinstance(applied, Transition) and applied == Transition(armed, "on"):
            armed = None
        if armed is not None and (not schedule.on_at(armed) or (current is not None and current.at > armed)):
            armed = None
        if armed is None:
            armed = schedule.first_on_after(moment)
        if armed != held.armed:
            held.armed = armed
            self._persist()
        if current is None:
            return None
        if isinstance(applied, Transition):
            if applied == current:
                return None
            if current.at < applied.at and schedule.has(applied):
                return None
        if applied is None or current.state == "off":
            return current
        crossed = armed is not None and current == Transition(armed, "on")
        return current if crossed else None

    @staticmethod
    def _dark(held: HeldLight, moment: float) -> Transition | None:
        """The night guard's period: the applied off in force, when an on of the schedule ends it."""
        current = held.schedule.current(moment)
        if current is None or current.state != "off" or held.applied != current:
            return None
        return current if held.schedule.on_after(current.at) else None

    def _guard_due(self, held: HeldLight, live: _Live, moment: float, now: float) -> bool:
        """The night guard reads the plug: an applied off whose dark period ends with an on, from 15 minutes in."""
        dark = self._dark(held, moment)
        if dark is None:
            live.guard = None
            return False
        if live.guard is None or live.guard.off_at != dark.at:
            live.guard = _Guard(off_at=dark.at)
        guard = live.guard
        if moment < dark.at + GUARD_START_SECONDS:
            return False
        if guard.check_at is not None:
            return now >= guard.check_at
        return guard.offs < GUARD_MAX_OFFS and now - guard.read_at >= GUARD_READ_SECONDS

    def _after_read(self, entity_id: str, plan: _Plan, state: str | None) -> tuple[str, str] | None:
        """Feed one read to the lock, the pending transition and the night guard; the call to make, if any."""
        held = self._held.get(entity_id)
        now, wall = self._clock.monotonic(), self._clock.time()
        mode, moment = self._mode(now, wall)
        if held is not None and mode == BEFORE_CONTACT and held.cloud_only:
            self._lock_read_at[entity_id] = now
            if state is None or state in UNAVAILABLE_STATES:
                self._answering[entity_id] = None
            elif self._answering.get(entity_id) is None:
                self._answering[entity_id] = now
        live = self._live.get(entity_id)
        if held is None or live is None or held.device_id != plan.device_id or moment is None:
            return None
        if plan.pending is not None:
            return self._read_pending(entity_id, held, live, plan.pending, state, mode, moment, wall, now)
        if plan.guard:
            return self._read_guard(entity_id, held, live, state, mode, moment, wall, now)
        return None

    def _read_pending(
        self,
        entity_id: str,
        held: HeldLight,
        live: _Live,
        transition: Transition,
        state: str | None,
        mode: str,
        moment: float,
        wall: float,
        now: float,
    ) -> tuple[str, str] | None:
        pending = live.pending
        if pending is None or pending.transition != transition or not held.schedule.has(transition):
            return None
        want = transition.state
        if pending.awaiting_check:
            pending.awaiting_check = False
            if state == want:
                self._commit(entity_id, held, live, switched=True)
                return None
            if pending.tries >= FAILED_AFTER_TRIES and not pending.failed_sent:
                pending.failed_sent = True
                self._queue(entity_id, "failed", want, transition.at, pending.tries)
                self._log.warning(messages.LIGHT_FAILED.format(verb=VERBS[want], entity=entity_id))
            pending.next_at = pending.called_at + RETRY_DELAYS[min(pending.tries, len(RETRY_DELAYS)) - 1]
            if now < pending.next_at:
                return None
        if state == want:
            # Already in the target state: applied, with no call and no event.
            self._commit(entity_id, held, live, switched=False)
            return None
        if state not in STATES or not self._may_call(entity_id, held, live, transition, mode, moment, wall, now):
            # HA or the plug not answering, the hold, or an on waiting for the lock: read again in 30 s.
            pending.next_at = now + WAIT_READ_SECONDS
            return None
        return want, "apply"

    def _read_guard(
        self,
        entity_id: str,
        held: HeldLight,
        live: _Live,
        state: str | None,
        mode: str,
        moment: float,
        wall: float,
        now: float,
    ) -> tuple[str, str] | None:
        guard = live.guard
        if guard is None:
            return None
        guard.read_at = now
        if guard.check_at is not None:
            guard.check_at = None
            if state == "off":
                self._queue(entity_id, "night_off", "off", guard.off_at, guard.attempts)
                guard.attempts = 0
                guard.on_since = None
                self._log.warning(messages.LIGHT_NIGHT_OFF.format(entity=entity_id))
            else:
                guard.on_since = now if state == "on" else None
            return None
        if state != "on":
            guard.on_since = None
            return None
        if guard.on_since is None:
            guard.on_since = now
        if now - guard.on_since < GUARD_ON_SECONDS or guard.offs >= GUARD_MAX_OFFS:
            return None
        if not self._guard_may_call(entity_id, held, live, mode, moment, wall, now):
            return None
        return "off", "guard"

    def _may_call(
        self,
        entity_id: str,
        held: HeldLight,
        live: _Live,
        transition: Transition,
        mode: str,
        moment: float,
        wall: float,
        now: float,
    ) -> bool:
        """Every try checks the mode and the device again, and an on before the first contact `cloud_only` and the
        lock."""
        if not self._acting(mode, moment, wall) or not self._device_ok(entity_id, held):
            return False
        if transition.state == "on" and mode != ANCHORED:
            since = self._answering.get(entity_id)
            if not held.cloud_only or since is None or now - since < LOCK_SECONDS:
                return False
        return not self._capped(entity_id, live, transition.at, now)

    def _guard_may_call(
        self,
        entity_id: str,
        held: HeldLight,
        live: _Live,
        mode: str,
        moment: float,
        wall: float,
        now: float,
    ) -> bool:
        """The night guard's off checks the mode, the device, its dark period and the cap."""
        guard = live.guard
        if guard is None or not self._acting(mode, moment, wall) or not self._device_ok(entity_id, held):
            return False
        dark = self._dark(held, moment)
        if dark is None or dark.at != guard.off_at:
            return False
        return not self._capped(entity_id, live, guard.off_at, now)

    def _still_planned(self, entity_id: str, plan: _Plan) -> bool:
        """The read of /config before a call runs outside the lock, for up to 10 s, and an answer, a map read, a
        contact or a halt can land in between. Under the lock, right before the call, every check of the try runs
        again on the state of now: the plug keeps the device of its marker, and the transition is still the pending
        one and still in the schedule, or the night guard's dark period still stands. When the plan was replaced or
        dropped meanwhile, nothing goes and the next pass decides afresh."""
        held = self._held.get(entity_id)
        live = self._live.get(entity_id)
        if held is None or live is None or held.device_id != plan.device_id:
            return False
        now, wall = self._clock.monotonic(), self._clock.time()
        mode, moment = self._mode(now, wall)
        if moment is None:
            return False
        if plan.pending is None:
            return plan.guard and self._guard_may_call(entity_id, held, live, mode, moment, wall, now)
        pending = live.pending
        if pending is None or pending.transition != plan.pending or not held.schedule.has(plan.pending):
            return False
        return self._may_call(entity_id, held, live, plan.pending, mode, moment, wall, now)

    def _device_ok(self, entity_id: str, held: HeldLight) -> bool:
        if entity_id in self._pumps or entity_id in self._compressors:
            return False
        return self._device_of is not None and self._device_of.get(entity_id) == held.device_id

    def _capped(self, entity_id: str, live: _Live, at: float, now: float) -> bool:
        """20 calls in a rolling hour stop the calls to the plug until the next transition."""
        live.calls = [moment for moment in live.calls if now - moment < CAP_WINDOW_SECONDS]
        if live.capped_at is not None and live.capped_at != at:
            live.capped_at = None
        if live.capped_at is None and len(live.calls) >= CAP_CALLS:
            live.capped_at = at
            self._log_once("capped", entity_id, messages.LIGHT_CAPPED.format(entity=entity_id))
        return live.capped_at is not None

    def _wait(self, entity_id: str) -> None:
        """HA not RUNNING: no call, and the pending transition reads again in 30 s."""
        live = self._live.get(entity_id)
        if live is not None and live.pending is not None:
            live.pending.next_at = self._clock.monotonic() + WAIT_READ_SECONDS

    def _after_call(self, entity_id: str, plan: _Plan, purpose: str) -> None:
        now = self._clock.monotonic()
        live = self._live.get(entity_id)
        if live is None:
            return
        live.calls.append(now)
        if purpose == "apply":
            pending = live.pending
            if pending is not None and pending.transition == plan.pending:
                pending.tries += 1
                pending.called_at = now
                pending.awaiting_check = True
                pending.next_at = now + CHECK_AFTER_SECONDS
            return
        guard = live.guard
        if guard is None:
            return
        guard.offs += 1
        guard.attempts += 1
        guard.on_since = None
        guard.check_at = now + CHECK_AFTER_SECONDS
        if guard.offs >= GUARD_MAX_OFFS:
            self._log.warning(messages.LIGHT_NIGHT_GUARD_DONE.format(entity=entity_id))

    def _commit(self, entity_id: str, held: HeldLight, live: _Live, switched: bool) -> None:
        """The plug reads the pending transition's state: it is applied, and the file follows."""
        pending = live.pending
        if pending is None:
            return
        held.applied = pending.transition
        live.pending = None
        if not switched:
            self._persist()
            return
        state = pending.transition.state
        self._queue(entity_id, "switched", state, pending.transition.at, pending.tries)
        self._log.info((messages.LIGHT_ON if state == "on" else messages.LIGHT_OFF).format(entity=entity_id))

    def _cadence(self) -> float:
        now = self._clock.monotonic()
        delays = [PASS_SECONDS]
        with self._lock:
            if self._anchor is None and now < self._started + STARTING_SECONDS:
                delays.append(self._started + STARTING_SECONDS - now)
            for live in self._live.values():
                due = (live.pending.next_at if live.pending else None, live.guard.check_at if live.guard else None)
                delays.extend(moment - now for moment in due if moment is not None and moment > now)
        return max(MIN_PASS_SECONDS, min(delays))

    # Home Assistant

    def _read(self, entity_id: str) -> str | None:
        """The plug's state; `unavailable` on a 404; None when HA did not answer with one."""
        try:
            response = self._ha.get(f"/states/{entity_id}")
        except TransportError:
            return None
        if response.status == 404:
            return "unavailable"
        payload = json_body(response) if response.status == 200 else None
        if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
            return None
        return payload["state"]

    def _running(self) -> bool:
        try:
            response = self._ha.get("/config")
        except TransportError:
            return False
        payload = json_body(response) if response.status == 200 else None
        return isinstance(payload, dict) and payload.get("state") == "RUNNING"

    def _call(self, entity_id: str, state: str) -> None:
        if not LIGHT_ENTITY_RE.fullmatch(entity_id):
            raise ValueError("the controller switches only a switch")
        try:
            self._ha.post(f"/services/{LOCAL_LIGHT_SERVICES[state]}", {"entity_id": entity_id})
        except TransportError:
            # Not answered: the read after it finds the plug as it was, and the retry follows.
            return

    # The file, the events and the log

    def _queue(self, entity_id: str, kind: str, state: str, transition_at: float, attempts: int) -> None:
        event = LightEvent(
            str(uuid.uuid4()), entity_id, kind, state, transition_at, min(attempts, MAX_ATTEMPTS), self._clock.time()
        )
        self._events.append(event)
        dropped = len(self._events) - MAX_EVENTS
        if dropped > 0:
            self._events = self._events[dropped:]
            self._log.warning(messages.LIGHT_EVENTS_DROPPED.format(count=dropped))
        self._persist()

    def _persist(self) -> None:
        content = LightFile(entities=dict(self._held), last_time=self._last_time, events=list(self._events))
        try:
            self._store.write(content)
        except OSError as error:
            # The state stays in memory and the next change writes it again; a switch never waits for the disk.
            self._log_error(error)
            return
        self._written_at = self._clock.monotonic()

    def _log_once(self, code: str, entity_id: str, line: str) -> None:
        now = self._clock.monotonic()
        last = self._logged.get((code, entity_id))
        if last is not None and now - last < LOG_INTERVAL_SECONDS:
            return
        self._logged[(code, entity_id)] = now
        self._log.warning(line)

    def _log_skew(self, now: float) -> None:
        if self._skew_logged is not None and now - self._skew_logged < LOG_INTERVAL_SECONDS:
            return
        self._skew_logged = now
        self._log.warning(messages.LIGHT_CLOCK_SKEW)

    def _log_error(self, error: Exception) -> None:
        name = type(error).__name__
        now = self._clock.monotonic()
        with self._lock:
            last = self._error_logged.get(name)
            if last is not None and now - last < ERROR_LOG_INTERVAL_SECONDS:
                return
            self._error_logged[name] = now
        self._log.error(messages.UNEXPECTED_ERROR.format(where=WHERE, error=name))
