"""Tests for fm_bot.planning.planner (spec 4.1, 5.4, 6.2, 7.3, 8.3, 16.2 phase 1, 17.1).

Every test plans from a consistent snapshot of the offline fixture world.
Default providers mean no eligibility source, no rules profiles and no
language model, which is the honest Phase 1 state: advice, not execution.
"""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from fractions import Fraction

from ..bridge_client.transport import FakeTransport
from ..execution.adapter import FAKE_WORKFLOWS, FakeAdapter
from ..execution.verification import PLANS, Evidence, VerdictKind, verify, verify_lineup
from ..interactions.promises import PromiseLedger, promise_from_observed
from ..models.registry import ModelRegistry
from ..planning import finance as fin
from ..planning import planner as pl
from ..planning import recruitment as rc
from ..planning.objective import default_profile
from ..rules.authority import AuthorityLimits, AuthorityMode, AuthorityProfile
from ..rules.capabilities import ACTION_REQUIREMENTS, CapabilityRegistry
from ..rules.competitions import RulesProfileRegistry
from ..rules.eligibility import DeclaredEligibilityProvider, EligibilityObservation
from ..state.records import ActionState, ConsistencyStatus, DecisionSnapshot
from ..state.status import Observed
from ..state.units import Money, Period
from ..state.views import player_state
from . import fixtures as fx

CLUB = 742
ROUTES = ["/game", "/manager", "/club", "/squad", "/finances", "/fixtures", "/tactics", "/inbox", "/staff"]
GBP = lambda pounds, period=Period.ONCE: Money.native_gbp(pounds, period)  # noqa: E731
NO_RULES = Observed.available_value({"rules": []}, "operator", what="regulatory_limits")


class World:
    """One registered career with a consistent snapshot of the fixture bridge."""

    def __init__(self, routes=ROUTES, overrides=None):
        self.snapshot = fx.snapshot_for(routes, overrides=overrides, club_id=CLUB)
        self.store, self.career, self.branch, self.client = self.snapshot.store, self.snapshot.career, self.snapshot.branch, self.snapshot.client

    def full_registry(self, *kinds: str) -> CapabilityRegistry:
        registry = CapabilityRegistry.from_status(fx.status_payload(), supported_builds=[fx.BUILD])
        for kind in kinds or ("submit.lineup",):
            for name in ACTION_REQUIREMENTS[kind]:
                registry.provide(name, "ui_adapter", "test double")
        return registry

    def verified_eligibility(self) -> DeclaredEligibilityProvider:
        provider = DeclaredEligibilityProvider(fx.GAME_DATE, fx.GAME_TIME)
        for spec in fx.SQUAD_SPEC:
            provider.register(EligibilityObservation.clear(spec[0], source="ui:squad", observed_at="2026-01-01T00:00:00Z", game_date=fx.GAME_DATE, game_time=fx.GAME_TIME, registered=True, competition_id=33))
        return provider

    def rules(self) -> RulesProfileRegistry:
        registry = RulesProfileRegistry(self.store)
        for competition in (33, 14):
            registry.store_profile(fx.rules_profile(competition, "current", with_deadlines=False))
        return registry

    def promises(self) -> PromiseLedger:
        ledger = PromiseLedger(self.store, self.branch.branch_id)
        promise, terms = promise_from_observed(1018, "player", "You will be a regular starter", source="obs:choice-1")
        ledger.record(promise, terms)
        return ledger


def scoped(*families: str, **limits) -> AuthorityProfile:
    return AuthorityProfile(AuthorityMode.SCOPED_EXECUTION, set(families or ("selection",)), AuthorityLimits(**limits), 2)


def invalid_snapshot() -> DecisionSnapshot:
    return fx.hand_snapshot(snapshot_id="snap-bad", consistency=ConsistencyStatus.SESSION_CHANGED, consistency_reasons=["session changed during collection"], club_id=CLUB)


def candidate_state(pid, name, positions, tier, wage):
    return player_state(fx.other_club_player(pid, name, list(positions), tier, wage), source="scout")


def package(candidate_id, state, *, wage, fee, registration=None, availability=None):
    terms = rc.package_terms("Oxford", weekly_wage=GBP(wage, Period.WEEKLY), wage_start="2024-02-20", wage_end="2027-06-30", source="offer:test", fee=GBP(fee), fee_due="2024-02-20", prefix=f"pkg:{candidate_id}")
    kw = {}
    if registration is not None:
        kw["registration"] = Observed.available_value(registration, "rules", what="registration_feasible")
    if availability is not None:
        kw["availability"] = Observed.available_value(availability, "scouting", what="availability")
    return rc.CandidatePackage(candidate_id, state, "buy", terms, rc.ACCEPTANCE_PLAUSIBLE, "agent indicated interest", target_position="DL", **kw)


def worked_example_providers(**extra) -> pl.Providers:
    """Spec 17.1: A is the better footballer but needs promotion income; B is feasible."""
    a = package("A", candidate_state(2001, "Candidate A", ("DL", "WBL"), 3, 4500), wage=4500, fee=1_500_000, registration=True, availability=True)
    b = package("B", candidate_state(2002, "Candidate B", ("DL", "ML", "DC"), 2, 1500), wage=1500, fee=200_000, registration=True, availability=True)
    promotion = fin.Scenario("promotion", Fraction(1, 2), receipts=[fin.ForecastReceipt("promotion prize money", GBP(3_000_000), "2024-05-30", 1)], flags={"promotion": True})
    stay = fin.Scenario("stay in the division", Fraction(1, 2), stress=True)
    return pl.Providers(finance_policy=fin.RiskPolicy(GBP(6_000_000)), scenarios=[promotion, stay], regulatory=NO_RULES, engine=fin.CashFlowEngine(transfer_windows=()), candidates=[a, b], **extra)


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------

class ProposeTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.planner = pl.Planner(store=self.world.store)
        self.candidates = self.planner.propose(self.world.snapshot)

    def test_every_horizon_is_covered_by_default_providers(self):
        horizons = {c.horizon for c in self.candidates}
        self.assertEqual(horizons, set(pl.HORIZONS))
        kinds = [c.kind for c in self.candidates]
        for kind in ("advise.lineup", "progress.continue", "respond.inbox", "advise.minutes", "advise.finance", "advise.succession"):
            self.assertIn(kind, kinds)
        self.assertNotIn("submit.lineup", kinds, "advise mode never proposes a submission")

    def test_advisory_lineup_is_labelled_unverified_and_not_submittable(self):
        lineup = next(c for c in self.candidates if c.kind == "advise.lineup")
        self.assertEqual(lineup.status, pl.STATUS_ADVISORY)
        self.assertTrue(lineup.unverified)
        self.assertFalse(lineup.submittable)
        self.assertTrue(any("unverified" in r for r in lineup.reasons))
        self.assertEqual(lineup.payload["lineup"]["status"], "advisory_unverified")
        self.assertEqual(len(lineup.claims.minutes), 11)
        self.assertTrue(all(m == pl.MATCH_MINUTES for m in lineup.claims.minutes.values()))
        self.assertIn("Bolton", lineup.description)

    def test_continue_is_blocked_by_mandatory_inbox_and_missing_capabilities(self):
        cont = next(c for c in self.candidates if c.kind == "progress.continue")
        self.assertEqual(cont.status, pl.STATUS_BLOCKED)
        self.assertTrue(any("inbox:501" in r for r in cont.reasons))
        self.assertTrue(any("missing capabilities" in r and "ui_action_adapter" in r for r in cont.reasons))
        self.assertTrue(any("advice only" in r for r in cont.reasons))
        inbox = [c for c in self.candidates if c.kind == "respond.inbox"]
        self.assertEqual({c.parameters["message_id"] for c in inbox}, {501, 503})
        self.assertTrue(all(c.status == pl.STATUS_BLOCKED for c in inbox))
        self.assertTrue(all(any("no language model" in r for r in c.reasons) for c in inbox))

    def test_minutes_finance_and_succession_are_advisory(self):
        minutes = next(c for c in self.candidates if c.kind == "advise.minutes")
        self.assertEqual(minutes.status, pl.STATUS_ADVISORY)
        self.assertEqual(sum(minutes.claims.minutes.values()), 5 * 11 * pl.MATCH_MINUTES)
        finance = next(c for c in self.candidates if c.kind == "advise.finance")
        self.assertEqual(finance.status, pl.STATUS_ADVISORY)
        self.assertEqual(finance.payload["finance_model"]["status"], "partial")
        succession = next(c for c in self.candidates if c.kind == "advise.succession")
        self.assertEqual(succession.horizon, pl.HORIZON_SUCCESSION)
        self.assertTrue(succession.payload["gaps"])
        self.assertTrue(all(c.family for c in self.candidates))

    def test_inconsistent_snapshot_yields_one_blocked_candidate(self):
        blocked = self.planner.propose(invalid_snapshot())
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].status, pl.STATUS_BLOCKED)
        self.assertIn("session_changed", blocked[0].reasons[0])

    def test_module_level_propose_matches_the_planner(self):
        candidates = pl.propose(self.world.snapshot, default_profile())
        self.assertEqual([c.candidate_id for c in candidates], [c.candidate_id for c in self.candidates])


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------

class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def _plan_and_pick(self, kind, **planner_kw):
        planner = pl.Planner(store=self.world.store, **planner_kw)
        candidates = planner.propose(self.world.snapshot)
        candidate = next(c for c in candidates if c.kind == kind)
        return planner, candidate, planner.evaluate(candidate, snapshot=self.world.snapshot)

    def test_lineup_forecasts_stay_unavailable_without_their_inputs(self):
        planner, candidate, report = self._plan_and_pick("advise.lineup")
        result = next(f for f in report.forecasts if f.forecast_target == "points_at_fixture")
        self.assertTrue(result.available, "three played matches feed the rolling points baseline")
        self.assertIsNotNone(result.interval)
        readiness = [f for f in report.forecasts if f.forecast_target.startswith("validated_readiness")]
        self.assertEqual(len(readiness), 11)
        self.assertTrue(all(f.status == "unavailable" for f in readiness))
        self.assertIn("recovery_per_day", readiness[0].reason)
        self.assertIn("result_forecast", report.model_versions)
        self.assertEqual(report.model_versions["lineup"], pl.LINEUP_SOLVER_VERSION)

    def test_readiness_forecasts_need_a_current_condition_and_a_recovery_baseline(self):
        planner, candidate, report = self._plan_and_pick("advise.lineup", providers=pl.Providers(recovery_per_day=1.5))
        readiness = [f for f in report.forecasts if f.forecast_target.startswith("validated_readiness")]
        self.assertTrue(all(f.available for f in readiness))
        self.assertTrue(all(93.0 <= f.point <= 100.0 for f in readiness))
        self.assertEqual(readiness[0].validity_period["days_ahead"], 3)

    def test_components_are_separate_and_a_missing_one_leaves_the_total_unavailable(self):
        planner, candidate, report = self._plan_and_pick("advise.lineup")
        components = report.components
        self.assertTrue(components.components["sporting"].contribution.available)
        self.assertFalse(components.components["risk"].contribution.available, "no reserve policy: downside loss is not estimated")
        self.assertFalse(components.total.available, "a missing component is never zero")
        self.assertIsNotNone(components.sporting_failure)
        names = {c.name for c in report.constraints}
        self.assertIn("eligibility_verified", names)
        self.assertIn("competition_rules", names)
        eligibility = next(c for c in report.constraints if c.name == "eligibility_verified")
        self.assertEqual(eligibility.status, "unknown", "advisory lineup: unverified, not failed")
        self.assertIsNone(report.feasible)
        self.assertTrue(any("heuristic" in n for n in report.notes))

    def test_reserve_policy_makes_the_risk_component_available(self):
        planner, candidate, report = self._plan_and_pick("advise.lineup", authority=AuthorityProfile(limits=AuthorityLimits(min_cash_reserve=GBP(2_000_000))))
        risk = report.components.components["risk"]
        self.assertTrue(risk.contribution.available)
        self.assertTrue(report.components.total.available)

    def test_continue_evaluation_lists_gate_blockers_as_binding(self):
        planner, candidate, report = self._plan_and_pick("progress.continue")
        self.assertIs(report.feasible, False)
        self.assertTrue(report.binding_constraints)
        self.assertTrue(all(c.binding for c in report.constraints if c.name.startswith("blocker:")))
        self.assertTrue(any(c.name.startswith("capability:") for c in report.constraints))
        self.assertEqual(report.forecasts, [])

    def test_minutes_evaluation_covers_every_fixture_and_promise(self):
        planner, candidate, report = self._plan_and_pick("advise.minutes", providers=pl.Providers(promises=self.world.promises()))
        fixtures = [c for c in report.constraints if c.name.startswith("fixture:")]
        self.assertEqual(len(fixtures), 5)
        promise = next(c for c in report.constraints if c.name == "promise:1018")
        self.assertEqual(promise.status, "pass")
        self.assertEqual(len([f for f in report.forecasts if f.forecast_target == "points_at_fixture"]), 5)
        self.assertEqual(report.model_versions["minutes"], pl.MINUTES_PLANNER_VERSION)

    def test_finance_evaluation_reports_observed_inputs_and_unknown_reserve(self):
        planner, candidate, report = self._plan_and_pick("advise.finance")
        reserve = next(c for c in report.constraints if c.name == "cash_reserve")
        self.assertEqual(reserve.status, "unknown")
        observed = {c.name: c.status for c in report.constraints if c.name.startswith("observed:")}
        self.assertEqual(set(observed.values()), {"pass"})
        self.assertIsNone(report.feasible)
        self.assertEqual(report.model_versions["finance"], fin.FINANCE_POLICY_VERSION)

    def test_recruit_evaluation_checks_the_promise_ledger_before_recruitment(self):
        """Spec 11.3: the ledger is checked before recruitment, so a promise the signing competes with becomes a constraint on that recruit."""
        ledger = PromiseLedger(self.world.store, self.world.branch.branch_id)
        competing, competing_terms = promise_from_observed(1018, "player", "You will be a regular starter at DL", source="obs:choice-1")
        ledger.record(competing, competing_terms)
        elsewhere, elsewhere_terms = promise_from_observed(1019, "player", "You will be a regular starter at GK", source="obs:choice-2")
        ledger.record(elsewhere, elsewhere_terms)
        self.assertEqual((competing_terms.position, elsewhere_terms.position), ("DL", "GK"))
        recruit = package("A", candidate_state(2001, "Candidate A", ("DL", "WBL"), 3, 3000), wage=3000, fee=100_000, registration=True, availability=True)
        self.assertEqual(recruit.target_position, "DL")
        providers = pl.Providers(finance_policy=fin.RiskPolicy(GBP(2_000_000)), regulatory=NO_RULES, engine=fin.CashFlowEngine(transfer_windows=()), candidates=[recruit], promises=ledger)
        planner = pl.Planner(store=self.world.store, providers=providers)
        candidate = next(c for c in planner.propose(self.world.snapshot) if c.candidate_id == "recruit:A")
        report = planner.evaluate(candidate, snapshot=self.world.snapshot)
        promises = {c.name: c for c in report.constraints if c.name.startswith("promise:")}
        self.assertEqual(list(promises), [f"promise:{competing.promise_id}"], "only the promise the DL signing competes with is reported")
        conflict = promises[f"promise:{competing.promise_id}"]
        self.assertEqual((conflict.status, conflict.binding, conflict.source), ("unknown", False, "promises"))
        self.assertIn("competes with the starting_role promise to 1018", conflict.reason)
        self.assertNotIn(f"promise:{elsewhere.promise_id}", {c.name for c in report.constraints})

    def test_recruit_evaluation_reports_no_promise_constraint_when_the_ledger_is_empty(self):
        """Spec 11.3: a ledger with no conflicting promise adds no constraint, so the check is not a blanket warning."""
        empty = PromiseLedger(self.world.store, self.world.branch.branch_id)
        recruit = package("A", candidate_state(2001, "Candidate A", ("DL", "WBL"), 3, 3000), wage=3000, fee=100_000, registration=True, availability=True)
        providers = pl.Providers(finance_policy=fin.RiskPolicy(GBP(2_000_000)), regulatory=NO_RULES, engine=fin.CashFlowEngine(transfer_windows=()), candidates=[recruit], promises=empty)
        planner = pl.Planner(store=self.world.store, providers=providers)
        candidate = next(c for c in planner.propose(self.world.snapshot) if c.candidate_id == "recruit:A")
        report = planner.evaluate(candidate, snapshot=self.world.snapshot)
        self.assertEqual([c.name for c in report.constraints if c.name.startswith("promise:")], [])

    def test_evaluate_without_a_snapshot_fails_closed(self):
        planner = pl.Planner()
        report = planner.evaluate(pl.CandidateDecision("x", "advise.lineup", pl.HORIZON_NEXT_DECISION, "", pl.STATUS_ADVISORY))
        self.assertIs(report.feasible, False)
        self.assertEqual(report.binding_constraints, ["snapshot"])


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------

class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def _shared(self, providers=None, **planner_kw):
        planner = pl.Planner(store=self.world.store, providers=providers, **planner_kw)
        candidates = planner.propose(self.world.snapshot)
        evaluations = {c.candidate_id: planner.evaluate(c, snapshot=self.world.snapshot) for c in candidates}
        return planner, candidates, evaluations, planner.reconcile(candidates, evaluations, snapshot=self.world.snapshot)

    def test_wage_headroom_is_one_pool_shared_across_recruits(self):
        a = package("A", candidate_state(2001, "Candidate A", ("DL", "WBL"), 3, 3000), wage=3000, fee=100_000, registration=True, availability=True)
        b = package("B", candidate_state(2002, "Candidate B", ("DL", "ML"), 2, 2500), wage=2500, fee=100_000, registration=True, availability=True)
        providers = pl.Providers(finance_policy=fin.RiskPolicy(GBP(2_000_000)), regulatory=NO_RULES, engine=fin.CashFlowEngine(transfer_windows=()), candidates=[a, b])
        planner, candidates, evaluations, shared = self._shared(providers)
        self.assertEqual(shared.money["wage_headroom_weekly"], str(GBP(78979 - 74250, Period.WEEKLY)))
        statuses = {cid: alloc["status"] for cid, alloc in shared.allocations.items()}
        self.assertEqual(sorted(statuses.values()), ["allocated", "deferred"], "each recruit fits alone; together they exceed the shared headroom")
        deferred = next(cid for cid, s in statuses.items() if s == "deferred")
        self.assertTrue(any("exceeds the remaining shared headroom" in r for r in shared.allocations[deferred]["reasons"]))
        self.assertEqual(len(shared.conflicts), 1)
        self.assertEqual(shared.money["allocation_order"][0], "recruit:A", "the larger plan gain is allocated first")
        self.assertEqual(shared.squad_places["recruits_allocated"], 1)
        self.assertIn("unknown", shared.squad_places["status"], "squad size limit needs a rules profile")

    def test_promised_minutes_consume_the_minutes_plan(self):
        planner, candidates, evaluations, shared = self._shared(pl.Providers(promises=self.world.promises()))
        self.assertEqual(shared.minutes["promised_minutes"], {"1018": 350})
        self.assertEqual(shared.minutes["unmet_promises"], {})
        minutes = next(c for c in candidates if c.kind == "advise.minutes")
        self.assertGreaterEqual(minutes.claims.minutes.get(1018, 0), 350, "the plan honours the reservation")
        self.assertIn("all planned", minutes.description)

    def test_eligibility_is_reconciled_as_one_shared_fact(self):
        planner, candidates, evaluations, shared = self._shared()
        eligibility = shared.eligibility
        self.assertEqual(eligibility["lineup_status"], "unverified")
        self.assertEqual(len(eligibility["unverified"]), 24)
        self.assertEqual(eligibility["verified"], [])
        depending = eligibility["candidates_depending_on_unverified"]
        self.assertEqual([d["candidate_id"] for d in depending], ["lineup:advisory"])
        self.assertEqual(len(depending[0]["unverified_or_ineligible"]), 11)

    def test_deadlines_and_priorities_are_listed(self):
        planner, candidates, evaluations, shared = self._shared()
        kinds = {d["kind"] for d in shared.deadlines}
        self.assertIn("pending_action", kinds)
        self.assertTrue(any(k.startswith("boundary:") for k in kinds))
        self.assertEqual(len(shared.priorities), 5)
        self.assertTrue(all(p["note"] is not None for p in shared.priorities.values()), "the default profile sets no competition priorities")
        json.dumps(shared.to_json())


# ---------------------------------------------------------------------------
# records, capability reports and intents
# ---------------------------------------------------------------------------

class DecisionRecordTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def test_one_decision_per_horizon_is_stored(self):
        report = pl.plan_once(self.world.snapshot, store=self.world.store)
        self.assertEqual([d.kind for d in report.decisions], [f"plan.{h}" for h in pl.HORIZONS])
        stored = {d.decision_id: d for d in self.world.store.list_decisions()}
        self.assertEqual(set(stored), {d.decision_id for d in report.decisions})
        first = stored[report.decisions[0].decision_id]
        self.assertEqual(first.snapshot_id, self.world.snapshot.snapshot_id)
        self.assertEqual(first.selected["kind"], "advise.lineup")
        self.assertEqual(first.objective_version, default_profile().version)
        self.assertIn("planner", first.model_versions)
        self.assertTrue(first.constraints)
        self.assertTrue(first.forecasts)
        self.assertTrue(all("candidate_id" in c for c in first.constraints))
        self.assertIsNotNone(self.world.store.get_snapshot(self.world.snapshot.snapshot_id))

    def test_rolling_horizon_selects_nothing_and_carries_the_shared_plan(self):
        report = pl.plan_once(self.world.snapshot, store=self.world.store, providers=worked_example_providers())
        rolling = next(d for d in report.decisions if d.kind == f"plan.{pl.HORIZON_ROLLING_12_MONTHS}")
        self.assertIsNone(rolling.selected)
        self.assertTrue(any("negotiated, confirmed offer" in r for r in rolling.reasons))
        self.assertIsNone(rolling.components["tradeoffs"]["ranking"])
        self.assertIn("allocations", rolling.components["shared_plan"])
        succession = next(d for d in report.decisions if d.kind == f"plan.{pl.HORIZON_SUCCESSION}")
        self.assertIsNone(succession.selected)
        self.assertIn("gap list only", succession.reasons[0])

    def test_capability_reports_block_only_the_families_that_need_a_missing_subsystem(self):
        planner = pl.Planner(store=self.world.store)
        reports = planner.capability_reports(self.world.snapshot)
        self.assertEqual(set(reports), set(pl.GATED_FAMILIES))
        self.assertTrue(all(r.blocked for r in reports.values()), "nothing bot-side is provided by default")
        self.assertIn("ui_action_adapter", reports["selection"].missing)
        self.assertIn("eligibility_injury", reports["selection"].missing)
        self.assertIn("contract_cash_flows", reports["contracts"].missing)
        provided = pl.Planner(store=self.world.store, capabilities=self.world.full_registry("submit.lineup"))
        reports = provided.capability_reports(self.world.snapshot)
        self.assertFalse(reports["selection"].blocked)
        self.assertTrue(reports["contracts"].blocked, "providing the selection prerequisites unblocks nothing else")


class IntentTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def test_advise_mode_creates_no_intents_and_records_why(self):
        report = pl.plan_once(self.world.snapshot, store=self.world.store)
        self.assertTrue(report.intents)
        self.assertTrue(all(not i.created for i in report.intents))
        self.assertEqual(self.world.store.list_intents(), [])
        self.assertNotIn("advise.lineup", [i.kind for i in report.intents], "advice is never an intent")

    def test_unverified_lineup_is_never_submitted_even_with_capabilities_and_authority(self):
        report = pl.plan_once(self.world.snapshot, store=self.world.store, authority=scoped("selection"), capabilities=self.world.full_registry("submit.lineup"), providers=pl.Providers(rules=self.world.rules()))
        submit = report.candidate("submit.lineup")
        self.assertEqual(submit.status, pl.STATUS_BLOCKED)
        self.assertTrue(submit.unverified)
        self.assertFalse(submit.submittable)
        advisory = report.candidate("advise.lineup")
        self.assertTrue(advisory.unverified)
        outcome = next(i for i in report.intents if i.kind == "submit.lineup")
        self.assertFalse(outcome.created)
        self.assertEqual(self.world.store.list_intents(), [])
        self.assertEqual(report.shared_plan.eligibility["lineup_status"], "unverified")

    def test_verified_lineup_with_capabilities_becomes_a_validated_intent(self):
        report = pl.plan_once(self.world.snapshot, store=self.world.store, authority=scoped("selection"), capabilities=self.world.full_registry("submit.lineup"), providers=pl.Providers(eligibility=self.world.verified_eligibility(), rules=self.world.rules()))
        submit = report.candidate("submit.lineup")
        self.assertEqual(submit.status, pl.STATUS_PROPOSED)
        self.assertTrue(submit.submittable)
        self.assertFalse(submit.unverified)
        outcome = next(i for i in report.intents if i.kind == "submit.lineup")
        self.assertTrue(outcome.created)
        self.assertEqual(outcome.state, ActionState.VALIDATED.value)
        stored = self.world.store.get_intent(outcome.action_id)
        self.assertIs(stored.state, ActionState.VALIDATED)
        self.assertEqual(stored.authority_scope, "selection.submit_lineup")
        self.assertEqual(stored.verification, "lineup_matches_selection")
        self.assertEqual(len(stored.parameters["player_ids"]), 11)
        self.assertEqual(stored.decision_snapshot_id, self.world.snapshot.snapshot_id)
        self.assertEqual(report.shared_plan.eligibility["lineup_status"], "verified")
        self.assertTrue(any("VALIDATED" in line for line in report.summaries))

    def test_verified_lineup_outside_the_enabled_families_is_outside_scope(self):
        report = pl.plan_once(self.world.snapshot, store=self.world.store, authority=scoped("training"), capabilities=self.world.full_registry("submit.lineup"), providers=pl.Providers(eligibility=self.world.verified_eligibility(), rules=self.world.rules()))
        outcome = next(i for i in report.intents if i.kind == "submit.lineup")
        self.assertTrue(outcome.created)
        self.assertEqual(outcome.state, ActionState.OUTSIDE_SCOPE.value)
        self.assertIn("selection", outcome.reason)

    def test_missing_capability_blocks_only_the_action_that_needs_it(self):
        registry = self.world.full_registry("submit.lineup")
        registry.withdraw("ui_action_adapter", "adapter lost focus")
        report = pl.plan_once(self.world.snapshot, store=self.world.store, authority=scoped("selection"), capabilities=registry, providers=pl.Providers(eligibility=self.world.verified_eligibility(), rules=self.world.rules()))
        submit = report.candidate("submit.lineup")
        self.assertEqual(submit.status, pl.STATUS_BLOCKED)
        self.assertTrue(any("ui_action_adapter" in r for r in submit.reasons))
        self.assertEqual(report.candidate("advise.lineup").status, pl.STATUS_ADVISORY, "advice needs no adapter")
        self.assertEqual(self.world.store.list_intents(), [])

    def _verified_kw(self) -> dict:
        return dict(store=self.world.store, authority=scoped("selection"), capabilities=self.world.full_registry("submit.lineup"), providers=pl.Providers(eligibility=self.world.verified_eligibility(), rules=self.world.rules()))

    def test_lineup_roles_are_a_dict_keyed_by_slot_position_that_the_workflow_and_verifier_accept(self):
        """spec 12.2: one lineup parameter contract. ``parameters["roles"]`` maps each slot's position code to its role name, which is
        what the ``set_lineup`` workflow sends and what ``lineup_matches_selection`` reads back; a list of role names would have
        broken the consequential input after it was sent."""
        report = pl.plan_once(self.world.snapshot, **self._verified_kw())
        submit = report.candidate("submit.lineup")
        self.assertEqual(submit.status, pl.STATUS_PROPOSED)
        roles = submit.parameters["roles"]
        self.assertIsInstance(roles, dict)
        self.assertEqual(list(roles), [s["position"] for s in fx.tactics_payload()["positions"]], "keyed by slot position code, in slot order")
        self.assertTrue(all(isinstance(v, str) for v in roles.values()), roles)
        self.assertEqual(len(submit.parameters["player_ids"]), 11)
        advisory = report.candidate("advise.lineup")
        self.assertIsInstance(advisory.parameters["roles"], dict)
        self.assertEqual(list(advisory.parameters["roles"]), list(roles), "the advisory eleven uses the same contract")
        stored = self.world.store.get_intent(next(i.action_id for i in report.intents if i.kind == "submit.lineup"))
        self.assertEqual(stored.parameters["roles"], roles)
        # the fake UI workflow accepts the parameters as they are, and the verifier's readback equals them
        steps = FAKE_WORKFLOWS["submit.lineup"].instantiate(stored.parameters)
        adapter = FakeAdapter(screen="squad.selection")
        result = adapter.perform(steps[1])
        self.assertTrue(result.ok, result.error)
        self.assertEqual(adapter.readback("lineup").require(), {"player_ids": stored.parameters["player_ids"], "roles": roles})
        verdict = verify_lineup(stored, Evidence(), Evidence(), adapter)
        self.assertIs(verdict.kind, VerdictKind.CONFIRMED, verdict.reasons)

    def test_act02_duplicate_effect_guard_refuses_a_new_lineup_intent_while_a_twin_is_unsettled(self):
        """ACT 02 / spec 12.3: a later plan on the same store (a new decision, hence a new idempotency key) does not mint another
        ``submit.lineup`` intent while the earlier one is UNCERTAIN or RECONCILING; the outcome names that intent so reconciliation,
        never a second dispatch, follows. Once the earlier intent is settled a new decision may mint again."""
        kw = self._verified_kw()
        first = pl.plan_once(self.world.snapshot, **kw)
        action_id = next(i.action_id for i in first.intents if i.kind == "submit.lineup")
        intent = self.world.store.get_intent(action_id)
        for state in (ActionState.QUEUED, ActionState.EXECUTING, ActionState.UNCERTAIN):
            self.world.store.update_intent_state(intent, state, "test: consequential input timed out after it went out")
        second = pl.plan_once(self.world.snapshot, **kw)
        outcome = next(i for i in second.intents if i.kind == "submit.lineup")
        self.assertFalse(outcome.created)
        self.assertIn("duplicate-effect guard", outcome.reason)
        self.assertIn(action_id, outcome.reason)
        self.assertIn("UNCERTAIN", outcome.reason)
        self.assertEqual([i.action_id for i in self.world.store.list_intents()], [action_id], "no second intent exists")
        self.world.store.update_intent_state(intent, ActionState.RECONCILING, "test")
        third = pl.plan_once(self.world.snapshot, **kw)
        self.assertFalse(next(i for i in third.intents if i.kind == "submit.lineup").created, "RECONCILING is still unsettled")
        self.world.store.update_intent_state(intent, ActionState.CONFIRMED, "test: effect established by readback")
        fourth = pl.plan_once(self.world.snapshot, **kw)
        self.assertTrue(next(i for i in fourth.intents if i.kind == "submit.lineup").created, "a settled effect no longer blocks a new decision")
        self.assertEqual(len(self.world.store.list_intents()), 2)


class ContinueIntentTests(unittest.TestCase):
    """The Continue the planner mints is judged by its effect on the game, never by where the UI ended up (spec 12.2, 12.4).

    The calendar here is clear: the only inbox item left is a read,
    non-mandatory one, every starter is verified eligible and the
    ``progress.continue`` capabilities are provided, so the gate opens and
    the candidate is proposed for the single UI writer.
    """

    CLEAR_INBOX = {"messages": [m for m in fx.inbox_payload()["messages"] if m["id"] == 502], "unread_count": 0, "scope": "current_human_inbox"}

    def setUp(self):
        self.world = World(overrides={"/inbox": FakeTransport.envelope(self.CLEAR_INBOX, session_id=fx.SESSION)})

    def _plan(self):
        return pl.plan_once(
            self.world.snapshot, store=self.world.store,
            authority=scoped("progression", "selection", allow_continue=True),
            capabilities=self.world.full_registry("progress.continue", "submit.lineup"),
            providers=pl.Providers(eligibility=self.world.verified_eligibility(), rules=self.world.rules()),
        )

    def test_cal01_a_planner_minted_continue_is_confirmed_by_the_game_advancing(self):
        """CAL 01 / spec 12.2, 12.4: a Continue intent minted by ``plan_once`` alone (no orchestrator overriding the table) carries
        the in-game moment it moves on from and the boundary it expects, and ``game_advanced_past_boundary`` confirms it from the
        in-game clock of a fresh consistent snapshot. Judged by a changed screen it could never have been confirmed at all."""
        report = self._plan()
        cont = report.candidate("progress.continue")
        self.assertEqual(cont.status, pl.STATUS_PROPOSED, cont.reasons)
        outcome = next(i for i in report.intents if i.kind == "progress.continue")
        self.assertTrue(outcome.created, outcome.reason)
        self.assertEqual(outcome.state, ActionState.VALIDATED.value)
        intent = self.world.store.get_intent(outcome.action_id)
        self.assertEqual(intent.verification, "game_advanced_past_boundary")
        self.assertNotIn("target", intent.parameters, "a screen target would claim a screen proves the calendar moved")
        self.assertNotIn("expected_screen", intent.parameters)
        self.assertEqual((intent.parameters["from_game_date"], intent.parameters["from_game_time"]), (fx.GAME_DATE, fx.GAME_TIME))
        self.assertEqual(intent.parameters["expected_boundary"]["date"], "2024-02-20", intent.parameters["expected_boundary"])
        # The game moves on; only the intent's own recorded moment is available to compare against (empty ``before`` evidence).
        moved = fx.hand_snapshot(snapshot_id="snap-after", game_date="2024-02-20", game_time="21:30")
        verdict = verify(intent, Evidence(), Evidence(snapshot=moved), FakeAdapter())
        self.assertIs(verdict.kind, VerdictKind.CONFIRMED, verdict.reasons)
        self.assertEqual(verdict.plan, "game_advanced_past_boundary")
        effect = verdict.details["effect"]
        self.assertEqual(effect["from"], {"game_date": fx.GAME_DATE, "game_time": fx.GAME_TIME})
        self.assertEqual((effect["game_date"], effect["game_time"]), ("2024-02-20", "21:30"))
        self.assertIs(effect["reached_expected_boundary"], True)
        self.assertTrue(all(r["source"].startswith("bridge:/game@") for r in verdict.readbacks), verdict.readbacks)

    def test_cal01_a_calendar_that_did_not_move_fails_the_same_intent(self):
        """CAL 01 / spec 12.2: the same planner-minted intent is FAILED - never CONFIRMED, never retried - when the fresh snapshot's
        in-game clock still reads the moment the intent recorded, and UNCERTAIN when no fresh clock reading exists at all."""
        intent = self.world.store.get_intent(next(i.action_id for i in self._plan().intents if i.kind == "progress.continue"))
        standstill = verify(intent, Evidence(), Evidence(snapshot=fx.hand_snapshot(snapshot_id="snap-same")), FakeAdapter())
        self.assertIs(standstill.kind, VerdictKind.FAILED, standstill.reasons)
        self.assertTrue(any("did not move on" in r for r in standstill.reasons), standstill.reasons)
        blind = verify(intent, Evidence(), Evidence(), FakeAdapter())
        self.assertIs(blind.kind, VerdictKind.UNCERTAIN, blind.reasons)

    def test_continue_and_inbox_answers_are_verified_by_effect_not_by_navigation(self):
        """spec 12.2 (CAL 01, AUD 01): every plan the planner names is a real plan in ``execution.verification``, and neither
        Continue nor an inbox answer is judged by ``navigation_only`` - a changed screen is not evidence of either effect."""
        self.assertLessEqual(set(pl.VERIFICATION_PLANS.values()), set(PLANS), "a plan name with no plan behind it can never be confirmed")
        self.assertEqual(pl.VERIFICATION_PLANS["progress.continue"], "game_advanced_past_boundary")
        self.assertEqual(pl.VERIFICATION_PLANS["respond.inbox"], "inbox_message_answered")
        self.assertNotIn("navigation_only", set(pl.VERIFICATION_PLANS.values()))


# ---------------------------------------------------------------------------
# finance model, succession, worked example and the report
# ---------------------------------------------------------------------------

class FinanceModelTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def test_partial_model_reports_observed_committed_and_unknown(self):
        model = pl.Planner(store=self.world.store).finance_model(self.world.snapshot)
        self.assertEqual(model.status, "partial")
        self.assertEqual(model.view["balance"]["value"], GBP(10_909_005).to_json())
        self.assertEqual(model.payroll["label"], "unexplained")
        self.assertEqual(Money.from_json(model.payroll["residual"]), GBP(-2400, Period.WEEKLY), "staff wages outside the bridge aggregate are shown, not hidden")
        self.assertEqual(model.committed_projection["scenario"], "committed_only")
        self.assertEqual(model.committed_projection["min_cash"], str(GBP(6_973_755)))
        self.assertIsNone(model.reserve_check)
        self.assertIn("contract_clauses", model.unresolved)
        self.assertTrue(any("reserve rule unknown" in n for n in model.notes))
        json.dumps(model.to_json())

    def test_authority_reserve_derives_a_labelled_policy(self):
        planner = pl.Planner(store=self.world.store, authority=AuthorityProfile(limits=AuthorityLimits(min_cash_reserve=GBP(8_000_000))))
        model = planner.finance_model(self.world.snapshot)
        self.assertEqual(model.reserve_check["status"], "fail")
        self.assertEqual(model.reserve_check["policy"], "derived_from_authority_min_cash_reserve")
        self.assertEqual(model.reserve_check["cvar_shortfall"], str(GBP(8_000_000 - 6_973_755)))

    def test_missing_balance_yields_no_projection(self):
        routes = dict(self.world.snapshot.routes)
        routes["/finances"] = {**fx.finances_payload(), "balance": None}
        snap = fx.hand_snapshot(routes, snapshot_id="snap-null", capabilities=self.world.snapshot.capabilities, unresolved=self.world.snapshot.unresolved, club_id=CLUB)
        model = pl.Planner().finance_model(snap)
        self.assertIsNone(model.committed_projection)
        self.assertEqual(model.view["balance"]["status"], "null")
        self.assertTrue(any("no projection possible" in n for n in model.notes))

    def test_finance_summary_is_scoped_to_committed_obligations_and_the_undecoded_sources_are_named(self):
        """The one-line finance headline never claims a full forecast, and the undecoded finance sources are named on the decision view (spec 8.1, 15.1)."""
        for authority in (None, AuthorityProfile(limits=AuthorityLimits(min_cash_reserve=GBP(8_000_000)))):
            with self.subTest(reserve_policy=authority is not None):
                planner = pl.Planner(store=self.world.store, authority=authority) if authority else pl.Planner(store=self.world.store)
                report = planner.plan(self.world.snapshot)
                line = next(s for s in report.summaries if s.startswith("Finances:"))
                self.assertIn("on committed obligations alone", line, "the headline scopes itself: it is not a complete forecast")
                self.assertIn("0 unknown item(s)", line)
                model = planner.finance_model(self.world.snapshot)
                self.assertEqual(model.unknown_items, 0, "an 8.1 classification count of the observed ledger, not a claim of completeness")
                self.assertIn("bridge does not decode: contract_clauses, transfer_target_terms, finance_breakdowns, scouting_budget, debts", model.notes)
                finance = next(c for c in report.candidates if c.kind == "advise.finance")
                self.assertTrue(any("bridge does not decode" in r for r in finance.reasons), "the undecoded sources travel with the finance candidate")
                rolling = next(d for d in report.decisions if d.kind == "plan.rolling_12_months")
                self.assertIn("bridge does not decode", json.dumps(rolling.to_json(), default=str), "the decision view names what the bridge cannot decode")


class FinanceWindowTests(unittest.TestCase):
    """The rolling projection window is placed in game time or not at all (spec 5.2: in-game time is the clock for game commitments)."""

    def setUp(self):
        self.world = World()
        a = package("A", candidate_state(2001, "Candidate A", ("DL", "WBL"), 3, 4500), wage=4500, fee=1_500_000, registration=True, availability=True)
        self.providers = pl.Providers(candidates=[a])      # no reserve policy: no FinanceInputs, so the planner places the window itself

    def test_window_starts_at_the_snapshot_game_date(self):
        planner = pl.Planner(providers=self.providers)
        recruit = next(c for c in planner.propose(self.world.snapshot) if c.candidate_id == "recruit:A")
        self.assertEqual(recruit.claims.guaranteed_fees, GBP(1_500_000))
        window = recruit.payload["finance_window"]
        self.assertEqual(window["status"], "available")
        self.assertEqual(window["value"][0], fx.GAME_DATE)
        self.assertNotIn("finance_window", [c.name for c in planner.evaluate(recruit, snapshot=self.world.snapshot).constraints])

    def test_missing_game_date_leaves_the_finance_window_unavailable_not_an_epoch_default(self):
        """spec 5.2 / OBS 02: without a game date the window is explicitly missing: no guaranteed-fee projection, the finance-window
        constraint is unknown (not binding, not passed), and no 1970 sentinel appears anywhere in the candidate or its evaluation."""
        snap = replace(self.world.snapshot, snapshot_id="snap-no-date", game_date=None, game_time=None)
        planner = pl.Planner(providers=self.providers)
        recruit = next(c for c in planner.propose(snap) if c.candidate_id == "recruit:A")
        self.assertIsNone(recruit.claims.guaranteed_fees, "fees over an unplaceable window are not projected")
        self.assertIsNone(recruit.parameters["total_fee"])
        self.assertEqual(recruit.payload["finance_window"]["status"], "missing")
        self.assertTrue(any("finance window missing" in r and "not projected" in r for r in recruit.reasons), recruit.reasons)
        evaluation = planner.evaluate(recruit, snapshot=snap)
        window = next(c for c in evaluation.constraints if c.name == "finance_window")
        self.assertEqual(window.status, "unknown")
        self.assertFalse(window.binding)
        self.assertIsNot(evaluation.feasible, True, "an unknown window never makes a package feasible")
        for text in (json.dumps(recruit.to_json()), json.dumps(evaluation.to_json())):
            self.assertNotIn("1970", text)


class WorkedExampleTests(unittest.TestCase):
    """Spec 17.1 through the shared planner: tradeoffs, scenario results, coverage, acceptance, allocation."""

    def setUp(self):
        self.world = World()
        self.report = pl.plan_once(self.world.snapshot, store=self.world.store, providers=worked_example_providers())

    def test_candidate_a_is_infeasible_under_the_reserve_policy_and_b_feasible(self):
        a, b = self.report.evaluations["recruit:A"], self.report.evaluations["recruit:B"]
        self.assertIs(a.feasible, False)
        self.assertEqual(a.binding_constraints, ["cash_reserve"])
        self.assertEqual(a.package_evaluation.feasibility.risk.pass_rate, Fraction(1, 2), "only the promotion path clears the reserve")
        self.assertIs(b.feasible, True)
        self.assertEqual(b.binding_constraints, [])
        self.assertGreater(a.package_evaluation.contribution.change("horizon_lineup_objective_sum"), b.package_evaluation.contribution.change("horizon_lineup_objective_sum"))
        self.assertEqual(a.scenarios, ["promotion", "stay in the division"])
        self.assertTrue(any("not a market price" in n for n in a.notes))

    def test_shared_plan_allocates_b_and_marks_a_infeasible(self):
        allocations = self.report.shared_plan.allocations
        self.assertEqual(allocations["recruit:A"]["status"], "infeasible")
        self.assertEqual(allocations["recruit:B"]["status"], "allocated")
        self.assertEqual(self.report.tradeoffs.undominated, ["B"])
        self.assertIsNone(self.report.tradeoffs.ranking)
        recruits = [c for c in self.report.candidates if c.package is not None]
        self.assertTrue(all(c.status == pl.STATUS_ADVISORY for c in recruits))
        self.assertTrue(all(any("confirmed executable offer" in r for r in c.reasons) for c in recruits))
        self.assertTrue(all(not i.created for i in self.report.intents if i.kind == "commit.transfer_offer"))

    def test_summary_speaks_football_and_names_the_binding_constraint(self):
        line = next(s for s in self.report.summaries if s.startswith("Recruitment tradeoffs"))
        self.assertIn("no ranking", line)
        self.assertIn("Candidate A (buy) infeasible: cash_reserve", line)
        self.assertIn("Candidate B (buy) feasible", line)
        self.assertIn("Wage headroom", line)

    def test_report_is_json_serialisable(self):
        json.dumps(self.report.to_json())


class PlanOnceTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def test_plan_once_returns_a_full_report_with_football_summaries(self):
        report = pl.plan_once(self.world.snapshot, store=self.world.store)
        self.assertEqual(report.status, "planned")
        self.assertEqual(report.game_date, fx.GAME_DATE)
        self.assertEqual(len(report.decisions), 4)
        self.assertEqual(len(report.evaluations), len(report.candidates))
        self.assertIsNotNone(report.continue_gate)
        self.assertFalse(report.continue_gate.allowed)
        self.assertIsNone(report.tradeoffs, "no candidate packages: nothing to trade off")
        self.assertEqual(sorted({g.position for g in report.succession_gaps}), ["DC", "DL", "DR", "GK", "MC", "ML", "MR", "ST"])
        text = "\n".join(report.summaries)
        for phrase in ("Next match:", "Not for submission", "Minutes plan", "Finances:", "Succession gaps", "Continue: blocked", "Actions:"):
            self.assertIn(phrase, text)
        self.assertEqual(report.model_versions["planner"], pl.PLANNER_VERSION)
        json.dumps(report.to_json())

    def test_inconsistent_snapshot_gives_a_blocked_report(self):
        report = pl.plan_once(invalid_snapshot(), store=self.world.store)
        self.assertEqual(report.status, "blocked")
        self.assertEqual(len(report.candidates), 1)
        self.assertEqual(report.decisions, [])
        self.assertTrue(report.capability_reports)
        self.assertEqual(self.world.store.list_decisions(), [])

    def test_model_registry_fallback_is_recorded(self):
        report = pl.plan_once(self.world.snapshot, store=self.world.store, providers=pl.Providers(models=ModelRegistry(self.world.store)))
        self.assertIn("fallback to baseline", report.model_versions["result_forecast.resolution"])
        self.assertEqual(report.model_versions["result_forecast"], pl.BASELINE_VERSION)

    def test_module_level_evaluate_uses_the_proposing_planner(self):
        planner = pl.Planner(store=self.world.store)
        candidate = next(c for c in planner.propose(self.world.snapshot) if c.kind == "advise.minutes")
        report = pl.evaluate(candidate, None, planner=planner, snapshot=self.world.snapshot)
        self.assertEqual(report.candidate_id, "minutes")


if __name__ == "__main__":
    unittest.main()
