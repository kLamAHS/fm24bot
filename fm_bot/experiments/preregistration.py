"""Preregistration: the hypothesis, outcome and stopping rule declared before a comparison runs.

The specification requires the hypothesis, treatment, outcome, smallest
useful effect and stopping rule to be stated *before* running a comparison
(13.2), and candidate models and thresholds to be frozen before the final
evaluation (13.3). The arithmetic of the gate lives in
:mod:`fm_bot.experiments.evaluation`; what lives here is the commitment: a
declaration that is journaled once, cannot be edited afterwards, and is
compared against the analysis that was actually run.

A result analysed differently from what was declared is not reported as an
improvement. It is reported as ``exploratory``, with the deviations named.
That is the whole point: a study whose outcome measure or stopping rule
moved after the data arrived has no confirmatory value, however good the
interval looks (13.5).

Nothing here runs a trial. :mod:`fm_bot.experiments.runner` does that, and
only when the laboratory capabilities are actually supported.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable

from ..state.identity import new_id, utc_now
from ..state.records import canonical_json
from ..state.visibility import InformationMode
from .evaluation import DEFAULT_ALPHA, DEFAULT_POWER, DEFAULT_RESAMPLES, ComparisonResult, GateReport, evidence_gate, required_units

PREREGISTRATION_VERSION = "experiments.preregistration/1"

JOURNAL_DECLARED = "experiment.preregistered"
JOURNAL_REPORTED = "experiment.result"
SETTING_PREFIX = "preregistration:"

HIGHER_IS_BETTER = "higher_is_better"
LOWER_IS_BETTER = "lower_is_better"
DIRECTIONS = (HIGHER_IS_BETTER, LOWER_IS_BETTER)

STATUS_DECLARED = "declared"
STATUS_REPORTED = "reported"

# A result that did not follow its own declaration is exploratory, whatever its interval says.
VERDICT_EXPLORATORY = "exploratory"
VERDICT_NOT_RUN = "not_run"


class PreregistrationError(ValueError):
    """A declaration is incomplete, or an attempt was made to change one after the fact."""


@dataclass(frozen=True)
class AnalysisPlan:
    """How the comparison will be analysed, fixed in advance.

    ``cluster_by`` records the unit the uncertainty is computed over: the
    spec forbids treating correlated fixtures as independent replicates, so
    the declared unit is part of the commitment, not an implementation
    detail.
    """

    metric: str
    paired: bool = True
    alpha: float = DEFAULT_ALPHA
    resamples: int = DEFAULT_RESAMPLES
    seed: int = 0
    cluster_by: str = "career"

    def deviations_from(self, comparison: ComparisonResult) -> list[str]:
        """How the comparison that ran differs from the plan (empty means it followed it)."""
        out: list[str] = []
        if comparison.metric != self.metric:
            out.append(f"outcome measure changed: declared {self.metric!r}, analysed {comparison.metric!r}")
        if comparison.alpha != self.alpha:
            out.append(f"alpha changed: declared {self.alpha}, analysed {comparison.alpha}")
        if comparison.resamples != self.resamples:
            out.append(f"resamples changed: declared {self.resamples}, analysed {comparison.resamples}")
        if comparison.seed != self.seed:
            out.append(f"seed changed: declared {self.seed}, analysed {comparison.seed}")
        return out

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Preregistration:
    """A declaration frozen before the comparison runs.

    ``threshold`` is the improvement the confidence interval must clear, in
    the metric's own units and always in the improving direction:
    :func:`report` orients a lower-is-better metric before applying the
    gate, so the declared threshold never has to be written as a negative
    number.
    """

    prereg_id: str
    hypothesis: str
    treatment: dict[str, Any]
    baseline_id: str
    primary_outcome: str
    smallest_useful_effect: float
    direction: str
    threshold: float
    stopping_rule: str
    analysis: AnalysisPlan
    guardrails: list[str] = field(default_factory=list)
    expected_sd: float | None = None
    required_clusters: int | None = None
    model_id: str | None = None
    model_version: str | None = None
    information_mode: str = InformationMode.BRIDGE_OBSERVED.value
    holdout_id: str | None = None
    secondary_outcomes: list[str] = field(default_factory=list)
    notes: str | None = None
    declared_at: str = field(default_factory=utc_now)
    version: str = PREREGISTRATION_VERSION

    def fingerprint(self) -> str:
        """Hash of the declaration itself, excluding the time it was written."""
        body = {k: v for k, v in self.to_json().items() if k != "declared_at"}
        return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["analysis"] = self.analysis.to_json()
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Preregistration":
        data = dict(data)
        data["analysis"] = AnalysisPlan(**data["analysis"])
        data.pop("fingerprint", None)
        return cls(**data)


def validate(prereg: Preregistration) -> list[str]:
    """Everything the specification requires a declaration to state, before it counts as one."""
    problems: list[str] = []
    if not prereg.hypothesis.strip():
        problems.append("no hypothesis stated")
    if not prereg.treatment:
        problems.append("no treatment stated")
    if not prereg.baseline_id.strip():
        problems.append("no baseline named; a model is compared against the best relevant baseline")
    if not prereg.primary_outcome.strip():
        problems.append("no primary outcome stated")
    if prereg.smallest_useful_effect <= 0:
        problems.append("the smallest useful effect must be positive")
    if prereg.direction not in DIRECTIONS:
        problems.append(f"direction must be one of {list(DIRECTIONS)}")
    if not prereg.stopping_rule.strip():
        problems.append("no stopping rule stated")
    if prereg.analysis.metric != prereg.primary_outcome:
        problems.append(f"the analysis measures {prereg.analysis.metric!r} but the primary outcome is {prereg.primary_outcome!r}")
    if prereg.threshold < 0:
        problems.append("the threshold is stated in the improving direction, so it is never negative")
    if prereg.expected_sd is not None and prereg.expected_sd <= 0:
        problems.append("the pilot standard deviation must be positive")
    return problems


def declare(*, hypothesis: str, treatment: dict[str, Any], baseline_id: str, primary_outcome: str, smallest_useful_effect: float, stopping_rule: str, direction: str = HIGHER_IS_BETTER, threshold: float | None = None, analysis: AnalysisPlan | None = None, guardrails: Iterable[str] = (), expected_sd: float | None = None, power: float = DEFAULT_POWER, prereg_id: str | None = None, **extra: Any) -> Preregistration:
    """Build a declaration, sizing it from the pilot's standard deviation when one is known.

    ``required_clusters`` stays ``None`` when no pilot variability is
    supplied: the size of the evaluation is then simply undeclared, which
    the gate later reports as such rather than assuming a convenient count.
    """
    plan = analysis or AnalysisPlan(primary_outcome)
    prereg = Preregistration(
        prereg_id or new_id("prereg"), hypothesis, dict(treatment), baseline_id, primary_outcome,
        smallest_useful_effect, direction, smallest_useful_effect if threshold is None else threshold,
        stopping_rule, plan, list(guardrails), expected_sd, None, **extra,
    )
    # Validate the declaration before sizing it: an incomplete declaration is
    # reported as such, never as an arithmetic error from the power formula.
    problems = validate(prereg)
    if problems:
        raise PreregistrationError("; ".join(problems))
    if expected_sd is None:
        return prereg
    return replace(prereg, required_clusters=required_units(smallest_useful_effect, expected_sd, alpha=plan.alpha, power=power, paired=plan.paired))


@dataclass
class PreregisteredResult:
    """What a comparison established against what it had promised to measure."""

    prereg_id: str
    fingerprint: str
    verdict: str                             # the gate's verdict, or "exploratory" / "not_run"
    gate: GateReport | None
    deviations: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    oriented_mean: float | None = None       # the difference in the improving direction
    reported_at: str = field(default_factory=utc_now)
    version: str = PREREGISTRATION_VERSION

    @property
    def confirmatory(self) -> bool:
        """A result only counts as confirmatory when it followed its own declaration."""
        return not self.deviations and self.verdict != VERDICT_EXPLORATORY

    def to_json(self) -> dict[str, Any]:
        return {"prereg_id": self.prereg_id, "fingerprint": self.fingerprint, "verdict": self.verdict, "gate": self.gate.to_json() if self.gate else None, "deviations": list(self.deviations), "reasons": list(self.reasons), "oriented_mean": self.oriented_mean, "confirmatory": self.confirmatory, "reported_at": self.reported_at, "version": self.version}


def _orient(prereg: Preregistration, comparison: ComparisonResult) -> ComparisonResult:
    """Point the comparison in the improving direction so one threshold rule serves both."""
    if prereg.direction == HIGHER_IS_BETTER or not comparison.available:
        return comparison
    return replace(comparison, mean_difference=None if comparison.mean_difference is None else -comparison.mean_difference, ci_low=None if comparison.ci_high is None else -comparison.ci_high, ci_high=None if comparison.ci_low is None else -comparison.ci_low)


class PreregistrationRegistry:
    """Declarations, journaled once and immutable afterwards.

    Backed by the store's versioned settings, so a declaration survives a
    restart and its journal entry records when it was made relative to the
    runs that follow it.
    """

    def __init__(self, store=None):
        self.store = store
        self._memory: dict[str, Preregistration] = {}

    def get(self, prereg_id: str) -> Preregistration | None:
        if self.store is not None:
            stored = self.store.get_setting(SETTING_PREFIX + prereg_id)
            if stored is not None:
                prereg = Preregistration.from_json(stored[0])
                self._memory[prereg_id] = prereg
                return prereg
        return self._memory.get(prereg_id)

    def declare(self, prereg: Preregistration) -> Preregistration:
        """Record a declaration. Re-declaring the same one is idempotent; changing it is refused."""
        problems = validate(prereg)
        if problems:
            raise PreregistrationError("; ".join(problems))
        existing = self.get(prereg.prereg_id)
        if existing is not None:
            if existing.fingerprint() != prereg.fingerprint():
                raise PreregistrationError(f"{prereg.prereg_id} was already declared at {existing.declared_at} with a different content; a preregistration cannot be edited. Declare a new one and say why.")
            return existing
        self._memory[prereg.prereg_id] = prereg
        if self.store is not None:
            self.store.put_setting(SETTING_PREFIX + prereg.prereg_id, prereg.to_json())
            self.store.journal(JOURNAL_DECLARED, {**prereg.to_json(), "fingerprint": prereg.fingerprint()}, prereg.prereg_id)
        return prereg

    def status(self, prereg_id: str) -> str:
        if self.get(prereg_id) is None:
            return "unknown"
        if self.store is None:
            return STATUS_DECLARED
        return STATUS_REPORTED if self.store.journal_entries(kind=JOURNAL_REPORTED, ref_id=prereg_id) else STATUS_DECLARED

    def report(self, prereg_id: str, comparison: ComparisonResult, *, guardrails: dict[str, bool] | None = None) -> PreregisteredResult:
        """Judge a comparison against its own declaration.

        The declared guardrails must all have been measured: an unmeasured
        guardrail is a deviation, not a pass. Undeclared guardrails are kept
        in the report but never turn a confirmatory result into a failure.
        """
        prereg = self.get(prereg_id)
        if prereg is None:
            raise PreregistrationError(f"no preregistration {prereg_id!r}; declare it before running the comparison")
        measured = dict(guardrails or {})
        deviations = prereg.analysis.deviations_from(comparison)
        deviations.extend(f"declared guardrail {name!r} was not measured" for name in prereg.guardrails if name not in measured)
        declared_guardrails = {name: measured[name] for name in prereg.guardrails if name in measured}
        extra = sorted(set(measured) - set(prereg.guardrails))
        oriented = _orient(prereg, comparison)
        gate = evidence_gate(oriented, threshold=prereg.threshold, guardrails=declared_guardrails, required_clusters=prereg.required_clusters)
        reasons = list(gate.reasons)
        if prereg.required_clusters is None:
            reasons.append("the evaluation size was never declared (no pilot variability), so the result is not judged against a required count")
        if extra:
            reasons.append(f"guardrails measured but not declared, recorded only: {extra}")
        if prereg.direction == LOWER_IS_BETTER:
            reasons.append(f"{prereg.primary_outcome} is better when lower; the interval is reported in the improving direction")
        verdict = VERDICT_EXPLORATORY if deviations else gate.verdict
        if deviations:
            reasons.insert(0, "the analysis departed from the preregistration, so the result is exploratory and carries no confirmatory weight")
        result = PreregisteredResult(prereg_id, prereg.fingerprint(), verdict, gate, deviations, reasons, oriented.mean_difference)
        if self.store is not None:
            self.store.journal(JOURNAL_REPORTED, result.to_json(), prereg_id)
        return result


# ---------------------------------------------------------------------------
# The first declared model experiment (specification ticket BOT 012)
# ---------------------------------------------------------------------------

def first_model_experiment() -> Preregistration:
    """The first model experiment, declared in advance and not yet run.

    It asks the modest question the observations can actually support: does
    the fatigue model in :mod:`fm_bot.models.dynamics` predict a player's
    readiness at the next fixture better than the empirical recovery curve
    baseline? The outcome is absolute error in condition points, so lower is
    better. It is declared here, with no evaluation size, because running it
    needs the laboratory capabilities (``save_restore`` and a validated
    adapter) that nothing supplies yet: the declaration is the deliverable,
    and it is what a later run will be judged against.
    """
    return declare(
        prereg_id="prereg-readiness-v1",
        hypothesis="The fitted fatigue model predicts validated readiness at the next fixture with a smaller absolute error than the empirical recovery-curve baseline, for players whose condition readings are current.",
        treatment={"model_id": "recovery-dynamics", "policy": "predict readiness from the fitted fatigue state", "applies_to": "players with a current readiness observation"},
        baseline_id="recovery-baseline-v1",
        primary_outcome="readiness_absolute_error",
        direction=LOWER_IS_BETTER,
        smallest_useful_effect=2.0,          # condition points; smaller than this changes no decision
        threshold=2.0,
        stopping_rule="Judge once, on the frozen holdout careers, after every declared career has completed its fixtures; no interim looks, and no extension if the interval is wide.",
        analysis=AnalysisPlan("readiness_absolute_error", paired=True, seed=20240217, cluster_by="career"),
        guardrails=["eligibility_violations", "reserve_shortfalls", "uncertain_actions"],
        model_id="recovery-dynamics",
        information_mode=InformationMode.BRIDGE_OBSERVED.value,
        notes="Declared before any run. Requires the laboratory capabilities save_restore and ui_action_adapter; until those are supplied by a validated adapter the experiment stays declared and unrun.",
    )
