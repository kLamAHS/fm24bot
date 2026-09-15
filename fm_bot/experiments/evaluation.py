"""Metrics, paired comparisons and evidence gates (spec 13.2, 13.4, 13.5, MOD 01).

Only metrics the bridge can actually support are implemented: results and
goal difference from the fixture list, eligibility violations from verified
eligibility records, planned versus actual minutes, cash reserve shortfalls,
forecast error, execution counts and wall time. Expected goals, possession,
tracking and complete injury data are not assumed to exist and have no
metric here.

Every metric returns an :class:`Observed` value: when the inputs cannot
support the metric (no played matches, unverified eligibility, a planned
player with no recorded minutes) the metric is unavailable with a reason,
never zero.

Comparisons are paired and clustered at the run/career level with a pure
Python bootstrap under an explicit seed. The power helper is a normal
approximation for sizing a pilot; the gate classifier turns a confidence
interval plus guardrail status into one of four honest verdicts.
"""
from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field, asdict
from decimal import ROUND_HALF_EVEN
from fractions import Fraction
from typing import Any, Callable, Iterable, Sequence

from ..state.status import Observed, ValueStatus
from ..state.units import Money, sum_money
from .manifests import RunManifest, TrialManifest

EVALUATION_VERSION = "experiments.evaluation/1"

POINTS_WIN, POINTS_DRAW, POINTS_LOSS = 3, 1, 0

# Reviewable defaults for the comparison machinery.
DEFAULT_ALPHA = 0.05
DEFAULT_POWER = 0.80
DEFAULT_RESAMPLES = 2000
MIN_CLUSTERS_FOR_INTERVAL = 2

RESULT_IMPROVEMENT = "improvement"
RESULT_NO_IMPROVEMENT = "no_improvement"
RESULT_INCONCLUSIVE = "inconclusive"
RESULT_BLOCKED = "blocked_by_guardrail"

# Eligibility statuses that count as a confirmed violation when a player was submitted.
INELIGIBLE_STATUSES = frozenset({"ineligible"})
VERIFIED_STATUSES = frozenset({"eligible", "ineligible"})


# ---------------------------------------------------------------------------
# Sporting metrics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchOutcome:
    goals_for: int
    goals_against: int
    competition_id: int | None = None
    date: str | None = None

    @property
    def points(self) -> int:
        if self.goals_for > self.goals_against:
            return POINTS_WIN
        if self.goals_for == self.goals_against:
            return POINTS_DRAW
        return POINTS_LOSS


def match_outcomes_from_fixtures(fixtures_payload: dict[str, Any], club_id: int, *, competition_ids: Iterable[int] | None = None) -> list[MatchOutcome]:
    """Played fixtures of ``club_id`` from a ``/fixtures`` payload, as outcomes. Unplayed fixtures are skipped, not scored."""
    allowed = set(competition_ids) if competition_ids is not None else None
    outcomes: list[MatchOutcome] = []
    for item in fixtures_payload.get("fixtures", []) or []:
        if item.get("status") != "played" or item.get("home_score") is None or item.get("away_score") is None:
            continue
        if allowed is not None and item.get("competition_id") not in allowed:
            continue
        home = (item.get("home") or {}).get("club_id") == club_id
        away = (item.get("away") or {}).get("club_id") == club_id
        if not (home or away):
            continue
        gf, ga = (item["home_score"], item["away_score"]) if home else (item["away_score"], item["home_score"])
        outcomes.append(MatchOutcome(int(gf), int(ga), item.get("competition_id"), item.get("date")))
    return outcomes


def points_per_match(matches: Sequence[MatchOutcome]) -> Observed[float]:
    if not matches:
        return Observed.unavailable(ValueStatus.MISSING, "points_per_match", "no played matches")
    return Observed.available_value(sum(m.points for m in matches) / len(matches), "fixtures", what="points_per_match")


def goal_difference(matches: Sequence[MatchOutcome]) -> Observed[int]:
    if not matches:
        return Observed.unavailable(ValueStatus.MISSING, "goal_difference", "no played matches")
    return Observed.available_value(sum(m.goals_for - m.goals_against for m in matches), "fixtures", what="goal_difference")


# ---------------------------------------------------------------------------
# Selection metrics
# ---------------------------------------------------------------------------


def eligibility_violations(submitted_lineups: Sequence[Sequence[dict[str, Any]]]) -> Observed[int]:
    """Count submitted players confirmed ineligible. Any unverified eligibility makes the count unavailable.

    Each lineup is a list of eligibility dicts (the PlayerState eligibility
    shape: ``status`` plus ``verified``). A missing or unverified status is
    not "no violation"; it is unknown, so the metric refuses to report a number.
    """
    violations, unverified = 0, 0
    for lineup in submitted_lineups:
        for record in lineup:
            status = (record or {}).get("status")
            if status not in VERIFIED_STATUSES or not (record or {}).get("verified", False):
                unverified += 1
            elif status in INELIGIBLE_STATUSES:
                violations += 1
    if unverified:
        return Observed.unavailable(ValueStatus.MISSING, "eligibility_violations", f"{unverified} submitted selection(s) had unverified eligibility")
    return Observed.available_value(violations, "eligibility", what="eligibility_violations")


def minutes_plan_deviation(planned: dict[int, int], actual: dict[int, int]) -> Observed[float]:
    """Mean absolute difference between planned and actual minutes over the planned players."""
    if not planned:
        return Observed.unavailable(ValueStatus.MISSING, "minutes_plan_deviation", "no minutes plan")
    missing = sorted(pid for pid in planned if pid not in actual)
    if missing:
        return Observed.unavailable(ValueStatus.MISSING, "minutes_plan_deviation", f"actual minutes unknown for players {missing}")
    total = sum(abs(int(planned[pid]) - int(actual[pid])) for pid in planned)
    return Observed.available_value(total / len(planned), "minutes", what="minutes_plan_deviation")


# ---------------------------------------------------------------------------
# Finance metrics (exact Money)
# ---------------------------------------------------------------------------


def reserve_shortfalls(balances: Sequence[Money], reserve: Money) -> Observed[int]:
    """How many observed balances fell below the declared cash reserve. Units must match exactly."""
    if not balances:
        return Observed.unavailable(ValueStatus.MISSING, "reserve_shortfalls", "no balance observations")
    count = sum(1 for balance in balances if balance < reserve)
    return Observed.available_value(count, "finances", what="reserve_shortfalls")


def forecast_error_money(forecasts: Sequence[Money], actuals: Sequence[Money]) -> Observed[Money]:
    """Mean absolute forecast error as Money (rounded half-even to the minor unit; the total is exact)."""
    if not forecasts or len(forecasts) != len(actuals):
        return Observed.unavailable(ValueStatus.MISSING, "forecast_error", "forecasts and actuals must be non-empty and aligned")
    total = sum_money((abs(f - a) for f, a in zip(forecasts, actuals)), forecasts[0].currency, forecasts[0].period)
    mean = total.times(Fraction(1, len(forecasts)), rounding=ROUND_HALF_EVEN)
    return Observed.available_value(mean, "finances", what="forecast_error")


def forecast_error(forecasts: Sequence[float], actuals: Sequence[float]) -> Observed[float]:
    """Mean absolute error for non-money forecasts (readiness, minutes, points)."""
    if not forecasts or len(forecasts) != len(actuals):
        return Observed.unavailable(ValueStatus.MISSING, "forecast_error", "forecasts and actuals must be non-empty and aligned")
    return Observed.available_value(sum(abs(f - a) for f, a in zip(forecasts, actuals)) / len(forecasts), "forecasts", what="forecast_error")


# ---------------------------------------------------------------------------
# Execution and operations metrics
# ---------------------------------------------------------------------------


def action_counts(results: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Confirmed / uncertain / failed action counts plus duplicate effects.

    ``results`` are ActionResult-shaped dicts with ``outcome`` and optionally
    ``idempotency_key``; a duplicate is a second confirmed effect under one key.
    """
    counts = {"confirmed": 0, "uncertain": 0, "failed": 0, "duplicates": 0, "unknown_outcome": 0}
    confirmed_keys: dict[str, int] = {}
    for result in results:
        outcome = result.get("outcome")
        if outcome == "confirmed":
            counts["confirmed"] += 1
            key = result.get("idempotency_key")
            if key is not None:
                confirmed_keys[key] = confirmed_keys.get(key, 0) + 1
        elif outcome == "uncertain":
            counts["uncertain"] += 1
        elif outcome == "failed":
            counts["failed"] += 1
        else:
            counts["unknown_outcome"] += 1
    counts["duplicates"] = sum(n - 1 for n in confirmed_keys.values() if n > 1)
    return counts


def wall_time(manifests: Sequence[RunManifest]) -> Observed[dict[str, float]]:
    """Total and mean measured wall seconds across runs; unavailable if any run lacks a measurement."""
    if not manifests:
        return Observed.unavailable(ValueStatus.MISSING, "wall_time", "no runs")
    missing = [m.run_id for m in manifests if m.elapsed_wall_seconds is None]
    if missing:
        return Observed.unavailable(ValueStatus.MISSING, "wall_time", f"runs without elapsed time: {missing}")
    total = sum(m.elapsed_wall_seconds for m in manifests)  # type: ignore[misc]
    return Observed.available_value({"total_seconds": total, "mean_seconds": total / len(manifests), "runs": len(manifests)}, "manifests", what="wall_time")


# ---------------------------------------------------------------------------
# Paired comparison with clustered uncertainty
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairedUnit:
    """One matched pair (treatment vs control from the same checkpoint) inside a cluster (run/career)."""

    cluster_id: str
    treatment: float
    control: float

    @property
    def difference(self) -> float:
        return self.treatment - self.control


@dataclass
class ComparisonResult:
    metric: str
    mean_difference: float | None
    ci_low: float | None
    ci_high: float | None
    n_units: int
    n_clusters: int
    alpha: float
    resamples: int
    seed: int
    status: str                      # "available" | "insufficient_clusters" | "no_units"
    note: str = ""
    version: str = EVALUATION_VERSION

    @property
    def available(self) -> bool:
        return self.status == "available"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _cluster_means(units: Sequence[PairedUnit]) -> dict[str, list[float]]:
    clusters: dict[str, list[float]] = {}
    for unit in units:
        clusters.setdefault(unit.cluster_id, []).append(unit.difference)
    return clusters


def paired_comparison(units: Sequence[PairedUnit], *, metric: str, seed: int, resamples: int = DEFAULT_RESAMPLES, alpha: float = DEFAULT_ALPHA) -> ComparisonResult:
    """Mean paired difference with a percentile bootstrap interval over clusters.

    Clusters (careers or runs) are resampled with replacement; units inside a
    cluster travel together so correlated fixtures never inflate the evidence.
    Fewer than :data:`MIN_CLUSTERS_FOR_INTERVAL` clusters gives no interval.
    """
    if not units:
        return ComparisonResult(metric, None, None, None, 0, 0, alpha, resamples, seed, "no_units", "no paired units")
    clusters = _cluster_means(units)
    mean_diff = sum(u.difference for u in units) / len(units)
    if len(clusters) < MIN_CLUSTERS_FOR_INTERVAL:
        return ComparisonResult(metric, mean_diff, None, None, len(units), len(clusters), alpha, resamples, seed, "insufficient_clusters", f"{len(clusters)} cluster(s); interval needs at least {MIN_CLUSTERS_FOR_INTERVAL}")
    rng = random.Random(seed)
    names = sorted(clusters)
    estimates: list[float] = []
    for _ in range(resamples):
        chosen = [clusters[names[rng.randrange(len(names))]] for _ in names]
        pooled = [d for group in chosen for d in group]
        estimates.append(sum(pooled) / len(pooled))
    estimates.sort()
    low = estimates[max(0, math.floor((alpha / 2) * resamples))]
    high = estimates[min(resamples - 1, math.ceil((1 - alpha / 2) * resamples) - 1)]
    return ComparisonResult(metric, mean_diff, low, high, len(units), len(clusters), alpha, resamples, seed, "available", "percentile cluster bootstrap")


# ---------------------------------------------------------------------------
# Power / pilot sizing
# ---------------------------------------------------------------------------


def required_units(smallest_useful_effect: float, sd: float, *, alpha: float = DEFAULT_ALPHA, power: float = DEFAULT_POWER, paired: bool = True) -> int:
    """Units needed to detect ``smallest_useful_effect`` with a normal approximation.

    ``sd`` is the standard deviation of the paired difference (paired design)
    or of one arm (unpaired, in which case the count is per arm). This is a
    pilot-sizing heuristic: the pilot supplies ``sd``, and clustering must be
    handled by counting clusters, not fixtures, as units.
    """
    if smallest_useful_effect <= 0 or sd <= 0:
        raise ValueError("effect and sd must be positive")
    if not (0 < alpha < 1 and 0 < power < 1):
        raise ValueError("alpha and power must lie in (0, 1)")
    normal = statistics.NormalDist()
    z = normal.inv_cdf(1 - alpha / 2) + normal.inv_cdf(power)
    n = (z * sd / smallest_useful_effect) ** 2
    if not paired:
        n *= 2
    return int(math.ceil(n))


def pilot_sd(differences: Sequence[float]) -> Observed[float]:
    """Sample standard deviation of pilot paired differences; unavailable below two observations."""
    if len(differences) < 2:
        return Observed.unavailable(ValueStatus.MISSING, "pilot_sd", "need at least two pilot differences")
    return Observed.available_value(statistics.stdev(differences), "pilot", what="pilot_sd")


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def classify_result(ci_low: float | None, ci_high: float | None, threshold: float, guardrails_degraded: bool, *, underpowered: bool = False) -> str:
    """One of improvement / no_improvement / inconclusive / blocked_by_guardrail.

    A guardrail degradation blocks regardless of the sporting result. An
    improvement claim needs the whole interval above ``threshold``; a
    no-improvement verdict needs the whole interval at or below it; anything
    else, an underpowered design, or a missing interval is inconclusive.
    """
    if guardrails_degraded:
        return RESULT_BLOCKED
    if underpowered or ci_low is None or ci_high is None:
        return RESULT_INCONCLUSIVE
    if ci_low > threshold:
        return RESULT_IMPROVEMENT
    if ci_high <= threshold:
        return RESULT_NO_IMPROVEMENT
    return RESULT_INCONCLUSIVE


@dataclass
class GateReport:
    comparison: ComparisonResult
    threshold: float
    guardrails: dict[str, bool]          # guardrail name -> degraded?
    required: int | None
    verdict: str
    reasons: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"comparison": self.comparison.to_json(), "threshold": self.threshold, "guardrails": dict(self.guardrails), "required_clusters": self.required, "verdict": self.verdict, "reasons": list(self.reasons)}


def evidence_gate(comparison: ComparisonResult, *, threshold: float, guardrails: dict[str, bool], required_clusters: int | None = None) -> GateReport:
    """Apply the 13.5 gate: guardrails, power, then the prespecified interval against the threshold."""
    degraded = [name for name, bad in guardrails.items() if bad]
    reasons: list[str] = []
    if degraded:
        reasons.append(f"guardrails degraded: {degraded}")
    underpowered = required_clusters is not None and comparison.n_clusters < required_clusters
    if underpowered:
        reasons.append(f"{comparison.n_clusters} clusters below the {required_clusters} required")
    if not comparison.available:
        reasons.append(f"comparison {comparison.status}: {comparison.note}")
    verdict = classify_result(comparison.ci_low, comparison.ci_high, threshold, bool(degraded), underpowered=underpowered)
    return GateReport(comparison, threshold, dict(guardrails), required_clusters, verdict, reasons)


# ---------------------------------------------------------------------------
# Validity split
# ---------------------------------------------------------------------------


@dataclass
class ValiditySplit:
    """Technically invalid trials are excluded; poor results stay in the analysis set, only flagged."""

    analysable: list[str]
    technically_invalid: dict[str, list[str]]     # trial id -> reasons
    poor_performance: list[str]                   # subset of analysable
    undecided: list[str]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def validity_split(trials: Iterable[TrialManifest], *, poor_performance: Callable[[dict[str, Any]], bool] | None = None) -> ValiditySplit:
    """Separate wrong-save/failed-treatment trials from trials that merely went badly."""
    analysable, invalid, poor, undecided = [], {}, [], []
    for trial in trials:
        if trial.technically_valid is None:
            undecided.append(trial.trial_id)
            continue
        if not trial.technically_valid:
            invalid[trial.trial_id] = list(trial.invalidity_reasons)
            continue
        analysable.append(trial.trial_id)
        if poor_performance is not None and poor_performance(trial.outcomes):
            poor.append(trial.trial_id)
    return ValiditySplit(analysable, invalid, poor, undecided)
