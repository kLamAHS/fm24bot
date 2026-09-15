"""Decision and action explanations (spec 15.1, AUD 01).

A decision view shows the selected action, the strongest alternatives, the
expected outcomes with their uncertainty, the constraints that bound the
choice, and where and when the evidence was observed. An executed action
must resolve to its inputs, the limits it was authorised under, the decision
version it came from and the evidence before and after it: ``explain_action``
walks intent -> decision -> snapshot -> observations and refuses to produce
a partial story when a link is missing.

Everything here is read from the store; nothing is inferred or filled in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..state.records import ActionIntent, ActionState, Decision, DecisionSnapshot, Observation
from .controls import KEY_AUTHORITY_PROFILE

EXPLAIN_VERSION = "interface.explain/1"
# How many alternatives a decision view lists beside the selected action.
ALTERNATIVES_SHOWN = 3
_PROFILE_VERSION_RE = re.compile(r"profile v(\d+)")


class ExplanationError(LookupError):
    """A link in the audit chain (intent -> decision -> snapshot -> observation) is missing."""


@dataclass
class SourceTimestamp:
    observation_id: str
    source: str
    observed_at: str
    game_date: str | None
    game_time: str | None
    quality: str
    http_status: int | None = None

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class DecisionExplanation:
    decision_id: str
    kind: str
    selected: dict[str, Any] | None
    alternatives: list[dict[str, Any]]
    expected_outcomes: list[dict[str, Any]]
    binding_constraints: list[dict[str, Any]]
    other_constraints: list[dict[str, Any]]
    reasons: list[str]
    components: dict[str, Any]
    snapshot_id: str | None
    game_date: str | None
    game_time: str | None
    collected_at: str | None
    source_timestamps: list[SourceTimestamp]
    objective_version: str
    settings_versions: dict[str, str]
    model_versions: dict[str, str]
    information_mode: str
    created_at: str
    gaps: list[str] = field(default_factory=list)
    version: str = EXPLAIN_VERSION

    @property
    def authority_profile_version(self) -> str | None:
        return self.settings_versions.get(KEY_AUTHORITY_PROFILE)

    def to_json(self) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != "source_timestamps"}
        data["source_timestamps"] = [s.to_json() for s in self.source_timestamps]
        data["authority_profile_version"] = self.authority_profile_version
        return data


@dataclass
class ActionExplanation:
    """AUD 01: one executed (or attempted) action resolved to inputs, limits, decision version and evidence."""

    action_id: str
    kind: str
    state: str
    state_reason: str | None
    authority_scope: str
    inputs: dict[str, Any]                       # targets, parameters, preconditions, required capabilities, entity versions
    limits: dict[str, Any]                       # authority profile version and its limits at validation time
    decision: DecisionExplanation
    transitions: list[dict[str, Any]]
    inputs_sent: list[dict[str, Any]]            # ui.input journal entries in order
    attempts: list[dict[str, Any]]
    before_evidence: list[SourceTimestamp]
    after_evidence: list[SourceTimestamp]
    verdict: dict[str, Any] | None
    idempotency_key: str
    version: str = EXPLAIN_VERSION

    def to_json(self) -> dict[str, Any]:
        return {"action_id": self.action_id, "kind": self.kind, "state": self.state, "state_reason": self.state_reason, "authority_scope": self.authority_scope, "inputs": self.inputs, "limits": self.limits, "decision": self.decision.to_json(), "transitions": self.transitions, "inputs_sent": self.inputs_sent, "attempts": self.attempts, "before_evidence": [s.to_json() for s in self.before_evidence], "after_evidence": [s.to_json() for s in self.after_evidence], "verdict": self.verdict, "idempotency_key": self.idempotency_key, "version": self.version}


# ----- helpers -----

def _timestamp(observation: Observation) -> SourceTimestamp:
    return SourceTimestamp(observation.observation_id, observation.source, observation.observed_at, observation.game_date, observation.game_time, observation.quality.value, observation.http_status)


def source_timestamps(store, observation_ids: list[str], *, strict: bool, gaps: list[str]) -> list[SourceTimestamp]:
    result: list[SourceTimestamp] = []
    for oid in observation_ids:
        observation = store.get_observation(oid, with_payload=False)
        if observation is None:
            if strict:
                raise ExplanationError(f"observation {oid} is referenced but not in the store")
            gaps.append(f"observation {oid} missing")
            continue
        result.append(_timestamp(observation))
    return result


def _score(candidate: dict[str, Any]) -> float | None:
    for key in ("score", "value", "objective_value", "expected_value"):
        value = candidate.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def strongest_alternatives(decision: Decision, count: int = ALTERNATIVES_SHOWN) -> list[dict[str, Any]]:
    """Candidates other than the selected one, best score first; unscored ones keep their order."""
    selected = decision.selected or {}
    others = [c for c in decision.candidates if c != selected and c.get("id", c.get("candidate_id")) != selected.get("id", selected.get("candidate_id", object()))]
    scored = [(c, _score(c)) for c in others]
    scored.sort(key=lambda pair: (pair[1] is None, -(pair[1] or 0.0)))
    return [c for c, _ in scored[:count]]


def _is_binding(constraint: dict[str, Any]) -> bool:
    return bool(constraint.get("binding")) or str(constraint.get("status", "")).lower() == "fail"


def _outcome(forecast: dict[str, Any]) -> dict[str, Any]:
    return {
        "target": forecast.get("forecast_target") or forecast.get("target") or forecast.get("name"),
        "point": forecast.get("point_estimate", forecast.get("point")),
        "interval": forecast.get("interval"),
        "status": forecast.get("status", "unknown"),
        "reason": forecast.get("reason"),
        "method": forecast.get("method"),
        "model_version": forecast.get("model_version"),
    }


def explain_decision(decision: Decision | str, store, *, strict: bool = False) -> DecisionExplanation:
    """Build the decision view for ``decision`` (an id or record).

    With ``strict`` a missing snapshot or observation raises
    :class:`ExplanationError`; otherwise the gap is listed under ``gaps`` so
    the operator can still read what the decision itself recorded.
    """
    record = store.get_decision(decision) if isinstance(decision, str) else decision
    if record is None:
        raise ExplanationError(f"decision {decision} is not in the store")
    gaps: list[str] = []
    snapshot: DecisionSnapshot | None = store.get_snapshot(record.snapshot_id) if record.snapshot_id else None
    if snapshot is None:
        if strict:
            raise ExplanationError(f"decision {record.decision_id} references snapshot {record.snapshot_id!r} which is not in the store")
        gaps.append(f"snapshot {record.snapshot_id!r} not in the store")
    timestamps = source_timestamps(store, snapshot.observation_ids if snapshot else [], strict=strict, gaps=gaps)
    settings_versions = {k: v for k, v in record.model_versions.items() if k.startswith("settings:")}
    model_versions = {k: v for k, v in record.model_versions.items() if not k.startswith("settings:")}
    return DecisionExplanation(
        record.decision_id, record.kind, record.selected, strongest_alternatives(record),
        [_outcome(f) for f in record.forecasts],
        [c for c in record.constraints if _is_binding(c)], [c for c in record.constraints if not _is_binding(c)],
        list(record.reasons), dict(record.components),
        snapshot.snapshot_id if snapshot else record.snapshot_id,
        snapshot.game_date if snapshot else None, snapshot.game_time if snapshot else None, snapshot.collected_at if snapshot else None,
        timestamps, record.objective_version, settings_versions, model_versions, record.information_mode, record.created_at, gaps,
    )


def _limits_at_validation(store, transitions: list[dict[str, Any]]) -> dict[str, Any]:
    """The authority profile version named when the intent was validated, and that version's limits from the journal."""
    version = None
    for entry in transitions:
        if entry["body"].get("to") == ActionState.VALIDATED.value:
            match = _PROFILE_VERSION_RE.search(entry["body"].get("reason") or "")
            if match:
                version = int(match.group(1))
    if version is None:
        return {"authority_profile_version": None, "status": "missing", "reason": "no VALIDATED transition names a profile version"}
    for entry in store.journal_entries(kind="setting", ref_id=KEY_AUTHORITY_PROFILE):
        if entry["body"].get("version") == version:
            body = entry["body"]["body"]
            return {"authority_profile_version": version, "status": "available", "mode": body.get("mode"), "enabled_families": body.get("enabled_families"), "limits": body.get("limits"), "delegation": body.get("delegation")}
    return {"authority_profile_version": version, "status": "missing", "reason": f"authority profile v{version} was not stored through the operator settings journal"}


def explain_action(action_id: str, store) -> ActionExplanation:
    """AUD 01: resolve an action to its inputs, limits, decision version and evidence, or fail loudly."""
    intent: ActionIntent | None = store.get_intent(action_id)
    if intent is None:
        raise ExplanationError(f"action {action_id} is not in the store")
    if not intent.decision_id:
        raise ExplanationError(f"action {action_id} records no decision; the audit chain is broken at intent -> decision")
    decision = store.get_decision(intent.decision_id)
    if decision is None:
        raise ExplanationError(f"action {action_id} references decision {intent.decision_id} which is not in the store")
    explanation = explain_decision(decision, store, strict=True)
    if intent.decision_snapshot_id and intent.decision_snapshot_id != decision.snapshot_id:
        raise ExplanationError(f"action {action_id} was built on snapshot {intent.decision_snapshot_id} but its decision used {decision.snapshot_id}")
    entries = store.journal_entries(ref_id=action_id, limit=10_000)
    transitions = [e for e in entries if e["kind"] == "intent.transition"]
    inputs_sent = [e["body"] for e in entries if e["kind"] == "ui.input"]
    attempts = store.list_attempts(action_id)
    before_ids = [oid for a in attempts for oid in a.get("before_evidence", [])]
    after_ids = [oid for a in attempts for oid in a.get("after_evidence", [])]
    gaps: list[str] = []
    before = source_timestamps(store, before_ids, strict=True, gaps=gaps)
    after = source_timestamps(store, after_ids, strict=True, gaps=gaps)
    verdict = None
    for entry in entries:
        if entry["kind"] == "executor.result" and entry["body"].get("verdict"):
            verdict = entry["body"]["verdict"]
    inputs = {"targets": intent.targets, "parameters": intent.parameters, "preconditions": intent.preconditions, "required_capabilities": intent.required_capabilities, "entity_versions": intent.entity_versions, "risk_class": intent.risk_class, "verification_plan": intent.verification}
    return ActionExplanation(intent.action_id, intent.kind, intent.state.value, intent.state_reason, intent.authority_scope, inputs, _limits_at_validation(store, transitions), explanation, [{"seq": t["seq"], "at": t["at_utc"], **t["body"]} for t in transitions], inputs_sent, attempts, before, after, verdict, intent.idempotency_key)


# ----- football-language rendering -----

def _money_text(value: Any) -> str:
    if isinstance(value, dict) and "minor" in value:
        from ..state.units import Money
        return str(Money.from_json(value))
    return str(value)


def _describe_action(action: dict[str, Any] | None) -> str:
    if not action:
        return "no action selected"
    kind = action.get("kind") or action.get("action") or "action"
    parts = [str(kind)]
    params = action.get("parameters") or {}
    if "tactic_catalog_id" in params:
        parts.append(f"tactic {params.get('catalog_name') or params['tactic_catalog_id']}")
    if "player_ids" in params:
        parts.append(f"{len(params['player_ids'])} players")
    for key in ("weekly_wage", "total_fee"):
        if key in params:
            parts.append(f"{key.replace('_', ' ')} {_money_text(params[key])}")
    if action.get("label"):
        parts.append(str(action["label"]))
    return " - ".join(parts)


def render_decision(explanation: DecisionExplanation) -> str:
    """The decision view in football language; evidence detail lives in :func:`render_decision_evidence`."""
    lines = [f"Decision: {explanation.kind} (made {explanation.created_at[:16]} on game day {explanation.game_date or 'unknown'} {explanation.game_time or ''})".rstrip()]
    lines.append(f"  Chosen: {_describe_action(explanation.selected)}")
    if explanation.alternatives:
        lines.append("  Strongest alternatives:")
        for alt in explanation.alternatives:
            score = _score(alt)
            lines.append(f"    - {_describe_action(alt)}" + (f" (score {score:.3f})" if score is not None else ""))
    else:
        lines.append("  Strongest alternatives: none recorded")
    if explanation.expected_outcomes:
        lines.append("  Expected outcomes:")
        for outcome in explanation.expected_outcomes:
            if outcome["status"] == "available":
                lines.append(f"    - {outcome['target']}: {outcome['point']} (range {outcome['interval']})")
            else:
                lines.append(f"    - {outcome['target']}: unavailable ({outcome.get('reason') or outcome['status']})")
    if explanation.binding_constraints:
        lines.append("  What bound the choice:")
        for constraint in explanation.binding_constraints:
            lines.append(f"    - {constraint.get('name', 'constraint')}: {constraint.get('reason') or constraint.get('status')}")
    for reason in explanation.reasons:
        lines.append(f"  Why: {reason}")
    if explanation.gaps:
        lines.append("  Gaps in the record: " + "; ".join(explanation.gaps))
    return "\n".join(lines)


def render_decision_evidence(explanation: DecisionExplanation) -> str:
    lines = [f"Evidence for decision {explanation.decision_id}", f"  snapshot {explanation.snapshot_id} collected {explanation.collected_at}", f"  objective version {explanation.objective_version}; information mode {explanation.information_mode}"]
    if explanation.settings_versions:
        lines.append("  settings: " + ", ".join(f"{k.split(':', 1)[1]} v{v}" for k, v in sorted(explanation.settings_versions.items())))
    if explanation.model_versions:
        lines.append("  models: " + ", ".join(f"{k} {v}" for k, v in sorted(explanation.model_versions.items())))
    for stamp in explanation.source_timestamps:
        lines.append(f"  {stamp.observation_id} {stamp.source} observed {stamp.observed_at} game {stamp.game_date} {stamp.game_time or ''} quality {stamp.quality}")
    for constraint in explanation.other_constraints:
        lines.append(f"  constraint {constraint.get('name')}: {constraint.get('status')} {constraint.get('reason') or ''}".rstrip())
    return "\n".join(lines)


def render_action(explanation: ActionExplanation) -> str:
    lines = [f"Action {explanation.action_id}: {explanation.kind} is {explanation.state}" + (f" ({explanation.state_reason})" if explanation.state_reason else "")]
    limits = explanation.limits
    if limits.get("status") == "available":
        lines.append(f"  Authorised under profile v{limits['authority_profile_version']} ({limits.get('mode')}; families {limits.get('enabled_families')})")
    else:
        lines.append(f"  Authority limits: {limits.get('status')} - {limits.get('reason')}")
    lines.append(f"  Inputs: targets {explanation.inputs['targets']}, parameters {explanation.inputs['parameters']}")
    lines.append(f"  UI inputs sent: {len(explanation.inputs_sent)}; attempts: {len(explanation.attempts)}")
    if explanation.verdict:
        lines.append(f"  Verified by {explanation.verdict.get('plan')}: {explanation.verdict.get('kind')} - " + "; ".join(explanation.verdict.get("reasons", [])))
    lines.append(f"  Evidence before: {len(explanation.before_evidence)} observations; after: {len(explanation.after_evidence)}")
    lines.append(render_decision(explanation.decision))
    return "\n".join(lines)
