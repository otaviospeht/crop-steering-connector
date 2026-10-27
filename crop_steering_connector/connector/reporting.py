"""The report loop: HA states → filter → ages → trim → `POST /reports`, every 60 s ± 5 s.

A 0.2.0 report also carries the pump watch's queued events, and a queued event brings the next report
forward, at least 15 s after the last one (the server takes 4 reports a minute). It also carries each
entity's HA device and the device names, from the device map read before /states.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import __version__, messages, policy
from .backoff import Backoff, parse_retry_after
from .clients import describe, error_code, is_success, json_body
from .context import Context
from .devices import DeviceMapReader
from .entities import build_report, report_entities
from .pump_store import PUMP_ENTITY_RE, PumpEvent, is_max_on
from .transport import Response, TransportError

REPORT_INTERVAL_SECONDS = 60.0
REPORT_JITTER_SECONDS = 5.0
BACKOFF_BASE_SECONDS = 10.0
BACKOFF_CAP_SECONDS = 300.0
EARLY_REPORT_AFTER_SECONDS = 15.0
TICK_SECONDS = 1.0


def utc_now_iso(now: float) -> str:
    return datetime.fromtimestamp(int(now), tz=timezone.utc).isoformat()


def read_running_config(context: Context) -> dict | None:
    """HA's `/config` when its state is RUNNING, otherwise None."""
    try:
        response = context.ha.get("/config")
    except TransportError:
        return None
    if response.status != 200:
        return None
    payload = json_body(response)
    if not isinstance(payload, dict) or payload.get("state") != "RUNNING":
        return None
    version = payload.get("version")
    if isinstance(version, str) and 0 < len(version) <= 32:
        context.ha_version = version
    return payload


class ReportLoop:
    def __init__(self, context: Context) -> None:
        self._context = context
        self._backoff = Backoff(BACKOFF_BASE_SECONDS, BACKOFF_CAP_SECONDS, context.rng)
        self._devices = DeviceMapReader(context)
        self._restriction = 0
        self._ha_was_running = True
        self._first_report_logged = False
        self._last_dropped = 0
        self._sent_events: list[PumpEvent] = []
        self._accepted_last = False
        self._last_at: float | None = None
        self._next_at = 0.0

    def _interval(self) -> float:
        return REPORT_INTERVAL_SECONDS + self._context.rng.uniform(-REPORT_JITTER_SECONDS, REPORT_JITTER_SECONDS)

    def tick(self) -> float:
        """The runtime's round: a report when one is due, or early for a queued pump event; never more than 1 s."""
        clock = self._context.clock
        now = clock.monotonic()
        if now >= self._next_at or self._early(now):
            self._last_at = now
            self._next_at = now + self.cycle()
            now = clock.monotonic()
        return max(0.0, min(TICK_SECONDS, self._next_at - now))

    def _early(self, now: float) -> bool:
        # Only after an accepted report: a backoff or a Retry-After is never cut short.
        watch = self._context.pump_watch
        return (
            watch is not None
            and self._accepted_last
            and self._last_at is not None
            and now - self._last_at >= EARLY_REPORT_AFTER_SECONDS
            and watch.events_waiting()
        )

    def cycle(self) -> float:
        """One round; returns the seconds until the next one."""
        context = self._context
        config = read_running_config(context)
        if config is None:
            if self._ha_was_running:
                context.log.info(messages.V17_WAITING_HA)
            self._ha_was_running = False
            return self._interval()
        self._ha_was_running = True
        unit = config.get("unit_system", {}).get("temperature") if isinstance(config.get("unit_system"), dict) else None
        if not isinstance(unit, str) or not unit:
            context.log.warning(messages.HA_READ_FAILED.format(path="/config", detail="unit_system"))
            return self._interval()
        device_map = self._devices.current()
        # The events go out as they stood before /states is read, so an `ended` never travels beside an `on`
        # read before it; one queued during the read waits for the next report.
        watch = context.pump_watch
        self._sent_events = watch.snapshot_events() if watch is not None else []
        try:
            response = context.ha.get("/states")
        except TransportError as error:
            context.log.warning(messages.HA_READ_FAILED.format(path="/states", detail=error.kind))
            return self._interval()
        states = json_body(response) if response.status == 200 else None
        if not isinstance(states, list):
            context.log.warning(messages.HA_READ_FAILED.format(path="/states", detail=f"HTTP {response.status}"))
            return self._interval()
        now = context.clock.time()
        entities = report_entities(states, now, device_map.devices if device_map is not None else None)
        envelope = {
            "connector_version": __version__,
            "ha_version": context.ha_version,
            "sent_at": utc_now_iso(now),
            "unit_system": {"temperature": unit},
            "pump_events": [event.wire(now) for event in self._sent_events],
        }
        body, sent, dropped = build_report(
            envelope,
            entities,
            context.allowlist.entity_ids(),
            self._restriction,
            device_names=device_map.names if device_map is not None else None,
        )
        if self._restriction == 0 and dropped != self._last_dropped:
            self._last_dropped = dropped
            if dropped:
                context.log.warning(messages.REPORT_TRIMMED.format(count=dropped))
        try:
            answer = context.server.post("/reports", context.token, body=body)
        except TransportError as error:
            self._accepted_last = False
            return self._failed(error.kind)
        if is_success(answer):
            return self._accepted(answer, sent)
        self._accepted_last = False
        return self._refused(answer)

    def _accepted(self, answer: Response, sent: int) -> float:
        watch = self._context.pump_watch
        if watch is not None and self._sent_events:
            # Any 2xx took the events, whatever the body says.
            watch.drop_events(event.id for event in self._sent_events)
        self._sent_events = []
        payload = json_body(answer)
        data = payload.get("data") if isinstance(payload, dict) else None
        entries = data.get("allowlist") if isinstance(data, dict) else None
        parsed = _parse_allowlist(entries)
        if parsed is None:
            self._accepted_last = False
            return self._failed("resposta inválida")
        if watch is not None:
            # The pumps first, so a pulse_on the new allowlist allows always finds its pump.
            watch.replace_list(_parse_pumps(entries))
        self._context.allowlist.replace(parsed)
        self._accepted_last = True
        self._backoff.reset()
        self._restriction = 0
        if not self._first_report_logged:
            self._first_report_logged = True
            self._context.log.info(messages.FIRST_REPORT.format(sent=sent, allowed=len(parsed)))
        return self._interval()

    def _refused(self, answer: Response) -> float:
        context = self._context
        action = policy.classify(policy.REPORTS, answer.status, error_code(answer))
        if action == policy.STOP:
            context.stop(messages.V13_REVOKED, forget_token=True)
            return 0.0
        if action == policy.BACKOFF_300:
            context.forbidden()
            return self._backoff.ceiling()
        if action == policy.ALLOWLISTED_ONLY:
            self._restriction += 1
            if answer.status == 413:
                context.log.warning(messages.V15_TOO_LARGE)
            else:
                context.log.warning(messages.REPORT_REFUSED.format(detail=describe(answer)))
            return self._backoff.next_delay()
        retry_after = None
        if action == policy.RETRY_AFTER:
            retry_after = parse_retry_after(answer.header("retry-after"), context.clock.time())
        return self._failed(describe(answer), retry_after)

    def _failed(self, detail: str, retry_after: float | None = None) -> float:
        delay = self._backoff.next_delay(retry_after)
        self._context.log.warning(
            messages.ENDPOINT_FAILED.format(
                endpoint=policy.ENDPOINT_NAMES[policy.REPORTS], detail=detail, seconds=round(delay)
            )
        )
        return delay


def _parse_allowlist(entries: object) -> list[tuple[str, list[str]]] | None:
    if not isinstance(entries, list):
        return None
    parsed: list[tuple[str, list[str]]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        entity_id = entry.get("entity_id")
        actions = entry.get("actions")
        if not isinstance(entity_id, str) or not isinstance(actions, list):
            return None
        if not all(isinstance(action, str) for action in actions):
            return None
        if "pump" in entry and not _valid_pump_marker(entry["pump"]):
            return None
        parsed.append((entity_id, actions))
    return parsed


def _valid_pump_marker(marker: object) -> bool:
    """`{"max_on_seconds": <int 20..915|null>}`; a 0.1.0 server never sends it."""
    if not isinstance(marker, dict) or "max_on_seconds" not in marker:
        return False
    value = marker["max_on_seconds"]
    return value is None or is_max_on(value)


def _parse_pumps(entries: list) -> list[tuple[str, int | None]]:
    """The pumps of an allowlist `_parse_allowlist` accepted: only a `switch` with the marker is a pump."""
    return [
        (entry["entity_id"], entry["pump"]["max_on_seconds"])
        for entry in entries
        if "pump" in entry and PUMP_ENTITY_RE.fullmatch(entry["entity_id"])
    ]
