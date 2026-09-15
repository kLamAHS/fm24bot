"""Calibration and proper scoring rules (spec 6.3, 13.4, BOT 012).

Predicted probabilities (a win, an injury-free week, an accepted offer) are
scored on held-out outcomes with proper scoring rules and compared against a
reference baseline. A :class:`CalibrationReport` records pass/fail against
thresholds *supplied by the caller*: there is no universal acceptable Brier
score, because a coin-flip league and a lopsided cup tie have different
irreducible error. :data:`PROPOSED_THRESHOLDS` documents defaults as a
proposal only; the release gate must be given explicit numbers.

Empty reliability bins report ``observed_frequency=None`` rather than 0:
an unobserved bin has no frequency.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

CALIBRATION_VERSION = "calibration-1.0"


class ScoringError(ValueError):
    """Inputs are inconsistent (length mismatch, probability outside [0, 1], outcome not 0/1)."""


def _check_pairs(probs: Sequence[float], outcomes: Sequence[int]) -> None:
    if len(probs) != len(outcomes):
        raise ScoringError("probabilities and outcomes differ in length")
    for p, y in zip(probs, outcomes):
        if not 0.0 <= p <= 1.0:
            raise ScoringError(f"probability {p} outside [0, 1]")
        if y not in (0, 1):
            raise ScoringError(f"outcome {y!r} is not 0 or 1; a missing outcome must be excluded, not coded")


def brier_score(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean squared error between predicted probability and the 0/1 outcome (lower is better)."""
    _check_pairs(probs, outcomes)
    if not probs:
        raise ScoringError("no forecasts to score")
    return sum((p - y) ** 2 for p, y in zip(probs, outcomes)) / len(probs)


def log_score(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Mean negative log likelihood (lower is better). A certain wrong forecast scores ``inf``; nothing is clipped."""
    _check_pairs(probs, outcomes)
    if not probs:
        raise ScoringError("no forecasts to score")
    total = 0.0
    for p, y in zip(probs, outcomes):
        q = p if y == 1 else 1.0 - p
        if q <= 0.0:
            return math.inf
        total -= math.log(q)
    return total / len(probs)


@dataclass(frozen=True)
class ReliabilityBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float | None
    observed_frequency: float | None


def reliability_table(probs: Sequence[float], outcomes: Sequence[int], bins: int = 10) -> list[ReliabilityBin]:
    """Group forecasts into equal-width probability bins; bins with no forecasts have ``None`` statistics."""
    _check_pairs(probs, outcomes)
    if bins < 1:
        raise ScoringError("bins must be at least 1")
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for p, y in zip(probs, outcomes):
        buckets[min(int(p * bins), bins - 1)].append((p, y))
    table = []
    for i, items in enumerate(buckets):
        lower, upper = i / bins, (i + 1) / bins
        if not items:
            table.append(ReliabilityBin(lower, upper, 0, None, None))
            continue
        table.append(ReliabilityBin(lower, upper, len(items), sum(p for p, _ in items) / len(items), sum(y for _, y in items) / len(items)))
    return table


def expected_calibration_error(probs: Sequence[float], outcomes: Sequence[int], bins: int = 10) -> float:
    """Count-weighted mean absolute gap between predicted and observed frequency across bins."""
    table = reliability_table(probs, outcomes, bins)
    n = sum(b.count for b in table)
    if n == 0:
        raise ScoringError("no forecasts to score")
    return sum(b.count * abs(b.mean_predicted - b.observed_frequency) for b in table if b.count) / n


@dataclass(frozen=True)
class CoverageResult:
    covered: int
    scored: int
    excluded: int              # intervals that were unavailable; they are not counted as misses

    @property
    def coverage(self) -> float | None:
        return None if self.scored == 0 else self.covered / self.scored


def interval_coverage(intervals: Sequence[tuple[float, float] | None], outcomes: Sequence[float | None]) -> CoverageResult:
    """Share of realised outcomes inside their forecast interval. Unavailable intervals or outcomes are excluded, not counted as misses."""
    if len(intervals) != len(outcomes):
        raise ScoringError("intervals and outcomes differ in length")
    covered = scored = excluded = 0
    for interval, y in zip(intervals, outcomes):
        if interval is None or y is None:
            excluded += 1
            continue
        low, high = interval
        if low > high:
            raise ScoringError(f"interval {interval} has low above high")
        scored += 1
        covered += int(low <= y <= high)
    return CoverageResult(covered, scored, excluded)


@dataclass(frozen=True)
class ScoringComparison:
    """Candidate against reference on the same outcomes. ``skill`` is ``1 - candidate/reference`` (positive means better)."""

    candidate_brier: float
    reference_brier: float
    candidate_log: float
    reference_log: float

    @property
    def brier_skill(self) -> float | None:
        if self.reference_brier == 0.0:
            return None
        return 1.0 - self.candidate_brier / self.reference_brier

    @property
    def beats_reference(self) -> bool:
        return self.candidate_brier < self.reference_brier


def compare_to_reference(candidate: Sequence[float], reference: Sequence[float], outcomes: Sequence[int]) -> ScoringComparison:
    return ScoringComparison(brier_score(candidate, outcomes), brier_score(reference, outcomes), log_score(candidate, outcomes), log_score(reference, outcomes))


@dataclass(frozen=True)
class CalibrationThresholds:
    """Caller-supplied pass criteria. Every field is required; ``None`` disables that criterion explicitly."""

    max_brier: float | None
    max_ece: float | None
    min_samples: int
    min_brier_skill: float | None = None       # requires a reference forecast when set
    min_interval_coverage: float | None = None  # requires intervals when set
    bins: int = 10

    def to_json(self) -> dict[str, Any]:
        return {"max_brier": self.max_brier, "max_ece": self.max_ece, "min_samples": self.min_samples, "min_brier_skill": self.min_brier_skill, "min_interval_coverage": self.min_interval_coverage, "bins": self.bins}


# Proposal only: a release gate must pass its own thresholds explicitly.
PROPOSED_THRESHOLDS = CalibrationThresholds(max_brier=0.25, max_ece=0.05, min_samples=200, min_brier_skill=0.0, min_interval_coverage=None)


@dataclass
class CalibrationReport:
    """Outcome of scoring one model on one held-out set against explicit thresholds.

    ``status`` is ``passed``, ``failed`` or ``inconclusive`` (too few samples
    to judge). Only ``passed`` clears a release gate.
    """

    model_id: str
    version: str
    samples: int
    brier: float | None
    log: float | None
    ece: float | None
    coverage: float | None
    brier_skill: float | None
    thresholds: dict[str, Any]
    status: str
    failures: list[str] = field(default_factory=list)
    evaluation_scope: dict[str, Any] = field(default_factory=dict)
    calibration_version: str = CALIBRATION_VERSION

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_json(self) -> dict[str, Any]:
        return {"model_id": self.model_id, "version": self.version, "samples": self.samples, "brier": self.brier, "log": self.log, "ece": self.ece, "coverage": self.coverage, "brier_skill": self.brier_skill, "thresholds": self.thresholds, "status": self.status, "failures": list(self.failures), "evaluation_scope": self.evaluation_scope, "calibration_version": self.calibration_version}


def evaluate_calibration(model_id: str, version: str, probs: Sequence[float], outcomes: Sequence[int], thresholds: CalibrationThresholds, *, reference: Sequence[float] | None = None, intervals: Sequence[tuple[float, float] | None] | None = None, interval_outcomes: Sequence[float | None] | None = None, evaluation_scope: dict[str, Any] | None = None) -> CalibrationReport:
    """Score ``probs`` on held-out ``outcomes`` and judge them against ``thresholds``."""
    _check_pairs(probs, outcomes)
    n = len(probs)
    scope = dict(evaluation_scope or {})
    if n < thresholds.min_samples:
        return CalibrationReport(model_id, version, n, None, None, None, None, None, thresholds.to_json(), "inconclusive", [f"{n} samples below minimum {thresholds.min_samples}"], scope)
    failures: list[str] = []
    brier = brier_score(probs, outcomes)
    log = log_score(probs, outcomes)
    ece = expected_calibration_error(probs, outcomes, thresholds.bins)
    if thresholds.max_brier is not None and brier > thresholds.max_brier:
        failures.append(f"brier {brier:.4f} above {thresholds.max_brier}")
    if thresholds.max_ece is not None and ece > thresholds.max_ece:
        failures.append(f"ece {ece:.4f} above {thresholds.max_ece}")
    skill = None
    if thresholds.min_brier_skill is not None:
        if reference is None:
            failures.append("min_brier_skill set but no reference forecast supplied")
        else:
            skill = compare_to_reference(probs, reference, outcomes).brier_skill
            if skill is None or skill < thresholds.min_brier_skill:
                failures.append(f"brier skill {skill} below {thresholds.min_brier_skill}")
    coverage = None
    if thresholds.min_interval_coverage is not None:
        if intervals is None or interval_outcomes is None:
            failures.append("min_interval_coverage set but no intervals supplied")
        else:
            coverage = interval_coverage(intervals, interval_outcomes).coverage
            if coverage is None or coverage < thresholds.min_interval_coverage:
                failures.append(f"interval coverage {coverage} below {thresholds.min_interval_coverage}")
    return CalibrationReport(model_id, version, n, brier, log, ece, coverage, skill, thresholds.to_json(), "failed" if failures else "passed", failures, scope)
