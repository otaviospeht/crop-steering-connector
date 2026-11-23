"""The climate reserve (0.2.17): with the Cropilot silent, the connector moves the air conditioner's and the in-loop
dehumidifier's setpoints at the light transitions the Cropilot missed, to the values the Cropilot sent for them.

The server stays the only brain and the connector executes. Each accepted report answer of a server with
`climate_backup_supported: true` carries, on the entry of each eligible air conditioner and in-loop dehumidifier, the
`climate_backup` marker (climate_backup_store.py): the light transitions of the next 24 hours with each period's target,
the device's HA device (the pin), `takeover_seconds` and, on an air conditioner, `mode_exit`. The connector holds the
last valid marker in memory only, replaces it at every accepted answer and drops it on an explicit null, on a missing
key, on a missing entry, on an answer without the flag (every schedule goes), on a pin that no longer matches the last
good map read, on every halt of the connector (a stop line), on a new pairing, on a 403 from any Cropilot endpoint and
at its `valid_until`, with an `expired` event when that ends a takeover.

Silence in server time, as the backup watering reads it (pump_backup.py): every 2xx of the Cropilot is a contact,
placed at its `server_time`, else its `Date` header, else its arrival on the anchored clock, and any 2xx ends a takeover
at once. A device's takeover starts at the last contact plus its `takeover_seconds` (the server sends 1200; the
connector takes 600 to 3600), on the clock anchored on a contact of this process.

In a takeover each step whose `at` is after the last contact applies once, at `max(at, takeover)` and at least 1800 s
after the previous apply on that device; a missed step that a later missed step overtakes never applies, nor does the
period in force at the last contact. An air conditioner's target goes by `climate.set_temperature` only while it reads
`cool`; a dehumidifier's by `humidifier.set_humidity` only while it reads `on`. A value outside the entity's live range
or step is skipped (`invalid_value`), and so is any action while the anchored time and the wall clock are more than
300 s apart (`clock_skew`). The mode exit, only with a `mode_exit` and only on an air conditioner that reads its
`from`, never before its `after`: a `dry` goes to `cool` from the takeover on, and a `heat` only while the first missed
step with `lights_on` is in force, never at a night step. An air conditioner that reads any other mode gets no mode
change, and its steps wait for `cool`. Never a power service, never an off, never a humidifier: the only HA services are
those of `LOCAL_CLIMATE_SERVICES` (actions.py).

The events (`climate_backup_events`) leave the queue only when an answer's `climate_backup_accepted` lists them; one
that 60 accepted answers carried without acknowledging it, or older than 30 days, is dropped with one line. Like the
watches, it runs in its own daemon thread with its own HA client and never calls the Cropilot.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import messages
from .actions import LOCAL_CLIMATE_SERVICES, humidity_fits, temperature_fits
from .clients import HomeAssistantClient, json_body
from .climate_backup_store import (
    AIR_CONDITIONER,
    COOL,
    MAX_ATTEMPTS,
    MAX_DEVICES,
    MAX_EVENT_AGE_SECONDS,
    MAX_EVENTS,
    MAX_FILE_BYTES,
    OPERATION_MODE,
    TARGET_TEMPERATURE,
    ClimateEvent,
    ClimateFile,
    ClimateSchedule,
    ClimateStore,
    Step,
    Value,
    encode,
    parse_schedule,
)
from .context import Clock
from .light_store import parse_instant, utc_iso
from .pump_backup import _answer_server_time, _date_header
from .transport import Response, Transport, TransportError

SKEW_SECONDS = 300.0
STEP_GAP_SECONDS = 1800.0
PASS_SECONDS = 5.0
MIN_PASS_SECONDS = 1.0
IDLE_MAX_SECONDS = 60.0
# A step or an exit that waits on the device's state reads it again this often.
WAIT_READ_SECONDS = 30.0
RETRY_SECONDS = 10.0
FAILED_AFTER_TRIES = 3
REPORT_KEYS_MAX_BYTES = 16384
MAX_EVENTS_PER_REPORT = 40
UNACKNOWLEDGED_AFTER_ANSWERS = 60
LOG_INTERVAL_SECONDS = 3600.0
HALT_POLL_SECONDS = 1.0
ERROR_LOG_INTERVAL_SECONDS = 60.0
UNAVAILABLE_STATES = frozenset({"unavailable", "unknown"})
# What an allowlist entry must allow for its device to take a schedule, by kind's domain.
NEEDED_ACTIONS = {"climate": "set_temperature", "humidifier": "set_humidity"}
WHERE = "reserva do clima"
EXIT = "exit"
STEP = "step"
GO = "go"
WAIT = "wait"


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass
class _Takeover:
    """What the reserve decided on one device in the takeover that follows the contact `contact`."""

    contact: float
    decided: set[float] = field(default_factory=set)
    # A step that could not apply yet, by its `at`, with the reason its skip carries if a later step overtakes it.
    waiting: dict[float, str] = field(default_factory=dict)
    exit_decided: bool = False
    exited: bool = False
    announced: bool = False
    tries: int = 0
    tries_for: tuple[str, float] | None = None
    next_at: float = float("-inf")


@dataclass(frozen=True)
class _Call:
    """The one HA call a visit decided: the mode exit, or a step's target. An exit belongs to the step in force."""

    action: str
    step: Step
    service: str
    body: dict
    capability: str
    value: Value
    takeover: _Takeover


def _offers_cool(attributes: dict) -> bool:
    modes = attributes.get("hvac_modes")
    return isinstance(modes, list) and COOL in modes


class ClimateBackup:
    def __init__(
        self,
        *,
        data_dir: Path,
        transport: Transport,
        supervisor_token: str,
        clock: Clock,
        log: logging.Logger,
    ) -> None:
        self._store = ClimateStore(data_dir)
        # Its own event, never set: the connector's halt is a trigger of the reserve, never its end.
        self._ha = HomeAssistantClient(transport, supervisor_token, threading.Event())
        self._clock = clock
        self._log = log
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._retired = threading.Event()
        self._thread: threading.Thread | None = None
        self._halt_thread: threading.Thread | None = None
        self._held: dict[str, ClimateSchedule] = {}
        self._device_of: dict[str, str | None] | None = None
        # (server_time, monotonic moment it arrived) of the last answer with one, as the backup watering keeps it.
        self._anchor: tuple[float, float] | None = None
        self._contact: float | None = None
        self._unplaced_at: float | None = None
        self._takeovers: dict[str, _Takeover] = {}
        # The monotonic moment of the last target applied on each device, across takeovers.
        self._last_apply: dict[str, float] = {}
        self._carried: dict[str, int] = {}
        self._logged: dict[tuple[str, str], float] = {}
        self._error_logged: dict[str, float] = {}
        content = self._store.read()
        if content is None:
            self._store.set_aside()
            log.warning(messages.CLIMATE_FILE_UNREADABLE)
            content = ClimateFile()
        self._events = content.events
        if self._prune(clock.time()):
            self._persist()

    # The threads

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="reserva-clima", daemon=True)
        self._thread.start()

    def follow_halt(self, halt: threading.Event) -> None:
        """Drop every schedule the moment the connector halts on a stop line or an idle line of the start."""

        def follow() -> None:
            while not self._retired.is_set():
                if halt.wait(HALT_POLL_SECONDS):
                    self.stop()
                    return

        self._halt_thread = threading.Thread(target=follow, name="reserva-clima-parada", daemon=True)
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
                # Nothing held: only an answer or a stop wakes it.
                self._wake.wait()
            else:
                self._clock.wait(self._wake, delay)

    def step(self) -> float | None:
        """One pass; the seconds until the next one, or None when only a wake can bring work."""
        try:
            self._pass()
        except Exception as error:  # noqa: BLE001 - the reserve must survive any defect of one pass
            self._log_error(error)
        return self._cadence()

    # Calls from the transport, the report loop and the start step

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
        device_of: Mapping[str, str | None] | None,
    ) -> None:
        """An accepted report answer: its allowlist entries, its `climate_backup_supported` and `server_time`, and the
        HA device of each entity in the last good map read."""
        received = self._clock.monotonic()
        moment = parse_instant(server_time)
        marked = {
            entry["entity_id"]: entry
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("entity_id"), str) and "climate_backup" in entry
        }
        with self._lock:
            if moment is not None:
                # The answer is itself a contact at its server_time: the reserve never relies on the transport alone.
                if self._anchor is None:
                    self._anchor = (moment, received)
                self._place(moment)
            if device_of is not None:
                self._device_of = dict(device_of)
            if supported is not True:
                # Fail closed: an answer without the flag (an older server) keeps no schedule nor takes one.
                marked = {}
            for entity_id in sorted(self._held):
                if entity_id not in marked:
                    # The entry or its key is gone, or the answer lacks the flag.
                    self._drop(entity_id)
            for entity_id, entry in sorted(marked.items()):
                if entry["climate_backup"] is None:
                    self._drop(entity_id)
                    continue
                reason = self._refusal(entity_id, entry)
                schedule = parse_schedule(entity_id, entry["climate_backup"], moment) if reason is None else None
                if reason is None and schedule is None:
                    reason = "invalid"
                if reason is None and schedule is not None and self._pin(entity_id) != schedule.device_id:
                    reason = "device_unknown" if self._pin(entity_id) is None else "device_mismatch"
                if reason is not None or schedule is None:
                    line = messages.CLIMATE_SCHEDULE_REFUSED.format(entity=entity_id, reason=reason or "invalid")
                    self._log_once(reason or "invalid", entity_id, line)
                    self._drop(entity_id)
                    continue
                self._held[entity_id] = schedule
        self._wake.set()

    def devices(self, device_of: Mapping[str, str | None]) -> None:
        """A good map read: a schedule whose entity the map no longer gives to its pinned device goes."""
        with self._lock:
            self._device_of = dict(device_of)
            for entity_id in sorted(self._held):
                if self._pin(entity_id) != self._held[entity_id].device_id:
                    line = messages.CLIMATE_SCHEDULE_REFUSED.format(entity=entity_id, reason="device_changed")
                    self._log_once("device_changed", entity_id, line)
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
        """A 403: the account lost access, and the reserve fails closed."""
        self.stop()

    def stop(self) -> None:
        """A halt: every schedule goes, and the devices stay as they are."""
        with self._lock:
            held = bool(self._held)
            for entity_id in sorted(self._held):
                self._drop(entity_id)
        if held:
            self._log.warning(messages.CLIMATE_STOPPED)
        self._wake.set()

    def schedules(self) -> list[tuple[str, float]]:
        with self._lock:
            return [(entity_id, self._held[entity_id].valid_until) for entity_id in sorted(self._held)]

    def events_waiting(self) -> bool:
        """An event no accepted answer has carried yet; one already carried waits for the normal cadence."""
        with self._lock:
            return any(event.event_id not in self._carried for event in self._events)

    def snapshot_events(self) -> list[ClimateEvent]:
        with self._lock:
            return list(self._events)

    def report_keys(
        self, candidates: Sequence[ClimateEvent], now: float, max_bytes: int = REPORT_KEYS_MAX_BYTES
    ) -> tuple[list[dict], list[ClimateEvent]]:
        """`climate_backup_events`: the oldest of the events taken before /states that fit in `max_bytes` encoded with
        their key, at most 40; returns the list and the events sent."""
        with self._lock:
            queued = {event.event_id for event in self._events}
        events = [event for event in candidates if event.event_id in queued][:MAX_EVENTS_PER_REPORT]
        wires: list[dict] = []
        sent: list[ClimateEvent] = []
        for event in events:
            wire = event.wire(now)
            if len(_encode({"climate_backup_events": [*wires, wire]})) > max_bytes:
                break
            wires.append(wire)
            sent.append(event)
        return wires, sent

    def acknowledge(self, accepted: Any, carried: Iterable[str]) -> None:
        """An accepted report answer: drop only the events `climate_backup_accepted` lists, and those 60 answers in a
        row carried without acknowledging them."""
        ids = {item for item in accepted if isinstance(item, str)} if isinstance(accepted, list) else set()
        with self._lock:
            held = {event.event_id for event in self._events}
            for event_id in carried:
                if event_id in held and event_id not in ids:
                    self._carried[event_id] = self._carried.get(event_id, 0) + 1
            stale = {event_id for event_id, count in self._carried.items() if count >= UNACKNOWLEDGED_AFTER_ANSWERS}
            kept = [event for event in self._events if event.event_id not in ids and event.event_id not in stale]
            if stale & held:
                self._log.warning(messages.CLIMATE_EVENTS_UNACKNOWLEDGED.format(count=len(stale & held)))
            if len(kept) != len(self._events):
                self._events = kept
                self._persist()
            remaining = {event.event_id for event in self._events}
            self._carried = {event_id: count for event_id, count in self._carried.items() if event_id in remaining}

    # One pass

    def _pass(self) -> None:
        with self._lock:
            mono, wall = self._clock.monotonic(), self._clock.time()
            now = self._server_now(mono)
            self._expire(now, wall)
            if self._prune(wall):
                self._persist()
            visits = self._plans(now, mono)
        for entity_id in visits:
            try:
                self._visit(entity_id)
            except Exception as error:  # noqa: BLE001 - one device's defect must not skip the others
                self._log_error(error)

    def _plans(self, now: float | None, mono: float) -> list[str]:
        """The devices in a takeover with an exit to make or a missed step to apply now."""
        contact = self._last_contact()
        if now is None or contact is None:
            return []
        visits: list[str] = []
        for entity_id in sorted(self._held):
            schedule = self._held[entity_id]
            if now < contact + schedule.takeover_seconds:
                continue
            takeover = self._takeover(entity_id, contact)
            if not takeover.announced:
                takeover.announced = True
                self._log.info(messages.CLIMATE_TAKEOVER.format(entity=entity_id))
            if mono < takeover.next_at:
                continue
            step = self._due_step(entity_id, schedule, takeover, now)
            if step is not None or self._exit_state(schedule, takeover, now) == GO:
                visits.append(entity_id)
        return visits

    def _visit(self, entity_id: str) -> None:
        """One device: one read and at most one call, each outside the lock."""
        state, attributes = self._read(entity_id)
        with self._lock:
            call = self._decide(entity_id, state, attributes)
        if call is None:
            return
        succeeded = self._call(call)
        with self._lock:
            self._after_call(entity_id, call, succeeded)

    # The rule, under the lock

    def _decide(self, entity_id: str, state: str | None, attributes: dict) -> _Call | None:
        """Every check again, on the state read now; the call to make, or None after a wait, a skip or nothing due."""
        schedule = self._held.get(entity_id)
        mono, wall = self._clock.monotonic(), self._clock.time()
        now, contact = self._server_now(mono), self._last_contact()
        if schedule is None or now is None or contact is None or now < contact + schedule.takeover_seconds:
            return None
        if now >= schedule.valid_until or wall >= schedule.valid_until:
            return None
        takeover = self._takeover(entity_id, contact)
        skew = abs(now - wall) > SKEW_SECONDS
        known = state is not None and state not in UNAVAILABLE_STATES
        in_force = schedule.in_force(now)
        mode_exit = schedule.mode_exit
        if mode_exit is not None and self._exit_state(schedule, takeover, now) == GO:
            if skew:
                takeover.exit_decided = True
                self._queue(entity_id, in_force.at, "skipped", "clock_skew", OPERATION_MODE, COOL, 0)
            elif not known:
                takeover.next_at = mono + WAIT_READ_SECONDS
                return None
            elif state == mode_exit.source and _offers_cool(attributes):
                service = LOCAL_CLIMATE_SERVICES[("climate", OPERATION_MODE)]
                body = {"entity_id": entity_id, "hvac_mode": COOL}
                return self._calling(_Call(EXIT, in_force, service, body, OPERATION_MODE, COOL, takeover))
            else:
                # It reads another mode, the person's or already `cool`: no mode change in this takeover.
                takeover.exit_decided = True
        step = self._due_step(entity_id, schedule, takeover, now)
        if step is None:
            return None
        if skew:
            self._skip(entity_id, schedule, takeover, step, "clock_skew")
            return None
        last = self._last_apply.get(entity_id)
        if last is not None and mono < last + STEP_GAP_SECONDS:
            takeover.next_at = last + STEP_GAP_SECONDS
            return None
        if not known:
            takeover.waiting[step.at] = "unavailable"
            takeover.next_at = mono + WAIT_READ_SECONDS
            return None
        if schedule.kind == AIR_CONDITIONER:
            if state == COOL:
                if not temperature_fits(step.target, attributes):
                    self._skip(entity_id, schedule, takeover, step, "invalid_value")
                    return None
                service = LOCAL_CLIMATE_SERVICES[("climate", TARGET_TEMPERATURE)]
                body = {"entity_id": entity_id, "temperature": step.target}
                return self._calling(_Call(STEP, step, service, body, schedule.capability, step.target, takeover))
            if state != "off" and self._exit_pending(schedule, takeover, step, state):
                takeover.waiting[step.at] = "mode"
                takeover.next_at = mono + WAIT_READ_SECONDS
                return None
            self._skip(entity_id, schedule, takeover, step, "state" if state == "off" else "mode")
            return None
        if state != "on":
            self._skip(entity_id, schedule, takeover, step, "state")
            return None
        if not humidity_fits(step.target, attributes):
            self._skip(entity_id, schedule, takeover, step, "invalid_value")
            return None
        service = LOCAL_CLIMATE_SERVICES[("humidifier", schedule.capability)]
        body = {"entity_id": entity_id, "humidity": step.target}
        return self._calling(_Call(STEP, step, service, body, schedule.capability, step.target, takeover))

    def _calling(self, call: _Call) -> _Call:
        """The tries count for one exit or one step at a time."""
        key = (call.action, call.step.at)
        if call.takeover.tries_for != key:
            call.takeover.tries_for = key
            call.takeover.tries = 0
        return call

    def _after_call(self, entity_id: str, call: _Call, succeeded: bool) -> None:
        takeover = call.takeover
        takeover.tries += 1
        attempts = takeover.tries
        if not succeeded and attempts < FAILED_AFTER_TRIES:
            takeover.next_at = self._clock.monotonic() + RETRY_SECONDS
            return
        takeover.tries, takeover.tries_for = 0, None
        kind, reason = ("applied", None) if succeeded else ("failed", "service_error")
        self._queue(entity_id, call.step.at, kind, reason, call.capability, call.value, attempts)
        if call.action == EXIT:
            takeover.exit_decided = True
            takeover.exited = succeeded
            if succeeded:
                self._log.info(messages.CLIMATE_MODE_EXIT.format(entity=entity_id))
            else:
                self._log.warning(self._skip_line(entity_id, call.step, "service_error"))
            return
        takeover.decided.add(call.step.at)
        takeover.waiting.pop(call.step.at, None)
        if succeeded:
            self._last_apply[entity_id] = self._clock.monotonic()
            self._log.info(
                messages.CLIMATE_APPLIED.format(
                    entity=entity_id, step=utc_iso(call.step.at), capability=call.capability, value=call.value
                )
            )
        else:
            self._log.warning(self._skip_line(entity_id, call.step, "service_error"))

    def _due_step(self, entity_id: str, schedule: ClimateSchedule, takeover: _Takeover, now: float) -> Step | None:
        """The latest missed step whose time has come and that is not decided; the missed ones it overtakes are decided
        now, with the skip they were waiting on, if any."""
        latest: Step | None = None
        for step in schedule.steps:
            if step.at <= takeover.contact or step.at > now or step.at in takeover.decided:
                continue
            if latest is not None:
                takeover.decided.add(latest.at)
                reason = takeover.waiting.pop(latest.at, None)
                if reason is not None:
                    self._queue(entity_id, latest.at, "skipped", reason, schedule.capability, latest.target, 0)
                    self._log.warning(self._skip_line(entity_id, latest, reason))
            latest = step
        return latest

    def _exit_state(self, schedule: ClimateSchedule, takeover: _Takeover, now: float) -> str | None:
        """GO when the mode exit may run now, WAIT while it waits for its `after`, None when there is none to make."""
        mode_exit = schedule.mode_exit
        if mode_exit is None or takeover.exit_decided:
            return None
        if mode_exit.source == "heat":
            first_on = self._first_missed_on(schedule, takeover)
            if first_on is None or now < first_on.at:
                return None
            following = schedule.next_of(first_on)
            if following is not None and now >= following.at:
                # Its lights-on step is over: a heat never exits at a later step.
                takeover.exit_decided = True
                return None
        return GO if now >= mode_exit.after else WAIT

    def _exit_pending(self, schedule: ClimateSchedule, takeover: _Takeover, step: Step, state: str) -> bool:
        """An air conditioner that reads the exit's `from` waits for `cool` at this step: the exit already went and the
        read lags, or it may still go at this step."""
        mode_exit = schedule.mode_exit
        if mode_exit is None or state != mode_exit.source:
            return False
        if takeover.exited:
            return True
        if takeover.exit_decided:
            return False
        return mode_exit.source == "dry" or step == self._first_missed_on(schedule, takeover)

    @staticmethod
    def _first_missed_on(schedule: ClimateSchedule, takeover: _Takeover) -> Step | None:
        return next((step for step in schedule.steps if step.at > takeover.contact and step.lights_on), None)

    def _skip(self, entity_id: str, schedule: ClimateSchedule, takeover: _Takeover, step: Step, reason: str) -> None:
        takeover.decided.add(step.at)
        takeover.waiting.pop(step.at, None)
        self._queue(entity_id, step.at, "skipped", reason, schedule.capability, step.target, 0)
        self._log.warning(self._skip_line(entity_id, step, reason))

    @staticmethod
    def _skip_line(entity_id: str, step: Step, reason: str) -> str:
        return messages.CLIMATE_SKIPPED.format(entity=entity_id, step=utc_iso(step.at), reason=reason)

    def _takeover(self, entity_id: str, contact: float) -> _Takeover:
        """The device's record of the takeover after `contact`; a new contact starts a new one."""
        takeover = self._takeovers.get(entity_id)
        if takeover is None or takeover.contact != contact:
            takeover = _Takeover(contact)
            self._takeovers[entity_id] = takeover
        return takeover

    def _refusal(self, entity_id: str, entry: dict) -> str | None:
        actions = entry.get("actions")
        needed = NEEDED_ACTIONS.get(entity_id.split(".", 1)[0])
        if needed is None or not isinstance(actions, list) or needed not in actions:
            return "not_allowed"
        if entity_id not in self._held and len(self._held) >= MAX_DEVICES:
            return "too_many"
        return None

    def _expire(self, now: float | None, wall: float) -> None:
        """Past `valid_until` by the anchored time or by the wall clock, whichever comes first, a schedule goes; one
        that ends a takeover leaves an `expired` event."""
        contact = self._last_contact()
        for entity_id in sorted(self._held):
            schedule = self._held[entity_id]
            if wall < schedule.valid_until and (now is None or now < schedule.valid_until):
                continue
            if now is not None and contact is not None and now >= contact + schedule.takeover_seconds:
                step = schedule.in_force(schedule.valid_until)
                self._queue(entity_id, step.at, "expired", None, schedule.capability, None, 0)
            self._log.warning(messages.CLIMATE_EXPIRED.format(entity=entity_id))
            self._drop(entity_id)

    def _drop(self, entity_id: str) -> None:
        self._held.pop(entity_id, None)
        self._takeovers.pop(entity_id, None)

    # Time and contact, as the backup watering keeps them

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

    def _pin(self, entity_id: str) -> str | None:
        return self._device_of.get(entity_id) if self._device_of is not None else None

    # The events and the file

    def _queue(
        self,
        entity_id: str,
        step_at: float,
        kind: str,
        reason: str | None,
        capability: str,
        value: Value,
        attempts: int,
    ) -> None:
        self._events.append(
            ClimateEvent(
                event_id=str(uuid.uuid4()),
                entity_id=entity_id,
                step_at=step_at,
                kind=kind,
                reason=reason,
                capability=capability,
                value=value,
                attempts=min(max(0, attempts), MAX_ATTEMPTS),
                ended_at=self._clock.time(),
            )
        )
        dropped = len(self._events) - MAX_EVENTS
        if dropped > 0:
            self._events = self._events[dropped:]
            self._log.warning(messages.CLIMATE_EVENTS_DROPPED.format(count=dropped))
        self._persist()

    def _prune(self, wall: float) -> bool:
        """The events of the last 30 days."""
        kept = [event for event in self._events if wall - event.ended_at <= MAX_EVENT_AGE_SECONDS]
        if len(kept) == len(self._events):
            return False
        self._log.warning(messages.CLIMATE_EVENTS_DROPPED.format(count=len(self._events) - len(kept)))
        self._events = kept
        return True

    def _persist(self) -> None:
        content = ClimateFile(events=list(self._events))
        data = encode(content)
        dropped = 0
        while len(data) > MAX_FILE_BYTES and content.events:
            content.events = content.events[1:]
            dropped += 1
            data = encode(content)
        if dropped:
            self._events = list(content.events)
            self._log.warning(messages.CLIMATE_EVENTS_DROPPED.format(count=dropped))
        try:
            self._store.write(data)
        except OSError as error:
            # The events stay in memory and the next change writes them again.
            self._log_error(error)

    # Home Assistant

    def _read(self, entity_id: str) -> tuple[str | None, dict]:
        """The entity's state and attributes; `unavailable` on a 404; no state when HA did not answer with one."""
        try:
            response = self._ha.get(f"/states/{entity_id}")
        except TransportError:
            return None, {}
        if response.status == 404:
            return "unavailable", {}
        payload = json_body(response) if response.status == 200 else None
        if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
            return None, {}
        attributes = payload.get("attributes")
        return payload["state"], attributes if isinstance(attributes, dict) else {}

    def _call(self, call: _Call) -> bool:
        if call.service not in LOCAL_CLIMATE_SERVICES.values():
            raise ValueError("the climate reserve calls only its own services")
        try:
            response = self._ha.post(f"/services/{call.service}", call.body)
        except TransportError:
            return False
        return 200 <= response.status < 300

    # The cadence and the log

    def _cadence(self) -> float | None:
        with self._lock:
            if not self._held:
                return None
            now, contact = self._server_now(self._clock.monotonic()), self._last_contact()
            if now is None or contact is None:
                return PASS_SECONDS
            delays = [IDLE_MAX_SECONDS]
            for schedule in self._held.values():
                start = contact + schedule.takeover_seconds
                if now >= start:
                    return PASS_SECONDS
                delays.append(start - now)
            return max(MIN_PASS_SECONDS, min(delays))

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
