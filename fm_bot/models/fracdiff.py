"""Fractional differentiation experiment for longitudinal financial features (spec 8.4, 13.3).

The transform is ``z[t] = sum_{k=0..K} w[k] * series[t-k]`` with
``w[0] = 1`` and ``w[k] = -w[k-1] * (d-k+1) / k`` over a finite window K.
The order ``d`` is chosen *inside the training fold only*
(:func:`choose_d_within_fold`), because a differencing parameter tuned on
the evaluation period is leakage (spec 13.3).

Guards:

* :func:`log_levels` refuses logarithms of nonpositive balances instead of
  clipping them;
* :func:`dedupe_unchanged_balances` collapses repeated observations of an
  unchanged balance, which are the same fact seen twice and not independent
  financial samples.

:func:`admission_test` is the experiment's gate. It compares out-of-sample
forecast error for ``d=0`` (levels), ``d=1`` (changes), a seasonal
difference and the fractional candidates, and returns
``keep_accounting_baseline`` unless the best fractional order clears the
caller's margin. The explicit accounting ledger remains the baseline.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

FRACDIFF_VERSION = "fracdiff-1.0"

DEFAULT_WINDOW = 10
MIN_TRAIN_POINTS = 12       # fewer distinct balance points than this: history too short, keep baseline
FORECAST_MEMORY = 3         # rolling-mean length used to forecast the transformed series


class TransformError(ValueError):
    pass


def fracdiff_weights(d: float, window: int) -> list[float]:
    """Weights ``w[0..window]`` for order ``d``. ``d=0`` returns the identity, ``d=1`` a first difference."""
    if window < 0:
        raise TransformError("window must be non-negative")
    weights = [1.0]
    for k in range(1, window + 1):
        weights.append(-weights[-1] * (d - k + 1) / k)
    return weights


def transform(series: Sequence[float], d: float, window: int) -> list[float | None]:
    """Apply the fractional difference. The first ``window`` entries are ``None`` (insufficient history), not zero."""
    weights = fracdiff_weights(d, window)
    out: list[float | None] = [None] * min(window, len(series))
    for t in range(window, len(series)):
        out.append(sum(w * series[t - k] for k, w in enumerate(weights)))
    return out


def seasonal_difference(series: Sequence[float], period: int) -> list[float | None]:
    if period < 1:
        raise TransformError("seasonal period must be positive")
    return [None] * min(period, len(series)) + [series[t] - series[t - period] for t in range(period, len(series))]


@dataclass(frozen=True)
class LogResult:
    status: str                      # available | refused
    values: list[float] | None
    reason: str | None = None


def log_levels(series: Sequence[float]) -> LogResult:
    """Natural log of balances; refused outright when any balance is zero or negative (an overdrawn club has no log balance)."""
    bad = [i for i, v in enumerate(series) if v <= 0]
    if bad:
        return LogResult("refused", None, f"nonpositive balances at positions {bad[:5]}; logarithm undefined")
    return LogResult("available", [math.log(v) for v in series])


@dataclass(frozen=True)
class DedupeResult:
    points: list[tuple[str, float]]
    dropped: int
    reason: str


def dedupe_unchanged_balances(observations: Sequence[tuple[str, float]]) -> DedupeResult:
    """Keep the first observation of each balance level in date order; repeats of an unchanged balance are dropped.

    Two snapshots of the same ledger are one financial fact. Counting them
    twice would shrink every variance estimate and flatter any forecast.
    """
    ordered = sorted(observations, key=lambda item: item[0])
    kept: list[tuple[str, float]] = []
    for date, value in ordered:
        if kept and kept[-1][1] == value:
            continue
        kept.append((date, value))
    dropped = len(ordered) - len(kept)
    return DedupeResult(kept, dropped, f"{dropped} repeated unchanged balances dropped; they are not independent samples")


# ----- forecasting on transformed scales -----

def _forecast_next_level(series: Sequence[float], candidate: str, d: float | None, window: int, seasonal_period: int) -> float | None:
    """One-step level forecast: forecast the transformed series by a short rolling mean, then invert to the level."""
    n = len(series)
    if candidate == "seasonal":
        diffs = [v for v in seasonal_difference(series, seasonal_period) if v is not None]
        if len(diffs) < 1 or n < seasonal_period:
            return None
        return series[n - seasonal_period] + sum(diffs[-FORECAST_MEMORY:]) / len(diffs[-FORECAST_MEMORY:])
    assert d is not None
    z = [v for v in transform(series, d, window) if v is not None]
    if len(z) < 1:
        return None
    z_hat = sum(z[-FORECAST_MEMORY:]) / len(z[-FORECAST_MEMORY:])
    weights = fracdiff_weights(d, window)
    return z_hat - sum(weights[k] * series[n - k] for k in range(1, window + 1))


def _rolling_forecast_error(series: Sequence[float], start: int, candidate: str, d: float | None, window: int, seasonal_period: int) -> float | None:
    """Mean absolute one-step error over ``series[start:]`` using only earlier points for each forecast."""
    errors = []
    for t in range(start, len(series)):
        forecast = _forecast_next_level(series[:t], candidate, d, window, seasonal_period)
        if forecast is None:
            continue
        errors.append(abs(forecast - series[t]))
    return sum(errors) / len(errors) if errors else None


@dataclass
class FoldChoice:
    d: float | None
    status: str                                   # chosen | history_too_short
    errors: dict[str, float | None] = field(default_factory=dict)
    train_points: int = 0
    reason: str | None = None


def choose_d_within_fold(train_series: Sequence[float], candidates: Sequence[float], *, window: int = DEFAULT_WINDOW, seasonal_period: int | None = None) -> FoldChoice:
    """Select ``d`` by one-step forecast error inside the training fold only. The evaluation fold is never consulted."""
    n = len(train_series)
    if n < max(MIN_TRAIN_POINTS, window + 2):
        return FoldChoice(None, "history_too_short", {}, n, f"{n} training points below {max(MIN_TRAIN_POINTS, window + 2)}")
    start = max(window + 1, n // 2)
    errors: dict[str, float | None] = {}
    for d in candidates:
        errors[f"d={d:g}"] = _rolling_forecast_error(train_series, start, "fractional", d, window, seasonal_period or 1)
    scored = {k: v for k, v in errors.items() if v is not None}
    if not scored:
        return FoldChoice(None, "history_too_short", errors, n, "no candidate produced a forecast inside the fold")
    best = min(scored, key=scored.get)
    return FoldChoice(float(best[2:]), "chosen", errors, n)


@dataclass
class AdmissionResult:
    decision: str                                 # keep_accounting_baseline | admit_fractional
    chosen_d: float | None
    test_errors: dict[str, float | None]
    improvement: float | None                     # relative error reduction of the fractional candidate over the best of d=0, d=1, seasonal
    margin: float
    reason: str
    fold: FoldChoice | None = None
    fracdiff_version: str = FRACDIFF_VERSION


def admission_test(series: Sequence[float], fractional_candidates: Sequence[float], margin: float, *, window: int = DEFAULT_WINDOW, seasonal_period: int | None = None, holdout_fraction: float = 0.3) -> AdmissionResult:
    """Compare d=0, d=1, seasonal and fractional forecasts out of sample. Admit only if the improvement clears ``margin``.

    ``margin`` is a caller-provided relative error reduction (e.g. 0.1 for 10%).
    A stationarity statistic alone does not admit the transform (spec 8.4).
    """
    if not 0.0 < holdout_fraction < 1.0:
        raise TransformError("holdout_fraction must be in (0, 1)")
    if margin < 0.0:
        raise TransformError("margin must be non-negative")
    split = int(len(series) * (1.0 - holdout_fraction))
    train, full = series[:split], series
    fold = choose_d_within_fold(train, fractional_candidates, window=window, seasonal_period=seasonal_period)
    if fold.status != "chosen":
        return AdmissionResult("keep_accounting_baseline", None, {}, None, margin, f"history too short: {fold.reason}", fold)
    errors: dict[str, float | None] = {
        "d=0": _rolling_forecast_error(full, split, "fractional", 0.0, window, 1),
        "d=1": _rolling_forecast_error(full, split, "fractional", 1.0, window, 1),
    }
    if seasonal_period:
        errors["seasonal"] = _rolling_forecast_error(full, split, "seasonal", None, window, seasonal_period)
    assert fold.d is not None
    errors[f"fractional d={fold.d:g}"] = _rolling_forecast_error(full, split, "fractional", fold.d, window, 1)
    baseline_scores = [v for k, v in errors.items() if not k.startswith("fractional") and v is not None]
    fractional = errors[f"fractional d={fold.d:g}"]
    if not baseline_scores or fractional is None:
        return AdmissionResult("keep_accounting_baseline", fold.d, errors, None, margin, "holdout too short to score every candidate", fold)
    best_baseline = min(baseline_scores)
    improvement = 0.0 if best_baseline == 0.0 else (best_baseline - fractional) / best_baseline
    if improvement > margin:
        return AdmissionResult("admit_fractional", fold.d, errors, improvement, margin, f"fractional d={fold.d:g} reduced holdout error by {improvement:.1%} (> margin {margin:.1%})", fold)
    return AdmissionResult("keep_accounting_baseline", fold.d, errors, improvement, margin, f"improvement {improvement:.1%} does not clear margin {margin:.1%}", fold)
