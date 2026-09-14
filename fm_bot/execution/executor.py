"""Single-writer action executor (spec 4.2, 12.1-12.3, ACT 01-03, AUD 01).

One executor holds the ``ui_writer`` lock for a game; a second instance is
refused. Queued intents run first-in first-out. Immediately before any input
the executor re-checks, in order: Stop, career and branch, snapshot
consistency, source-object freshness, the availability of a validated
workflow, the display environment, the identified screen and the legality of
the first action on it, required capabilities, the authority profile, and
input focus. Any failure ends the intent (EXPIRED or CANCELLED) or pauses it
(focus) with no input sent.

The EXECUTING transition is persisted *before* the first input, so a crash
between the two leaves an in-flight record for
:mod:`fm_bot.execution.reconciliation` rather than a silent duplicate.

Navigation-class steps may be retried a bounded number of times after a fresh
screen check. Consequential steps (offer acceptance, selection submission,
Continue, ...) are never retried: a timeout or unexpected screen makes the
intent UNCERTAIN and stops the workflow with its evidence preserved.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from ..rules.authority import AuthorityProfile, authorize
from ..rules.capabilities import CapabilityRegistry
from ..state.identity import new_id, utc_now
from ..state.records import ActionIntent, ActionResult, ActionState, DecisionSnapshot, ExecutionOutcome, Observation, QualityStatus, Visibility
from ..state.status import MissingCapabilityReport
from .adapter import (ADAPTER_CONTRACT_VERSION, ANY_SCREEN, RISK_CONSEQUENTIAL, STEP_NO_FOCUS, STEP_STOPPED, STEP_TIMEOUT, STEP_UNEXPECTED_SCREEN, AdapterCrash, ScreenObservation, UIAdapter, UIStep, Workflow, validate_environment)
from .lifecycle import context_changes, enqueue as lifecycle_enqueue, transition
from .verification import Evidence, Verdict, VerdictKind, verify

EXECUTOR_VERSION = "execution.executor/1"

UI_WRITER_LOCK = "ui_writer"
# A lock whose heartbeat is older than this is treated as abandoned. Engineering default, not a measurement.
LOCK_STALE_SECONDS = 300.0
# Bounded retries for navigation-class steps after a fresh screen check (spec 12.3). Consequential steps: zero.
NAVIGATION_RETRY_LIMIT = 2

PREFLIGHT_ORDER = ("stop", "career_branch", "snapshot_valid", "freshness", "workflow", "environment", "screen", "capabilities", "authority", "focus")

RECOVERY_INSTRUCTION_UNCERTAIN = "do not retry; run reconciliation (reconcile_uncertain / reconcile_on_restart) to establish the actual state from readback"


class ExecutorError(RuntimeError):
    pass


class WriterLockHeld(ExecutorError):
    """Another executor already controls the UI for this game."""


@dataclass
class PreflightProblem:
    check: str
    reason: str
    outcome: ActionState | None            # EXPIRED / CANCELLED, or None to pause (intent stays QUEUED)
    report: MissingCapabilityReport | None = None

    def to_json(self) -> dict[str, Any]:
        return {"check": self.check, "reason": self.reason, "outcome": self.outcome.value if self.outcome else "pause", "report": self.report.to_json() if self.report else None}


@dataclass
class ExecutionReport:
    """What happened to one queued intent in one ``run_next`` call (AUD 01)."""

    action_id: str
    state: ActionState
    reason: str
    problems: list[PreflightProblem] = field(default_factory=list)
    result: ActionResult | None = None
    verdict: Verdict | None = None
    inputs_sent: int = 0
    paused: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"action_id": self.action_id, "state": self.state.value, "reason": self.reason, "problems": [p.to_json() for p in self.problems], "result": self.result.to_json() if self.result else None, "verdict": self.verdict.to_json() if self.verdict else None, "inputs_sent": self.inputs_sent, "paused": self.paused, "version": EXECUTOR_VERSION}


def record_screen_observation(store, observation: ScreenObservation, *, career_id: str | None, branch_id: str | None, game_date: str | None, game_time: str | None, adapter_name: str) -> str:
    """Journal a UI screen observation as evidence and return its observation id."""
    record = Observation.create(
        f"ui:screen.{observation.screen_id or 'unidentified'}", {"adapter": adapter_name, "screen": observation.to_json()},
        career_id=career_id, branch_id=branch_id, session_id=None, game_date=game_date, game_time=game_time,
        schema_version=ADAPTER_CONTRACT_VERSION, visibility=Visibility.VISIBLE, quality=QualityStatus.UI_UNVERIFIED,
    )
    store.insert_observation(record)
    return record.observation_id


@dataclass
class _StepsOutcome:
    state: ActionState | None            # None when every step completed; else FAILED or UNCERTAIN
    reason: str
    records: list[dict[str, Any]]
    consequential_sent: bool
    inputs_sent: int


class SingleWriterExecutor:
    """The one active UI writer for a game.

    Construction acquires the ``ui_writer`` lock. ``run_next`` executes one
    intent end to end: preflight, EXECUTING, steps, VERIFYING, verdict.
    ``stop`` cancels queued work and raises the adapter's Stop flag so no
    further input is sent (ACT 03).
    """

    def __init__(self, store, adapter: UIAdapter, *, owner_id: str, registry: CapabilityRegistry, profile: AuthorityProfile, career_id: str, branch_id: str, workflows: dict[str, Workflow], navigation_retry_limit: int = NAVIGATION_RETRY_LIMIT, lock_stale_seconds: float = LOCK_STALE_SECONDS):
        self.store, self.adapter, self.owner_id = store, adapter, owner_id
        self.registry, self.profile = registry, profile
        self.career_id, self.branch_id = career_id, branch_id
        self.workflows = dict(workflows)
        self.navigation_retry_limit = navigation_retry_limit
        self.stopped = False
        self.stop_reason: str | None = None
        self._queue: deque[str] = deque()
        if not store.acquire_lock(UI_WRITER_LOCK, owner_id, stale_after_seconds=lock_stale_seconds):
            raise WriterLockHeld(f"ui writer lock is held by {store.lock_owner(UI_WRITER_LOCK)!r}; one active UI writer per game")
        self.lock_held = True
        for name in adapter.capabilities():
            registry.provide(name, f"ui_adapter:{adapter.name}", "reported by the adapter; verified per workflow")
        store.journal("executor.started", {"owner_id": owner_id, "adapter": adapter.name, "career_id": career_id, "branch_id": branch_id, "version": EXECUTOR_VERSION}, owner_id)

    # ----- lifecycle of the executor itself -----
    def heartbeat(self) -> bool:
        return self.store.heartbeat_lock(UI_WRITER_LOCK, self.owner_id)

    def close(self) -> None:
        if self.lock_held:
            self.store.release_lock(UI_WRITER_LOCK, self.owner_id)
            self.lock_held = False
            self.store.journal("executor.closed", {"owner_id": self.owner_id}, self.owner_id)

    def stop(self, reason: str = "operator stop") -> list[str]:
        """Cancel every queued intent and raise the adapter's Stop flag. Returns the cancelled action ids."""
        self.stopped, self.stop_reason = True, reason
        self.adapter.stop(reason)
        cancelled: list[str] = []
        self.load_queue()          # queued work persisted by an earlier process is cancelled too
        while self._queue:
            intent = self.store.get_intent(self._queue.popleft())
            if intent is not None and intent.state is ActionState.QUEUED:
                transition(self.store, intent, ActionState.CANCELLED, f"stop: {reason}")
                cancelled.append(intent.action_id)
        self.store.journal("executor.stopped", {"owner_id": self.owner_id, "reason": reason, "cancelled": cancelled}, self.owner_id)
        return cancelled

    # ----- queue -----
    def enqueue(self, intent: ActionIntent) -> ActionIntent:
        if self.stopped:
            raise ExecutorError(f"executor stopped ({self.stop_reason}); refusing new work")
        if intent.state is ActionState.VALIDATED:
            lifecycle_enqueue(self.store, intent)
        elif intent.state is not ActionState.QUEUED:
            raise ExecutorError(f"only VALIDATED or QUEUED intents can be queued; {intent.action_id} is {intent.state.value}")
        if intent.action_id not in self._queue:
            self._queue.append(intent.action_id)
        return intent

    def load_queue(self) -> list[str]:
        """Pick up QUEUED intents persisted for this branch (e.g. by a previous process)."""
        for intent in self.store.list_intents([ActionState.QUEUED], branch_id=self.branch_id):
            if intent.action_id not in self._queue:
                self._queue.append(intent.action_id)
        return list(self._queue)

    def queue_ids(self) -> list[str]:
        return list(self._queue)

    # ----- preflight -----
    def _stop_requested(self) -> bool:
        flag = getattr(self.adapter, "stop_flag", None)
        return self.stopped or bool(flag and flag.is_set())

    def preflight(self, intent: ActionIntent, fresh: DecisionSnapshot, screen: ScreenObservation) -> list[PreflightProblem]:
        """Every check that must pass immediately before the first input (spec 12.2). Returns problems in check order."""
        problems: list[PreflightProblem] = []
        if self._stop_requested():
            problems.append(PreflightProblem("stop", f"stop requested: {self.stop_reason or getattr(self.adapter.stop_flag, 'reason', None)}", ActionState.CANCELLED))
        if (intent.career_id, intent.branch_id) != (self.career_id, self.branch_id) or (fresh.career_id, fresh.branch_id) != (self.career_id, self.branch_id):
            problems.append(PreflightProblem("career_branch", f"intent {intent.career_id}/{intent.branch_id} and snapshot {fresh.career_id}/{fresh.branch_id} must both be the executor's {self.career_id}/{self.branch_id}", ActionState.EXPIRED))
        if not fresh.valid:
            problems.append(PreflightProblem("snapshot_valid", f"fresh snapshot {fresh.consistency.value}: {fresh.consistency_reasons}", ActionState.EXPIRED))
        changes = context_changes(intent, fresh)
        if changes:
            problems.append(PreflightProblem("freshness", "; ".join(changes), ActionState.EXPIRED))
        workflow = self.workflows.get(intent.kind)
        if workflow is None:
            problems.append(PreflightProblem("workflow", f"no validated UI workflow for {intent.kind!r} on adapter {self.adapter.name!r}", ActionState.CANCELLED))
            self._capability_and_authority(intent, (), problems)
            return problems
        env_problems = validate_environment(screen, workflow.screen_model, require_focus=False)
        if env_problems:
            problems.append(PreflightProblem("environment", "; ".join(env_problems), ActionState.EXPIRED))
        else:
            first = workflow.steps[0]
            if first.screen != ANY_SCREEN and screen.screen_id != first.screen:
                problems.append(PreflightProblem("screen", f"screen {screen.screen_id!r} shown; workflow {workflow.workflow_id} starts on {first.screen!r}", ActionState.EXPIRED))
            elif not workflow.screen_model.legal(screen.screen_id, first.action):
                problems.append(PreflightProblem("screen", f"action {first.action!r} is not legal on screen {screen.screen_id!r}", ActionState.EXPIRED))
        self._capability_and_authority(intent, workflow.required_capabilities, problems)
        if not self.adapter.has_focus():
            problems.append(PreflightProblem("focus", "game window lost input focus; paused at a safe boundary, no input sent", None))
        return problems

    def _capability_and_authority(self, intent: ActionIntent, workflow_capabilities: tuple[str, ...], problems: list[PreflightProblem]) -> None:
        report = self.registry.check(intent.kind, extra=[*intent.required_capabilities, *workflow_capabilities])
        if report.blocked:
            problems.append(PreflightProblem("capabilities", f"missing capabilities: {', '.join(report.missing)}", ActionState.EXPIRED, report))
        decision = authorize(intent, self.profile, report)
        if not decision.allowed:
            problems.append(PreflightProblem("authority", "; ".join(decision.reasons), ActionState.EXPIRED))

    # ----- execution -----
    def run_next(self, fresh_snapshot_provider: Callable[[], DecisionSnapshot]) -> ExecutionReport | None:
        """Execute the next queued intent. Returns ``None`` when nothing is queued.

        The store is the durable FIFO; the in-memory deque is refilled from it
        when empty, so intents queued by the lifecycle (or by a previous
        process) are picked up in creation order.
        """
        if not self._queue and not self.stopped:
            self.load_queue()
        if not self._queue:
            return None
        action_id = self._queue.popleft()
        intent = self.store.get_intent(action_id)
        if intent is None or intent.state is not ActionState.QUEUED:
            return ExecutionReport(action_id, intent.state if intent else ActionState.CANCELLED, "intent is no longer queued; skipped")
        if self._stop_requested():
            transition(self.store, intent, ActionState.CANCELLED, f"stop: {self.stop_reason}")
            return ExecutionReport(action_id, ActionState.CANCELLED, "stop requested before execution; no input sent")
        fresh = fresh_snapshot_provider()
        screen = self.adapter.identify_screen()
        before_ids = [*fresh.observation_ids, self._record_screen(screen, fresh)]
        problems = self.preflight(intent, fresh, screen)
        if problems:
            return self._end_at_preflight(intent, problems)
        workflow = self.workflows[intent.kind]
        steps = workflow.instantiate(intent.parameters)
        # Persist EXECUTING before the first input; the journal order is the audit proof.
        transition(self.store, intent, ActionState.EXECUTING, f"preflight passed ({', '.join(PREFLIGHT_ORDER)}); workflow {workflow.workflow_id} v{workflow.version}")
        result = ActionResult(new_id("res"), intent.action_id, len(self.store.list_attempts(intent.action_id)) + 1, before_ids, [], ActionState.EXECUTING, None, None, None, None, adapter=self.adapter.name)
        self.store.insert_attempt(result)
        outcome = self._run_steps(intent, steps)
        after_snapshot = fresh_snapshot_provider()
        after_screen = self.adapter.identify_screen()
        result.after_evidence = [*after_snapshot.observation_ids, self._record_screen(after_screen, after_snapshot)]
        if outcome.state is not None:
            return self._end_after_steps(intent, result, outcome)
        transition(self.store, intent, ActionState.VERIFYING, "all steps completed; establishing the effect by readback")
        before = Evidence(before_ids, fresh, screen)
        after = Evidence(result.after_evidence, after_snapshot, after_screen, outcome.records)
        verdict = verify(intent, before, after, self.adapter)
        return self._end_after_verdict(intent, result, outcome, verdict)

    def _record_screen(self, screen: ScreenObservation, snapshot: DecisionSnapshot) -> str:
        return record_screen_observation(self.store, screen, career_id=self.career_id, branch_id=self.branch_id, game_date=snapshot.game_date, game_time=snapshot.game_time, adapter_name=self.adapter.name)

    def _end_at_preflight(self, intent: ActionIntent, problems: list[PreflightProblem]) -> ExecutionReport:
        decisive = [p for p in problems if p.outcome is not None]
        if not decisive:
            self._queue.appendleft(intent.action_id)
            self.store.journal("executor.paused", {"action_id": intent.action_id, "problems": [p.to_json() for p in problems]}, intent.action_id)
            return ExecutionReport(intent.action_id, ActionState.QUEUED, problems[0].reason, problems, paused=True)
        reason = "; ".join(f"preflight:{p.check}: {p.reason}" for p in decisive)
        transition(self.store, intent, decisive[0].outcome, reason)
        self.store.journal("executor.preflight_failed", {"action_id": intent.action_id, "problems": [p.to_json() for p in problems]}, intent.action_id)
        return ExecutionReport(intent.action_id, decisive[0].outcome, reason, problems)

    def _run_steps(self, intent: ActionIntent, steps: list[UIStep]) -> _StepsOutcome:
        records: list[dict[str, Any]] = []
        consequential_sent = False
        inputs = 0

        def stop_here(reason: str) -> _StepsOutcome:
            state = ActionState.UNCERTAIN if consequential_sent else ActionState.FAILED
            return _StepsOutcome(state, reason, records, consequential_sent, inputs)

        for step in steps:
            retries = 0
            while True:
                if self._stop_requested():
                    return stop_here(f"stop requested at safe boundary before step {step.step_id}; no further input sent")
                if not self.adapter.has_focus():
                    return stop_here(f"focus lost at safe boundary before step {step.step_id}; no further input sent")
                screen = self.adapter.identify_screen()
                self.store.journal("ui.input", {"action_id": intent.action_id, "step": step.to_json(), "retry": retries, "screen_before": screen.to_json()}, intent.action_id)
                inputs += 1
                try:
                    outcome = self.adapter.perform(step)
                except AdapterCrash as exc:
                    records.append({"step_id": step.step_id, "status": "crash", "error": str(exc)})
                    consequential_sent = consequential_sent or step.risk_class == RISK_CONSEQUENTIAL
                    return _StepsOutcome(ActionState.UNCERTAIN, f"adapter crashed during step {step.step_id}: {exc}; effect unknown", records, consequential_sent, inputs)
                records.append({"step_id": step.step_id, "risk_class": step.risk_class, "retry": retries, **outcome.to_json()})
                if outcome.ok:
                    consequential_sent = consequential_sent or step.risk_class == RISK_CONSEQUENTIAL
                    break
                if outcome.status in (STEP_STOPPED, STEP_NO_FOCUS):
                    return stop_here(f"step {step.step_id} refused ({outcome.status}): {outcome.error}")
                if step.risk_class == RISK_CONSEQUENTIAL:
                    if outcome.status in (STEP_TIMEOUT, STEP_UNEXPECTED_SCREEN):
                        # Input went out and the confirmation did not come back: the effect is unknown.
                        return _StepsOutcome(ActionState.UNCERTAIN, f"consequential step {step.step_id} {outcome.status}: {outcome.error}; never retried", records, True, inputs)
                    # The adapter refused or the UI rejected the input before any effect (illegal action, unknown screen, explicit error).
                    return _StepsOutcome(ActionState.UNCERTAIN if consequential_sent else ActionState.FAILED, f"consequential step {step.step_id} {outcome.status}: {outcome.error}; not retried", records, consequential_sent, inputs)
                if retries >= self.navigation_retry_limit:
                    return stop_here(f"navigation step {step.step_id} failed after {retries} retries ({outcome.status}): {outcome.error}")
                retries += 1
        return _StepsOutcome(None, "all steps completed", records, consequential_sent, inputs)

    def _end_after_steps(self, intent: ActionIntent, result: ActionResult, outcome: _StepsOutcome) -> ExecutionReport:
        transition(self.store, intent, outcome.state, outcome.reason)
        result.execution_state = outcome.state
        result.outcome = ExecutionOutcome.UNCERTAIN if outcome.state is ActionState.UNCERTAIN else ExecutionOutcome.FAILED
        result.uncertainty = outcome.reason if outcome.state is ActionState.UNCERTAIN else None
        result.recovery_instruction = RECOVERY_INSTRUCTION_UNCERTAIN if outcome.state is ActionState.UNCERTAIN else None
        result.finished_at = utc_now()
        self.store.update_attempt(result)
        self.store.journal("executor.result", {"action_id": intent.action_id, "state": outcome.state.value, "reason": outcome.reason, "steps": outcome.records}, intent.action_id)
        return ExecutionReport(intent.action_id, outcome.state, outcome.reason, result=result, inputs_sent=outcome.inputs_sent)

    def _end_after_verdict(self, intent: ActionIntent, result: ActionResult, outcome: _StepsOutcome, verdict: Verdict) -> ExecutionReport:
        state = {VerdictKind.CONFIRMED: ActionState.CONFIRMED, VerdictKind.FAILED: ActionState.FAILED, VerdictKind.UNCERTAIN: ActionState.UNCERTAIN}[verdict.kind]
        reason = f"verification {verdict.plan}: " + "; ".join(verdict.reasons)
        transition(self.store, intent, state, reason)
        result.execution_state = state
        result.outcome = ExecutionOutcome(verdict.kind.value)
        result.confirmed_effect = verdict.details.get("effect") if verdict.confirmed else None
        result.uncertainty = reason if state is ActionState.UNCERTAIN else None
        result.recovery_instruction = RECOVERY_INSTRUCTION_UNCERTAIN if state is ActionState.UNCERTAIN else None
        result.finished_at = utc_now()
        self.store.update_attempt(result)
        self.store.journal("executor.result", {"action_id": intent.action_id, "state": state.value, "reason": reason, "verdict": verdict.to_json(), "steps": outcome.records}, intent.action_id)
        return ExecutionReport(intent.action_id, state, reason, result=result, verdict=verdict, inputs_sent=outcome.inputs_sent)
