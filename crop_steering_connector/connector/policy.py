"""What the connector does with each refusal of the Cropilot API.

The rows and the action names are those of the server's error contract, and a test checks every row of it
against `classify()`.
"""

from __future__ import annotations

PAIR = "POST /api/v1/home-assistant/pair"
REPORTS = "POST /api/v1/home-assistant/reports"
CLAIM = "POST /api/v1/home-assistant/commands/claim"
START = "POST /api/v1/home-assistant/commands/{id}/start"
RESULT = "POST /api/v1/home-assistant/commands/{id}/result"
ANY = "*"

STOP = "stop"
BACKOFF_300 = "backoff_300"
RETRY_AFTER = "retry_after"
ALLOWLISTED_ONLY = "allowlisted_only"
NO_HA_CALL = "no_ha_call"
FINAL = "final"
RETRY = "retry"
BACKOFF = "backoff"

ENDPOINT_NAMES = {
    PAIR: "pareamento",
    REPORTS: "relatório",
    CLAIM: "busca de comandos",
    START: "início do comando",
    RESULT: "resultado do comando",
}

# (endpoint, status, code) -> action; code None matches any code of that status.
_RULES: dict[tuple[str, int, str | None], str] = {
    (ANY, 401, None): STOP,
    (ANY, 403, None): BACKOFF_300,
    (ANY, 429, None): RETRY_AFTER,
    (PAIR, 422, None): STOP,
    (REPORTS, 413, None): ALLOWLISTED_ONLY,
    (REPORTS, 422, "too_many_entities"): ALLOWLISTED_ONLY,
    (START, 404, None): NO_HA_CALL,
    (START, 409, None): NO_HA_CALL,
    (START, 413, None): NO_HA_CALL,
    (START, 422, None): NO_HA_CALL,
    (RESULT, 403, None): RETRY,
    (RESULT, 404, None): FINAL,
    (RESULT, 409, None): FINAL,
    (RESULT, 413, None): FINAL,
    (RESULT, 422, None): FINAL,
}


def classify(endpoint: str, status: int, code: str | None) -> str:
    """The action for a non-success answer; the most specific rule wins (endpoint first, then code)."""
    for key in ((endpoint, status, code), (endpoint, status, None), (ANY, status, code), (ANY, status, None)):
        if key in _RULES:
            return _RULES[key]
    if endpoint == RESULT:
        # A started command's result is never dropped: what is not final is retried.
        return RETRY
    if endpoint == START:
        return NO_HA_CALL
    return BACKOFF


def is_transient(status: int) -> bool:
    """A 3xx (never followed), a 5xx or any status the contract does not name backs the endpoint off."""
    return status < 400 or status >= 500
