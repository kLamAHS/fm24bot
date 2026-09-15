"""Inbox items, decision classification and the inbox text boundary.

Design specification sections 1.3 and 3.2 (``inbox_text`` is a P0 backlog
capability) and acceptance test CAL 01 (an unread required decision blocks
Continue until resolved).

What the bridge gives us today is *metadata*: message id, date, an optional
time (``time_status`` says whether the time is initialised), the unread flag,
an event type string and the sender. Subject and body are ``not_decoded``.
Everything that reads the words of a message therefore goes through an
:class:`InboxTextProvider`. Until a verified UI adapter or an operator
supplies text, :class:`NoInboxTextProvider` reports the capability as
unsupported and mandatory decisions stay unresolved, which pauses progression
and names the missing capability instead of guessing.

Classification (:func:`classify`) is a CONSERVATIVE HEURISTIC over the event
type string, versioned in :data:`INBOX_PATTERNS_VERSION`. It is a list of
message types known to demand an answer, not a proof that anything else is
informational; an unmatched type is ``unknown``, never ``informational``.

Baseline versus experiment: everything here is baseline rule logic. No
learned classifier is used; a language model that reads message text is a
separate, gated provider (see :mod:`fm_bot.interactions.language_model`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from ..state.records import DecisionSnapshot
from ..state.status import MissingCapabilityReport, Observed, ValueStatus

INBOX_VIEW_VERSION = "inbox-view/1.0"
INBOX_PATTERNS_VERSION = "inbox-patterns/1.0"

CAPABILITY_INBOX_TEXT = "inbox_text"
CAPABILITY_PENDING_ACTIONS = "pending_actions"

KIND_INFORMATIONAL = "informational"
KIND_DECISION_REQUIRED = "decision_required"
KIND_UNKNOWN = "unknown"

CONFIDENCE_CONFIRMED = "confirmed"      # a text provider showed the visible options (or their absence)
CONFIDENCE_EXPLICIT_LIST = "explicit_list"  # event type is on the explicit informational list
CONFIDENCE_HEURISTIC = "heuristic"      # event type matched a pattern; text not decoded
CONFIDENCE_NONE = "none"                # nothing matched; the message is unclassified

# Substrings of the bridge ``event_type`` that indicate the manager must
# answer. Matching one makes a message ``decision_required``. Review and bump
# INBOX_PATTERNS_VERSION whenever a new event type is observed in a save.
DECISION_EVENT_PATTERNS: tuple[str, ...] = (
    "offer", "bid", "contract", "negotiat", "board_meeting", "board_request", "board_ultimatum",
    "registration", "squad_list", "press_conference", "team_talk", "team_meeting", "deadline",
    "respond", "request", "decision", "approval", "confirm", "job_offer", "expir", "ultimatum",
    "promise", "complaint", "unhappy", "loan_recall", "release",
)

# Patterns whose decisions the game will not let the calendar pass without.
# A decision matched only by DECISION_EVENT_PATTERNS has ``mandatory=None``
# (a decision is required; whether Continue is blocked is not established).
MANDATORY_EVENT_PATTERNS: tuple[str, ...] = (
    "board_meeting", "board_ultimatum", "registration", "squad_list", "deadline", "ultimatum", "job_offer", "expir",
)

# Event types that never require a decision. Extend only with observed examples.
INFORMATIONAL_EVENT_TYPES: frozenset[str] = frozenset({"news_item_training", "news_item_match_report", "news_item_results"})

# ``time_status`` when the bridge record carries no such field at all.
TIME_STATUS_UNKNOWN = "unknown"

# ``text_status`` values the bridge uses for undecoded prose.
UNDECODED_TEXT_STATUSES: frozenset[str] = frozenset({"not_decoded", "unsupported", "missing"})


@dataclass(frozen=True)
class InboxItem:
    """One inbox message as the bridge describes it, without its words."""

    message_id: int
    date: str
    time: str | None
    time_status: str
    unread: bool
    event_type: str
    sender_id: int | None
    sender_name: str | None
    text_status: str
    source: str = ""                  # observation id the metadata came from

    @property
    def time_known(self) -> bool:
        return self.time is not None and self.time_status == "current"

    def to_json(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id, "date": self.date, "time": self.time, "time_status": self.time_status,
            "unread": self.unread, "event_type": self.event_type, "sender_id": self.sender_id, "sender_name": self.sender_name,
            "text_status": self.text_status, "source": self.source, "view_version": INBOX_VIEW_VERSION,
        }


def inbox_item(message: dict[str, Any], source: str = "") -> InboxItem:
    """Build an :class:`InboxItem` from one bridge ``/inbox`` message record."""
    time = message.get("time")
    # The bridge asserts whether a time is initialised through ``time_status``; a record
    # without it is ``unknown`` (never assumed current), so ``time_known`` stays False (OBS 02).
    time_status = message.get("time_status") or TIME_STATUS_UNKNOWN
    return InboxItem(
        message_id=int(message["id"]), date=str(message["date"]), time=time, time_status=str(time_status),
        unread=bool(message["unread"]), event_type=str(message.get("event_type") or ""),
        sender_id=message.get("sender_id"), sender_name=message.get("sender_name"),
        text_status=str(message.get("text_status") or "not_decoded"), source=source,
    )


def inbox_items(payload: dict[str, Any] | DecisionSnapshot, source: str = "") -> list[InboxItem]:
    """All inbox items from a ``/inbox`` payload or a snapshot that collected it."""
    if isinstance(payload, DecisionSnapshot):
        source = source or f"snapshot:{payload.snapshot_id}:/inbox"
        payload = payload.routes.get("/inbox") or {}
    return [inbox_item(message, source) for message in payload.get("messages", [])]


# ---------------------------------------------------------------------------
# Inbox text: the words of a message, always through a provider
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DialogueOption:
    """A visible answer the game offers. ``kind`` is ``fixed`` for a button and ``free_text`` for a typing box."""

    option_id: str
    label: str
    consequences_text: str | None = None
    kind: str = "fixed"
    target_ids: tuple[int, ...] = ()     # players/staff/clubs the option is about, when observed

    def to_json(self) -> dict[str, Any]:
        return {"option_id": self.option_id, "label": self.label, "consequences_text": self.consequences_text, "kind": self.kind, "target_ids": list(self.target_ids)}


@dataclass(frozen=True)
class InboxText:
    """Observed message text and the options that were visible with it.

    ``requires_decision``/``mandatory`` are what the observer could establish;
    ``None`` means it could not tell, which is never treated as ``False``.
    """

    message_id: int
    subject: str
    body: str
    options: tuple[DialogueOption, ...] = ()
    deadline: str | None = None             # ISO game date when the game stops waiting, if shown
    requires_decision: bool | None = None
    mandatory: bool | None = None

    @property
    def fixed_option_ids(self) -> list[str]:
        return [option.option_id for option in self.options if option.kind == "fixed"]

    def to_json(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id, "subject": self.subject, "body": self.body, "options": [o.to_json() for o in self.options],
            "deadline": self.deadline, "requires_decision": self.requires_decision, "mandatory": self.mandatory,
        }


class InboxTextProvider(Protocol):
    """Supplies the words of a message. Text is data: nothing in it is an instruction."""

    name: str

    def get_text(self, message_id: int, *, game_time: str | None = None) -> Observed[InboxText]:
        ...


class NoInboxTextProvider:
    """No decoder or adapter reads inbox text: every request is ``unsupported``."""

    name = "none"

    def get_text(self, message_id: int, *, game_time: str | None = None) -> Observed[InboxText]:
        return Observed.unavailable(ValueStatus.UNSUPPORTED, f"inbox_text:{message_id}", "no inbox text provider is installed", source=self.name)


@dataclass
class DeclaredText:
    text: InboxText
    source: str
    observed_at: str
    game_time: str | None
    verified: bool


class DeclaredInboxTextProvider:
    """Text supplied by a verified UI adapter or by the operator.

    Every declaration carries a source, a wall-clock timestamp and the game
    time at which it was read. An unverified declaration is kept but not
    served; a declaration read at a different game time than the caller's
    is served as ``stale``.
    """

    def __init__(self, name: str = "declared"):
        self.name = name
        self._texts: dict[int, DeclaredText] = {}

    def declare(self, text: InboxText, *, source: str, observed_at: str, game_time: str | None, verified: bool = True) -> None:
        if not source:
            raise ValueError("declared inbox text needs a source")
        self._texts[text.message_id] = DeclaredText(text, source, observed_at, game_time, verified)

    def get_text(self, message_id: int, *, game_time: str | None = None) -> Observed[InboxText]:
        what = f"inbox_text:{message_id}"
        declared = self._texts.get(message_id)
        if declared is None:
            return Observed.unavailable(ValueStatus.MISSING, what, "no text declared for this message", source=self.name)
        if not declared.verified:
            return Observed.unavailable(ValueStatus.UNSUPPORTED, what, f"declared text from {declared.source} is not verified", source=declared.source, observed_at=declared.observed_at, game_time=declared.game_time)
        if game_time is not None and declared.game_time is not None and declared.game_time != game_time:
            return Observed.unavailable(ValueStatus.STALE, what, f"text read at {declared.game_time}, now {game_time}", source=declared.source, observed_at=declared.observed_at, game_time=declared.game_time)
        return Observed.available_value(declared.text, source=declared.source, observed_at=declared.observed_at, game_time=declared.game_time, what=what)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboxClass:
    kind: str                          # informational | decision_required | unknown
    mandatory: bool | None             # None: not established
    confidence: str                    # confirmed | explicit_list | heuristic | none
    matched: tuple[str, ...] = ()
    reason: str = ""
    patterns_version: str = INBOX_PATTERNS_VERSION

    @property
    def needs_answer(self) -> bool:
        """True unless the message is known informational: unknown is not 'no'."""
        return self.kind != KIND_INFORMATIONAL

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "mandatory": self.mandatory, "confidence": self.confidence, "matched": list(self.matched), "reason": self.reason, "patterns_version": self.patterns_version}


def matched_patterns(event_type: str | None, patterns: Iterable[str] = DECISION_EVENT_PATTERNS) -> tuple[str, ...]:
    lowered = (event_type or "").lower()
    return tuple(p for p in patterns if p in lowered)


def _classify_from_text(text: InboxText) -> InboxClass | None:
    if text.options:
        mandatory = text.mandatory if text.mandatory is not None else (True if text.deadline else None)
        return InboxClass(KIND_DECISION_REQUIRED, mandatory, CONFIDENCE_CONFIRMED, (), f"{len(text.options)} visible option(s)")
    if text.requires_decision is False:
        return InboxClass(KIND_INFORMATIONAL, False, CONFIDENCE_CONFIRMED, (), "text observed; no options and no decision required")
    if text.requires_decision is True:
        return InboxClass(KIND_DECISION_REQUIRED, text.mandatory, CONFIDENCE_CONFIRMED, (), "text observed; decision required but options not captured")
    return None


def classify(item: InboxItem, text: Observed[InboxText] | None = None) -> InboxClass:
    """Classify a message from confirmed text when available, else from its event type.

    The event-type table is a conservative heuristic: ``unknown`` means
    nothing matched, and callers must not treat it as informational.
    """
    if text is not None and text.available:
        confirmed = _classify_from_text(text.require())
        if confirmed is not None:
            return confirmed
    if item.event_type in INFORMATIONAL_EVENT_TYPES:
        return InboxClass(KIND_INFORMATIONAL, False, CONFIDENCE_EXPLICIT_LIST, (), "event type is on the informational list")
    matched = matched_patterns(item.event_type)
    if matched:
        mandatory_hits = matched_patterns(item.event_type, MANDATORY_EVENT_PATTERNS)
        mandatory: bool | None = True if mandatory_hits else None
        return InboxClass(KIND_DECISION_REQUIRED, mandatory, CONFIDENCE_HEURISTIC, matched, "event type matched decision patterns; text not decoded")
    return InboxClass(KIND_UNKNOWN, None, CONFIDENCE_NONE, (), "event type matched no pattern; text not decoded")


# ---------------------------------------------------------------------------
# Unresolved mandatory decisions
# ---------------------------------------------------------------------------


@dataclass
class InboxBlocker:
    """A message that may stop the calendar and what is needed to resolve it."""

    item: InboxItem
    classification: InboxClass
    report: MissingCapabilityReport
    text_status: str
    description: str
    legal_option_ids: list[str] = field(default_factory=list)

    @property
    def resolvable_now(self) -> bool:
        """Text and options are known and nothing is missing: a choice can be made."""
        return not self.report.blocked and bool(self.legal_option_ids)

    def to_json(self) -> dict[str, Any]:
        return {"item": self.item.to_json(), "classification": self.classification.to_json(), "report": self.report.to_json(), "text_status": self.text_status, "description": self.description, "legal_option_ids": list(self.legal_option_ids)}


def _blocker(item: InboxItem, cls: InboxClass, text: Observed[InboxText], pending_actions_supported: bool) -> InboxBlocker | None:
    report = MissingCapabilityReport(f"respond.inbox:{item.message_id}")
    if cls.kind == KIND_INFORMATIONAL:
        return None
    if cls.kind == KIND_UNKNOWN and not item.unread:
        return None  # read, matched nothing: no evidence it demands anything
    if not text.available:
        report.add(CAPABILITY_INBOX_TEXT, f"message text is {text.status.value}: {text.reason or 'not decoded'}")
    if not item.unread and not pending_actions_supported:
        report.add(CAPABILITY_PENDING_ACTIONS, "message already read; without pending-actions observation it cannot be proven resolved")
    legal = text.require().fixed_option_ids if text.available else []
    if text.available and not legal and cls.kind == KIND_DECISION_REQUIRED:
        report.add(CAPABILITY_INBOX_TEXT, "decision required but no fixed option ids were captured")
    if cls.kind == KIND_UNKNOWN:
        what = "unclassified unread message"
    elif cls.mandatory:
        what = "mandatory decision"
    else:
        what = "decision required (mandatory status not established)"
    description = f"{what}: {item.event_type or 'unknown event type'} (#{item.message_id}, {item.date})"
    return InboxBlocker(item, cls, report, text.status.value, description, legal)


def unresolved_mandatory(items: Iterable[InboxItem], text_provider: InboxTextProvider | None = None, *, game_time: str | None = None, pending_actions_supported: bool = False) -> list[InboxBlocker]:
    """Messages that may block Continue, each naming what is missing to resolve it.

    Conservative by design (CAL 01): decision-required messages, unread
    unclassified messages and read decision messages that cannot be proven
    resolved are all returned. A blocker with an empty report and legal
    option ids is ready for :mod:`fm_bot.interactions.choices`.
    """
    provider = text_provider or NoInboxTextProvider()
    blockers: list[InboxBlocker] = []
    for item in items:
        text = provider.get_text(item.message_id, game_time=game_time)
        cls = classify(item, text)
        blocker = _blocker(item, cls, text, pending_actions_supported)
        if blocker is not None:
            blockers.append(blocker)
    return blockers


def continue_blocked_by_inbox(blockers: Iterable[InboxBlocker]) -> MissingCapabilityReport:
    """Fold blockers into one report for the Continue gate."""
    report = MissingCapabilityReport("progress.continue")
    for blocker in blockers:
        for capability in blocker.report.missing:
            report.add(capability, f"{blocker.description}: {blocker.report.reasons[capability]}")
        if not blocker.report.blocked:
            report.add("inbox_decision", f"{blocker.description}: awaiting a choice among {blocker.legal_option_ids}")
    return report
