"""Tests for fm_bot.planning.negotiation (spec 8.3, BOT 011)."""
from __future__ import annotations

import unittest
from fractions import Fraction

from ..planning import finance as fin
from ..planning import negotiation as neg
from ..state.records import Certainty, MovementKind
from ..state.status import Observed
from ..state.units import Money, Period, UnitError
from ..state.views import FinanceView, finance_view
from . import fixtures as fx

GBP = lambda pounds, period=Period.ONCE: Money.native_gbp(pounds, period)  # noqa: E731
OXFORD = neg.Counterparty("Oxford", "club")


def buy_offer(**overrides) -> dict:
    terms = {
        "transfer_fee": {"amount": 800000, "payer": "club", "instalments": [{"amount": 400000, "due_date": "2024-02-20"}, {"amount": 400000, "due_date": "2024-08-20"}]},
        "signing_on_fee": {"amount": 20000, "due_date": "2024-02-20"},
        "agent_fee": {"amount": 15000, "due_date": "2024-02-20"},
        "weekly_wage": {"amount": 4000, "start_date": "2024-02-20", "end_date": "2026-06-30"},
        "promotion_bonus": {"amount": 50000, "due_date": "2024-06-30"},
        "sell_on_percentage": {"percent": 20},
    }
    terms.update(overrides.pop("terms", {}))
    raw = {"offer_id": "off-1", "direction": "buy", "counterparty": OXFORD.to_json(), "currency": "GBP", "date": fx.GAME_DATE, "terms": terms}
    raw.update(overrides)
    return raw


def reservation(**kw) -> neg.ReservationPackage:
    base = dict(max_total_commitment=GBP(900000), max_weekly_wage=GBP(4500, Period.WEEKLY), allowed_clauses={"transfer_fee", "signing_on_fee", "agent_fee", "weekly_wage", "promotion_bonus", "sell_on_percentage"}, intended_playing_time="regular starter", walk_away_condition="guaranteed money above 900k or wage above 4.5k", max_instalments=2, max_instalment_months=12, max_conditional_total=GBP(100000))
    base.update(kw)
    return neg.ReservationPackage(**base)


def live_finance():
    snap = fx.snapshot_for(["/finances", "/squad", "/staff"])
    view = finance_view(snap)
    return view, fin.CommitmentLedger.from_snapshot(snap, view)


class ParseOfferTests(unittest.TestCase):
    def test_complete_buy_offer_becomes_commitments(self):
        parsed = neg.parse_offer(buy_offer())
        self.assertTrue(parsed.complete)
        by_id = {c.commitment_id: c for c in parsed.commitments}
        self.assertEqual(sorted(by_id), ["off-1:agent_fee", "off-1:promotion_bonus", "off-1:signing_on_fee", "off-1:transfer_fee:1", "off-1:transfer_fee:2", "off-1:weekly_wage"])
        fee1 = by_id["off-1:transfer_fee:1"]
        self.assertIs(fee1.kind, MovementKind.PAYMENT)
        self.assertEqual(fee1.amount, GBP(400000))
        self.assertEqual(fee1.due_date, "2024-02-20")
        self.assertIs(fee1.certainty, Certainty.OBSERVED_COMMITTED)
        wage = by_id["off-1:weekly_wage"]
        self.assertEqual(wage.amount, GBP(4000, Period.WEEKLY))
        self.assertIs(wage.recurrence, Period.WEEKLY)
        self.assertEqual(wage.end_date, "2026-06-30")
        bonus = by_id["off-1:promotion_bonus"]
        self.assertIs(bonus.certainty, Certainty.CONDITIONAL)
        self.assertEqual(bonus.trigger, "promotion")
        self.assertEqual(parsed.guaranteed_once_total(), GBP(835000))
        self.assertEqual(parsed.weekly_wage_total(), GBP(4000, Period.WEEKLY))
        self.assertEqual(parsed.conditional_once_total(), GBP(50000))
        self.assertEqual(parsed.unquantified, ["sell_on_percentage"])
        self.assertEqual(parsed.structural[0]["clause"], "sell_on_percentage")
        self.assertTrue(parsed.terms_hash and parsed.raw_hash)

    def test_unrecognized_clause_is_listed_not_guessed(self):
        parsed = neg.parse_offer(buy_offer(terms={"image_rights_share": {"amount": 1000}}))
        self.assertEqual(parsed.unrecognized_clauses, ["image_rights_share"])
        self.assertFalse(parsed.complete)
        self.assertIn("unrecognized clauses", parsed.stop_reasons()[0])
        self.assertEqual(len(parsed.commitments), 6, "recognised clauses are still modelled for planning")

    def test_ambiguous_payer_when_neither_club_nor_counterparty(self):
        parsed = neg.parse_offer(buy_offer(terms={"agent_fee": {"amount": 15000, "payer": "third-party investor"}}))
        self.assertEqual(len(parsed.ambiguous_payer), 1)
        self.assertIn("third-party investor", parsed.ambiguous_payer[0])
        self.assertFalse(parsed.complete)
        self.assertNotIn("off-1:agent_fee", {c.commitment_id for c in parsed.commitments})

    def test_missing_payer_needs_a_direction(self):
        parsed = neg.parse_offer(buy_offer(direction=None))
        self.assertTrue(parsed.ambiguous_payer)
        self.assertFalse(parsed.complete)
        inferred = neg.parse_offer(buy_offer())
        self.assertEqual({c.payer for c in inferred.commitments}, {"club"})

    def test_sell_direction_makes_the_fee_a_receipt(self):
        raw = {"offer_id": "sale-1", "direction": "sell", "counterparty": OXFORD.to_json(), "currency": "GBP", "date": fx.GAME_DATE, "terms": {"transfer_fee": {"amount": 300000, "due_date": "2024-02-20"}}}
        parsed = neg.parse_offer(raw)
        self.assertTrue(parsed.complete)
        fee = parsed.commitments[0]
        self.assertIs(fee.kind, MovementKind.RECEIPT)
        self.assertEqual(fee.payer, "Oxford")
        self.assertEqual(parsed.receipts_once_total(), GBP(300000))
        stated = neg.parse_offer({**raw, "terms": {"transfer_fee": {"amount": 300000, "payer": "oxford"}}})
        self.assertEqual(stated.commitments[0].payer, "Oxford")

    def test_inexact_amounts_and_bad_instalments_are_problems(self):
        parsed = neg.parse_offer(buy_offer(terms={"signing_on_fee": {"amount": 100.5}}))
        self.assertTrue(any("signing_on_fee" in p for p in parsed.problems))
        self.assertFalse(parsed.complete)
        mismatch = neg.parse_offer(buy_offer(terms={"transfer_fee": {"amount": 800000, "instalments": [{"amount": 100, "due_date": "2024-03-01"}]}}))
        self.assertTrue(any("do not add up" in p for p in mismatch.problems))
        no_end = neg.parse_offer(buy_offer(terms={"weekly_wage": {"amount": 4000}}))
        self.assertTrue(any("end_date" in p for p in no_end.problems))
        euro = neg.parse_offer(buy_offer(terms={"agent_fee": {"amount": Money.of(10, "EUR")}}))
        self.assertTrue(any("EUR" in p for p in euro.problems))

    def test_offer_without_date_is_incomplete_and_nothing_is_dated_for_it(self):
        """FIN 02 / spec 8.3: an offer with no in-game date cannot be dated, checked or accepted; no date is invented."""
        raw = buy_offer()
        raw.pop("date")
        raw["terms"] = {"transfer_fee": {"amount": 5_000_000}, "weekly_wage": {"amount": 1000, "start_date": "2024-02-20", "end_date": "2026-06-30"}}
        parsed = neg.parse_offer(raw)
        self.assertFalse(parsed.complete)
        self.assertIn(neg.NO_OFFER_DATE, parsed.problems)
        self.assertTrue(any("transfer_fee" in p and "no due_date" in p for p in parsed.problems), parsed.problems)
        self.assertNotIn("off-1:transfer_fee", {c.commitment_id for c in parsed.commitments}, "an undated fee is not modelled at a made-up date")
        self.assertFalse(any(c.due_date.startswith("1970") for c in parsed.commitments))
        self.assertEqual([c.commitment_id for c in parsed.commitments], ["off-1:weekly_wage"], "dated clauses are still planned")
        self.assertTrue(any("instalment horizon" in v for v in reservation().violations(neg.parse_offer({**raw, "terms": {"transfer_fee": {"amount": 800_000, "instalments": [{"amount": 800_000, "due_date": "2027-01-01"}]}}}))))
        machine = neg.NegotiationStateMachine(OXFORD, reservation(), "buy")
        v1 = machine.record_offer(raw, "counterparty")
        self.assertIs(machine.state, neg.NegotiationState.STOPPED)
        self.assertIn(neg.NO_OFFER_DATE, machine.stop_reason)
        machine.confirm_version(1, v1.hash, by="ui_adapter")
        check = machine.accept_ready(1, FinanceView(*(Observed.unavailable(fin.ValueStatus.MISSING, w) for w in ("balance", "transfer_budget", "wage_budget_weekly", "payroll_spending_weekly")), None), reservation(), None, game_date=fx.GAME_DATE)
        self.assertFalse(check.ready)
        self.assertTrue(any(neg.NO_OFFER_DATE in r for r in check.reasons))
        self.assertIs(machine.state, neg.NegotiationState.STOPPED)

    def test_amount_in_another_period_is_rejected_not_relabelled(self):
        """FIN 01: a monthly (or period-less) figure offered for a weekly clause is a parse problem, never re-stamped weekly."""
        monthly = neg.parse_offer(buy_offer(terms={"weekly_wage": {"amount": GBP(4000, Period.MONTHLY), "start_date": "2024-02-20", "end_date": "2026-06-30"}}))
        self.assertFalse(monthly.complete)
        self.assertTrue(any("weekly_wage" in p and "monthly" in p for p in monthly.problems), monthly.problems)
        self.assertEqual([c for c in monthly.commitments if c.category == "wages"], [])
        no_period = neg.parse_offer(buy_offer(terms={"weekly_wage": {"amount": {"minor": 400000, "currency": "GBP"}, "start_date": "2024-02-20", "end_date": "2026-06-30"}}))
        self.assertTrue(any("no period" in p for p in no_period.problems), no_period.problems)
        weekly_fee = neg.parse_offer(buy_offer(terms={"signing_on_fee": {"amount": GBP(20000, Period.WEEKLY).to_json()}}))
        self.assertTrue(any("signing_on_fee" in p and "weekly" in p for p in weekly_fee.problems), weekly_fee.problems)
        right = neg.parse_offer(buy_offer(terms={"weekly_wage": {"amount": GBP(4000, Period.WEEKLY).to_json(), "start_date": "2024-02-20", "end_date": "2026-06-30"}}))
        self.assertTrue(right.complete, right.problems)
        self.assertEqual(right.weekly_wage_total(), GBP(4000, Period.WEEKLY))

    def test_invalid_counterparty_and_direction(self):
        parsed = neg.parse_offer(buy_offer(counterparty={"name": "", "kind": "sponsor"}))
        self.assertFalse(parsed.counterparty.valid)
        self.assertFalse(parsed.complete)
        self.assertFalse(neg.parse_offer(buy_offer(direction="swap")).complete)
        self.assertFalse(neg.parse_offer({"offer_id": "x", "counterparty": OXFORD.to_json()}).complete)


class ReservationTests(unittest.TestCase):
    def test_offer_inside_package_has_no_violations(self):
        self.assertEqual(reservation().violations(neg.parse_offer(buy_offer())), [])

    def test_each_limit_is_reported(self):
        parsed = neg.parse_offer(buy_offer(terms={"weekly_wage": {"amount": 5000, "start_date": "2024-02-20", "end_date": "2026-06-30"}, "transfer_fee": {"amount": 950000, "instalments": [{"amount": 300000, "due_date": "2024-02-20"}, {"amount": 300000, "due_date": "2024-08-20"}, {"amount": 350000, "due_date": "2025-06-20"}]}, "goal_bonus": {"amount": 500}, "promotion_bonus": {"amount": 150000}}))
        out = reservation().violations(parsed)
        joined = "\n".join(out)
        self.assertIn("exceed the reservation GBP 900,000.00", joined)
        self.assertIn("weekly wage GBP 5,000.00/week exceeds", joined)
        self.assertIn("3 instalments exceed the allowed 2", joined)
        self.assertIn("later than 12 months", joined)
        self.assertIn("conditional commitments GBP 150,500.00 exceed", joined)

    def test_disallowed_and_unquantified_clauses(self):
        strict = reservation(allowed_clauses={"transfer_fee", "weekly_wage"})
        out = strict.violations(neg.parse_offer(buy_offer()))
        self.assertTrue(any("clause 'sell_on_percentage' is not in the allowed set" in v for v in out))
        self.assertTrue(any("'signing_on_fee'" in v for v in out))
        self.assertTrue(any("unquantified clause 'sell_on_percentage'" in v for v in out))

    def test_sale_minimum_receipt(self):
        pack = reservation(min_total_receipt=GBP(500000))
        raw = {"offer_id": "sale-1", "direction": "sell", "counterparty": OXFORD.to_json(), "currency": "GBP", "date": fx.GAME_DATE, "terms": {"transfer_fee": {"amount": 300000}}}
        self.assertTrue(any("below the reservation minimum" in v for v in pack.violations(neg.parse_offer(raw))))

    def test_package_validation(self):
        with self.assertRaises(UnitError):
            reservation(max_total_commitment=GBP(1, Period.WEEKLY))
        with self.assertRaises(ValueError):
            reservation(allowed_clauses={"transfer_fee", "image_rights"})


class CounterProposalTests(unittest.TestCase):
    def test_counter_clamps_to_reservation(self):
        raw = buy_offer(terms={"weekly_wage": {"amount": 6000, "start_date": "2024-02-20", "end_date": "2026-06-30"}, "transfer_fee": {"amount": 1200000, "instalments": [{"amount": 400000, "due_date": "2024-02-20"}, {"amount": 400000, "due_date": "2024-08-20"}, {"amount": 400000, "due_date": "2025-08-20"}]}, "image_rights_share": {"amount": 1}})
        parsed = neg.parse_offer(raw)
        proposal = neg.propose_counter(parsed, reservation(), raw["terms"])
        self.assertEqual(proposal.kind, "counter")
        self.assertEqual(Money.from_json(proposal.terms["weekly_wage"]["amount"]), GBP(4500, Period.WEEKLY))
        self.assertEqual(Money.from_json(proposal.terms["transfer_fee"]["amount"]), GBP(900000 - 35000))
        self.assertEqual([Money.from_json(i["amount"]) for i in proposal.terms["transfer_fee"]["instalments"]], [GBP(432500), GBP(432500)])
        self.assertEqual([i["due_date"] for i in proposal.terms["transfer_fee"]["instalments"]], ["2024-02-17", "2024-08-17"])
        self.assertNotIn("image_rights_share", proposal.terms)
        counter = neg.parse_offer({**raw, "terms": proposal.terms})
        self.assertEqual(reservation().violations(counter), [])
        self.assertTrue(counter.complete)

    def test_no_counter_needed_and_walk_away_rules(self):
        parsed = neg.parse_offer(buy_offer())
        self.assertEqual(neg.propose_counter(parsed, reservation(), buy_offer()["terms"]).kind, "no_counter_needed")
        self.assertTrue(neg.propose_counter(parsed, reservation(), buy_offer()["terms"], rounds_so_far=4).walk_away)
        self.assertTrue(neg.propose_counter(parsed, reservation(), buy_offer()["terms"], previous_counterparty_hashes=[parsed.terms_hash]).walk_away)
        greedy = neg.parse_offer(buy_offer(terms={"signing_on_fee": {"amount": 950000}}))
        self.assertTrue(neg.propose_counter(greedy, reservation(), buy_offer()["terms"]).walk_away)

    def test_sale_counter_raises_asking_price(self):
        raw = {"offer_id": "sale-1", "direction": "sell", "counterparty": OXFORD.to_json(), "currency": "GBP", "date": fx.GAME_DATE, "terms": {"transfer_fee": {"amount": 300000}}}
        pack = reservation(min_total_receipt=GBP(500000))
        proposal = neg.propose_counter(neg.parse_offer(raw), pack, raw["terms"])
        self.assertEqual(proposal.kind, "counter")
        self.assertEqual(proposal.terms["transfer_fee"], {"amount": GBP(500000).to_json(), "payer": "Oxford"})
        self.assertTrue(neg.parse_offer({**raw, "terms": proposal.terms}).complete, "the club's own counter re-parses as complete")

    def test_counter_keeps_pence_exact_and_reparses_complete(self):
        """Spec 5.3: counter amounts are exact minor units; an uneven split still adds up to the clamped total."""
        pack = reservation(max_total_commitment=Money(100_000_001), max_instalments=3, max_instalment_months=12)   # GBP 1,000,000.01
        raw = buy_offer()
        raw["terms"] = {"transfer_fee": {"amount": 2_000_000, "due_date": "2024-02-20"}, "weekly_wage": {"amount": 4000, "start_date": "2024-02-20", "end_date": "2026-06-30"}}
        proposal = neg.propose_counter(neg.parse_offer(raw), pack, raw["terms"])
        self.assertEqual(proposal.kind, "counter")
        parts = [Money.from_json(i["amount"]) for i in proposal.terms["transfer_fee"]["instalments"]]
        self.assertEqual(parts, [Money(33_333_334), Money(33_333_334), Money(33_333_333)])
        self.assertEqual(sum(m.minor for m in parts), 100_000_001, "no penny is truncated")
        self.assertEqual(Money.from_json(proposal.terms["transfer_fee"]["amount"]), Money(100_000_001))
        counter = neg.parse_offer({**raw, "terms": proposal.terms})
        self.assertTrue(counter.complete, counter.problems)
        self.assertEqual(pack.violations(counter), [])
        self.assertEqual(counter.guaranteed_once_total(), Money(100_000_001))

    def test_counter_without_in_game_date_is_stopped_not_anchored_on_the_wall_clock(self):
        """Spec 5.2 / 8.1: instalments are scheduled on in-game dates only; without one the proposal stops."""
        raw = buy_offer(terms={"transfer_fee": {"amount": 1_200_000, "instalments": [{"amount": 600_000, "due_date": "2024-02-20"}, {"amount": 600_000, "due_date": "2024-08-20"}]}})
        raw.pop("date")
        parsed = neg.parse_offer(raw)
        stopped = neg.propose_counter(parsed, reservation(), raw["terms"])
        self.assertEqual(stopped.kind, "stopped")
        self.assertTrue(stopped.stopped)
        self.assertIsNone(stopped.terms)
        self.assertTrue(any("in-game date" in r for r in stopped.reasons))
        anchored = neg.propose_counter(parsed, reservation(), raw["terms"], game_date=fx.GAME_DATE)
        self.assertEqual(anchored.kind, "counter")
        self.assertEqual([i["due_date"] for i in anchored.terms["transfer_fee"]["instalments"]], ["2024-02-17", "2024-08-17"])
        # through the state machine: a stopped proposal records nothing; a game date dates the club's counter
        machine = neg.NegotiationStateMachine(OXFORD, reservation(), "buy")
        machine.record_offer(raw, "counterparty")
        self.assertIs(machine.state, neg.NegotiationState.STOPPED)
        self.assertEqual(machine.counter().kind, "stopped")
        self.assertEqual(len(machine.versions), 1)
        self.assertIs(machine.state, neg.NegotiationState.STOPPED)
        self.assertEqual(machine.counter(game_date=fx.GAME_DATE).kind, "counter")
        self.assertEqual(machine.latest.offer.date, fx.GAME_DATE)
        self.assertTrue(machine.latest.offer.complete)
        self.assertIs(machine.state, neg.NegotiationState.OFFERED)


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.machine = neg.NegotiationStateMachine(OXFORD, reservation(), "buy")

    def test_versions_and_transitions(self):
        v1 = self.machine.record_offer(buy_offer(terms={"weekly_wage": {"amount": 6000, "start_date": "2024-02-20", "end_date": "2026-06-30"}}), "counterparty")
        self.assertIs(self.machine.state, neg.NegotiationState.COUNTERED)
        self.assertEqual(v1.version, 1)
        proposal = self.machine.counter()
        self.assertEqual(proposal.kind, "counter")
        self.assertIs(self.machine.state, neg.NegotiationState.OFFERED)
        self.assertEqual(self.machine.latest.version, 2)
        self.assertEqual(self.machine.latest.author, "club")
        self.assertNotEqual(self.machine.versions[0].hash, self.machine.versions[1].hash)
        self.machine.record_offer(buy_offer(), "counterparty")
        self.assertIs(self.machine.state, neg.NegotiationState.COUNTERED)
        self.assertEqual(self.machine.rounds(), 2)
        self.assertEqual([h["to"] for h in self.machine.history], ["COUNTERED", "OFFERED", "COUNTERED"])

    def test_unrecognized_clause_stops_acceptance_but_planning_continues(self):
        self.machine.record_offer(buy_offer(terms={"image_rights_share": {"amount": 1000}}), "counterparty")
        self.assertIs(self.machine.state, neg.NegotiationState.STOPPED)
        self.assertIn("unrecognized clauses", self.machine.stop_reason)
        self.assertEqual(len(self.machine.planning_commitments()), 6)
        check = self.machine.accept_ready(1, FinanceView(*(Observed.unavailable(fin.ValueStatus.MISSING, w) for w in ("balance", "transfer_budget", "wage_budget_weekly", "payroll_spending_weekly")), None), reservation(), None, game_date=fx.GAME_DATE)
        self.assertFalse(check.ready)
        self.assertTrue(any("STOPPED" in r for r in check.reasons))
        self.machine.record_offer(buy_offer(), "counterparty")
        self.assertIs(self.machine.state, neg.NegotiationState.COUNTERED)
        self.assertIsNone(self.machine.stop_reason)

    def test_repeated_offer_walks_away_and_ends_the_negotiation(self):
        self.machine.record_offer(buy_offer(terms={"weekly_wage": {"amount": 6000, "start_date": "2024-02-20", "end_date": "2026-06-30"}}), "counterparty")
        self.machine.counter()
        self.machine.record_offer(buy_offer(terms={"weekly_wage": {"amount": 6000, "start_date": "2024-02-20", "end_date": "2026-06-30"}}), "counterparty")
        proposal = self.machine.counter()
        self.assertTrue(proposal.walk_away)
        self.assertIs(self.machine.state, neg.NegotiationState.WALKED_AWAY)
        with self.assertRaises(neg.NegotiationError):
            self.machine.record_offer(buy_offer(), "counterparty")

    def test_round_limit_is_counted_from_the_rounds_the_machine_actually_saw(self):
        """Spec 8.3: the walk-away condition is driven through real rounds, not asserted on propose_counter alone.

        Each round the counterparty moves (a different wage, so the repeated-offer
        rule cannot fire) and the club counters. The machine walks away on the
        round that reaches ``max_rounds``, which only happens if ``counter()``
        passes its own :meth:`rounds` count into the counter heuristic.
        """
        pack = reservation()
        seen = []
        for wage in (6000, 5900, 5800, 5700):
            self.machine.record_offer(buy_offer(terms={"weekly_wage": {"amount": wage, "start_date": "2024-02-20", "end_date": "2026-06-30"}}), "counterparty")
            self.assertIs(self.machine.state, neg.NegotiationState.COUNTERED)
            proposal = self.machine.counter()
            seen.append((self.machine.rounds(), proposal.kind))
            if proposal.walk_away:
                break
        self.assertEqual(seen, [(1, "counter"), (2, "counter"), (3, "counter"), (4, "walk_away")])
        self.assertEqual(self.machine.rounds(), pack.max_rounds, "four counterparty versions were seen")
        self.assertEqual(len(self.machine.versions), 7, "three club counters were recorded; the fourth round walked away instead of countering")
        self.assertIs(self.machine.state, neg.NegotiationState.WALKED_AWAY)
        self.assertTrue(any(f"round limit {pack.max_rounds} reached" in h["reason"] for h in self.machine.history), self.machine.history)
        self.assertIn(pack.walk_away_condition, self.machine.history[-1]["reason"])
        with self.assertRaises(neg.NegotiationError):
            self.machine.counter()

    def test_a_shorter_round_limit_walks_away_earlier_through_the_machine(self):
        """Spec 8.3: the limit is the reservation package's, read per negotiation, not a constant."""
        machine = neg.NegotiationStateMachine(OXFORD, reservation(max_rounds=2), "buy")
        machine.record_offer(buy_offer(terms={"weekly_wage": {"amount": 6000, "start_date": "2024-02-20", "end_date": "2026-06-30"}}), "counterparty")
        self.assertEqual(machine.counter().kind, "counter")
        machine.record_offer(buy_offer(terms={"weekly_wage": {"amount": 5900, "start_date": "2024-02-20", "end_date": "2026-06-30"}}), "counterparty")
        self.assertEqual(machine.rounds(), 2)
        proposal = machine.counter()
        self.assertTrue(proposal.walk_away)
        self.assertTrue(any("round limit 2 reached" in r for r in proposal.reasons), proposal.reasons)
        self.assertIs(machine.state, neg.NegotiationState.WALKED_AWAY)
        self.assertEqual(len(machine.versions), 3, "no fourth version: the walk-away records no counter")

    def test_illegal_transitions_raise(self):
        with self.assertRaises(neg.NegotiationError):
            self.machine.mark_accepted("nothing agreed")
        with self.assertRaises(neg.NegotiationError):
            self.machine.counter()
        with self.assertRaises(neg.NegotiationError):
            self.machine.record_offer(buy_offer(), "board")
        with self.assertRaises(neg.NegotiationError):
            self.machine.confirm_version(9, "x", by="operator")

    def test_confirm_version_checks_hash(self):
        v1 = self.machine.record_offer(buy_offer(), "counterparty")
        with self.assertRaises(neg.NegotiationError):
            self.machine.confirm_version(1, "deadbeef", by="ui_adapter")
        self.assertFalse(v1.confirmed)
        self.machine.confirm_version(1, v1.hash, by="ui_adapter")
        self.assertTrue(v1.confirmed)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.view, self.ledger = live_finance()
        self.reservation = reservation()
        self.machine = neg.NegotiationStateMachine(OXFORD, self.reservation, "buy")
        self.raw = buy_offer(terms={"transfer_fee": {"amount": 200000, "due_date": "2024-02-20"}, "weekly_wage": {"amount": 1000, "start_date": "2024-02-20", "end_date": "2026-06-30"}})
        self.no_rules = Observed.available_value({"rules": []}, "operator", what="regulatory_limits")

    def feasibility(self, parsed):
        return fin.check_package(parsed.commitments, self.view, self.ledger, fin.RiskPolicy(GBP(2000000)), engine=fin.CashFlowEngine(transfer_windows=()), regulatory=self.no_rules)

    def test_happy_path_requires_every_condition(self):
        v1 = self.machine.record_offer(self.raw, "counterparty")
        feasibility = self.feasibility(v1.offer)
        self.assertIs(feasibility.feasible, True)
        not_confirmed = self.machine.accept_ready(1, self.view, self.reservation, feasibility, game_date=fx.GAME_DATE)
        self.assertFalse(not_confirmed.ready)
        self.assertTrue(any("not been confirmed" in r for r in not_confirmed.reasons))
        self.machine.confirm_version(1, v1.hash, by="ui_adapter")
        stale = self.machine.accept_ready(1, self.view, self.reservation, feasibility, game_date="2024-02-18")
        self.assertFalse(stale.ready)
        self.assertTrue(any("not fresh" in r for r in stale.reasons))
        changed = neg.ReservationPackage(**{**{k: v for k, v in self.reservation.__dict__.items()}, "version": 2})
        self.assertFalse(self.machine.accept_ready(1, self.view, changed, feasibility, game_date=fx.GAME_DATE).ready)
        unknown = fin.check_package(v1.offer.commitments, self.view, self.ledger, fin.RiskPolicy(GBP(2000000)), engine=fin.CashFlowEngine(transfer_windows=()))
        self.assertIsNone(unknown.feasible)
        self.assertFalse(self.machine.accept_ready(1, self.view, self.reservation, unknown, game_date=fx.GAME_DATE).ready)
        self.assertFalse(self.machine.accept_ready(1, self.view, self.reservation, None, game_date=fx.GAME_DATE).ready)
        self.assertIs(self.machine.state, neg.NegotiationState.COUNTERED)
        ready = self.machine.accept_ready(1, self.view, self.reservation, feasibility, game_date=fx.GAME_DATE)
        self.assertTrue(ready.ready, ready.reasons)
        self.assertEqual(ready.hash, v1.hash)
        self.assertIs(self.machine.state, neg.NegotiationState.AGREED_PENDING_ACCEPT)
        self.machine.mark_accepted("ui readback: contract signed")
        self.assertIs(self.machine.state, neg.NegotiationState.ACCEPTED)

    def test_outside_reservation_or_wrong_version_is_not_ready(self):
        v1 = self.machine.record_offer(buy_offer(), "counterparty")
        self.machine.record_offer(self.raw, "counterparty")
        self.machine.confirm_version(1, v1.hash, by="ui_adapter")
        check = self.machine.accept_ready(1, self.view, self.reservation, self.feasibility(v1.offer), game_date=fx.GAME_DATE)
        self.assertFalse(check.ready)
        self.assertTrue(any("not the latest" in r for r in check.reasons))
        rich = self.machine.record_offer(buy_offer(terms={"weekly_wage": {"amount": 9000, "start_date": "2024-02-20", "end_date": "2026-06-30"}}), "counterparty")
        self.machine.confirm_version(rich.version, rich.hash, by="ui_adapter")
        check = self.machine.accept_ready(rich.version, self.view, self.reservation, self.feasibility(rich.offer), game_date=fx.GAME_DATE)
        self.assertFalse(check.ready)
        self.assertTrue(any("outside reservation" in r for r in check.reasons))

    def test_feasibility_for_a_different_package_is_rejected(self):
        v1 = self.machine.record_offer(self.raw, "counterparty")
        self.machine.confirm_version(1, v1.hash, by="ui_adapter")
        other = self.feasibility(neg.parse_offer(buy_offer()))
        check = self.machine.accept_ready(1, self.view, self.reservation, other, game_date=fx.GAME_DATE)
        self.assertFalse(check.ready)
        self.assertTrue(any("different package" in r for r in check.reasons))

    def test_deal_within_budget_but_outside_cash_reserve_cannot_be_accepted(self):
        """FIN 02 through the negotiation path."""
        v1 = self.machine.record_offer(self.raw, "counterparty")
        self.machine.confirm_version(1, v1.hash, by="ui_adapter")
        tight = fin.check_package(v1.offer.commitments, self.view, self.ledger, fin.RiskPolicy(GBP(10000000)), engine=fin.CashFlowEngine(transfer_windows=()), regulatory=self.no_rules)
        self.assertEqual(tight.binding, "cash_reserve")
        check = self.machine.accept_ready(1, self.view, self.reservation, tight, game_date=fx.GAME_DATE)
        self.assertFalse(check.ready)
        self.assertTrue(any("cash_reserve" in r for r in check.reasons))


class SaleDecisionTests(unittest.TestCase):
    def test_guaranteed_costs_are_not_reduced_by_uncertain_proceeds(self):
        proceeds = [fin.ForecastReceipt("Championship club bid", GBP(400000), "2024-07-01", Fraction(3, 10), "player_sale"), fin.ForecastReceipt("League One bid", GBP(250000), "2024-07-15", Fraction(1, 2), "player_sale")]
        inputs = neg.sale_decision_inputs(proceeds, GBP(300000), {"role_coverage_lost": "first-choice striker", "replacement_lead_time_weeks": 3})
        self.assertEqual(inputs.guaranteed_costs, GBP(300000))
        self.assertEqual(inputs.no_sale_probability, Fraction(1, 5))
        self.assertEqual([s["label"] for s in inputs.scenarios], ["Championship club bid", "League One bid", "no sale"])
        self.assertEqual(inputs.worst_case_net, GBP(-300000))
        self.assertEqual(inputs.best_case_net, GBP(100000))
        self.assertEqual(inputs.expected_proceeds, GBP(245000))
        self.assertEqual(inputs.expected_proceeds_label, "information_only_not_for_feasibility")
        self.assertIn("worst_case", inputs.feasibility_basis)
        self.assertEqual(inputs.squad_damage["role_coverage_lost"], "first-choice striker")
        with self.assertRaises(ValueError):
            neg.sale_decision_inputs([fin.ForecastReceipt("a", GBP(1), "2024-07-01", Fraction(3, 4)), fin.ForecastReceipt("b", GBP(1), "2024-07-01", Fraction(1, 2))], GBP(1), "x")
        with self.assertRaises(UnitError):
            neg.sale_decision_inputs([], GBP(1, Period.WEEKLY), "x")


if __name__ == "__main__":
    unittest.main()
