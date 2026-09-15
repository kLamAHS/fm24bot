"""As-of feature store, leakage detection and fit-inside-folds guard (spec 5.4, 13.3, EXP 02).

A training example is a decision the manager faced at a moment in game time.
A feature may only describe what the manager could have observed at that
moment on that career branch: the scout report that arrived a week later, the
contract signed next window, the full-time statistics of a match that had
not kicked off, and anything observed on a sibling laboratory branch are all
future information. Labels keep two clocks: when the event happened
(``event_game_time``) and when the outcome became known
(``known_at_game_time``); the second is what matters for availability.

:func:`detect_leakage` fails a contaminated dataset explicitly. It is a rule
check, not a statistical test: it can only catch what the rows declare, so
rows without a known-at time are flagged rather than trusted.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

from ..state.units import game_time_key

LEAKAGE_RULES_VERSION = "experiments.leakage/1"

# Feature categories with an extra timing rule beyond "known at or before the decision".
CATEGORY_FEATURE = "feature"
CATEGORY_FINAL_MATCH_STATISTIC = "final_match_statistic"   # complete only after the match finished
CATEGORY_CONTRACT = "contract"                             # the contract must have started by the decision
CATEGORY_SCOUT_KNOWLEDGE = "scout_knowledge"               # scout knowledge as of the report date
CATEGORY_READINESS = "readiness"                           # condition/sharpness as of the observation
CATEGORY_OUTCOME_DERIVED = "outcome_derived"               # built from labels; may only be fitted in-fold

RULE_FUTURE_KNOWLEDGE = "future_knowledge"
RULE_CROSS_BRANCH = "cross_branch"
RULE_FINAL_MATCH_STATISTIC = "final_match_statistics_before_full_time"
RULE_FUTURE_CONTRACT = "future_contract"
RULE_LATER_SCOUT_KNOWLEDGE = "later_scout_knowledge"
RULE_UNKNOWN_AVAILABILITY = "unknown_availability"
RULE_LABEL_AS_FEATURE = "label_used_as_feature"
RULE_LABEL_KNOWN_BEFORE_EVENT = "label_known_before_event"
RULE_UNKNOWN_BRANCH = "unknown_branch"


def game_moment_key(moment: str | None) -> tuple[int, int] | None:
    """Order key for ``"YYYY-MM-DD"``, ``"YYYY-MM-DD HH:MM"`` or ``"YYYY-MM-DDTHH:MM"``. ``None`` stays ``None``."""
    if moment is None:
        return None
    text = moment.strip()
    if "T" in text:
        date_part, _, time_part = text.partition("T")
    elif " " in text:
        date_part, _, time_part = text.partition(" ")
    else:
        date_part, time_part = text, ""
    return game_time_key(date_part, time_part or None)


@dataclass(frozen=True)
class FeatureRow:
    """One feature value with the moment it became knowable and where it was observed."""

    entity_id: str | int
    feature_name: str
    value: Any
    known_at_game_time: str | None           # in-game moment the value became observable
    known_at_wall_time: str | None           # collection wall clock (audit only)
    branch_id: str | None
    source_observation_id: str | None
    event_game_time: str | None = None       # the underlying event (match end, contract start, report date)
    category: str = CATEGORY_FEATURE

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LabelRow:
    """An outcome with separate event and known-at clocks."""

    entity_id: str | int
    label_name: str
    value: Any
    event_game_time: str | None
    known_at_game_time: str | None
    branch_id: str | None
    source_observation_id: str | None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _known_before(row_moment: str | None, decision_time: str) -> bool | None:
    """True/False when both moments parse; ``None`` when the row has no moment (never assumed available)."""
    key = game_moment_key(row_moment)
    if key is None:
        return None
    return key <= game_moment_key(decision_time)


def as_of_join(rows: Iterable[FeatureRow], decision_time: str, branch_id: str, *, entity_id: str | int | None = None) -> list[FeatureRow]:
    """Features a decision at ``decision_time`` on ``branch_id`` could have used.

    Keeps only rows known at or before the decision and observed on the same
    branch, and for each (entity, feature) the latest such row. Rows without a
    known-at moment are excluded: unknown availability is not availability.
    """
    latest: dict[tuple[str | int, str], FeatureRow] = {}
    for row in rows:
        if row.branch_id != branch_id:
            continue
        if entity_id is not None and row.entity_id != entity_id:
            continue
        if _known_before(row.known_at_game_time, decision_time) is not True:
            continue
        key = (row.entity_id, row.feature_name)
        current = latest.get(key)
        if current is None or game_moment_key(row.known_at_game_time) >= game_moment_key(current.known_at_game_time):  # type: ignore[operator]
            latest[key] = row
    return list(latest.values())


@dataclass
class DecisionExample:
    """One training example: a decision moment with the features and labels attached to it."""

    example_id: str
    entity_id: str | int
    decision_time: str
    branch_id: str
    features: list[FeatureRow] = field(default_factory=list)
    labels: list[LabelRow] = field(default_factory=list)
    career_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"example_id": self.example_id, "entity_id": self.entity_id, "decision_time": self.decision_time, "branch_id": self.branch_id, "career_id": self.career_id, "features": [f.to_json() for f in self.features], "labels": [l.to_json() for l in self.labels]}


@dataclass
class Dataset:
    name: str
    examples: list[DecisionExample] = field(default_factory=list)
    rules_version: str = LEAKAGE_RULES_VERSION


@dataclass(frozen=True)
class LeakageFinding:
    example_id: str
    name: str                 # feature or label name
    rule: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LeakageReport:
    dataset: str
    checked_examples: int
    checked_rows: int
    findings: list[LeakageFinding] = field(default_factory=list)
    rules_version: str = LEAKAGE_RULES_VERSION

    @property
    def passed(self) -> bool:
        return not self.findings

    def rules_hit(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.rule] = counts.get(finding.rule, 0) + 1
        return counts

    def to_json(self) -> dict[str, Any]:
        return {"dataset": self.dataset, "checked_examples": self.checked_examples, "checked_rows": self.checked_rows, "passed": self.passed, "findings": [f.to_json() for f in self.findings], "rules_hit": self.rules_hit(), "rules_version": self.rules_version}


def _check_feature(example: DecisionExample, row: FeatureRow, label_names: set[str]) -> list[LeakageFinding]:
    findings: list[LeakageFinding] = []
    ex, name = example.example_id, row.feature_name
    if row.branch_id is None:
        findings.append(LeakageFinding(ex, name, RULE_UNKNOWN_BRANCH, "feature row has no branch; branch isolation cannot be established"))
    elif row.branch_id != example.branch_id:
        findings.append(LeakageFinding(ex, name, RULE_CROSS_BRANCH, f"observed on branch {row.branch_id}, decision on {example.branch_id}"))
    known = _known_before(row.known_at_game_time, example.decision_time)
    if known is None:
        findings.append(LeakageFinding(ex, name, RULE_UNKNOWN_AVAILABILITY, "feature row has no known-at game time; availability cannot be established"))
    elif not known:
        rule = RULE_LATER_SCOUT_KNOWLEDGE if row.category == CATEGORY_SCOUT_KNOWLEDGE else RULE_FUTURE_KNOWLEDGE
        findings.append(LeakageFinding(ex, name, rule, f"known at {row.known_at_game_time}, after the decision at {example.decision_time}"))
    if row.category == CATEGORY_FINAL_MATCH_STATISTIC:
        event = game_moment_key(row.event_game_time)
        if event is None:
            findings.append(LeakageFinding(ex, name, RULE_FINAL_MATCH_STATISTIC, "final-match statistic without a match end time"))
        elif event >= game_moment_key(example.decision_time):
            findings.append(LeakageFinding(ex, name, RULE_FINAL_MATCH_STATISTIC, f"match ended {row.event_game_time}, at or after the decision at {example.decision_time}"))
    if row.category == CATEGORY_CONTRACT:
        start = game_moment_key(row.event_game_time)
        if start is None:
            findings.append(LeakageFinding(ex, name, RULE_FUTURE_CONTRACT, "contract feature without a start date"))
        elif start > game_moment_key(example.decision_time):
            findings.append(LeakageFinding(ex, name, RULE_FUTURE_CONTRACT, f"contract starts {row.event_game_time}, after the decision at {example.decision_time}"))
    if name in label_names or row.category == CATEGORY_OUTCOME_DERIVED:
        findings.append(LeakageFinding(ex, name, RULE_LABEL_AS_FEATURE, "outcome-derived value present among the features of the same example"))
    return findings


def _check_label(example: DecisionExample, row: LabelRow) -> list[LeakageFinding]:
    findings: list[LeakageFinding] = []
    ex, name = example.example_id, row.label_name
    if row.branch_id is None:
        findings.append(LeakageFinding(ex, name, RULE_UNKNOWN_BRANCH, "label row has no branch"))
    elif row.branch_id != example.branch_id:
        findings.append(LeakageFinding(ex, name, RULE_CROSS_BRANCH, f"label observed on branch {row.branch_id}, decision on {example.branch_id}"))
    event, known = game_moment_key(row.event_game_time), game_moment_key(row.known_at_game_time)
    if event is None or known is None:
        findings.append(LeakageFinding(ex, name, RULE_UNKNOWN_AVAILABILITY, "label lacks an event time or a known-at time"))
    elif known < event:
        findings.append(LeakageFinding(ex, name, RULE_LABEL_KNOWN_BEFORE_EVENT, f"label known at {row.known_at_game_time} before its event at {row.event_game_time}; contradictory history"))
    return findings


def detect_leakage(dataset: Dataset) -> LeakageReport:
    """Fail any example whose features could not have been observed at its decision moment on its branch."""
    report = LeakageReport(dataset.name, len(dataset.examples), 0)
    for example in dataset.examples:
        label_names = {l.label_name for l in example.labels}
        for row in example.features:
            report.checked_rows += 1
            report.findings.extend(_check_feature(example, row, label_names))
        for row in example.labels:
            report.checked_rows += 1
            report.findings.extend(_check_label(example, row))
    return report


def build_example(example_id: str, entity_id: str | int, decision_time: str, branch_id: str, feature_rows: Iterable[FeatureRow], labels: Iterable[LabelRow], *, career_id: str | None = None) -> DecisionExample:
    """Assemble an example using the as-of join so the features are already point-in-time correct."""
    return DecisionExample(example_id, entity_id, decision_time, branch_id, as_of_join(feature_rows, decision_time, branch_id, entity_id=entity_id), list(labels), career_id)


# ---------------------------------------------------------------------------
# Fit-inside-folds guard
# ---------------------------------------------------------------------------

# Transforms that learn something from data and therefore must see training rows only.
FITTED_TRANSFORM_KINDS: tuple[str, ...] = ("scaler", "imputer", "fracdiff", "feature_selection", "outcome_label", "encoder", "threshold", "calibrator")


class FoldLeakageError(ValueError):
    """A data-dependent transform was fitted on rows outside the training fold."""


@dataclass(frozen=True)
class FittedTransform:
    kind: str
    name: str
    fold_id: str
    example_ids: tuple[str, ...]
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "fold_id": self.fold_id, "example_ids": list(self.example_ids), "parameters": dict(self.parameters)}


class FoldScope:
    """Context in which every fitted transform is checked against the fold's training rows.

    Use as ``with FoldScope("fold-1", train_ids) as scope: scope.fit("scaler", "standardise", ids, params)``.
    A fit that touches a non-training example raises immediately (fail closed)
    and is recorded as a violation so the report shows it.
    """

    def __init__(self, fold_id: str, training_ids: Iterable[str], *, kinds: Iterable[str] = FITTED_TRANSFORM_KINDS):
        self.fold_id = fold_id
        self.training_ids = frozenset(training_ids)
        self.kinds = tuple(kinds)
        self.fitted: list[FittedTransform] = []
        self.violations: list[str] = []
        self.closed = False

    def __enter__(self) -> "FoldScope":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.closed = True

    def fit(self, kind: str, name: str, example_ids: Iterable[str], parameters: dict[str, Any] | None = None) -> FittedTransform:
        if self.closed:
            raise FoldLeakageError(f"fold {self.fold_id} is closed; fit transforms inside the scope")
        if kind not in self.kinds:
            raise FoldLeakageError(f"unknown fitted transform kind {kind!r}; known: {self.kinds}")
        ids = tuple(example_ids)
        outside = sorted(set(ids) - self.training_ids)
        if outside:
            message = f"{kind} {name!r} fitted on {len(outside)} non-training example(s) in fold {self.fold_id}: {outside[:5]}"
            self.violations.append(message)
            raise FoldLeakageError(message)
        record = FittedTransform(kind, name, self.fold_id, ids, dict(parameters or {}))
        self.fitted.append(record)
        return record

    @property
    def clean(self) -> bool:
        return not self.violations

    def assert_clean(self) -> None:
        if self.violations:
            raise FoldLeakageError("; ".join(self.violations))

    def report(self) -> dict[str, Any]:
        return {"fold_id": self.fold_id, "training_examples": len(self.training_ids), "fitted": [f.to_json() for f in self.fitted], "violations": list(self.violations), "clean": self.clean}
