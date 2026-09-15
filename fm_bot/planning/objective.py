"""Versioned club objective profile and component scoring (spec 6.2, 15.1; AUD 01).

The objective for *feasible* plans is

    sporting_value
    + w_dev    * normalised_development_value
    + w_cont   * normalised_squad_continuity
    - w_risk   * normalised_downside_loss
    - w_change * normalised_plan_changes

Hard execution, eligibility and authority constraints are enforced before
this score is ever computed (by the lineup, finance and authority modules);
nothing here can compensate for an illegal lineup or an unauthorised
commitment. Raw pounds, points and squad indices are never added together:
each input is divided by the declared scale in the profile first, and every
component is reported separately so a high total cannot hide a sporting
failure (``sporting_failure`` is set whenever the normalised sporting
component is below the profile's floor, whatever the total).

Baseline: this module is bookkeeping over declared weights; the weights and
scales are the club's stated policy (operator-controlled, spec 15.1), not
fitted values. Fitting weights to outcomes would be an experiment for
``fm_bot.models`` and is not done here. :func:`default_profile` is a
starting point to be edited, not a recommendation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from ..state.status import Observed, ValueStatus
from ..state.units import Money, Period, UnitError

OBJECTIVE_PROFILE_VERSION = "club-objective-v1"
COMPONENT_NAMES: tuple[str, ...] = ("sporting", "development", "continuity", "risk", "change")
# Sign of each component in the combined score; the weight of sporting value is fixed at 1.
COMPONENT_SIGNS: dict[str, int] = {"sporting": 1, "development": 1, "continuity": 1, "risk": -1, "change": -1}
COMPONENT_UNITS: dict[str, str] = {"sporting": "expected_points", "development": "development_index", "continuity": "continuity_index", "risk": "money_once", "change": "plan_changes"}

DEFAULT_SPORTING_FLOOR = 0.25   # normalised sporting value below which the plan is a sporting failure regardless of total


@dataclass(frozen=True)
class NormalisationScales:
    """Declared conversions from raw units to dimensionless component values (value == scale -> 1.0).

    ``downside_loss`` is a one-off :class:`Money` amount: the loss that
    counts as a full unit of risk. Money is divided exactly (minor units)
    and only when currency and period match.
    """

    sporting_points: float
    development: float
    continuity: float
    downside_loss: Money
    plan_changes: float

    def __post_init__(self):
        for name in ("sporting_points", "development", "continuity", "plan_changes"):
            if not getattr(self, name) > 0:
                raise ValueError(f"normalisation scale {name} must be positive")
        if self.downside_loss.minor <= 0 or self.downside_loss.period is not Period.ONCE:
            raise ValueError("downside_loss scale must be a positive one-off amount")

    def to_json(self) -> dict[str, Any]:
        return {"sporting_points": self.sporting_points, "development": self.development, "continuity": self.continuity, "downside_loss": self.downside_loss.to_json(), "plan_changes": self.plan_changes}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "NormalisationScales":
        return cls(float(data["sporting_points"]), float(data["development"]), float(data["continuity"]), Money.from_json(data["downside_loss"]), float(data["plan_changes"]))


@dataclass
class ClubObjectiveProfile:
    """The club's stated priorities, weights and scales (operator-controlled, versioned).

    ``competition_priorities`` maps competition id (as text) to a relative
    priority; ``promotion_target``/``relegation_floor`` are free-text targets
    shown to the operator; ``cash_reserve_policy`` is a reference to the
    finance risk policy in force (the money limits live there, not here);
    ``development_emphasis`` in 0..1 records how much youth development is
    stressed and is exposed for reporting alongside ``w_dev``.
    """

    version: str
    competition_priorities: dict[str, float]
    promotion_target: str | None
    relegation_floor: str | None
    cash_reserve_policy: str
    development_emphasis: float
    w_dev: float
    w_cont: float
    w_risk: float
    w_change: float
    scales: NormalisationScales
    sporting_floor: float = DEFAULT_SPORTING_FLOOR
    notes: list[str] = field(default_factory=list)

    def __post_init__(self):
        for name in ("w_dev", "w_cont", "w_risk", "w_change"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative; the sign is fixed by the objective")
        if not 0.0 <= self.development_emphasis <= 1.0:
            raise ValueError("development_emphasis must be within 0..1")
        if any(v < 0 for v in self.competition_priorities.values()):
            raise ValueError("competition priorities must be non-negative")

    def weight(self, component: str) -> float:
        return {"sporting": 1.0, "development": self.w_dev, "continuity": self.w_cont, "risk": self.w_risk, "change": self.w_change}[component]

    def priority(self, competition_id: int | str) -> float | None:
        return self.competition_priorities.get(str(competition_id))

    def to_json(self) -> dict[str, Any]:
        return {"version": self.version, "competition_priorities": dict(self.competition_priorities), "promotion_target": self.promotion_target, "relegation_floor": self.relegation_floor, "cash_reserve_policy": self.cash_reserve_policy, "development_emphasis": self.development_emphasis, "w_dev": self.w_dev, "w_cont": self.w_cont, "w_risk": self.w_risk, "w_change": self.w_change, "scales": self.scales.to_json(), "sporting_floor": self.sporting_floor, "notes": list(self.notes)}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ClubObjectiveProfile":
        return cls(str(data["version"]), {str(k): float(v) for k, v in (data.get("competition_priorities") or {}).items()}, data.get("promotion_target"), data.get("relegation_floor"), str(data["cash_reserve_policy"]), float(data["development_emphasis"]), float(data["w_dev"]), float(data["w_cont"]), float(data["w_risk"]), float(data["w_change"]), NormalisationScales.from_json(data["scales"]), float(data.get("sporting_floor", DEFAULT_SPORTING_FLOOR)), list(data.get("notes") or []))


def default_profile() -> ClubObjectiveProfile:
    """A starting profile for a league club: edit before use; weights are policy, not estimates."""
    scales = NormalisationScales(sporting_points=15.0, development=1.0, continuity=1.0, downside_loss=Money.native_gbp(500_000), plan_changes=5.0)
    return ClubObjectiveProfile(OBJECTIVE_PROFILE_VERSION, {}, None, None, "finance-baseline-0.1", 0.3, 0.2, 0.1, 0.5, 0.05, scales, notes=["default profile: competition priorities and targets must be set by the operator"])


@dataclass
class ObjectiveInputs:
    """Raw inputs, each an :class:`Observed` in its own unit; unavailable inputs stay unavailable."""

    sporting_value: Observed        # float expected points (or the profile's sporting unit) over the horizon
    development_value: Observed     # float development index
    continuity: Observed            # float continuity index
    downside_loss: Observed         # Money (one-off) expected/CVaR loss from the finance engine
    plan_changes: Observed          # int count of unnecessary plan changes

    def observed(self, component: str) -> Observed:
        return {"sporting": self.sporting_value, "development": self.development_value, "continuity": self.continuity, "risk": self.downside_loss, "change": self.plan_changes}[component]


@dataclass
class Component:
    name: str
    unit: str
    raw: Any                        # the raw value (float, int or Money json) or None
    scale: Any
    normalised: Observed
    weight: float
    sign: int
    contribution: Observed          # sign * weight * normalised

    def to_json(self) -> dict[str, Any]:
        raw = self.raw.to_json() if isinstance(self.raw, Money) else self.raw
        scale = self.scale.to_json() if isinstance(self.scale, Money) else self.scale
        return {"name": self.name, "unit": self.unit, "raw": raw, "scale": scale, "normalised": self.normalised.to_json(), "weight": self.weight, "sign": self.sign, "contribution": self.contribution.to_json()}


@dataclass
class ObjectiveComponents:
    components: dict[str, Component]
    total: Observed
    sporting_failure: bool | None           # None when the sporting component is unavailable
    sporting_status: str
    profile_version: str
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"components": {k: v.to_json() for k, v in self.components.items()}, "total": self.total.to_json(), "sporting_failure": self.sporting_failure, "sporting_status": self.sporting_status, "profile_version": self.profile_version, "notes": list(self.notes)}


def _normalise(name: str, observed: Observed, profile: ClubObjectiveProfile) -> tuple[Observed, Any]:
    """Divide a raw input by its declared scale; money is divided exactly and must match the scale's units."""
    scales = profile.scales
    if name == "risk":
        scale: Any = scales.downside_loss
    else:
        scale = {"sporting": scales.sporting_points, "development": scales.development, "continuity": scales.continuity, "change": scales.plan_changes}[name]
    if not observed.available:
        return Observed.unavailable(observed.status, f"{name}_normalised", observed.reason, observed.source), scale
    value = observed.value
    if name == "risk":
        if not isinstance(value, Money):
            raise UnitError(f"downside loss must be Money, got {type(value).__name__}")
        if value.currency != scale.currency or value.period is not scale.period:
            raise UnitError(f"downside loss {value.currency}/{value.period.value} does not match the scale {scale.currency}/{scale.period.value}")
        normalised = float(Fraction(value.minor, scale.minor))
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} input must be a number, got {type(value).__name__}")
        normalised = float(value) / float(scale)
    return Observed.available_value(normalised, observed.source, what=f"{name}_normalised"), scale


def score_components(profile: ClubObjectiveProfile, inputs: ObjectiveInputs) -> ObjectiveComponents:
    """Score a feasible plan: every component separately plus the combined total.

    The total is unavailable when any component is unavailable (a missing
    risk estimate is not zero risk). ``sporting_failure`` is ``True`` when the
    normalised sporting value is below ``profile.sporting_floor`` regardless
    of the total, ``False`` when at or above it, and ``None`` when sporting
    value is unavailable.
    """
    components: dict[str, Component] = {}
    total = 0.0
    blocked: list[str] = []
    for name in COMPONENT_NAMES:
        observed = inputs.observed(name)
        normalised, scale = _normalise(name, observed, profile)
        weight = profile.weight(name)
        sign = COMPONENT_SIGNS[name]
        if normalised.available:
            contribution = Observed.available_value(sign * weight * normalised.value, observed.source, what=f"{name}_contribution")
            total += contribution.value
        else:
            contribution = Observed.unavailable(normalised.status, f"{name}_contribution", normalised.reason, normalised.source)
            blocked.append(f"{name} {normalised.status.value}: {normalised.reason or 'no value'}")
        components[name] = Component(name, COMPONENT_UNITS[name], observed.value, scale, normalised, weight, sign, contribution)
    sporting = components["sporting"].normalised
    if sporting.available:
        failure: bool | None = sporting.value < profile.sporting_floor
        sporting_status = "below_floor" if failure else "at_or_above_floor"
    else:
        failure = None
        sporting_status = f"unavailable ({sporting.status.value})"
    if blocked:
        total_observed = Observed.unavailable(ValueStatus.MISSING, "objective_total", "; ".join(blocked), "objective")
    else:
        total_observed = Observed.available_value(total, "objective", what="objective_total")
    notes = []
    if failure:
        notes.append(f"sporting value {sporting.value:.3f} is below the floor {profile.sporting_floor}; the total does not override this")
    return ObjectiveComponents(components, total_observed, failure, sporting_status, profile.version, notes)
