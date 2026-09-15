"""Tests for fm_bot.planning.recruitment (spec 7.2, 7.3, 8.3, 11.1, 17.1).

The squad, tactic and fixtures come from the offline fixture world. Money
is exact GBP; wages weekly. The 17.1 worked example lives in
:class:`WorkedExampleTests`: candidate A improves the eleven but needs
promotion income to respect the reserve rule, candidate B stays feasible.
"""
from __future__ import annotations

import json
import unittest
from fractions import Fraction

from ..planning import finance as fin
from ..planning import lineup as lu
from ..planning import minutes as mn
from ..planning import recruitment as rc
from ..state.records import Certainty, CompetitionContext, FinancialCommitment, MovementKind
from ..state.status import Observed, ValueStatus
from ..state.units import Money, Period
from ..state.views import FinanceView, finance_view, fixture_views, player_state, tactic_view
from ..state.visibility import InformationMode
from . import fixtures as fx

CLUB = 742
GBP = lambda pounds, period=Period.ONCE: Money.native_gbp(pounds, period)  # noqa: E731
NO_RULES = Observed.available_value({"rules": []}, "operator", what="regulatory_limits")


def players():
    return [player_state(fx.player_payload(*spec), source="test") for spec in fx.SQUAD_SPEC]


def hand_snapshot():
    return fx.hand_snapshot({"/tactics": fx.tactics_payload(), "/fixtures": fx.fixtures_payload()}, club_id=CLUB)


def slots():
    return lu.slots_from_tactic_view(tactic_view(hand_snapshot())).require()


def upcoming(count=5):
    return [f for f in fixture_views(hand_snapshot()) if f.scheduled][:count]


def context(squad=None, fixtures=None, **kw) -> rc.SquadContext:
    kw.setdefault("club_id", CLUB)
    return rc.SquadContext(squad or players(), slots(), fixtures if fixtures is not None else upcoming(), **kw)


def candidate_state(pid=2001, name="Brad Target", positions=("DL", "WBL"), tier=3, wage=4500, **kw):
    return player_state(fx.other_club_player(pid, name, list(positions), tier, wage), source="scout", **kw)


def terms(counterparty="Oxford", *, wage=4500, fee=1_500_000, prefix=None, **kw):
    return rc.package_terms(counterparty, weekly_wage=GBP(wage, Period.WEEKLY), wage_start="2024-02-20", wage_end="2027-06-30", source="offer:test", fee=GBP(fee) if fee is not None else None, fee_due="2024-02-20" if fee is not None else None, prefix=prefix, **kw)


def package(candidate_id="A", state=None, **kw):
    state = state or candidate_state()
    return rc.CandidatePackage(candidate_id, state, kw.pop("kind", "buy"), kw.pop("terms", terms(prefix=f"pkg:{candidate_id}")), kw.pop("acceptance", rc.ACCEPTANCE_PLAUSIBLE), kw.pop("acceptance_basis", "agent indicated interest"), target_position="DL", **kw)


def live_snapshot(routes=("/finances", "/squad", "/staff")):
    return fx.snapshot_for(routes, club_id=CLUB)


def finance_inputs(reserve_pounds: int, scenarios=None, *, regulatory=NO_RULES) -> rc.FinanceInputs:
    snap = live_snapshot()
    view = finance_view(snap)
    ledger = fin.CommitmentLedger.from_snapshot(snap, view)
    return rc.FinanceInputs(view, ledger, fin.RiskPolicy(GBP(reserve_pounds)), scenarios, fin.CashFlowEngine(transfer_windows=()), regulatory, fx.GAME_DATE)


# ---------------------------------------------------------------------------
# packages and terms
# ---------------------------------------------------------------------------

class CandidatePackageTests(unittest.TestCase):
    def test_package_exposes_exact_money_totals(self):
        pkg = package()
        self.assertEqual(pkg.weekly_wage(), GBP(4500, Period.WEEKLY))
        self.assertEqual(pkg.guaranteed_fees("2024-02-17", "2025-02-17"), GBP(1_500_000))
        self.assertEqual(pkg.guaranteed_fees("2024-03-01", "2025-02-17"), GBP(0), "a fee due before the window is not inside it")
        self.assertEqual(pkg.conditional_total(), GBP(0))
        self.assertEqual(pkg.currency, "GBP")
        self.assertEqual(pkg.player_id, 2001)

    def test_conditional_terms_never_count_as_guaranteed(self):
        pkg = package(terms=terms(conditional=[("promotion", GBP(250_000), "club promoted to the Championship", "2024-06-30")]))
        self.assertEqual(pkg.conditional_total(), GBP(250_000))
        self.assertEqual(pkg.guaranteed_fees("2024-02-17", "2025-02-17"), GBP(1_500_000))
        conditional = [c for c in pkg.terms if c.certainty is Certainty.CONDITIONAL]
        self.assertEqual(len(conditional), 1)
        self.assertEqual(conditional[0].trigger, "club promoted to the Championship")
        self.assertEqual(conditional[0].category, "bonus")

    def test_instalments_must_add_up_and_fee_needs_a_due_date(self):
        parts = [("2024-02-20", GBP(500_000)), ("2024-08-01", GBP(500_000)), ("2025-02-01", GBP(500_000))]
        built = terms(instalments=parts)
        fees = [c for c in built if c.category == "transfer_fee"]
        self.assertEqual(len(fees), 3)
        self.assertEqual(sum((c.amount.minor for c in fees), 0), GBP(1_500_000).minor)
        with self.assertRaises(ValueError):
            terms(instalments=[("2024-02-20", GBP(1))])
        with self.assertRaises(ValueError):
            rc.package_terms("Oxford", weekly_wage=GBP(100, Period.WEEKLY), wage_start="2024-02-20", wage_end="2025-06-30", source="t", fee=GBP(10))
        with self.assertRaises(ValueError):
            rc.package_terms("Oxford", weekly_wage=GBP(100), wage_start="2024-02-20", wage_end="2025-06-30", source="t")

    def test_labels_and_currencies_are_validated(self):
        with self.assertRaises(ValueError):
            package(kind="steal")
        with self.assertRaises(ValueError):
            package(acceptance="certain")
        mixed = terms() + [FinancialCommitment("eur", "Oxford", MovementKind.PAYMENT, Money(100, "EUR", Period.ONCE), "2024-03-01", Period.ONCE, None, None, "club", Certainty.OBSERVED_COMMITTED, "t", 1, "agent_fee")]
        with self.assertRaises(ValueError):
            package(terms=mixed)

    def test_default_registration_and_availability_are_missing_not_true(self):
        pkg = package()
        self.assertIs(pkg.registration.status, ValueStatus.MISSING)
        self.assertIs(pkg.availability.status, ValueStatus.MISSING)
        self.assertFalse(pkg.registration.available)
        data = pkg.to_json()
        self.assertEqual(data["registration"]["status"], "missing")
        self.assertEqual(data["acceptance"], "plausible")
        json.dumps(data)

    def test_with_terms_bumps_the_version_and_keeps_the_player(self):
        pkg = package()
        cheaper = pkg.with_terms(terms(wage=3000, fee=900_000, prefix="pkg:A"), source="negotiation:1")
        self.assertEqual(cheaper.terms_version, 2)
        self.assertEqual(cheaper.weekly_wage(), GBP(3000, Period.WEEKLY))
        self.assertIs(cheaper.player, pkg.player)
        self.assertEqual(pkg.terms_version, 1, "the original package is untouched")


class RegistrationFeasibilityTests(unittest.TestCase):
    def _context(self, limit=None, windows=None, status="available"):
        squad_rules = {"status": status, "squad_size_limit": {"status": "available", "value": limit} if limit is not None else {"status": "missing", "value": None}}
        if windows is not None:
            squad_rules["registration_windows"] = {"status": "available", "value": windows}
        return CompetitionContext(33, "Cup", "current", None, squad_rules, {}, [], "operator", 1)

    def test_no_profile_is_missing_not_true(self):
        self.assertIs(rc.registration_feasibility(None, 24, fx.GAME_DATE).status, ValueStatus.MISSING)
        self.assertIs(rc.registration_feasibility(self._context(status="missing"), 24, fx.GAME_DATE).status, ValueStatus.MISSING)
        self.assertIs(rc.registration_feasibility(self._context(), 24, fx.GAME_DATE).status, ValueStatus.MISSING)

    def test_limit_and_window_are_applied_when_observed(self):
        self.assertIs(rc.registration_feasibility(self._context(limit=24), 24, fx.GAME_DATE).value, False)
        self.assertIs(rc.registration_feasibility(self._context(limit=25), 24, fx.GAME_DATE).value, True)
        closed = self._context(limit=25, windows=[{"opens": "2024-01-01", "closes": "2024-02-01"}])
        self.assertIs(rc.registration_feasibility(closed, 24, fx.GAME_DATE).value, False)
        open_ = self._context(limit=25, windows=[{"opens": "2024-02-10", "closes": "2024-02-28"}])
        self.assertIs(rc.registration_feasibility(open_, 24, fx.GAME_DATE).value, True)


# ---------------------------------------------------------------------------
# marginal contribution
# ---------------------------------------------------------------------------

class MarginalContributionTests(unittest.TestCase):
    def setUp(self):
        self.ctx = context()
        self.baseline = mn.plan(self.ctx.request())

    def test_strong_left_back_changes_the_plan_and_is_reported_unverified(self):
        pkg = package()
        result = rc.marginal_contribution(pkg, self.ctx, baseline=self.baseline)
        self.assertTrue(result.available)
        self.assertGreater(result.change("horizon_lineup_objective_sum"), 0.0)
        self.assertGreater(result.change("candidate_starts"), 0.0)
        self.assertEqual(result.change("candidate_planned_minutes"), 90.0 * result.change("candidate_starts"))
        self.assertTrue(result.unverified, "a candidate is never verified eligible for our fixtures")
        self.assertEqual(result.method, "resolve_with_and_without")
        self.assertTrue(result.candidate_slots)
        self.assertTrue(all(s["position"] in ("DL", "WBL") for s in result.candidate_slots))
        self.assertTrue(result.displaced, "somebody loses the starts the candidate takes")
        self.assertTrue(all(row["starts_lost"] > 0 for row in result.displaced))
        json.dumps(result.to_json())

    def test_role_coverage_reports_depth_with_and_without(self):
        result = rc.marginal_contribution(package(), self.ctx, baseline=self.baseline)
        by_slot = {c.slot: c for c in result.coverage}
        left_back = next(c for c in result.coverage if c.position == "DL")
        self.assertEqual(left_back.change, 1)
        self.assertEqual(left_back.depth_with, left_back.depth_without + 1)
        self.assertIn(left_back.slot, result.coverage_summary["deepened"])
        goalkeeper = by_slot[0]
        self.assertEqual(goalkeeper.change, 0, "a full-back is no goalkeeper cover")
        self.assertEqual(result.coverage_summary["min_depth"], rc.COVERAGE_MIN_DEPTH)

    def test_valuation_is_labelled_and_never_a_price(self):
        result = rc.marginal_contribution(package(), self.ctx, baseline=self.baseline)
        valuation = result.valuation
        self.assertEqual(valuation.label, rc.VALUATION_LABEL)
        self.assertEqual(valuation.unit, rc.VALUATION_UNIT)
        self.assertFalse(valuation.executable_buying_price.available)
        self.assertFalse(valuation.guaranteed_resale_income.available)
        self.assertIs(valuation.executable_buying_price.status, ValueStatus.UNSUPPORTED)
        self.assertEqual(valuation.model_value, round(result.change("horizon_lineup_objective_sum"), 9))

    def test_masked_attributes_leave_the_contribution_unavailable_not_zero(self):
        masked = candidate_state(mode=InformationMode.MANAGER_VISIBLE)
        self.assertEqual(masked.attributes, {})
        result = rc.marginal_contribution(package(state=masked), self.ctx, baseline=self.baseline)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("cannot be scored", result.reason)
        self.assertEqual(result.components, {})
        self.assertIsNone(result.valuation)
        self.assertIsNone(result.versatility)
        self.assertTrue(result.coverage, "coverage of the current squad is still reported")

    def test_refuses_a_squad_member_and_an_empty_horizon(self):
        insider = player_state(fx.player_payload(*fx.SQUAD_SPEC[7]), source="test")
        result = rc.marginal_contribution(package(state=insider), self.ctx, baseline=self.baseline)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("already in the squad", result.reason)
        empty = rc.marginal_contribution(package(), context(fixtures=[]))
        self.assertEqual(empty.status, "unavailable")
        self.assertIn("no fixtures", empty.reason)


class VersatilityTests(unittest.TestCase):
    def setUp(self):
        self.ctx = context()

    def test_default_scenarios_are_one_per_starter_equally_weighted(self):
        base = lu.solve(self.ctx.lineup_request())
        scenarios = rc.default_availability_scenarios(base)
        self.assertEqual(len(scenarios), 11)
        self.assertEqual(sum((s.weight for s in scenarios), Fraction(0)), Fraction(1))
        self.assertEqual({s.unavailable_player_ids for s in scenarios}, {(pid,) for pid in base.player_ids})

    def test_value_is_the_weighted_reduction_in_loss_with_one_assignment_per_scenario(self):
        candidate = candidate_state()
        left_back = next(a.player_id for a in lu.solve(self.ctx.lineup_request()).assignments if a.position == "DL")
        keeper = next(a.player_id for a in lu.solve(self.ctx.lineup_request()).assignments if a.position == "GK")
        scenarios = [rc.AvailabilityScenario("left-back out", (left_back,), Fraction(3)), rc.AvailabilityScenario("keeper out", (keeper,), Fraction(1))]
        value = rc.versatility_value(self.ctx, candidate, scenarios)
        self.assertEqual(len(value.scenarios), 2)
        rows = {r["name"]: r for r in value.scenarios}
        self.assertEqual(rows["left-back out"]["weight"], "3/4")
        self.assertEqual(rows["keeper out"]["weight"], "1/4")
        for row in value.scenarios:
            self.assertAlmostEqual(row["reduction"], row["loss_without"] - row["loss_with"], places=6)
        expected = 0.75 * rows["left-back out"]["reduction"] + 0.25 * rows["keeper out"]["reduction"]
        self.assertAlmostEqual(value.value, expected, places=6)
        self.assertGreaterEqual(rows["left-back out"]["reduction"], rows["keeper out"]["reduction"] - 1e-9, "cover for the left-back's absence is where a left-back helps")
        self.assertIn("one simultaneous assignment", value.note)

    def test_weights_must_be_positive_and_no_scenarios_is_explicit(self):
        with self.assertRaises(ValueError):
            rc.versatility_value(self.ctx, candidate_state(), [rc.AvailabilityScenario("none", (1001,), Fraction(0))])
        empty = rc.versatility_value(context(fixtures=upcoming(1)), candidate_state(), [])
        self.assertEqual(empty.value, 0.0)
        self.assertIn("no availability scenarios", empty.note)


class ShadowValueTests(unittest.TestCase):
    def setUp(self):
        self.ctx = context()
        self.baseline = mn.plan(self.ctx.request())

    def test_squad_place_of_a_regular_starter_is_worth_more_than_a_fringe_player(self):
        starts = {}
        for fixture in self.baseline.fixtures:
            for pid in fixture.planned_minutes:
                starts[pid] = starts.get(pid, 0) + 1
        regular = max(starts, key=starts.get)
        fringe = next(p.player_id for p in self.ctx.players if p.player_id not in starts)
        regular_value = rc.shadow_value_of_squad_place(self.ctx, rc.RESOURCE_SQUAD_PLACE, regular, baseline=self.baseline)
        fringe_value = rc.shadow_value_of_squad_place(self.ctx, rc.RESOURCE_SQUAD_PLACE, fringe, baseline=self.baseline)
        self.assertTrue(regular_value.value.available)
        self.assertGreater(regular_value.value.value, fringe_value.value.value)
        self.assertGreaterEqual(fringe_value.value.value, -1e-9)
        self.assertEqual(regular_value.label, rc.VALUATION_LABEL)
        self.assertEqual(regular_value.detail["starts_lost"], starts[regular])
        self.assertEqual(regular_value.unit, rc.VALUATION_UNIT)
        json.dumps(regular_value.to_json())

    def test_role_cover_names_the_starter_and_the_replacement(self):
        value = rc.shadow_value_of_squad_place(self.ctx, rc.RESOURCE_ROLE_COVER, 0)
        self.assertTrue(value.value.available)
        self.assertEqual(value.detail["starter"], 1001, "first-choice keeper")
        self.assertEqual(value.detail["replacement"], 1002, "the second keeper steps in")
        self.assertGreater(value.value.value, 0.0)

    def test_unknown_resources_are_explicit(self):
        missing = rc.shadow_value_of_squad_place(self.ctx, rc.RESOURCE_SQUAD_PLACE, 9999)
        self.assertIs(missing.value.status, ValueStatus.MISSING)
        with self.assertRaises(ValueError):
            rc.shadow_value_of_squad_place(self.ctx, "goodwill", 1)
        no_fixtures = rc.shadow_value_of_squad_place(context(fixtures=[]), rc.RESOURCE_SQUAD_PLACE, 1001)
        self.assertFalse(no_fixtures.value.available)


# ---------------------------------------------------------------------------
# evaluator, recompute hook and tradeoffs
# ---------------------------------------------------------------------------

class EvaluatorTests(unittest.TestCase):
    def test_without_finance_inputs_feasibility_is_blocked_not_assumed(self):
        evaluator = rc.RecruitmentEvaluator(context(), None)
        evaluation = evaluator.evaluate(package())
        self.assertIsNone(evaluation.feasibility.feasible)
        self.assertIsNotNone(evaluation.feasibility.blocked)
        self.assertIn("club_finances", evaluation.feasibility.blocked.missing)
        self.assertTrue(all(c.status is fin.ConstraintStatus.UNKNOWN for c in evaluation.feasibility.constraints))
        self.assertTrue(evaluation.contribution.available)

    def test_no_in_game_date_means_no_window_and_unknown_dated_constraints(self):
        """FIN 02 / spec 8.1: the projection window is never anchored on an invented date."""
        inputs = finance_inputs(6_000_000)
        inputs.start_date = None
        inputs.ledger.as_of = None
        inputs.finance_view = FinanceView(inputs.finance_view.balance, inputs.finance_view.transfer_budget, inputs.finance_view.wage_budget_weekly, inputs.finance_view.payroll_spending_weekly, None)
        self.assertIsNone(inputs.as_of())
        self.assertIsNone(inputs.window())
        report = rc.package_feasibility(package(), inputs)
        self.assertIs(report.by_name("cash_reserve").status, fin.ConstraintStatus.UNKNOWN)
        self.assertIs(report.by_name("transfer_budget").status, fin.ConstraintStatus.UNKNOWN)
        self.assertIsNone(report.feasible)
        self.assertIn("game_date", report.blocked.missing)
        inputs.start_date = fx.GAME_DATE
        self.assertEqual(inputs.window()[0].isoformat(), fx.GAME_DATE)

    def test_terms_change_recomputes_finance_and_reuses_the_sporting_side(self):
        evaluator = rc.RecruitmentEvaluator(context(), finance_inputs(6_000_000))
        pkg = package()
        first = evaluator.evaluate(pkg)
        self.assertEqual(first.evaluated_terms_version, 1)
        second = evaluator.on_terms_changed(pkg, terms(wage=1500, fee=200_000, prefix="pkg:A"), source="negotiation:n1")
        self.assertEqual(second.evaluated_terms_version, 2)
        self.assertIs(second.contribution, first.contribution, "same player, same squad: the re-solve is reused")
        self.assertNotEqual(second.feasibility.package_summary, first.feasibility.package_summary)
        self.assertEqual(second.package.source, "negotiation:n1")

    def test_from_negotiation_uses_the_state_machine_planning_commitments(self):
        class FakeNegotiation:
            negotiation_id = "neg-7"

            def planning_commitments(self):
                return terms(wage=2000, fee=300_000, prefix="neg")

        evaluator = rc.RecruitmentEvaluator(context(), finance_inputs(6_000_000))
        evaluation = evaluator.from_negotiation(package(), FakeNegotiation())
        self.assertEqual(evaluation.package.source, "negotiation:neg-7")
        self.assertEqual(evaluation.package.weekly_wage(), GBP(2000, Period.WEEKLY))


class TradeoffTests(unittest.TestCase):
    def test_table_shows_dimensions_and_no_ranking(self):
        evaluator = rc.RecruitmentEvaluator(context(), None)
        table = rc.package_tradeoffs([evaluator.evaluate(package("A")), evaluator.evaluate(package("B", candidate_state(2002, "Sam Winger", ("DL", "ML", "DC"), 2, 1500), terms=terms(wage=1500, fee=200_000, prefix="pkg:B")))])
        self.assertIsNone(table.ranking)
        self.assertEqual(table.dimensions, rc.TRADEOFF_DIMENSIONS)
        self.assertEqual({r.candidate_id for r in table.rows}, {"A", "B"})
        row = table.row("A")
        for name in rc.TRADEOFF_DIMENSIONS:
            self.assertIn(name, row.values)
        self.assertEqual(row.values["weekly_wage"], str(GBP(4500, Period.WEEKLY)))
        self.assertIsNone(row.values["feasible"])
        self.assertTrue(any("feasibility unknown" in n for n in row.notes))
        self.assertIn("not a ranking", table.note)
        json.dumps(table.to_json())
        with self.assertRaises(KeyError):
            table.row("Z")

    def test_fees_without_a_window_are_the_all_dates_total_not_a_made_up_window(self):
        """Spec 8.1: no projection window means the package's committed fees over every date, labelled as such."""
        evaluator = rc.RecruitmentEvaluator(context(), None)
        parts = [("2024-02-20", GBP(500_000)), ("2025-08-01", GBP(500_000)), ("2026-02-01", GBP(500_000))]
        evaluation = evaluator.evaluate(package("A", terms=terms(instalments=parts, prefix="pkg:A")))
        no_window = rc.package_tradeoffs([evaluation]).row("A")
        self.assertEqual(no_window.values["fees_basis"], "all_dates")
        self.assertEqual(no_window.values["guaranteed_fees"], str(GBP(1_500_000)))
        windowed = rc.package_tradeoffs([evaluation], window=("2024-02-17", "2025-02-17")).row("A")
        self.assertEqual(windowed.values["fees_basis"], "window")
        self.assertEqual(windowed.values["guaranteed_fees"], str(GBP(500_000)))

    def test_infeasible_packages_cannot_dominate(self):
        evaluator = rc.RecruitmentEvaluator(context(), finance_inputs(6_000_000))
        strong = evaluator.evaluate(package("A"))
        modest = evaluator.evaluate(package("B", candidate_state(2002, "Sam Winger", ("DL", "ML", "DC"), 2, 1500), terms=terms(wage=1500, fee=200_000, prefix="pkg:B")))
        self.assertIs(strong.feasibility.feasible, False)
        self.assertIs(modest.feasibility.feasible, True)
        table = rc.package_tradeoffs([strong, modest])
        self.assertEqual(table.undominated, ["B"])
        self.assertTrue(any("infeasible" in n for n in table.row("A").notes))


class WorkedExampleTests(unittest.TestCase):
    """Spec 17.1: A improves the eleven but needs promotion income to meet the reserve; B stays feasible."""

    def setUp(self):
        promotion = fin.Scenario("promotion", Fraction(1, 2), receipts=[fin.ForecastReceipt("promotion prize money", GBP(3_000_000), "2024-05-30", 1)], flags={"promotion": True})
        stay = fin.Scenario("stay in the division", Fraction(1, 2), stress=True)
        self.inputs = finance_inputs(6_000_000, [promotion, stay])
        self.evaluator = rc.RecruitmentEvaluator(context(), self.inputs)
        self.a = package("A", candidate_state(2001, "Candidate A", ("DL", "WBL"), 3, 4500), terms=terms(wage=4500, fee=1_500_000, prefix="pkg:A"))
        self.b = package("B", candidate_state(2002, "Candidate B", ("DL", "ML", "DC"), 2, 1500), terms=terms(wage=1500, fee=200_000, prefix="pkg:B"))

    def test_candidate_a_needs_promotion_income_and_is_infeasible_under_the_policy(self):
        evaluation = self.evaluator.evaluate(self.a)
        feasibility = evaluation.feasibility
        self.assertIs(feasibility.by_name("transfer_budget").status, fin.ConstraintStatus.PASS)
        self.assertIs(feasibility.by_name("wage_budget_headroom_weekly").status, fin.ConstraintStatus.PASS, "A consumes most of the wage capacity but fits")
        self.assertIs(feasibility.by_name("cash_reserve").status, fin.ConstraintStatus.FAIL)
        self.assertEqual(feasibility.binding, "cash_reserve")
        self.assertIs(feasibility.feasible, False)
        risk = feasibility.risk
        self.assertEqual(risk.pass_rate, Fraction(1, 2), "only the promotion path clears the reserve")
        self.assertFalse(risk.stress_survival["stay in the division"])
        self.assertTrue(feasibility.projection.path("promotion").min_cash >= GBP(6_000_000))
        self.assertTrue(feasibility.projection.path("stay in the division").min_cash < GBP(6_000_000))

    def test_candidate_b_is_feasible_and_covers_more_roles(self):
        a, b = self.evaluator.evaluate(self.a), self.evaluator.evaluate(self.b)
        self.assertIs(b.feasibility.feasible, True)
        self.assertIsNone(b.feasibility.binding)
        self.assertGreater(a.contribution.change("horizon_lineup_objective_sum"), b.contribution.change("horizon_lineup_objective_sum"), "A is the better footballer for the eleven")
        self.assertGreater(len(b.contribution.coverage_summary["deepened"]), len(a.contribution.coverage_summary["deepened"]), "B covers more of the tactic")

    def test_report_presents_the_tradeoff_rather_than_a_verdict(self):
        table = rc.package_tradeoffs([self.evaluator.evaluate(self.a), self.evaluator.evaluate(self.b)], window=self.inputs.window())
        self.assertIsNone(table.ranking)
        self.assertEqual(table.undominated, ["B"])
        row_a, row_b = table.row("A"), table.row("B")
        self.assertIs(row_a.values["feasible"], False)
        self.assertEqual(row_a.values["binding_constraint"], "cash_reserve")
        self.assertIs(row_b.values["feasible"], True)
        self.assertEqual(row_a.values["guaranteed_fees"], str(GBP(1_500_000)))
        self.assertEqual(row_b.values["weekly_wage"], str(GBP(1500, Period.WEEKLY)))
        self.assertEqual(row_a.values["acceptance"], "plausible")
        self.assertEqual(row_a.values["registration"], "missing", "nobody has checked registration; it is not assumed")


# ---------------------------------------------------------------------------
# succession gaps
# ---------------------------------------------------------------------------

class SuccessionTests(unittest.TestCase):
    def test_gaps_are_grouped_by_base_position_and_explain_themselves(self):
        gaps = rc.succession_gaps(players(), slots(), fx.GAME_DATE, club_id=CLUB)
        positions = [g.position for g in gaps]
        self.assertEqual(len(positions), len(set(positions)))
        self.assertNotIn("DCR", positions)
        self.assertIn("DC", positions)
        keeper = next(g for g in gaps if g.position == "GK")
        self.assertEqual(keeper.horizon_date, "2027-02-17")
        self.assertEqual(sorted(keeper.expiring), [1001, 1002], "every fixture contract ends 2025-06-30")
        self.assertEqual(keeper.depth_after, 0)
        self.assertIn("contract(s) end by 2027-02-17", keeper.reason)
        json.dumps([g.to_json() for g in gaps])

    def test_long_contracts_and_young_cover_leave_only_the_thin_positions(self):
        squad = []
        for spec in fx.SQUAD_SPEC:
            payload = fx.player_payload(*spec)
            payload["contracts"][0]["end_date"] = "2028-06-30"
            payload["age"] = 22
            squad.append(player_state(payload, source="test"))
        gaps = rc.succession_gaps(squad, slots(), fx.GAME_DATE, club_id=CLUB)
        self.assertEqual([g.position for g in gaps], ["ML"], "the fixture squad has one left midfielder: thin today, not a contract question")
        self.assertEqual(gaps[0].expiring, [])
        self.assertEqual(gaps[0].ageing, [])
        self.assertEqual(gaps[0].depth_after, 1)

    def test_unobserved_contract_end_is_reported_not_assumed(self):
        squad = []
        for spec in fx.SQUAD_SPEC:
            payload = fx.player_payload(*spec)
            payload["contracts"][0]["end_date"] = "2028-06-30"
            payload["age"] = 22
            if spec[0] == 1001:
                payload["contracts"][0]["end_date"] = "2025-06-30"     # first-choice keeper leaves inside the horizon
            if spec[0] == 1002:
                payload["contracts"] = []                               # second keeper: contract and age unobserved
                payload["age"] = None
            squad.append(player_state(payload, source="test"))
        gaps = rc.succession_gaps(squad, slots(), fx.GAME_DATE, club_id=CLUB)
        keeper = next(g for g in gaps if g.position == "GK")
        self.assertEqual(keeper.expiring, [1001])
        self.assertIn("unobserved for [1002]", keeper.reason)
        self.assertNotIn(1002, keeper.expiring, "an unobserved contract end is not counted as leaving")
        self.assertNotIn(1002, keeper.ageing, "an unobserved age is not counted as ageing")
        self.assertEqual(keeper.depth_after, 1)


if __name__ == "__main__":
    unittest.main()
