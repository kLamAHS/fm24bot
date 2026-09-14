"""Action intent lifecycle (spec 12.2, 12.3, BOT 006).

An :class:`~fm_bot.state.records.ActionIntent` is the bot's promise to do one
thing in the game: pick a tactic, name a lineup, accept an offer. It is
created from a decision snapshot, validated against the authority profile and
the capability registry, queued, and only then handed to the executor.

Every state change goes through the store so the append-only journal records
the full history. Intents expire whenever the source objects they were built
on change, even if the in-game date has not moved (ACT 01).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..rules.authority import Allowed, AuthorityProfile, OutsideScope, authorize
from ..rules.capabilities import ACTION_REQUIREMENTS, CapabilityRegistry
from ..state.identity import new_id
from ..state.records import ActionIntent, ActionState, DecisionSnapshot, payload_hash
from ..state.status import MissingCapabilityReport

LIFECYCLE_VERSION = "execution.lifecycle/1"

# Preconditions every intent carries; workflows may add their own.
DEFAULT_PRECONDITIONS = ("same_career_and_branch", "verified_stable_decision_point", "unchanged_relevant_state")

# Hash length kept in the idempotency key. 16 hex characters keeps keys readable in the journal.
IDEMPOTENCY_HASH_LENGTH = 16


class LifecycleError(ValueError):
    """The intent cannot be created from the given snapshot."""


def idempotency_key(branch_id: str, decision_ref: str, kind: str, targets: dict[str, Any], parameters: dict[str, Any]) -> str:
    """``branch:decision-or-snapshot:kind:hash`` - the same decision on the same branch never dispatches twice."""
    digest = payload_hash({"targets": targets, "parameters": parameters})[:IDEMPOTENCY_HASH_LENGTH]
    return f"{branch_id}:{decision_ref}:{kind}:{digest}"


class IntentFactory:
    """Builds intents from a valid decision snapshot and (optionally) persists them."""

    def __init__(self, store=None):
        self.store = store

    def create(self, kind: str, authority_scope: str, snapshot: DecisionSnapshot, targets: dict[str, Any], parameters: dict[str, Any], *, preconditions: Iterable[str] = DEFAULT_PRECONDITIONS, required_capabilities: Iterable[str] | None = None, verification: str, risk_class: str = "consequential", decision_id: str | None = None, expires_on: str = "relevant_state_change") -> ActionIntent:
        if not snapshot.valid:
            raise LifecycleError(f"cannot build an intent from an inconsistent snapshot ({snapshot.consistency.value}): {snapshot.consistency_reasons}")
        if not snapshot.career_id or not snapshot.branch_id:
            raise LifecycleError("snapshot has no career/branch lineage; register the career first")
        if risk_class not in ("navigation", "consequential"):
            raise LifecycleError(f"unknown risk class {risk_class!r}")
        versions = self._entity_versions(snapshot, targets)
        capabilities = list(required_capabilities) if required_capabilities is not None else list(ACTION_REQUIREMENTS.get(kind, []))
        intent = ActionIntent(
            new_id("act"), kind, authority_scope, snapshot.career_id, snapshot.branch_id, snapshot.snapshot_id,
            dict(targets), dict(parameters), list(preconditions), capabilities, expires_on, verification,
            idempotency_key(snapshot.branch_id, decision_id or snapshot.snapshot_id, kind, targets, parameters),
            entity_versions=versions, risk_class=risk_class, decision_id=decision_id,
        )
        if self.store is not None:
            self.store.insert_intent(intent)
        return intent

    @staticmethod
    def _entity_versions(snapshot: DecisionSnapshot, targets: dict[str, Any]) -> dict[str, str]:
        """Copy the payload hashes of every targeted route so execution can check freshness (ACT 01)."""
        versions: dict[str, str] = {}
        for route in targets.get("routes", []):
            if route not in snapshot.entity_versions:
                raise LifecycleError(f"target route {route} was not collected in snapshot {snapshot.snapshot_id}; an intent cannot reference unobserved state")
            versions[route] = snapshot.entity_versions[route]
        for pid in targets.get("player_ids", []):
            route = f"/players/{pid}"
            if route in snapshot.entity_versions:
                versions[route] = snapshot.entity_versions[route]
        return versions


def transition(store, intent: ActionIntent, state: ActionState, reason: str | None = None) -> ActionIntent:
    """Move an intent to ``state`` through the store; illegal transitions raise ``StoreError``."""
    return store.update_intent_state(intent, state, reason)


def context_changes(intent: ActionIntent, fresh: DecisionSnapshot) -> list[str]:
    """Why ``fresh`` no longer matches the context the intent was built on. Empty means unchanged."""
    reasons: list[str] = []
    if fresh.career_id != intent.career_id or fresh.branch_id != intent.branch_id:
        reasons.append(f"career/branch {fresh.career_id}/{fresh.branch_id} differs from intent {intent.career_id}/{intent.branch_id}")
    if not fresh.valid:
        reasons.append(f"fresh snapshot is not consistent ({fresh.consistency.value})")
    for route, version in intent.entity_versions.items():
        current = fresh.entity_versions.get(route)
        if current is None:
            reasons.append(f"{route} not present in fresh snapshot")
        elif current != version:
            reasons.append(f"{route} changed since the decision ({version[:12]} -> {current[:12]})")
    return reasons


def expire_if_context_changed(intent: ActionIntent, fresh: DecisionSnapshot, store=None) -> bool:
    """True when the intent's relevant context changed; persists EXPIRED when a store is given and the transition is legal.

    The game date is deliberately not consulted: a changed offer, player or
    tactic on the same morning invalidates the intent just as much.
    """
    reasons = context_changes(intent, fresh)
    if not reasons:
        return False
    if store is not None and intent.state in (ActionState.PROPOSED, ActionState.VALIDATED, ActionState.QUEUED):
        transition(store, intent, ActionState.EXPIRED, "context changed: " + "; ".join(reasons))
    return True


@dataclass
class ValidationResult:
    state: ActionState
    reasons: list[str] = field(default_factory=list)
    capability_report: MissingCapabilityReport | None = None
    authorization: Allowed | OutsideScope | None = None

    @property
    def ok(self) -> bool:
        return self.state is ActionState.VALIDATED

    def to_json(self) -> dict[str, Any]:
        auth = None
        if self.authorization is not None:
            auth = {"allowed": self.authorization.allowed, "scope": self.authorization.scope, "profile_version": self.authorization.profile_version, "reasons": getattr(self.authorization, "reasons", getattr(self.authorization, "notes", []))}
        return {"state": self.state.value, "reasons": list(self.reasons), "capability_report": self.capability_report.to_json() if self.capability_report else None, "authorization": auth}


def validate(intent: ActionIntent, registry: CapabilityRegistry, profile: AuthorityProfile, snapshot: DecisionSnapshot, store=None) -> ValidationResult:
    """PROPOSED -> VALIDATED or OUTSIDE_SCOPE.

    Order: freshness against ``snapshot`` (an intent built on stale state is
    expired), then required capabilities (an unknown prerequisite blocks the
    action), then the authority profile with its numeric limits.
    """
    if intent.state is not ActionState.PROPOSED:
        raise LifecycleError(f"only PROPOSED intents are validated; {intent.action_id} is {intent.state.value}")
    changes = context_changes(intent, snapshot)
    if changes:
        if store is not None:
            transition(store, intent, ActionState.EXPIRED, "context changed before validation: " + "; ".join(changes))
        return ValidationResult(ActionState.EXPIRED, changes)
    report = registry.check(intent.kind, extra=intent.required_capabilities)
    decision = authorize(intent, profile, report)
    if not decision.allowed:
        reasons = list(decision.reasons)
        if store is not None:
            transition(store, intent, ActionState.OUTSIDE_SCOPE, "; ".join(reasons))
        return ValidationResult(ActionState.OUTSIDE_SCOPE, reasons, report, decision)
    if store is not None:
        transition(store, intent, ActionState.VALIDATED, f"authorized under profile v{profile.version}; capabilities present")
    return ValidationResult(ActionState.VALIDATED, list(decision.notes), report, decision)


def enqueue(store, intent: ActionIntent, reason: str = "queued for the single UI writer") -> ActionIntent:
    return transition(store, intent, ActionState.QUEUED, reason)


def duplicate_of(store, intent: ActionIntent) -> ActionIntent | None:
    """An already-recorded intent with the same idempotency key but a different action id."""
    existing = store.find_intent_by_key(intent.idempotency_key)
    if existing is None or existing.action_id == intent.action_id:
        return None
    return existing
