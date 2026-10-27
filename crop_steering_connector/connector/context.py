"""State shared by the start step and the two loops: clock, halt, allowlist, the pump watch and the account line."""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import TYPE_CHECKING, Iterable

from . import messages
from .clients import HomeAssistantClient, ServerClient
from .token_store import TokenStore

if TYPE_CHECKING:
    from .pump import PumpWatch

V14_INTERVAL_SECONDS = 3600.0


class Clock:
    """Wall time for `sent_at` and ages, the monotonic clock for every deadline, and an interruptible wait."""

    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def wait(self, event: threading.Event, seconds: float) -> bool:
        """Sleep up to `seconds`; True when `event` was set (the connector halted)."""
        return event.wait(max(0.0, seconds))


class Allowlist:
    """The entities and actions of the last report answer, handed from the report loop to the command loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._actions: dict[str, frozenset[str]] = {}
        self.ready = threading.Event()

    def replace(self, entries: Iterable[tuple[str, Iterable[str]]]) -> None:
        actions = {entity_id: frozenset(allowed) for entity_id, allowed in entries}
        with self._lock:
            self._actions = actions
        self.ready.set()

    def actions_of(self, entity_id: str) -> frozenset[str] | None:
        with self._lock:
            return self._actions.get(entity_id)

    def entity_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._actions)


class Context:
    def __init__(
        self,
        *,
        server: ServerClient,
        ha: HomeAssistantClient,
        token_store: TokenStore,
        clock: Clock,
        rng: random.Random,
        log: logging.Logger,
        halt: threading.Event,
        pump_watch: PumpWatch | None = None,
    ) -> None:
        self.server = server
        self.ha = ha
        self.token_store = token_store
        self.clock = clock
        self.rng = rng
        self.log = log
        self.halt = halt
        # Started before this context, from the pump file; None only where there is no HA access (tests).
        self.pump_watch = pump_watch
        self.allowlist = Allowlist()
        self.token = ""
        self.ha_version = "unknown"
        self._lock = threading.Lock()
        self._last_v14: float | None = None

    def sleep(self, seconds: float) -> bool:
        """True when the connector halted during the sleep."""
        return self.clock.wait(self.halt, seconds)

    def stop(self, line: str, forget_token: bool = False) -> None:
        """Stop both loops and stay idle until a restart; the line is logged once, whoever stops first."""
        with self._lock:
            if self.halt.is_set():
                return
            self.halt.set()
        if forget_token:
            # A 401 means this pairing is dead; without the file a restart pairs with the code pasted in its place.
            try:
                self.token_store.delete()
            except OSError as error:
                self.log.warning(messages.UNEXPECTED_ERROR.format(where="pareamento", error=type(error).__name__))
        self.log.error(line)

    def forbidden(self) -> None:
        now = self.clock.monotonic()
        with self._lock:
            if self._last_v14 is not None and now - self._last_v14 < V14_INTERVAL_SECONDS:
                return
            self._last_v14 = now
        self.log.warning(messages.V14_FORBIDDEN)
