"""The backup watering: the connector runs an automatic shot of the pump by itself, only when it missed that shot
because the Cropilot was silent.

The server decides and the connector executes. Each accepted report answer carries, on a pump's allowlist entry, the
`pump_backup` schedule: the automatic slots of the next 24 hours with the dose of each pulse, the pump's HA device (the
pin), the plug's own pulse and `takeover_seconds` (pump_backup_store.py). The connector holds the last valid one in
memory only, replaces it at every accepted answer and drops it on an explicit null, on a missing key, on an answer
without `pump_backup_supported: true` (every schedule goes), when the pump's entry or its `pump` marker is gone, on a
pin that no longer matches, on every halt of the connector (a stop line), on a new pairing and on a 403 from any
Cropilot endpoint.

Silence in server time. Every HTTP answer of the Cropilot reaches `server_answered` through `AnsweredTransport`. Each
2xx is a contact, placed at the answer's `server_time` (the report, the claim and the start answer with one), else at
its `Date` header, else at its arrival on the anchored clock, which can only delay a takeover; the latest one counts.
A `server_time` also anchors the clock: from the first contact of the process, the time is the last `server_time` plus
the monotonic clock. Without a contact in this process nothing runs, and a schedule only arrives with one.

The takeover of slot s, at `slot_at + takeover_seconds` on the anchored clock: only when no contact is placed from
`slot_at - 30 s` on (the server sends pulse 0 only until `slot_at + 120 s`, and a start needs a claim and a start
answered with 2xx, so no server pulse of the slot ran or can still run), the slot was never decided here (run or
skipped, kept in the file for 48 hours), the anchored time and the wall clock are at most 300 s apart, `valid_until`
is ahead, the pin matches the last good device map, the watch lists the pump with the schedule's `max_on_seconds`, the
pump reads `off` with no cycle or shot under way on it, and the plug's pulse, read in Home Assistant right before the
pulse, is the schedule's `plug_pulse.seconds`. Pulse 0 starts at most 120 s after the takeover; pulse k starts 20 s
after pulse k - 1 ended and within 900 s of pulse 0, after the same checks. Only an `ended` pulse leads to the next
one: any other end, a skip or a failure ends the slot, and a stop ends it after the pulse under way, with no event for
the pulses it never started.

Every pulse goes through the pump watch (pump.py): `backup_pulse_starting` before the HA call, so the watch cuts it at
its dose, forces the off at `max_on` and cuts it at a restart, as a Cropilot shot; its end comes back through the sink,
never as a `pump_events` item. The only HA service is `switch.turn_on` (`LOCAL_PUMP_SERVICES`, actions.py), body exactly
`{entity_id}`; never a flush, a test or a valve.

The events (`pump_backup_events`) leave the queue only when an answer's `pump_backup_accepted` lists them; one that 60
accepted answers carried without acknowledging it, or older than 30 days, is dropped with one line. Like the watches,
it runs in its own daemon thread with its own HA client and never calls the Cropilot.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import uuid
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from . import messages
from .actions import LOCAL_PUMP_SERVICES
from .clients import HomeAssistantClient, json_body
from .context import Clock
from .light_store import parse_instant, utc_iso
from .pump_backup_store import (
    BACKUP_PUMP_RE,
    DECIDED_KEEP_SECONDS,
    DOSE_BELOW_MAX_ON_SECONDS,
    MAX_ATTEMPTS,
    MAX_DECIDED,
    MAX_EVENT_AGE_SECONDS,
    MAX_EVENTS,
    MAX_FILE_BYTES,
    MAX_PUMPS,
    WATCH_KINDS,
    BackupEvent,
    BackupFile,
    BackupSchedule,
    BackupStore,
    InFlight,
    Slot,
    encode,
    parse_schedule,
)
from .pump_store import BackupKey
from .transport import CONNECT, Response, Transport, TransportError

if TYPE_CHECKING:
    from .pump import PumpWatch

CONTACT_MARGIN_SECONDS = 30.0
START_GRACE_SECONDS = 120.0
PULSE_GAP_SECONDS = 20.0
SLOT_WINDOW_SECONDS = 900.0
SKEW_SECONDS = 300.0
# The watch's attach window after the HA call (30 s) and a margin: past it, a pulse with no cycle never ran.
ATTACH_WAIT_SECONDS = 35.0
PASS_SECONDS = 5.0
HOT_PASS_SECONDS = 1.0
REPORT_KEYS_MAX_BYTES = 16384
MAX_EVENTS_PER_REPORT = 40
UNACKNOWLEDGED_AFTER_ANSWERS = 60
# The server's unit table of a plug's pulse (HomeAssistantEntityRules::DURATION_UNITS).
DURATION_UNITS = {"s": 1, "min": 60, "h": 3600}
NUMERIC_STATE_RE = re.compile(r"^\s*[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\s*$")
WHOLE_SECOND_TOLERANCE = 1e-6
LOG_INTERVAL_SECONDS = 3600.0
HALT_POLL_SECONDS = 1.0
ERROR_LOG_INTERVAL_SECONDS = 60.0
WHERE = "rega de reserva"

STARTING = "starting"
ON = "on"
GAP = "gap"
GO = "go"


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def plug_pulse_seconds(state: Any) -> int | None:
    """A plug's pulse `number` in whole seconds, by the server's unit table; None for anything else: another unit, a
    state that is not a plain number, a value off the whole second, or no state at all."""
    if not isinstance(state, dict):
        return None
    attributes = state.get("attributes")
    unit = attributes.get("unit_of_measurement") if isinstance(attributes, dict) else None
    factor = DURATION_UNITS.get(unit) if isinstance(unit, str) else None
    value = state.get("state")
    if factor is None or not isinstance(value, str) or NUMERIC_STATE_RE.fullmatch(value) is None:
        return None
    seconds = float(value) * factor
    if not math.isfinite(seconds) or abs(seconds - round(seconds)) > WHOLE_SECOND_TOLERANCE:
        return None
    return int(round(seconds))


def _answer_server_time(response: Response) -> float | None:
    payload = json_body(response)
    data = payload.get("data") if isinstance(payload, dict) else None
    return parse_instant(data.get("server_time")) if isinstance(data, dict) else None


def _date_header(response: Response) -> float | None:
    value = response.header("date")
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if moment is None or moment.tzinfo is None:
        return None
    instant = moment.timestamp()
    return instant if math.isfinite(instant) else None


@dataclass
class _Active:
    """The slot in progress on one pump, from pulse 0's start to the slot's end, the gaps included."""

    slot: Slot
    fingerprint: str
    pulse_index: int
    phase: str
    window_until: float
    pulse_started_at: float
    next_at: float = 0.0
    attach_until: float | None = None
    stopping: bool = False
    # The start of the pulse before the current one, for a pulse the watch refuses before its HA call.
    previous_started_at: float = 0.0

    @property
    def dose(self) -> int:
        return self.slot.pulses[self.pulse_index]


@dataclass(frozen=True)
class _Finished:
    """A slot that ended: its last pulse, shown as `running` until the report that carries its last event."""

    slot: Slot
    pulse_index: int
    pulse_started_at: float
    last_event_id: str


@dataclass(frozen=True)
class _Plan:
    """What one visit does for a pump: start pulse k of a slot, or check that a pulse called in HA reached it."""

    entity_id: str
    slot: Slot
    pulse_index: int
    attach_check: bool = False


def _running(slot: Slot, pulse_index: int, pulse_on: bool, started_at: float, now: float) -> dict:
    return {
        "light_date": slot.light_date,
        "slot_index": slot.slot_index,
        "slot_at": utc_iso(slot.slot_at),
        "pulse_index": pulse_index,
        "pulse_on": pulse_on,
        "dose_seconds": slot.pulses[pulse_index],
        "started_age_seconds": min(MAX_EVENT_AGE_SECONDS, max(0, int(now - started_at))),
    }


class PumpBackup:
    def __init__(
        self,
        *,
        data_dir: Path,
        transport: Transport,
        supervisor_token: str,
        clock: Clock,
        log: logging.Logger,
        watch: PumpWatch | None,
    ) -> None:
        self._store = BackupStore(data_dir)
        # Its own event, never set: the connector's halt is a trigger of the backup, never its end.
        self._ha = HomeAssistantClient(transport, supervisor_token, threading.Event())
        self._clock = clock
        self._log = log
        self._watch = watch
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._retired = threading.Event()
        self._thread: threading.Thread | None = None
        self._halt_thread: threading.Thread | None = None
        self._held: dict[str, BackupSchedule] = {}
        self._device_of: dict[str, str | None] | None = None
        # (server_time, monotonic moment it arrived) of the last answer with one.
        self._anchor: tuple[float, float] | None = None
        # The latest contact placed in server time, and a later one that came before any anchor (monotonic).
        self._contact: float | None = None
        self._unplaced_at: float | None = None
        self._active: dict[str, _Active] = {}
        self._finished: dict[str, _Finished] = {}
        # The accepted answers that carried each queued event without acknowledging it, in this process.
        self._carried: dict[str, int] = {}
        self._logged: dict[tuple[str, str], float] = {}
        self._error_logged: dict[str, float] = {}
        content = self._store.read()
        if content is None:
            self._store.set_aside()
            log.warning(messages.BACKUP_FILE_UNREADABLE)
            content = BackupFile()
        self._decided = content.decided
        self._flights = content.in_flight
        self._events = content.events
        # A pulse in flight at a restart: the watch cuts it (`boot_off`) and its end comes through the sink. With no
        # such cycle in the watch nothing ever would, so it goes, and its slot stays decided.
        changed = False
        for entity_id, flight in sorted(self._flights.items()):
            if watch is None or not watch.backup_pending(entity_id, flight.key):
                del self._flights[entity_id]
                changed = True
                log.warning(messages.BACKUP_PULSE_LOST.format(entity=entity_id))
        if self._prune(clock.time()) or changed:
            self._persist()
        if watch is not None:
            watch.attach_backup(self._watch_event, self.slot_open)

    # The threads

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="rega-reserva", daemon=True)
        self._thread.start()

    def follow_halt(self, halt: threading.Event) -> None:
        """Drop every schedule the moment the connector halts on a stop line or an idle line of the start."""

        def follow() -> None:
            while not self._retired.is_set():
                if halt.wait(HALT_POLL_SECONDS):
                    self.stop()
                    return

        self._halt_thread = threading.Thread(target=follow, name="rega-reserva-parada", daemon=True)
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
            if delay is None:
                # Nothing held and nothing under way: only an answer, the watch or a stop wakes it.
                self._wake.wait()
            else:
                self._clock.wait(self._wake, delay)

    def step(self) -> float | None:
        """One pass; the seconds until the next one, or None when only a wake can bring work."""
        try:
            self._pass()
        except Exception as error:  # noqa: BLE001 - the backup must survive any defect of one pass
            self._log_error(error)
        return self._cadence()

    # Calls from the transport, the report loop and the start step, never holding the watch's lock

    def server_answered(self, response: Response) -> None:
        """Every HTTP answer of the Cropilot: a 2xx is a contact placed in server time, a 403 drops every schedule."""
        try:
            if response.status == 403:
                self.forbidden()
                return
            if not 200 <= response.status < 300:
                return
            arrival = self._clock.monotonic()
            server_time = _answer_server_time(response)
            dated = _date_header(response) if server_time is None else None
            with self._lock:
                if server_time is not None:
                    self._anchor = (server_time, arrival)
                    self._place(server_time)
                elif dated is not None:
                    self._place(dated)
                elif self._anchor is not None:
                    self._place(self._anchored(arrival))
                else:
                    self._unplaced_at = arrival
        except Exception as error:  # noqa: BLE001 - a defect here must never break a Cropilot call
            self._log_error(error)

    def replace(
        self,
        entries: Iterable[Any],
        supported: bool,
        server_time: Any,
        compressors: Iterable[str],
        device_of: Mapping[str, str | None] | None,
    ) -> None:
        """An accepted report answer: its pump entries, its `pump_backup_supported` and `server_time`, the switches
        that are compressor plugs and the HA device of each entity in the last good map read."""
        received = self._clock.monotonic()
        moment = parse_instant(server_time)
        pumps = {
            entry["entity_id"]: entry
            for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("entity_id"), str)
            and isinstance(entry.get("pump"), dict)
        }
        watch = self._watch
        listed = {entity_id: watch.listed_max_on(entity_id) if watch is not None else None for entity_id in pumps}
        refused = frozenset(compressors)
        with self._lock:
            if moment is not None:
                # The answer is itself a contact at its server_time: the backup never relies on the transport alone.
                if self._anchor is None:
                    self._anchor = (moment, received)
                self._place(moment)
            if device_of is not None:
                self._device_of = dict(device_of)
            if supported is not True:
                # Fail closed: every answer of a server that sends schedules says so, and an answer without the flag
                # (an older server) keeps none, nor takes a `pump_backup` it may carry.
                pumps = {}
            for entity_id in sorted(self._held):
                if entity_id not in pumps:
                    # The pump's entry or its `pump` marker is gone, or the answer lacks the flag.
                    self._drop(entity_id)
            for entity_id, entry in sorted(pumps.items()):
                if "pump_backup" not in entry:
                    self._drop(entity_id)
                    continue
                raw = entry["pump_backup"]
                if raw is None:
                    self._drop(entity_id)
                    continue
                max_on = entry["pump"].get("max_on_seconds")
                reason = self._refusal(entity_id, max_on, listed.get(entity_id), refused)
                schedule = parse_schedule(raw, max_on, moment) if reason is None else None
                if reason is None and schedule is None:
                    reason = "invalid"
                if reason is None and schedule is not None and self._pin(entity_id) != schedule.device_id:
                    reason = "device_unknown" if self._pin(entity_id) is None else "device_mismatch"
                if reason is not None or schedule is None:
                    self._log_once(
                        reason or "invalid",
                        entity_id,
                        messages.BACKUP_SCHEDULE_REFUSED.format(entity=entity_id, reason=reason or "invalid"),
                    )
                    self._drop(entity_id)
                    continue
                self._held[entity_id] = schedule
        self._wake.set()

    def devices(self, device_of: Mapping[str, str | None]) -> None:
        """A good map read: a schedule whose pump the map no longer gives to its pinned device goes."""
        with self._lock:
            self._device_of = dict(device_of)
            for entity_id in sorted(self._held):
                if self._pin(entity_id) != self._held[entity_id].device_id:
                    self._log_once(
                        "device_changed",
                        entity_id,
                        messages.BACKUP_SCHEDULE_REFUSED.format(entity=entity_id, reason="device_changed"),
                    )
                    self._drop(entity_id)
        self._wake.set()

    def drop_all(self) -> None:
        """An answer the connector cannot read keeps no schedule: fail closed."""
        with self._lock:
            for entity_id in sorted(self._held):
                self._drop(entity_id)
        self._wake.set()

    def paired(self) -> None:
        """A new pairing: every schedule goes before the first claim; its first answer brings them back."""
        self.drop_all()

    def forbidden(self) -> None:
        """A 403: the account lost access, and the pump fails closed (the light keeps its schedule on one)."""
        self.stop()

    def stop(self) -> None:
        """A halt: every schedule goes, and a slot in progress ends after its current pulse."""
        with self._lock:
            held = bool(self._held)
            for entity_id in sorted(self._held):
                self._drop(entity_id)
        if held:
            self._log.warning(messages.BACKUP_STOPPED)
        self._wake.set()

    def slot_open(self, entity_id: str) -> bool:
        """A slot of the backup is open on the pump, from pulse 0's start to the slot's end, gaps included, or a pulse
        of it is still in flight after a restart."""
        with self._lock:
            return entity_id in self._active or entity_id in self._flights

    def schedules(self) -> list[tuple[str, float]]:
        with self._lock:
            return [(entity_id, self._held[entity_id].valid_until) for entity_id in sorted(self._held)]

    def events_waiting(self) -> bool:
        """An event no accepted answer has carried yet; one already carried waits for the normal cadence."""
        with self._lock:
            return any(event.event_id not in self._carried for event in self._events)

    def snapshot_events(self) -> list[BackupEvent]:
        with self._lock:
            return list(self._events)

    def report_keys(
        self, candidates: Sequence[BackupEvent], now: float, max_bytes: int = REPORT_KEYS_MAX_BYTES
    ) -> tuple[list[dict], list[dict], list[BackupEvent]]:
        """`pump_backup`, whole, with each pump's `running` as it stands now, and the oldest of the events taken before
        /states that fit beside it in `max_bytes` encoded, at most 40; returns both lists and the events sent. A
        finished slot stays as `running` until the report that carries its last event."""
        with self._lock:
            queued = {event.event_id for event in self._events}
            items: list[dict] = []
            finishing: dict[str, str] = {}
            for entity_id in sorted(self._held):
                running = None
                active = self._active.get(entity_id)
                finished = self._finished.get(entity_id)
                if active is not None:
                    running = _running(active.slot, active.pulse_index, active.phase != GAP, active.pulse_started_at, now)
                elif finished is not None and finished.last_event_id in queued:
                    running = _running(finished.slot, finished.pulse_index, False, finished.pulse_started_at, now)
                    finishing[entity_id] = finished.last_event_id
                items.append(
                    {"entity_id": entity_id, "valid_until": utc_iso(self._held[entity_id].valid_until), "running": running}
                )
            for entity_id in [key for key, item in self._finished.items() if item.last_event_id not in queued]:
                del self._finished[entity_id]
            events = [event for event in candidates if event.event_id in queued][:MAX_EVENTS_PER_REPORT]
        wires: list[dict] = []
        sent: list[BackupEvent] = []
        for event in events:
            wire = event.wire(now)
            if len(_encode({"pump_backup": items, "pump_backup_events": [*wires, wire]})) > max_bytes:
                break
            wires.append(wire)
            sent.append(event)
        carried = {event.event_id for event in sent}
        for item in items:
            if finishing.get(item["entity_id"]) in carried:
                item["running"] = None
        return items, wires, sent

    def acknowledge(self, accepted: Any, carried: Iterable[str]) -> None:
        """An accepted report answer: drop only the events `pump_backup_accepted` lists, and those 60 answers in a row
        carried without acknowledging them."""
        ids = {item for item in accepted if isinstance(item, str)} if isinstance(accepted, list) else set()
        with self._lock:
            held = {event.event_id for event in self._events}
            for event_id in carried:
                if event_id in held and event_id not in ids:
                    self._carried[event_id] = self._carried.get(event_id, 0) + 1
            stale = {event_id for event_id, count in self._carried.items() if count >= UNACKNOWLEDGED_AFTER_ANSWERS}
            kept = [event for event in self._events if event.event_id not in ids and event.event_id not in stale]
            if stale & held:
                self._log.warning(messages.BACKUP_EVENTS_UNACKNOWLEDGED.format(count=len(stale & held)))
            if len(kept) != len(self._events):
                self._events = kept
                self._persist()
            remaining = {event.event_id for event in self._events}
            self._carried = {event_id: count for event_id, count in self._carried.items() if event_id in remaining}

    # The watch's sink, called under the watch's lock

    def _watch_event(
        self, entity_id: str, key: BackupKey, kind: str, on_seconds: int | None, ended_at: float, attempts: int
    ) -> None:
        with self._lock:
            flight = self._flights.get(entity_id)
            if flight is None or flight.key != key:
                # A repeat of an end already taken, after a crash between the two files.
                return
            kind = kind if kind in WATCH_KINDS else "forced_off"
            if kind == "off_failed":
                event = self._queue(entity_id, flight, "off_failed", None, None, ended_at, attempts)
                self._persist()
                return
            event = self._queue(entity_id, flight, kind, None, on_seconds, ended_at, attempts)
            del self._flights[entity_id]
            active = self._active.get(entity_id)
            if active is not None and active.slot.key(active.pulse_index) == key:
                active.attach_until = None
                more = active.pulse_index + 1 < len(active.slot.pulses)
                if kind == "ended" and more and not active.stopping:
                    active.phase = GAP
                    active.next_at = self._clock.monotonic() + PULSE_GAP_SECONDS
                else:
                    self._end(entity_id, active, event.event_id)
            self._persist()
        self._wake.set()

    # One pass

    def _pass(self) -> None:
        with self._lock:
            mono, wall = self._clock.monotonic(), self._clock.time()
            now = self._server_now(mono)
            self._expire(now, wall)
            if self._prune(wall):
                self._persist()
            plans = self._plans(now, mono)
        for plan in plans:
            try:
                self._visit(plan)
            except Exception as error:  # noqa: BLE001 - one pump's defect must not skip the others
                self._log_error(error)

    def _plans(self, now: float | None, mono: float) -> list[_Plan]:
        plans: list[_Plan] = []
        for entity_id in sorted(set(self._held) | set(self._active)):
            active = self._active.get(entity_id)
            if active is not None:
                if active.phase == GAP and (active.stopping or entity_id not in self._held):
                    self._end(entity_id, active, None)
                    self._persist()
                elif active.phase == GAP and mono >= active.next_at:
                    plans.append(_Plan(entity_id, active.slot, active.pulse_index + 1))
                elif active.phase == ON and active.attach_until is not None and mono >= active.attach_until:
                    plans.append(_Plan(entity_id, active.slot, active.pulse_index, attach_check=True))
                continue
            schedule = self._held.get(entity_id)
            if schedule is None or now is None:
                continue
            contact = self._last_contact()
            for slot in schedule.slots:
                takeover = slot.slot_at + schedule.takeover_seconds
                if now < takeover:
                    break
                if self._is_decided(entity_id, slot) or self._owned(contact, slot):
                    continue
                if now > takeover + START_GRACE_SECONDS:
                    self._skip(entity_id, slot, schedule.fingerprint(slot.light_date), 0, "late", None)
                    continue
                plans.append(_Plan(entity_id, slot, 0))
                break
        return plans

    def _visit(self, plan: _Plan) -> None:
        """One pump: at most two reads and one call, each outside the lock."""
        watch = self._watch
        if watch is None:
            return
        key = plan.slot.key(plan.pulse_index)
        if plan.attach_check:
            pending = watch.backup_pending(plan.entity_id, key)
            with self._lock:
                self._after_attach_wait(plan, pending)
            return
        booted = watch.booted.is_set()
        busy = watch.busy(plan.entity_id)
        listed = watch.listed_max_on(plan.entity_id)
        with self._lock:
            schedule = self._held.get(plan.entity_id)
            plug_entity = schedule.plug_entity_id if schedule is not None else None
        state = self._read_state(plan.entity_id)
        plug = self._read_plug(plug_entity) if state == "off" and not busy and plug_entity is not None else None
        with self._lock:
            go = self._decide(plan, booted, busy, listed, state, plug)
            schedule = self._held.get(plan.entity_id)
        if go is None or schedule is None:
            return
        dose = plan.slot.pulses[plan.pulse_index]
        if not watch.backup_pulse_starting(plan.entity_id, key, schedule.max_on_seconds, dose):
            with self._lock:
                self._skip_started(plan, "pump_busy")
            return
        outcome = self._turn_on(plan.entity_id)
        if outcome in ("ok", "ha_error"):
            # An HA error may still have turned the pump on: the watch keeps the attach window, and the pulse fails
            # only when no cycle opened in it.
            watch.pulse_succeeded(plan.entity_id, None, key)
        else:
            watch.pulse_failed(plan.entity_id, None, key)
        with self._lock:
            self._after_call(plan, outcome)

    # The rule, under the lock

    def _decide(
        self, plan: _Plan, booted: bool, busy: bool, listed: int | None, state: str | None, plug: int | None
    ) -> str | None:
        """Every check of the pulse again, on the state of now; GO marks the pulse started in the file before the HA
        call. A skip records its event, a stop ends the slot, and a hold reads again at the next pass."""
        entity_id, slot, index = plan.entity_id, plan.slot, plan.pulse_index
        mono, wall = self._clock.monotonic(), self._clock.time()
        now = self._server_now(mono)
        schedule = self._held.get(entity_id)
        active = self._active.get(entity_id)
        if index == 0:
            if active is not None or schedule is None or slot not in schedule.slots or self._is_decided(entity_id, slot):
                return None
            if self._owned(self._last_contact(), slot) or now is None:
                return None
            takeover = slot.slot_at + schedule.takeover_seconds
            if now < takeover:
                return None
            fingerprint = schedule.fingerprint(slot.light_date)
            if now > takeover + START_GRACE_SECONDS:
                self._skip(entity_id, slot, fingerprint, 0, "late", None)
                return None
        else:
            if active is None or active.slot != slot or active.phase != GAP or active.pulse_index != index - 1:
                return None
            if active.stopping or schedule is None:
                self._end(entity_id, active, None)
                self._persist()
                return None
            fingerprint = active.fingerprint
            if mono > active.window_until or now is None:
                self._skip(entity_id, slot, fingerprint, index, "late", active)
                return None
        if now >= schedule.valid_until or wall >= schedule.valid_until:
            self._expire(now, wall)
            return None
        if abs(now - wall) > SKEW_SECONDS:
            self._skip(entity_id, slot, fingerprint, index, "clock_skew", active)
            return None
        refusal = "device_changed" if self._pin(entity_id) != schedule.device_id else None
        if refusal is None and listed != schedule.max_on_seconds:
            refusal = "not_watched"
        if refusal is not None:
            self._log_once(refusal, entity_id, messages.BACKUP_SCHEDULE_REFUSED.format(entity=entity_id, reason=refusal))
            self._drop(entity_id)
            return None
        if not booted or state is None:
            # The watch has not read the pumps yet, or HA did not answer: read again until the grace or the window ends.
            return None
        if state != "off" or busy:
            self._skip(entity_id, slot, fingerprint, index, "pump_busy", active)
            return None
        dose = slot.pulses[index]
        if plug != schedule.plug_seconds or dose > schedule.max_on_seconds - DOSE_BELOW_MAX_ON_SECONDS:
            self._skip(entity_id, slot, fingerprint, index, "pulse_mismatch", active)
            return None
        if index == 0:
            active = _Active(slot, fingerprint, 0, STARTING, mono + SLOT_WINDOW_SECONDS, wall)
            self._active[entity_id] = active
            self._mark_decided(entity_id, slot)
            self._finished.pop(entity_id, None)
            self._log.info(messages.BACKUP_SLOT_STARTED.format(slot=slot.slot_index, date=slot.light_date, entity=entity_id))
        else:
            active.previous_started_at = active.pulse_started_at
            active.pulse_index = index
            active.phase = STARTING
            active.pulse_started_at = wall
        self._flights[entity_id] = InFlight(
            light_date=slot.light_date,
            slot_index=slot.slot_index,
            pulse_index=index,
            slot_at=slot.slot_at,
            pulse_count=len(slot.pulses),
            fingerprint=fingerprint,
            dose_seconds=dose,
            started_at=wall,
        )
        self._persist()
        return GO

    def _skip_started(self, plan: _Plan, reason: str) -> None:
        """The watch refused a pulse already marked started: it never reached HA."""
        active = self._active.get(plan.entity_id)
        flight = self._flights.pop(plan.entity_id, None)
        if active is None or flight is None or flight.key != plan.slot.key(plan.pulse_index):
            self._persist()
            return
        event = self._queue(plan.entity_id, flight, "skipped", reason, None, self._clock.time(), 0)
        self._log.warning(
            messages.BACKUP_SLOT_SKIPPED.format(
                slot=plan.slot.slot_index, date=plan.slot.light_date, entity=plan.entity_id, reason=reason
            )
        )
        if plan.pulse_index > 0:
            # Back to the pulse that ended last, which `running` shows until this event leaves.
            active.pulse_index = plan.pulse_index - 1
            active.pulse_started_at = active.previous_started_at
        self._end(plan.entity_id, active, event.event_id if plan.pulse_index > 0 else None, quiet=True)
        self._persist()

    def _after_call(self, plan: _Plan, outcome: str) -> None:
        entity_id = plan.entity_id
        active = self._active.get(entity_id)
        flight = self._flights.get(entity_id)
        if active is None or flight is None or flight.key != plan.slot.key(plan.pulse_index):
            # The watch already ended the pulse.
            return
        if outcome in ("ok", "ha_error"):
            active.phase = ON
            active.attach_until = self._clock.monotonic() + ATTACH_WAIT_SECONDS
            return
        self._fail(entity_id, active, flight, outcome)

    def _after_attach_wait(self, plan: _Plan, pending: bool) -> None:
        entity_id = plan.entity_id
        active = self._active.get(entity_id)
        flight = self._flights.get(entity_id)
        if active is None or flight is None or flight.key != plan.slot.key(plan.pulse_index):
            return
        if pending:
            # A cycle opened: the watch ends it, at the dose or at max_on.
            active.attach_until = None
            return
        # HA took the call, but the pump never read on in the attach window: the pulse did not run.
        self._fail(entity_id, active, flight, "ha_error")

    def _fail(self, entity_id: str, active: _Active, flight: InFlight, code: str) -> None:
        del self._flights[entity_id]
        event = self._queue(entity_id, flight, "failed", code, None, self._clock.time(), 0)
        self._log.warning(
            messages.BACKUP_SLOT_SKIPPED.format(
                slot=active.slot.slot_index, date=active.slot.light_date, entity=entity_id, reason=code
            )
        )
        self._end(entity_id, active, event.event_id, quiet=True)
        self._persist()

    def _skip(
        self, entity_id: str, slot: Slot, fingerprint: str, index: int, reason: str, active: _Active | None
    ) -> None:
        """Pulse `index` never starts: its `skipped` event ends the slot."""
        flight = InFlight(
            light_date=slot.light_date,
            slot_index=slot.slot_index,
            pulse_index=index,
            slot_at=slot.slot_at,
            pulse_count=len(slot.pulses),
            fingerprint=fingerprint,
            dose_seconds=slot.pulses[index],
            started_at=self._clock.time(),
        )
        event = self._queue(entity_id, flight, "skipped", reason, None, self._clock.time(), 0)
        self._mark_decided(entity_id, slot)
        self._log.warning(
            messages.BACKUP_SLOT_SKIPPED.format(slot=slot.slot_index, date=slot.light_date, entity=entity_id, reason=reason)
        )
        if active is not None:
            self._end(entity_id, active, event.event_id, quiet=True)
        self._persist()

    def _end(self, entity_id: str, active: _Active, last_event_id: str | None, quiet: bool = False) -> None:
        """The slot is over: its last pulse stays as `running` until the report that carries its last event."""
        self._active.pop(entity_id, None)
        if last_event_id is not None:
            self._finished[entity_id] = _Finished(active.slot, active.pulse_index, active.pulse_started_at, last_event_id)
        if quiet:
            return
        line = messages.BACKUP_SLOT_STOPPED if active.stopping else messages.BACKUP_SLOT_ENDED
        self._log.info(line.format(slot=active.slot.slot_index, date=active.slot.light_date, entity=entity_id))

    def _drop(self, entity_id: str) -> None:
        """The schedule goes; a slot in progress starts no further pulse (the current one ends by the watch)."""
        self._held.pop(entity_id, None)
        active = self._active.get(entity_id)
        if active is None:
            return
        active.stopping = True
        if active.phase == GAP:
            self._end(entity_id, active, None)
            self._persist()

    def _refusal(self, entity_id: str, max_on: Any, listed: int | None, compressors: frozenset[str]) -> str | None:
        if not BACKUP_PUMP_RE.fullmatch(entity_id):
            return "not_a_plug"
        if entity_id in compressors:
            return "compressor_plug"
        if max_on is None:
            return "no_max_on"
        if listed is None or listed != max_on:
            return "not_watched"
        if entity_id not in self._held and len(self._held) >= MAX_PUMPS:
            return "too_many"
        return None

    def _expire(self, now: float | None, wall: float) -> None:
        """Past `valid_until` by the anchored time or by the wall clock, whichever comes first, a schedule goes."""
        for entity_id in sorted(self._held):
            until = self._held[entity_id].valid_until
            if wall < until and (now is None or now < until):
                continue
            self._drop(entity_id)
            self._log.warning(messages.BACKUP_EXPIRED.format(entity=entity_id))

    # Time and contact

    def _anchored(self, mono: float) -> float:
        server_time, received = self._anchor  # type: ignore[misc]
        return server_time + (mono - received)

    def _server_now(self, mono: float) -> float | None:
        return self._anchored(mono) if self._anchor is not None else None

    def _place(self, moment: float) -> None:
        self._contact = moment if self._contact is None else max(self._contact, moment)

    def _last_contact(self) -> float | None:
        contact = self._contact
        if self._unplaced_at is not None and self._anchor is not None:
            arrival = self._anchored(self._unplaced_at)
            contact = arrival if contact is None else max(contact, arrival)
        return contact

    @staticmethod
    def _owned(contact: float | None, slot: Slot) -> bool:
        """A 2xx placed from 30 s before the slot on: the server owns the slot, and the backup never runs it."""
        return contact is not None and contact >= slot.slot_at - CONTACT_MARGIN_SECONDS

    def _pin(self, entity_id: str) -> str | None:
        return self._device_of.get(entity_id) if self._device_of is not None else None

    # The decided slots, the events and the file

    def _is_decided(self, entity_id: str, slot: Slot) -> bool:
        return (slot.light_date, slot.slot_index) in self._decided.get(entity_id, {})

    def _mark_decided(self, entity_id: str, slot: Slot) -> None:
        self._decided.setdefault(entity_id, {})[(slot.light_date, slot.slot_index)] = slot.slot_at

    def _prune(self, wall: float) -> bool:
        """The decided slots of the last 48 hours, at most 2048, and the events of the last 30 days."""
        changed = False
        for entity_id in list(self._decided):
            slots = {key: at for key, at in self._decided[entity_id].items() if at >= wall - DECIDED_KEEP_SECONDS}
            if len(slots) != len(self._decided[entity_id]):
                changed = True
            if slots:
                self._decided[entity_id] = slots
            else:
                del self._decided[entity_id]
        every = sorted((at, entity_id, key) for entity_id, slots in self._decided.items() for key, at in slots.items())
        for at, entity_id, key in every[: max(0, len(every) - MAX_DECIDED)]:
            del self._decided[entity_id][key]
            changed = True
        self._decided = {entity_id: slots for entity_id, slots in self._decided.items() if slots}
        kept = [event for event in self._events if wall - event.ended_at <= MAX_EVENT_AGE_SECONDS]
        if len(kept) != len(self._events):
            self._log.warning(messages.BACKUP_EVENTS_DROPPED.format(count=len(self._events) - len(kept)))
            self._events = kept
            changed = True
        return changed

    def _queue(
        self,
        entity_id: str,
        flight: InFlight,
        kind: str,
        reason: str | None,
        on_seconds: int | None,
        ended_at: float,
        attempts: int,
    ) -> BackupEvent:
        event = BackupEvent(
            event_id=str(uuid.uuid4()),
            entity_id=entity_id,
            light_date=flight.light_date,
            slot_index=flight.slot_index,
            slot_at=flight.slot_at,
            pulse_index=flight.pulse_index,
            pulse_count=flight.pulse_count,
            fingerprint=flight.fingerprint,
            kind=kind,
            reason=reason,
            dose_seconds=flight.dose_seconds,
            on_seconds=None if on_seconds is None else max(0, int(on_seconds)),
            ended_at=ended_at,
            attempts=min(max(0, attempts), MAX_ATTEMPTS),
        )
        self._events.append(event)
        dropped = len(self._events) - MAX_EVENTS
        if dropped > 0:
            self._events = self._events[dropped:]
            self._log.warning(messages.BACKUP_EVENTS_DROPPED.format(count=dropped))
        return event

    def _persist(self) -> None:
        content = BackupFile(decided=self._decided, in_flight=dict(self._flights), events=list(self._events))
        data = encode(content)
        dropped = 0
        while len(data) > MAX_FILE_BYTES and content.events:
            # Only an extreme backlog gets here: the oldest events go first, never a decided slot or a pulse in flight.
            content.events = content.events[1:]
            dropped += 1
            data = encode(content)
        if dropped:
            self._events = list(content.events)
            self._log.warning(messages.BACKUP_EVENTS_DROPPED.format(count=dropped))
        try:
            self._store.write(data)
        except OSError as error:
            # The state stays in memory and the next change writes it again; a pulse never waits for the disk.
            self._log_error(error)

    # Home Assistant

    def _read_state(self, entity_id: str) -> str | None:
        """The pump's state; `unavailable` on a 404; None when HA did not answer with one."""
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

    def _read_plug(self, entity_id: str) -> int | None:
        """The plug's pulse in whole seconds, read right before a pulse; None on anything else, no answer included."""
        try:
            response = self._ha.get(f"/states/{entity_id}")
        except TransportError:
            return None
        return plug_pulse_seconds(json_body(response)) if response.status == 200 else None

    def _turn_on(self, entity_id: str) -> str:
        if not BACKUP_PUMP_RE.fullmatch(entity_id):
            raise ValueError("the backup watering turns on only a pump plug")
        try:
            response = self._ha.post(f"/services/{LOCAL_PUMP_SERVICES['switch']}", {"entity_id": entity_id})
        except TransportError as error:
            return "ha_unreachable" if error.phase == CONNECT else "ha_error"
        if response.status == 200:
            return "ok"
        if response.status in (400, 404):
            return "ha_rejected"
        return "ha_error"

    # The cadence and the log

    def _cadence(self) -> float | None:
        with self._lock:
            if self._active:
                return HOT_PASS_SECONDS
            if not self._held:
                return None
            now = self._server_now(self._clock.monotonic())
            if now is None:
                return PASS_SECONDS
            delays = [PASS_SECONDS]
            for entity_id, schedule in self._held.items():
                for slot in schedule.slots:
                    if self._is_decided(entity_id, slot):
                        continue
                    delays.append(slot.slot_at + schedule.takeover_seconds - now)
                    break
            return max(HOT_PASS_SECONDS, min(delays))

    def _log_once(self, code: str, entity_id: str, line: str) -> None:
        now = self._clock.monotonic()
        last = self._logged.get((code, entity_id))
        if last is not None and now - last < LOG_INTERVAL_SECONDS:
            return
        self._logged[(code, entity_id)] = now
        self._log.warning(line)

    def _log_error(self, error: Exception) -> None:
        name = type(error).__name__
        now = self._clock.monotonic()
        with self._lock:
            last = self._error_logged.get(name)
            if last is not None and now - last < ERROR_LOG_INTERVAL_SECONDS:
                return
            self._error_logged[name] = now
        self._log.error(messages.UNEXPECTED_ERROR.format(where=WHERE, error=name))
