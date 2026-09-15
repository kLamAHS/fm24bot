"""Tests for fm_bot.planning.minutes (spec 7.1, 7.3, 10.1; BOT 008)."""
from __future__ import annotations

import json
import unittest

from ..planning import lineup as lu
from ..planning import minutes as mn
from ..state.records import ConsistencyStatus, DecisionSnapshot
from ..state.status import Observed, ValueStatus
from ..state.views import FixtureView, fixture_views, player_state, tactic_view
from . import fixtures as fx

CLUB = 742


def players():
    return [player_state(fx.player_payload(*spec), source="test") for spec in fx.SQUAD_SPEC]


def snapshot():
    return DecisionSnapshot("snap-hand", [], ConsistencyStatus.CONSISTENT, [], [], [], {}, "bridge_observed", "c", "b", fx.SESSION, fx.GAME_DATE, fx.GAME_TIME, routes={"/tactics": fx.tactics_payload(), "/fixtures": fx.fixtures_payload()}, manager_id=90001, club_id=CLUB)


def slots():
    return lu.slots_from_tactic_view(tactic_view(snapshot())).require()


def upcoming(count=5):
    return [f for f in fixture_views(snapshot()) if f.scheduled][:count]


def verified(ids):
    return {pid: Observed.available_value(True, "ui:squad", what="verified_eligible") for pid in ids}


def scenario(ids):
    return {pid: Observed.unavailable(ValueStatus.MISSING, "verified_eligible", "future fixture: not yet observable", "none") for pid in ids}


def request(fixtures=None, *, current_verified=True, **kw):
    squad = kw.pop("players", None) or players()
    fixtures = fixtures if fixtures is not None else upcoming()
    ids = [p.player_id for p in squad]
    eligibility = kw.pop("eligibility", None) or [verified(ids) if (i == 0 and current_verified) else scenario(ids) for i in range(len(fixtures))]
    kw.setdefault("club_id", CLUB)
    return mn.MinutesRequest(squad, fixtures, slots(), eligibility, **kw)


class HorizonTests(unittest.TestCase):
    def test_immediate_is_the_action_and_futures_are_flagged_plans(self):
        result = mn.plan(request(mode="submit"))
        self.assertEqual(len(result.fixtures), 5)
        first = result.immediate
        self.assertEqual(first.kind, "action")
        self.assertEqual(first.lineup.status, "legal")
        self.assertNotIn(mn.FLAG_PLAN_NOT_CONFIRMED_FIT, first.flags)
        for future in result.fixtures[1:]:
            self.assertEqual(future.kind, "plan")
            self.assertIn(mn.FLAG_PLAN_NOT_CONFIRMED_FIT, future.flags)
            self.assertEqual(future.lineup.mode, "advisory")
            self.assertEqual(future.lineup.status, "advisory_unverified")
            self.assertFalse(future.lineup.submittable)
            for assignment in future.lineup.assignments:
                self.assertIn(mn.FLAG_PLAN_NOT_CONFIRMED_FIT, assignment.flags)
        self.assertEqual(result.method, "sequential_heuristic")
        self.assertEqual(result.version, mn.MINUTES_PLANNER_VERSION)

    def test_cumulative_minutes_are_planned_starts_times_match_minutes(self):
        result = mn.plan(request())
        for pid, total in result.cumulative_minutes.items():
            starts = sum(1 for f in result.fixtures if pid in f.planned_minutes)
            self.assertEqual(total, starts * 90)
        self.assertEqual(sum(result.cumulative_minutes.values()), 5 * 11 * 90)

    def test_long_horizon_is_truncated_and_short_horizon_noted(self):
        extra = upcoming() + [FixtureView("2024-03-23", "15:00", 14, "League One", CLUB, 809, "Wycombe", "Wigan", "scheduled", None, None, "f6"), FixtureView("2024-03-30", "15:00", 14, "League One", 810, CLUB, "Exeter", "Wycombe", "scheduled", None, None, "f7")]
        result = mn.plan(request(extra))
        self.assertEqual(len(result.fixtures), 6)
        self.assertTrue(any("truncated" in n for n in result.horizon["notes"]))
        short = mn.plan(request(upcoming(2)))
        self.assertEqual(len(short.fixtures), 2)
        self.assertTrue(any("shorter" in n for n in short.horizon["notes"]))
        empty = mn.plan(request([]))
        self.assertEqual(empty.fixtures, [])
        self.assertIn("no fixtures supplied for the horizon", empty.violations)

    def test_request_validation(self):
        with self.assertRaises(ValueError):
            mn.MinutesRequest(players(), upcoming(), slots(), [verified([])])
        with self.assertRaises(ValueError):
            mn.Restriction(1001, "ban", "operator")


class RestrictionTests(unittest.TestCase):
    def test_suspension_excludes_only_the_dated_fixtures(self):
        best = mn.plan(request()).immediate.lineup.player_ids
        pid = best[4]
        ban = mn.Restriction(pid, "suspension", "ui:squad", "one-match ban", from_date="2024-02-24", to_date="2024-02-24")
        result = mn.plan(request(restrictions=[ban]))
        self.assertNotIn(pid, result.fixtures[1].planned_minutes)
        self.assertIn(pid, result.fixtures[1].restricted)
        self.assertTrue(any("ineligible" in r for r in result.fixtures[1].lineup.exclusions[pid]))
        self.assertIn(pid, result.fixtures[0].planned_minutes)

    def test_competition_specific_suspension(self):
        pid = mn.plan(request()).immediate.lineup.player_ids[3]
        ban = mn.Restriction(pid, "suspension", "ui:squad", "cup ban", competition_id=33)
        result = mn.plan(request(restrictions=[ban]))
        self.assertNotIn(pid, result.fixtures[0].planned_minutes)   # cup fixture on 02-20
        self.assertIn(pid, result.fixtures[1].planned_minutes)

    def test_loan_player_cannot_face_parent_club(self):
        pid = mn.plan(request()).immediate.lineup.player_ids[6]
        loan = mn.Restriction(pid, "loan_parent_club", "bridge:/players contracts", "loan from Derby", opponent_club_id=805)
        result = mn.plan(request(restrictions=[loan]))
        self.assertNotIn(pid, result.fixtures[1].planned_minutes)   # away at Derby (805)
        self.assertIn(pid, result.fixtures[0].planned_minutes)
        self.assertFalse(loan.applies(upcoming()[1], None))          # unknown own club: cannot apply


class RecoveryAndWorkloadTests(unittest.TestCase):
    def test_recovery_cap_forces_rotation_between_close_fixtures(self):
        def model(pid, days, minutes):
            if days is None:
                return Observed.available_value(90, "recovery-test", what="permitted_minutes")
            return Observed.available_value(45 if days < 5 else 90, "recovery-test", what="permitted_minutes")
        result = mn.plan(request(recovery_model=model))
        first, second = result.fixtures[0], result.fixtures[1]   # 02-20 and 02-24: four days apart
        self.assertFalse(set(first.planned_minutes) & set(second.planned_minutes))
        self.assertTrue(all(second.caps[pid] == 45 for pid in first.planned_minutes))
        self.assertTrue(any("rested by recovery/workload caps" in t for t in result.trade_offs))
        self.assertEqual(second.flags, [mn.FLAG_PLAN_NOT_CONFIRMED_FIT, mn.FLAG_FUTURE_ELIGIBILITY_SCENARIO])

    def test_last_appearance_seeds_the_recovery_model(self):
        seen = {}

        def model(pid, days, minutes):
            seen[pid] = (days, minutes)
            return Observed.unavailable(ValueStatus.MISSING, "permitted_minutes", "no model", "recovery-test")
        mn.plan(request(upcoming(1), recovery_model=model, last_appearance={1001: ("2024-02-17", 90)}))
        self.assertEqual(seen[1001], (3, 90))
        self.assertEqual(seen[1004], (None, None))

    def test_unavailable_recovery_estimate_applies_no_cap_and_is_flagged(self):
        model = lambda pid, days, minutes: Observed.unavailable(ValueStatus.UNSUPPORTED, "permitted_minutes", "recovery model not calibrated", "recovery-test")  # noqa: E731
        result = mn.plan(request(recovery_model=model))
        for fixture in result.fixtures:
            self.assertIn(mn.FLAG_RECOVERY_UNAVAILABLE, fixture.flags)
            self.assertEqual(fixture.caps, {})
            self.assertEqual(len(fixture.recovery_unavailable), 24)
            self.assertEqual(len(fixture.lineup.assignments), 11)
        self.assertTrue(any("recovery not assessed" in t for t in result.trade_offs))

    def test_workload_cap_bounds_total_planned_minutes(self):
        pid = mn.plan(request()).immediate.lineup.player_ids[2]
        result = mn.plan(request(workload_caps={pid: 180}))
        self.assertLessEqual(result.cumulative_minutes.get(pid, 0), 180)
        self.assertEqual(result.cumulative_minutes.get(pid, 0), 180)
        self.assertFalse(any("workload cap" in v for v in result.violations))


class PromiseTests(unittest.TestCase):
    def test_promised_minutes_are_consumed(self):
        baseline = mn.plan(request())
        benched = [p.player_id for p in players() if p.player_id not in baseline.cumulative_minutes]
        self.assertTrue(benched)
        for pid in benched:
            result = mn.plan(request(promised_minutes={pid: 180}))
            self.assertEqual(result.promises[pid]["status"], "planned", pid)
            self.assertGreaterEqual(result.cumulative_minutes[pid], 180)
            self.assertLessEqual(result.improvement["iterations"], mn.MAX_IMPROVEMENT_ITERATIONS + 1)
            self.assertFalse(any("promised" in v for v in result.violations))

    def test_improvement_pass_forces_a_promise_the_bonus_alone_could_not_meet(self):
        # Regression check on the heuristic with roles-v1 weights: Jamie Mills (1019) needs a forced start.
        result = mn.plan(request(promised_minutes={1019: 180}))
        self.assertEqual(result.promises[1019]["status"], "planned")
        self.assertGreaterEqual(result.improvement["accepted"], 1)
        self.assertEqual(result.improvement["method"], "bounded_forced_reassignment")
        forced = [a for f in result.fixtures for a in f.lineup.assignments if mn.FLAG_PROMISE_IMPROVEMENT in a.flags]
        self.assertTrue(forced)
        self.assertTrue(all(a.player_id == 1019 for a in forced))

    def test_impossible_promise_is_a_listed_violation_not_a_fabrication(self):
        result = mn.plan(request(promised_minutes={1019: 90 * 6}))
        self.assertEqual(result.promises[1019]["status"], "unmet")
        self.assertTrue(any("promised 540 minutes" in v for v in result.violations))
        self.assertLessEqual(result.cumulative_minutes.get(1019, 0), 90 * 5)

    def test_promise_to_a_restricted_player_stays_unmet_in_that_fixture(self):
        ban = mn.Restriction(1021, "suspension", "ui:squad", "ban", from_date="2024-02-01", to_date="2024-03-31")
        result = mn.plan(request(restrictions=[ban], promised_minutes={1021: 90}))
        self.assertEqual(result.cumulative_minutes.get(1021, 0), 0)
        self.assertEqual(result.promises[1021]["status"], "unmet")


class InfeasibilityTests(unittest.TestCase):
    def test_a_fixture_without_a_legal_eleven_is_a_violation(self):
        squad = players()[:12]   # eleven starters plus one spare
        bans = [mn.Restriction(pid, "suspension", "ui:squad", "ban", from_date="2024-02-24", to_date="2024-02-24") for pid in (1003, 1004)]
        result = mn.plan(request(players=squad, restrictions=bans))
        self.assertEqual(result.fixtures[1].lineup.status, "infeasible")
        self.assertEqual(result.fixtures[1].planned_minutes, {})
        self.assertEqual(result.fixtures[1].lineup.conflicts[0].kind, "coverage")
        self.assertTrue(any("fixture 1" in v and "no legal eleven" in v for v in result.violations))
        for index in (0, 2, 3, 4):
            self.assertNotEqual(result.fixtures[index].lineup.status, "infeasible")


class SerialisationTests(unittest.TestCase):
    def test_plan_to_json_is_plain_data(self):
        result = mn.plan(request(promised_minutes={1021: 90}, bench_size=Observed.available_value(7, "rules", what="bench_size")))
        data = json.loads(json.dumps(result.to_json()))
        self.assertEqual(len(data["fixtures"]), 5)
        self.assertEqual(data["fixtures"][0]["kind"], "action")
        self.assertIn("1021", data["promises"])
        self.assertEqual(data["method"], "sequential_heuristic")
        self.assertEqual(len(data["fixtures"][0]["lineup"]["bench"]), 7)


if __name__ == "__main__":
    unittest.main()
