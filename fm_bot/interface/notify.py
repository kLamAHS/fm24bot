"""Operator notifications with a meaningful-event policy (spec 15.1).

The manager's phone should buzz for five things only: the game needs a
person (a required decision, a lineup only a human may confirm), a mandatory
workflow the bot cannot perform, a material change of plan, a completed
action, and a failure. An unchanged poll never produces a message: the
notifier fingerprints each event and suppresses repeats of the same event
for the same subject.

Sinks are pluggable (a log, a callback, an in-memory list for tests) and the
notifier journals every delivered notification when a store is attached.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from ..state.identity import new_id, utc_now
from ..state.records import Decision, payload_hash
from ..state.status import MissingCapabilityReport

NOTIFY_POLICY_VERSION = "interface.notify/1"

# Fields of a decision's selected action whose change is a material plan change.
# Versioned so a reviewer can see what counts; scores alone do not.
PLAN_CHANGE_FIELDS: tuple[str, ...] = ("kind", "targets", "parameters", "player_ids", "action", "tactic_catalog_id", "option_id")


class NotificationKind(str, Enum):
    USER_ACTION_REQUIRED = "user_action_required"
    UNSUPPORTED_MANDATORY_WORKFLOW = "unsupported_mandatory_workflow"
    PLAN_CHANGED = "plan_changed"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class Notification:
    notification_id: str
    kind: NotificationKind
    title: str
    detail: str
    ref_id: str | None
    fingerprint: str
    at: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"notification_id": self.notification_id, "kind": self.kind.value, "title": self.title, "detail": self.detail, "ref_id": self.ref_id, "fingerprint": self.fingerprint, "at": self.at, "context": dict(self.context)}


class NotificationSink(Protocol):
    def deliver(self, notification: Notification) -> None: ...


class LogSink:
    """Writes notifications to a standard logger (IDs and typed fields, spec 15.3)."""

    def __init__(self, logger: logging.Logger | None = None, level: int = logging.INFO):
        self.logger = logger or logging.getLogger("fm_bot.notify")
        self.level = level

    def deliver(self, notification: Notification) -> None:
        self.logger.log(self.level, "%s %s: %s [%s]", notification.kind.value, notification.title, notification.detail, notification.ref_id)


class CallbackSink:
    def __init__(self, callback: Callable[[Notification], None]):
        self.callback = callback

    def deliver(self, notification: Notification) -> None:
        self.callback(notification)


class MemorySink:
    """Keeps every delivered notification; used by tests and the operator view."""

    def __init__(self):
        self.delivered: list[Notification] = []

    def deliver(self, notification: Notification) -> None:
        self.delivered.append(notification)


@dataclass
class NotificationPolicy:
    """Which kinds may be delivered and whether identical repeats are suppressed."""

    enabled: frozenset[NotificationKind] = frozenset(NotificationKind)
    suppress_repeats: bool = True
    version: str = NOTIFY_POLICY_VERSION

    def allows(self, kind: NotificationKind) -> bool:
        return kind in self.enabled


def _selected_summary(decision: Decision | None) -> dict[str, Any]:
    if decision is None or not decision.selected:
        return {}
    return {name: decision.selected.get(name) for name in PLAN_CHANGE_FIELDS if name in decision.selected}


def material_plan_change(previous: Decision | None, current: Decision) -> list[str]:
    """Why ``current`` is a material change from ``previous`` (empty when it is not).

    A change of the selected action or of the binding constraints is material;
    a different score for the same action is not. A first decision of a kind
    is a change only when it selects something.
    """
    reasons: list[str] = []
    before, after = _selected_summary(previous), _selected_summary(current)
    if previous is None:
        if after:
            reasons.append(f"first {current.kind} decision selects {after.get('kind') or after.get('action') or 'an action'}")
        return reasons
    if previous.kind != current.kind:
        reasons.append(f"decision kind changed {previous.kind} -> {current.kind}")
    if before != after:
        reasons.append("selected action changed: " + ", ".join(f"{k}: {before.get(k)!r} -> {after.get(k)!r}" for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)))
    binding_before = sorted(c.get("name", "") for c in previous.constraints if c.get("binding") or c.get("status") == "fail")
    binding_after = sorted(c.get("name", "") for c in current.constraints if c.get("binding") or c.get("status") == "fail")
    if binding_before != binding_after:
        reasons.append(f"binding constraints changed {binding_before} -> {binding_after}")
    return reasons


class Notifier:
    """Delivers notifications that pass the policy; never one for an unchanged poll."""

    def __init__(self, sinks: list[NotificationSink] | None = None, policy: NotificationPolicy | None = None, store=None):
        self.sinks = list(sinks or [])
        self.policy = policy or NotificationPolicy()
        self.store = store
        self.history: list[Notification] = []
        self.suppressed = 0
        self._last_fingerprint: dict[tuple[str, str | None], str] = {}

    def add_sink(self, sink: NotificationSink) -> None:
        self.sinks.append(sink)

    def notify(self, kind: NotificationKind, title: str, detail: str, *, ref_id: str | None = None, fingerprint: Any = None, subject: str | None = None, context: dict[str, Any] | None = None) -> Notification | None:
        """Deliver unless the policy disables the kind or the same event was already reported for this subject.

        ``subject`` (default ``ref_id``) names what the event is about; a
        repeat is the same kind, the same subject and the same fingerprint as
        the last delivery for that subject.
        """
        if not self.policy.allows(kind):
            return None
        digest = payload_hash(fingerprint if fingerprint is not None else {"title": title, "detail": detail})
        subject = (kind.value, subject if subject is not None else ref_id)
        if self.policy.suppress_repeats and self._last_fingerprint.get(subject) == digest:
            self.suppressed += 1
            return None
        self._last_fingerprint[subject] = digest
        notification = Notification(new_id("ntf"), kind, title, detail, ref_id, digest, utc_now(), dict(context or {}))
        for sink in self.sinks:
            sink.deliver(notification)
        self.history.append(notification)
        if self.store is not None:
            self.store.journal("notification", notification.to_json(), ref_id)
        return notification

    def poll_unchanged(self) -> None:
        """Explicitly nothing: an unchanged poll is not an event."""
        return None

    # ----- the five meaningful events -----
    def required_action(self, subject: str, blockers: list[str], *, ref_id: str | None = None) -> Notification | None:
        if not blockers:
            return None
        return self.notify(NotificationKind.USER_ACTION_REQUIRED, f"The club needs you: {subject}", "; ".join(blockers), ref_id=ref_id, fingerprint={"subject": subject, "blockers": sorted(blockers)})

    def unsupported_workflow(self, report: MissingCapabilityReport, *, ref_id: str | None = None) -> Notification | None:
        if not report.blocked:
            return None
        detail = "; ".join(f"{name}: {report.reasons.get(name, 'not provided')}" for name in report.missing)
        return self.notify(NotificationKind.UNSUPPORTED_MANDATORY_WORKFLOW, f"Cannot handle {report.blocked_action} yet", detail, ref_id=ref_id, fingerprint=report.to_json(), subject=f"{ref_id}:{report.blocked_action}")

    def plan_changed(self, previous: Decision | None, current: Decision) -> Notification | None:
        reasons = material_plan_change(previous, current)
        if not reasons:
            return None
        return self.notify(NotificationKind.PLAN_CHANGED, f"Plan changed: {current.kind}", "; ".join(reasons), ref_id=current.decision_id, fingerprint={"decision": current.decision_id, "reasons": reasons})

    def completed(self, action_id: str, description: str, *, detail: str = "") -> Notification | None:
        return self.notify(NotificationKind.COMPLETED, f"Done: {description}", detail or "confirmed by readback", ref_id=action_id, fingerprint={"action": action_id, "outcome": "completed"})

    def failed(self, subject: str, reason: str, *, ref_id: str | None = None) -> Notification | None:
        return self.notify(NotificationKind.FAILED, f"Failed: {subject}", reason, ref_id=ref_id, fingerprint={"subject": subject, "reason": reason})
