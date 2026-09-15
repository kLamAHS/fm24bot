"""Pending actions, mandatory decisions and the Continue gate (spec 3.2 P0, 12.4, CAL 01).

The bridge exposes inbox *metadata* only: an event type string, the unread
flag and ``text_status == "not_decoded"``. Whether a message demands a
decision before the calendar can advance is therefore a conservative
heuristic on the event type until an ``inbox_text`` / ``pending_actions``
provider exists. The heuristic errs towards blocking: an unread message
whose type merely *looks* mandatory blocks Continue, and a read message
that looks mandatory keeps blocking too, because "read" is not "answered".
Only a text provider that says no decision is required, or a
:class:`PendingActionsObservation` at the snapshot's game time (with the
``pending_actions`` capability supported) that reports the message
resolved, lifts it. The heuristic can still miss a required decision whose
event type has no matching substring; that is why ``progress.continue``
also requires the ``pending_actions`` capability in
:data:`ACTION_REQUIREMENTS`.

Baseline only: no learned classifier is used here. If a language model is
ever attached to read message text it is a separate, gated provider.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

from ..state.records import CompetitionContext, DecisionSnapshot
from ..state.status import MissingCapabilityReport
from ..state.units import parse_date
from ..state.views import FixtureView, horizon_truncated, upcoming_fixtures
from .authority import AuthorityMode
from .capabilities import ACTION_REQUIREMENTS

DEADLINES_RULES_VERSION = "deadlines-rules/1.0"

# Substrings of bridge inbox ``event_type`` values that indicate a decision is
# required before the calendar may advance. CONSERVATIVE HEURISTIC: it is a
# list of things we know demand an answer, not a proof that anything else is
# informational. Review and version it when new event types are observed.
MANDATORY_EVENT_PATTERNS_VERSION = "mandatory-event-patterns/1.0"
MANDATORY_EVENT_PATTERNS: tuple[str, ...] = (
    "offer", "bid", "contract", "negotiat", "board_meeting", "board_request", "board_ultimatum",
    "registration", "squad_list", "press_conference", "team_talk", "team_meeting", "deadline",
    "respond", "request", "decision", "approval", "confirm", "job_offer", "expir", "ultimatum",
    "promise", "complaint", "unhappy", "loan_recall", "release",
)

# Event types that never require a decision, even when they match a pattern
# above. Kept short and explicit; extend only with observed examples.
INFORMATIONAL_EVENT_TYPES: frozenset[str] = frozenset({"news_item_training", "news_item_match_report", "news_item_results"})

# Continue is gated on lineup verification when the next fixture is this close.
LINEUP_GATE_HORIZON_DAYS = 1
# Registration deadlines block Continue when they fall within this many days.
REGISTRATION_DEADLINE_WINDOW_DAYS = 7
EXECUTION_MODES: frozenset[AuthorityMode] = frozenset({AuthorityMode.SCOPED_EXECUTION, AuthorityMode.CLUB_AUTONOMY})

CLASSIFICATION_MANDATORY_CONFIRMED = "mandatory_confirmed"       # a text/pending-actions provider said so
CLASSIFICATION_MANDATORY_HEURISTIC = "mandatory_heuristic"       # event type matched a pattern; text not decoded
CLASSIFICATION_UNRESOLVED_READ = "unresolved_read_heuristic"     # read, looks mandatory, resolution unknown: blocks until observed resolved
CLASSIFICATION_UNCLASSIFIED = "unclassified"                     # unread, no pattern match, text not decoded
CLASSIFICATION_INFORMATIONAL = "informational"                   # provider or explicit list says no decision needed

# A text provider returns None (cannot read) or a dict with at least
# {"requires_decision": bool}; optionally "deadline", "options", "text".
InboxTextProvider = Callable[[dict[str, Any]], "dict[str, Any] | None"]


@dataclass
class PendingAction:
    """One thing the manager may have to resolve before the calendar advances."""

    action_id: str                     # e.g. "inbox:501", "registration:14:current:2024-03-01"
    kind: str                          # inbox_message | registration_deadline | rules_deadline
    classification: str
    blocks_continue: bool
    description: str
    source: str
    message_id: int | None = None
    event_type: str | None = None
    unread: bool | None = None
    text_status: str | None = None
    matched_patterns: list[str] = field(default_factory=list)
    deadline_date: str | None = None
    deadline_time: str | None = None
    game_date: str | None = None
    game_time: str | None = None
    requires_capability: str | None = None
    options: list[str] | None = None
    resolved: bool = False
    rules_version: str = DEADLINES_RULES_VERSION

    def to_json(self) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in self.__dict__.items()}


def classify_event_type(event_type: str | None) -> list[str]:
    """Patterns matched by an inbox event type (empty when nothing matched)."""
    if not event_type:
        return []
    lowered = event_type.lower()
    return [p for p in MANDATORY_EVENT_PATTERNS if p in lowered]


def _message_action(message: dict[str, Any], text_provider: InboxTextProvider | None) -> PendingAction | None:
    event_type = message.get("event_type")
    unread = message.get("unread")
    matched = classify_event_type(event_type)
    base = dict(action_id=f"inbox:{message.get('id')}", kind="inbox_message", source="bridge:/inbox", message_id=message.get("id"), event_type=event_type, unread=unread, text_status=message.get("text_status"), matched_patterns=matched, game_date=message.get("date"), game_time=message.get("time"))
    reading = text_provider(message) if text_provider is not None else None
    if reading is not None and "requires_decision" in reading:
        if reading["requires_decision"]:
            return PendingAction(classification=CLASSIFICATION_MANDATORY_CONFIRMED, blocks_continue=True, description=f"{event_type}: decision required ({reading.get('text') or 'text read by provider'})", deadline_date=reading.get("deadline"), options=list(reading.get("options") or []) or None, **base)
        return PendingAction(classification=CLASSIFICATION_INFORMATIONAL, blocks_continue=False, description=f"{event_type}: informational", resolved=True, **base)
    if event_type in INFORMATIONAL_EVENT_TYPES:
        return PendingAction(classification=CLASSIFICATION_INFORMATIONAL, blocks_continue=False, description=f"{event_type}: informational by explicit list", resolved=True, **base)
    if matched and unread:
        return PendingAction(classification=CLASSIFICATION_MANDATORY_HEURISTIC, blocks_continue=True, description=f"unread {event_type} looks like a required decision (matched {', '.join(matched)}); text not decoded", requires_capability="inbox_text", **base)
    if matched:
        return PendingAction(classification=CLASSIFICATION_UNRESOLVED_READ, blocks_continue=True, description=f"read {event_type} looks like a decision (matched {', '.join(matched)}); whether it was answered cannot be verified without a pending_actions observation", requires_capability="pending_actions", **base)
    if unread:
        return PendingAction(classification=CLASSIFICATION_UNCLASSIFIED, blocks_continue=False, description=f"unread {event_type} matched no mandatory pattern; text not decoded so it may still require a decision", requires_capability="inbox_text", **base)
    return None


def pending_actions(snapshot: DecisionSnapshot, inbox_text_provider: InboxTextProvider | None = None) -> list[PendingAction]:
    """Pending actions derived from the snapshot's inbox metadata.

    Read messages that match no pattern are dropped; everything else is kept
    with an explicit classification so the operator can see what the bot
    could not decide. An uncollected ``/inbox`` yields no actions here and
    is reported by :func:`continue_gate` as a missing ``inbox_metadata``.
    """
    inbox = snapshot.routes.get("/inbox")
    if not inbox:
        return []
    result: list[PendingAction] = []
    for message in inbox.get("messages", []) or []:
        action = _message_action(message, inbox_text_provider)
        if action is not None:
            result.append(action)
    return result


def registration_actions(contexts: Iterable[CompetitionContext], game_date: str | None, *, window_days: int = REGISTRATION_DEADLINE_WINDOW_DAYS) -> list[PendingAction]:
    """Deadlines from rules profiles that fall inside the blocking window."""
    if game_date is None:
        return []
    today = parse_date(game_date)
    result: list[PendingAction] = []
    for context in contexts:
        for entry in context.deadlines:
            if not entry.get("date"):
                continue
            due = parse_date(entry["date"])
            if due < today:
                continue
            within = (due - today).days <= window_days
            kind = "registration_deadline" if entry.get("kind") == "registration_deadline" else "rules_deadline"
            resolved = bool(entry.get("resolved"))
            result.append(PendingAction(action_id=f"{kind}:{context.competition_id}:{context.stage or 'current'}:{entry['date']}", kind=kind, classification=CLASSIFICATION_MANDATORY_CONFIRMED if within and not resolved else CLASSIFICATION_INFORMATIONAL, blocks_continue=within and not resolved, description=f"{context.competition_name or context.competition_id}: {entry.get('description') or kind} on {entry['date']}", source=f"rules_profile:{context.source}", deadline_date=entry["date"], deadline_time=entry.get("time"), game_date=game_date, resolved=resolved))
    return result


@dataclass(frozen=True)
class PendingActionsObservation:
    """What a ``pending_actions`` provider reported at one in-game moment (spec 12.4, CAL 01).

    ``resolved_action_ids`` are the pending-action ids (``inbox:<id>``,
    ``registration_deadline:...``) the provider observed as answered or
    cleared. Only an observation at the snapshot's own game date *and*
    time counts: in-game time is the clock, and a reading from another
    moment proves nothing about this one.
    """

    resolved_action_ids: frozenset[str]
    source: str
    game_date: str | None
    game_time: str | None
    observed_at: str | None = None

    def current_for(self, snapshot: DecisionSnapshot) -> bool:
        return self.game_date is not None and self.game_time is not None and self.game_date == snapshot.game_date and self.game_time == snapshot.game_time

    def reports_resolved(self, action_id: str) -> bool:
        return action_id in self.resolved_action_ids

    def to_json(self) -> dict[str, Any]:
        return {"resolved_action_ids": sorted(self.resolved_action_ids), "source": self.source, "game_date": self.game_date, "game_time": self.game_time, "observed_at": self.observed_at}


def resolve_read_actions(snapshot: DecisionSnapshot, pending: Iterable[PendingAction], observation: PendingActionsObservation | None, *, capabilities=None) -> tuple[list[PendingAction], list[str]]:
    """Copies of ``pending`` in which read-but-unresolved messages are marked resolved only on observed evidence.

    A ``CLASSIFICATION_UNRESOLVED_READ`` action is resolved when the
    ``pending_actions`` capability is supported *and* a current
    :class:`PendingActionsObservation` reports its id. Anything less (no
    observation, an observation from another game time, an unsupported
    capability) leaves it blocking, with a note saying why.
    """
    notes: list[str] = []
    result: list[PendingAction] = []
    supported, reason = _capability_supported(snapshot, capabilities, "pending_actions")
    for action in pending:
        if action.classification != CLASSIFICATION_UNRESOLVED_READ or action.resolved:
            result.append(action)
            continue
        if observation is None:
            result.append(action)
            continue
        if not supported:
            notes.append(f"{action.action_id}: pending-actions observation from {observation.source} ignored; pending_actions capability {reason}")
            result.append(action)
            continue
        if not observation.current_for(snapshot):
            notes.append(f"{action.action_id}: pending-actions observation at {observation.game_date} {observation.game_time} is not the snapshot game time {snapshot.game_date} {snapshot.game_time}; resolution unverified")
            result.append(action)
            continue
        if observation.reports_resolved(action.action_id):
            notes.append(f"{action.action_id}: reported resolved by {observation.source} at {observation.game_date} {observation.game_time}")
            result.append(replace(action, resolved=True, blocks_continue=False, description=f"{action.description}; reported resolved by {observation.source}"))
        else:
            result.append(action)
    return result, notes


@dataclass
class LineupStatus:
    """What selection reported for the next fixture's lineup."""

    status: str                                 # verified | unverified | ineligible | infeasible | missing | not_required
    fixture_identity: str | None = None
    unverified_players: list[int] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> "LineupStatus":
        if not data:
            return cls("missing", reasons=["no lineup status supplied"])
        return cls(data.get("status", "missing"), data.get("fixture_identity"), list(data.get("unverified_players") or []), list(data.get("reasons") or []))


@dataclass
class DecisionBoundary:
    """The expected next stable point after Continue (spec 12.4)."""

    kind: str                       # fixture | deadline | pending_action | unknown
    date: str | None
    time: str | None
    identity: str | None
    description: str
    source: str
    truncated_horizon: bool = False

    def to_json(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ContinueGate:
    allowed: bool
    blockers: list[str]
    missing_capabilities: MissingCapabilityReport
    pending: list[PendingAction] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    next_boundary: DecisionBoundary | None = None
    rules_version: str = DEADLINES_RULES_VERSION

    def to_json(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "blockers": list(self.blockers), "missing_capabilities": self.missing_capabilities.to_json(), "pending": [p.to_json() for p in self.pending], "notes": list(self.notes), "next_boundary": self.next_boundary.to_json() if self.next_boundary else None, "rules_version": self.rules_version}


def _capability_supported(snapshot: DecisionSnapshot, capabilities, name: str) -> tuple[bool, str]:
    if capabilities is not None:
        if capabilities.supported(name):
            return True, "supported"
        item = capabilities.capabilities.get(name)
        return False, (f"{item.status.value} ({item.provider}): {item.note or 'no detail'}" if item else "no provider registered")
    if name in (snapshot.capabilities or []):
        return True, "bridge"
    if name in (snapshot.unresolved or []):
        return False, "reported unresolved by the bridge"
    return False, "no provider registered"


def continue_gate(snapshot: DecisionSnapshot, pending: list[PendingAction], rules_contexts: list[CompetitionContext], lineup_status: LineupStatus | dict[str, Any] | None, *, authority_mode: AuthorityMode = AuthorityMode.ADVISE, capabilities=None, lineup_horizon_days: int = LINEUP_GATE_HORIZON_DAYS, registration_window_days: int = REGISTRATION_DEADLINE_WINDOW_DAYS, pending_observation: PendingActionsObservation | None = None) -> ContinueGate:
    """Decide whether the calendar may be advanced (spec 12.4, CAL 01).

    ``blockers`` are things a person or a prior action can resolve (an
    unread or read-but-unanswered required decision, an unverified lineup
    before a fixture, a registration deadline inside its window, an invalid
    snapshot). ``missing_capabilities`` names subsystems
    ``progress.continue`` needs that nobody provides. Continue is allowed
    only when both are empty. ``capabilities`` may be a
    :class:`CapabilityRegistry`; without one the snapshot's own capability
    lists are consulted. A read mandatory-looking message stops blocking
    only when ``pending_observation`` (current for this snapshot, with the
    ``pending_actions`` capability supported) reports it resolved; see
    :func:`resolve_read_actions`.
    """
    lineup = lineup_status if isinstance(lineup_status, LineupStatus) else LineupStatus.from_json(lineup_status)
    report = MissingCapabilityReport("progress.continue")
    blockers: list[str] = []
    pending, notes = resolve_read_actions(snapshot, pending, pending_observation, capabilities=capabilities)
    for name in ACTION_REQUIREMENTS["progress.continue"]:
        ok, reason = _capability_supported(snapshot, capabilities, name)
        if not ok:
            report.add(name, reason)
    if not snapshot.valid:
        blockers.append(f"snapshot is not consistent ({snapshot.consistency.value}): " + "; ".join(snapshot.consistency_reasons))
    if "/inbox" not in snapshot.routes:
        blockers.append("inbox metadata was not collected; pending decisions cannot be checked")
        report.add("inbox_metadata", "/inbox not in snapshot")
    all_pending = list(pending) + registration_actions(rules_contexts, snapshot.game_date, window_days=registration_window_days)
    for action in all_pending:
        if action.blocks_continue and not action.resolved:
            blockers.append(f"{action.action_id}: {action.description}")
            if action.requires_capability:
                ok, reason = _capability_supported(snapshot, capabilities, action.requires_capability)
                if not ok:
                    previous = report.reasons.get(action.requires_capability)
                    report.add(action.requires_capability, f"{previous}; also {action.action_id}" if previous else f"needed to resolve {action.action_id}: {reason}")
        elif action.classification == CLASSIFICATION_UNCLASSIFIED:
            notes.append(f"{action.action_id}: {action.description}")
    for context in rules_contexts:
        if context.squad_rules.get("status") == "missing":
            notes.append(f"competition {context.competition_id} ({context.competition_name}): rules profile missing; deadlines unknown")
    blockers.extend(_lineup_blockers(snapshot, lineup, authority_mode, lineup_horizon_days, notes))
    boundary = next_decision_boundary(snapshot, all_pending, rules_contexts)
    allowed = not blockers and not report.blocked
    return ContinueGate(allowed, blockers, report, all_pending, notes, boundary)


def _lineup_blockers(snapshot: DecisionSnapshot, lineup: LineupStatus, mode: AuthorityMode, horizon_days: int, notes: list[str]) -> list[str]:
    nxt = upcoming_fixtures(snapshot, 1)
    if not nxt or snapshot.game_date is None:
        return []
    days_ahead = (parse_date(nxt[0].date) - parse_date(snapshot.game_date)).days
    if days_ahead > horizon_days:
        return []
    fixture = nxt[0]
    if lineup.status == "verified" and (lineup.fixture_identity in (None, fixture.identity)):
        return []
    text = f"lineup for {fixture.identity} is {lineup.status}" + (f" ({'; '.join(lineup.reasons)})" if lineup.reasons else "")
    if lineup.status == "verified":
        text = f"lineup was verified for {lineup.fixture_identity}, not for the next fixture {fixture.identity}"
    if mode in EXECUTION_MODES or lineup.status in ("ineligible", "infeasible"):
        return [text]
    notes.append(f"advisory: {text}")
    return []


def next_decision_boundary(snapshot: DecisionSnapshot, pending: Iterable[PendingAction] = (), rules_contexts: Iterable[CompetitionContext] = ()) -> DecisionBoundary:
    """The earliest of: next scheduled fixture, next pending deadline, next rules deadline.

    Always returns a boundary; ``kind == "unknown"`` states why none could be
    identified (no game date, or the fixture horizon is truncated at the
    calendar-year boundary and no dated deadline is known).
    """
    if snapshot.game_date is None:
        return DecisionBoundary("unknown", None, None, None, "snapshot has no game date", "snapshot")
    today = parse_date(snapshot.game_date)
    candidates: list[tuple[dt.date, str, DecisionBoundary]] = []
    nxt = upcoming_fixtures(snapshot, 1)
    if nxt:
        f: FixtureView = nxt[0]
        candidates.append((parse_date(f.date), f.time or "", DecisionBoundary("fixture", f.date, f.time, f.identity, f"{f.competition_name}: {f.home_name} v {f.away_name}", "bridge:/fixtures")))
    for action in pending:
        if action.deadline_date and parse_date(action.deadline_date) >= today:
            candidates.append((parse_date(action.deadline_date), action.deadline_time or "", DecisionBoundary("pending_action" if action.kind == "inbox_message" else "deadline", action.deadline_date, action.deadline_time, action.action_id, action.description, action.source)))
    for context in rules_contexts:
        for entry in context.deadlines:
            if entry.get("date") and parse_date(entry["date"]) >= today:
                candidates.append((parse_date(entry["date"]), entry.get("time") or "", DecisionBoundary("deadline", entry["date"], entry.get("time"), f"{context.competition_id}:{context.stage}:{entry['date']}", entry.get("description") or entry.get("kind", "deadline"), f"rules_profile:{context.source}")))
    truncated = horizon_truncated(snapshot, 1)
    if not candidates:
        reason = "fixture horizon truncated at the calendar-year boundary and no dated deadline known" if truncated else "no scheduled fixture or dated deadline observed"
        return DecisionBoundary("unknown", None, None, None, reason, "bridge:/fixtures", truncated)
    candidates.sort(key=lambda c: (c[0], c[1]))
    boundary = candidates[0][2]
    boundary.truncated_horizon = truncated
    return boundary
