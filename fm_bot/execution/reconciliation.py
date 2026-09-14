"""Recovery of in-flight and uncertain actions (spec 12.3, 12.4, ACT 02, REC 01).

When the bot restarts, or when an executor reports UNCERTAIN, the club may or
may not already be living with the consequences of an input: an offer may
have been accepted just before the confirmation timed out. Reconciliation
never guesses and never re-sends. It moves the intent to RECONCILING, reads
the committed state back through the adapter's independent readback
(:func:`fm_bot.execution.verification.verify`), corroborates it with a fresh
bridge snapshot, and decides:

* CONFIRMED  - the intended effect is observed;
* FAILED     - the effect is provably absent (and either the context has
               expired or nobody asked for an explicit requeue);
* UNCERTAIN  - the readback cannot establish the effect: work on that
               workflow stays stopped and the evidence is preserved.

An intent is never silently reset to QUEUED. Re-dispatch requires the
explicit ``requeue_if_absent`` flag, and even then only when the effect is
provably absent and the intent's context is still fresh; the journal records
who asked. Compensating actions come only from :data:`COMPENSATIONS`, where a
reversal is known and permitted; they are proposed, never executed here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..state.records import ActionIntent, ActionResult, ActionState, DecisionSnapshot, ExecutionOutcome
from .adapter import UIAdapter
from .executor import record_screen_observation
from .lifecycle import IntentFactory, LifecycleError, context_changes, duplicate_of, transition
from .verification import Evidence, Verdict, VerdictKind, verify

RECONCILIATION_VERSION = "execution.reconciliation/1"

IN_FLIGHT_OR_UNCERTAIN = [ActionState.EXECUTING, ActionState.VERIFYING, ActionState.UNCERTAIN, ActionState.RECONCILING]


class ReconciliationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Compensation:
    """A known, permitted reversal for one action kind. ``requires`` names the intent parameters the reversal needs."""

    reversal_kind: str | None
    permitted: bool
    requires: tuple[str, ...]
    note: str


# Reversals are permitted only where the effect of undoing is understood and free of new commitments.
COMPENSATIONS: dict[str, Compensation] = {
    "select_validated_tactic": Compensation("select_validated_tactic", True, ("previous_tactic_catalog_id",), "re-select the previously selected catalog tactic; no financial or contractual effect"),
    "set.training": Compensation("set.training", True, ("previous_settings",), "restore the previously committed training settings"),
    "submit.lineup": Compensation(None, False, (), "lineup reversal is not a validated workflow; re-selection before kick-off is an ordinary new intent"),
    "commit.contract": Compensation(None, False, (), "an accepted agreement cannot be reversed through the UI; the obligations stand"),
    "commit.transfer_offer": Compensation(None, False, (), "a submitted offer cannot be withdrawn without a validated workflow"),
    "respond.inbox": Compensation(None, False, (), "conversation and inbox confirmations are irreversible"),
    "progress.continue": Compensation(None, False, (), "time cannot be reversed in production; laboratory restore is a separate logged experiment operation"),
    "match.substitute": Compensation(None, False, (), "substitutions are irreversible"),
}


@dataclass
class RecoveryDecision:
    action_id: str
    kind: str
    previous_state: ActionState
    new_state: ActionState
    reason: str
    evidence: list[str] = field(default_factory=list)
    verdict: Verdict | None = None
    compensation: dict[str, Any] | None = None
    duplicate_of: str | None = None
    requeued: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"action_id": self.action_id, "kind": self.kind, "previous_state": self.previous_state.value, "new_state": self.new_state.value, "reason": self.reason, "evidence": list(self.evidence), "verdict": self.verdict.to_json() if self.verdict else None, "compensation": self.compensation, "duplicate_of": self.duplicate_of, "requeued": self.requeued, "version": RECONCILIATION_VERSION}


def compensation_for(intent: ActionIntent) -> dict[str, Any]:
    """Whether a reversal is known, permitted and actually available for this intent."""
    entry = COMPENSATIONS.get(intent.kind)
    if entry is None:
        return {"kind": intent.kind, "known": False, "permitted": False, "available": False, "reversal_kind": None, "note": "no reversal is known for this action kind"}
    missing = [name for name in entry.requires if name not in intent.parameters]
    return {"kind": intent.kind, "known": True, "permitted": entry.permitted, "available": entry.permitted and not missing, "reversal_kind": entry.reversal_kind, "missing_parameters": missing, "note": entry.note}


def propose_compensation(store, factory: IntentFactory, intent: ActionIntent, snapshot: DecisionSnapshot) -> ActionIntent | None:
    """Create a PROPOSED reversal intent when the table permits it. It still has to be validated, queued and executed."""
    info = compensation_for(intent)
    if not info["available"]:
        store.journal("reconcile.compensation_refused", {"action_id": intent.action_id, **info}, intent.action_id)
        return None
    entry = COMPENSATIONS[intent.kind]
    parameters = {key[len("previous_"):]: intent.parameters[key] for key in entry.requires}
    targets = dict(intent.targets)
    try:
        reversal = factory.create(entry.reversal_kind, intent.authority_scope, snapshot, targets, parameters, required_capabilities=intent.required_capabilities, verification=intent.verification, risk_class=intent.risk_class, decision_id=f"compensate:{intent.action_id}")
    except LifecycleError as exc:
        store.journal("reconcile.compensation_refused", {"action_id": intent.action_id, "reason": str(exc)}, intent.action_id)
        return None
    store.journal("reconcile.compensation_proposed", {"action_id": intent.action_id, "reversal_action_id": reversal.action_id, **info}, intent.action_id)
    return reversal


def _last_attempt(store, action_id: str) -> ActionResult | None:
    attempts = store.list_attempts(action_id)
    if not attempts:
        return None
    data = dict(attempts[-1])
    data["execution_state"] = ActionState(data["execution_state"])
    data["outcome"] = ExecutionOutcome(data["outcome"]) if data.get("outcome") else None
    return ActionResult(**data)


def reconcile_uncertain(store, adapter: UIAdapter, intent: ActionIntent, fresh: DecisionSnapshot, *, reason: str | None = None, requeue_if_absent: bool = False, requested_by: str | None = None) -> RecoveryDecision:
    """Establish what actually happened to one in-flight or uncertain intent (ACT 02).

    No input is sent. The adapter readback decides; the fresh bridge snapshot
    corroborates. A duplicate intent with the same idempotency key is reported,
    never dispatched.
    """
    previous = intent.state
    if intent.state in (ActionState.EXECUTING, ActionState.VERIFYING):
        transition(store, intent, ActionState.UNCERTAIN, reason or f"found {previous.value} without a recorded outcome; effect unknown")
    if intent.state is ActionState.UNCERTAIN:
        transition(store, intent, ActionState.RECONCILING, reason or "reconciling from readback")
    if intent.state is not ActionState.RECONCILING:
        raise ReconciliationError(f"{intent.action_id} is {intent.state.value}; only in-flight or uncertain intents are reconciled")

    screen = adapter.identify_screen()
    screen_id = record_screen_observation(store, screen, career_id=intent.career_id, branch_id=intent.branch_id, game_date=fresh.game_date, game_time=fresh.game_time, adapter_name=adapter.name)
    attempt = _last_attempt(store, intent.action_id)
    before = Evidence(list(attempt.before_evidence) if attempt else [])
    after = Evidence([*fresh.observation_ids, screen_id], fresh, screen)
    verdict = verify(intent, before, after, adapter)
    expired = context_changes(intent, fresh)
    new_state, why, requeued = _decide(verdict, expired, requeue_if_absent, requested_by)
    transition(store, intent, new_state, why)
    if attempt is not None:
        attempt.after_evidence = list(after.observation_ids)
        attempt.execution_state = new_state
        attempt.outcome = ExecutionOutcome.CONFIRMED if new_state is ActionState.CONFIRMED else ExecutionOutcome.FAILED if new_state is ActionState.FAILED else ExecutionOutcome.UNCERTAIN
        attempt.confirmed_effect = verdict.details.get("effect") if verdict.confirmed else None
        attempt.uncertainty = why if new_state is ActionState.UNCERTAIN else None
        attempt.recovery_instruction = "reconciled from readback; no input sent" if new_state is not ActionState.UNCERTAIN else "work stopped; evidence preserved; reconcile again when readback is available"
        store.update_attempt(attempt)
    twin = duplicate_of(store, intent)
    decision = RecoveryDecision(intent.action_id, intent.kind, previous, new_state, why, list(after.observation_ids), verdict, compensation_for(intent), twin.action_id if twin else None, requeued)
    store.journal("reconcile.decision", decision.to_json(), intent.action_id)
    return decision


def _decide(verdict: Verdict, expired: list[str], requeue_if_absent: bool, requested_by: str | None) -> tuple[ActionState, str, bool]:
    if verdict.kind is VerdictKind.CONFIRMED:
        return ActionState.CONFIRMED, "effect observed by independent readback; no second dispatch", False
    if verdict.kind is VerdictKind.FAILED:
        absent = "effect provably absent: " + "; ".join(verdict.reasons)
        if expired:
            return ActionState.FAILED, absent + "; intent expired: " + "; ".join(expired), False
        if requeue_if_absent:
            return ActionState.QUEUED, absent + f"; explicitly re-queued by {requested_by or 'operator'} with fresh context", True
        return ActionState.FAILED, absent + "; not re-queued without an explicit instruction (a new decision must create a new intent)", False
    return ActionState.UNCERTAIN, "readback could not establish the effect: " + "; ".join(verdict.reasons) + "; work stopped, evidence preserved", False


def reconcile_on_restart(store, adapter: UIAdapter, fresh: DecisionSnapshot, *, branch_id: str | None = None) -> list[RecoveryDecision]:
    """On start-up, settle every persisted EXECUTING/VERIFYING/UNCERTAIN/RECONCILING intent before any new work (REC 01)."""
    decisions: list[RecoveryDecision] = []
    for intent in store.list_intents(IN_FLIGHT_OR_UNCERTAIN, branch_id=branch_id):
        decisions.append(reconcile_uncertain(store, adapter, intent, fresh, reason=f"restart found intent {intent.state.value}; reconciling before resuming"))
    store.journal("reconcile.restart", {"branch_id": branch_id, "decisions": [d.to_json() for d in decisions], "version": RECONCILIATION_VERSION}, branch_id)
    return decisions
