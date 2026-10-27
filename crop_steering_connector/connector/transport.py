"""HTTP through `urllib.request`: no redirects, no proxies, the default verifying TLS context, bounded bodies."""

from __future__ import annotations

import http.client
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping, Protocol

CONNECT = "connect"
RESPONSE = "response"


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


class TransportError(Exception):
    """A request without an HTTP answer.

    `phase` is CONNECT when the request was never sent (no connection, TLS refused, invalid request) and
    RESPONSE when it was sent and no complete answer came back (timeout, reset, oversized body). For a call to
    Home Assistant that is the difference between `ha_unreachable` and `ha_error`.
    """

    def __init__(self, phase: str, kind: str) -> None:
        super().__init__(f"{phase}:{kind}")
        self.phase = phase
        self.kind = kind


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        max_bytes: int,
    ) -> Response: ...


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Every 3xx comes back as an HTTP error of its own endpoint and is never followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _read_bounded(stream, max_bytes: int) -> bytes:  # noqa: ANN001
    data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise TransportError(RESPONSE, "too_large")
    return data


def _headers_of(message) -> dict[str, str]:  # noqa: ANN001
    if message is None:
        return {}
    return {key.lower(): value for key, value in message.items()}


class UrllibTransport:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            _RefuseRedirects(),
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        max_bytes: int,
    ) -> Response:
        try:
            request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        except ValueError as error:
            raise TransportError(CONNECT, "invalid_request") from error
        try:
            with self._opener.open(request, timeout=timeout) as answer:
                return Response(answer.status, _read_bounded(answer, max_bytes), _headers_of(answer.headers))
        except urllib.error.HTTPError as error:
            try:
                data = _read_bounded(error, max_bytes) if error.fp is not None else b""
            except (OSError, http.client.HTTPException):
                data = b""
            finally:
                error.close()
            return Response(error.code, data, _headers_of(error.headers))
        except urllib.error.URLError as error:
            # urllib wraps only what fails while connecting and sending the request.
            kind = "timeout" if isinstance(error.reason, TimeoutError) else "network"
            if isinstance(error.reason, ssl.SSLError):
                kind = "tls"
            raise TransportError(CONNECT, kind) from None
        except TimeoutError:
            raise TransportError(RESPONSE, "timeout") from None
        except ValueError:
            # http.client refuses an invalid header before anything is sent.
            raise TransportError(CONNECT, "invalid_request") from None
        except (OSError, http.client.HTTPException):
            raise TransportError(RESPONSE, "network") from None
