"""Data splits for model evaluation (spec 13.3).

Three ways of holding data back, each answering a different question:

* :func:`grouped_split` keeps every branch of one starting career in the
  same fold, so a model is tested on careers it has never seen. Laboratory
  forks of the same save are not independent careers.
* :func:`chronological_split` trains on the past only and embargoes the
  units whose outcome windows straddle the cutoff, so a match whose result
  was still unknown at the cutoff cannot inform the training side.
* :class:`HoldoutRegistry` freezes a final set of untouched careers and the
  candidate models that will be judged on it. Evaluating on the holdout and
  then changing the model consumes the holdout; a further release claim
  needs a fresh one.

Fold assignment is deterministic given a seed (pure Python ``random``).
"""
from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

from ..state.identity import utc_now
from .leakage import game_moment_key

SPLIT_RULES_VERSION = "experiments.splits/1"


@dataclass(frozen=True)
class SplitUnit:
    """One analysable unit: a decision example or trial with its career, branch and outcome window."""

    unit_id: str
    career_id: str
    branch_id: str
    start_time: str                    # game moment of the decision or window start
    end_time: str | None = None        # game moment the outcome was known (window end)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Split:
    name: str
    train: list[str]
    test: list[str]
    tune: list[str] = field(default_factory=list)
    embargoed: list[str] = field(default_factory=list)
    groups: dict[str, list[str]] = field(default_factory=dict)   # set name -> career ids
    seed: int | None = None
    rules_version: str = SPLIT_RULES_VERSION

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class SplitError(ValueError):
    pass


def _by_career(units: Iterable[SplitUnit]) -> dict[str, list[SplitUnit]]:
    groups: dict[str, list[SplitUnit]] = {}
    for unit in units:
        groups.setdefault(unit.career_id, []).append(unit)
    return groups


def assert_grouped(split: Split, units: Iterable[SplitUnit]) -> None:
    """Raise if any career appears in more than one of train/tune/test."""
    career_of = {u.unit_id: u.career_id for u in units}
    sets = {"train": set(split.train), "tune": set(split.tune), "test": set(split.test)}
    seen: dict[str, str] = {}
    for set_name, ids in sets.items():
        for unit_id in ids:
            career = career_of.get(unit_id)
            if career is None:
                raise SplitError(f"unit {unit_id} in {set_name} is not among the units")
            if career in seen and seen[career] != set_name:
                raise SplitError(f"career {career} appears in both {seen[career]} and {set_name}; branches from one career must stay together")
            seen[career] = set_name


def grouped_split(units: list[SplitUnit], *, n_folds: int, seed: int) -> list[Split]:
    """K folds where every unit of a career (all its branches) lands in the same fold."""
    if n_folds < 2:
        raise SplitError("need at least two folds")
    careers = sorted(_by_career(units))
    if len(careers) < n_folds:
        raise SplitError(f"{len(careers)} career(s) cannot fill {n_folds} folds; group by career, not by fixture")
    rng = random.Random(seed)
    order = list(careers)
    rng.shuffle(order)
    fold_of = {career: index % n_folds for index, career in enumerate(order)}
    splits: list[Split] = []
    for fold in range(n_folds):
        test_careers = sorted(c for c, f in fold_of.items() if f == fold)
        train_careers = sorted(c for c, f in fold_of.items() if f != fold)
        split = Split(f"grouped-fold-{fold + 1}", [u.unit_id for u in units if u.career_id in set(train_careers)], [u.unit_id for u in units if u.career_id in set(test_careers)], groups={"train": train_careers, "test": test_careers}, seed=seed)
        assert_grouped(split, units)
        splits.append(split)
    return splits


def grouped_train_tune_test(units: list[SplitUnit], *, seed: int, tune_fraction: float = 0.2, test_fraction: float = 0.2) -> Split:
    """One train/tune/test partition by career. Fractions apply to careers, not units."""
    careers = sorted(_by_career(units))
    if len(careers) < 3:
        raise SplitError("need at least three careers for train, tune and test sets")
    rng = random.Random(seed)
    order = list(careers)
    rng.shuffle(order)
    n_test = max(1, round(len(order) * test_fraction))
    n_tune = max(1, round(len(order) * tune_fraction))
    if n_test + n_tune >= len(order):
        raise SplitError("fractions leave no career for training")
    test_c, tune_c, train_c = set(order[:n_test]), set(order[n_test:n_test + n_tune]), set(order[n_test + n_tune:])
    split = Split("grouped-train-tune-test", [u.unit_id for u in units if u.career_id in train_c], [u.unit_id for u in units if u.career_id in test_c], [u.unit_id for u in units if u.career_id in tune_c], groups={"train": sorted(train_c), "tune": sorted(tune_c), "test": sorted(test_c)}, seed=seed)
    assert_grouped(split, units)
    return split


def _split_moment(moment: str) -> tuple[str, str, str]:
    """``"YYYY-MM-DD[ T]HH:MM"`` as (date, separator, time). A date-only moment keeps an empty time."""
    text = moment.strip()
    for separator in ("T", " "):
        if separator in text:
            date_part, _, time_part = text.partition(separator)
            return date_part, separator, time_part
    return text, "", ""


def _shift_days(moment: str, days: int) -> str:
    """Move a game moment by whole days, keeping its time of day.

    Truncating to the date would move a timed cutoff back to 00:00 of its
    day, and ``game_moment_key`` scores a date-only moment as minutes ``-1``
    (before midnight, see :mod:`fm_bot.state.units`). The embargo boundary
    would then sit earlier than the cutoff itself, so units starting before
    the cutoff would compare as on-or-after it. Cutoff and unit must be read
    on the same clock.
    """
    date_part, separator, time_part = _split_moment(moment)
    shifted = (dt.date.fromisoformat(date_part) + dt.timedelta(days=days)).isoformat()
    return f"{shifted}{separator}{time_part}" if time_part else shifted


def chronological_split(units: list[SplitUnit], *, cutoff: str, embargo_days: int = 0) -> Split:
    """Past-only training with an embargo around the cutoff.

    Training units must have their outcome *known* strictly before the
    cutoff; a unit without an end time cannot prove that and is embargoed.
    Test units start at or after the cutoff plus ``embargo_days``. Units in
    between (outcome windows overlapping the cutoff) are embargoed.

    The cutoff may carry a time of day, and it is then compared against the
    units on that same clock: a unit that starts at 10:00 on the cutoff day
    with a 15:00 cutoff began *before* the cutoff and is embargoed, not
    tested.
    """
    if embargo_days < 0:
        raise SplitError("embargo_days must be non-negative")
    cutoff_key = game_moment_key(cutoff)
    test_from_key = game_moment_key(_shift_days(cutoff, embargo_days))
    train, test, embargoed = [], [], []
    for unit in units:
        start_key, end_key = game_moment_key(unit.start_time), game_moment_key(unit.end_time)
        if end_key is not None and end_key < cutoff_key:
            train.append(unit.unit_id)
        elif start_key >= test_from_key:
            test.append(unit.unit_id)
        else:
            embargoed.append(unit.unit_id)
    return Split(f"chronological@{cutoff}+{embargo_days}d", train, test, embargoed=embargoed)


# ---------------------------------------------------------------------------
# Final holdout
# ---------------------------------------------------------------------------


class HoldoutError(ValueError):
    pass


class HoldoutConsumedError(HoldoutError):
    """The holdout has been used to change a model; it no longer supports release claims."""


@dataclass
class FinalHoldout:
    """A frozen set of untouched careers plus the frozen candidates to be judged on them."""

    holdout_id: str
    career_ids: list[str]
    frozen_models: dict[str, str]                 # model_id -> artifact hash at freeze time
    frozen_at: str = field(default_factory=utc_now)
    evaluations: list[dict[str, Any]] = field(default_factory=list)
    consumed: bool = False
    consumed_reason: str | None = None
    consumed_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "FinalHoldout":
        return cls(**data)

    def evaluated_models(self) -> set[str]:
        return {e["model_id"] for e in self.evaluations}


class HoldoutRegistry:
    """Keeps final holdouts frozen and marks them consumed when a judged model changes.

    Backed by the store's versioned settings when a store is given, so the
    consumption of a holdout survives restarts and is journaled.
    """

    SETTING_PREFIX = "holdout:"

    def __init__(self, store=None):
        self.store = store
        self._memory: dict[str, FinalHoldout] = {}

    # ----- persistence -----
    def _save(self, holdout: FinalHoldout) -> None:
        self._memory[holdout.holdout_id] = holdout
        if self.store is not None:
            self.store.put_setting(self.SETTING_PREFIX + holdout.holdout_id, holdout.to_json())

    def get(self, holdout_id: str) -> FinalHoldout | None:
        if self.store is not None:
            stored = self.store.get_setting(self.SETTING_PREFIX + holdout_id)
            if stored is not None:
                holdout = FinalHoldout.from_json(stored[0])
                self._memory[holdout_id] = holdout
                return holdout
        return self._memory.get(holdout_id)

    # ----- lifecycle -----
    def freeze(self, holdout_id: str, career_ids: Iterable[str], candidate_models: dict[str, str]) -> FinalHoldout:
        """Freeze the holdout careers and the candidates (model_id -> artifact hash) before any evaluation."""
        if self.get(holdout_id) is not None:
            raise HoldoutError(f"holdout {holdout_id} already exists; a holdout is frozen once")
        careers = sorted(set(career_ids))
        if not careers:
            raise HoldoutError("a holdout needs at least one career")
        if not candidate_models:
            raise HoldoutError("freeze the candidate models and thresholds before evaluating")
        holdout = FinalHoldout(holdout_id, careers, dict(candidate_models))
        self._save(holdout)
        return holdout

    def assert_training_excludes(self, holdout: FinalHoldout, training_career_ids: Iterable[str]) -> None:
        overlap = sorted(set(training_career_ids) & set(holdout.career_ids))
        if overlap:
            raise HoldoutError(f"training lineage touches holdout careers {overlap}")

    def evaluate(self, holdout_id: str, model_id: str, artifact_hash: str, metrics: dict[str, Any]) -> FinalHoldout:
        """Record an evaluation of a frozen candidate on the holdout."""
        holdout = self.get(holdout_id)
        if holdout is None:
            raise HoldoutError(f"unknown holdout {holdout_id}")
        if holdout.consumed:
            raise HoldoutConsumedError(f"holdout {holdout_id} was consumed ({holdout.consumed_reason}); freeze a new holdout")
        frozen = holdout.frozen_models.get(model_id)
        if frozen is None:
            raise HoldoutError(f"model {model_id} was not frozen on holdout {holdout_id}; it cannot be judged there")
        if frozen != artifact_hash:
            self._consume(holdout, f"model {model_id} changed after freezing ({frozen[:12]} -> {artifact_hash[:12]})")
            raise HoldoutConsumedError(f"model {model_id} differs from its frozen artifact; holdout {holdout_id} is now consumed")
        holdout.evaluations.append({"model_id": model_id, "artifact_hash": artifact_hash, "metrics": dict(metrics), "at": utc_now()})
        self._save(holdout)
        return holdout

    def model_changed(self, holdout_id: str, model_id: str, new_artifact_hash: str) -> FinalHoldout:
        """Declare that a candidate changed. If it had been judged on the holdout, the holdout is consumed."""
        holdout = self.get(holdout_id)
        if holdout is None:
            raise HoldoutError(f"unknown holdout {holdout_id}")
        if model_id in holdout.evaluated_models() and holdout.frozen_models.get(model_id) != new_artifact_hash:
            self._consume(holdout, f"model {model_id} was changed after seeing holdout results")
        return holdout

    def _consume(self, holdout: FinalHoldout, reason: str) -> None:
        holdout.consumed = True
        holdout.consumed_reason = reason
        holdout.consumed_at = utc_now()
        self._save(holdout)
        if self.store is not None:
            self.store.journal("holdout.consumed", {"holdout_id": holdout.holdout_id, "reason": reason}, holdout.holdout_id)

    def usable_for_release_claim(self, holdout_id: str, model_id: str, artifact_hash: str) -> tuple[bool, list[str]]:
        holdout = self.get(holdout_id)
        problems: list[str] = []
        if holdout is None:
            return False, [f"unknown holdout {holdout_id}"]
        if holdout.consumed:
            problems.append(f"consumed: {holdout.consumed_reason}")
        if holdout.frozen_models.get(model_id) != artifact_hash:
            problems.append("model artifact is not the frozen candidate")
        return (not problems), problems
