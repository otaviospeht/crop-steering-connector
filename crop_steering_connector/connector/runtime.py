"""Start step, pairing and the two loop threads, after the pump watch.

Idle until a restart: a stop line (a revoked pairing, a refused pairing code, a refused server address or a
missing code) leaves the process running with no loop and no Cropilot call, because the Supervisor's watchdog
restarts an app that exits and would repeat the line in a loop. The pump watch starts first, from the pump file,
and keeps running through all of them.
"""

from __future__ import annotations

import logging
import random
import threading
import uuid
from pathlib import Path
from typing import Callable

from . import __version__, messages, policy
from .backoff import Backoff, parse_retry_after
from .clients import Halted, HomeAssistantClient, ServerClient, describe, error_code, is_success, json_body
from .commands import CommandLoop
from .context import Clock, Context
from .pump import PumpWatch
from .reporting import BACKOFF_BASE_SECONDS, BACKOFF_CAP_SECONDS, ReportLoop, read_running_config, utc_now_iso
from .settings import is_connector_token, load_options, pairing_code_state, resolve_server_url
from .token_store import TokenStore
from .transport import Response, Transport, TransportError

RUNNING_POLL_SECONDS = 10.0
UNEXPECTED_ERROR_DELAY_SECONDS = 30.0


class Connector:
    def __init__(
        self,
        *,
        data_dir: Path,
        supervisor_token: str | None,
        transport: Transport,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self.run_id = str(uuid.uuid4())
        self.halt = threading.Event()
        self._data_dir = data_dir
        self._supervisor_token = supervisor_token
        self._transport = transport
        self._clock = clock or Clock()
        self._rng = rng or random.SystemRandom()
        self._log = log or logging.getLogger("crop_steering_connector")
        self.context: Context | None = None
        self.pump_watch: PumpWatch | None = None

    def run(self) -> None:
        """Start the pump watch, then start and run both loops until the connector halts; returns when it is idle."""
        if self._supervisor_token:
            # Before the options, the token, the pairing and any Cropilot call.
            self.pump_watch = PumpWatch(
                data_dir=self._data_dir,
                transport=self._transport,
                supervisor_token=self._supervisor_token,
                clock=self._clock,
                log=self._log,
            )
            self.pump_watch.start()
        context = self.start()
        if context is None:
            return
        threads = [
            threading.Thread(target=self._loop, args=("relatório", ReportLoop(context).tick), daemon=True),
            threading.Thread(target=self._loop, args=("comandos", self._command_cycle(context)), daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def start(self) -> Context | None:
        """Validate, wait for HA and pair; None means idle (the reason was logged once)."""
        log = self._log
        log.info(messages.STARTED.format(version=__version__))
        options = load_options(self._data_dir / "options.json")
        server_url = resolve_server_url(options.server_url)
        if server_url is None:
            return self._idle(messages.V34_SERVER_URL_INVALID)
        token_store = TokenStore(self._data_dir)
        token = token_store.read()
        if token is None:
            code_state = pairing_code_state(options.pairing_code)
            if code_state == "absent":
                return self._idle(messages.V35_NO_PAIRING_CODE)
            if code_state == "malformed":
                return self._idle(messages.V33_PAIRING_CODE_REFUSED)
        if not self._supervisor_token:
            return self._idle(messages.NO_SUPERVISOR_TOKEN)
        context = Context(
            server=ServerClient(self._transport, server_url, self.run_id, self.halt),
            ha=HomeAssistantClient(self._transport, self._supervisor_token, self.halt),
            token_store=token_store,
            clock=self._clock,
            rng=self._rng,
            log=log,
            halt=self.halt,
            pump_watch=self.pump_watch,
        )
        try:
            if not self._wait_running(context):
                return None
            if token is None:
                token = self._pair(context, options.pairing_code)
                if token is None:
                    return None
        except Halted:
            return None
        context.token = token
        self.context = context
        return context

    def _idle(self, line: str) -> None:
        self.halt.set()
        self._log.error(line)
        return None

    def _wait_running(self, context: Context) -> bool:
        logged = False
        while read_running_config(context) is None:
            if not logged:
                context.log.info(messages.V17_WAITING_HA)
                logged = True
            if context.sleep(RUNNING_POLL_SECONDS):
                return False
        return True

    def _pair(self, context: Context, pairing_code: str) -> str | None:
        backoff = Backoff(BACKOFF_BASE_SECONDS, BACKOFF_CAP_SECONDS, context.rng)
        endpoint = policy.ENDPOINT_NAMES[policy.PAIR]
        while True:
            body = {
                "connector_version": __version__,
                "ha_version": context.ha_version,
                "sent_at": utc_now_iso(context.clock.time()),
            }
            retry_after = None
            try:
                answer = context.server.post("/pair", pairing_code, body)
            except TransportError as error:
                detail = error.kind
            else:
                if is_success(answer):
                    token = _token_of(answer)
                    if token is not None:
                        self._store(context, token)
                        context.log.info(messages.V16_PAIRED)
                        return token
                    detail = "resposta inválida"
                else:
                    detail = describe(answer)
                    action = policy.classify(policy.PAIR, answer.status, error_code(answer))
                    if action == policy.STOP:
                        line = messages.V33_PAIRING_CODE_REFUSED if answer.status == 401 else None
                        context.stop(line or messages.PAIR_STOPPED.format(detail=detail))
                        return None
                    if action == policy.BACKOFF_300:
                        context.forbidden()
                        if context.sleep(backoff.ceiling()):
                            return None
                        continue
                    if action == policy.RETRY_AFTER:
                        retry_after = parse_retry_after(answer.header("retry-after"), context.clock.time())
            delay = backoff.next_delay(retry_after)
            context.log.warning(messages.ENDPOINT_FAILED.format(endpoint=endpoint, detail=detail, seconds=round(delay)))
            if context.sleep(delay):
                return None

    def _store(self, context: Context, token: str) -> None:
        try:
            context.token_store.write(token)
        except OSError as error:
            # The token still works in memory; a restart without the file needs a new code.
            context.log.error(messages.TOKEN_WRITE_FAILED.format(error=type(error).__name__))

    def _command_cycle(self, context: Context) -> Callable[[], float]:
        loop = CommandLoop(context)
        waiting = {"allowlist": True}

        def cycle() -> float:
            # No claim before the first allowlist arrives.
            if waiting["allowlist"]:
                if not context.allowlist.ready.is_set():
                    return 1.0
                waiting["allowlist"] = False
            return loop.cycle()

        return cycle

    def _loop(self, name: str, cycle: Callable[[], float]) -> None:
        while not self.halt.is_set():
            try:
                delay = cycle()
            except Halted:
                return
            except Exception as error:  # noqa: BLE001 - one loop must survive a defect of one round
                self._log.error(messages.UNEXPECTED_ERROR.format(where=name, error=type(error).__name__))
                delay = UNEXPECTED_ERROR_DELAY_SECONDS
            if self._clock.wait(self.halt, delay):
                return


def _token_of(answer: Response) -> str | None:
    payload = json_body(answer)
    data = payload.get("data") if isinstance(payload, dict) else None
    token = data.get("token") if isinstance(data, dict) else None
    return token if is_connector_token(token) else None
