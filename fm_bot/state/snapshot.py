"""Consistent snapshot protocol (spec 5.2, BOT 003, OBS 01-03).

Bridge reads are not atomic. The collector:

1. verifies ``/status`` is connected and build-supported;
2. reads ``/game`` and the identity context;
3. collects only the routes the decision needs, sequentially;
4. rereads ``/game`` and the identity anchors;
5. accepts only a compatible time, session, identity and stable
   action-critical fields (second stable read).

Two equal timestamps do not prove atomicity, so action-critical routes are
read twice and compared by payload hash. After three bounded attempts the
snapshot is invalidated and dependent work stops. Wall-clock and in-game
time are both recorded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..bridge_client.client import BridgeClient, BridgeResponse
from ..rules.capabilities import CapabilityRegistry
from .identity import Anchor, ContinuityResult, check_continuity, new_id
from .records import ConsistencyStatus, DecisionSnapshot, payload_hash
from .visibility import InformationMode

IDENTITY_ROUTES = ("/manager", "/club")


@dataclass
class SnapshotRequirements:
    routes: list[str] = field(default_factory=list)
    player_ids: list[int] = field(default_factory=list)
    action_critical: list[str] = field(default_factory=list)   # routes (or "/players/{id}") needing a second stable read
    optional: list[str] = field(default_factory=list)           # routes allowed to be unavailable without invalidating
    identity_routes: tuple[str, ...] = IDENTITY_ROUTES
    label: str = "generic"

    def to_json(self) -> dict[str, Any]:
        return {"routes": list(self.routes), "player_ids": list(self.player_ids), "action_critical": list(self.action_critical), "optional": list(self.optional), "identity_routes": list(self.identity_routes), "label": self.label}

    def all_routes(self) -> list[str]:
        return [*self.routes, *(f"/players/{pid}" for pid in self.player_ids)]


@dataclass
class CollectionContext:
    career_id: str | None
    branch_id: str | None
    information_mode: InformationMode = InformationMode.BRIDGE_OBSERVED
    previous_anchor: Anchor | None = None
    lineage_confirmed: bool = False
    bot_progressed: bool = False
    stable_point: Callable[[], bool] | None = None    # e.g. the UI adapter confirming FM is idle or paused at a decision point
    sequence: int = 0


class SnapshotCollector:
    def __init__(self, client: BridgeClient, store=None, *, attempts: int = 3):
        self.client = client
        self.store = store
        self.attempts = attempts

    # ----- helpers -----
    @staticmethod
    def _identity(responses: dict[str, BridgeResponse]) -> tuple[int | None, int | None]:
        manager = responses.get("/manager")
        club = responses.get("/club")
        manager_id = manager.data.get("id") if manager and manager.ok and isinstance(manager.data, dict) else None
        club_id = club.data.get("id") if club and club.ok and isinstance(club.data, dict) else None
        if club_id is None:
            finances = responses.get("/finances")
            if finances and finances.ok:
                club_id = finances.data.get("club_id")
        return manager_id, club_id

    def _finish(self, snapshot: DecisionSnapshot) -> DecisionSnapshot:
        if self.store is not None:
            self.store.insert_snapshot(snapshot)
        return snapshot

    # ----- protocol -----
    def collect(self, requirements: SnapshotRequirements, context: CollectionContext) -> DecisionSnapshot:
        reasons_all: list[str] = []
        last_status: ConsistencyStatus = ConsistencyStatus.ATTEMPTS_EXHAUSTED
        observation_ids: list[str] = []
        for attempt in range(1, self.attempts + 1):
            result = self._attempt(requirements, context, attempt, observation_ids)
            if result.consistency is ConsistencyStatus.CONSISTENT:
                result.attempts = attempt
                return self._finish(result)
            reasons_all.extend(f"attempt {attempt}: {r}" for r in result.consistency_reasons)
            last_status = result.consistency
            # Disconnection and unsupported builds do not improve by retrying inside one collection.
            continuity_failed = bool(result.continuity and result.continuity.get("invalidates_snapshot"))
            if continuity_failed or result.consistency in (ConsistencyStatus.DISCONNECTED, ConsistencyStatus.BUILD_UNSUPPORTED, ConsistencyStatus.IDENTITY_CHANGED):
                result.attempts = attempt
                result.consistency_reasons = reasons_all
                return self._finish(result)
        snapshot = DecisionSnapshot(new_id("snap"), observation_ids, ConsistencyStatus.ATTEMPTS_EXHAUSTED, [f"no consistent snapshot after {self.attempts} attempts; last status {last_status.value}", *reasons_all], [], [], {}, context.information_mode.value, context.career_id, context.branch_id, None, None, None, requirements=requirements.to_json(), attempts=self.attempts)
        return self._finish(snapshot)

    def _attempt(self, requirements: SnapshotRequirements, context: CollectionContext, attempt: int, observation_ids: list[str]) -> DecisionSnapshot:
        def invalid(status: ConsistencyStatus, reasons: list[str], **extra) -> DecisionSnapshot:
            snap = DecisionSnapshot(new_id("snap"), list(observation_ids), status, reasons, extra.get("capabilities", []), extra.get("unresolved", []), extra.get("entity_versions", {}), context.information_mode.value, context.career_id, context.branch_id, extra.get("session_id"), extra.get("game_date"), extra.get("game_time"), routes=extra.get("routes", {}), requirements=requirements.to_json(), attempts=attempt, manager_id=extra.get("manager_id"), club_id=extra.get("club_id"))
            return snap

        # 1. connection and build
        state = self.client.connection_state()
        if state.get("observation_id"):
            observation_ids.append(state["observation_id"])
        if not state["connected"]:
            return invalid(ConsistencyStatus.DISCONNECTED, [state.get("reason") or "bridge disconnected"])
        if not state["build_supported"]:
            return invalid(ConsistencyStatus.BUILD_UNSUPPORTED, [state.get("reason") or "unsupported build"])
        session_id = state["session_id"]
        capabilities, unresolved = state["capabilities"], state["unresolved"]
        if context.stable_point is not None and not context.stable_point():
            return invalid(ConsistencyStatus.FIELD_CHANGED, ["FM is not at a validated idle or paused decision point"], session_id=session_id)

        # 2. game time and identity context
        game = self.client.game()
        if game.observation_id:
            observation_ids.append(game.observation_id)
        if not game.ok:
            return invalid(ConsistencyStatus.ROUTE_UNAVAILABLE, [f"/game unavailable: {game.error}"], session_id=session_id)
        game_date, game_time = game.data.get("date"), game.data.get("time")
        responses: dict[str, BridgeResponse] = {}
        for route in requirements.identity_routes:
            response = self.client.get(route, game_date=game_date, game_time=game_time)
            if response.observation_id:
                observation_ids.append(response.observation_id)
            if not response.ok:
                return invalid(ConsistencyStatus.ROUTE_UNAVAILABLE, [f"{route} unavailable: {response.error}"], session_id=session_id, game_date=game_date, game_time=game_time)
            responses[route] = response
        manager_id, club_id = self._identity(responses)
        sessions = {game.session_id, *(r.session_id for r in responses.values())}
        if len(sessions - {None}) > 1 or (session_id and session_id not in sessions):
            return invalid(ConsistencyStatus.SESSION_CHANGED, [f"session ids differ during identity reads: {sorted(s for s in sessions if s)} vs status {session_id}"], session_id=session_id, game_date=game_date, game_time=game_time)

        # 3. required routes, sequentially
        unavailable_optional: list[str] = []
        for route in requirements.all_routes():
            if route in responses:
                continue
            response = self.client.get(route, game_date=game_date, game_time=game_time)
            if response.observation_id:
                observation_ids.append(response.observation_id)
            if not response.ok:
                if route in requirements.optional:
                    unavailable_optional.append(route)
                    responses[route] = response
                    continue
                return invalid(ConsistencyStatus.ROUTE_UNAVAILABLE, [f"{route} unavailable: {response.error}"], session_id=session_id, game_date=game_date, game_time=game_time, manager_id=manager_id, club_id=club_id)
            if response.session_id != session_id:
                return invalid(ConsistencyStatus.SESSION_CHANGED, [f"{route} came from session {response.session_id}, expected {session_id}"], session_id=session_id, game_date=game_date, game_time=game_time)
            responses[route] = response

        # 4. reread anchors
        game_again = self.client.game()
        if game_again.observation_id:
            observation_ids.append(game_again.observation_id)
        if not game_again.ok:
            return invalid(ConsistencyStatus.ROUTE_UNAVAILABLE, [f"/game reread unavailable: {game_again.error}"], session_id=session_id, game_date=game_date, game_time=game_time)
        if (game_again.data.get("date"), game_again.data.get("time")) != (game_date, game_time):
            return invalid(ConsistencyStatus.TIME_CHANGED, [f"game time moved {game_date} {game_time} -> {game_again.data.get('date')} {game_again.data.get('time')} during collection"], session_id=session_id, game_date=game_date, game_time=game_time)
        if game_again.session_id != session_id:
            return invalid(ConsistencyStatus.SESSION_CHANGED, [f"session changed during collection: {session_id} -> {game_again.session_id}"], session_id=session_id, game_date=game_date, game_time=game_time)
        identity_again: dict[str, BridgeResponse] = {}
        for route in requirements.identity_routes:
            response = self.client.get(route, game_date=game_date, game_time=game_time)
            if response.observation_id:
                observation_ids.append(response.observation_id)
            if not response.ok:
                return invalid(ConsistencyStatus.ROUTE_UNAVAILABLE, [f"{route} reread unavailable: {response.error}"], session_id=session_id, game_date=game_date, game_time=game_time)
            identity_again[route] = response
        manager_again, club_again = self._identity(identity_again)
        if (manager_again, club_again) != (manager_id, club_id):
            return invalid(ConsistencyStatus.IDENTITY_CHANGED, [f"manager/club changed during collection: {manager_id}/{club_id} -> {manager_again}/{club_again}"], session_id=session_id, game_date=game_date, game_time=game_time, manager_id=manager_id, club_id=club_id)

        # 5. action-critical second stable read
        entity_versions = {route: payload_hash(r.data) for route, r in responses.items() if r.ok}
        for route in requirements.action_critical:
            if route not in responses or not responses[route].ok:
                return invalid(ConsistencyStatus.ROUTE_UNAVAILABLE, [f"action-critical route {route} was not collected"], session_id=session_id, game_date=game_date, game_time=game_time)
            again = self.client.get(route, game_date=game_date, game_time=game_time)
            if again.observation_id:
                observation_ids.append(again.observation_id)
            if not again.ok or payload_hash(again.data) != entity_versions[route]:
                return invalid(ConsistencyStatus.FIELD_CHANGED, [f"action-critical route {route} changed between reads (same-tick update or unavailable)"], session_id=session_id, game_date=game_date, game_time=game_time, manager_id=manager_id, club_id=club_id)

        # continuity against the previous anchor
        context.sequence += 1
        anchor = Anchor(context.career_id, context.branch_id, session_id, state.get("build"), manager_id, club_id, game_date, game_time, context.sequence)
        continuity: ContinuityResult = check_continuity(context.previous_anchor, anchor, lineage_confirmed=context.lineage_confirmed, bot_progressed=context.bot_progressed)
        routes = {route: r.data for route, r in responses.items() if r.ok}
        snapshot = DecisionSnapshot(new_id("snap"), list(observation_ids), ConsistencyStatus.CONSISTENT, [], capabilities, unresolved, entity_versions, context.information_mode.value, context.career_id, context.branch_id, session_id, game_date, game_time, routes=routes, attempts=attempt, continuity=continuity.to_json(), requirements=requirements.to_json(), manager_id=manager_id, club_id=club_id)
        if unavailable_optional:
            snapshot.consistency_reasons = [f"optional routes unavailable: {unavailable_optional}"]
        if continuity.invalidates_snapshot:
            snapshot.consistency = ConsistencyStatus.IDENTITY_CHANGED if continuity.stops_for_identity_resolution else ConsistencyStatus.SESSION_CHANGED
            snapshot.consistency_reasons = [f"continuity {continuity.status.value}: " + "; ".join(continuity.reasons)]
        snapshot.anchor = anchor  # type: ignore[attr-defined]
        return snapshot

    def capability_registry(self, snapshot: DecisionSnapshot) -> CapabilityRegistry:
        return CapabilityRegistry.from_status({"connected": snapshot.valid, "build": self.client.last_status.get("build") if self.client.last_status else None, "capabilities": snapshot.capabilities, "unresolved": snapshot.unresolved}, supported_builds=self.client.supported_builds)


def anchor_of(snapshot: DecisionSnapshot) -> Anchor | None:
    anchor = getattr(snapshot, "anchor", None)
    if anchor is not None:
        return anchor
    if snapshot.session_id is None:
        return None
    return Anchor(snapshot.career_id, snapshot.branch_id, snapshot.session_id, None, snapshot.manager_id, snapshot.club_id, snapshot.game_date, snapshot.game_time)
