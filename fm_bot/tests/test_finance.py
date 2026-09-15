"""Tests for fm_bot.planning.finance (BOT 004, BOT 011, FIN 01, FIN 02)."""
from __future__ import annotations

import datetime as dt
import unittest
from fractions import Fraction

from ..bridge_client.client import BridgeClient
from ..planning import finance as fin
from ..state.identity import CareerRegistry, SaveManifest
from ..state.records import Certainty, ConsistencyStatus, DecisionSnapshot, FinancialCommitment, MovementKind
from ..state.snapshot import CollectionContext, SnapshotCollector, SnapshotRequirements
from ..state.status import Observed, ValueStatus
from ..state.store import Store
from ..state.units import Money, Period, UnitError, total_over
from ..state.views import finance_view
from . import fixtures as fx

GBP = lambda pounds, period=Period.ONCE: Money.native_gbp(pounds, period)  # noqa: E731
START = dt.date(2024, 2, 17)
PLAYER_WAGES = sum(s[4] for s in fx.SQUAD_SPEC)     # 74,250 = the bridge's payroll aggregate in the fixture
STAFF_WAGES = 1500 + 900


def live_snapshot(routes=("/finances", "/squad", "/staff")) -> DecisionSnapshot:
    store = Store.memory()
    career, branch, _ = CareerRegistry(store).register_career("t", SaveManifest(fx.BUILD, 90001, 742, fx.GAME_DATE, fx.GAME_TIME))
    client = BridgeClient(fx.transport(), store, context={"career_id": career.career_id, "branch_id": branch.branch_id})
    snap = SnapshotCollector(client, store).collect(SnapshotRequirements(routes=list(routes)), CollectionContext(career.career_id, branch.branch_id, lineage_confirmed=True))
    assert snap.valid, snap.consistency_reasons
    return snap


def hand_snapshot(routes: dict) -> DecisionSnapshot:
    return DecisionSnapshot("snap-hand", [], ConsistencyStatus.CONSISTENT, [], [], [], {}, "bridge_observed", "c", "b", fx.SESSION, fx.GAME_DATE, fx.GAME_TIME, routes=routes, manager_id=90001, club_id=742)


def fee(counterparty: str, pounds: int, due: str, category: str = "transfer_fee", cid: str | None = None) -> FinancialCommitment:
    return FinancialCommitment(cid or f"{category}:{counterparty}:{due}", counterparty, MovementKind.PAYMENT, GBP(pounds), due, Period.ONCE, None, None, "club", Certainty.OBSERVED_COMMITTED, "test", 1, category)


def wage(counterparty: str, pounds: int, start: str = "2024-02-17", end: str = "2026-06-30", cid: str | None = None) -> FinancialCommitment:
    return FinancialCommitment(cid or f"wage:{counterparty}", counterparty, MovementKind.PAYMENT, GBP(pounds, Period.WEEKLY), start, Period.WEEKLY, end, None, "club", Certainty.OBSERVED_COMMITTED, "test", 1, "wages")


class LedgerFromSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.snap = live_snapshot()
        self.ledger = fin.CommitmentLedger.from_snapshot(self.snap)

    def test_player_and_staff_wages_become_weekly_commitments(self):
        wages = self.ledger.by_category("wages")
        self.assertEqual(len(wages), 24 + 2)
        first = self.ledger.get("contract:employment:1001")
        self.assertEqual(first.amount, GBP(3500, Period.WEEKLY))
        self.assertIs(first.certainty, Certainty.OBSERVED_COMMITTED)
        self.assertIs(first.recurrence, Period.WEEKLY)
        self.assertEqual(first.end_date, "2025-06-30")
        self.assertEqual(first.included_in_aggregate, fin.PAYROLL_AGGREGATE)
        self.assertEqual(first.payer, "club")

    def test_weekly_payroll_uses_aggregate_and_exposes_unexplained_residual(self):
        recon = self.ledger.weekly_payroll(START)
        self.assertEqual(recon.basis, "aggregate")
        self.assertEqual(recon.weekly(), GBP(PLAYER_WAGES, Period.WEEKLY))
        self.assertEqual(recon.contract_sum, GBP(PLAYER_WAGES + STAFF_WAGES, Period.WEEKLY))
        self.assertEqual(recon.label, "unexplained")
        self.assertTrue(recon.residual.available)
        self.assertEqual(recon.residual.value, GBP(-STAFF_WAGES, Period.WEEKLY))
        self.assertEqual(recon.contracts_counted, 26)

    def test_payroll_movements_equal_the_aggregate_not_aggregate_plus_contracts(self):
        # 2024-02-17 is a Saturday, like the contract start dates, so one calendar week holds exactly one payroll cycle.
        week = [m for m in self.ledger.movements(START, START + dt.timedelta(days=6)) if m.category in ("wages", "payroll_residual")]
        total = sum((m.signed.minor for m in week), 0)
        self.assertEqual(total, -PLAYER_WAGES * 100)
        self.assertTrue(any(m.category == "payroll_residual" and "unexplained" in m.label for m in week))

    def test_fin01_payroll_aggregate_is_not_added_on_top_of_per_contract_wages(self):
        """FIN 01 (double counting): payroll charged over the window equals the observed aggregate, never aggregate + contracts."""
        end = fin.add_months(START, 12)
        payroll = [m for m in self.ledger.movements(START, end) if m.category in ("wages", "payroll_residual")]
        weeks = 53                                                     # Saturdays 2024-02-17 .. 2025-02-15 inclusive
        charged = -sum(m.signed.minor for m in payroll)
        self.assertEqual(charged, PLAYER_WAGES * 100 * weeks)
        self.assertNotEqual(charged, (PLAYER_WAGES + PLAYER_WAGES + STAFF_WAGES) * 100 * weeks, "aggregate plus contracts would be the double count")
        contract_lines = [m for m in payroll if m.category == "wages"]
        self.assertEqual(-sum(m.signed.minor for m in contract_lines), (PLAYER_WAGES + STAFF_WAGES) * 100 * weeks, "every contract is still expanded individually")
        self.assertTrue(all(c.included_in_aggregate == fin.PAYROLL_AGGREGATE for c in self.ledger.by_category("wages")))
        proj = fin.CashFlowEngine(transfer_windows=()).project(GBP(10_000_000), START, self.ledger)
        self.assertEqual(proj.path("baseline").end_cash, GBP(10_000_000 - PLAYER_WAGES * weeks))

    def test_contract_without_start_date_is_unknown_not_anchored_on_a_made_up_date(self):
        """Spec 8.1 / 5.2: an unobserved contract start is an unknown, listed at the ledger's as-of date, never 1970."""
        undated = fx.player_payload(3003, "No Start", ["DC"], 1, 700, "Okay")
        undated["contracts"][0].pop("start_date")
        ledger = fin.CommitmentLedger.from_snapshot(hand_snapshot({"/finances": fx.finances_payload(), "/squad": [undated, fx.player_payload(*fx.SQUAD_SPEC[0])]}))
        item = ledger.get("contract:employment:3003")
        self.assertIs(item.certainty, Certainty.UNKNOWN)
        self.assertEqual(item.due_date, fx.GAME_DATE)
        self.assertTrue(any("contract:employment:3003" in n and "start_date unobserved" in n for n in ledger.notes))
        self.assertNotIn(item, ledger.in_aggregate(as_of=START), "an unplaceable line does not reduce the aggregate residual")
        recon = ledger.weekly_payroll(START)
        self.assertEqual(recon.contract_sum, GBP(3500, Period.WEEKLY))
        proj = fin.CashFlowEngine(transfer_windows=()).project(GBP(1_000_000), START, ledger)
        self.assertTrue(proj.unknown_count >= 1)
        self.assertTrue(all(u["commitment_id"] == "contract:employment:3003" and u["reason"] == "certainty unknown" for u in proj.unknown))
        week = [m for m in ledger.movements(START, START + dt.timedelta(days=6)) if m.category in ("wages", "payroll_residual")]
        self.assertEqual([m.certainty for m in week if m.commitment_id == "contract:employment:3003"], [Certainty.UNKNOWN], "listed, never charged")
        charged = -sum(m.signed.minor for m in week if m.certainty is Certainty.OBSERVED_COMMITTED)
        self.assertEqual(charged, PLAYER_WAGES * 100, "cash charged still equals the observed aggregate")
        no_date = DecisionSnapshot("snap-nodate", [], ConsistencyStatus.CONSISTENT, [], [], [], {}, "bridge_observed", "c", "b", fx.SESSION, None, None, routes={"/finances": fx.finances_payload(), "/squad": [undated]}, manager_id=90001, club_id=742)
        dateless = fin.CommitmentLedger.from_snapshot(no_date)
        self.assertIsNone(dateless.get("contract:employment:3003"))
        self.assertTrue(any("no as-of date" in n for n in dateless.notes))
        with self.assertRaises(fin.LedgerError):
            dateless.active()

    def test_missing_aggregate_falls_back_to_contract_sum_with_explicit_status(self):
        routes = dict(self.snap.routes)
        routes["/finances"] = {**fx.finances_payload(), "payroll_spending_weekly": None}
        ledger = fin.CommitmentLedger.from_snapshot(hand_snapshot(routes))
        recon = ledger.weekly_payroll(START)
        self.assertEqual(recon.basis, "contracts_only")
        self.assertIs(recon.residual.status, ValueStatus.NULL)
        self.assertEqual(recon.weekly(), GBP(PLAYER_WAGES + STAFF_WAGES, Period.WEEKLY))
        self.assertEqual([m for m in ledger.movements(START, START) if m.category == "payroll_residual"], [])

    def test_loan_contracts_are_direction_aware(self):
        borrowed = fx.player_payload(3001, "Loanee In", ["ST"], 2, 2000, "Good", club_id=803, club_name="Oxford")
        borrowed["contracts"].append({"kind": "loan", "club_id": 742, "club_name": "Wycombe", "team_id": 7420, "start_date": "2024-01-10", "end_date": "2024-05-31", "weekly_wage_gbp": 1200, "wage_basis": "loan_contribution"})
        loaned_out = fx.player_payload(3002, "Loanee Out", ["DC"], 1, 900, "Okay")
        loaned_out["contracts"].append({"kind": "loan", "club_id": 805, "club_name": "Derby", "team_id": 8005, "start_date": "2024-01-10", "end_date": "2024-05-31", "weekly_wage_gbp": 400, "wage_basis": "loan_contribution"})
        ledger = fin.CommitmentLedger.from_snapshot(hand_snapshot({"/finances": fx.finances_payload(), "/squad": [borrowed, loaned_out]}))
        self.assertIsNone(ledger.get("contract:employment:3001"), "the parent club's wage is not our obligation")
        contribution = ledger.get("contract:loan:3001")
        self.assertIs(contribution.kind, MovementKind.PAYMENT)
        self.assertEqual(contribution.included_in_aggregate, fin.PAYROLL_AGGREGATE)
        incoming = ledger.get("contract:loan:3002")
        self.assertIs(incoming.kind, MovementKind.RECEIPT)
        self.assertIsNone(incoming.included_in_aggregate)
        self.assertEqual(ledger.get("contract:employment:3002").amount, GBP(900, Period.WEEKLY))


class LedgerMutationTests(unittest.TestCase):
    def setUp(self):
        self.ledger = fin.CommitmentLedger(742, fx.GAME_DATE)

    def test_versioned_supersede_and_remove(self):
        self.ledger.add_commitment(fee("Oxford", 100000, "2024-03-01", cid="fee:1"))
        with self.assertRaises(fin.LedgerError):
            self.ledger.add_commitment(fee("Oxford", 90000, "2024-03-01", cid="fee:1"))
        newer = fee("Oxford", 90000, "2024-03-01", cid="fee:1")
        newer.version = 2
        self.ledger.supersede("fee:1", newer)
        self.assertEqual(self.ledger.get("fee:1").amount, GBP(90000))
        self.assertEqual(self.ledger.superseded[0]["replaced_by"], 2)
        self.ledger.remove("fee:1", "offer withdrawn")
        self.assertIsNone(self.ledger.get("fee:1"))
        with self.assertRaises(fin.LedgerError):
            self.ledger.remove("fee:1", "again")

    def test_currency_mismatch_is_refused(self):
        euro = FinancialCommitment("eur", "Ajax", MovementKind.PAYMENT, Money.of(100, "EUR"), "2024-03-01", Period.ONCE, None, None, "club", Certainty.OBSERVED_COMMITTED, "t")
        with self.assertRaises(UnitError):
            self.ledger.add_commitment(euro)

    def test_conditional_commitment_keeps_trigger_text(self):
        bonus = fin.conditional_commitment("Oxford", GBP(50000), "promotion to the Championship", category="bonus", source="offer", due_date="2024-06-30")
        self.assertIs(bonus.certainty, Certainty.CONDITIONAL)
        self.assertEqual(bonus.trigger, "promotion to the Championship")
        with self.assertRaises(fin.LedgerError):
            fin.conditional_commitment("Oxford", GBP(1), "", category="bonus", source="offer", due_date="2024-06-30")


class MoneyTimingTests(unittest.TestCase):
    """FIN 01: calendar and currency fixtures detect weekly/monthly errors and double counting."""

    def test_weekly_and_once_never_summed_as_rates(self):
        with self.assertRaises(UnitError):
            GBP(100, Period.WEEKLY) + GBP(100)
        expanded = fin.expand_commitment(wage("A", 1000, "2024-02-17", "2026-06-30"), START, fin.add_months(START, 12))
        self.assertEqual(len(expanded), 53)     # 2024-02-17 .. 2025-02-15 inclusive: 53 Saturdays
        self.assertEqual(sum(m.signed.minor for m in expanded), -total_over(GBP(1000, Period.WEEKLY), START, fin.add_months(START, 12)).minor)

    def test_monthly_instalments_follow_calendar_month_ends(self):
        parts = fin.instalment_commitments("Oxford", GBP(900000), [("2024-01-31", GBP(300000)), ("2024-02-29", GBP(300000)), ("2024-03-31", GBP(300000))], source="offer", commitment_prefix="fee")
        self.assertEqual([p.due_date for p in parts], ["2024-01-31", "2024-02-29", "2024-03-31"])
        monthly = FinancialCommitment("m", "Oxford", MovementKind.PAYMENT, GBP(300000, Period.MONTHLY), "2024-01-31", Period.MONTHLY, "2024-04-30", None, "club", Certainty.OBSERVED_COMMITTED, "t", 1, "transfer_fee")
        dates = [m.date for m in fin.expand_commitment(monthly, dt.date(2024, 1, 1), dt.date(2024, 12, 31))]
        self.assertEqual(dates, [dt.date(2024, 1, 31), dt.date(2024, 2, 29), dt.date(2024, 3, 31), dt.date(2024, 4, 30)])

    def test_instalments_must_add_up(self):
        with self.assertRaises(fin.LedgerError):
            fin.instalment_commitments("Oxford", GBP(900000), [("2024-03-01", GBP(300000))], source="offer")
        with self.assertRaises(UnitError):
            fin.instalment_commitments("Oxford", GBP(900000, Period.WEEKLY), [("2024-03-01", GBP(900000))], source="offer")

    def test_guaranteed_total_is_calendar_exact(self):
        items = [wage("A", 1000, "2024-02-17", "2024-03-09"), fee("B", 5000, "2024-02-20"), fin.conditional_commitment("C", GBP(99999), "x", category="bonus", source="t", due_date="2024-02-21")]
        self.assertEqual(fin.guaranteed_total(items, START, dt.date(2024, 12, 31)), GBP(4 * 1000 + 5000))


class CashFlowEngineTests(unittest.TestCase):
    def setUp(self):
        self.ledger = fin.CommitmentLedger(742, fx.GAME_DATE)
        self.ledger.aggregates[fin.PAYROLL_AGGREGATE] = Observed.unavailable(ValueStatus.MISSING, fin.PAYROLL_AGGREGATE, "not collected")
        self.engine = fin.CashFlowEngine(transfer_windows=())

    def test_rolling_horizon_and_step_granularity(self):
        self.ledger.add_commitment(fee("Oxford", 1000, "2024-05-15", cid="f"))
        proj = self.engine.project(GBP(10000), START, self.ledger)
        self.assertEqual(proj.end_date, dt.date(2025, 2, 17))
        for offset in range(-3, 4):
            self.assertIn(dt.date(2024, 5, 15) + dt.timedelta(days=offset), proj.steps)
        self.assertNotIn(dt.date(2024, 5, 15) + dt.timedelta(days=5), proj.steps)   # weekly elsewhere
        self.assertIn(START + dt.timedelta(days=7), proj.steps)
        path = proj.path("baseline")
        self.assertEqual(path.min_cash, GBP(9000))
        self.assertEqual(path.min_cash_date, dt.date(2024, 5, 15))
        self.assertEqual(path.end_cash, GBP(9000))

    def test_transfer_window_days_get_daily_steps(self):
        engine = fin.CashFlowEngine()
        proj = engine.project(GBP(1), START, self.ledger)
        self.assertIn(dt.date(2024, 6, 15), proj.steps)
        self.assertIn(dt.date(2024, 6, 16), proj.steps)
        self.assertIn(dt.date(2025, 1, 20), proj.steps)

    def test_instalment_reduces_immediate_outflow_but_creates_dated_obligations(self):
        lump = fin.CommitmentLedger(742, fx.GAME_DATE)
        lump.add_commitment(fee("Oxford", 900000, "2024-02-20", cid="lump"))
        split = fin.CommitmentLedger(742, fx.GAME_DATE)
        split.add_all(fin.instalment_commitments("Oxford", GBP(900000), [("2024-02-20", GBP(300000)), ("2024-08-20", GBP(300000)), ("2025-02-01", GBP(300000))], source="offer", commitment_prefix="inst"))
        lump_path = self.engine.project(GBP(1000000), START, lump).path("baseline")
        split_path = self.engine.project(GBP(1000000), START, split).path("baseline")
        self.assertEqual(lump_path.min_cash, GBP(100000))
        self.assertEqual(split_path.min_cash, GBP(100000))
        self.assertEqual(split_path.min_cash_date, dt.date(2025, 2, 1))
        self.assertEqual(lump_path.min_cash_date, dt.date(2024, 2, 20))
        on_feb_21 = dict(split_path.series)[dt.date(2024, 2, 21)]
        self.assertEqual(on_feb_21, GBP(700000))
        self.assertEqual(sorted(c.due_date for c in split.commitments.values()), ["2024-02-20", "2024-08-20", "2025-02-01"])

    def test_conditional_triggers_resolved_per_scenario_and_unresolved_go_to_unknown(self):
        self.ledger.add_commitment(fin.conditional_commitment("Player", GBP(20000), "promotion", category="bonus", source="t", due_date="2024-06-01", commitment_id="bonus"))
        up = fin.Scenario("promotion", Fraction(1, 4), {"promotion": True}, flags={"promotion": True})
        stay = fin.Scenario("stay_up", Fraction(3, 4), {"promotion": False})
        silent = fin.Scenario("silent", 1)
        proj = self.engine.project(GBP(100000), START, self.ledger, [up, stay])
        self.assertEqual(proj.path("promotion").min_cash, GBP(80000))
        self.assertEqual(proj.path("stay_up").min_cash, GBP(100000))
        self.assertEqual(proj.unknown_count, 0)
        self.assertEqual(proj.path("promotion").weight, Fraction(1, 4))
        proj2 = self.engine.project(GBP(100000), START, self.ledger, [silent])
        self.assertEqual(proj2.unknown_count, 1)
        self.assertIn("unresolved", proj2.unknown[0]["reason"])
        self.assertEqual(proj2.path("silent").min_cash, GBP(100000))

    def test_uncertain_receipts_are_enumerated_not_taken_at_expected_value(self):
        sale = fin.ForecastReceipt("sale of Vokes", GBP(400000), "2024-07-01", Fraction(3, 10), "player_sale")
        scenario = fin.Scenario("summer", 1, receipts=[sale])
        proj = self.engine.project(GBP(50000), START, self.ledger, [scenario])
        names = {p.name: p for p in proj.paths}
        self.assertEqual(set(names), {"summer/sale of Vokes=no", "summer/sale of Vokes=yes"})
        self.assertEqual(names["summer/sale of Vokes=yes"].weight, Fraction(3, 10))
        self.assertEqual(names["summer/sale of Vokes=no"].weight, Fraction(7, 10))
        self.assertEqual(names["summer/sale of Vokes=no"].end_cash, GBP(50000))
        self.assertEqual(names["summer/sale of Vokes=yes"].end_cash, GBP(450000))
        self.assertEqual(proj.worst_min_cash(), GBP(50000))

    def test_uncertain_item_cap_refuses_rather_than_approximates(self):
        receipts = [fin.ForecastReceipt(f"r{i}", GBP(1), "2024-05-01", Fraction(1, 2)) for i in range(fin.MAX_UNCERTAIN_ITEMS_PER_SCENARIO + 1)]
        with self.assertRaises(fin.ProjectionError):
            self.engine.project(GBP(1), START, self.ledger, [fin.Scenario("many", 1, receipts=receipts)])

    def test_forecast_without_probability_is_unknown_not_taken_at_face_value(self):
        """Spec 8.1: a forecast receipt is certain only with an explicit probability of one; none at all is unknown."""
        def prize(prob, cid):
            return FinancialCommitment(cid, "FA", MovementKind.RECEIPT, GBP(1_000_000), "2024-05-01", Period.ONCE, None, None, "FA", Certainty.FORECAST, "t", 1, "prize_money", None, prob)
        self.ledger.add_commitment(prize(None, "silent"))
        proj = self.engine.project(GBP(0), START, self.ledger)
        self.assertEqual(proj.unknown_count, 1)
        self.assertEqual(proj.unknown[0]["commitment_id"], "silent")
        self.assertIn("forecast without a probability", proj.unknown[0]["reason"])
        self.assertEqual(proj.path("baseline").end_cash, GBP(0), "no probability, no cash in any path")
        self.ledger.remove("silent", "test")
        self.ledger.add_commitment(prize(1.0, "sure"))
        self.assertEqual(self.engine.project(GBP(0), START, self.ledger).path("baseline").end_cash, GBP(1_000_000))
        self.ledger.remove("sure", "test")
        self.ledger.add_commitment(prize(0.5, "maybe"))
        proj = self.engine.project(GBP(0), START, self.ledger)
        self.assertEqual(sorted(p.end_cash.minor for p in proj.paths), [0, 100_000_000])

    def test_unknown_certainty_is_bucketed_never_zeroed(self):
        unknown = FinancialCommitment("u", "HMRC", MovementKind.PAYMENT, GBP(5000), "2024-04-01", Period.ONCE, None, None, "club", Certainty.UNKNOWN, "t", 1, "tax")
        self.ledger.add_commitment(unknown)
        proj = self.engine.project(GBP(10000), START, self.ledger)
        self.assertEqual(proj.unknown_count, 1)
        self.assertEqual(proj.unknown[0]["commitment_id"], "u")
        self.assertEqual(proj.path("baseline").min_cash, GBP(10000))

    def test_currency_and_period_errors_are_raised(self):
        with self.assertRaises(UnitError):
            self.engine.project(GBP(1, Period.WEEKLY), START, self.ledger)
        with self.assertRaises(UnitError):
            self.engine.project(GBP(1), START, self.ledger, [fin.Scenario("x", 1, receipts=[fin.ForecastReceipt("weekly gate", GBP(10, Period.WEEKLY), "2024-03-01")])])
        with self.assertRaises(UnitError):
            self.engine.project(Money.of(1, "EUR"), START, self.ledger, [fin.Scenario("x", 1, receipts=[fin.ForecastReceipt("gbp", GBP(10), "2024-03-01")])])


class ReconciliationTests(unittest.TestCase):
    def test_residual_is_labelled_unexplained(self):
        moves = [fin.CashMovement(dt.date(2024, 2, 18), GBP(-1000), Certainty.OBSERVED_COMMITTED, "wages", "w"), fin.CashMovement(dt.date(2024, 2, 19), GBP(250), Certainty.OBSERVED_COMMITTED, "gate", "g")]
        result = fin.reconcile(GBP(10000), GBP(9100), moves)
        self.assertEqual(result.explained, GBP(-750))
        self.assertEqual(result.expected_after, GBP(9250))
        self.assertEqual(result.residual, GBP(-150))
        self.assertEqual(result.label, "unexplained")
        self.assertFalse(result.balanced)
        self.assertTrue(fin.reconcile(GBP(10000), GBP(9250), moves).balanced)


class RiskPolicyTests(unittest.TestCase):
    def setUp(self):
        self.ledger = fin.CommitmentLedger(742, fx.GAME_DATE)
        self.engine = fin.CashFlowEngine(transfer_windows=())

    def test_pass_rate_cvar_and_credibility(self):
        self.ledger.add_commitment(fin.conditional_commitment("Player", GBP(60000), "relegation", category="bonus", source="t", due_date="2024-06-01", commitment_id="drop"))
        good = fin.Scenario("stay_up", Fraction(9, 10), {"relegation": False})
        bad = fin.Scenario("relegation", Fraction(1, 10), {"relegation": True}, stress=True)
        proj = self.engine.project(GBP(100000), START, self.ledger, [good, bad])
        policy = fin.RiskPolicy(GBP(50000), epsilon=Fraction(1, 20), cvar_tail=Fraction(1, 10))
        report = fin.evaluate_risk(proj, policy)
        self.assertEqual(report.pass_rate, Fraction(9, 10))
        self.assertFalse(report.passes)
        self.assertEqual(report.stress_survival, {"relegation": False})
        self.assertEqual(report.cvar_shortfall, GBP(10000))      # worst 10% of weight is exactly the relegation path
        self.assertEqual(report.credibility, fin.CREDIBILITY_UNCALIBRATED)
        self.assertFalse(report.recovery_objective)
        self.assertEqual(report.worst_path, "relegation")
        self.assertIn("NOT an externally guaranteed", fin.RiskReport.__doc__)
        lenient = fin.evaluate_risk(proj, fin.RiskPolicy(GBP(50000), epsilon=Fraction(1, 5)))
        self.assertFalse(lenient.passes, "a stress scenario that is not survived fails regardless of epsilon")
        relaxed = fin.evaluate_risk(proj, fin.RiskPolicy(GBP(30000), epsilon=Fraction(1, 5)))
        self.assertTrue(relaxed.passes)

    def test_existing_shortfall_sets_recovery_objective(self):
        proj = self.engine.project(GBP(1000), START, self.ledger)
        report = fin.evaluate_risk(proj, fin.RiskPolicy(GBP(5000)))
        self.assertTrue(report.recovery_objective)
        self.assertEqual(report.pass_rate, Fraction(0))
        self.assertEqual(report.cvar_shortfall, GBP(4000))

    def test_policy_validation(self):
        with self.assertRaises(UnitError):
            fin.RiskPolicy(GBP(1, Period.WEEKLY))
        with self.assertRaises(ValueError):
            fin.RiskPolicy(GBP(1), epsilon=2)
        self.assertEqual(fin.RiskPolicy(GBP(1), epsilon=0.05).epsilon, Fraction(1, 20))


class PackageFeasibilityTests(unittest.TestCase):
    """FIN 02: a deal within transfer budget but outside cash limits is rejected."""

    def setUp(self):
        self.snap = live_snapshot()
        self.view = finance_view(self.snap)
        self.ledger = fin.CommitmentLedger.from_snapshot(self.snap, self.view)
        self.engine = fin.CashFlowEngine(transfer_windows=())
        self.no_rules = Observed.available_value({"rules": []}, "operator", what="regulatory_limits")

    def test_deal_inside_transfer_budget_but_below_cash_reserve_is_rejected(self):
        package = [fee("Oxford", 1500000, "2024-02-20", cid="fee:target"), wage("Brad Target", 4000, cid="wage:target")]
        policy = fin.RiskPolicy(GBP(9000000))   # balance 10,909,005 minus a year of payroll and the fee dips below this
        report = fin.check_package(package, self.view, self.ledger, policy, engine=self.engine, regulatory=self.no_rules)
        self.assertIs(report.by_name("transfer_budget").status, fin.ConstraintStatus.PASS)
        self.assertIs(report.by_name("wage_budget_headroom_weekly").status, fin.ConstraintStatus.PASS)
        self.assertIs(report.by_name("cash_reserve").status, fin.ConstraintStatus.FAIL)
        self.assertEqual(report.binding, "cash_reserve")
        self.assertIs(report.feasible, False)
        self.assertTrue(report.by_name("cash_reserve").binding)
        self.assertLess(report.risk.worst_min_cash, GBP(9000000))

    def test_feasible_package_passes_every_constraint(self):
        package = [fee("Oxford", 200000, "2024-02-20", cid="fee:small"), wage("Cheap Signing", 1000, cid="wage:small")]
        report = fin.check_package(package, self.view, self.ledger, fin.RiskPolicy(GBP(2000000)), engine=self.engine, regulatory=self.no_rules)
        self.assertIs(report.feasible, True)
        self.assertIsNone(report.binding)
        self.assertEqual(report.unknown, [])

    def test_regulatory_limits_are_unknown_by_default_and_unknown_is_not_a_pass(self):
        report = fin.check_package([], self.view, self.ledger, fin.RiskPolicy(GBP(2000000)), engine=self.engine)
        self.assertIs(report.by_name("regulatory_limits").status, fin.ConstraintStatus.UNKNOWN)
        self.assertIsNone(report.feasible)
        self.assertEqual(report.unknown, ["regulatory_limits"])
        self.assertIsNone(report.binding)

    def test_observed_regulatory_rule_can_fail_the_package(self):
        rule = Observed.available_value({"rules": [{"kind": "max_weekly_wage_bill", "limit": 75000}]}, "operator", what="regulatory_limits")
        report = fin.check_package([wage("Star", 2000, cid="wage:star")], self.view, self.ledger, fin.RiskPolicy(GBP(2000000)), engine=self.engine, regulatory=rule)
        self.assertIs(report.by_name("regulatory_limits").status, fin.ConstraintStatus.FAIL)
        self.assertEqual(report.binding, "regulatory_limits")
        odd = Observed.available_value({"rules": [{"kind": "profit_and_sustainability", "limit": 1}]}, "operator")
        self.assertIs(fin.check_regulatory_limits(odd).status, fin.ConstraintStatus.UNKNOWN)

    def test_transfer_budget_and_wage_headroom_fail_independently(self):
        package = [fee("Oxford", 1800000, "2024-02-20", cid="fee:big"), wage("Star", 5000, cid="wage:star")]
        report = fin.check_package(package, self.view, self.ledger, fin.RiskPolicy(GBP(1000000)), engine=self.engine, regulatory=self.no_rules)
        self.assertIs(report.by_name("cash_reserve").status, fin.ConstraintStatus.PASS)
        self.assertIs(report.by_name("transfer_budget").status, fin.ConstraintStatus.FAIL)
        self.assertIs(report.by_name("wage_budget_headroom_weekly").status, fin.ConstraintStatus.FAIL)
        self.assertEqual(report.binding, "transfer_budget", "the first failing constraint in reporting order is named as binding")

    def test_missing_balance_blocks_with_capability_report(self):
        routes = dict(self.snap.routes)
        routes["/finances"] = {**fx.finances_payload(), "balance": None}
        snap = hand_snapshot(routes)
        view = finance_view(snap)
        ledger = fin.CommitmentLedger.from_snapshot(snap, view)
        report = fin.check_package([], view, ledger, fin.RiskPolicy(GBP(1)), engine=self.engine)
        self.assertIsNotNone(report.blocked)
        self.assertIn("club_finances", report.blocked.missing)
        self.assertIs(report.by_name("cash_reserve").status, fin.ConstraintStatus.UNKNOWN)
        self.assertIsNone(report.projection)
        self.assertIsNone(report.feasible)

    def test_missing_as_of_date_leaves_dated_constraints_unknown_not_passed(self):
        """FIN 02 / spec 8.1: with no in-game date the window is not anchored anywhere; dated constraints stay unknown."""
        view = finance_view(self.snap)
        undated_view = type(view)(view.balance, view.transfer_budget, view.wage_budget_weekly, view.payroll_spending_weekly, None)
        ledger = self.ledger.copy()
        ledger.as_of = None
        package = [fee("Oxford", 50_000_000, "2024-02-20", cid="fee:huge"), wage("Star", 1000, cid="wage:star")]
        report = fin.check_package(package, undated_view, ledger, fin.RiskPolicy(GBP(1)), engine=self.engine, regulatory=self.no_rules)
        self.assertIs(report.by_name("cash_reserve").status, fin.ConstraintStatus.UNKNOWN)
        self.assertIs(report.by_name("transfer_budget").status, fin.ConstraintStatus.UNKNOWN)
        self.assertIs(report.by_name("wage_budget_headroom_weekly").status, fin.ConstraintStatus.PASS, "the weekly headroom test needs no window")
        self.assertIsNone(report.feasible)
        self.assertIsNone(report.projection)
        self.assertIsNone(report.binding)
        self.assertIn("game_date", report.blocked.missing)
        self.assertIn("no as-of date", report.by_name("cash_reserve").reason)
        self.assertEqual(report.package_summary["guaranteed_total_in_window"], "unknown (no window)")
        self.assertEqual(report.package_summary["items"], 2)
        dated = fin.check_package(package, undated_view, ledger, fin.RiskPolicy(GBP(1)), start_date=fx.GAME_DATE, engine=self.engine, regulatory=self.no_rules)
        self.assertIs(dated.feasible, False, "the same package with a date is tested and fails")
        self.assertIs(dated.by_name("transfer_budget").status, fin.ConstraintStatus.FAIL)

    def test_no_sale_or_owner_funding_is_assumed(self):
        package = [fee("Oxford", 1500000, "2024-02-20", cid="fee:target")]
        hopeful = fin.Scenario("sale funds it", 1, receipts=[fin.ForecastReceipt("sell Vokes", GBP(5000000), "2024-02-19", Fraction(1, 2))])
        report = fin.check_package(package, self.view, self.ledger, fin.RiskPolicy(GBP(9000000), epsilon=Fraction(1, 20)), [hopeful], engine=self.engine, regulatory=self.no_rules)
        self.assertIs(report.feasible, False)
        self.assertEqual(report.risk.pass_rate, Fraction(1, 2))


if __name__ == "__main__":
    unittest.main()
