"""Typed bridge client that records schema version, errors and source payloads (BOT 001).

Every read returns a :class:`BridgeResponse` and, when a store is attached,
journals an :class:`Observation` with the raw payload hash. HTTP 200 on
``/status`` can still mean disconnected; ``connected`` is checked explicitly.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .. import SUPPORTED_BUILD
from ..state.identity import utc_now
from ..state.records import Observation, QualityStatus, Visibility, payload_hash
from .schemas import SCHEMA_VERSION, validate_payload
from .transport import HttpTransport, RawResponse, Transport, TransportError


class BridgeUnavailable(RuntimeError):
    def __init__(self, route: str, reason: str, http_status: int | None = None):
        self.route = route
        self.reason = reason
        self.http_status = http_status
        super().__init__(f"{route}: {reason}")


@dataclass
class BridgeResponse:
    route: str
    http_status: int | None
    ok: bool
    data: Any                              # the ``data`` member of a successful envelope, or the /status body
    session_id: str | None
    observed_at: str | None                # bridge-reported UTC collection time
    received_at: str                       # bot wall clock
    payload_hash: str
    schema_version: str = SCHEMA_VERSION
    schema_problems: list[str] = field(default_factory=list)
    error: str | None = None
    observation_id: str | None = None
    elapsed_ms: float = 0.0

    @property
    def quality(self) -> QualityStatus:
        if not self.ok:
            return QualityStatus.UNAVAILABLE if self.http_status in (503, 404) else QualityStatus.ERROR
        return QualityStatus.SCHEMA_MISMATCH if self.schema_problems else QualityStatus.VALIDATED

    def require(self) -> Any:
        if not self.ok:
            raise BridgeUnavailable(self.route, self.error or "unavailable", self.http_status)
        return self.data


class BridgeClient:
    """Reads the bridge routes; never writes. All results are recorded when a store is attached."""

    def __init__(self, transport: Transport | None = None, store=None, *, supported_builds: tuple[str, ...] = (SUPPORTED_BUILD,), context: dict[str, Any] | None = None):
        self.transport = transport or HttpTransport()
        self.store = store
        self.supported_builds = tuple(supported_builds)
        self.context = dict(context or {})      # career_id / branch_id used for journaling
        self.errors: list[dict[str, Any]] = []
        self.sequence = 0
        self.last_status: dict[str, Any] | None = None

    # ----- context -----
    def set_context(self, *, career_id: str | None = None, branch_id: str | None = None) -> None:
        self.context = {"career_id": career_id, "branch_id": branch_id}

    # ----- low-level -----
    def get(self, route: str, *, game_date: str | None = None, game_time: str | None = None) -> BridgeResponse:
        self.sequence += 1
        started = time.monotonic()
        try:
            raw: RawResponse = self.transport.get(route)
        except TransportError as exc:
            response = BridgeResponse(route, None, False, None, None, None, utc_now(), payload_hash(None), error=str(exc), elapsed_ms=(time.monotonic() - started) * 1000)
            self._record(response, game_date, game_time)
            return response
        elapsed = (time.monotonic() - started) * 1000
        body = raw.body
        if route == "/status":
            ok = raw.status == 200 and isinstance(body, dict)
            problems = validate_payload(route, body) if ok else []
            response = BridgeResponse(route, raw.status, ok, body if ok else None, body.get("session_id") if isinstance(body, dict) else None, None, utc_now(), payload_hash(body), SCHEMA_VERSION, problems, None if ok else f"HTTP {raw.status}: {body}", elapsed_ms=elapsed)
            if ok:
                self.last_status = body
        elif raw.status == 200 and isinstance(body, dict) and "data" in body:
            problems = validate_payload(route, body["data"])
            response = BridgeResponse(route, 200, True, body["data"], body.get("session_id"), body.get("observed_at"), utc_now(), payload_hash(body), SCHEMA_VERSION, problems, elapsed_ms=elapsed)
        else:
            message = body.get("message") or body.get("error") if isinstance(body, dict) else str(body)
            response = BridgeResponse(route, raw.status, False, None, None, None, utc_now(), payload_hash(body), error=f"HTTP {raw.status}: {message}", elapsed_ms=elapsed)
        if not response.ok or response.schema_problems:
            self.errors.append({"route": route, "at": response.received_at, "http_status": response.http_status, "error": response.error, "schema_problems": list(response.schema_problems)})
        self._record(response, game_date, game_time)
        return response

    def _record(self, response: BridgeResponse, game_date: str | None, game_time: str | None) -> None:
        if self.store is None:
            return
        payload = {"route": response.route, "http_status": response.http_status, "data": response.data, "session_id": response.session_id, "observed_at": response.observed_at, "error": response.error}
        observation = Observation.create(
            f"bridge:{response.route}", payload,
            career_id=self.context.get("career_id"), branch_id=self.context.get("branch_id"),
            session_id=response.session_id, game_date=game_date, game_time=game_time,
            schema_version=response.schema_version, visibility=Visibility.PRIVILEGED,
            quality=response.quality, http_status=response.http_status, error=response.error, sequence=self.sequence,
        )
        self.store.insert_observation(observation)
        response.observation_id = observation.observation_id

    # ----- capability checks -----
    def status(self) -> BridgeResponse:
        return self.get("/status")

    def connection_state(self) -> dict[str, Any]:
        """Explicit connection/build check. HTTP 200 alone never means connected."""
        response = self.status()
        if not response.ok:
            return {"connected": False, "build_supported": False, "reason": response.error, "session_id": None, "status": None}
        body = response.data
        connected = body.get("connected") is True
        build = body.get("build")
        build_supported = build in self.supported_builds
        reason = None
        if not connected:
            reason = body.get("reason") or "bridge reports connected=false"
        elif not build_supported:
            reason = f"build {build!r} is not in the supported list {list(self.supported_builds)}"
        return {"connected": connected, "build_supported": build_supported, "build": build, "reason": reason, "session_id": body.get("session_id"), "game_date": body.get("game_date"), "capabilities": list(body.get("capabilities", [])), "unresolved": list(body.get("unresolved", [])), "status": body, "observation_id": response.observation_id}

    # ----- typed routes -----
    def game(self, **kw) -> BridgeResponse: return self.get("/game", **kw)
    def manager(self, **kw) -> BridgeResponse: return self.get("/manager", **kw)
    def club(self, **kw) -> BridgeResponse: return self.get("/club", **kw)
    def squad(self, **kw) -> BridgeResponse: return self.get("/squad", **kw)
    def finances(self, **kw) -> BridgeResponse: return self.get("/finances", **kw)
    def fixtures(self, **kw) -> BridgeResponse: return self.get("/fixtures", **kw)
    def staff(self, **kw) -> BridgeResponse: return self.get("/staff", **kw)
    def tactics(self, **kw) -> BridgeResponse: return self.get("/tactics", **kw)
    def inbox(self, **kw) -> BridgeResponse: return self.get("/inbox", **kw)
    def training(self, **kw) -> BridgeResponse: return self.get("/training", **kw)
    def scouting(self, **kw) -> BridgeResponse: return self.get("/scouting", **kw)
    def shortlists(self, **kw) -> BridgeResponse: return self.get("/shortlists", **kw)
    def transfer_targets(self, **kw) -> BridgeResponse: return self.get("/transfer-targets", **kw)
    def match(self, **kw) -> BridgeResponse: return self.get("/match", **kw)

    def player(self, player_id: int, **kw) -> BridgeResponse:
        if isinstance(player_id, bool) or not isinstance(player_id, int) or player_id <= 0:
            raise ValueError("player_id must be a positive int")
        return self.get(f"/players/{player_id}", **kw)

    def players(self, player_ids, **kw) -> dict[int, BridgeResponse]:
        """Bulk per-player reads; a failure for one player never aborts the others."""
        return {pid: self.player(pid, **kw) for pid in player_ids}

    def close(self) -> None:
        self.transport.close()
