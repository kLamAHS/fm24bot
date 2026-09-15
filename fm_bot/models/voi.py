"""Value of information for scouting assignments (spec 11.1).

``VOI = E[best decision value after evidence] - best decision value now
- scouting cost - opportunity cost``. Values are in *decision-value units*
chosen by the caller's objective profile (spec 6.2); pounds, points and
morale must be converted explicitly before they reach this module, which
is why the inputs are plain numbers rather than :class:`Money`.

This is a model-based estimate of decision improvement, a baseline
heuristic rather than a Bayesian experimental design. In bridge-observed
mode the bot already reads a candidate's actual attributes, so a scouting
report cannot "discover" them; such candidates are flagged
``attributes_already_known`` and only the non-attribute part of their VOI
(contract terms, availability, context) is credited.

Report handling: a stored report is weighted by source quality and decays
with age. Absence of a stored report is a distinct status
(``no_stored_report``), not proof that the player is unknown: without a
report or an observed knowledge level the share of the gain still to be
learned is unknown, and the candidate's VOI is ``unavailable`` (flagged
``knowledge_unknown``) rather than credited in full.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..state.visibility import InformationMode

VOI_VERSION = "voi-1.0"

PROBABILITY_TOLERANCE = 1e-6
REPORT_HALF_LIFE_DAYS = 90.0     # a report's weight halves every 90 game days (heuristic)
ATTRIBUTE_KNOWN_FLAG = "attributes_already_known"
KNOWLEDGE_UNKNOWN_FLAG = "knowledge_unknown"
KNOWLEDGE_OBSERVED_FLAG = "knowledge_observed"


class VoiError(ValueError):
    pass


@dataclass(frozen=True)
class Scenario:
    """One weighted state of the world: the decision value now and after the evidence arrives."""

    name: str
    probability: float
    value_now: float
    value_after_evidence: float


@dataclass(frozen=True)
class VoiResult:
    voi: float
    expected_value_after_evidence: float
    current_decision_value: float
    scouting_cost: float
    opportunity_cost: float
    status: str = "available"
    reason: str | None = None
    voi_version: str = VOI_VERSION

    @property
    def worthwhile(self) -> bool:
        return self.voi > 0.0


def scenario_expectation(scenarios: Sequence[Scenario]) -> tuple[float, float]:
    """Probability-weighted (value_now, value_after_evidence). Probabilities must sum to one."""
    if not scenarios:
        raise VoiError("no scenarios")
    total = sum(s.probability for s in scenarios)
    if any(s.probability < 0 for s in scenarios) or abs(total - 1.0) > PROBABILITY_TOLERANCE:
        raise VoiError(f"scenario probabilities must be non-negative and sum to 1, got {total}")
    return sum(s.probability * s.value_now for s in scenarios), sum(s.probability * s.value_after_evidence for s in scenarios)


def value_of_information(current_decision_value: float | None, expected_value_after_evidence: float | None, scouting_cost: float, opportunity_cost: float, *, scenarios: Sequence[Scenario] | None = None) -> VoiResult:
    """VOI in decision-value units. Pass ``scenarios`` for a scenario-weighted expectation, or the two values directly."""
    if scouting_cost < 0 or opportunity_cost < 0:
        raise VoiError("costs must be non-negative")
    if scenarios is not None:
        current_decision_value, expected_value_after_evidence = scenario_expectation(scenarios)
    if current_decision_value is None or expected_value_after_evidence is None:
        return VoiResult(0.0, 0.0, 0.0, scouting_cost, opportunity_cost, "unavailable", "decision values not available; VOI cannot be estimated")
    voi = expected_value_after_evidence - current_decision_value - scouting_cost - opportunity_cost
    return VoiResult(voi, expected_value_after_evidence, current_decision_value, scouting_cost, opportunity_cost)


# ----- reports and beliefs -----

def report_weight(source_quality: float, report_date: str | None, as_of: str) -> float:
    """Source quality in [0, 1] decayed by report age with :data:`REPORT_HALF_LIFE_DAYS`. Undated reports get quality only."""
    if not 0.0 <= source_quality <= 1.0:
        raise VoiError("source_quality must be in [0, 1]")
    if report_date is None:
        return source_quality
    age = (dt.date.fromisoformat(as_of) - dt.date.fromisoformat(report_date)).days
    if age < 0:
        raise VoiError(f"report dated {report_date} is after {as_of}; a future report is leakage")
    return source_quality * (0.5 ** (age / REPORT_HALF_LIFE_DAYS))


@dataclass(frozen=True)
class Belief:
    mean: float
    variance: float


def update_belief(prior: Belief, report_value: float, report_variance: float, weight: float) -> Belief:
    """Precision-weighted update where the report's precision is scaled by ``weight`` (quality x freshness)."""
    if report_variance <= 0 or prior.variance <= 0:
        raise VoiError("variances must be positive")
    if weight <= 0.0:
        return prior
    prior_precision = 1.0 / prior.variance
    report_precision = weight / report_variance
    precision = prior_precision + report_precision
    return Belief((prior.mean * prior_precision + report_value * report_precision) / precision, 1.0 / precision)


# ----- assignment ranking -----

@dataclass
class ScoutingCandidate:
    player_id: int
    name: str
    attributes_available: bool                    # true when the bridge already exposes the actual attributes
    attribute_scenarios: list[Scenario] = field(default_factory=list)   # value gained by learning attributes
    context_scenarios: list[Scenario] = field(default_factory=list)     # value gained by learning terms/availability/context
    last_report_date: str | None = None
    source_quality: float | None = None
    scouting_cost: float = 0.0
    opportunity_cost: float = 0.0
    knowledge_level: float | None = None          # observed share of the player already known, in [0, 1] (e.g. bridge scout_report_knowledge); None = not observed


@dataclass
class AssignmentRanking:
    player_id: int
    name: str
    voi: float | None
    status: str                       # available | unavailable
    flags: list[str] = field(default_factory=list)
    report_status: str = "no_stored_report"
    report_weight: float | None = None
    components: dict[str, float | None] = field(default_factory=dict)
    reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"player_id": self.player_id, "name": self.name, "voi": self.voi, "status": self.status, "flags": list(self.flags), "report_status": self.report_status, "report_weight": self.report_weight, "components": dict(self.components), "reason": self.reason}


def _component(scenarios: Sequence[Scenario]) -> float | None:
    if not scenarios:
        return None
    now, after = scenario_expectation(scenarios)
    return after - now


def rank_assignments(candidates: Sequence[ScoutingCandidate], mode: InformationMode, as_of: str) -> list[AssignmentRanking]:
    """Rank candidates by VOI, highest first; unavailable estimates sort last and keep their reason.

    In ``BRIDGE_OBSERVED`` mode a candidate whose attributes the bridge
    already exposes is flagged and only its context component counts. In
    ``MANAGER_VISIBLE`` mode actual attributes are never consulted, so the
    attribute component is credited whenever the caller supplied it.

    The gain is scaled by the share still to be learned: ``1 - weight`` of a
    stored report, else ``1 - knowledge_level`` when a knowledge level was
    observed. With neither, that share is unknown and the estimate is
    ``unavailable`` (spec 11.1: no stored report is not proof the player is
    unknown), never the full gain.
    """
    rankings = []
    for c in candidates:
        flags: list[str] = []
        attribute_gain = _component(c.attribute_scenarios)
        context_gain = _component(c.context_scenarios)
        if mode is InformationMode.BRIDGE_OBSERVED and c.attributes_available:
            flags.append(ATTRIBUTE_KNOWN_FLAG)
            attribute_gain = 0.0 if attribute_gain is not None else None
        weight = report_weight(c.source_quality, c.last_report_date, as_of) if c.source_quality is not None else None
        report_status = "stored_report" if c.last_report_date is not None or c.source_quality is not None else "no_stored_report"
        components = {"attributes": attribute_gain, "context": context_gain}
        gains = [g for g in components.values() if g is not None]
        if not gains:
            rankings.append(AssignmentRanking(c.player_id, c.name, None, "unavailable", flags + ["no_decision_scenarios"], report_status, weight, components, "no decision scenarios supplied"))
            continue
        # A fresh high-quality report (or an observed knowledge level) leaves less to learn: scale the gain by what is not already known.
        if weight is not None:
            remaining = 1.0 - weight
        elif c.knowledge_level is not None:
            if not 0.0 <= c.knowledge_level <= 1.0:
                raise VoiError("knowledge_level must be in [0, 1]")
            remaining = 1.0 - c.knowledge_level
            flags.append(KNOWLEDGE_OBSERVED_FLAG)
        else:
            rankings.append(AssignmentRanking(c.player_id, c.name, None, "unavailable", flags + [KNOWLEDGE_UNKNOWN_FLAG], report_status, None, components, "no stored report and no observed knowledge level: how much of the gain is still unknown cannot be estimated"))
            continue
        voi = sum(gains) * remaining - c.scouting_cost - c.opportunity_cost
        rankings.append(AssignmentRanking(c.player_id, c.name, voi, "available", flags, report_status, weight, components))
    rankings.sort(key=lambda r: (r.voi is None, -(r.voi or 0.0)))
    return rankings
