"""Commitment ledger, cash-flow engine, risk policy and feasibility checks.

Design specification sections 5.3, 8.1, 8.2, 14 (FIN 01, FIN 02), 17.1;
backlog tickets BOT 004 and BOT 011.

This module is the *accounting baseline* for club finances. Everything in it
is explicit bookkeeping: contracts become dated obligations, obligations are
expanded over the calendar with :func:`payment_dates`, and scenarios are
enumerated paths rather than fitted distributions. Nothing here is a trained
model. The one statistical-looking output, the scenario pass rate, is only
as credible as the scenarios the caller supplies and is labelled
``scenario_model_uncalibrated`` by default.

Baseline versus experiment
--------------------------
* Baseline (this module): the commitment ledger, calendar-exact cash
  projection, scenario enumeration, reserve/CVaR summaries and separate
  budget constraints.
* Experiment (not implemented here): fractional differentiation of
  longitudinal balance series (spec 8.4) belongs to ``fm_bot.models`` and is
  admitted only after out-of-sample forecast gains inside training folds.
  See :data:`FRACTIONAL_DIFFERENTIATION_REFERENCE`.

Money rules (spec 5.3): every amount is a :class:`Money` with currency and
period; weekly and monthly amounts are expanded over calendar dates before
they are summed with one-off amounts; there are no floats in money paths.

The transfer-window calendar and the default reserve are *heuristic
defaults*, declared once as versioned module constants so a reviewer can
see and override them.
"""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN
from enum import Enum
from fractions import Fraction
from typing import Any, Iterable

from ..state.identity import new_id
from ..state.records import Certainty, DecisionSnapshot, FinancialCommitment, MovementKind
from ..state.status import MissingCapabilityReport, Observed, ValueStatus
from ..state.units import Money, Period, UnitError, parse_date, payment_dates, sum_money
from ..state.views import FinanceView

FINANCE_POLICY_VERSION = "finance-baseline-0.1"

# Aggregate name reported by the bridge on /finances. Contracts flagged with it
# are already inside that number and must never be added on top of it.
PAYROLL_AGGREGATE = "payroll_spending_weekly"

# Categories whose guaranteed one-off payments are charged against the
# transfer budget. Signing-on and agent fees are charged here by assumption
# (heuristic: FM shows them as transfer-related spend); the allocation is
# reviewable and can be overridden per call.
TRANSFER_BUDGET_CATEGORIES: frozenset[str] = frozenset({"transfer_fee", "loan_fee", "agent_fee", "signing_on_fee"})

# Weekly categories charged against the wage budget headroom.
WAGE_BUDGET_CATEGORIES: frozenset[str] = frozenset({"wages", "loan_wage_contribution"})

# Heuristic transfer-window calendar (month, day) pairs for English leagues.
# Dates inside a window get daily projection steps. Configurable per engine.
TRANSFER_WINDOWS_HEURISTIC: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = (((1, 1), (2, 1)), ((6, 14), (9, 1)))

DAILY_STEP_RADIUS_DAYS = 3          # daily steps this many days either side of a movement
PROJECTION_MONTHS = 12               # rolling horizon
MAX_UNCERTAIN_ITEMS_PER_SCENARIO = 8 # 2**8 sub-paths; beyond this the engine refuses rather than approximating
DEFAULT_EPSILON = Fraction(1, 20)    # P(min cash >= reserve) >= 1 - epsilon
DEFAULT_CVAR_TAIL = Fraction(1, 10)  # worst 10% of scenario weight
CREDIBILITY_UNCALIBRATED = "scenario_model_uncalibrated"

FRACTIONAL_DIFFERENTIATION_REFERENCE = (
    "spec 8.4: z[t] = sum_k w[k]*series[t-k], w[0]=1, w[k] = -w[k-1]*(d-k+1)/k; "
    "candidate feature transform for fm_bot.models, chosen inside training folds; not part of the accounting baseline"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def as_date(value: str | dt.date) -> dt.date:
    return value if isinstance(value, dt.date) else parse_date(value)


def add_months(day: dt.date, months: int) -> dt.date:
    """Calendar-aware month addition that clamps to the last day of shorter months."""
    total = day.month - 1 + months
    year, month = day.year + total // 12, total % 12 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def as_fraction(value: Fraction | int | float | str) -> Fraction:
    """Probabilities are stored exactly; floats are converted through their decimal text."""
    if isinstance(value, bool):
        raise TypeError("a probability cannot be a bool")
    if isinstance(value, float):
        return Fraction(str(value))
    return Fraction(value)


def _weighted_money(amount: Money, weight: Fraction) -> Money:
    return amount.times(weight, rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------------------
# movements
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CashMovement:
    """One dated cash movement from the club's point of view.

    ``signed`` is a one-off amount: positive for receipts, negative for
    payments. ``certainty`` says whether the movement is observed and
    committed, conditional on a trigger, a forecast, or unknown.
    """

    date: dt.date
    signed: Money
    certainty: Certainty
    category: str
    label: str
    commitment_id: str | None = None
    counterparty: str | None = None
    trigger: str | None = None
    probability: Fraction | None = None

    @property
    def kind(self) -> MovementKind:
        return MovementKind.RECEIPT if self.signed.minor > 0 else MovementKind.PAYMENT

    def to_json(self) -> dict[str, Any]:
        return {"date": self.date.isoformat(), "signed": self.signed.to_json(), "certainty": self.certainty.value, "category": self.category, "label": self.label, "commitment_id": self.commitment_id, "counterparty": self.counterparty, "trigger": self.trigger, "probability": None if self.probability is None else str(self.probability)}


def signed_amount(commitment: FinancialCommitment) -> Money:
    """Per-occurrence amount as a one-off, signed from the club's perspective."""
    once = commitment.amount.as_once()
    return once if commitment.kind is MovementKind.RECEIPT else -once


def expand_commitment(commitment: FinancialCommitment, start: dt.date, end: dt.date) -> list[CashMovement]:
    """All dated movements of one commitment inside ``[start, end]``.

    Recurring amounts are expanded with :func:`payment_dates` so a weekly wage
    and a monthly instalment are never summed as rates (FIN 01).
    """
    first = as_date(commitment.due_date)
    last = as_date(commitment.end_date) if commitment.end_date else None
    dates = [d for d in payment_dates(commitment.recurrence, first, end, last=last) if d >= start]
    amount = signed_amount(commitment)
    prob = None if commitment.probability is None else as_fraction(commitment.probability)
    return [CashMovement(d, amount, commitment.certainty, commitment.category, f"{commitment.category}:{commitment.counterparty}", commitment.commitment_id, commitment.counterparty, commitment.trigger, prob) for d in dates]


# ---------------------------------------------------------------------------
# commitment ledger
# ---------------------------------------------------------------------------

@dataclass
class PayrollReconciliation:
    """Observed weekly payroll aggregate against the sum of known contracts.

    ``residual`` = aggregate - contracts. It is labelled ``unexplained`` and
    reported, never absorbed into a contract line. ``basis`` says which
    number a planner should use: the observed aggregate when the bridge
    reports one, otherwise the contract sum with an explicit warning.
    """

    aggregate: Observed
    contract_sum: Money
    residual: Observed
    contracts_counted: int
    basis: str                      # "aggregate" | "contracts_only"
    label: str = "unexplained"

    def weekly(self) -> Money:
        """The weekly payroll a planner should charge. Aggregate first, contracts otherwise."""
        return self.aggregate.value if self.aggregate.available else self.contract_sum

    def to_json(self) -> dict[str, Any]:
        return {"aggregate": self.aggregate.to_json() if not self.aggregate.available else self.aggregate.value.to_json(), "contract_sum": self.contract_sum.to_json(), "residual": self.residual.value.to_json() if self.residual.available else self.residual.to_json(), "contracts_counted": self.contracts_counted, "basis": self.basis, "label": self.label}


class LedgerError(ValueError):
    """A commitment could not be added, superseded or removed as requested."""


@dataclass
class CommitmentLedger:
    """Every dated obligation and expected receipt the club is party to.

    Built from a snapshot: player employment contracts, loan contributions
    and staff wages become weekly commitments flagged as members of the
    bridge's ``payroll_spending_weekly`` aggregate. Negotiated terms are
    added with :meth:`add_commitment` and replaced by version with
    :meth:`supersede`. The ledger is a baseline record, not a forecast.
    """

    club_id: int | None
    as_of: str | None
    currency: str = "GBP"
    snapshot_id: str | None = None
    commitments: dict[str, FinancialCommitment] = field(default_factory=dict)
    superseded: list[dict[str, Any]] = field(default_factory=list)
    aggregates: dict[str, Observed] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    version: str = FINANCE_POLICY_VERSION

    # ----- construction -----
    @classmethod
    def from_snapshot(cls, snapshot: DecisionSnapshot, finance: FinanceView | None = None, *, staff_in_aggregate: bool = True) -> "CommitmentLedger":
        """Read contracts out of ``/squad`` (or ``/club``), ``/staff`` and the ``/finances`` aggregate.

        ``staff_in_aggregate`` records the assumption that FM's weekly payroll
        figure covers staff as well as players; the reconciliation residual
        makes the consequence of that assumption visible either way.
        """
        from ..state.views import finance_view as _finance_view
        view = finance or _finance_view(snapshot)
        ledger = cls(snapshot.club_id, snapshot.game_date, view.currency if view.currency in ("GBP", "EUR", "USD") else "GBP", snapshot.snapshot_id)
        ledger.aggregates[PAYROLL_AGGREGATE] = view.payroll_spending_weekly
        squad = snapshot.routes.get("/squad")
        if squad is None:
            squad = (snapshot.routes.get("/club") or {}).get("squad") or []
        source = f"{snapshot.snapshot_id}:/squad"
        for player in squad:
            for item in cls._player_contract_commitments(player, ledger.club_id, source):
                ledger.commitments[item.commitment_id] = item
        staff = snapshot.routes.get("/staff") or []
        for person in staff:
            item = cls._staff_commitment(person, ledger.club_id, f"{snapshot.snapshot_id}:/staff", staff_in_aggregate)
            if item is not None:
                ledger.commitments[item.commitment_id] = item
        if not squad:
            ledger.notes.append("no squad route in snapshot: player wage commitments absent")
        if "/staff" not in snapshot.routes:
            ledger.notes.append("no /staff route in snapshot: staff wage commitments absent")
        return ledger

    @staticmethod
    def _player_contract_commitments(player: dict[str, Any], club_id: int | None, source: str) -> list[FinancialCommitment]:
        """Wages and loan contributions from one player's observed contracts.

        Only obligations our club actually carries are recorded: our own
        employment contracts, the contribution we pay for a player borrowed
        from elsewhere, and the contribution another club pays us for a
        player we loaned out (a receipt outside the payroll aggregate).
        """
        contracts = player.get("contracts") or []
        name = player.get("name", str(player.get("id")))
        pid = player.get("id")
        employed_here = any(c.get("kind") == "employment" and c.get("club_id") == club_id for c in contracts)
        items: list[FinancialCommitment] = []
        for contract in contracts:
            wage = contract.get("weekly_wage_gbp")
            if wage is None:
                continue
            money = Money.native_gbp(int(wage), Period.WEEKLY)
            kind, category, aggregate = None, None, None
            if contract.get("kind") == "employment" and contract.get("club_id") == club_id:
                kind, category, aggregate = MovementKind.PAYMENT, "wages", PAYROLL_AGGREGATE
            elif contract.get("kind") == "loan" and contract.get("club_id") == club_id and not employed_here:
                kind, category, aggregate = MovementKind.PAYMENT, "loan_wage_contribution", PAYROLL_AGGREGATE
            elif contract.get("kind") == "loan" and employed_here and contract.get("club_id") != club_id:
                kind, category, aggregate = MovementKind.RECEIPT, "loan_wage_contribution", None
            if kind is None:
                continue
            items.append(FinancialCommitment(f"contract:{contract.get('kind')}:{pid}", name, kind, money, contract.get("start_date") or "1970-01-01", Period.WEEKLY, contract.get("end_date"), None, "club" if kind is MovementKind.PAYMENT else contract.get("club_name", "loan club"), Certainty.OBSERVED_COMMITTED, source, 1, category, aggregate))
        return items

    @staticmethod
    def _staff_commitment(person: dict[str, Any], club_id: int | None, source: str, in_aggregate: bool) -> FinancialCommitment | None:
        contract = person.get("employment") or {}
        wage = contract.get("weekly_wage_gbp")
        if wage is None or contract.get("club_id") != club_id:
            return None
        money = Money.native_gbp(int(wage), Period.WEEKLY)
        return FinancialCommitment(f"contract:staff:{person.get('id')}", person.get("name", str(person.get("id"))), MovementKind.PAYMENT, money, contract.get("start_date") or "1970-01-01", Period.WEEKLY, contract.get("end_date"), None, "club", Certainty.OBSERVED_COMMITTED, source, 1, "wages", PAYROLL_AGGREGATE if in_aggregate else None)

    # ----- mutation -----
    def add_commitment(self, commitment: FinancialCommitment) -> FinancialCommitment:
        """Register a negotiated or observed commitment; a repeated id must supersede by version."""
        if commitment.amount.currency != self.currency:
            raise UnitError(f"ledger is in {self.currency}; commitment {commitment.commitment_id} is in {commitment.amount.currency}")
        existing = self.commitments.get(commitment.commitment_id)
        if existing is not None:
            if commitment.version <= existing.version:
                raise LedgerError(f"{commitment.commitment_id} v{commitment.version} does not supersede v{existing.version}")
            self.superseded.append({"commitment_id": existing.commitment_id, "version": existing.version, "replaced_by": commitment.version})
        self.commitments[commitment.commitment_id] = commitment
        return commitment

    def add_all(self, commitments: Iterable[FinancialCommitment]) -> None:
        for item in commitments:
            self.add_commitment(item)

    def supersede(self, commitment_id: str, replacement: FinancialCommitment) -> FinancialCommitment:
        if commitment_id not in self.commitments:
            raise LedgerError(f"{commitment_id} is not in the ledger")
        if replacement.commitment_id != commitment_id:
            raise LedgerError("a replacement keeps the commitment id and raises the version")
        return self.add_commitment(replacement)

    def remove(self, commitment_id: str, reason: str) -> FinancialCommitment:
        existing = self.commitments.pop(commitment_id, None)
        if existing is None:
            raise LedgerError(f"{commitment_id} is not in the ledger")
        self.superseded.append({"commitment_id": commitment_id, "version": existing.version, "removed": reason})
        return existing

    def copy(self) -> "CommitmentLedger":
        clone = CommitmentLedger(self.club_id, self.as_of, self.currency, self.snapshot_id, dict(self.commitments), list(self.superseded), dict(self.aggregates), list(self.notes), self.version)
        return clone

    # ----- queries -----
    def get(self, commitment_id: str) -> FinancialCommitment | None:
        return self.commitments.get(commitment_id)

    def by_category(self, category: str) -> list[FinancialCommitment]:
        return [c for c in self.commitments.values() if c.category == category]

    def active(self, as_of: str | dt.date | None = None) -> list[FinancialCommitment]:
        """Commitments whose payment window has not closed by ``as_of``."""
        day = as_date(as_of or self.as_of or "1970-01-01")
        result = []
        for c in self.commitments.values():
            if c.recurrence is Period.ONCE:
                if as_date(c.due_date) >= day:
                    result.append(c)
            elif c.end_date is None or as_date(c.end_date) >= day:
                result.append(c)
        return result

    def in_aggregate(self, aggregate: str = PAYROLL_AGGREGATE, as_of: str | dt.date | None = None) -> list[FinancialCommitment]:
        return [c for c in self.active(as_of) if c.included_in_aggregate == aggregate and c.recurrence is Period.WEEKLY and c.kind is MovementKind.PAYMENT]

    def weekly_payroll(self, as_of: str | dt.date | None = None) -> PayrollReconciliation:
        """Observed payroll aggregate reconciled against the per-contract sum.

        The aggregate wins when available; the per-contract sum explains part
        of it and the rest is returned as an ``unexplained`` residual.
        """
        members = self.in_aggregate(PAYROLL_AGGREGATE, as_of)
        contract_sum = sum_money((c.amount for c in members), self.currency, Period.WEEKLY)
        aggregate = self.aggregates.get(PAYROLL_AGGREGATE) or Observed.unavailable(ValueStatus.MISSING, PAYROLL_AGGREGATE, "no aggregate recorded")
        if aggregate.available:
            residual = Observed.available_value(aggregate.value - contract_sum, "ledger", what="unexplained_payroll_residual_weekly")
            return PayrollReconciliation(aggregate, contract_sum, residual, len(members), "aggregate")
        residual = Observed.unavailable(aggregate.status, "unexplained_payroll_residual_weekly", f"aggregate {aggregate.status.value}: {aggregate.reason}")
        return PayrollReconciliation(aggregate, contract_sum, residual, len(members), "contracts_only")

    def movements(self, start: str | dt.date, end: str | dt.date) -> list[CashMovement]:
        """Dated movements inside the window, with the payroll aggregate honoured.

        Contract lines inside the aggregate are expanded individually so that
        contract expiries are respected; the ``unexplained`` residual is added
        as its own weekly line so the total equals the observed aggregate at
        ``as_of``. It is never double counted.
        """
        start_day, end_day = as_date(start), as_date(end)
        result: list[CashMovement] = []
        for c in self.commitments.values():
            result.extend(expand_commitment(c, start_day, end_day))
        result.extend(self._residual_movements(start_day, end_day))
        result.sort(key=lambda m: (m.date, m.category, m.label))
        return result

    def _residual_movements(self, start: dt.date, end: dt.date) -> list[CashMovement]:
        recon = self.weekly_payroll(start)
        if recon.basis != "aggregate" or recon.residual.value.is_zero:
            return []
        residual: Money = recon.residual.value
        anchor = start
        weekly = -residual.as_once()
        return [CashMovement(d, weekly, Certainty.OBSERVED_COMMITTED, "payroll_residual", "unexplained payroll residual (aggregate - known contracts)", None, None) for d in payment_dates(Period.WEEKLY, anchor, end)]

    def to_json(self) -> dict[str, Any]:
        return {"club_id": self.club_id, "as_of": self.as_of, "currency": self.currency, "snapshot_id": self.snapshot_id, "commitments": [c.to_json() for c in self.commitments.values()], "superseded": list(self.superseded), "notes": list(self.notes), "version": self.version}


# ---------------------------------------------------------------------------
# negotiated-term helpers (fees, instalments, bonuses)
# ---------------------------------------------------------------------------

def instalment_commitments(counterparty: str, total: Money, instalments: list[tuple[str | dt.date, Money]], *, category: str = "transfer_fee", source: str, payer: str = "club", commitment_prefix: str | None = None, version: int = 1) -> list[FinancialCommitment]:
    """Split a fee into dated one-off obligations. The instalments must add up to ``total`` exactly.

    An instalment reduces the immediate outflow but each later date is a
    real obligation; the ledger carries all of them (spec 8.1).
    """
    if total.period is not Period.ONCE:
        raise UnitError("a fee total is a one-off amount")
    parts = sum_money((amount.as_once() for _, amount in instalments), total.currency, Period.ONCE)
    if parts != total:
        raise LedgerError(f"instalments {parts} do not add up to the agreed total {total}")
    prefix = commitment_prefix or new_id("fee")
    kind = MovementKind.PAYMENT if payer == "club" else MovementKind.RECEIPT
    return [FinancialCommitment(f"{prefix}:{index}", counterparty, kind, amount.as_once(), as_date(day).isoformat(), Period.ONCE, None, None, payer, Certainty.OBSERVED_COMMITTED, source, version, category) for index, (day, amount) in enumerate(instalments, start=1)]


def conditional_commitment(counterparty: str, amount: Money, trigger: str, *, category: str, source: str, due_date: str | dt.date, payer: str = "club", recurrence: Period = Period.ONCE, end_date: str | None = None, probability: Fraction | float | None = None, commitment_id: str | None = None, version: int = 1) -> FinancialCommitment:
    """A bonus or clause that pays only if ``trigger`` happens. The trigger text is kept verbatim."""
    if not trigger:
        raise LedgerError("a conditional commitment needs its trigger text")
    kind = MovementKind.PAYMENT if payer == "club" else MovementKind.RECEIPT
    prob = None if probability is None else float(as_fraction(probability))
    return FinancialCommitment(commitment_id or new_id("cond"), counterparty, kind, amount, as_date(due_date).isoformat(), recurrence, end_date, trigger, payer, Certainty.CONDITIONAL, source, version, category, None, prob)


def guaranteed_total(commitments: Iterable[FinancialCommitment], start: str | dt.date, end: str | dt.date, *, currency: str = "GBP") -> Money:
    """Calendar-exact one-off total of observed-committed club payments in a window."""
    s, e = as_date(start), as_date(end)
    total = Money.zero(currency, Period.ONCE)
    for c in commitments:
        if c.certainty is Certainty.OBSERVED_COMMITTED and c.kind is MovementKind.PAYMENT:
            for m in expand_commitment(c, s, e):
                total = total + (-m.signed)
    return total


# ---------------------------------------------------------------------------
# scenarios and projection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForecastReceipt:
    """A receipt the club hopes for: prize money, cup gate, a player sale.

    ``probability`` below one means the engine enumerates both outcomes; the
    amount is never multiplied by the probability inside a path.
    """

    label: str
    amount: Money
    date: str | dt.date
    probability: Fraction | float | int = 1
    category: str = "forecast_receipt"

    def prob(self) -> Fraction:
        p = as_fraction(self.probability)
        if p < 0 or p > 1:
            raise ValueError(f"probability {p} of {self.label} is outside [0, 1]")
        return p


@dataclass
class Scenario:
    """One named future: which triggers fire, which receipts arrive, promotion flags.

    ``weight`` is the caller's probability weight for the scenario; weights
    are normalised across the set. ``stress`` marks a scenario that the
    policy requires the club to survive regardless of its weight.
    """

    name: str
    weight: Fraction | float | int = 1
    conditional_outcomes: dict[str, bool] = field(default_factory=dict)   # trigger text -> fires?
    receipts: list[ForecastReceipt] = field(default_factory=list)
    flags: dict[str, bool] = field(default_factory=dict)                  # promotion / relegation / stay_up ...
    stress: bool = False
    extra_commitments: list[FinancialCommitment] = field(default_factory=list)

    def normalised_weight(self) -> Fraction:
        w = as_fraction(self.weight)
        if w < 0:
            raise ValueError(f"scenario {self.name} has a negative weight")
        return w


@dataclass
class ScenarioPath:
    """Cash series and summary for one enumerated path."""

    name: str
    scenario: str
    weight: Fraction
    series: list[tuple[dt.date, Money]]
    min_cash: Money
    min_cash_date: dt.date
    end_cash: Money
    movement_counts: dict[str, int]
    stress: bool = False

    def shortfall(self, reserve: Money) -> Money:
        gap = reserve - self.min_cash
        return gap if gap.minor > 0 else Money.zero(reserve.currency, Period.ONCE)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "scenario": self.scenario, "weight": str(self.weight), "min_cash": self.min_cash.to_json(), "min_cash_date": self.min_cash_date.isoformat(), "end_cash": self.end_cash.to_json(), "movement_counts": dict(self.movement_counts), "stress": self.stress, "points": len(self.series)}


@dataclass
class CashFlowProjection:
    start_date: dt.date
    end_date: dt.date
    start_balance: Money
    steps: list[dt.date]
    paths: list[ScenarioPath]
    unknown: list[dict[str, Any]]              # movements/commitments that no path could place
    unexplained_weekly_residual: Money | None
    currency: str
    version: str = FINANCE_POLICY_VERSION

    @property
    def unknown_count(self) -> int:
        return len(self.unknown)

    def path(self, name: str) -> ScenarioPath:
        for p in self.paths:
            if p.name == name:
                return p
        raise KeyError(name)

    def worst_min_cash(self) -> Money:
        return min((p.min_cash for p in self.paths), default=self.start_balance)

    def per_scenario(self) -> dict[str, list[ScenarioPath]]:
        grouped: dict[str, list[ScenarioPath]] = {}
        for p in self.paths:
            grouped.setdefault(p.scenario, []).append(p)
        return grouped

    def to_json(self) -> dict[str, Any]:
        return {"start_date": self.start_date.isoformat(), "end_date": self.end_date.isoformat(), "start_balance": self.start_balance.to_json(), "steps": len(self.steps), "paths": [p.to_json() for p in self.paths], "unknown": list(self.unknown), "unexplained_weekly_residual": self.unexplained_weekly_residual.to_json() if self.unexplained_weekly_residual else None, "currency": self.currency, "version": self.version}


class ProjectionError(ValueError):
    """The engine refused to project rather than approximate."""


@dataclass
class CashFlowEngine:
    """Project cash over a rolling twelve-month calendar (spec 8.1).

    Steps are weekly by default and daily around every movement date and
    inside transfer windows. Each movement is classified observed
    committed, conditional (resolved per scenario), forecast (enumerated by
    probability) or unknown (bucketed, never silently dropped or zeroed).
    """

    transfer_windows: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = TRANSFER_WINDOWS_HEURISTIC
    months: int = PROJECTION_MONTHS
    daily_radius: int = DAILY_STEP_RADIUS_DAYS
    max_uncertain: int = MAX_UNCERTAIN_ITEMS_PER_SCENARIO

    # ----- calendar -----
    def horizon(self, start: dt.date) -> tuple[dt.date, dt.date]:
        return start, add_months(start, self.months)

    def in_transfer_window(self, day: dt.date) -> bool:
        for (m1, d1), (m2, d2) in self.transfer_windows:
            if (m1, d1) <= (day.month, day.day) <= (m2, d2):
                return True
        return False

    def step_dates(self, start: dt.date, end: dt.date, movement_dates: Iterable[dt.date]) -> list[dt.date]:
        days: set[dt.date] = set()
        current = start
        while current <= end:
            days.add(current)
            current += dt.timedelta(days=7)
        days.add(end)
        for m in movement_dates:
            for offset in range(-self.daily_radius, self.daily_radius + 1):
                d = m + dt.timedelta(days=offset)
                if start <= d <= end:
                    days.add(d)
        current = start
        while current <= end:
            if self.in_transfer_window(current):
                days.add(current)
            current += dt.timedelta(days=1)
        return sorted(days)

    # ----- classification -----
    def classify(self, movements: Iterable[CashMovement], scenario: Scenario) -> tuple[list[CashMovement], list[CashMovement], list[dict[str, Any]]]:
        """Split movements into (certain-in-this-scenario, uncertain-to-enumerate, unknown)."""
        certain: list[CashMovement] = []
        uncertain: list[CashMovement] = []
        unknown: list[dict[str, Any]] = []
        for m in movements:
            if m.certainty is Certainty.UNKNOWN:
                unknown.append({"commitment_id": m.commitment_id, "label": m.label, "date": m.date.isoformat(), "reason": "certainty unknown"})
            elif m.certainty is Certainty.CONDITIONAL:
                outcome = scenario.conditional_outcomes.get(m.trigger or "")
                if outcome is None:
                    unknown.append({"commitment_id": m.commitment_id, "label": m.label, "date": m.date.isoformat(), "reason": f"trigger {m.trigger!r} unresolved in scenario {scenario.name}"})
                elif outcome:
                    certain.append(m)
            elif m.certainty is Certainty.FORECAST and m.probability is not None and m.probability < 1:
                if m.probability > 0:
                    uncertain.append(m)
            else:
                certain.append(m)
        return certain, uncertain, unknown

    @staticmethod
    def _receipt_movements(scenario: Scenario) -> list[CashMovement]:
        items = []
        for r in scenario.receipts:
            if r.amount.period is not Period.ONCE:
                raise UnitError(f"forecast receipt {r.label} must be a one-off amount; expand recurring receipts first")
            items.append(CashMovement(as_date(r.date), r.amount, Certainty.FORECAST, r.category, r.label, None, None, None, r.prob()))
        return items

    # ----- paths -----
    def _walk(self, name: str, scenario: Scenario, weight: Fraction, start_balance: Money, movements: list[CashMovement], steps: list[dt.date]) -> ScenarioPath:
        ordered = sorted(movements, key=lambda m: m.date)
        series: list[tuple[dt.date, Money]] = []
        cash = start_balance
        index = 0
        best = (start_balance, steps[0])
        counts: dict[str, int] = {}
        for day in steps:
            while index < len(ordered) and ordered[index].date <= day:
                m = ordered[index]
                cash = cash + m.signed
                counts[m.certainty.value] = counts.get(m.certainty.value, 0) + 1
                index += 1
            series.append((day, cash))
            if cash < best[0]:
                best = (cash, day)
        return ScenarioPath(name, scenario.name, weight, series, best[0], best[1], cash, counts, scenario.stress)

    def _enumerate(self, scenario: Scenario, weight: Fraction, certain: list[CashMovement], uncertain: list[CashMovement], start_balance: Money, steps: list[dt.date]) -> list[ScenarioPath]:
        if len(uncertain) > self.max_uncertain:
            raise ProjectionError(f"scenario {scenario.name} has {len(uncertain)} uncertain receipts; the cap is {self.max_uncertain}. Split the scenario instead of approximating")
        if not uncertain:
            return [self._walk(scenario.name, scenario, weight, start_balance, certain, steps)]
        paths: list[ScenarioPath] = []
        for mask in range(1 << len(uncertain)):
            included = [u for bit, u in enumerate(uncertain) if mask >> bit & 1]
            w = weight
            tags = []
            for bit, u in enumerate(uncertain):
                fires = bool(mask >> bit & 1)
                w = w * (u.probability if fires else 1 - u.probability)  # type: ignore[operator]
                tags.append(f"{u.label}={'yes' if fires else 'no'}")
            if w == 0:
                continue
            paths.append(self._walk(f"{scenario.name}/" + ",".join(tags), scenario, w, start_balance, certain + included, steps))
        return paths

    def project(self, start_balance: Money, start_date: str | dt.date, ledger: CommitmentLedger, scenarios: list[Scenario] | None = None, *, extra_commitments: Iterable[FinancialCommitment] = ()) -> CashFlowProjection:
        """Enumerate every scenario path over the rolling horizon.

        ``start_balance`` is the observed balance (a one-off Money); it is not
        adjusted by owner funding or hoped-for sales (spec 8.1).
        """
        if start_balance.period is not Period.ONCE:
            raise UnitError("start balance is a one-off amount")
        start, end = self.horizon(as_date(start_date))
        scenarios = scenarios or [Scenario("baseline")]
        total_weight = sum((s.normalised_weight() for s in scenarios), Fraction(0))
        if total_weight == 0:
            raise ProjectionError("scenario weights sum to zero")
        base_movements = ledger.movements(start, end)
        for c in extra_commitments:
            base_movements.extend(expand_commitment(c, start, end))
        paths: list[ScenarioPath] = []
        unknown: list[dict[str, Any]] = []
        all_dates: set[dt.date] = {m.date for m in base_movements}
        prepared = []
        for s in scenarios:
            movements = list(base_movements) + self._receipt_movements(s)
            for c in s.extra_commitments:
                movements.extend(expand_commitment(c, start, end))
            certain, uncertain, unk = self.classify(movements, s)
            unknown.extend(unk)
            all_dates.update(m.date for m in certain + uncertain)
            prepared.append((s, certain, uncertain))
        steps = self.step_dates(start, end, all_dates)
        for s, certain, uncertain in prepared:
            for m in certain + uncertain:
                if m.signed.currency != start_balance.currency:
                    raise UnitError(f"movement {m.label} is in {m.signed.currency}; the balance is in {start_balance.currency}")
            paths.extend(self._enumerate(s, s.normalised_weight() / total_weight, certain, uncertain, start_balance, steps))
        recon = ledger.weekly_payroll(start)
        residual = recon.residual.value if recon.residual.available else None
        return CashFlowProjection(start, end, start_balance, steps, paths, unknown, residual, start_balance.currency)


# ---------------------------------------------------------------------------
# reconciliation against observed balances
# ---------------------------------------------------------------------------

@dataclass
class ReconciliationResult:
    """Observed balance change versus the movements the ledger knew about.

    ``residual`` = observed_after - (observed_before + known movements). It
    is labelled ``unexplained``; the caller decides whether to investigate.
    """

    observed_before: Money
    observed_after: Money
    explained: Money
    expected_after: Money
    residual: Money
    movement_count: int
    label: str = "unexplained"

    @property
    def balanced(self) -> bool:
        return self.residual.is_zero

    def to_json(self) -> dict[str, Any]:
        return {"observed_before": self.observed_before.to_json(), "observed_after": self.observed_after.to_json(), "explained": self.explained.to_json(), "expected_after": self.expected_after.to_json(), "residual": self.residual.to_json(), "movement_count": self.movement_count, "label": self.label}


def reconcile(observed_before: Money, observed_after: Money, movements_between: Iterable[CashMovement]) -> ReconciliationResult:
    """Compare two observed balances with the movements the ledger expected between them."""
    if observed_before.period is not Period.ONCE or observed_after.period is not Period.ONCE:
        raise UnitError("balances are one-off amounts")
    items = list(movements_between)
    explained = sum_money((m.signed for m in items), observed_before.currency, Period.ONCE)
    expected = observed_before + explained
    return ReconciliationResult(observed_before, observed_after, explained, expected, observed_after - expected, len(items))


# ---------------------------------------------------------------------------
# risk policy
# ---------------------------------------------------------------------------

@dataclass
class RiskPolicy:
    """Configured reserve, tolerance and tail fraction (spec 8.2). Not football constants."""

    reserve: Money
    epsilon: Fraction | float = DEFAULT_EPSILON
    cvar_tail: Fraction | float = DEFAULT_CVAR_TAIL
    credibility: str = CREDIBILITY_UNCALIBRATED
    version: str = FINANCE_POLICY_VERSION
    label: str = "default"

    def __post_init__(self):
        if self.reserve.period is not Period.ONCE:
            raise UnitError("the cash reserve is a one-off amount")
        self.epsilon = as_fraction(self.epsilon)
        self.cvar_tail = as_fraction(self.cvar_tail)
        if not (0 <= self.epsilon <= 1) or not (0 < self.cvar_tail <= 1):
            raise ValueError("epsilon must be in [0, 1] and the CVaR tail in (0, 1]")

    def to_json(self) -> dict[str, Any]:
        return {"reserve": self.reserve.to_json(), "epsilon": str(self.epsilon), "cvar_tail": str(self.cvar_tail), "credibility": self.credibility, "version": self.version, "label": self.label}


@dataclass
class RiskReport:
    """Reserve test results. ``pass_rate`` is a scenario pass rate under the supplied scenario model.

    It is explicitly NOT an externally guaranteed solvency probability; the
    ``credibility`` label travels with it.
    """

    reserve: Money
    pass_rate: Fraction
    required_rate: Fraction
    passes: bool
    cvar_shortfall: Money
    cvar_tail: Fraction
    stress_survival: dict[str, bool]
    all_stress_survived: bool
    recovery_objective: bool
    worst_path: str | None
    worst_min_cash: Money
    unknown_count: int
    credibility: str
    notes: list[str] = field(default_factory=list)
    version: str = FINANCE_POLICY_VERSION

    def to_json(self) -> dict[str, Any]:
        return {"reserve": self.reserve.to_json(), "pass_rate": str(self.pass_rate), "required_rate": str(self.required_rate), "passes": self.passes, "cvar_shortfall": self.cvar_shortfall.to_json(), "cvar_tail": str(self.cvar_tail), "stress_survival": dict(self.stress_survival), "all_stress_survived": self.all_stress_survived, "recovery_objective": self.recovery_objective, "worst_path": self.worst_path, "worst_min_cash": self.worst_min_cash.to_json(), "unknown_count": self.unknown_count, "credibility": self.credibility, "notes": list(self.notes), "version": self.version}


def cvar_shortfall(paths: list[ScenarioPath], reserve: Money, tail: Fraction) -> Money:
    """Weighted mean shortfall below the reserve in the worst ``tail`` of scenario weight."""
    if not paths:
        return Money.zero(reserve.currency, Period.ONCE)
    ordered = sorted(paths, key=lambda p: p.shortfall(reserve).minor, reverse=True)
    remaining = tail
    total = Money.zero(reserve.currency, Period.ONCE)
    for p in ordered:
        if remaining <= 0:
            break
        take = min(p.weight, remaining)
        total = total + _weighted_money(p.shortfall(reserve), take)
        remaining -= take
    covered = tail - remaining
    if covered == 0:
        return Money.zero(reserve.currency, Period.ONCE)
    return total.times(1 / covered, rounding=ROUND_HALF_EVEN)


def evaluate_risk(projection: CashFlowProjection, policy: RiskPolicy) -> RiskReport:
    """Apply the reserve rule, CVaR summary and stress survival to a projection."""
    reserve = policy.reserve
    passing = sum((p.weight for p in projection.paths if p.min_cash >= reserve), Fraction(0))
    total = sum((p.weight for p in projection.paths), Fraction(0))
    pass_rate = passing / total if total else Fraction(0)
    required = 1 - policy.epsilon
    stress = {p.name: p.min_cash >= reserve for p in projection.paths if p.stress}
    worst = min(projection.paths, key=lambda p: p.min_cash.minor, default=None)
    baseline_paths = [p for p in projection.paths if not p.stress and p.movement_counts.get(Certainty.FORECAST.value, 0) == 0]
    committed_short = any(p.min_cash < reserve for p in baseline_paths)
    recovery = projection.start_balance < reserve or committed_short
    notes = [f"pass rate is a scenario pass rate under {len(projection.paths)} enumerated paths; credibility {policy.credibility}"]
    if projection.unknown_count:
        notes.append(f"{projection.unknown_count} movement(s) could not be placed in any path and are excluded from every figure")
    if recovery:
        notes.append("existing shortfall: recovery objective active (reduce commitments, improve feasible options)")
    return RiskReport(reserve, pass_rate, required, pass_rate >= required and all(stress.values()), cvar_shortfall(projection.paths, reserve, policy.cvar_tail), policy.cvar_tail, stress, all(stress.values()), recovery, worst.name if worst else None, worst.min_cash if worst else projection.start_balance, projection.unknown_count, policy.credibility, notes)


# ---------------------------------------------------------------------------
# constraints and package feasibility
# ---------------------------------------------------------------------------

class ConstraintStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass
class ConstraintResult:
    name: str
    status: ConstraintStatus
    reason: str
    observed: dict[str, Any] = field(default_factory=dict)
    binding: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status.value, "reason": self.reason, "observed": dict(self.observed), "binding": self.binding}


def _once_total_by_category(commitments: Iterable[FinancialCommitment], categories: frozenset[str], start: dt.date, end: dt.date, currency: str) -> Money:
    """Guaranteed one-off club payments in the given categories, expanded over the window."""
    return guaranteed_total((c for c in commitments if c.category in categories), start, end, currency=currency)


def _weekly_total_by_category(commitments: Iterable[FinancialCommitment], categories: frozenset[str], currency: str) -> Money:
    items = [c.amount for c in commitments if c.category in categories and c.recurrence is Period.WEEKLY and c.kind is MovementKind.PAYMENT and c.certainty is Certainty.OBSERVED_COMMITTED]
    return sum_money(items, currency, Period.WEEKLY)


def check_transfer_budget(package: list[FinancialCommitment], finance: FinanceView, start: str | dt.date, end: str | dt.date, *, categories: frozenset[str] = TRANSFER_BUDGET_CATEGORIES) -> ConstraintResult:
    """Guaranteed fees in the window against the observed transfer budget."""
    if not finance.transfer_budget.available:
        return ConstraintResult("transfer_budget", ConstraintStatus.UNKNOWN, f"transfer budget {finance.transfer_budget.status.value}: {finance.transfer_budget.reason}")
    budget: Money = finance.transfer_budget.value
    fees = _once_total_by_category(package, categories, as_date(start), as_date(end), budget.currency)
    observed = {"budget": str(budget), "guaranteed_fees": str(fees)}
    if fees > budget:
        return ConstraintResult("transfer_budget", ConstraintStatus.FAIL, f"guaranteed fees {fees} exceed the transfer budget {budget}", observed)
    return ConstraintResult("transfer_budget", ConstraintStatus.PASS, f"guaranteed fees {fees} within the transfer budget {budget}", observed)


def check_wage_headroom(package: list[FinancialCommitment], finance: FinanceView, *, categories: frozenset[str] = WAGE_BUDGET_CATEGORIES) -> ConstraintResult:
    """New weekly wages against observed weekly headroom (budget minus payroll)."""
    headroom = finance.headroom_weekly()
    if not headroom.available:
        return ConstraintResult("wage_budget_headroom_weekly", ConstraintStatus.UNKNOWN, f"headroom {headroom.status.value}: {headroom.reason}")
    room: Money = headroom.value
    wages = _weekly_total_by_category(package, categories, room.currency)
    observed = {"headroom_weekly": str(room), "new_wages_weekly": str(wages)}
    if wages > room:
        return ConstraintResult("wage_budget_headroom_weekly", ConstraintStatus.FAIL, f"new wages {wages} exceed weekly headroom {room}", observed)
    return ConstraintResult("wage_budget_headroom_weekly", ConstraintStatus.PASS, f"new wages {wages} within weekly headroom {room}", observed)


def check_cash_reserve(risk: RiskReport) -> ConstraintResult:
    """The reserve rule from the risk report, including stress survival."""
    observed = {"reserve": str(risk.reserve), "pass_rate": str(risk.pass_rate), "required": str(risk.required_rate), "worst_min_cash": str(risk.worst_min_cash), "credibility": risk.credibility}
    if risk.passes:
        return ConstraintResult("cash_reserve", ConstraintStatus.PASS, f"minimum cash stays above {risk.reserve} in {risk.pass_rate} of scenario weight (credibility {risk.credibility})", observed)
    failed_stress = [n for n, ok in risk.stress_survival.items() if not ok]
    detail = f"worst path {risk.worst_path} reaches {risk.worst_min_cash} against reserve {risk.reserve}; pass rate {risk.pass_rate} < required {risk.required_rate}"
    if failed_stress:
        detail += f"; stress scenarios not survived: {failed_stress}"
    return ConstraintResult("cash_reserve", ConstraintStatus.FAIL, detail, observed)


# Regulatory rule kinds this baseline can evaluate. Anything else stays unknown.
KNOWN_REGULATORY_RULES: frozenset[str] = frozenset({"max_weekly_wage_bill", "max_guaranteed_fees_in_window"})


def check_regulatory_limits(observed_limits: Observed | None = None, package: list[FinancialCommitment] | None = None, *, finance: FinanceView | None = None, start: str | dt.date | None = None, end: str | dt.date | None = None) -> ConstraintResult:
    """Observed regulatory limits (profitability, squad cost rules). Missing by default.

    The bridge does not decode any regulatory rule, so without an observation
    source this constraint is ``unknown``, not passed. A provider (operator
    or competition_rules adapter) supplies ``Observed[{"rules": [...]}]``;
    an empty rule list means "no limit applies here" and passes. Rules are
    ``{"kind": <KNOWN_REGULATORY_RULES>, "limit": <whole pounds>}``; an
    unknown kind leaves the constraint unknown.
    """
    if observed_limits is None or not observed_limits.available:
        status = observed_limits.status.value if observed_limits else ValueStatus.MISSING.value
        return ConstraintResult("regulatory_limits", ConstraintStatus.UNKNOWN, f"regulatory limits {status}: no observation source", {"capability": "competition_rules"})
    rules = list((observed_limits.value or {}).get("rules") or [])
    if not rules:
        return ConstraintResult("regulatory_limits", ConstraintStatus.PASS, f"observation source {observed_limits.source or 'provider'} reports no regulatory limit applies", {"rules": []})
    package = package or []
    for rule in rules:
        kind = rule.get("kind")
        if kind not in KNOWN_REGULATORY_RULES:
            return ConstraintResult("regulatory_limits", ConstraintStatus.UNKNOWN, f"regulatory rule kind {kind!r} cannot be evaluated by this baseline", {"rules": rules})
        limit = Money.native_gbp(int(rule["limit"]), Period.WEEKLY if kind == "max_weekly_wage_bill" else Period.ONCE)
        if kind == "max_weekly_wage_bill":
            if finance is None or not finance.payroll_spending_weekly.available:
                return ConstraintResult("regulatory_limits", ConstraintStatus.UNKNOWN, "wage bill rule needs an observed payroll aggregate", {"rules": rules})
            bill = finance.payroll_spending_weekly.value + _weekly_total_by_category(package, WAGE_BUDGET_CATEGORIES, limit.currency)
            if bill > limit:
                return ConstraintResult("regulatory_limits", ConstraintStatus.FAIL, f"weekly wage bill {bill} would exceed the observed limit {limit}", {"rules": rules, "wage_bill": str(bill)})
        else:
            if start is None or end is None:
                return ConstraintResult("regulatory_limits", ConstraintStatus.UNKNOWN, "fee rule needs a window", {"rules": rules})
            fees = _once_total_by_category(package, TRANSFER_BUDGET_CATEGORIES, as_date(start), as_date(end), limit.currency)
            if fees > limit:
                return ConstraintResult("regulatory_limits", ConstraintStatus.FAIL, f"guaranteed fees {fees} would exceed the observed limit {limit}", {"rules": rules, "fees": str(fees)})
    return ConstraintResult("regulatory_limits", ConstraintStatus.PASS, "package within every observed regulatory limit", {"rules": rules})


@dataclass
class FeasibilityReport:
    """Every constraint reported separately, with the binding failure named.

    ``feasible`` is True only when every constraint passed; unknown is not a
    pass. ``blocked`` carries a capability report when a mandatory input was
    unavailable.
    """

    constraints: list[ConstraintResult]
    feasible: bool | None
    binding: str | None
    projection: CashFlowProjection | None
    risk: RiskReport | None
    blocked: MissingCapabilityReport | None = None
    package_summary: dict[str, Any] = field(default_factory=dict)
    version: str = FINANCE_POLICY_VERSION

    def by_name(self, name: str) -> ConstraintResult:
        for c in self.constraints:
            if c.name == name:
                return c
        raise KeyError(name)

    @property
    def unknown(self) -> list[str]:
        return [c.name for c in self.constraints if c.status is ConstraintStatus.UNKNOWN]

    def to_json(self) -> dict[str, Any]:
        return {"constraints": [c.to_json() for c in self.constraints], "feasible": self.feasible, "binding": self.binding, "risk": self.risk.to_json() if self.risk else None, "projection": self.projection.to_json() if self.projection else None, "blocked": self.blocked.to_json() if self.blocked else None, "package_summary": dict(self.package_summary), "version": self.version}


def package_summary(package: list[FinancialCommitment], start: dt.date, end: dt.date, currency: str) -> dict[str, Any]:
    guaranteed = guaranteed_total(package, start, end, currency=currency)
    weekly = _weekly_total_by_category(package, WAGE_BUDGET_CATEGORIES, currency)
    conditional = sum_money((c.amount.as_once() for c in package if c.certainty is Certainty.CONDITIONAL and c.kind is MovementKind.PAYMENT and c.recurrence is Period.ONCE), currency, Period.ONCE)
    return {"guaranteed_total_in_window": str(guaranteed), "weekly_wages": str(weekly), "conditional_once_total": str(conditional), "items": len(package)}


def check_package(package: list[FinancialCommitment], finance: FinanceView, ledger: CommitmentLedger, policy: RiskPolicy, scenarios: list[Scenario] | None = None, *, start_date: str | dt.date | None = None, engine: CashFlowEngine | None = None, regulatory: Observed | None = None) -> FeasibilityReport:
    """Test a complete proposed package against every financial constraint separately.

    Cash reserve, transfer budget, wage headroom and regulatory limits are
    independent checks (spec 8.1); a deal inside the transfer budget can
    still fail the cash rule (FIN 02). Nothing assumes owner funding or a
    sale to make the deal fit.
    """
    engine = engine or CashFlowEngine()
    start = as_date(start_date or ledger.as_of or finance.as_of or "1970-01-01")
    _, end = engine.horizon(start)
    constraints: list[ConstraintResult] = []
    projection: CashFlowProjection | None = None
    risk: RiskReport | None = None
    blocked: MissingCapabilityReport | None = None
    if finance.balance.available:
        projection = engine.project(finance.balance.value, start, ledger, scenarios, extra_commitments=package)
        risk = evaluate_risk(projection, policy)
        constraints.append(check_cash_reserve(risk))
    else:
        blocked = MissingCapabilityReport("finance.check_package")
        blocked.add("club_finances", f"balance {finance.balance.status.value}: {finance.balance.reason}")
        constraints.append(ConstraintResult("cash_reserve", ConstraintStatus.UNKNOWN, f"balance {finance.balance.status.value}; no projection possible"))
    constraints.append(check_transfer_budget(package, finance, start, end))
    constraints.append(check_wage_headroom(package, finance))
    constraints.append(check_regulatory_limits(regulatory, package, finance=finance, start=start, end=end))
    binding = next((c.name for c in constraints if c.status is ConstraintStatus.FAIL), None)
    for c in constraints:
        c.binding = c.name == binding
    if binding is not None:
        feasible: bool | None = False
    elif all(c.status is ConstraintStatus.PASS for c in constraints):
        feasible = True
    else:
        feasible = None
    return FeasibilityReport(constraints, feasible, binding, projection, risk, blocked, package_summary(package, start, end, ledger.currency))
