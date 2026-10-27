"""Options of the app (`/data/options.json`) and their validation at start."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_SERVER_URL = "https://app.cropsteering.ai"
PRODUCTION_HOST = "app.cropsteering.ai"
API_PREFIX = "/api/v1/home-assistant"

# The pairing code: "{id}|", the "cs_" prefix, 40 letters or digits and their crc32b in 8 hex digits.
PAIRING_CODE_RE = re.compile(r"^[0-9]{1,20}\|cs_[A-Za-z0-9]{40}[0-9a-f]{8}$")
# The connector token comes from the server; the check only keeps it a safe header value.
CONNECTOR_TOKEN_RE = re.compile(r"^[0-9]{1,20}\|[A-Za-z0-9_]{1,200}$")

_PRIVATE_NETWORKS = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)
_ALLOWED_URL_CHARS = re.compile(r"^[A-Za-z0-9:/._\-]+$")


@dataclass(frozen=True)
class Options:
    pairing_code: object
    server_url: object


def load_options(path: Path) -> Options:
    """Read the options the Supervisor wrote; a missing or unreadable file means no option set."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return Options(pairing_code=raw.get("pairing_code"), server_url=raw.get("server_url"))


def resolve_server_url(value: object) -> str | None:
    """Return the base URL (scheme and host, no trailing slash), or None when the value is refused.

    Unset means production. Otherwise only https://app.cropsteering.ai (port 443 at most) or http:// with a
    private IPv4 host and an explicit port, for the local validation; never userinfo, a path, a query or a
    fragment. The Supervisor's match() checked the same, and this runs again whatever it checked.
    """
    if value is None or value == "":
        return DEFAULT_SERVER_URL
    if not isinstance(value, str) or len(value) > 200 or not _ALLOWED_URL_CHARS.fullmatch(value):
        return None
    if value.startswith("https://"):
        scheme = "https"
    elif value.startswith("http://"):
        scheme = "http"
    else:
        return None
    parts = urlsplit(value)
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        return None
    netloc = parts.netloc
    if not netloc or "@" in netloc or netloc != netloc.lower():
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname or ""
    expected_netloc = host if port is None else f"{host}:{port}"
    if netloc != expected_netloc:
        return None
    if scheme == "https":
        if host != PRODUCTION_HOST or port not in (None, 443):
            return None
        return DEFAULT_SERVER_URL
    if port is None or not 1 <= port <= 65535:
        return None
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        return None
    if not any(address in network for network in _PRIVATE_NETWORKS):
        return None
    return f"http://{host}:{port}"


def pairing_code_state(value: object) -> str:
    """'absent', 'valid' or 'malformed'."""
    if value is None or value == "":
        return "absent"
    if isinstance(value, str) and PAIRING_CODE_RE.fullmatch(value):
        return "valid"
    return "malformed"


def is_connector_token(value: object) -> bool:
    return isinstance(value, str) and CONNECTOR_TOKEN_RE.fullmatch(value) is not None
