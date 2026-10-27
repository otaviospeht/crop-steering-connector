"""The two HTTP peers: the Cropilot API and the HA Core API behind the Supervisor proxy.

Both refuse to send once the connector is halted, so after a stop line (a revoked pairing, a refused pairing
code, a refused server address or a missing code) no call leaves the process.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any

from . import USER_AGENT
from .settings import API_PREFIX
from .transport import Response, Transport

TIMEOUT_SECONDS = 10.0
SERVER_MAX_BYTES = 1 << 20
HA_MAX_BYTES = 16 << 20
SUPERVISOR_CORE_API = "http://supervisor/core/api"
_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")


class Halted(Exception):
    """The connector stopped; no further call may leave."""


def json_body(response: Response) -> Any:
    try:
        return json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def error_code(response: Response) -> str | None:
    """The `code` of an error body, when it is a plain code; the `message` is never read."""
    payload = json_body(response)
    if isinstance(payload, dict):
        code = payload.get("code")
        if isinstance(code, str) and _CODE_RE.fullmatch(code):
            return code
    return None


def is_success(response: Response) -> bool:
    """A 2xx of the Cropilot API; the body is validated by each caller."""
    return 200 <= response.status < 300


def describe(response: Response) -> str:
    code = error_code(response)
    return f"HTTP {response.status}" + (f" {code}" if code else "")


def _encode(payload: dict | None) -> bytes:
    if payload is None:
        return b""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


class ServerClient:
    def __init__(self, transport: Transport, base_url: str, run_id: str, halt: threading.Event) -> None:
        self._transport = transport
        self._base = base_url + API_PREFIX
        self._run_id = run_id
        self._halt = halt

    def post(self, path: str, bearer: str, payload: dict | None = None, body: bytes | None = None) -> Response:
        if self._halt.is_set():
            raise Halted()
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-Connector-Run": self._run_id,
        }
        data = body if body is not None else _encode(payload)
        return self._transport.request("POST", self._base + path, headers, data, TIMEOUT_SECONDS, SERVER_MAX_BYTES)


class HomeAssistantClient:
    def __init__(self, transport: Transport, supervisor_token: str, halt: threading.Event) -> None:
        self._transport = transport
        self._token = supervisor_token
        self._halt = halt

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def get(self, path: str) -> Response:
        if self._halt.is_set():
            raise Halted()
        return self._transport.request(
            "GET", SUPERVISOR_CORE_API + path, self._headers(), None, TIMEOUT_SECONDS, HA_MAX_BYTES
        )

    def post(self, path: str, payload: dict) -> Response:
        if self._halt.is_set():
            raise Halted()
        return self._transport.request(
            "POST", SUPERVISOR_CORE_API + path, self._headers(), _encode(payload), TIMEOUT_SECONDS, SERVER_MAX_BYTES
        )
