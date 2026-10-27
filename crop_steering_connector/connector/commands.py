"""The command loop: claim, checks, start, the HA call and the result that is never dropped."""

from __future__ import annotations

from collections import deque

from . import messages, policy
from .actions import (
    NEEDS_POWER_ON,
    Command,
    InvalidCommand,
    ServiceSpec,
    deadline_of,
    max_on_seconds,
    parse_command,
    service_body,
    service_spec,
)
from .backoff import Backoff, parse_retry_after
from .clients import Halted, describe, error_code, is_success, json_body
from .context import Context
from .reporting import utc_now_iso
from .transport import CONNECT, Response, TransportError

CLAIM_INTERVAL_SECONDS = 10.0
CLAIM_JITTER_SECONDS = 2.0
MAX_COMMANDS_PER_CLAIM = 5
ENDPOINT_BACKOFF = (10.0, 300.0)
RESULT_BACKOFF = (2.0, 300.0)
POWER_ON_WAIT_SECONDS = 10.0
POWER_ON_POLL_SECONDS = 1.0
POWER_ON_MAX_POLLS = 10
POWER_ON_MARK_TTL_SECONDS = 180.0
HANDLED_IDS_KEPT = 10_000
UNAVAILABLE_STATES = frozenset({"unavailable", "unknown"})
# A target change on these can power the unit on: it is refused while the state reads `off`, and after a
# power_on it waits for the state to leave `off`. The dehumidifier is here for its programmed humidity.
POWERED_DOMAINS = frozenset({"climate", "humidifier"})


class Refused(Exception):
    """A check before the start failed; the code is posted as the result and nothing ran."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CommandLoop:
    def __init__(self, context: Context) -> None:
        self._context = context
        self._claim_backoff = Backoff(*ENDPOINT_BACKOFF, context.rng)
        self._start_backoff = Backoff(*ENDPOINT_BACKOFF, context.rng)
        self._start_not_before = 0.0
        self._handled: set[str] = set()
        self._handled_order: deque[str] = deque()
        # entity_id -> (monotonic time of a succeeded power_on of POWERED_DOMAINS, its `changed_states` state or None)
        self._power_on_marks: dict[str, tuple[float, dict | None]] = {}

    # The claim

    def cycle(self) -> float:
        """One claim and its batch; returns the seconds until the next claim."""
        context = self._context
        try:
            answer = context.server.post("/commands/claim", context.token)
        except TransportError as error:
            return self._endpoint_failed(policy.CLAIM, self._claim_backoff, error.kind)
        received_at = context.clock.monotonic()
        if not is_success(answer):
            return self._endpoint_refused(policy.CLAIM, self._claim_backoff, answer)
        payload = json_body(answer)
        data = payload.get("data") if isinstance(payload, dict) else None
        items = data.get("commands") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return self._endpoint_failed(policy.CLAIM, self._claim_backoff, "resposta inválida")
        self._claim_backoff.reset()
        if not items:
            return CLAIM_INTERVAL_SECONDS + context.rng.uniform(-CLAIM_JITTER_SECONDS, CLAIM_JITTER_SECONDS)
        if len(items) > MAX_COMMANDS_PER_CLAIM:
            context.log.warning(messages.TOO_MANY_COMMANDS.format(count=len(items)))
        batch = items[:MAX_COMMANDS_PER_CLAIM]
        for position, item in enumerate(batch):
            if context.halt.is_set():
                break
            command = parse_command(item, received_at)
            if command is None:
                context.log.warning(messages.INVALID_COMMAND_ID)
                continue
            if not self.handle(command):
                left = len(batch) - position - 1
                if left and not context.halt.is_set():
                    context.log.warning(messages.BATCH_DROPPED.format(count=left))
                break
        return 0.0

    # One command

    def handle(self, command: Command) -> bool:
        """Run one command; False when its result needed retries, so the rest of the batch is not run."""
        context = self._context
        if command.id in self._handled:
            context.log.warning(messages.DUPLICATE_COMMAND.format(id=command.id))
            return True
        self._remember(command.id)
        wait = self._start_not_before - context.clock.monotonic()
        if wait > 0 and context.sleep(wait):
            return False
        try:
            spec, body = self._checks(command)
        except Refused as refusal:
            context.log.warning(
                messages.COMMAND_REFUSED.format(
                    id=command.id, action=command.label_action, entity=command.label_entity, code=refusal.code
                )
            )
            self._post_refusal(command, refusal.code)
            return True
        if not self._start(command):
            return True
        watch = context.pump_watch if command.action == "pulse_on" else None
        if watch is not None:
            # Just before the HA call, so an `on` read from here on belongs to this shot.
            watch.pulse_starting(command.entity_id, command.id, max_on_seconds(command))
        outcome, code, changed = self._call(spec.domain, spec.service, body)
        if watch is not None:
            if outcome == "succeeded":
                watch.pulse_succeeded(command.entity_id, command.id)
            else:
                watch.pulse_failed(command.entity_id, command.id)
        if outcome == "succeeded" and command.domain in POWERED_DOMAINS and command.action == "power_on":
            self._power_on_marks[command.entity_id] = (context.clock.monotonic(), changed.get(command.entity_id))
        context.log.info(
            messages.COMMAND_DONE.format(
                id=command.id,
                action=command.label_action,
                entity=command.label_entity,
                outcome=outcome if code is None else f"{outcome} {code}",
            )
        )
        return self._post_started_result(command, outcome, code)

    def _remember(self, command_id: str) -> None:
        self._handled.add(command_id)
        self._handled_order.append(command_id)
        while len(self._handled_order) > HANDLED_IDS_KEPT:
            self._handled.discard(self._handled_order.popleft())

    def _checks(self, command: Command) -> tuple[ServiceSpec, dict]:
        """The refusals before the start, in order; returns the service and its body when the command may start."""
        context = self._context
        deadline = deadline_of(command)
        if deadline is None:
            raise Refused("invalid_command")
        if context.clock.monotonic() >= deadline:
            raise Refused("expired")
        allowed = context.allowlist.actions_of(command.entity_id) if isinstance(command.entity_id, str) else None
        if allowed is None or not isinstance(command.action, str) or command.action not in allowed:
            raise Refused("not_allowlisted")
        if command.action == "pulse_on" and (context.pump_watch is None or not context.pump_watch.is_pump(command.entity_id)):
            # A pulse the watch would not follow is never sent.
            raise Refused("not_allowlisted")
        try:
            spec = service_spec(command)
        except InvalidCommand:
            raise Refused("invalid_command") from None
        state = self._live_state(command.entity_id)
        try:
            body = service_body(command, spec, state)
        except InvalidCommand:
            raise Refused("invalid_command") from None
        if command.action == "pulse_on" and state.get("state") != "off":
            raise Refused("already_on")
        if command.action in NEEDS_POWER_ON and command.domain in POWERED_DOMAINS and state.get("state") == "off":
            raise Refused("would_power_on")
        if context.clock.monotonic() >= deadline:
            raise Refused("expired")
        return spec, body

    def _live_state(self, entity_id: str) -> dict:
        """HA's state of the entity, after the wait that follows a power_on that succeeded."""
        context = self._context
        mark = self._power_on_marks.pop(entity_id, None)
        if mark is not None and context.clock.monotonic() - mark[0] > POWER_ON_MARK_TTL_SECONDS:
            mark = None
        if mark is not None and _usable(mark[1]) and mark[1].get("state") != "off":
            state = mark[1]
        else:
            state = self._read_state(entity_id)
            waited_until = context.clock.monotonic() + POWER_ON_WAIT_SECONDS
            polls = 0
            # The poll count also bounds the wait when the monotonic clock stands still.
            while (
                mark is not None
                and state.get("state") == "off"
                and polls < POWER_ON_MAX_POLLS
                and context.clock.monotonic() < waited_until
            ):
                if context.sleep(POWER_ON_POLL_SECONDS):
                    raise Halted()
                polls += 1
                state = self._read_state(entity_id)
        if state.get("state") in UNAVAILABLE_STATES:
            raise Refused("entity_unavailable")
        return state

    def _read_state(self, entity_id: str) -> dict:
        try:
            response = self._context.ha.get(f"/states/{entity_id}")
        except TransportError:
            raise Refused("ha_unreachable") from None
        if response.status == 404:
            raise Refused("entity_unavailable")
        payload = json_body(response) if response.status == 200 else None
        if not _usable(payload):
            raise Refused("ha_unreachable")
        return payload

    # The start and the HA call

    def _start(self, command: Command) -> bool:
        context = self._context
        label = {"id": command.id, "action": command.label_action, "entity": command.label_entity}
        try:
            answer = context.server.post(
                f"/commands/{command.id}/start", context.token, {"sent_at": utc_now_iso(context.clock.time())}
            )
        except TransportError as error:
            self._start_failed(error.kind)
            context.log.warning(messages.COMMAND_NOT_STARTED.format(**label, detail=error.kind))
            return False
        payload = json_body(answer) if is_success(answer) else None
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and data.get("started") is True:
            self._start_backoff.reset()
            self._start_not_before = 0.0
            return True
        if not is_success(answer):
            action = policy.classify(policy.START, answer.status, error_code(answer))
            if action == policy.STOP:
                context.stop(messages.V13_REVOKED, forget_token=True)
                return False
            if action == policy.BACKOFF_300:
                context.forbidden()
                self._start_not_before = context.clock.monotonic() + self._start_backoff.ceiling()
            elif action == policy.RETRY_AFTER:
                retry_after = parse_retry_after(answer.header("retry-after"), context.clock.time())
                self._start_failed(describe(answer), retry_after)
            elif policy.is_transient(answer.status):
                self._start_failed(describe(answer))
        context.log.warning(messages.COMMAND_NOT_STARTED.format(**label, detail=describe(answer)))
        return False

    def _start_failed(self, detail: str, retry_after: float | None = None) -> None:
        delay = self._start_backoff.next_delay(retry_after)
        self._start_not_before = self._context.clock.monotonic() + delay

    def _call(self, domain: str, service: str, body: dict) -> tuple[str, str | None, dict[str, dict]]:
        """The only action after the start `200`: one HA service call, mapped to an outcome."""
        try:
            response = self._context.ha.post(f"/services/{domain}/{service}", body)
        except TransportError as error:
            return "failed", ("ha_unreachable" if error.phase == CONNECT else "ha_error"), {}
        if response.status == 200:
            return "succeeded", None, _changed_states(json_body(response))
        if response.status in (400, 404):
            return "failed", "ha_rejected", {}
        return "failed", "ha_error", {}

    # Results

    def _post_refusal(self, command: Command, code: str) -> None:
        """A refusal before the start is posted once: if it is lost, the row closes as expired, never run."""
        context = self._context
        try:
            answer = context.server.post(
                f"/commands/{command.id}/result", context.token, {"outcome": "failed", "error_code": code}
            )
        except TransportError as error:
            context.log.warning(messages.RESULT_NOT_SENT.format(id=command.id, detail=error.kind))
            return
        if is_success(answer):
            return
        if policy.classify(policy.RESULT, answer.status, error_code(answer)) == policy.STOP:
            context.stop(messages.V13_REVOKED, forget_token=True)
            return
        context.log.warning(messages.RESULT_NOT_SENT.format(id=command.id, detail=describe(answer)))

    def _post_started_result(self, command: Command, outcome: str, code: str | None) -> bool:
        """Retry until the server gives a final answer; True when the first attempt settled it.

        While it retries, the loop does nothing else: no other command of the batch and no claim, so a
        connector that claims never holds a pending result.
        """
        context = self._context
        payload = {"outcome": outcome} if code is None else {"outcome": outcome, "error_code": code}
        backoff = Backoff(*RESULT_BACKOFF, context.rng)
        while True:
            retry_after = None
            try:
                answer = context.server.post(f"/commands/{command.id}/result", context.token, payload)
            except TransportError as error:
                detail = error.kind
            else:
                if is_success(answer):
                    return backoff.failures == 0
                detail = describe(answer)
                action = policy.classify(policy.RESULT, answer.status, error_code(answer))
                if action == policy.STOP:
                    context.stop(messages.V13_REVOKED, forget_token=True)
                    return False
                if action == policy.FINAL:
                    context.log.warning(messages.RESULT_SETTLED.format(id=command.id, detail=detail))
                    return backoff.failures == 0
                if action == policy.RETRY_AFTER:
                    retry_after = parse_retry_after(answer.header("retry-after"), context.clock.time())
            delay = backoff.next_delay(retry_after)
            context.log.warning(messages.RESULT_RETRY.format(id=command.id, detail=detail, seconds=round(delay)))
            if context.sleep(delay):
                return False

    # Endpoint errors of the claim

    def _endpoint_refused(self, endpoint: str, backoff: Backoff, answer: Response) -> float:
        context = self._context
        action = policy.classify(endpoint, answer.status, error_code(answer))
        if action == policy.STOP:
            context.stop(messages.V13_REVOKED, forget_token=True)
            return 0.0
        if action == policy.BACKOFF_300:
            context.forbidden()
            return backoff.ceiling()
        retry_after = None
        if action == policy.RETRY_AFTER:
            retry_after = parse_retry_after(answer.header("retry-after"), context.clock.time())
        return self._endpoint_failed(endpoint, backoff, describe(answer), retry_after)

    def _endpoint_failed(self, endpoint: str, backoff: Backoff, detail: str, retry_after: float | None = None) -> float:
        delay = backoff.next_delay(retry_after)
        self._context.log.warning(
            messages.ENDPOINT_FAILED.format(endpoint=policy.ENDPOINT_NAMES[endpoint], detail=detail, seconds=round(delay))
        )
        return delay


def _usable(state: object) -> bool:
    return isinstance(state, dict) and isinstance(state.get("state"), str)


def _changed_states(payload: object) -> dict[str, dict]:
    """`changed_states` of a service call: the states the call changed, by entity id."""
    if not isinstance(payload, list):
        return {}
    return {
        item["entity_id"]: item
        for item in payload
        if isinstance(item, dict) and isinstance(item.get("entity_id"), str) and _usable(item)
    }
