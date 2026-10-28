"""The sensor refresh: every 600 s ± 30 s, one `homeassistant.update_entity` call for the climate entities and the
temperature and humidity sensors the Cropilot uses.

Some integrations write a state in HA only when the value changes, and the report ages a reading from HA's last write:
without a new write, a steady temperature crosses the server's age cap and drops. The service is this module's
constant, and it only asks HA to read the entities again. The server only names the entities it uses (the report's
allowlist); an id leaves only when HA's own state, read by this round's /states, is one of the refreshed kinds. A
failure waits for the next tick and never touches the report's backoff.
"""

from __future__ import annotations

from typing import Any, Iterable

from . import messages
from .context import Context
from .entities import is_refreshed, written_age_seconds
from .transport import TransportError

REFRESH_PATH = "/services/homeassistant/update_entity"
INTERVAL_SECONDS = 600.0
JITTER_SECONDS = 30.0
# Under the shortest interval (570 s), so an entity the last tick refreshed is due again at the next one.
MIN_AGE_SECONDS = 540
MAX_ENTITIES = 50


def targets(states: Iterable[Any], allowlist_ids: frozenset[str], now: float) -> list[str]:
    """The allowlisted ids of a refreshed kind last written `MIN_AGE_SECONDS` ago or more; the stalest first, at most
    `MAX_ENTITIES`."""
    ages: dict[str, int] = {}
    for state in states:
        if not is_refreshed(state) or state["entity_id"] not in allowlist_ids:
            continue
        age = written_age_seconds(state, now)
        if age is not None and age >= MIN_AGE_SECONDS:
            ages[state["entity_id"]] = age
    return sorted(ages, key=lambda entity_id: (-ages[entity_id], entity_id))[:MAX_ENTITIES]


class SensorRefresher:
    """Called by the report loop after each report, with the states that report read."""

    def __init__(self, context: Context) -> None:
        self._context = context
        self._next_at: float | None = None
        self._failing = False

    def refresh(self, states: Iterable[Any], now: float) -> None:
        """One call when a tick is due, the allowlist has arrived and an entity needs it; `Halted` propagates."""
        context = self._context
        if not context.allowlist.ready.is_set():
            return
        moment = context.clock.monotonic()
        if self._next_at is not None and moment < self._next_at:
            return
        self._next_at = moment + INTERVAL_SECONDS + context.rng.uniform(-JITTER_SECONDS, JITTER_SECONDS)
        ids = targets(states, context.allowlist.entity_ids(), now)
        if not ids:
            return
        try:
            response = context.ha.post(REFRESH_PATH, {"entity_id": ids})
        except TransportError as error:
            self._failed(len(ids), error.kind)
            return
        if response.status != 200:
            self._failed(len(ids), f"HTTP {response.status}")
            return
        self._failing = False

    def _failed(self, count: int, detail: str) -> None:
        if self._failing:
            return
        self._failing = True
        self._context.log.warning(messages.SENSOR_REFRESH_FAILED.format(count=count, detail=detail))
