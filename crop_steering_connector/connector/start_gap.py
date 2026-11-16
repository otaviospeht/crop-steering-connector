"""The start gap (0.2.15): a claimed command may carry `not_before`, and it never reaches HA before then.

The server spaces the compressor starts of a space with several air conditioners or dehumidifiers (15 s between two
starts of the same type) by giving each start a `not_before`, an ISO 8601 instant with an offset. The connector
holds such a command in this queue, without blocking the loop: a command without `not_before` runs at once, as
before, unless a command for the same entity is still waiting, in which case it goes behind it, so a target change
never overtakes the power_on it follows.

Time. `not_before` is read on the server's clock: the last report answer's `server_time` plus the monotonic clock
since it (the wall clock only before the first answer), so a skewed HA clock never shortens a gap. It becomes a
monotonic instant at the claim, the same reference for the whole batch.

Spacing. The queue runs in the order of `not_before`, head first. Between two commands that carry one, the HA call
of the later never comes sooner after the HA call of the earlier than the gap between their `not_before` (up to
`KEPT_GAP_SECONDS`): a batch claimed late, after a reconnection, or a head held back by a slow HA call keeps its
spacing instead of running back to back. A start without `not_before` (a `power_on` of a climate or humidifier
entity) still runs at once, but it counts too: the next command that carries one never reaches HA sooner than
`START_GAP_SECONDS` after its HA call, nor sooner than the gap between its own `not_before` and that start's claim
(up to `KEPT_GAP_SECONDS`), so a late claim never sends it right behind an unstamped start.

Limits, fail closed. A `not_before` that is not an ISO 8601 string with an offset, or more than
`MAX_AHEAD_SECONDS` ahead (the server's start window: a later start would expire anyway), is refused as
`invalid_command`. A missing or null `not_before` runs at once, and a past one runs as soon as the spacing allows.
A waiting command still meets its own deadline (`expires_in_seconds`): past it, the checks refuse it as `expired`.
While `MAX_WAITING` commands wait, the loop claims nothing more. A halt drops the queue: a command never started
never runs.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

from .actions import Command, deadline_of
from .light_store import parse_instant

MAX_AHEAD_SECONDS = 120.0
KEPT_GAP_SECONDS = 60.0
# The server's gap between two compressor starts of a type, kept after a start that carried no `not_before`.
START_GAP_SECONDS = 15.0
MAX_WAITING = 50


class InvalidNotBefore(Exception):
    """A `not_before` the connector cannot read, or too far ahead."""


def not_before_of(command: Command, server_now: float) -> float | None:
    """The monotonic instant of the command's `not_before`, None without one; `server_now` is the server's time
    at `command.received_at`."""
    value = command.not_before
    if value is None:
        return None
    instant = parse_instant(value)
    if instant is None:
        raise InvalidNotBefore()
    ahead = instant - server_now
    if not math.isfinite(ahead) or ahead > MAX_AHEAD_SECONDS:
        raise InvalidNotBefore()
    return command.received_at + ahead


@dataclass(order=True)
class Waiting:
    nominal: float
    order: int
    command: Command = field(compare=False)
    # True when the command carries its own `not_before`; a follower only keeps its place behind its entity.
    spaced: bool = field(compare=False)


class StartQueue:
    """The commands waiting for their `not_before`; used by the command loop's thread alone."""

    def __init__(self) -> None:
        self._heap: list[Waiting] = []
        self._order = 0
        # (not_before, monotonic moment of the HA call) of the last spaced command that reached HA.
        self._last: tuple[float, float] | None = None
        # (monotonic claim moment, monotonic moment of the HA call) of the last start without `not_before`.
        self._unstamped: tuple[float, float] | None = None

    def __len__(self) -> int:
        return len(self._heap)

    def latest_of(self, entity_id: object) -> float | None:
        """The latest instant a command for this entity waits for, None when none waits."""
        instants = [item.nominal for item in self._heap if item.command.entity_id == entity_id]
        return max(instants) if instants else None

    def add(self, command: Command, nominal: float, spaced: bool) -> None:
        self._order += 1
        heapq.heappush(self._heap, Waiting(nominal, self._order, command, spaced))

    def due_at(self) -> float | None:
        """When the head may run (or must be refused, at its deadline); None with an empty queue."""
        if not self._heap:
            return None
        head = self._heap[0]
        deadline = deadline_of(head.command)
        if deadline is None:
            return -math.inf
        return min(self._earliest(head), deadline)

    def pop_due(self, now: float) -> Waiting | None:
        due = self.due_at()
        if due is None or due > now:
            return None
        return heapq.heappop(self._heap)

    def called(self, item: Waiting, moment: float) -> None:
        """The command's HA call went out at `moment`."""
        if item.spaced:
            self._last = (item.nominal, moment)

    def started_unstamped(self, received_at: float, moment: float) -> None:
        """A start without `not_before`, claimed at `received_at`, made its HA call at `moment`."""
        self._unstamped = (received_at, moment)

    def clear(self) -> None:
        self._heap.clear()

    def _earliest(self, item: Waiting) -> float:
        if not item.spaced:
            return item.nominal
        earliest = item.nominal
        for previous, floor in ((self._last, 0.0), (self._unstamped, START_GAP_SECONDS)):
            if previous is not None:
                last_nominal, last_call = previous
                gap = min(max(item.nominal - last_nominal, floor), KEPT_GAP_SECONDS)
                earliest = max(earliest, last_call + gap)
        return earliest
