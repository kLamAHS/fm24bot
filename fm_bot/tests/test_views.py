"""Tests for typed views over snapshot payloads (spec 5.3, OBS 02)."""
from __future__ import annotations

import unittest

from ..bridge_client.client import BridgeClient
from ..state.identity import CareerRegistry, SaveManifest
from ..state.records import ConsistencyStatus, DecisionSnapshot
from ..state.snapshot import CollectionContext, SnapshotCollector, SnapshotRequirements
from ..state.status import ValueStatus
from ..state.store import Store
from ..state.units import Money, Period
from ..state.views import (
    finance_view, fixture_identity, fixture_views, horizon_truncated, missing_eligibility, player_state, readiness_observed,
    squad_states, tactic_view, upcoming_fixtures,
)
from ..state.visibility import InformationMode
from . import fixtures as fx


def collected_snapshot(routes=("/squad", "/finances", "/fixtures", "/tactics")) -> DecisionSnapshot:
    store = Store.memory()
    career, branch, _ = CareerRegistry(store).register_career("t", SaveManifest(fx.BUILD, 90001, 742, fx.GAME_DATE, fx.GAME_TIME))
    client = BridgeClient(fx.transport(), store, context={"career_id": career.career_id, "branch_id": branch.branch_id})
    return SnapshotCollector(client, store).collect(SnapshotRequirements(routes=list(routes)), CollectionContext(career.career_id, branch.branch_id, lineage_confirmed=True))


def manual_snapshot(routes: dict, *, game_date=fx.GAME_DATE, mode="bridge_observed") -> DecisionSnapshot:
    """A hand-built consistent snapshot for view tests that need a specific payload."""
    return DecisionSnapshot("snap-manual", [], ConsistencyStatus.CONSISTENT, [], [], [], {}, mode, "career-a", "branch-a", fx.SESSION, game_date, fx.GAME_TIME, routes=routes, manager_id=90001, club_id=742)


class ReadinessTests(unittest.TestCase):
    def test_current_readiness_yields_condition_and_sharpness(self):
        condition, sharpness = readiness_observed(fx.player_payload(*fx.SQUAD_SPEC[0]), "obs-1")
        self.assertEqual(condition.require(), 93.0)
        self.assertEqual(sharpness.require(), 88.0)
        self.assertEqual(condition.source, "obs-1")

    def test_stale_readiness_is_stale_not_a_number(self):
        """OBS 02: a stale readiness cache leaves condition None with status stale, never a stale float."""
        payload = fx.player_payload(*fx.SQUAD_SPEC[0], readiness_current=False)
        condition, sharpness = readiness_observed(payload)
        self.assertIs(condition.status, ValueStatus.STALE)
        self.assertIs(sharpness.status, ValueStatus.STALE)
        self.assertIn("2024-02-10", condition.reason)
        state = player_state(payload)
        self.assertIsNone(state.condition)
        self.assertIsNone(state.match_sharpness)
        self.assertEqual(state.readiness_status, "stale")

    def test_unknown_readiness_is_missing(self):
        payload = fx.player_payload(*fx.SQUAD_SPEC[0])
        payload["readiness"] = None
        condition, _ = readiness_observed(payload)
        self.assertIs(condition.status, ValueStatus.MISSING)
        self.assertEqual(player_state(payload).readiness_status, "unknown")


class PlayerStateTests(unittest.TestCase):
    def test_player_state_from_payload(self):
        payload = fx.player_payload(*fx.SQUAD_SPEC[3])
        state = player_state(payload, source="obs-7")
        self.assertEqual((state.player_id, state.name), (1004, "Ryan Tafazolli"))
        self.assertEqual(len(state.attributes), 47)
        self.assertEqual(state.attribute_units, "fm_1_to_20")
        self.assertIn("DC", state.positions)
        self.assertEqual(state.morale, "Very Good")
        self.assertEqual(state.morale_status, "available")
        self.assertEqual(state.employment[0]["weekly_wage_gbp"], 5200)
        self.assertEqual(state.age, payload["age"])
        self.assertEqual(state.source_observation_id, "obs-7")

    def test_default_eligibility_is_missing(self):
        """OBS 02: with no eligibility source registered, eligibility is explicitly missing, not clear."""
        state = player_state(fx.player_payload(*fx.SQUAD_SPEC[0]))
        self.assertEqual(state.eligibility, missing_eligibility(1001))
        self.assertEqual(state.eligibility["status"], "missing")
        self.assertFalse(state.eligibility["verified"])
        self.assertIsNone(state.eligibility["injury"])
        self.assertIsNone(state.eligibility["registration"])

    def test_manager_visible_mode_strips_attributes(self):
        state = player_state(fx.player_payload(*fx.SQUAD_SPEC[0]), mode=InformationMode.MANAGER_VISIBLE)
        self.assertEqual(state.attributes, {})
        self.assertEqual(state.attribute_units, "unavailable")
        self.assertEqual(state.position_ratings, {})
        self.assertEqual(state.morale, "Good")   # own-club morale is visible

    def test_other_club_player_in_manager_visible_mode_has_no_morale(self):
        state = player_state(fx.other_club_player(), mode=InformationMode.MANAGER_VISIBLE, other_club=True)
        self.assertIsNone(state.morale)
        self.assertEqual(state.morale_status, "unsupported")
        self.assertEqual(state.employment, [])


class SquadStatesTests(unittest.TestCase):
    def test_squad_states_default_to_missing_eligibility(self):
        snap = collected_snapshot()
        states = squad_states(snap)
        self.assertEqual(len(states), 24)
        self.assertEqual([s.player_id for s in states][:3], [1001, 1002, 1003])
        self.assertTrue(all(s.eligibility["status"] == "missing" for s in states))
        self.assertTrue(all(s.source_observation_id == f"{snap.snapshot_id}:/squad" for s in states))

    def test_squad_states_use_the_provider_and_fixture(self):
        snap = collected_snapshot()
        seen = []

        def provider(pid, fixture):
            seen.append((pid, fixture))
            return {"status": "clear", "verified": True, "source": "ui:squad", "reasons": [], "injury": None, "suspension": None, "loan_absence": None, "registration": None}

        states = squad_states(snap, eligibility=provider, fixture={"identity": "f1"})
        self.assertEqual(states[0].eligibility["status"], "clear")
        self.assertEqual(seen[0], (1001, {"identity": "f1"}))

    def test_squad_falls_back_to_club_roster_then_empty(self):
        from_club = manual_snapshot({"/club": {"id": 742, "name": "Wycombe", "squad": fx.squad_payload()[:2]}})
        self.assertEqual(len(squad_states(from_club)), 2)
        self.assertEqual(squad_states(manual_snapshot({})), [])

    def test_snapshot_mode_applies_to_squad(self):
        snap = manual_snapshot({"/squad": fx.squad_payload()[:1]}, mode="manager_visible")
        self.assertEqual(squad_states(snap)[0].attribute_units, "unavailable")


class FinanceViewTests(unittest.TestCase):
    def test_money_basis_and_headroom(self):
        view = finance_view(collected_snapshot())
        self.assertTrue(view.available)
        self.assertEqual(view.balance.require(), Money.native_gbp(10909005))
        self.assertEqual(view.transfer_budget.require(), Money.native_gbp(1720250))
        self.assertEqual(view.wage_budget_weekly.require(), Money.native_gbp(78979, Period.WEEKLY))
        payroll = view.payroll_spending_weekly.require()
        self.assertIs(payroll.period, Period.WEEKLY)
        self.assertEqual(payroll.minor, sum(s[4] for s in fx.SQUAD_SPEC) * 100)
        headroom = view.headroom_weekly().require()
        self.assertEqual(headroom, Money.native_gbp(78979 - sum(s[4] for s in fx.SQUAD_SPEC), Period.WEEKLY))
        self.assertEqual(view.as_of, fx.GAME_DATE)
        self.assertEqual(view.balance.game_time, fx.GAME_DATE)

    def test_null_money_stays_null(self):
        """OBS 02: a null wage budget is NULL, not zero, and the headroom becomes unavailable."""
        payload = fx.finances_payload()
        payload["wage_budget_weekly"] = None
        view = finance_view(manual_snapshot({"/finances": payload}))
        self.assertIs(view.wage_budget_weekly.status, ValueStatus.NULL)
        self.assertFalse(view.available)
        self.assertIs(view.headroom_weekly().status, ValueStatus.MISSING)
        self.assertTrue(view.balance.available)

    def test_absent_route_is_missing(self):
        view = finance_view(manual_snapshot({}))
        self.assertIs(view.balance.status, ValueStatus.MISSING)
        self.assertIn("/finances not collected", view.balance.reason)
        self.assertIsNone(view.as_of)

    def test_non_gbp_currency_is_unsupported(self):
        payload = fx.finances_payload()
        payload["currency"] = "EUR"
        view = finance_view(manual_snapshot({"/finances": payload}))
        self.assertIs(view.balance.status, ValueStatus.UNSUPPORTED)
        self.assertEqual(view.currency, "EUR")
        self.assertIn("EUR", view.balance.reason)


class FixtureViewTests(unittest.TestCase):
    def test_fixture_views_are_sorted_by_date_and_time(self):
        views = fixture_views(collected_snapshot())
        self.assertEqual(len(views), 8)
        self.assertEqual([v.date for v in views], sorted(v.date for v in views))
        self.assertEqual(views[0].status, "played")
        self.assertEqual((views[0].home_score, views[0].away_score), (1, 1))

    def test_upcoming_fixtures_are_scheduled_from_today_in_order(self):
        snap = collected_snapshot()
        upcoming = upcoming_fixtures(snap)
        self.assertEqual([f.date for f in upcoming], ["2024-02-20", "2024-02-24", "2024-03-02", "2024-03-09", "2024-03-16"])
        first = upcoming[0]
        self.assertTrue(first.scheduled)
        self.assertTrue(first.is_home(742))
        self.assertEqual(first.opponent(742), "Bolton")
        self.assertEqual(first.competition_name, "Bristol Street Motors Trophy")
        self.assertEqual(first.identity, "2024-02-20T19:45 home:742 away:804 comp:33")
        self.assertFalse(upcoming[1].is_home(742))
        self.assertEqual(upcoming[1].opponent(742), "Derby")
        self.assertEqual(len(upcoming_fixtures(snap, count=2)), 2)

    def test_upcoming_includes_a_fixture_on_the_snapshot_date(self):
        payload = fx.fixtures_payload()
        payload["fixtures"].append({"date": fx.GAME_DATE, "time": "15:00", "competition_id": 14, "competition_name": "L1", "home": fx.HOME, "away": fx._team(8009, 809, "Exeter"), "status": "scheduled", "home_score": None, "away_score": None})
        upcoming = upcoming_fixtures(manual_snapshot({"/fixtures": payload}))
        self.assertEqual(upcoming[0].date, fx.GAME_DATE)
        self.assertEqual(upcoming_fixtures(manual_snapshot({"/fixtures": payload}, game_date=None)), [])

    def test_horizon_truncation_flag(self):
        """Five future fixtures in February are fewer than six, but the year has plenty of room: not truncated."""
        snap = collected_snapshot()
        self.assertFalse(horizon_truncated(snap))
        self.assertFalse(horizon_truncated(snap, count=5))
        # Late in the calendar year with nothing left the bridge's year-scoped list is the limit.
        late = manual_snapshot({"/fixtures": fx.fixtures_payload()}, game_date="2024-11-20")
        self.assertEqual(upcoming_fixtures(late), [])
        self.assertTrue(horizon_truncated(late))
        self.assertTrue(horizon_truncated(manual_snapshot({}, game_date=None)))

    def test_fixture_identity_without_time(self):
        item = dict(fx.fixtures_payload()["fixtures"][0])
        item["time"] = None
        self.assertTrue(fixture_identity(item).startswith("2024-01-27T??:??"))


class TacticViewTests(unittest.TestCase):
    def test_decoded_tactic(self):
        view = tactic_view(collected_snapshot())
        self.assertEqual(view["status"], "available")
        self.assertEqual(view["name"], "4-4-2 Balanced")
        self.assertEqual(view["mentality"], "Balanced")
        self.assertEqual(len(view["slots"]), 11)
        self.assertEqual(view["slots"][0], {"slot": 0, "position": "GK", "role": "Goalkeeper", "duty": "Defend", "player_id": 1001, "role_status": "decoded"})
        self.assertEqual(view["substitutes"], [1002, 1021, 1010, 1016, 1017, 1023, 1006])
        self.assertEqual(view["undecoded_roles"], [])

    def test_undecoded_roles_are_listed_by_slot(self):
        payload = fx.tactics_payload()
        payload["positions"][3]["role"] = None
        payload["positions"][7]["role"] = None
        view = tactic_view(manual_snapshot({"/tactics": payload}))
        self.assertEqual(view["undecoded_roles"], [3, 7])
        self.assertEqual(view["slots"][3]["role_status"], "not_decoded")
        self.assertEqual(view["slots"][4]["role_status"], "decoded")

    def test_missing_and_unavailable_tactics(self):
        self.assertEqual(tactic_view(manual_snapshot({}))["status"], "missing")
        view = tactic_view(manual_snapshot({"/tactics": {"available": False, "reason": "no tactic selected"}}))
        self.assertEqual(view, {"status": "unavailable", "reason": "no tactic selected"})


if __name__ == "__main__":
    unittest.main()
