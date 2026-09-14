"""Negotiation controller: reservation packages, offer parsing and the offer state machine.

Design specification section 8.3; tickets BOT 011; authority rules from
section 1.2 and ``fm_bot.rules.authority``.

Before any negotiation the club stores a :class:`ReservationPackage`: the
most it will commit in guaranteed money, the highest weekly wage, the
payment schedule it can carry, the clauses it is prepared to sign, the
playing time it intends to give, and the condition under which it walks
away. Every changed offer is parsed into :class:`FinancialCommitment`
records (:func:`parse_offer`). A clause the catalog does not know is an
*unrecognized clause*; an obligation whose payer is neither the club nor
the counterparty is an *ambiguous payer*. Either stops acceptance while the
supported part of the offer can still be planned.

A multi-round negotiation is a :class:`NegotiationStateMachine` with hashed
offer versions. Counter-proposals (:func:`propose_counter`) are a labelled
heuristic: they clamp terms to the reservation package; they do not model
the counterparty. Final acceptance (:meth:`NegotiationStateMachine.accept_ready`)
requires exact terms, fresh budgets, a valid counterparty and a confirmed
offer version. This module never executes anything in the game: the UI
adapter is a separate component that acts only on an ``AGREED_PENDING_ACCEPT``
negotiation with an authorised :class:`ActionIntent`.

Baseline versus experiment: everything here is a baseline rule set. There is
no learned acceptance model; counterparty behaviour is not predicted.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN
from enum import Enum
from fractions import Fraction
from typing import Any, Iterable

from ..state.identity import new_id, utc_now
from ..state.records import Certainty, FinancialCommitment, MovementKind, payload_hash
from ..state.units import Money, Period, UnitError, sum_money
from ..state.views import FinanceView
from .finance import FeasibilityReport, ForecastReceipt, as_date, as_fraction, add_months

NEGOTIATION_VERSION = "negotiation-baseline-0.1"
KNOWN_CLAUSES_VERSION = "clauses-0.1"

# Counterparty kinds a negotiation may be held with. Anything else is invalid.
VALID_COUNTERPARTY_KINDS: frozenset[str] = frozenset({"club", "player", "agent"})

# Offer directions and who plays which role in each. A clause's default payer
# is a role; the direction turns it into "club" or the counterparty.
DIRECTION_ROLES: dict[str, dict[str, str]] = {
    "buy": {"buyer": "club", "seller": "counterparty", "employer": "club"},
    "sell": {"buyer": "counterparty", "seller": "club", "employer": "counterparty"},
    "contract": {"employer": "club"},
    "loan_in": {"borrower": "club", "lender": "counterparty", "employer": "counterparty"},
    "loan_out": {"borrower": "counterparty", "lender": "club", "employer": "club"},
}

CLUB_ALIASES: frozenset[str] = frozenset({"club", "us", "we", "our club"})


@dataclass(frozen=True)
class ClauseSpec:
    """Catalog entry for one offer clause.

    ``monetary`` clauses become commitments. Structural clauses (release
    clause, sell-on percentage, contract length) are recorded but have no
    quantifiable cash flow; ``unquantified`` ones may cost money later and
    must be explicitly allowed by the reservation package.
    """

    key: str
    category: str
    recurrence: Period
    certainty: Certainty
    default_payer_role: str | None
    monetary: bool = True
    unquantified: bool = False
    trigger: str | None = None          # fixed trigger text for conditional clauses
    description: str = ""


KNOWN_CLAUSES: dict[str, ClauseSpec] = {spec.key: spec for spec in (
    ClauseSpec("transfer_fee", "transfer_fee", Period.ONCE, Certainty.OBSERVED_COMMITTED, "buyer", description="guaranteed fee, optionally in dated instalments"),
    ClauseSpec("loan_fee", "loan_fee", Period.ONCE, Certainty.OBSERVED_COMMITTED, "borrower", description="guaranteed loan fee"),
    ClauseSpec("signing_on_fee", "signing_on_fee", Period.ONCE, Certainty.OBSERVED_COMMITTED, "employer", description="one-off payment to the player on signing"),
    ClauseSpec("agent_fee", "agent_fee", Period.ONCE, Certainty.OBSERVED_COMMITTED, "employer", description="one-off payment to the agent"),
    ClauseSpec("weekly_wage", "wages", Period.WEEKLY, Certainty.OBSERVED_COMMITTED, "employer", description="weekly wage between start_date and end_date"),
    ClauseSpec("loan_wage_contribution", "loan_wage_contribution", Period.WEEKLY, Certainty.OBSERVED_COMMITTED, "borrower", description="weekly share of the wage paid by the borrowing club"),
    ClauseSpec("appearance_fee", "bonus", Period.ONCE, Certainty.CONDITIONAL, "employer", trigger="each competitive appearance", description="paid per appearance"),
    ClauseSpec("goal_bonus", "bonus", Period.ONCE, Certainty.CONDITIONAL, "employer", trigger="each goal scored", description="paid per goal"),
    ClauseSpec("clean_sheet_bonus", "bonus", Period.ONCE, Certainty.CONDITIONAL, "employer", trigger="each clean sheet", description="paid per clean sheet"),
    ClauseSpec("promotion_bonus", "bonus", Period.ONCE, Certainty.CONDITIONAL, "employer", trigger="promotion", description="paid on promotion"),
    ClauseSpec("unused_substitute_fee", "bonus", Period.ONCE, Certainty.CONDITIONAL, "employer", trigger="each unused substitute appearance", description="paid when named but unused"),
    ClauseSpec("fee_after_appearances", "transfer_fee", Period.ONCE, Certainty.CONDITIONAL, "buyer", trigger="appearance threshold reached", description="additional fee after N appearances"),
    ClauseSpec("sell_on_percentage", "sell_on", Period.ONCE, Certainty.UNKNOWN, "buyer", monetary=False, unquantified=True, description="percentage of a future sale owed to the seller; amount unknown now"),
    ClauseSpec("yearly_wage_rise", "wages", Period.WEEKLY, Certainty.UNKNOWN, "employer", monetary=False, unquantified=True, description="wage increases each contract year; expansion not modelled here"),
    ClauseSpec("release_clause", "release_clause", Period.ONCE, Certainty.UNKNOWN, None, monetary=False, description="a buyer may trigger this amount; not a club cash flow"),
    ClauseSpec("contract_length_years", "contract", Period.ONCE, Certainty.UNKNOWN, None, monetary=False, description="length in years"),
    ClauseSpec("optional_future_fee", "transfer_fee", Period.ONCE, Certainty.CONDITIONAL, "buyer", trigger="option exercised", description="option to buy after a loan"),
)}


# ---------------------------------------------------------------------------
# parsed offers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Counterparty:
    name: str
    kind: str

    @property
    def valid(self) -> bool:
        return bool(self.name) and self.kind in VALID_COUNTERPARTY_KINDS

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind}


@dataclass
class ParsedOffer:
    """The commitment model of one offer version plus everything that could not be modelled."""

    offer_id: str
    direction: str | None
    counterparty: Counterparty
    currency: str
    date: str | None
    commitments: list[FinancialCommitment]
    structural: list[dict[str, Any]] = field(default_factory=list)
    unrecognized_clauses: list[str] = field(default_factory=list)
    ambiguous_payer: list[str] = field(default_factory=list)
    unquantified: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    clause_of: dict[str, str] = field(default_factory=dict)     # commitment_id -> clause key it came from
    raw_hash: str = ""
    terms_hash: str = ""
    catalog_version: str = KNOWN_CLAUSES_VERSION

    @property
    def complete(self) -> bool:
        """Every clause recognised, every payer known, every amount exact."""
        return not (self.unrecognized_clauses or self.ambiguous_payer or self.problems)

    def stop_reasons(self) -> list[str]:
        reasons = []
        if self.unrecognized_clauses:
            reasons.append(f"unrecognized clauses: {self.unrecognized_clauses}")
        if self.ambiguous_payer:
            reasons.append(f"ambiguous payer: {self.ambiguous_payer}")
        if self.problems:
            reasons.append(f"parse problems: {self.problems}")
        return reasons

    def club_payments(self, certainty: Certainty, recurrence: Period) -> list[FinancialCommitment]:
        return [c for c in self.commitments if c.kind is MovementKind.PAYMENT and c.certainty is certainty and c.recurrence is recurrence]

    def guaranteed_once_total(self) -> Money:
        return sum_money((c.amount for c in self.club_payments(Certainty.OBSERVED_COMMITTED, Period.ONCE)), self.currency, Period.ONCE)

    def weekly_wage_total(self) -> Money:
        return sum_money((c.amount for c in self.club_payments(Certainty.OBSERVED_COMMITTED, Period.WEEKLY)), self.currency, Period.WEEKLY)

    def conditional_once_total(self) -> Money:
        return sum_money((c.amount for c in self.club_payments(Certainty.CONDITIONAL, Period.ONCE)), self.currency, Period.ONCE)

    def receipts_once_total(self) -> Money:
        return sum_money((c.amount for c in self.commitments if c.kind is MovementKind.RECEIPT and c.certainty is Certainty.OBSERVED_COMMITTED and c.recurrence is Period.ONCE), self.currency, Period.ONCE)

    def instalment_schedule(self) -> list[FinancialCommitment]:
        return sorted((c for c in self.club_payments(Certainty.OBSERVED_COMMITTED, Period.ONCE) if c.category in ("transfer_fee", "loan_fee")), key=lambda c: c.due_date)

    def clause_keys(self) -> set[str]:
        """Catalog keys present in the offer (monetary and structural)."""
        keys = {self.clause_of.get(c.commitment_id, c.category) for c in self.commitments}
        keys.update(s["clause"] for s in self.structural)
        return keys

    def to_json(self) -> dict[str, Any]:
        return {"offer_id": self.offer_id, "direction": self.direction, "counterparty": self.counterparty.to_json(), "currency": self.currency, "date": self.date, "commitments": [c.to_json() for c in self.commitments], "clause_of": dict(self.clause_of), "structural": list(self.structural), "unrecognized_clauses": list(self.unrecognized_clauses), "ambiguous_payer": list(self.ambiguous_payer), "unquantified": list(self.unquantified), "problems": list(self.problems), "raw_hash": self.raw_hash, "terms_hash": self.terms_hash, "catalog_version": self.catalog_version, "complete": self.complete}


def _money(value: Any, currency: str, period: Period) -> Money:
    if isinstance(value, Money):
        if value.currency != currency:
            raise UnitError(f"amount in {value.currency} inside a {currency} offer")
        return value.with_period(period)
    if isinstance(value, dict):
        return Money.from_json(value).with_period(period)
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnitError(f"amount {value!r} is not an exact whole-currency integer")
    return Money.of(value, currency, period)


def _resolve_payer(stated: Any, spec: ClauseSpec, direction: str | None, counterparty: Counterparty) -> tuple[str | None, str]:
    """Return (payer, how). ``payer`` is "club" or the counterparty name; None when ambiguous."""
    if stated is not None:
        text = str(stated).strip()
        if text.lower() in CLUB_ALIASES:
            return "club", "stated"
        if text.lower() in (counterparty.name.lower(), counterparty.kind.lower()):
            return counterparty.name, "stated"
        return None, f"stated payer {text!r} is neither the club nor the counterparty {counterparty.name!r}"
    if spec.default_payer_role is None:
        return None, "clause has no default payer"
    roles = DIRECTION_ROLES.get(direction or "")
    if not roles or spec.default_payer_role not in roles:
        return None, f"payer not stated and direction {direction!r} does not define the {spec.default_payer_role} role"
    who = roles[spec.default_payer_role]
    return ("club" if who == "club" else counterparty.name), "inferred from direction"


def _clause_commitments(key: str, spec: ClauseSpec, clause: dict[str, Any], payer: str, parsed: ParsedOffer, offer_date: str, source: str) -> list[FinancialCommitment]:
    kind = MovementKind.PAYMENT if payer == "club" else MovementKind.RECEIPT
    counterparty = parsed.counterparty.name
    trigger = clause.get("trigger") or spec.trigger
    if spec.certainty is Certainty.CONDITIONAL and not trigger:
        parsed.problems.append(f"{key}: conditional clause without trigger text")
        return []
    instalments = clause.get("instalments")
    if instalments:
        return _instalments(key, spec, clause, kind, payer, counterparty, parsed, source)
    if "amount" not in clause:
        parsed.problems.append(f"{key}: no amount")
        return []
    try:
        amount = _money(clause["amount"], parsed.currency, spec.recurrence)
    except UnitError as exc:
        parsed.problems.append(f"{key}: {exc}")
        return []
    due = clause.get("due_date") or clause.get("start_date") or offer_date
    end = clause.get("end_date") if spec.recurrence is not Period.ONCE else None
    if spec.recurrence is not Period.ONCE and not end:
        parsed.problems.append(f"{key}: recurring clause without end_date")
        return []
    prob = clause.get("probability")
    return [FinancialCommitment(f"{parsed.offer_id}:{key}", counterparty, kind, amount, due, spec.recurrence, end, trigger, payer, spec.certainty, source, 1, spec.category, None, None if prob is None else float(as_fraction(prob)))]


def _instalments(key: str, spec: ClauseSpec, clause: dict[str, Any], kind: MovementKind, payer: str, counterparty: str, parsed: ParsedOffer, source: str) -> list[FinancialCommitment]:
    items: list[FinancialCommitment] = []
    running = Money.zero(parsed.currency, Period.ONCE)
    for index, part in enumerate(clause["instalments"], start=1):
        try:
            amount = _money(part["amount"], parsed.currency, Period.ONCE)
            due = as_date(part["due_date"]).isoformat()
        except (KeyError, ValueError, UnitError) as exc:
            parsed.problems.append(f"{key} instalment {index}: {exc}")
            return []
        running = running + amount
        items.append(FinancialCommitment(f"{parsed.offer_id}:{key}:{index}", counterparty, kind, amount, due, Period.ONCE, None, clause.get("trigger") or spec.trigger, payer, spec.certainty, source, 1, spec.category))
    if "amount" in clause:
        try:
            if _money(clause["amount"], parsed.currency, Period.ONCE) != running:
                parsed.problems.append(f"{key}: instalments {running} do not add up to the stated total")
                return []
        except UnitError as exc:
            parsed.problems.append(f"{key}: {exc}")
            return []
    return items


def parse_offer(raw: dict[str, Any], *, source: str = "offer") -> ParsedOffer:
    """Turn a raw offer dictionary into commitments plus explicit gaps.

    Expected shape::

        {"offer_id": "...", "direction": "buy|sell|contract|loan_in|loan_out",
         "counterparty": {"name": "Oxford", "kind": "club"}, "currency": "GBP",
         "date": "2024-02-17",
         "terms": {"transfer_fee": {"amount": 800000, "payer": "club",
                                    "instalments": [{"amount": 400000, "due_date": "2024-02-20"}, ...]},
                   "weekly_wage": {"amount": 4000, "start_date": ..., "end_date": ...},
                   "promotion_bonus": {"amount": 50000, "trigger": "promotion"},
                   "sell_on_percentage": {"percent": 20}, ...}}

    Nothing is guessed: an unknown key is unrecognized, a payer that is not
    the club or the counterparty is ambiguous, an inexact amount is a
    problem. All three leave ``complete`` False.
    """
    cp_raw = raw.get("counterparty") or {}
    counterparty = Counterparty(str(cp_raw.get("name") or ""), str(cp_raw.get("kind") or ""))
    currency = raw.get("currency") or "GBP"
    offer_date = raw.get("date")
    parsed = ParsedOffer(str(raw.get("offer_id") or new_id("offer")), raw.get("direction"), counterparty, currency, offer_date, [], raw_hash=payload_hash(raw))
    if not counterparty.valid:
        parsed.problems.append(f"counterparty {counterparty.to_json()} is not a valid club, player or agent")
    if parsed.direction is not None and parsed.direction not in DIRECTION_ROLES:
        parsed.problems.append(f"unknown direction {parsed.direction!r}")
    terms = raw.get("terms")
    if not isinstance(terms, dict):
        parsed.problems.append("offer has no terms dictionary")
        terms = {}
    for key, clause in terms.items():
        spec = KNOWN_CLAUSES.get(key)
        if spec is None:
            parsed.unrecognized_clauses.append(key)
            continue
        clause = clause if isinstance(clause, dict) else {"amount": clause}
        if not spec.monetary:
            parsed.structural.append({"clause": key, **{k: v for k, v in clause.items()}})
            if spec.unquantified:
                parsed.unquantified.append(key)
            continue
        payer, how = _resolve_payer(clause.get("payer"), spec, parsed.direction, counterparty)
        if payer is None:
            parsed.ambiguous_payer.append(f"{key}: {how}")
            continue
        items = _clause_commitments(key, spec, clause, payer, parsed, offer_date or "1970-01-01", source)
        parsed.commitments.extend(items)
        parsed.clause_of.update({c.commitment_id: key for c in items})
    parsed.terms_hash = payload_hash({"terms": terms, "direction": parsed.direction, "counterparty": counterparty.to_json(), "currency": currency})
    return parsed


# ---------------------------------------------------------------------------
# reservation package
# ---------------------------------------------------------------------------

@dataclass
class ReservationPackage:
    """What the club decided before talking: the most it will commit and when it walks away.

    ``max_total_commitment`` bounds guaranteed one-off club payments in the
    offer; ``max_weekly_wage`` bounds the weekly wage; ``max_conditional_total``
    bounds one-off conditional payments. ``allowed_clauses`` is the complete
    set of clause keys the bot may sign; any other clause is a violation even
    if the catalog knows it. ``min_total_receipt`` applies to sales and loans
    out.
    """

    max_total_commitment: Money
    max_weekly_wage: Money
    allowed_clauses: frozenset[str]
    intended_playing_time: str
    walk_away_condition: str
    max_instalments: int | None = None
    max_instalment_months: int | None = None
    max_conditional_total: Money | None = None
    min_total_receipt: Money | None = None
    max_rounds: int = 4
    version: int = 1
    authority_profile_version: int | None = None
    label: str = "default"

    def __post_init__(self):
        if self.max_total_commitment.period is not Period.ONCE:
            raise UnitError("max_total_commitment is a one-off amount")
        if self.max_weekly_wage.period is not Period.WEEKLY:
            raise UnitError("max_weekly_wage is a weekly amount")
        self.allowed_clauses = frozenset(self.allowed_clauses)
        unknown = sorted(self.allowed_clauses - set(KNOWN_CLAUSES))
        if unknown:
            raise ValueError(f"reservation allows clauses the catalog {KNOWN_CLAUSES_VERSION} does not know: {unknown}")

    def violations(self, offer: ParsedOffer) -> list[str]:
        """Every way the offer exceeds the package. Empty means the offer is inside it."""
        out: list[str] = []
        if offer.currency != self.max_total_commitment.currency:
            return [f"offer currency {offer.currency} differs from the reservation currency {self.max_total_commitment.currency}"]
        for key in sorted(offer.clause_keys() - self.allowed_clauses):
            out.append(f"clause {key!r} is not in the allowed set")
        once = offer.guaranteed_once_total()
        if once > self.max_total_commitment:
            out.append(f"guaranteed one-off commitments {once} exceed the reservation {self.max_total_commitment}")
        weekly = offer.weekly_wage_total()
        if weekly > self.max_weekly_wage:
            out.append(f"weekly wage {weekly} exceeds the reservation {self.max_weekly_wage}")
        if self.max_conditional_total is not None:
            conditional = offer.conditional_once_total()
            if conditional > self.max_conditional_total:
                out.append(f"conditional commitments {conditional} exceed the reservation {self.max_conditional_total}")
        schedule = offer.instalment_schedule()
        if self.max_instalments is not None and len(schedule) > self.max_instalments:
            out.append(f"{len(schedule)} instalments exceed the allowed {self.max_instalments}")
        if self.max_instalment_months is not None and schedule and offer.date:
            last = as_date(schedule[-1].due_date)
            if last > add_months(as_date(offer.date), self.max_instalment_months):
                out.append(f"final instalment {last.isoformat()} is later than {self.max_instalment_months} months from the offer date")
        if self.min_total_receipt is not None and offer.direction in ("sell", "loan_out"):
            receipts = offer.receipts_once_total()
            if receipts < self.min_total_receipt:
                out.append(f"guaranteed receipts {receipts} are below the reservation minimum {self.min_total_receipt}")
        for key in offer.unquantified:
            if key not in self.allowed_clauses:
                out.append(f"unquantified clause {key!r} is not allowed")
        return out

    def to_json(self) -> dict[str, Any]:
        return {"max_total_commitment": self.max_total_commitment.to_json(), "max_weekly_wage": self.max_weekly_wage.to_json(), "allowed_clauses": sorted(self.allowed_clauses), "intended_playing_time": self.intended_playing_time, "walk_away_condition": self.walk_away_condition, "max_instalments": self.max_instalments, "max_instalment_months": self.max_instalment_months, "max_conditional_total": self.max_conditional_total.to_json() if self.max_conditional_total else None, "min_total_receipt": self.min_total_receipt.to_json() if self.min_total_receipt else None, "max_rounds": self.max_rounds, "version": self.version, "authority_profile_version": self.authority_profile_version, "label": self.label}


# ---------------------------------------------------------------------------
# counter-proposals (heuristic)
# ---------------------------------------------------------------------------

@dataclass
class CounterProposal:
    """Either a counter inside the reservation package or a walk-away. ``terms`` is a raw offer."""

    kind: str                       # "counter" | "walk_away" | "no_counter_needed"
    terms: dict[str, Any] | None
    reasons: list[str]
    heuristic: str = "clamp-to-reservation-0.1"

    @property
    def walk_away(self) -> bool:
        return self.kind == "walk_away"


def _split_evenly(total: Money, parts: int) -> list[Money]:
    base, remainder = divmod(total.minor, parts)
    return [Money(base + (1 if i < remainder else 0), total.currency, Period.ONCE) for i in range(parts)]


def _reschedule_fee(total: Money, offer_date: dt.date, reservation: ReservationPackage) -> list[dict[str, Any]]:
    """Equal instalments inside the reservation's count and month limits (heuristic)."""
    count = max(1, reservation.max_instalments or 1)
    months = reservation.max_instalment_months or 0
    amounts = _split_evenly(total, count)
    step = months // count if count > 1 and months else 0
    return [{"amount": int(a.major()), "due_date": add_months(offer_date, i * step).isoformat()} for i, a in enumerate(amounts)]


def propose_counter(offer: ParsedOffer, reservation: ReservationPackage, raw_terms: dict[str, Any], *, previous_counterparty_hashes: Iterable[str] = (), rounds_so_far: int = 0) -> CounterProposal:
    """Counter inside the reservation package, or walk away (heuristic, not a model).

    Rules, in order: walk away when the round limit is reached or the
    counterparty repeated an offer unchanged; otherwise drop disallowed
    clauses, clamp the wage, clamp guaranteed fees to what remains of the
    total commitment, and reschedule instalments to the allowed shape. If the
    offer already sits inside the package no counter is needed.
    """
    reasons: list[str] = []
    if rounds_so_far >= reservation.max_rounds:
        return CounterProposal("walk_away", None, [f"round limit {reservation.max_rounds} reached: {reservation.walk_away_condition}"])
    if offer.terms_hash in set(previous_counterparty_hashes):
        return CounterProposal("walk_away", None, ["counterparty repeated an earlier offer unchanged: no movement"])
    violations = reservation.violations(offer)
    if not violations and offer.complete:
        return CounterProposal("no_counter_needed", None, ["offer is inside the reservation package"])
    terms = {k: (dict(v) if isinstance(v, dict) else {"amount": v}) for k, v in raw_terms.items() if k in reservation.allowed_clauses and k in KNOWN_CLAUSES}
    dropped = sorted(set(raw_terms) - set(terms))
    if dropped:
        reasons.append(f"dropped clauses outside the allowed set or catalog: {dropped}")
    currency = reservation.max_total_commitment.currency
    if "weekly_wage" in terms:
        wage = offer.weekly_wage_total()
        if wage > reservation.max_weekly_wage:
            terms["weekly_wage"]["amount"] = int(reservation.max_weekly_wage.major())
            reasons.append(f"weekly wage clamped from {wage} to {reservation.max_weekly_wage}")
    fee_key = "transfer_fee" if "transfer_fee" in terms else ("loan_fee" if "loan_fee" in terms else None)
    other_once = sum_money((c.amount for c in offer.club_payments(Certainty.OBSERVED_COMMITTED, Period.ONCE) if c.category not in ("transfer_fee", "loan_fee")), currency, Period.ONCE)
    if fee_key is not None and offer.direction in ("buy", "loan_in", "contract"):
        room = reservation.max_total_commitment - other_once
        if room.is_negative or room.is_zero:
            return CounterProposal("walk_away", None, [f"signing-on and agent fees alone ({other_once}) use the whole reservation {reservation.max_total_commitment}: {reservation.walk_away_condition}"])
        fee_total = sum_money((c.amount for c in offer.instalment_schedule()), currency, Period.ONCE)
        target = min(fee_total, room)
        if target != fee_total:
            reasons.append(f"guaranteed fee clamped from {fee_total} to {target}")
        offer_day = as_date(offer.date) if offer.date else dt.date.today()
        terms[fee_key] = {"amount": int(target.major()), "payer": "club", "instalments": _reschedule_fee(target, offer_day, reservation)}
        if terms[fee_key]["instalments"] and len(terms[fee_key]["instalments"]) == 1:
            terms[fee_key].pop("instalments")
            terms[fee_key]["due_date"] = offer_day.isoformat()
    if fee_key is not None and offer.direction in ("sell", "loan_out") and reservation.min_total_receipt is not None:
        receipts = offer.receipts_once_total()
        if receipts < reservation.min_total_receipt:
            terms[fee_key] = {"amount": int(reservation.min_total_receipt.major()), "payer": offer.counterparty.name}
            reasons.append(f"asking price raised from {receipts} to the reservation minimum {reservation.min_total_receipt}")
    if not terms:
        return CounterProposal("walk_away", None, ["nothing acceptable remains after removing disallowed clauses"])
    reasons.extend(f"violation addressed: {v}" for v in violations)
    return CounterProposal("counter", terms, reasons)


# ---------------------------------------------------------------------------
# state machine
# ---------------------------------------------------------------------------

class NegotiationState(str, Enum):
    DRAFT = "DRAFT"
    OFFERED = "OFFERED"                      # the club's terms are on the table
    COUNTERED = "COUNTERED"                  # the counterparty's terms are on the table
    AGREED_PENDING_ACCEPT = "AGREED_PENDING_ACCEPT"
    ACCEPTED = "ACCEPTED"
    WALKED_AWAY = "WALKED_AWAY"
    STOPPED = "STOPPED"                      # acceptance stopped; planning continues


NEGOTIATION_TRANSITIONS: dict[NegotiationState, set[NegotiationState]] = {
    NegotiationState.DRAFT: {NegotiationState.OFFERED, NegotiationState.COUNTERED, NegotiationState.STOPPED, NegotiationState.WALKED_AWAY},
    NegotiationState.OFFERED: {NegotiationState.COUNTERED, NegotiationState.OFFERED, NegotiationState.AGREED_PENDING_ACCEPT, NegotiationState.STOPPED, NegotiationState.WALKED_AWAY},
    NegotiationState.COUNTERED: {NegotiationState.OFFERED, NegotiationState.COUNTERED, NegotiationState.AGREED_PENDING_ACCEPT, NegotiationState.STOPPED, NegotiationState.WALKED_AWAY},
    NegotiationState.AGREED_PENDING_ACCEPT: {NegotiationState.ACCEPTED, NegotiationState.COUNTERED, NegotiationState.STOPPED, NegotiationState.WALKED_AWAY},
    NegotiationState.ACCEPTED: set(),
    NegotiationState.WALKED_AWAY: set(),
    NegotiationState.STOPPED: {NegotiationState.OFFERED, NegotiationState.COUNTERED, NegotiationState.WALKED_AWAY},
}


class NegotiationError(ValueError):
    """An illegal transition or an unknown offer version."""


@dataclass
class OfferVersion:
    version: int
    author: str                     # "club" | "counterparty"
    offer: ParsedOffer
    raw: dict[str, Any]
    hash: str
    created_at: str = field(default_factory=utc_now)
    confirmed: bool = False         # an independent readback matched ``hash``
    confirmed_by: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"version": self.version, "author": self.author, "hash": self.hash, "created_at": self.created_at, "confirmed": self.confirmed, "confirmed_by": self.confirmed_by, "offer": self.offer.to_json()}


@dataclass
class AcceptanceCheck:
    ready: bool
    reasons: list[str]
    version: int | None
    hash: str | None
    checked_at: str = field(default_factory=utc_now)

    def to_json(self) -> dict[str, Any]:
        return {"ready": self.ready, "reasons": list(self.reasons), "version": self.version, "hash": self.hash, "checked_at": self.checked_at}


@dataclass
class NegotiationStateMachine:
    """Visible offer versions and the rules for moving between negotiation states.

    The machine never talks to the game. ``record_offer`` parses each new
    version; incomplete versions move the machine to ``STOPPED`` with a
    reason but keep the recognised commitments available through
    :meth:`planning_commitments`. A later complete version resumes.
    """

    counterparty: Counterparty
    reservation: ReservationPackage
    direction: str
    negotiation_id: str = field(default_factory=lambda: new_id("neg"))
    state: NegotiationState = NegotiationState.DRAFT
    versions: list[OfferVersion] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str | None = None
    version: str = NEGOTIATION_VERSION

    # ----- transitions -----
    def _transition(self, new_state: NegotiationState, reason: str) -> None:
        if new_state not in NEGOTIATION_TRANSITIONS[self.state]:
            raise NegotiationError(f"{self.state.value} -> {new_state.value} is not a legal negotiation transition ({reason})")
        self.history.append({"from": self.state.value, "to": new_state.value, "reason": reason, "at": utc_now()})
        self.state = new_state

    @property
    def latest(self) -> OfferVersion | None:
        return self.versions[-1] if self.versions else None

    def get_version(self, number: int) -> OfferVersion:
        for v in self.versions:
            if v.version == number:
                return v
        raise NegotiationError(f"offer version {number} does not exist")

    def counterparty_hashes(self) -> list[str]:
        return [v.offer.terms_hash for v in self.versions if v.author == "counterparty"]

    def rounds(self) -> int:
        return sum(1 for v in self.versions if v.author == "counterparty")

    def record_offer(self, raw: dict[str, Any], author: str) -> OfferVersion:
        """Parse and store a new offer version from either side."""
        if author not in ("club", "counterparty"):
            raise NegotiationError("author must be 'club' or 'counterparty'")
        if self.state in (NegotiationState.ACCEPTED, NegotiationState.WALKED_AWAY):
            raise NegotiationError(f"negotiation is {self.state.value}; no further offers")
        raw = dict(raw)
        raw.setdefault("direction", self.direction)
        raw.setdefault("counterparty", self.counterparty.to_json())
        raw.setdefault("offer_id", f"{self.negotiation_id}:v{len(self.versions) + 1}")
        parsed = parse_offer(raw, source=f"{self.negotiation_id}:v{len(self.versions) + 1}")
        item = OfferVersion(len(self.versions) + 1, author, parsed, raw, payload_hash(raw))
        self.versions.append(item)
        if parsed.counterparty != self.counterparty:
            parsed.problems.append(f"offer counterparty {parsed.counterparty.to_json()} differs from the negotiation counterparty {self.counterparty.to_json()}")
        if not parsed.complete:
            self.stop_reason = "; ".join(parsed.stop_reasons())
            self._transition(NegotiationState.STOPPED, self.stop_reason)
            return item
        self.stop_reason = None
        self._transition(NegotiationState.OFFERED if author == "club" else NegotiationState.COUNTERED, f"v{item.version} by {author}")
        return item

    def confirm_version(self, number: int, hash_seen: str, *, by: str) -> OfferVersion:
        """An independent readback (UI adapter or operator) confirms the version's hash."""
        item = self.get_version(number)
        if hash_seen != item.hash:
            raise NegotiationError(f"readback hash for v{number} does not match the recorded offer; terms may have changed")
        item.confirmed, item.confirmed_by = True, by
        return item

    def counter(self) -> CounterProposal:
        """Propose a counter to the latest counterparty version, recording it as a club version."""
        latest = self.latest
        if latest is None or latest.author != "counterparty":
            raise NegotiationError("no counterparty offer to counter")
        proposal = propose_counter(latest.offer, self.reservation, latest.raw.get("terms") or {}, previous_counterparty_hashes=self.counterparty_hashes()[:-1], rounds_so_far=self.rounds())
        if proposal.walk_away:
            self.walk_away("; ".join(proposal.reasons))
        elif proposal.kind == "counter":
            self.record_offer({"terms": proposal.terms, "currency": latest.offer.currency, "date": latest.offer.date}, "club")
        return proposal

    def walk_away(self, reason: str) -> None:
        self._transition(NegotiationState.WALKED_AWAY, reason)

    def stop(self, reason: str) -> None:
        self.stop_reason = reason
        self._transition(NegotiationState.STOPPED, reason)

    def planning_commitments(self) -> list[FinancialCommitment]:
        """Recognised commitments of the latest version, usable for planning even when STOPPED."""
        return list(self.latest.offer.commitments) if self.latest else []

    # ----- acceptance -----
    def accept_ready(self, version: int, fresh_finance_view: FinanceView, reservation: ReservationPackage, feasibility: FeasibilityReport | None, *, game_date: str | None) -> AcceptanceCheck:
        """Every condition for final acceptance, each reported by name.

        Exact terms (complete parse, no unquantified clause outside the
        package), fresh budgets (the finance view dated to the current game
        date), a valid counterparty, a confirmed latest offer version, the
        stored reservation and a feasibility report that passed. On success
        the machine moves to ``AGREED_PENDING_ACCEPT``; the UI adapter still
        has to execute and verify.
        """
        reasons: list[str] = []
        try:
            item = self.get_version(version)
        except NegotiationError as exc:
            return AcceptanceCheck(False, [str(exc)], None, None)
        if self.state in (NegotiationState.STOPPED, NegotiationState.WALKED_AWAY, NegotiationState.ACCEPTED, NegotiationState.DRAFT):
            reasons.append(f"negotiation state {self.state.value} does not allow acceptance" + (f": {self.stop_reason}" if self.stop_reason else ""))
        if self.latest is not item:
            reasons.append(f"v{version} is not the latest version (latest is v{self.latest.version})")
        if not item.confirmed:
            reasons.append(f"v{version} has not been confirmed by an independent readback")
        offer = item.offer
        if not offer.complete:
            reasons.extend(offer.stop_reasons())
        if not offer.counterparty.valid or offer.counterparty != self.counterparty:
            reasons.append(f"counterparty {offer.counterparty.to_json()} is not the valid negotiation counterparty")
        if reservation.version != self.reservation.version or reservation.to_json() != self.reservation.to_json():
            reasons.append("reservation package differs from the one stored for this negotiation")
        reasons.extend(f"outside reservation: {v}" for v in reservation.violations(offer))
        if not fresh_finance_view.available:
            reasons.append("finance view incomplete: balance, budgets or payroll unavailable")
        elif game_date is None or fresh_finance_view.as_of != game_date:
            reasons.append(f"finance view dated {fresh_finance_view.as_of} is not fresh for game date {game_date}")
        if feasibility is None:
            reasons.append("no feasibility report")
        elif feasibility.feasible is not True:
            reasons.append(f"feasibility is {feasibility.feasible} (binding {feasibility.binding}, unknown {feasibility.unknown})")
        elif feasibility.package_summary.get("items") != len(offer.commitments):
            reasons.append("feasibility report was built for a different package")
        if reasons:
            return AcceptanceCheck(False, reasons, version, item.hash)
        self._transition(NegotiationState.AGREED_PENDING_ACCEPT, f"v{version} ready to accept")
        return AcceptanceCheck(True, [f"v{version} {item.hash[:12]} exact, fresh, valid, confirmed and feasible"], version, item.hash)

    def mark_accepted(self, evidence: str) -> None:
        """Only after the UI adapter verified the acceptance in the game."""
        self._transition(NegotiationState.ACCEPTED, f"accepted in game: {evidence}")

    def to_json(self) -> dict[str, Any]:
        return {"negotiation_id": self.negotiation_id, "counterparty": self.counterparty.to_json(), "direction": self.direction, "state": self.state.value, "reservation": self.reservation.to_json(), "versions": [v.to_json() for v in self.versions], "history": list(self.history), "stop_reason": self.stop_reason, "version": self.version}


# ---------------------------------------------------------------------------
# sale evaluation inputs
# ---------------------------------------------------------------------------

@dataclass
class SaleDecisionInputs:
    """Inputs for a sale decision: guaranteed costs stay guaranteed; proceeds stay scenarios.

    ``feasibility_basis`` explains that the worst-case scenario, not the
    face value or expected value of the proceeds, is what a feasibility
    check may rely on. ``expected_proceeds`` is provided for information and
    labelled as unusable for feasibility.
    """

    guaranteed_costs: Money
    scenarios: list[dict[str, Any]]
    worst_case_net: Money
    best_case_net: Money
    expected_proceeds: Money
    expected_proceeds_label: str
    squad_damage: dict[str, Any]
    feasibility_basis: str
    no_sale_probability: Fraction

    def to_json(self) -> dict[str, Any]:
        return {"guaranteed_costs": self.guaranteed_costs.to_json(), "scenarios": list(self.scenarios), "worst_case_net": self.worst_case_net.to_json(), "best_case_net": self.best_case_net.to_json(), "expected_proceeds": self.expected_proceeds.to_json(), "expected_proceeds_label": self.expected_proceeds_label, "squad_damage": dict(self.squad_damage), "feasibility_basis": self.feasibility_basis, "no_sale_probability": str(self.no_sale_probability)}


def sale_decision_inputs(proceeds_scenarios: list[ForecastReceipt], replacement_cost: Money, squad_damage: dict[str, Any] | str, *, extra_guaranteed_costs: Iterable[Money] = ()) -> SaleDecisionInputs:
    """Combine sale proceeds scenarios with replacement cost and sporting damage (spec 8.3).

    Proceeds never reduce guaranteed costs at face value: each scenario nets
    its own proceeds, a ``no sale`` scenario fills any probability mass the
    caller left unassigned, and the worst case is reported as the number a
    feasibility check may rely on.
    """
    if replacement_cost.period is not Period.ONCE:
        raise UnitError("replacement cost is a one-off amount")
    currency = replacement_cost.currency
    guaranteed = sum_money([replacement_cost, *extra_guaranteed_costs], currency, Period.ONCE)
    assigned = sum((r.prob() for r in proceeds_scenarios), Fraction(0))
    if assigned > 1:
        raise ValueError(f"proceeds scenario probabilities sum to {assigned} > 1")
    scenarios = [{"label": r.label, "proceeds": r.amount.as_once(), "probability": r.prob(), "date": as_date(r.date).isoformat()} for r in proceeds_scenarios]
    no_sale = 1 - assigned
    if no_sale > 0:
        scenarios.append({"label": "no sale", "proceeds": Money.zero(currency, Period.ONCE), "probability": no_sale, "date": None})
    for s in scenarios:
        s["net_after_guaranteed_costs"] = s["proceeds"] - guaranteed
    nets = [s["net_after_guaranteed_costs"] for s in scenarios if s["probability"] > 0]
    expected = Money.zero(currency, Period.ONCE)
    for s in scenarios:
        expected = expected + s["proceeds"].times(s["probability"], rounding=ROUND_HALF_EVEN)
    damage = squad_damage if isinstance(squad_damage, dict) else {"summary": squad_damage}
    for s in scenarios:
        s["proceeds"] = s["proceeds"].to_json()
        s["net_after_guaranteed_costs"] = s["net_after_guaranteed_costs"].to_json()
        s["probability"] = str(s["probability"])
    return SaleDecisionInputs(guaranteed, scenarios, min(nets), max(nets), expected, "information_only_not_for_feasibility", damage, "worst_case_scenario: guaranteed costs are never offset by uncertain proceeds at face value", no_sale)
