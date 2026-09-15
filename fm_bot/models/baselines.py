"""Transparent forecasting baselines (spec 6.3, 13.4, 17.3).

Every advanced model in the bot is compared against one of these baselines.
They are deliberately simple: rolling averages, exponentially weighted
averages and an opponent-adjusted ridge regression solved in pure Python.

A :class:`Forecast` always states its training scope, uncertainty method,
known missing inputs and validity period. A forecast whose inputs are not
available is returned with ``status="unavailable"`` and a reason; that is a
valid result and callers must not replace it with a confident number just
to satisfy an interface (spec 17.3).

Everything here is a *baseline*. The intervals are heuristic prediction
intervals built from sample dispersion, not calibrated posteriors; they are
labelled as such in ``uncertainty_method``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..state.status import Observed

BASELINE_VERSION = "baselines-1.0"

# Reviewable constants. The z-multiplier turns a sample standard deviation
# into a symmetric heuristic interval; it is not a calibrated quantile.
INTERVAL_Z = 1.96
MIN_POINTS_FOR_DISPERSION = 2
DEFAULT_RIDGE_LAMBDA = 0.1

# Widening factors applied to interval half-widths when the context differs
# from the training scope (spec 6.3: widen uncertainty or defer). Multiplicative.
UNFAMILIAR_LEAGUE_WIDEN = 1.5
UNFAMILIAR_CLUB_WIDEN = 1.25
CHANGED_BUILD_WIDEN = 2.0


@dataclass(frozen=True)
class Forecast:
    """A point forecast with its provenance.

    ``point`` and ``interval`` are ``None`` whenever ``status`` is
    ``"unavailable"``; ``reason`` then says which input was missing.
    """

    point: float | None
    interval: tuple[float, float] | None
    method: str
    training_scope: dict[str, Any] = field(default_factory=dict)
    uncertainty_method: str = "none"
    known_missing_inputs: list[str] = field(default_factory=list)
    validity_period: dict[str, Any] = field(default_factory=dict)
    model_version: str = BASELINE_VERSION
    status: str = "available"
    reason: str | None = None
    forecast_target: str | None = None

    def __post_init__(self):
        if self.status not in ("available", "unavailable"):
            raise ValueError(f"forecast status must be available or unavailable, got {self.status!r}")
        if self.status == "unavailable" and (self.point is not None or self.interval is not None):
            raise ValueError("an unavailable forecast cannot carry a point or interval")
        if self.status == "available" and self.point is None:
            raise ValueError("an available forecast needs a point estimate")

    @property
    def available(self) -> bool:
        return self.status == "available"

    def widened(self, factor: float, note: str) -> "Forecast":
        """Return a copy whose interval half-widths are multiplied by ``factor``."""
        if not self.available or self.interval is None or factor == 1.0:
            return self
        low, high = self.interval
        assert self.point is not None
        return Forecast(self.point, (self.point - (self.point - low) * factor, self.point + (high - self.point) * factor), self.method, dict(self.training_scope), f"{self.uncertainty_method}; widened x{factor:g} ({note})", list(self.known_missing_inputs), dict(self.validity_period), self.model_version, self.status, self.reason, self.forecast_target)

    def to_json(self) -> dict[str, Any]:
        return {
            "point_estimate": self.point, "interval": list(self.interval) if self.interval else None, "method": self.method,
            "training_scope": self.training_scope, "uncertainty_method": self.uncertainty_method,
            "known_missing_inputs": list(self.known_missing_inputs), "validity_period": self.validity_period,
            "model_version": self.model_version, "status": self.status, "reason": self.reason, "forecast_target": self.forecast_target,
        }


def unavailable_forecast(method: str, reason: str, *, missing: Iterable[str] = (), target: str | None = None, scope: dict[str, Any] | None = None) -> Forecast:
    return Forecast(None, None, method, dict(scope or {}), "none", list(missing), {}, BASELINE_VERSION, "unavailable", reason, target)


# ----- descriptive statistics (pure python) -----

def _split_known(series: Sequence[float | None]) -> tuple[list[float], int]:
    known = [float(v) for v in series if v is not None]
    return known, len(series) - len(known)


def sample_std(values: Sequence[float]) -> float | None:
    """Unbiased sample standard deviation; ``None`` with fewer than two values."""
    if len(values) < MIN_POINTS_FOR_DISPERSION:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _interval(point: float, dispersion: float | None) -> tuple[tuple[float, float] | None, str]:
    if dispersion is None:
        return None, "none: fewer than two points, no dispersion estimate"
    return (point - INTERVAL_Z * dispersion, point + INTERVAL_Z * dispersion), f"heuristic: sample_std x {INTERVAL_Z}"


def rolling_average(series: Sequence[float | None], window: int, *, target: str | None = None, scope: dict[str, Any] | None = None) -> Forecast:
    """Mean of the last ``window`` known points (e.g. points per match). Missing entries are excluded, never zeroed."""
    if window < 1:
        raise ValueError("window must be at least 1")
    known, missing = _split_known(series[-window:])
    if not known:
        return unavailable_forecast("rolling_average", "no known points inside the window", missing=[f"{missing} missing values in window"] if missing else [], target=target, scope=scope)
    point = sum(known) / len(known)
    interval, method = _interval(point, sample_std(known))
    return Forecast(point, interval, "rolling_average", dict(scope or {}), method, [f"{missing} missing values in window"] if missing else [], {"window": window, "points_used": len(known)}, BASELINE_VERSION, "available", None, target)


def exponentially_weighted_average(series: Sequence[float | None], alpha: float, *, target: str | None = None, scope: dict[str, Any] | None = None) -> Forecast:
    """EWMA with smoothing ``alpha`` in (0, 1]; recent matches count more. Missing entries are skipped."""
    if not 0.0 < alpha <= 1.0:
        raise ValueError("alpha must be in (0, 1]")
    known, missing = _split_known(series)
    if not known:
        return unavailable_forecast("ewma", "no known points in the series", target=target, scope=scope)
    level = known[0]
    for value in known[1:]:
        level = alpha * value + (1.0 - alpha) * level
    residuals = [v - level for v in known]
    interval, method = _interval(level, sample_std(residuals))
    return Forecast(level, interval, "ewma", dict(scope or {}), method, [f"{missing} missing values skipped"] if missing else [], {"alpha": alpha, "points_used": len(known)}, BASELINE_VERSION, "available", None, target)


# ----- pure python linear algebra -----

def solve_linear(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting. Raises ``ValueError`` on a singular system."""
    n = len(matrix)
    a = [list(map(float, row)) + [float(rhs[i])] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("singular system; add a ridge penalty or remove collinear columns")
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(col + 1, n):
            factor = a[r][col] / a[col][col]
            if factor:
                for c in range(col, n + 1):
                    a[r][c] -= factor * a[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (a[r][n] - sum(a[r][c] * x[c] for c in range(r + 1, n))) / a[r][r]
    return x


def ridge_regression(design: list[list[float]], targets: list[float], ridge_lambda: float, *, penalize_intercept: bool = False) -> list[float]:
    """Solve ``(X'X + lambda I) b = X'y``. Column 0 is treated as the intercept unless ``penalize_intercept``."""
    if ridge_lambda < 0:
        raise ValueError("ridge_lambda must be non-negative")
    if not design:
        raise ValueError("empty design matrix")
    p = len(design[0])
    xtx = [[sum(row[i] * row[j] for row in design) for j in range(p)] for i in range(p)]
    xty = [sum(row[i] * y for row, y in zip(design, targets)) for i in range(p)]
    for i in range(p):
        if i == 0 and not penalize_intercept:
            continue
        xtx[i][i] += ridge_lambda
    return solve_linear(xtx, xty)


@dataclass
class OpponentAdjustedFit:
    """Ridge fit of ``outcome = intercept + home + team_strength - opponent_strength`` on match rows.

    A baseline, not a rating system: strengths are shrunk toward zero by the
    explicit ``ridge_lambda`` so that teams with few matches do not get
    extreme ratings.
    """

    intercept: float
    home_effect: float
    team_strength: dict[Any, float]
    residual_std: float | None
    ridge_lambda: float
    rows_used: int
    training_scope: dict[str, Any] = field(default_factory=dict)

    def predict(self, team: Any, opponent: Any, home: bool, *, target: str | None = None) -> Forecast:
        missing = [f"team {name} not in training rows" for name in (team, opponent) if name not in self.team_strength]
        strength = self.team_strength.get(team, 0.0) - self.team_strength.get(opponent, 0.0)
        point = self.intercept + (self.home_effect if home else 0.0) + strength
        interval, method = _interval(point, self.residual_std)
        forecast = Forecast(point, interval, "opponent_adjusted_regression", dict(self.training_scope), method, missing, {"rows_used": self.rows_used}, BASELINE_VERSION, "available", None, target)
        if missing:
            return forecast.widened(UNFAMILIAR_CLUB_WIDEN, "unseen team shrunk to average strength")
        return forecast


def opponent_adjusted_regression(rows: Sequence[dict[str, Any]], ridge_lambda: float = DEFAULT_RIDGE_LAMBDA, *, scope: dict[str, Any] | None = None) -> OpponentAdjustedFit | None:
    """Fit strengths from rows ``{"team", "opponent", "home": bool, "outcome": float}``.

    Rows whose outcome is ``None`` are skipped (a missing result is not a
    draw). Returns ``None`` when no usable rows exist.
    """
    usable = [r for r in rows if r.get("outcome") is not None]
    if not usable:
        return None
    teams = sorted({r["team"] for r in usable} | {r["opponent"] for r in usable}, key=repr)
    index = {t: i for i, t in enumerate(teams)}
    design = []
    for r in usable:
        row = [1.0, 1.0 if r.get("home") else 0.0] + [0.0] * len(teams)
        row[2 + index[r["team"]]] += 1.0
        row[2 + index[r["opponent"]]] -= 1.0
        design.append(row)
    targets = [float(r["outcome"]) for r in usable]
    beta = ridge_regression(design, targets, ridge_lambda)
    residuals = [y - sum(b * x for b, x in zip(beta, row)) for row, y in zip(design, targets)]
    return OpponentAdjustedFit(beta[0], beta[1], {t: beta[2 + i] for t, i in index.items()}, sample_std(residuals), ridge_lambda, len(usable), dict(scope or {}))


# ----- unfamiliar context -----

@dataclass(frozen=True)
class UnfamiliarityReport:
    widen_factor: float
    reasons: list[str]

    @property
    def unfamiliar(self) -> bool:
        return self.widen_factor != 1.0


def unfamiliar_context(context: dict[str, Any], training_scope: dict[str, Any]) -> UnfamiliarityReport:
    """Compare ``{"league", "club", "build"}`` against ``{"leagues", "clubs", "builds"}`` and return a widening factor.

    Widening is the conservative response of spec 6.3; deferring to a
    supported baseline is the caller's alternative. A context key absent from
    ``context`` is unknown and treated as unfamiliar rather than familiar.
    """
    factor, reasons = 1.0, []
    for key, plural, widen in (("league", "leagues", UNFAMILIAR_LEAGUE_WIDEN), ("club", "clubs", UNFAMILIAR_CLUB_WIDEN), ("build", "builds", CHANGED_BUILD_WIDEN)):
        value = context.get(key)
        if value is None:
            factor *= widen
            reasons.append(f"{key} unknown")
        elif value not in (training_scope.get(plural) or []):
            factor *= widen
            reasons.append(f"{key} {value!r} not in training scope")
    return UnfamiliarityReport(factor, reasons)


# ----- fixture-level baselines -----

def result_forecast(points_per_match: Sequence[float | None], fixture: dict[str, Any], training_scope: dict[str, Any], *, window: int = 6) -> Forecast:
    """Baseline expected points for a fixture from recent points per match.

    ``fixture`` carries ``{"date", "league", "club", "build"}``; the forecast
    is valid for that date only. Unfamiliar league, club or build widens the
    heuristic interval by :func:`unfamiliar_context`.
    """
    base = rolling_average(points_per_match, window, target="points_at_fixture", scope=training_scope)
    report = unfamiliar_context(fixture, training_scope)
    forecast = base.widened(report.widen_factor, "; ".join(report.reasons)) if base.available else base
    validity = dict(forecast.validity_period)
    validity["fixture_date"] = fixture.get("date")
    return Forecast(forecast.point, forecast.interval, forecast.method, forecast.training_scope, forecast.uncertainty_method, forecast.known_missing_inputs, validity, forecast.model_version, forecast.status, forecast.reason, forecast.forecast_target)


def readiness_forecast(condition: Observed, days_until_fixture: int, recovery_per_day: float, *, target: str = "validated_readiness_at_next_fixture") -> Forecast:
    """Recovery-curve baseline for readiness at the next fixture.

    Only a *current* condition observation is a measurement; a stale or
    missing one yields an unavailable forecast (spec 17.3 example). The
    recovery rate is a caller-provided empirical baseline slope.
    """
    if not condition.available:
        return unavailable_forecast("recovery_baseline", "required_current_readiness_observation_missing", missing=[f"condition:{condition.status.value}"], target=target)
    if days_until_fixture < 0:
        raise ValueError("days_until_fixture must be non-negative")
    point = min(100.0, float(condition.require()) + recovery_per_day * days_until_fixture)
    return Forecast(point, None, "recovery_baseline", {}, "none: deterministic curve, no dispersion estimate", [], {"days_ahead": days_until_fixture}, BASELINE_VERSION, "available", None, target)
