"""Backoff per endpoint and the Retry-After header."""

from __future__ import annotations

import email.utils
import random

RETRY_AFTER_CEILING_SECONDS = 3600.0


class Backoff:
    """`base` doubling up to `cap`, with ±`jitter` around the step; `reset()` after a success."""

    def __init__(self, base: float, cap: float, rng: random.Random, jitter: float = 0.2) -> None:
        self._base = base
        self._cap = cap
        self._rng = rng
        self._jitter = jitter
        self._failures = 0

    @property
    def failures(self) -> int:
        return self._failures

    def reset(self) -> None:
        self._failures = 0

    def next_delay(self, retry_after: float | None = None) -> float:
        step = min(self._cap, self._base * (2 ** min(self._failures, 32)))
        self._failures += 1
        delay = step * self._rng.uniform(1 - self._jitter, 1 + self._jitter)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return delay

    def ceiling(self) -> float:
        """The cap, at once (a 403: «that endpoint backs off to 300 s»)."""
        self._failures = max(self._failures, 32)
        return self._cap


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Seconds to wait from a Retry-After value (delta-seconds or an HTTP date), or None when unusable."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        seconds = float(int(value))
    else:
        try:
            moment = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if moment is None or moment.tzinfo is None:
            return None
        seconds = moment.timestamp() - now
    return max(0.0, min(RETRY_AFTER_CEILING_SECONDS, seconds))
