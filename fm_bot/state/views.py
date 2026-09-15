"""Typed views over snapshot payloads.

These turn raw bridge payloads into :class:`PlayerState`, money and fixture
views with explicit statuses. Eligibility is *separately sourced*: the bridge
does not decode injury, suspension, loan absence or registration, so every
player carries an eligibility dictionary whose default status is ``missing``
until a verified provider supplies it.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .records import DecisionSnapshot, PlayerState
from .status import Observed, ValueStatus
from .units import Money, Period, parse_date
from .visibility import InformationMode, MaskedRecord, VisibilityMask, apply_mode

EligibilityProvider = Callable[[int, dict[str, Any] | None], dict[str, Any]]


def missing_eligibility(player_id: int, fixture: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"status": "missing", "verified": False, "source": None, "reasons": ["no eligibility observation source is registered"], "injury": None, "suspension": None, "loan_absence": None, "registration": None}


def readiness_observed(payload: dict[str, Any], source: str = "") -> tuple[Observed, Observed]:
    """Condition and sharpness are withheld by the bridge unless the cache is current."""
    status = (payload.get("readiness") or {}).get("status")
    if status == "current" and payload.get("condition") is not None:
        return (Observed.available_value(float(payload["condition"]), source, what="condition"), Observed.available_value(float(payload["match_sharpness"]), source, what="match_sharpness"))
    if status == "stale":
        reason = f"readiness cache dated {(payload.get('readiness') or {}).get('updated_on')} is not the current game time"
        return (Observed.unavailable(ValueStatus.STALE, "condition", reason, source), Observed.unavailable(ValueStatus.STALE, "match_sharpness", reason, source))
    return (Observed.unavailable(ValueStatus.MISSING, "condition", "readiness freshness unknown", source), Observed.unavailable(ValueStatus.MISSING, "match_sharpness", "readiness freshness unknown", source))


READINESS_FIELDS: tuple[str, ...] = ("condition", "match_sharpness")


def masked_readiness(masked: MaskedRecord, payload: dict[str, Any], source: str = "") -> tuple[Observed, Observed, str]:
    """Condition, sharpness and the readiness status, derived from the *masked* record (spec 1.2, VIS 01).

    A field the information mode removed (privileged or of unknown
    visibility, for example another club's player in manager-visible mode)
    is ``unsupported`` here and never reaches :class:`PlayerState`, however
    fresh the bridge's own readiness cache is. Only fields the mode kept are
    freshness-checked through :func:`readiness_observed`.
    """
    removed = [item for item in masked.masked if item.field in READINESS_FIELDS]
    if removed:
        reason = f"{', '.join(sorted(item.field for item in removed))} masked in {masked.mode.value} mode: {removed[0].reason}"
        return (Observed.unavailable(ValueStatus.UNSUPPORTED, "condition", reason, source), Observed.unavailable(ValueStatus.UNSUPPORTED, "match_sharpness", reason, source), "unsupported")
    visible = dict(payload)
    for name in READINESS_FIELDS:
        visible[name] = masked.fields.get(name)
    condition, sharpness = readiness_observed(visible, source)
    return condition, sharpness, (payload.get("readiness") or {}).get("status", "unknown")


def player_state(payload: dict[str, Any], *, source: str = "", eligibility: dict[str, Any] | None = None, mode: InformationMode = InformationMode.BRIDGE_OBSERVED, mask: VisibilityMask | None = None, other_club: bool = False) -> PlayerState:
    masked = apply_mode("player", payload, mode, mask, other_club=other_club)
    fields = masked.fields
    condition, sharpness, readiness_status = masked_readiness(masked, payload, source)
    attributes = dict(fields.get("attributes") or {})
    return PlayerState(
        player_id=payload["id"], name=payload.get("name", ""),
        attributes=attributes, attribute_units="fm_1_to_20" if attributes else "unavailable",
        positions=list(fields.get("positions") or []), position_ratings=dict(fields.get("position_ratings") or {}),
        employment=list(fields.get("contracts") or []),
        morale=fields.get("morale"), morale_status="available" if "morale" in fields else "unsupported",
        condition=condition.value, match_sharpness=sharpness.value,
        readiness_status=readiness_status,
        eligibility=eligibility or missing_eligibility(payload["id"]),
        age=payload.get("age"), source_observation_id=source or None,
    )


SQUAD_ROUTE_COLLECTED = "collected"
SQUAD_ROUTE_MISSING = "missing"


def _squad_payload(snapshot: DecisionSnapshot) -> tuple[list[dict[str, Any]] | None, str]:
    """The roster list and the route it came from; ``(None, reason)`` when no roster route was collected."""
    if "/squad" in snapshot.routes and snapshot.routes["/squad"] is not None:
        return list(snapshot.routes["/squad"] or []), "/squad"
    club = snapshot.routes.get("/club")
    if isinstance(club, dict) and club.get("squad") is not None:
        return list(club.get("squad") or []), "/club"
    return None, "neither /squad nor /club (with a squad list) was collected in this snapshot"


def squad_route_status(snapshot: DecisionSnapshot) -> str:
    """``collected`` when a roster route (``/squad``, else ``/club.squad``) is in the snapshot, ``missing`` otherwise (spec 5.2, OBS 02).

    :func:`squad_states` returns ``[]`` in both the *collected-but-empty* and
    the *missing* case; callers that must tell "no players observed" from
    "roster not observed" consult this or use :func:`squad_states_observed`.
    """
    squad, _ = _squad_payload(snapshot)
    return SQUAD_ROUTE_COLLECTED if squad is not None else SQUAD_ROUTE_MISSING


def squad_states_observed(snapshot: DecisionSnapshot, *, eligibility: EligibilityProvider | None = None, fixture: dict[str, Any] | None = None, mode: InformationMode | None = None, mask: VisibilityMask | None = None) -> Observed[list[PlayerState]]:
    """The roster as an :class:`Observed`: AVAILABLE (possibly empty) when a roster route was collected, MISSING when it was not.

    An uncollected route is never reported as an observed empty squad (OBS 02).
    """
    squad, route_or_reason = _squad_payload(snapshot)
    if squad is None:
        return Observed.unavailable(ValueStatus.MISSING, "squad", route_or_reason, f"{snapshot.snapshot_id}:/squad")
    mode = mode or InformationMode(snapshot.information_mode)
    provider = eligibility or missing_eligibility
    states = [player_state(p, source=f"{snapshot.snapshot_id}:/squad", eligibility=provider(p["id"], fixture), mode=mode, mask=mask) for p in squad]
    game_time = f"{snapshot.game_date} {snapshot.game_time}" if snapshot.game_date and snapshot.game_time else None
    return Observed.available_value(states, f"{snapshot.snapshot_id}:{route_or_reason}", game_time=game_time, what="squad")


def squad_states(snapshot: DecisionSnapshot, *, eligibility: EligibilityProvider | None = None, fixture: dict[str, Any] | None = None, mode: InformationMode | None = None, mask: VisibilityMask | None = None) -> list[PlayerState]:
    """Roster players as :class:`PlayerState`; ``[]`` when the roster route was not collected.

    The empty list is ambiguous on its own: check :func:`squad_route_status`
    (``missing`` versus ``collected``) or call :func:`squad_states_observed`
    before treating it as an observed empty roster.
    """
    observed = squad_states_observed(snapshot, eligibility=eligibility, fixture=fixture, mode=mode, mask=mask)
    return list(observed.value) if observed.available else []


@dataclass
class FinanceView:
    balance: Observed
    transfer_budget: Observed
    wage_budget_weekly: Observed
    payroll_spending_weekly: Observed
    as_of: str | None
    currency: str = "GBP"

    @property
    def available(self) -> bool:
        return all(x.available for x in (self.balance, self.transfer_budget, self.wage_budget_weekly, self.payroll_spending_weekly))

    def headroom_weekly(self) -> Observed:
        if not (self.wage_budget_weekly.available and self.payroll_spending_weekly.available):
            return Observed.unavailable(ValueStatus.MISSING, "wage_headroom_weekly", "wage budget or payroll unavailable")
        return Observed.available_value(self.wage_budget_weekly.value - self.payroll_spending_weekly.value, "finances", what="wage_headroom_weekly")


def finance_view(snapshot: DecisionSnapshot) -> FinanceView:
    payload = snapshot.routes.get("/finances")
    source = f"{snapshot.snapshot_id}:/finances"
    if not payload:
        absent = lambda what: Observed.unavailable(ValueStatus.MISSING, what, "/finances not collected", source)  # noqa: E731
        return FinanceView(absent("balance"), absent("transfer_budget"), absent("wage_budget_weekly"), absent("payroll_spending_weekly"), None)
    if payload.get("currency") != "GBP":
        absent = lambda what: Observed.unavailable(ValueStatus.UNSUPPORTED, what, f"currency {payload.get('currency')} is not the native GBP basis", source)  # noqa: E731
        return FinanceView(absent("balance"), absent("transfer_budget"), absent("wage_budget_weekly"), absent("payroll_spending_weekly"), payload.get("as_of"), payload.get("currency") or "?")

    def money(key: str, period: Period) -> Observed:
        value = payload.get(key)
        if value is None:
            return Observed.unavailable(ValueStatus.NULL, key, "bridge reported null", source)
        return Observed.available_value(Money.native_gbp(int(value), period), source, game_time=payload.get("as_of"), what=key)

    return FinanceView(money("balance", Period.ONCE), money("transfer_budget", Period.ONCE), money("wage_budget_weekly", Period.WEEKLY), money("payroll_spending_weekly", Period.WEEKLY), payload.get("as_of"))


@dataclass
class FixtureView:
    date: str
    time: str | None
    competition_id: int
    competition_name: str
    home_club_id: int
    away_club_id: int
    home_name: str
    away_name: str
    status: str
    home_score: int | None
    away_score: int | None
    identity: str

    @property
    def scheduled(self) -> bool:
        return self.status == "scheduled"

    def is_home(self, club_id: int) -> bool:
        return self.home_club_id == club_id

    def opponent(self, club_id: int) -> str:
        return self.away_name if self.home_club_id == club_id else self.home_name

    def to_json(self) -> dict[str, Any]:
        return {"date": self.date, "time": self.time, "competition_id": self.competition_id, "competition_name": self.competition_name, "home_club_id": self.home_club_id, "away_club_id": self.away_club_id, "home_name": self.home_name, "away_name": self.away_name, "status": self.status, "home_score": self.home_score, "away_score": self.away_score, "identity": self.identity}


def fixture_identity(item: dict[str, Any]) -> str:
    return f"{item['date']}T{item.get('time') or '??:??'} home:{item['home']['club_id']} away:{item['away']['club_id']} comp:{item['competition_id']}"


def fixture_views(snapshot: DecisionSnapshot) -> list[FixtureView]:
    payload = snapshot.routes.get("/fixtures") or {}
    items = []
    for item in payload.get("fixtures", []):
        items.append(FixtureView(item["date"], item.get("time"), item["competition_id"], item.get("competition_name", ""), item["home"]["club_id"], item["away"]["club_id"], item["home"].get("club_name", ""), item["away"].get("club_name", ""), item.get("status", "unknown"), item.get("home_score"), item.get("away_score"), fixture_identity(item)))
    items.sort(key=lambda f: (f.date, f.time or ""))
    return items


def upcoming_fixtures(snapshot: DecisionSnapshot, count: int = 6) -> list[FixtureView]:
    """Scheduled fixtures on or after the snapshot's game date, in the current calendar year only.

    The bridge exposes the current calendar year, not a complete season; a
    horizon that crosses the year boundary is explicitly truncated.
    """
    if snapshot.game_date is None:
        return []
    today = parse_date(snapshot.game_date)
    result = [f for f in fixture_views(snapshot) if f.scheduled and parse_date(f.date) >= today]
    return result[:count]


def horizon_truncated(snapshot: DecisionSnapshot, count: int = 6) -> bool:
    if snapshot.game_date is None:
        return True
    payload = snapshot.routes.get("/fixtures") or {}
    year = payload.get("calendar_year")
    future = upcoming_fixtures(snapshot, 10_000)
    if len(future) >= count:
        return False
    return year is not None and dt.date(year, 12, 31) - parse_date(snapshot.game_date) < dt.timedelta(days=60)


def tactic_view(snapshot: DecisionSnapshot) -> dict[str, Any]:
    payload = snapshot.routes.get("/tactics")
    if not payload:
        return {"status": "missing", "reason": "/tactics not collected"}
    if not payload.get("available"):
        return {"status": "unavailable", "reason": payload.get("reason")}
    slots = [{"slot": p["slot"], "position": p.get("position"), "role": p.get("role"), "duty": p.get("duty"), "player_id": p.get("player_id"), "role_status": "decoded" if p.get("role") else "not_decoded"} for p in payload.get("positions", [])]
    return {"status": "available", "name": payload.get("stored_name"), "mentality": payload.get("mentality"), "slots": slots, "substitutes": [s.get("player_id") for s in payload.get("substitutes", [])], "undecoded_roles": [s["slot"] for s in slots if s["role_status"] != "decoded"]}
