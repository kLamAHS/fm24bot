"""Promise ledger (design specification section 11.3; capability ``promises`` in 3.2 P2).

A promise is something the manager told a player, a member of staff or the
board that the game will hold the club to: a starting role, regular football,
a new contract, permission to leave, a signing in a position. The ledger
keeps the *exact observed terms*, the parties, the deadline, progress and
the source. Promises enter the ledger only from observed terms, meaning a
dialogue choice the bot made (``source_choice``) or message text an
:class:`~fm_bot.interactions.inbox.InboxTextProvider` read. A promise is
never inferred from morale, body language or a hunch; :meth:`PromiseLedger.record`
refuses anything whose confidence is not ``observed``.

Planners consult the ledger before recruitment, renewals, selection, sales
and conversations (:meth:`PromiseLedger.check_before`). Playing-time
promises reserve forecast minutes (:meth:`PromiseLedger.minutes_commitments`)
that the minutes planner must consume rather than plan over.

The :class:`Promise` record has no structured fields beyond its commitment
text, so the ledger journals a :class:`PromiseTerms` entry beside every
promise (append-only ``promise_terms`` journal). When no terms were
supplied, :func:`parse_terms` derives them from the commitment text with a
versioned keyword table, and the result is labelled ``heuristic``.

Baseline versus experiment: the conflict rules and default minutes are
baseline heuristics with reviewable constants. No knock-on (dressing-room)
effect is estimated here; the specification requires repeated evidence
before such effects are modelled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..state.identity import new_id, utc_now
from ..state.records import Promise
from ..state.store import Store

PROMISE_LEDGER_VERSION = "promise-ledger/1.0"
PROMISE_TERM_PATTERNS_VERSION = "promise-term-patterns/1.0"
PROMISE_CONFLICT_RULES_VERSION = "promise-conflict-rules/1.0"

CONFIDENCE_OBSERVED = "observed"

STATUS_OPEN = "open"
STATUS_KEPT = "kept"
STATUS_BROKEN = "broken"
STATUS_EXPIRED = "expired"
STATUS_WITHDRAWN = "withdrawn"        # the counterparty released the club from the promise
CLOSED_STATUSES: frozenset[str] = frozenset({STATUS_KEPT, STATUS_BROKEN, STATUS_EXPIRED, STATUS_WITHDRAWN})

KIND_STARTING_ROLE = "starting_role"      # "you will be a regular starter"
KIND_PLAYING_TIME = "playing_time"        # "you will get more games / N minutes"
KIND_SQUAD_STATUS = "squad_status"        # "you will be a key player / important player"
KIND_ALLOW_TRANSFER = "allow_transfer"    # "we will let you leave for a fair offer"
KIND_NEW_CONTRACT = "new_contract"        # "we will offer you a new deal"
KIND_RECRUIT = "recruit"                  # "we will sign a new <position>"
KIND_OTHER = "other"

# Keyword table for deriving a kind from observed commitment text. HEURISTIC:
# the first kind whose keyword appears wins, in this order. Supplying explicit
# PromiseTerms from the dialogue observation is always preferred.
PROMISE_TERM_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (KIND_STARTING_ROLE, ("start", "first team regular", "regular starter", "first choice", "first-choice")),
    (KIND_PLAYING_TIME, ("playing time", "minutes", "more games", "more football", "game time", "regular football")),
    (KIND_SQUAD_STATUS, ("key player", "important player", "squad status", "first team player", "star player")),
    (KIND_ALLOW_TRANSFER, ("allow you to leave", "let you leave", "listen to offers", "transfer list", "allowed to leave", "can leave")),
    (KIND_NEW_CONTRACT, ("new contract", "new deal", "improved contract", "contract talks", "renew")),
    (KIND_RECRUIT, ("sign a", "sign new", "bring in", "strengthen", "recruit", "new signing")),
)

# Positions recognised in commitment text, longest first so "AMC" wins over "MC".
POSITION_TOKENS: tuple[str, ...] = ("WBL", "WBR", "AML", "AMC", "AMR", "GK", "DL", "DC", "DR", "DM", "ML", "MC", "MR", "ST")
POSITION_WORDS: dict[str, str] = {"goalkeeper": "GK", "striker": "ST", "forward": "ST", "centre-back": "DC", "center-back": "DC", "central defender": "DC", "left-back": "DL", "right-back": "DR", "winger": "AML", "midfielder": "MC", "playmaker": "AMC"}

# Minutes reserved per fixture when the observed terms carry no number.
# HEURISTIC defaults; a promise with observed minutes uses those instead and
# the reservation says which basis applied.
STARTING_ROLE_MINUTES_PER_FIXTURE = 70
PLAYING_TIME_MINUTES_PER_FIXTURE = 30
MATCH_MINUTES = 90

SEVERITY_BLOCKING = "blocking"   # the action would break the promise outright
SEVERITY_WARNING = "warning"     # the action makes keeping the promise harder
SEVERITY_INFO = "info"           # the action concerns a party with an open promise

# Which promise kinds an action kind threatens. Actions not listed only get
# info-level notices for the player named in the context.
OUTGOING_PLAYER_ACTIONS: frozenset[str] = frozenset({"sell_player", "accept_sale", "release_player", "player.release", "loan_out", "loans.out", "transfer_list", "transfers.sell"})
OUTGOING_CONFLICT_KINDS: frozenset[str] = frozenset({KIND_STARTING_ROLE, KIND_PLAYING_TIME, KIND_SQUAD_STATUS, KIND_NEW_CONTRACT})
RECRUITMENT_ACTIONS: frozenset[str] = frozenset({"commit.transfer_offer", "commit.contract", "recruit", "transfers.buy", "loans.in"})
RECRUITMENT_CONFLICT_KINDS: frozenset[str] = frozenset({KIND_STARTING_ROLE, KIND_PLAYING_TIME})
SELECTION_ACTIONS: frozenset[str] = frozenset({"advise.lineup", "submit.lineup"})
RENEWAL_ACTIONS: frozenset[str] = frozenset({"contracts.renew", "renew_contract"})


class PromiseError(ValueError):
    """A promise was not observed, is malformed, or a status change is illegal."""


@dataclass(frozen=True)
class PromiseTerms:
    """Structured reading of the observed commitment."""

    kind: str
    position: str | None = None
    minutes_per_fixture: int | None = None
    fixtures: int | None = None                 # how many matches the promise spans, when stated
    basis: str = "observed"                     # observed | heuristic
    terms_version: str = PROMISE_TERM_PATTERNS_VERSION

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "position": self.position, "minutes_per_fixture": self.minutes_per_fixture, "fixtures": self.fixtures, "basis": self.basis, "terms_version": self.terms_version}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "PromiseTerms":
        return cls(data["kind"], data.get("position"), data.get("minutes_per_fixture"), data.get("fixtures"), data.get("basis", "observed"), data.get("terms_version", PROMISE_TERM_PATTERNS_VERSION))


def _position_in(text: str) -> str | None:
    upper = text.upper()
    for token in POSITION_TOKENS:
        if f" {token} " in f" {upper} " or f" {token}." in f" {upper} ":
            return token
    lowered = text.lower()
    for word, position in POSITION_WORDS.items():
        if word in lowered:
            return position
    return None


def _minutes_in(text: str) -> int | None:
    lowered = text.lower()
    for token in lowered.replace(",", " ").split():
        if token.isdigit() and "minute" in lowered:
            value = int(token)
            if 0 < value <= MATCH_MINUTES:
                return value
    return None


def parse_terms(commitment: str) -> PromiseTerms:
    """Derive structured terms from commitment text (HEURISTIC keyword table)."""
    lowered = commitment.lower()
    kind = KIND_OTHER
    for candidate, keywords in PROMISE_TERM_PATTERNS:
        if any(keyword in lowered for keyword in keywords):
            kind = candidate
            break
    return PromiseTerms(kind, _position_in(commitment), _minutes_in(commitment), None, "heuristic")


def promise_from_observed(party_id: int, party_kind: str, commitment: str, *, source: str, source_choice: str | None = None, deadline: str | None = None, consequences: str | None = None, terms: PromiseTerms | None = None, promise_id: str | None = None) -> tuple[Promise, PromiseTerms]:
    """Build a promise from terms the bot actually saw (a chosen dialogue option or read text)."""
    if not source:
        raise PromiseError("an observed promise needs a source observation or choice id")
    terms = terms or parse_terms(commitment)
    minutes = terms.minutes_per_fixture
    promise = Promise(promise_id or new_id("promise"), party_id, party_kind, commitment, deadline, "not started", consequences, source_choice, CONFIDENCE_OBSERVED, source, playing_time_minutes_per_fixture=minutes, status=STATUS_OPEN)
    return promise, terms


@dataclass
class PromiseConflict:
    promise_id: str
    party_id: int
    action_kind: str
    severity: str
    reason: str
    rules_version: str = PROMISE_CONFLICT_RULES_VERSION

    @property
    def blocking(self) -> bool:
        return self.severity == SEVERITY_BLOCKING

    def to_json(self) -> dict[str, Any]:
        return {"promise_id": self.promise_id, "party_id": self.party_id, "action_kind": self.action_kind, "severity": self.severity, "reason": self.reason, "rules_version": self.rules_version}


@dataclass
class MinutesReservation:
    player_id: int
    promise_id: str
    minutes_per_fixture: int
    fixtures: int
    basis: str                     # observed | heuristic
    total_minutes: int


def _covers(promise_position: str | None, wanted: str | None) -> bool:
    if promise_position is None or wanted is None:
        return promise_position is None and wanted is not None  # a positionless playing-time promise competes with any signing
    return promise_position.upper() == wanted.upper()


class PromiseLedger:
    """Exact observed promises for one branch, persisted through the store."""

    def __init__(self, store: Store, branch_id: str):
        self.store = store
        self.branch_id = branch_id

    # ----- recording -----
    def record(self, promise: Promise, terms: PromiseTerms | None = None) -> Promise:
        """Persist a promise. Refuses anything not backed by observed terms."""
        if promise.confidence != CONFIDENCE_OBSERVED:
            raise PromiseError(f"promise {promise.promise_id} has confidence {promise.confidence!r}; only observed terms may be recorded")
        if not promise.source or not (promise.source_choice or ":" in promise.source):
            raise PromiseError(f"promise {promise.promise_id} needs a source choice id or a source observation reference")
        if not promise.commitment.strip():
            raise PromiseError("a promise needs its exact observed commitment text")
        terms = terms or parse_terms(promise.commitment)
        if promise.playing_time_minutes_per_fixture is None and terms.minutes_per_fixture is not None:
            promise.playing_time_minutes_per_fixture = terms.minutes_per_fixture
        self.store.upsert_promise(promise, self.branch_id)
        self.store.journal("promise_terms", {"promise_id": promise.promise_id, "branch_id": self.branch_id, "terms": terms.to_json(), "ledger_version": PROMISE_LEDGER_VERSION}, promise.promise_id)
        return promise

    def get(self, promise_id: str) -> Promise | None:
        for promise in self.store.list_promises(self.branch_id):
            if promise.promise_id == promise_id:
                return promise
        return None

    def terms(self, promise_id: str) -> PromiseTerms:
        entries = self.store.journal_entries(kind="promise_terms", ref_id=promise_id)
        if entries:
            return PromiseTerms.from_json(entries[-1]["body"]["terms"])
        promise = self.get(promise_id)
        if promise is None:
            raise PromiseError(f"unknown promise {promise_id}")
        return parse_terms(promise.commitment)

    def open(self) -> list[Promise]:
        return self.store.list_promises(self.branch_id, STATUS_OPEN)

    def for_party(self, party_id: int, *, include_closed: bool = False) -> list[Promise]:
        promises = self.store.list_promises(self.branch_id) if include_closed else self.open()
        return [p for p in promises if p.party_id == party_id]

    # ----- checks before actions -----
    def check_before(self, action_kind: str, context: dict[str, Any]) -> list[PromiseConflict]:
        """Promises an action would break, strain or touch. Rules are versioned in PROMISE_CONFLICT_RULES_VERSION."""
        conflicts: list[PromiseConflict] = []
        player_id = context.get("player_id")
        if action_kind in OUTGOING_PLAYER_ACTIONS and player_id is not None:
            conflicts.extend(self._outgoing_conflicts(action_kind, int(player_id)))
        elif action_kind in RECRUITMENT_ACTIONS:
            conflicts.extend(self._recruitment_conflicts(action_kind, context.get("position")))
        elif action_kind in SELECTION_ACTIONS and "starters" in context:
            conflicts.extend(self._selection_conflicts(action_kind, [int(p) for p in context["starters"]]))
        elif player_id is not None:
            for promise in self.for_party(int(player_id)):
                conflicts.append(PromiseConflict(promise.promise_id, promise.party_id, action_kind, SEVERITY_INFO, f"open promise to {promise.party_id}: {promise.commitment!r}"))
        return conflicts

    def _outgoing_conflicts(self, action_kind: str, player_id: int) -> list[PromiseConflict]:
        found = []
        for promise in self.for_party(player_id):
            terms = self.terms(promise.promise_id)
            if terms.kind in OUTGOING_CONFLICT_KINDS:
                found.append(PromiseConflict(promise.promise_id, player_id, action_kind, SEVERITY_BLOCKING, f"{action_kind} breaks the {terms.kind} promise {promise.commitment!r}"))
            elif terms.kind == KIND_ALLOW_TRANSFER:
                found.append(PromiseConflict(promise.promise_id, player_id, action_kind, SEVERITY_INFO, f"{action_kind} fulfils the promise {promise.commitment!r}"))
        return found

    def _recruitment_conflicts(self, action_kind: str, position: str | None) -> list[PromiseConflict]:
        found = []
        for promise in self.open():
            terms = self.terms(promise.promise_id)
            if terms.kind in RECRUITMENT_CONFLICT_KINDS and _covers(terms.position, position):
                where = terms.position or "any position"
                found.append(PromiseConflict(promise.promise_id, promise.party_id, action_kind, SEVERITY_WARNING, f"recruiting a {position or 'player'} competes with the {terms.kind} promise to {promise.party_id} ({where}): {promise.commitment!r}"))
            elif terms.kind == KIND_RECRUIT and (position is None or _covers(terms.position, position) or terms.position is None):
                found.append(PromiseConflict(promise.promise_id, promise.party_id, action_kind, SEVERITY_INFO, f"recruitment progresses the promise {promise.commitment!r}"))
        return found

    def _selection_conflicts(self, action_kind: str, starters: list[int]) -> list[PromiseConflict]:
        found = []
        for promise in self.open():
            terms = self.terms(promise.promise_id)
            if terms.kind == KIND_STARTING_ROLE and promise.party_id not in starters:
                found.append(PromiseConflict(promise.promise_id, promise.party_id, action_kind, SEVERITY_WARNING, f"{promise.party_id} was promised a starting role and is not in the starting eleven"))
        return found

    # ----- minutes -----
    def minutes_reservations(self, fixture_count: int, fixture_dates: Iterable[str] | None = None) -> list[MinutesReservation]:
        """Forecast minutes each open playing-time promise reserves over the next fixtures.

        With ``fixture_dates`` only fixtures on or before the promise deadline
        count. The minutes planner must consume these before allocating.
        """
        dates = list(fixture_dates) if fixture_dates is not None else None
        reservations: list[MinutesReservation] = []
        for promise in self.open():
            if promise.party_kind != "player":
                continue
            terms = self.terms(promise.promise_id)
            per_fixture, basis = self._minutes_basis(promise, terms)
            if per_fixture is None:
                continue
            fixtures = self._fixtures_in_scope(promise, terms, fixture_count, dates)
            reservations.append(MinutesReservation(promise.party_id, promise.promise_id, per_fixture, fixtures, basis, per_fixture * fixtures))
        return reservations

    def minutes_commitments(self, fixture_count: int, fixture_dates: Iterable[str] | None = None) -> dict[int, int]:
        """``{player_id: minutes reserved}`` over the next ``fixture_count`` fixtures (capped at the match total)."""
        reserved: dict[int, int] = {}
        for item in self.minutes_reservations(fixture_count, fixture_dates):
            reserved[item.player_id] = min(reserved.get(item.player_id, 0) + item.total_minutes, MATCH_MINUTES * item.fixtures)
        return reserved

    @staticmethod
    def _minutes_basis(promise: Promise, terms: PromiseTerms) -> tuple[int | None, str]:
        if promise.playing_time_minutes_per_fixture is not None:
            return promise.playing_time_minutes_per_fixture, "observed"
        if terms.kind == KIND_STARTING_ROLE:
            return STARTING_ROLE_MINUTES_PER_FIXTURE, "heuristic"
        if terms.kind == KIND_PLAYING_TIME:
            return PLAYING_TIME_MINUTES_PER_FIXTURE, "heuristic"
        return None, "none"

    @staticmethod
    def _fixtures_in_scope(promise: Promise, terms: PromiseTerms, fixture_count: int, dates: list[str] | None) -> int:
        count = fixture_count
        if dates is not None:
            considered = dates[:fixture_count]
            count = len([d for d in considered if promise.deadline is None or d <= promise.deadline])
        if terms.fixtures is not None:
            count = min(count, terms.fixtures)
        return max(count, 0)

    # ----- progress and closure -----
    def update_progress(self, promise_id: str, evidence: str, progress: str) -> Promise:
        promise = self._require(promise_id)
        promise.progress = progress
        self.store.upsert_promise(promise, self.branch_id)
        self.store.journal("promise_progress", {"promise_id": promise_id, "evidence": evidence, "progress": progress, "at": utc_now()}, promise_id)
        return promise

    def mark_kept(self, promise_id: str, evidence: str) -> Promise:
        return self._close(promise_id, STATUS_KEPT, evidence)

    def mark_broken(self, promise_id: str, evidence: str, consequences: str | None = None) -> Promise:
        """Record a broken promise; consequences are stored only when the game showed them."""
        return self._close(promise_id, STATUS_BROKEN, evidence, consequences)

    def expire(self, promise_id: str, evidence: str) -> Promise:
        return self._close(promise_id, STATUS_EXPIRED, evidence)

    def withdraw(self, promise_id: str, evidence: str) -> Promise:
        return self._close(promise_id, STATUS_WITHDRAWN, evidence)

    def _close(self, promise_id: str, status: str, evidence: str, consequences: str | None = None) -> Promise:
        promise = self._require(promise_id)
        if promise.status != STATUS_OPEN:
            raise PromiseError(f"promise {promise_id} is already {promise.status}")
        if not evidence:
            raise PromiseError("closing a promise needs the observation that showed it")
        promise.status = status
        if consequences is not None:
            promise.consequences = consequences
        self.store.upsert_promise(promise, self.branch_id)
        self.store.journal("promise_status", {"promise_id": promise_id, "status": status, "evidence": evidence, "consequences": consequences, "at": utc_now()}, promise_id)
        return promise

    def _require(self, promise_id: str) -> Promise:
        promise = self.get(promise_id)
        if promise is None:
            raise PromiseError(f"unknown promise {promise_id}")
        return promise
