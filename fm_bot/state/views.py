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
from .visibility import InformationMode, VisibilityMask, apply_mode

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


def player_state(payload: dict[str, Any], *, source: str = "", eligibility: dict[str, Any] | None = None, mode: InformationMode = InformationMode.BRIDGE_OBSERVED, mask: VisibilityMask | None = None, other_club: bool = False) -> PlayerState:
    masked = apply_mode("player", payload, mode, mask, other_club=other_club)
    fields = masked.fields
    condition, sharpness = readiness_observed(payload, source)
    attributes = dict(fields.get("attributes") or {})
    return PlayerState(
        player_id=payload["id"], name=payload.get("name", ""),
        attributes=attributes, attribute_units="fm_1_to_20" if attributes else "unavailable",
        positions=list(fields.get("positions") or []), position_ratings=dict(fields.get("position_ratings") or {}),
        employment=list(fields.get("contracts") or []),
        morale=fields.get("morale"), morale_status="available" if "morale" in fields else "unsupported",
        condition=condition.value, match_sharpness=sharpness.value,
        readiness_status=(payload.get("readiness") or {}).get("status", "unknown"),
        eligibility=eligibility or missing_eligibility(payload["id"]),
        age=payload.get("age"), source_observation_id=source or None,
    )


def squad_states(snapshot: DecisionSnapshot, *, eligibility: EligibilityProvider | None = None, fixture: dict[str, Any] | None = None, mode: InformationMode | None = None, mask: VisibilityMask | None = None) -> list[PlayerState]:
    squad = snapshot.routes.get("/squad")
    if squad is None:
        club = snapshot.routes.get("/club") or {}
        squad = club.get("squad")
    if squad is None:
        return []
    mode = mode or InformationMode(snapshot.information_mode)
    provider = eligibility or missing_eligibility
    return [player_state(p, source=f"{snapshot.snapshot_id}:/squad", eligibility=provider(p["id"], fixture), mode=mode, mask=mask) for p in squad]


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
