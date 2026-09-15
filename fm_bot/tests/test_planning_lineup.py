"""Tests for fm_bot.planning.lineup (spec 7.1, 7.3; SEL 01, SEL 02; BOT 008)."""
from __future__ import annotations

import itertools
import random
import unittest

from ..planning import lineup as lu
from ..planning.roles import FLAG_ROLE_FALLBACK
from ..state.records import CompetitionContext, ConsistencyStatus, DecisionSnapshot
from ..state.status import Observed, ValueStatus
from ..state.views import fixture_views, player_state, tactic_view
from . import fixtures as fx


def players(**kw):
    return [player_state(fx.player_payload(*spec, **kw), source="test") for spec in fx.SQUAD_SPEC]


def snapshot():
    return DecisionSnapshot("snap-hand", [], ConsistencyStatus.CONSISTENT, [], [], [], {}, "bridge_observed", "c", "b", fx.SESSION, fx.GAME_DATE, fx.GAME_TIME, routes={"/tactics": fx.tactics_payload(), "/fixtures": fx.fixtures_payload()}, manager_id=90001, club_id=742)


def slots():
    return lu.slots_from_tactic_view(tactic_view(snapshot())).require()


def next_fixture():
    return [f for f in fixture_views(snapshot()) if f.scheduled][0]


def verified(ids, false_ids=(), unavailable_ids=()):
    result = {pid: Observed.available_value(True, "ui:squad", what="verified_eligible") for pid in ids}
    for pid in false_ids:
        result[pid] = Observed(False, ValueStatus.AVAILABLE, "ui:squad", None, None, "injury: hamstring", "verified_eligible")
    for pid in unavailable_ids:
        result[pid] = Observed.unavailable(ValueStatus.MISSING, "verified_eligible", "no eligibility observation", "none")
    return result


def request(squad=None, mode="advisory", **kw):
    squad = squad or players()
    kw.setdefault("eligibility", verified([p.player_id for p in squad]))
    return lu.LineupRequest(squad, slots(), next_fixture(), mode=mode, **kw)


def objective(plan):
    return sum(a.score for a in plan.assignments)


class SlotTests(unittest.TestCase):
    def test_slots_come_from_the_decoded_tactic(self):
        found = slots()
        self.assertEqual(len(found), 11)
        self.assertEqual(found[0], lu.RoleSlot(0, "GK", "Goalkeeper", "Defend"))
        self.assertEqual(found[2].position, "DCR")

    def test_undecoded_tactic_gives_unavailable_slots(self):
        self.assertIs(lu.slots_from_tactic_view({"status": "unavailable", "reason": "no tactic"}).status, ValueStatus.UNSUPPORTED)
        self.assertIs(lu.slots_from_tactic_view({"status": "missing"}).status, ValueStatus.MISSING)


class HungarianTests(unittest.TestCase):
    def test_matches_brute_force_on_random_matrices(self):
        rng = random.Random(7)
        for _ in range(40):
            n, m = rng.randint(1, 4), rng.randint(1, 5)
            n = min(n, m)
            cost = [[rng.randint(0, 20) for _ in range(m)] for _ in range(n)]
            best = min(sum(cost[i][perm[i]] for i in range(n)) for perm in itertools.permutations(range(m), n))
            cols = lu.hungarian(cost)
            self.assertEqual(len(set(cols)), n)
            self.assertEqual(sum(cost[i][cols[i]] for i in range(n)), best)

    def test_rectangular_requires_padding(self):
        with self.assertRaises(ValueError):
            lu.hungarian([[1], [2]])
        self.assertEqual(lu.hungarian([]), [])


class LegalLineupTests(unittest.TestCase):
    def test_full_verified_squad_gives_a_legal_exact_eleven(self):
        plan = lu.solve(request(mode="submit", bench_size=Observed.available_value(7, "rules", what="bench_size"), substitutes_allowed=Observed.available_value(5, "rules", what="substitutes_allowed")))
        self.assertEqual(plan.status, "legal")
        self.assertTrue(plan.submittable)
        self.assertEqual(len(plan.assignments), 11)
        self.assertEqual(len({a.player_id for a in plan.assignments}), 11)
        self.assertEqual(sorted(a.slot for a in plan.assignments), list(range(11)))
        self.assertEqual(plan.solver["method"], "hungarian_exact")
        self.assertTrue(plan.solver["independent_check"]["ok"])
        keeper = next(a for a in plan.assignments if a.position == "GK")
        self.assertIn(keeper.player_id, (1001, 1002))
        self.assertIn(keeper.familiarity, ("natural", "accomplished", "competent"))
        self.assertAlmostEqual(plan.objective_value, objective(plan))
        self.assertEqual(plan.bench_status, "available")
        self.assertEqual(len(plan.bench), 7)
        self.assertFalse({b.player_id for b in plan.bench} & set(plan.player_ids))
        self.assertTrue(lu.check_plan(plan, request(mode="submit", bench_size=Observed.available_value(7, "rules", what="bench_size"))).ok)

    def test_exact_solution_is_at_least_as_good_as_greedy(self):
        req = request()
        matrix = lu._build_matrix(req)
        greedy = lu._greedy_incumbent(matrix)
        greedy_value = sum(matrix.adjusted_score(i, j) for i, j in enumerate(greedy))
        plan = lu.solve(req)
        self.assertGreaterEqual(plan.objective_value + 1e-4, greedy_value)   # integer cost rounding tolerance

    def test_bench_unknown_when_rules_missing(self):
        plan = lu.solve(request(mode="submit"))
        self.assertEqual(plan.status, "legal")
        self.assertEqual(plan.bench_status, "unknown_rules")
        self.assertEqual(plan.bench, [])
        self.assertFalse(plan.submittable)
        self.assertTrue(any("bench size unknown" in v for v in plan.verification_required))
        self.assertTrue(any("substitution allowance unknown" in v for v in plan.verification_required))

    def test_competition_rules_supply_bench_and_substitution_rules(self):
        rules = {"bench_size": Observed.available_value(9, "operator", what="bench_size").to_json(), "substitutions_allowed": Observed.available_value(5, "operator", what="substitutions_allowed").to_json(), "status": "available"}
        ctx = CompetitionContext(14, "League One", "current", next_fixture().identity, {"status": "missing"}, rules, [], "operator", 1)
        req = request(competition_rules=ctx)
        self.assertEqual(req.bench_size.value, 9)
        self.assertEqual(req.substitutes_allowed.value, 5)
        plan = lu.solve(req)
        self.assertEqual(len(plan.bench), 9)
        self.assertTrue(any(lu.FLAG_GK_COVER in b.flags for b in plan.bench))
        missing_ctx = CompetitionContext(14, "League One", "current", None, {"status": "missing"}, {"status": "missing", "reason": "no profile", "capability": "competition_rules"}, [], "none", 0)
        self.assertIs(lu.rules_observed(missing_ctx, "bench_size").status, ValueStatus.MISSING)

    def test_undecoded_role_is_scored_by_fallback_and_listed_for_verification(self):
        base = slots()
        altered = [lu.RoleSlot(s.slot, s.position, None, s.duty) if s.slot == 2 else s for s in base]
        req = lu.LineupRequest(players(), altered, next_fixture(), eligibility=verified(range(1001, 1025)))
        plan = lu.solve(req)
        slot2 = next(a for a in plan.assignments if a.slot == 2)
        self.assertIn(FLAG_ROLE_FALLBACK, slot2.flags)
        self.assertEqual(slot2.role, "Central Defender")
        self.assertTrue(any("role not decoded" in v for v in plan.verification_required))


class EligibilityGateTests(unittest.TestCase):
    def test_sel01_observed_ineligible_player_is_never_assigned_in_either_mode(self):
        for mode in ("advisory", "submit"):
            best = lu.solve(request(mode=mode)).player_ids
            banned = best[:3]
            plan = lu.solve(request(mode=mode, eligibility=verified(range(1001, 1025), false_ids=banned)))
            self.assertNotEqual(plan.status, "infeasible")
            for pid in banned:
                self.assertNotIn(pid, plan.player_ids)
                self.assertNotIn(pid, [b.player_id for b in plan.bench])
                self.assertTrue(any("ineligible" in r for r in plan.exclusions[pid]))

    def test_unverified_player_allowed_in_advisory_only_and_plan_not_submittable(self):
        squad = players()
        elig = verified(range(1001, 1025), unavailable_ids=[1015])
        advisory = lu.solve(request(squad, mode="advisory", eligibility=elig))
        self.assertEqual(advisory.status, "advisory_unverified")
        self.assertIn(1015, advisory.player_ids)
        striker = next(a for a in advisory.assignments if a.player_id == 1015)
        self.assertIn(lu.FLAG_ELIGIBILITY_UNVERIFIED, striker.flags)
        self.assertFalse(advisory.submittable)
        self.assertTrue(any("not verified" in v for v in advisory.verification_required))
        submit = lu.solve(request(squad, mode="submit", eligibility=elig))
        self.assertEqual(submit.status, "legal")
        self.assertNotIn(1015, submit.player_ids)

    def test_players_missing_from_the_eligibility_map_count_as_unverified(self):
        plan = lu.solve(request(mode="submit", eligibility={}))
        self.assertEqual(plan.status, "infeasible")
        plan = lu.solve(request(mode="advisory", eligibility={}))
        self.assertEqual(plan.status, "advisory_unverified")

    def test_stale_eligibility_is_not_verified(self):
        elig = verified(range(1001, 1025))
        elig[1001] = Observed.unavailable(ValueStatus.STALE, "verified_eligible", "observed yesterday", "ui:squad")
        elig[1002] = Observed.unavailable(ValueStatus.STALE, "verified_eligible", "observed yesterday", "ui:squad")
        plan = lu.solve(request(mode="submit", eligibility=elig))
        # Both keepers are excluded; an unfamiliar outfielder keeps goal, visibly labelled.
        self.assertEqual(plan.status, "legal")
        self.assertNotIn(1001, plan.player_ids)
        self.assertNotIn(1002, plan.player_ids)
        self.assertTrue(any("stale" in r for r in plan.exclusions[1001]))
        keeper = next(a for a in plan.assignments if a.slot == 0)
        self.assertEqual(keeper.familiarity, "unfamiliar")
        self.assertTrue(any("out of position at GK" in b for b in plan.binding_constraints))


class InfeasibilityTests(unittest.TestCase):
    def test_sel02_two_goalkeeper_slots_but_one_goalkeeper(self):
        base = slots()
        altered = [lu.RoleSlot(10, "GK", "Goalkeeper", "Defend") if s.slot == 10 else s for s in base]
        squad = [p for p in players() if p.player_id != 1002]
        elig = verified([p.player_id for p in squad])
        req = lu.LineupRequest(squad, altered, next_fixture(), eligibility=elig, mode="submit")
        # Unfamiliar outfielders may still keep goal (heavily penalised), so make that impossible by exclusion of scores:
        for p in squad:
            if "GK" not in p.positions:
                p.attributes = {}   # masked attributes: no usable score in goal or anywhere
        plan = lu.solve(req)
        self.assertEqual(plan.status, "infeasible")
        self.assertEqual(plan.assignments, [])
        self.assertIsNone(plan.objective_value)
        self.assertFalse(plan.submittable)
        gk_conflict = next(c for c in plan.conflicts if set(c.slots) >= {0, 10})
        self.assertEqual(gk_conflict.kind, "coverage")
        self.assertIn("2 required slot(s)", gk_conflict.message)
        self.assertIn(1001, gk_conflict.players)
        self.assertTrue(lu.check_plan(plan, req).ok)

    def test_both_keepers_injured_explains_the_missing_cover(self):
        squad = players()
        for p in squad:
            if "GK" not in p.positions:
                p.attributes = {}
        elig = verified(range(1001, 1025), false_ids=[1001, 1002])
        plan = lu.solve(lu.LineupRequest(squad, slots(), next_fixture(), eligibility=elig, mode="advisory"))
        self.assertEqual(plan.status, "infeasible")
        conflict = plan.conflicts[0]
        self.assertEqual(conflict.slots, [0])
        self.assertEqual(conflict.players, [])
        self.assertTrue(any("1001" in r and "ineligible" in r for r in conflict.requirements))

    def test_forced_ineligible_player_is_a_conflict_not_an_assignment(self):
        elig = verified(range(1001, 1025), false_ids=[1004])
        plan = lu.solve(request(eligibility=elig, forced_assignments={2: 1004}))
        self.assertEqual(plan.status, "infeasible")
        self.assertIn(2, plan.conflicts[0].slots)
        self.assertNotIn(1004, plan.player_ids)

    def test_no_slots_is_reported(self):
        plan = lu.solve(lu.LineupRequest(players(), [], None, eligibility=verified(range(1001, 1025))))
        self.assertEqual(plan.status, "infeasible")
        self.assertEqual(plan.conflicts[0].kind, "no_slots")


class ConstraintTests(unittest.TestCase):
    def test_forced_assignment_is_honoured_and_flagged(self):
        plan = lu.solve(request(forced_assignments={0: 1002}))
        keeper = next(a for a in plan.assignments if a.slot == 0)
        self.assertEqual(keeper.player_id, 1002)
        self.assertIn(lu.FLAG_FORCED, keeper.flags)
        self.assertTrue(any("forced" in b for b in plan.binding_constraints))
        with self.assertRaises(ValueError):
            request(forced_assignments={0: 1002, 1: 1002})

    def test_minutes_cap_keeps_a_player_out_of_the_eleven(self):
        best = lu.solve(request()).player_ids
        plan = lu.solve(request(minutes_cap={best[5]: 30}))
        self.assertNotIn(best[5], plan.player_ids)
        self.assertTrue(any("minutes cap" in r for r in plan.exclusions[best[5]]))
        self.assertTrue(any("minutes caps" in b for b in plan.binding_constraints))
        # A cap of a full match is not binding.
        plan = lu.solve(request(minutes_cap={best[5]: 90}))
        self.assertIn(best[5], plan.player_ids)

    def test_excluded_players_never_start_or_sit_on_the_bench(self):
        best = lu.solve(request()).player_ids
        plan = lu.solve(request(excluded_player_ids=frozenset(best[:2]), bench_size=Observed.available_value(7, "rules", what="bench_size")))
        for pid in best[:2]:
            self.assertNotIn(pid, plan.player_ids)
            self.assertNotIn(pid, [b.player_id for b in plan.bench])

    def test_score_adjustments_change_the_pick_and_are_flagged(self):
        best = lu.solve(request()).player_ids
        plan = lu.solve(request(score_adjustments={pid: -0.9 for pid in best[1:]}))
        self.assertTrue(all(lu.FLAG_SCORE_ADJUSTED in a.flags for a in plan.assignments if a.player_id in best[1:]))
        # The objective reports unadjusted role fit; adjusted scores are reported separately.
        for a in plan.assignments:
            if a.player_id in best[1:]:
                self.assertLess(a.adjusted_score, a.score)

    def test_time_budget_zero_uses_incumbent_after_independent_check(self):
        plan = lu.solve(request(time_budget_seconds=0.0))
        self.assertEqual(plan.status, "legal")
        self.assertTrue(plan.solver["time_budget_exceeded"])
        self.assertIn("incumbent", plan.solver["method"])
        self.assertTrue(plan.solver["independent_check"]["ok"])
        self.assertTrue(any("time budget" in v for v in plan.verification_required))
        self.assertEqual(len({a.player_id for a in plan.assignments}), 11)


class CheckPlanTests(unittest.TestCase):
    def test_check_plan_rejects_tampered_plans(self):
        req = request(mode="submit", eligibility=verified(range(1001, 1025), false_ids=[1020]), bench_size=Observed.available_value(7, "rules", what="bench_size"))
        plan = lu.solve(req)
        self.assertTrue(lu.check_plan(plan, req).ok)
        plan.assignments[3].player_id = 1020
        result = lu.check_plan(plan, req)
        self.assertFalse(result.ok)
        self.assertTrue(any("ineligible" in v for v in result.violations))
        plan = lu.solve(req)
        plan.assignments[1].player_id = plan.assignments[0].player_id
        self.assertFalse(lu.check_plan(plan, req).ok)
        plan = lu.solve(req)
        plan.bench.append(lu.BenchEntry(plan.assignments[0].player_id, "dup", None, None, None))
        self.assertTrue(any("overlaps" in v for v in lu.check_plan(plan, req).violations))
        plan = lu.solve(req)
        plan.status = "legal"
        plan.assignments[0].player_id = 9999
        self.assertTrue(any("not in the request" in v for v in lu.check_plan(plan, req).violations))

    def test_check_plan_rejects_a_dishonest_legal_label(self):
        req = request(mode="advisory", eligibility=verified(range(1001, 1025), unavailable_ids=[1015]))
        plan = lu.solve(req)
        self.assertEqual(plan.status, "advisory_unverified")
        plan.status = "legal"
        result = lu.check_plan(plan, req)
        self.assertFalse(result.ok)
        self.assertTrue(any("labelled legal" in v for v in result.violations))

    def test_check_plan_rejects_bench_when_rules_unknown_or_oversized(self):
        req = request()
        plan = lu.solve(req)
        plan.bench.append(lu.BenchEntry(1019, "Jamie Mills", None, None, None))
        self.assertTrue(any("bench size is unknown" in v for v in lu.check_plan(plan, req).violations))
        req = request(bench_size=Observed.available_value(1, "rules", what="bench_size"))
        plan = lu.solve(req)
        plan.bench.append(lu.BenchEntry(1019, "Jamie Mills", None, None, None))
        self.assertTrue(any("rules allow 1" in v for v in lu.check_plan(plan, req).violations))


class SerialisationTests(unittest.TestCase):
    def test_plan_to_json_is_plain_data(self):
        import json
        plan = lu.solve(request(bench_size=Observed.available_value(7, "rules", what="bench_size")))
        data = json.loads(json.dumps(plan.to_json()))
        self.assertEqual(data["status"], "legal")
        self.assertEqual(len(data["assignments"]), 11)
        self.assertEqual(data["version"], lu.LINEUP_SOLVER_VERSION)
        self.assertFalse(data["submittable"])   # advisory mode is never submittable

    def test_eligibility_map_uses_the_rules_module(self):
        squad = players()
        result = lu.eligibility_map(squad, next_fixture(), snapshot_game_date=fx.GAME_DATE, snapshot_game_time=fx.GAME_TIME, club_id=742)
        self.assertEqual(len(result), 24)
        self.assertFalse(result[1001].available)   # no eligibility source registered
        self.assertIn(result[1001].status, (ValueStatus.MISSING, ValueStatus.UNSUPPORTED))


if __name__ == "__main__":
    unittest.main()


class MatchRulesTests(unittest.TestCase):
    def test_mat02_unknown_substitution_rules_block_submission_and_name_the_capability(self):
        """MAT 02: with no verified substitution rules the substitutes are unknown, the eleven is legal only as advice, and
        submission is blocked naming competition_rules; no bench is fabricated."""
        plan = lu.solve(request(mode="submit"))
        self.assertEqual(plan.status, "legal")
        self.assertFalse(plan.submittable)
        self.assertEqual(plan.bench, [])
        self.assertEqual(plan.bench_status, "unknown_rules")
        self.assertIs(plan.substitutes_allowed.status, ValueStatus.MISSING)
        self.assertTrue(any("substitution allowance unknown" in v and "competition_rules" in v for v in plan.verification_required))
