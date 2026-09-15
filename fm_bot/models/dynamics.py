"""Fatigue and recovery dynamics experiment (spec 10.1, 10.2).

Everything in this module is an *experiment* competing against the
empirical recovery curve baseline; nothing here is the default training
scheduler. The candidate models are:

* :class:`DiscreteFatigueModel` - daily update ``F[t+1] = F[t] + alpha*workload - beta*F[t]``
  with match-load jumps;
* :class:`ContinuousFatigueModel` - the ODE ``dF/dt = alpha*workload - beta*F``
  integrated with fixed-step explicit Euler, match load added as a jump;
* :class:`ObservationModel` - a linear map with offset from latent fatigue
  ``F`` to the bridge's condition percentage.

Measurements: the bridge's condition percentage is a *stale-cached* value
unless the readiness freshness check reports ``current``. Only current
observations are used as measurements (:func:`collect_measurements`); stale
and missing ones are listed as rejected, never interpolated.

Load records: one row per consecutive day. A gap in the record is a
missing workload, never an assumed rest day, so a gapped, duplicated or
out-of-order schedule is refused (:func:`load_record_gaps`) instead of being
stepped over as if the logged days were consecutive.

Identifiability: :func:`fit_parameters` runs a bounded grid and coordinate
search and then asks whether the data can tell parameter sets apart. If the
objective is flat within tolerance over a wide region, or there are too few
informative observations, the result is ``unidentifiable`` and no
player-specific parameters are produced. Pooling across players with
:func:`pooled_prior` and :func:`individual_estimate` keeps individual
uncertainty explicit rather than pretending to know each player.

Heuristic thresholds below are module constants with a version string.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..state.status import Observed, ValueStatus
from ..state.views import readiness_observed
from .baselines import ridge_regression, sample_std

DYNAMICS_VERSION = "dynamics-1.0"

MIN_INFORMATIVE_OBSERVATIONS = 6       # measurements needed before any fit is attempted
FLAT_OBJECTIVE_TOLERANCE = 0.05        # relative: grid points within 5% of the best objective are "as good"
DEFAULT_MEASUREMENT_RESOLUTION = 1.0   # condition is displayed as whole percentage points; fits closer than this are indistinguishable
IDENTIFIABLE_SPREAD_FRACTION = 0.5     # near-optimal set spanning > 50% of a bound range -> unidentifiable
GRID_STEPS = 7                         # per parameter, bounded grid
COORDINATE_ROUNDS = 4                  # refinement rounds after the grid
EULER_SUBSTEPS_PER_DAY = 24
UNIDENTIFIED_SD_INFLATION = 2.0        # pooled spread multiplier for a player without an identified fit
MAX_SINDY_SAMPLING_DAYS = 1.0          # derivative estimates need at least daily samples
MIN_SINDY_SAMPLES = 20
MIN_DELTA_TO_RESOLUTION_RATIO = 3.0    # mean |dF| must exceed 3x the rounding resolution
SINDY_MAX_ITERATIONS = 10


@dataclass(frozen=True)
class FatigueParameters:
    alpha: float   # workload gain per unit of training load
    beta: float    # daily recovery rate of latent fatigue


@dataclass(frozen=True)
class ParameterBounds:
    alpha: tuple[float, float] = (0.0, 2.0)
    beta: tuple[float, float] = (0.01, 0.9)
    offset: tuple[float, float] = (60.0, 100.0)
    scale: tuple[float, float] = (0.1, 5.0)

    def as_dict(self) -> dict[str, tuple[float, float]]:
        return {"alpha": self.alpha, "beta": self.beta, "offset": self.offset, "scale": self.scale}


@dataclass(frozen=True)
class DailyLoad:
    """Training workload for one day plus any match load applied at the end of that day (model units)."""

    day: int
    workload: float
    match_load: float = 0.0


@dataclass(frozen=True)
class ConditionMeasurement:
    """A *current* condition observation on ``day``; stale caches never become measurements."""

    day: int
    condition: float
    source: str = ""


def collect_measurements(daily_payloads: Sequence[tuple[int, dict[str, Any]]]) -> tuple[list[ConditionMeasurement], list[dict[str, Any]]]:
    """Turn ``(day, player_payload)`` pairs into measurements, keeping only current readiness; the rest are reported as rejected."""
    kept: list[ConditionMeasurement] = []
    rejected: list[dict[str, Any]] = []
    for day, payload in daily_payloads:
        condition, _ = readiness_observed(payload, source=f"day:{day}")
        if condition.available:
            kept.append(ConditionMeasurement(day, float(condition.require()), condition.source))
        else:
            rejected.append({"day": day, "status": condition.status.value, "reason": condition.reason})
    return kept, rejected


# ----- latent dynamics -----

class DiscreteFatigueModel:
    """Daily state update; the simpler comparator to the ODE."""

    def __init__(self, params: FatigueParameters):
        self.params = params

    def step(self, fatigue: float, load: DailyLoad) -> float:
        return fatigue + self.params.alpha * load.workload - self.params.beta * fatigue + load.match_load

    def simulate(self, fatigue0: float, loads: Sequence[DailyLoad]) -> list[float]:
        """Latent fatigue at the start of each day, followed by the state after the final day."""
        path = [fatigue0]
        for load in loads:
            path.append(self.step(path[-1], load))
        return path


class ContinuousFatigueModel:
    """``dF/dt = alpha*workload - beta*F`` integrated with fixed-step explicit Euler; match load is a jump at the end of the day."""

    def __init__(self, params: FatigueParameters, substeps: int = EULER_SUBSTEPS_PER_DAY):
        if substeps < 1:
            raise ValueError("substeps must be at least 1")
        self.params = params
        self.substeps = substeps

    def step(self, fatigue: float, load: DailyLoad) -> float:
        dt = 1.0 / self.substeps
        for _ in range(self.substeps):
            fatigue += dt * (self.params.alpha * load.workload - self.params.beta * fatigue)
        return fatigue + load.match_load

    def simulate(self, fatigue0: float, loads: Sequence[DailyLoad]) -> list[float]:
        path = [fatigue0]
        for load in loads:
            path.append(self.step(path[-1], load))
        return path


@dataclass(frozen=True)
class ObservationModel:
    """``condition_pct = offset - scale * F``. Linear with offset; fitted, not assumed equal to F."""

    offset: float
    scale: float

    def condition(self, fatigue: float) -> float:
        return self.offset - self.scale * fatigue

    def latent(self, condition: float) -> float:
        if self.scale == 0.0:
            raise ValueError("scale is zero; condition carries no information about fatigue")
        return (self.offset - condition) / self.scale


def load_record_gaps(loads: Sequence[DailyLoad]) -> list[str]:
    """Reasons a load record cannot be simulated as a day-by-day series.

    The models advance the latent state exactly one day per record, so the
    records must be one row per consecutive day. A missing day is a *missing
    workload*, not a rest day: assuming zero load there would be a fabricated
    default, so a gapped, duplicated or out-of-order record is reported here
    and refused rather than silently stepped over (spec 10.2, 5.2).
    """
    reasons: list[str] = []
    days = [l.day for l in loads]
    if days != sorted(days):
        reasons.append(f"load records are not in day order: {days}")
    duplicates = sorted({d for d in days if days.count(d) > 1})
    if duplicates:
        reasons.append(f"more than one load record for days {duplicates}")
    ordered = sorted(set(days))
    missing = [d for d in range(ordered[0], ordered[-1] + 1) if d not in set(ordered)] if ordered else []
    if missing:
        reasons.append(f"no load records for days {missing}; unlogged days are not assumed to be zero load")
    return reasons


def predict_conditions(model: DiscreteFatigueModel | ContinuousFatigueModel, observation: ObservationModel, fatigue0: float, loads: Sequence[DailyLoad]) -> dict[int, float]:
    """Predicted condition at the start of each day, keyed by the load record's own day.

    The final entry is the state after the last record, keyed
    ``loads[-1].day + 1``. Each simulation step is one calendar day, so the
    records must cover consecutive days; a gapped, duplicated or out-of-order
    record raises :class:`ValueError` instead of returning predictions whose
    keys silently drift away from the real days (spec 10.2).
    """
    if not loads:
        return {}
    gaps = load_record_gaps(loads)
    if gaps:
        raise ValueError("load records are not a consecutive daily series: " + "; ".join(gaps))
    path = model.simulate(fatigue0, loads)
    days = [l.day for l in loads] + [loads[-1].day + 1]
    return {day: observation.condition(f) for day, f in zip(days, path)}


# ----- fitting -----

@dataclass
class FitResult:
    """``status``: identified | unidentifiable | insufficient_data. Parameters are only set when identified."""

    status: str
    parameters: FatigueParameters | None
    observation: ObservationModel | None
    objective: float | None
    reasons: list[str] = field(default_factory=list)
    near_optimal_spread: dict[str, float] = field(default_factory=dict)
    measurements_used: int = 0
    model_kind: str = "discrete"
    dynamics_version: str = DYNAMICS_VERSION

    @property
    def identified(self) -> bool:
        return self.status == "identified"


def _grid(bounds: tuple[float, float], steps: int) -> list[float]:
    low, high = bounds
    if steps == 1:
        return [low]
    return [low + (high - low) * i / (steps - 1) for i in range(steps)]


def _objective(point: dict[str, float], loads: Sequence[DailyLoad], measurements: Sequence[ConditionMeasurement], model_kind: str, fatigue0: float) -> float:
    params = FatigueParameters(point["alpha"], point["beta"])
    model = ContinuousFatigueModel(params) if model_kind == "continuous" else DiscreteFatigueModel(params)
    predicted = predict_conditions(model, ObservationModel(point["offset"], point["scale"]), fatigue0, loads)
    return sum((predicted[m.day] - m.condition) ** 2 for m in measurements) / len(measurements)


def _informative(loads: Sequence[DailyLoad], measurements: Sequence[ConditionMeasurement]) -> list[str]:
    reasons = []
    if len(measurements) < MIN_INFORMATIVE_OBSERVATIONS:
        reasons.append(f"{len(measurements)} current measurements below minimum {MIN_INFORMATIVE_OBSERVATIONS}")
    if measurements and len({m.condition for m in measurements}) == 1:
        reasons.append("all measurements identical; no variation to explain")
    if loads and len({l.workload for l in loads}) == 1 and not any(l.match_load for l in loads):
        reasons.append("constant workload and no match loads; inputs do not excite the dynamics")
    reasons.extend(load_record_gaps(loads))
    days = {l.day for l in loads}
    outside = [m.day for m in measurements if m.day not in days and m.day != (loads[-1].day + 1 if loads else None)]
    if outside:
        reasons.append(f"measurements on days without load records: {outside}")
    return reasons


def fit_parameters(loads: Sequence[DailyLoad], measurements: Sequence[ConditionMeasurement], *, bounds: ParameterBounds = ParameterBounds(), model_kind: str = "discrete", fatigue0: float = 0.0, measurement_resolution: float = DEFAULT_MEASUREMENT_RESOLUTION) -> FitResult:
    """Bounded grid then coordinate search over (alpha, beta, offset, scale) minimising mean squared condition error.

    ``fatigue0`` is the assumed latent fatigue at the first day (0 = fully
    rested); it is an explicit assumption, not a fitted quantity. The search
    is a heuristic optimiser, not a likelihood-based estimator.
    ``measurement_resolution`` is the rounding step of the condition values;
    parameter sets whose mean squared error lies within one resolution step
    squared of the best (or within :data:`FLAT_OBJECTIVE_TOLERANCE`) are
    treated as indistinguishable when judging identifiability.
    """
    if measurement_resolution <= 0.0:
        raise ValueError("measurement_resolution must be positive")
    if model_kind not in ("discrete", "continuous"):
        raise ValueError("model_kind must be discrete or continuous")
    reasons = _informative(loads, measurements)
    if reasons:
        incomplete = len(measurements) < MIN_INFORMATIVE_OBSERVATIONS or bool(load_record_gaps(loads))
        return FitResult("insufficient_data" if incomplete else "unidentifiable", None, None, None, reasons, {}, len(measurements), model_kind)
    names = ["alpha", "beta", "offset", "scale"]
    grids = {n: _grid(b, GRID_STEPS) for n, b in bounds.as_dict().items()}
    evaluated: list[tuple[float, dict[str, float]]] = []
    for a in grids["alpha"]:
        for b in grids["beta"]:
            for o in grids["offset"]:
                for s in grids["scale"]:
                    point = {"alpha": a, "beta": b, "offset": o, "scale": s}
                    evaluated.append((_objective(point, loads, measurements, model_kind, fatigue0), point))
    best_value, best = min(evaluated, key=lambda item: item[0])
    best = dict(best)
    spread = _near_optimal_spread(evaluated, best_value, bounds, measurement_resolution)
    best_value, best = _coordinate_refine(best, best_value, names, bounds, loads, measurements, model_kind, fatigue0)
    flat = [f"{n} spans {frac:.0%} of its bounds among near-optimal fits" for n, frac in spread.items() if frac > IDENTIFIABLE_SPREAD_FRACTION]
    if flat:
        return FitResult("unidentifiable", None, None, best_value, ["objective flat within tolerance"] + flat, spread, len(measurements), model_kind)
    return FitResult("identified", FatigueParameters(best["alpha"], best["beta"]), ObservationModel(best["offset"], best["scale"]), best_value, [], spread, len(measurements), model_kind)


def _near_optimal_spread(evaluated: list[tuple[float, dict[str, float]]], best_value: float, bounds: ParameterBounds, resolution: float) -> dict[str, float]:
    ceiling = max(best_value * (1.0 + FLAT_OBJECTIVE_TOLERANCE), best_value + resolution ** 2)
    near = [p for v, p in evaluated if v <= ceiling]
    spread = {}
    for name, (low, high) in bounds.as_dict().items():
        values = [p[name] for p in near]
        spread[name] = (max(values) - min(values)) / (high - low) if high > low else 0.0
    return spread


def _coordinate_refine(best: dict[str, float], best_value: float, names: list[str], bounds: ParameterBounds, loads, measurements, model_kind: str, fatigue0: float) -> tuple[float, dict[str, float]]:
    ranges = bounds.as_dict()
    for round_index in range(1, COORDINATE_ROUNDS + 1):
        for name in names:
            low, high = ranges[name]
            step = (high - low) / (GRID_STEPS - 1) / (2 ** round_index)
            for candidate in (best[name] - step, best[name] + step):
                if not low <= candidate <= high:
                    continue
                trial = dict(best)
                trial[name] = candidate
                value = _objective(trial, loads, measurements, model_kind, fatigue0)
                if value < best_value:
                    best_value, best = value, trial
    return best_value, best


# ----- empirical recovery curve baseline -----

@dataclass
class RecoveryCurve:
    """Mean current condition by days since the last match. The comparison baseline for the dynamic models."""

    by_days_since_match: dict[int, tuple[float, int]]   # lag -> (mean condition, count)
    status: str
    reason: str | None = None

    def predict(self, days_since_match: int) -> Observed:
        entry = self.by_days_since_match.get(days_since_match)
        if entry is None:
            return Observed.unavailable(ValueStatus.MISSING, "condition", f"no current measurements {days_since_match} days after a match", source="recovery_curve")
        return Observed.available_value(entry[0], source="recovery_curve", what="condition")


def empirical_recovery_curve(loads: Sequence[DailyLoad], measurements: Sequence[ConditionMeasurement]) -> RecoveryCurve:
    match_days = sorted(l.day for l in loads if l.match_load > 0)
    if not match_days:
        return RecoveryCurve({}, "unavailable", "no match loads in the schedule")
    grouped: dict[int, list[float]] = {}
    for m in measurements:
        previous = [d for d in match_days if d < m.day]
        if not previous:
            continue
        grouped.setdefault(m.day - previous[-1], []).append(m.condition)
    if not grouped:
        return RecoveryCurve({}, "unavailable", "no current measurements after a match")
    return RecoveryCurve({lag: (sum(v) / len(v), len(v)) for lag, v in sorted(grouped.items())}, "available")


# ----- pooling across players -----

@dataclass(frozen=True)
class PooledPrior:
    alpha_mean: float | None
    alpha_sd: float | None
    beta_mean: float | None
    beta_sd: float | None
    players: int
    status: str
    reason: str | None = None


def pooled_prior(fits: Sequence[FitResult]) -> PooledPrior:
    """Mean and spread of identified per-player parameters; needs at least two identified players."""
    identified = [f.parameters for f in fits if f.identified and f.parameters is not None]
    if len(identified) < 2:
        return PooledPrior(None, None, None, None, len(identified), "unavailable", "fewer than two identified players")
    alphas = [p.alpha for p in identified]
    betas = [p.beta for p in identified]
    return PooledPrior(sum(alphas) / len(alphas), sample_std(alphas), sum(betas) / len(betas), sample_std(betas), len(identified), "available")


@dataclass(frozen=True)
class IndividualEstimate:
    """A player's parameters with explicit uncertainty and the basis they rest on."""

    alpha: float | None
    beta: float | None
    alpha_sd: float | None
    beta_sd: float | None
    basis: str          # individual | shrunk | pooled | unavailable
    reasons: list[str] = field(default_factory=list)


def individual_estimate(fit: FitResult, prior: PooledPrior, *, shrinkage: float = 0.5) -> IndividualEstimate:
    """Combine a player's own fit with the pooled prior.

    Identified fits are shrunk toward the pool by the explicit ``shrinkage``
    weight. Unidentified players inherit the pooled mean with inflated
    spread; they never receive a falsely precise individual value.
    """
    if not 0.0 <= shrinkage <= 1.0:
        raise ValueError("shrinkage must be in [0, 1]")
    if fit.identified and fit.parameters is not None:
        if prior.status != "available":
            return IndividualEstimate(fit.parameters.alpha, fit.parameters.beta, None, None, "individual", ["no pooled prior; individual uncertainty not quantified"])
        a = (1 - shrinkage) * fit.parameters.alpha + shrinkage * prior.alpha_mean
        b = (1 - shrinkage) * fit.parameters.beta + shrinkage * prior.beta_mean
        return IndividualEstimate(a, b, prior.alpha_sd, prior.beta_sd, "shrunk", [f"shrinkage {shrinkage:g} toward pool of {prior.players}"])
    if prior.status != "available":
        return IndividualEstimate(None, None, None, None, "unavailable", [f"fit {fit.status}"] + fit.reasons + [prior.reason or "no pooled prior"])
    return IndividualEstimate(prior.alpha_mean, prior.beta_mean, (prior.alpha_sd or 0.0) * UNIDENTIFIED_SD_INFLATION, (prior.beta_sd or 0.0) * UNIDENTIFIED_SD_INFLATION, "pooled", [f"fit {fit.status}; pooled values with x{UNIDENTIFIED_SD_INFLATION:g} spread"] + fit.reasons)


# ----- SINDy-style sparse regression -----

SINDY_LIBRARY = ("1", "F", "u", "F*u", "F^2")


@dataclass
class SindyResult:
    status: str                     # fitted | refused
    coefficients: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    samples: int = 0


def _rounding_resolution(values: Sequence[float]) -> float:
    for resolution in (1.0, 0.5, 0.1):
        if all(abs(v / resolution - round(v / resolution)) < 1e-9 for v in values):
            return resolution
    return 0.0


def sindy_fit(states: Sequence[float], inputs: Sequence[float], sampling_interval_days: float, *, threshold: float = 0.05, ridge_lambda: float = 1e-3) -> SindyResult:
    """Sequential thresholded least squares for ``dF/dt`` over a small candidate library.

    Refuses when derivative estimates would come from rounded or infrequent
    observations: sampling coarser than daily, fewer than
    ``MIN_SINDY_SAMPLES`` points, or day-to-day changes below a few times the
    rounding resolution of the recorded values (spec 10.2).
    """
    reasons = []
    if len(states) != len(inputs):
        raise ValueError("states and inputs differ in length")
    if sampling_interval_days > MAX_SINDY_SAMPLING_DAYS:
        reasons.append(f"sampling every {sampling_interval_days:g} days is coarser than {MAX_SINDY_SAMPLING_DAYS:g}")
    if len(states) < MIN_SINDY_SAMPLES:
        reasons.append(f"{len(states)} samples below minimum {MIN_SINDY_SAMPLES}")
    deltas = [abs(b - a) for a, b in zip(states, states[1:])]
    resolution = _rounding_resolution(states)
    if deltas and resolution > 0.0 and (sum(deltas) / len(deltas)) < MIN_DELTA_TO_RESOLUTION_RATIO * resolution:
        reasons.append(f"values rounded to {resolution:g} and mean change {sum(deltas) / len(deltas):.2f} is within rounding noise")
    if reasons:
        return SindyResult("refused", {}, reasons, len(states))
    derivatives = [(b - a) / sampling_interval_days for a, b in zip(states, states[1:])]
    design = [[1.0, f, u, f * u, f * f] for f, u in zip(states[:-1], inputs[:-1])]
    active = list(range(len(SINDY_LIBRARY)))
    coefficients = [0.0] * len(SINDY_LIBRARY)
    for _ in range(SINDY_MAX_ITERATIONS):
        sub = [[row[i] for i in active] for row in design]
        beta = ridge_regression(sub, derivatives, ridge_lambda, penalize_intercept=True)
        keep = [i for i, b in zip(active, beta) if abs(b) >= threshold]
        coefficients = [0.0] * len(SINDY_LIBRARY)
        for i, b in zip(active, beta):
            coefficients[i] = b
        if keep == active:
            break
        if not keep:
            return SindyResult("fitted", {}, ["all terms below threshold; no dynamics recovered"], len(states))
        active = keep
    return SindyResult("fitted", {name: c for name, c in zip(SINDY_LIBRARY, coefficients) if c != 0.0}, [], len(states))
