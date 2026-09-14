"""HTTP transport for the loopback bridge and a scripted fake for tests.

The bridge only accepts GET on ``127.0.0.1:8765`` with a matching Host header
and no query string. The fake transport replays recorded responses and can
script per-call sequences so tests can inject time, session and identity
changes between reads.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class TransportError(RuntimeError):
    """The bridge could not be reached or returned a non-JSON body."""


@dataclass
class RawResponse:
    status: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: float = 0.0


class Transport:
    def get(self, path: str) -> RawResponse:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        return None


class HttpTransport(Transport):
    def __init__(self, base_url: str = "http://127.0.0.1:8765", timeout: float = 10.0):
        parts = urlsplit(base_url)
        if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("the bridge is loopback-only; refusing a non-local base URL")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._authority = parts.netloc

    def get(self, path: str) -> RawResponse:
        if "?" in path or "#" in path:
            raise ValueError("bridge routes take no query string")
        request = Request(self.base_url + path, method="GET", headers={"Host": self._authority, "Accept": "application/json"})
        started = time.monotonic()
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                status = response.status
                headers = dict(response.headers.items())
        except HTTPError as exc:
            raw = exc.read()
            status = exc.code
            headers = dict(exc.headers.items()) if exc.headers else {}
        except (URLError, OSError, TimeoutError) as exc:
            raise TransportError(f"bridge unreachable at {self.base_url}{path}: {exc}") from exc
        elapsed = (time.monotonic() - started) * 1000
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError(f"bridge returned a non-JSON body for {path} (HTTP {status})") from exc
        return RawResponse(status, body, headers, elapsed)


ResponseSpec = RawResponse | tuple[int, Any] | Callable[[], "RawResponse | tuple[int, Any]"] | Exception


class FakeTransport(Transport):
    """Scripted responses per route.

    ``routes`` maps a path to either a single response or a list of responses
    consumed in order (the last one repeats). A response may be a
    ``RawResponse``, a ``(status, body)`` tuple, a zero-argument callable or an
    exception instance to raise. ``calls`` records every request in order.
    """

    def __init__(self, routes: dict[str, ResponseSpec | list[ResponseSpec]] | None = None):
        self.routes: dict[str, list[ResponseSpec]] = {}
        self.calls: list[str] = []
        for path, spec in (routes or {}).items():
            self.set(path, spec)

    def set(self, path: str, spec: ResponseSpec | list[ResponseSpec]) -> None:
        self.routes[path] = list(spec) if isinstance(spec, list) else [spec]

    def get(self, path: str) -> RawResponse:
        self.calls.append(path)
        sequence = self.routes.get(path)
        if not sequence:
            return RawResponse(404, {"error": "not_found"})
        spec = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        if callable(spec) and not isinstance(spec, Exception):
            spec = spec()
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, RawResponse):
            return spec
        status, body = spec
        return RawResponse(status, json.loads(json.dumps(body)))

    @staticmethod
    def envelope(data: Any, session_id: str = "session-1", observed_at: str = "2026-09-14T12:00:00+00:00") -> tuple[int, Any]:
        return 200, {"observed_at": observed_at, "session_id": session_id, "data": data}
