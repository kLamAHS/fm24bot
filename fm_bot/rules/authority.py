"""Authority profile and authorization (spec 1.2, BOT 006).

Information mode and action authority are independent settings. A profile is
configured once and persists until changed; actions inside it do not need
repeated confirmation. Outside-profile actions are reported with exact terms
and consequences. An unknown prerequisite blocks the dependent action even if
the action family is authorised.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

from ..state.records import ActionIntent
from ..state.status import MissingCapabilityReport
from ..state.units import Money, Period


class AuthorityMode(str, Enum):
    OBSERVE = "observe"
    ADVISE = "advise"
    SCOPED_EXECUTION = "scoped_execution"
    CLUB_AUTONOMY = "club_autonomy"


# Action families. An action's authority_scope is "family.action".
ACTION_FAMILIES = ["tactics", "selection", "training", "transfers", "contracts", "loans", "staff", "board", "scouting", "inbox", "conversations", "media", "registration", "progression", "match", "laboratory"]

# The validated club workflow used by club autonomy. Laboratory operations are never part of it.
CLUB_WORKFLOW_FAMILIES = [f for f in ACTION_FAMILIES if f != "laboratory"]


@dataclass
class AuthorityLimits:
    """Money limits are one-off or weekly commitments the bot may add without confirmation."""

    max_weekly_wage_commitment: Money | None = None       # Period.WEEKLY, per new/renewed contract
    max_total_fee_commitment: Money | None = None         # Period.ONCE, per deal, all guaranteed instalments
    max_total_conditional_commitment: Money | None = None
    min_cash_reserve: Money | None = None                 # Period.ONCE
    max_contract_years: int | None = None
    allow_player_release: bool = False
    allow_player_sale: bool = False
    allow_continue: bool = False

    def to_json(self) -> dict[str, Any]:
        data = {}
        for key, value in asdict(self).items():
            data[key] = value
        for key in ("max_weekly_wage_commitment", "max_total_fee_commitment", "max_total_conditional_commitment", "min_cash_reserve"):
            money = getattr(self, key)
            data[key] = money.to_json() if money else None
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "AuthorityLimits":
        kwargs = dict(data)
        for key in ("max_weekly_wage_commitment", "max_total_fee_commitment", "max_total_conditional_commitment", "min_cash_reserve"):
            if kwargs.get(key) is not None:
                kwargs[key] = Money.from_json(kwargs[key])
        return cls(**kwargs)


@dataclass
class AuthorityProfile:
    mode: AuthorityMode = AuthorityMode.ADVISE
    enabled_families: set[str] = field(default_factory=set)
    limits: AuthorityLimits = field(default_factory=AuthorityLimits)
    version: int = 1
    delegation: dict[str, str] = field(default_factory=dict)   # optional in-game delegation policy per family, e.g. {"media": "assistant"}
    label: str = "default"

    def to_json(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "enabled_families": sorted(self.enabled_families), "limits": self.limits.to_json(), "version": self.version, "delegation": dict(self.delegation), "label": self.label}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "AuthorityProfile":
        return cls(AuthorityMode(data["mode"]), set(data.get("enabled_families", [])), AuthorityLimits.from_json(data.get("limits", {})), int(data.get("version", 1)), dict(data.get("delegation", {})), data.get("label", "default"))

    def bump(self) -> "AuthorityProfile":
        self.version += 1
        return self


@dataclass
class Allowed:
    scope: str
    profile_version: int
    notes: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return True


@dataclass
class OutsideScope:
    scope: str
    profile_version: int
    reasons: list[str]
    exact_terms: dict[str, Any] = field(default_factory=dict)   # shown to the operator with consequences
    missing_capabilities: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return False


def _money(value: Any) -> Money | None:
    if value is None:
        return None
    if isinstance(value, Money):
        return value
    if isinstance(value, dict):
        return Money.from_json(value)
    raise TypeError("money terms must be Money or its JSON form")


def authorize(intent: ActionIntent, profile: AuthorityProfile, capability_report: MissingCapabilityReport | None = None) -> Allowed | OutsideScope:
    """Decide whether ``intent`` is inside the persistent authority profile.

    Numeric limits are enforced here, outside any model. ``intent.parameters``
    may carry ``weekly_wage``, ``total_fee``, ``conditional_total`` (Money JSON)
    and ``contract_years`` which are compared with the profile limits.
    """
    scope = intent.authority_scope
    family = scope.split(".")[0]
    reasons: list[str] = []
    terms: dict[str, Any] = {}
    if capability_report is not None and capability_report.blocked:
        return OutsideScope(scope, profile.version, [f"unknown prerequisite blocks {intent.kind}: missing {', '.join(capability_report.missing)}"], {}, list(capability_report.missing))
    if profile.mode is AuthorityMode.OBSERVE:
        reasons.append("authority mode is observe: no game actions")
    elif profile.mode is AuthorityMode.ADVISE:
        reasons.append("authority mode is advise: recommendations only")
    elif profile.mode is AuthorityMode.SCOPED_EXECUTION:
        if family not in profile.enabled_families:
            reasons.append(f"action family {family!r} is not enabled in the scoped execution profile")
    elif profile.mode is AuthorityMode.CLUB_AUTONOMY:
        if family == "laboratory":
            reasons.append("laboratory operations are never part of the club autonomy workflow")
        elif family not in profile.enabled_families and family not in CLUB_WORKFLOW_FAMILIES:
            reasons.append(f"action family {family!r} is outside the validated club workflow")
    limits = profile.limits
    params = intent.parameters or {}
    wage = _money(params.get("weekly_wage"))
    if wage is not None:
        terms["weekly_wage"] = str(wage)
        if wage.period is not Period.WEEKLY:
            reasons.append("weekly_wage must carry a weekly period")
        elif limits.max_weekly_wage_commitment is None:
            reasons.append("no weekly wage limit is configured; a wage commitment requires an explicit limit")
        elif wage > limits.max_weekly_wage_commitment:
            reasons.append(f"weekly wage {wage} exceeds the profile limit {limits.max_weekly_wage_commitment}")
    fee = _money(params.get("total_fee"))
    if fee is not None:
        terms["total_fee"] = str(fee)
        if fee.period is not Period.ONCE:
            reasons.append("total_fee must be a one-off total of all guaranteed instalments")
        elif limits.max_total_fee_commitment is None:
            reasons.append("no fee limit is configured; a fee commitment requires an explicit limit")
        elif fee > limits.max_total_fee_commitment:
            reasons.append(f"total fee {fee} exceeds the profile limit {limits.max_total_fee_commitment}")
    conditional = _money(params.get("conditional_total"))
    if conditional is not None:
        terms["conditional_total"] = str(conditional)
        if limits.max_total_conditional_commitment is None:
            reasons.append("no conditional commitment limit is configured")
        elif conditional > limits.max_total_conditional_commitment:
            reasons.append(f"conditional commitments {conditional} exceed the profile limit {limits.max_total_conditional_commitment}")
    years = params.get("contract_years")
    if years is not None:
        terms["contract_years"] = years
        if limits.max_contract_years is not None and years > limits.max_contract_years:
            reasons.append(f"contract length {years} years exceeds the profile limit {limits.max_contract_years}")
    if intent.kind in ("release_player", "player.release") and not limits.allow_player_release:
        reasons.append("player release is not permitted by the profile")
    if intent.kind in ("sell_player", "accept_sale") and not limits.allow_player_sale:
        reasons.append("player sale is not permitted by the profile")
    if family == "progression" and not limits.allow_continue:
        reasons.append("pressing Continue is not permitted by the profile")
    if params.get("unrecognized_clauses"):
        reasons.append(f"offer contains unrecognized clauses: {params['unrecognized_clauses']}")
    if params.get("ambiguous_payer"):
        reasons.append("offer contains an obligation with an ambiguous payer")
    if reasons:
        return OutsideScope(scope, profile.version, reasons, terms)
    return Allowed(scope, profile.version, [f"family {family} enabled in {profile.mode.value} profile v{profile.version}"])
