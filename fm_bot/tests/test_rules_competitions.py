"""Tests for versioned competition rules profiles (spec 11.4, 7.1, MAT 02)."""
from __future__ import annotations

import unittest

from ..bridge_client.client import BridgeClient
from ..rules.competitions import (
    COMPETITION_RULES_VERSION, DEFAULT_STAGE, RULE_FIELDS, CompetitionRules, RefreshDecision, RulesEvent, RulesProfileRegistry,
    competition_context, deadline_entries, needs_refresh, rule_missing, setting_key,
)
from ..state.identity import CareerRegistry, SaveManifest
from ..state.records import CompetitionContext
from ..state.snapshot import CollectionContext, SnapshotCollector, SnapshotRequirements
from ..state.status import Observed, ValueStatus
from ..state.store import Store
from ..state.views import upcoming_fixtures
from . import fixtures as fx

SOURCE = "ui:competition_rules_screen"


def default_rules_profile_for_tests(competition_id: int = 14, stage: str = "league", *, season: str = "2023/24", with_deadlines: bool = True) -> CompetitionRules:
    """A fully observed profile used only by tests. Values are test data, not real competition rules."""
    obs = lambda value, what: Observed.available_value(value, SOURCE, "2026-01-01T00:00:00Z", "2024-02-17 10:00", what)  # noqa: E731
    deadlines = obs([{"kind": "squad_registration", "date": "2024-02-22", "time": "17:00", "description": "post-window squad list due"}], "deadlines") if with_deadlines else rule_missing("deadlines", "not shown on the rules screen")
    return CompetitionRules(competition_id, stage, obs([{"opens": "2024-01-01", "closes": "2024-02-01", "kind": "winter"}], "registration_windows"), obs(22, "squad_size_limit"), obs({"minimum": 4, "definition": "club-trained"}, "homegrown_rule"), obs(7, "bench_size"), obs(5, "substitutions_allowed"), obs(3, "substitution_windows"), obs({"kind": "league"}, "tie_progression"), deadlines, SOURCE, season=season, division_id=3, competition_name="Sky Bet League One")


def build_snapshot():
    store = Store.memory()
    career, branch, _ = CareerRegistry(store).register_career("t", SaveManifest(fx.BUILD, 90001, 742, fx.GAME_DATE, fx.GAME_TIME))
    client = BridgeClient(fx.transport(), store, context={"career_id": career.career_id, "branch_id": branch.branch_id})
    snap = SnapshotCollector(client, store).collect(SnapshotRequirements(routes=["/fixtures"]), CollectionContext(career.career_id, branch.branch_id, lineage_confirmed=True))
    return store, snap


class CompetitionRulesTests(unittest.TestCase):
    def test_unknown_profile_has_no_universal_defaults(self):
        rules = CompetitionRules.unknown(33, "round_2")
        self.assertFalse(rules.complete)
        self.assertEqual(rules.missing_fields(), list(RULE_FIELDS))
        for name in RULE_FIELDS:
            self.assertIs(rules.field_observed(name).status, ValueStatus.MISSING)
        with self.assertRaises(Exception):
            rules.bench_size.require()

    def test_json_roundtrip_preserves_statuses(self):
        rules = default_rules_profile_for_tests(with_deadlines=False)
        rules.version = 3
        back = CompetitionRules.from_json(rules.to_json())
        self.assertEqual(back.bench_size.value, 7)
        self.assertIs(back.deadlines.status, ValueStatus.MISSING)
        self.assertEqual(back.deadlines.reason, "not shown on the rules screen")
        self.assertEqual(back.season, "2023/24")
        self.assertEqual(back.version, 3)
        self.assertEqual(back.rules_version, COMPETITION_RULES_VERSION)

    def test_setting_key_uses_default_stage(self):
        self.assertEqual(setting_key(14, None), f"competition_rules:14:{DEFAULT_STAGE}")
        self.assertEqual(setting_key(14, "group"), "competition_rules:14:group")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.store, self.snap = build_snapshot()
        self.registry = RulesProfileRegistry(self.store)
        self.fixtures = upcoming_fixtures(self.snap, 5)
        self.cup, self.league = self.fixtures[0], self.fixtures[1]

    def test_missing_profile_gives_missing_context(self):
        context = self.registry.context_for(self.cup)
        self.assertIsInstance(context, CompetitionContext)
        self.assertEqual(context.competition_id, 33)
        self.assertEqual(context.squad_rules["status"], "missing")
        self.assertEqual(context.squad_rules["capability"], "competition_rules")
        self.assertEqual(context.substitution_rules["status"], "missing")
        self.assertEqual(context.deadlines, [])
        self.assertEqual(context.squad_rules["deadlines_status"], "missing")
        self.assertEqual(context.fixture_identity, self.cup.identity)
        self.assertEqual(context.rules_version, 0)

    def test_store_load_and_versioning(self):
        rules = default_rules_profile_for_tests(14, "league")
        stored = self.registry.store_profile(rules)
        self.assertEqual(stored.version, 1)
        loaded = self.registry.load_profile(14, "league")
        self.assertEqual(loaded.version, 1)
        self.assertEqual(loaded.bench_size.value, 7)
        loaded.bench_size = Observed.available_value(9, SOURCE, what="bench_size")
        self.assertEqual(self.registry.store_profile(loaded).version, 2)
        self.assertEqual(self.registry.load_profile(14, "league").bench_size.value, 9)
        self.assertIsNone(self.registry.load_profile(14, "playoffs"))
        self.assertEqual([p.competition_id for p in self.registry.list_profiles()], [14])
        self.assertTrue(any(e["kind"] == "setting" for e in self.store.journal_entries("setting")))

    def test_context_from_stored_profile(self):
        self.registry.store_profile(default_rules_profile_for_tests(14, "league"))
        context = self.registry.context_for(self.league, "league")
        self.assertEqual(context.squad_rules["status"], "available")
        self.assertEqual(context.squad_rules["squad_size_limit"]["value"], 22)
        self.assertEqual(context.substitution_rules["bench_size"]["value"], 7)
        self.assertEqual(context.substitution_rules["substitutions_allowed"]["value"], 5)
        self.assertEqual(context.source, SOURCE)
        self.assertEqual(context.rules_version, 1)
        kinds = [d["kind"] for d in context.deadlines]
        self.assertEqual(kinds, ["registration_deadline", "squad_registration"])
        self.assertEqual(context.deadlines[0]["date"], "2024-02-01")

    def test_partial_profile_is_partial_not_assumed(self):
        rules = default_rules_profile_for_tests(14, "league")
        rules.bench_size = rule_missing("bench_size", "bench size not shown")
        context = competition_context(self.league, rules)
        self.assertEqual(context.substitution_rules["status"], "partial")
        self.assertEqual(context.substitution_rules["missing"], ["bench_size"])
        self.assertEqual(context.substitution_rules["bench_size"]["status"], "missing")

    def test_stage_keeps_profiles_separate(self):
        self.registry.store_profile(default_rules_profile_for_tests(33, "group"))
        self.assertEqual(self.registry.context_for(self.cup, "group").squad_rules["status"], "available")
        self.assertEqual(self.registry.context_for(self.cup, "knockout").squad_rules["status"], "missing")
        self.assertEqual(self.registry.context_for(self.cup).squad_rules["status"], "missing")

    def test_deadline_entries_without_observed_deadlines(self):
        rules = default_rules_profile_for_tests(with_deadlines=False)
        entries = deadline_entries(rules)
        self.assertEqual([e["kind"] for e in entries], ["registration_deadline"])
        rules.registration_windows = rule_missing("registration_windows")
        self.assertEqual(deadline_entries(rules), [])


class RefreshPolicyTests(unittest.TestCase):
    def test_missing_profile_needs_observing(self):
        decision = needs_refresh(None, RulesEvent("fixture"))
        self.assertIsInstance(decision, RefreshDecision)
        self.assertTrue(decision.refresh)

    def test_promotion_and_context_change_refresh(self):
        rules = default_rules_profile_for_tests()
        for kind in ("promotion", "relegation", "competition_context_changed"):
            self.assertTrue(needs_refresh(rules, RulesEvent(kind)).refresh, kind)

    def test_season_boundary(self):
        rules = default_rules_profile_for_tests(season="2023/24")
        self.assertTrue(needs_refresh(rules, RulesEvent("season_boundary", season="2024/25")).refresh)
        self.assertFalse(needs_refresh(rules, RulesEvent("season_boundary", season="2023/24")).refresh)
        self.assertTrue(needs_refresh(rules, RulesEvent("season_boundary")).refresh)

    def test_stage_and_division_changes(self):
        rules = default_rules_profile_for_tests(stage="league")
        self.assertFalse(needs_refresh(rules, RulesEvent("stage_changed", stage="league")).refresh)
        self.assertTrue(needs_refresh(rules, RulesEvent("stage_changed", stage="playoffs")).refresh)
        self.assertTrue(needs_refresh(rules, RulesEvent("fixture", division_id=2)).refresh)

    def test_unrelated_event_and_other_competition_do_not_refresh(self):
        rules = default_rules_profile_for_tests(14)
        self.assertFalse(needs_refresh(rules, RulesEvent("fixture")).refresh)
        self.assertFalse(needs_refresh(rules, RulesEvent("promotion", competition_id=33)).refresh)

    def test_profile_without_season_refreshes_when_season_known(self):
        rules = default_rules_profile_for_tests(season=None)
        rules.season = None
        self.assertTrue(needs_refresh(rules, RulesEvent("fixture", season="2023/24")).refresh)


if __name__ == "__main__":
    unittest.main()
