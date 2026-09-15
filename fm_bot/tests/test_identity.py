"""Tests for career/branch identity and continuity (spec 5.1, ID 01)."""
from __future__ import annotations

import unittest

from ..state.identity import (
    Anchor, BranchKind, CareerRegistry, ContinuityStatus, SaveManifest, check_continuity, new_id,
)
from ..state.records import Observation, QualityStatus, Visibility
from ..state.store import Store, StoreError
from . import fixtures as fx


def anchor(*, session="s1", build=fx.BUILD, manager=90001, club=742, date="2024-02-17", time="10:00", career="career-a", branch="branch-a", sequence=1) -> Anchor:
    return Anchor(career, branch, session, build, manager, club, date, time, sequence)


class ContinuityTruthTableTests(unittest.TestCase):
    """Each row of the continuity table: what the bot concludes about the loaded save."""

    def test_same_session_identity_and_time_is_continuous(self):
        result = check_continuity(anchor(), anchor(sequence=2))
        self.assertIs(result.status, ContinuityStatus.CONTINUOUS)
        self.assertFalse(result.invalidates_snapshot)
        self.assertFalse(result.requires_lineage_confirmation)

    def test_first_observation_without_confirmation_is_unknown_lineage(self):
        result = check_continuity(None, anchor())
        self.assertIs(result.status, ContinuityStatus.UNKNOWN_LINEAGE)
        self.assertTrue(result.invalidates_snapshot)
        self.assertTrue(result.requires_lineage_confirmation)

    def test_first_observation_with_confirmed_lineage_is_continuous(self):
        result = check_continuity(None, anchor(), lineage_confirmed=True)
        self.assertIs(result.status, ContinuityStatus.CONTINUOUS)

    def test_session_change_without_confirmation_requires_lineage(self):
        """A reconnect to the same process or club does not prove the same save is loaded."""
        result = check_continuity(anchor(session="s1"), anchor(session="s2", sequence=2))
        self.assertIs(result.status, ContinuityStatus.SESSION_CHANGED)
        self.assertTrue(result.requires_lineage_confirmation)
        self.assertFalse(result.stops_for_identity_resolution)
        self.assertTrue(any("does not prove continuity" in r for r in result.reasons))

    def test_session_change_with_confirmed_lineage_is_continuous(self):
        result = check_continuity(anchor(session="s1"), anchor(session="s2", sequence=2), lineage_confirmed=True)
        self.assertIs(result.status, ContinuityStatus.CONTINUOUS)
        self.assertTrue(any("lineage confirmed" in r for r in result.reasons))

    def test_date_reversed_means_a_load_happened(self):
        result = check_continuity(anchor(date="2024-02-17", time="10:00"), anchor(date="2024-02-10", time="10:00", sequence=2))
        self.assertIs(result.status, ContinuityStatus.DATE_REVERSED)
        self.assertTrue(result.stops_for_identity_resolution)
        # Same day, earlier clock is also a reversal.
        result = check_continuity(anchor(time="15:00"), anchor(time="10:00", sequence=2))
        self.assertIs(result.status, ContinuityStatus.DATE_REVERSED)

    def test_forward_jump_without_bot_progression_is_flagged(self):
        """Date monotonicity alone is not enough: a forward-dated load also changes the career state."""
        result = check_continuity(anchor(), anchor(date="2024-03-01", sequence=2))
        self.assertIs(result.status, ContinuityStatus.FORWARD_JUMP)
        self.assertTrue(result.requires_lineage_confirmation)

    def test_forward_move_after_bot_progression_is_continuous(self):
        result = check_continuity(anchor(), anchor(date="2024-02-18", sequence=2), bot_progressed=True)
        self.assertIs(result.status, ContinuityStatus.CONTINUOUS)

    def test_forward_move_with_confirmed_lineage_is_continuous(self):
        result = check_continuity(anchor(), anchor(date="2024-02-18", sequence=2), lineage_confirmed=True)
        self.assertIs(result.status, ContinuityStatus.CONTINUOUS)

    def test_manager_or_club_change_is_identity_changed(self):
        result = check_continuity(anchor(), anchor(manager=90002, sequence=2))
        self.assertIs(result.status, ContinuityStatus.IDENTITY_CHANGED)
        self.assertTrue(result.stops_for_identity_resolution)
        result = check_continuity(anchor(), anchor(club=743, sequence=2))
        self.assertIs(result.status, ContinuityStatus.IDENTITY_CHANGED)

    def test_career_or_branch_change_is_identity_changed(self):
        self.assertIs(check_continuity(anchor(), anchor(career="career-b", sequence=2)).status, ContinuityStatus.IDENTITY_CHANGED)
        self.assertIs(check_continuity(anchor(), anchor(branch="branch-b", sequence=2)).status, ContinuityStatus.IDENTITY_CHANGED)

    def test_build_change_is_reported_before_anything_else(self):
        result = check_continuity(anchor(build="24.4.2+2081827"), anchor(build="24.5.0+9999999", manager=90002, sequence=2))
        self.assertIs(result.status, ContinuityStatus.BUILD_CHANGED)
        self.assertTrue(result.stops_for_identity_resolution)

    def test_missing_session_or_identity_is_disconnected(self):
        for current in (anchor(session=None), anchor(manager=None), anchor(club=None), anchor(date=None)):
            result = check_continuity(anchor(), current)
            self.assertIs(result.status, ContinuityStatus.DISCONNECTED, current)
            self.assertTrue(result.stops_for_identity_resolution)

    def test_local_sequence_moving_backwards_is_contradictory(self):
        result = check_continuity(anchor(sequence=5), anchor(sequence=4))
        self.assertIs(result.status, ContinuityStatus.DATE_REVERSED)

    def test_to_json_carries_flags(self):
        data = check_continuity(anchor(session="s1"), anchor(session="s2", sequence=2)).to_json()
        self.assertEqual(data["status"], "session_changed")
        self.assertTrue(data["invalidates_snapshot"])
        self.assertTrue(data["requires_lineage_confirmation"])


class AnchorTests(unittest.TestCase):
    def test_game_key_none_without_date(self):
        self.assertIsNone(anchor(date=None).game_key())
        self.assertEqual(anchor(date="2024-02-17", time="10:00").game_key()[1], 600)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store.memory()
        self.registry = CareerRegistry(self.store)
        self.manifest = SaveManifest(fx.BUILD, 90001, 742, fx.GAME_DATE, fx.GAME_TIME)

    def test_register_career_creates_production_branch_and_checkpoint(self):
        career, branch, checkpoint = self.registry.register_career("wycombe", self.manifest)
        self.assertTrue(career.career_id.startswith("career-"))
        self.assertIs(branch.kind, BranchKind.PRODUCTION)
        self.assertIsNone(branch.parent_branch_id)
        self.assertEqual(branch.checkpoint_id, checkpoint.checkpoint_id)
        self.assertEqual(checkpoint.branch_id, branch.branch_id)
        self.assertEqual(self.store.get_career(career.career_id), career)
        self.assertEqual(self.store.get_branch(branch.branch_id), branch)
        self.assertEqual(self.store.get_checkpoint(checkpoint.checkpoint_id).manifest, self.manifest)
        kinds = [e["kind"] for e in self.store.journal_entries()]
        self.assertEqual(kinds, ["career.registered", "branch.created", "checkpoint.recorded"])

    def test_explicit_career_id_is_kept(self):
        career, _, _ = self.registry.register_career("wycombe", self.manifest, career_id="career-fixed")
        self.assertEqual(career.career_id, "career-fixed")

    def test_reloading_earlier_and_later_checkpoints_are_distinct(self):
        """ID 01: checkpoints at different game dates on one branch are distinct, ordered records."""
        career, branch, registration = self.registry.register_career("wycombe", self.manifest)
        earlier = self.registry.record_checkpoint(branch, SaveManifest(fx.BUILD, 90001, 742, "2024-01-05", "10:00", checksum_sha256="aaa"), label="earlier")
        later = self.registry.record_checkpoint(branch, SaveManifest(fx.BUILD, 90001, 742, "2024-03-01", "10:00", checksum_sha256="bbb"), label="later")
        ids = {registration.checkpoint_id, earlier.checkpoint_id, later.checkpoint_id}
        self.assertEqual(len(ids), 3)
        listed = self.store.list_checkpoints(branch.branch_id)
        self.assertEqual([c.checkpoint_id for c in listed], [registration.checkpoint_id, earlier.checkpoint_id, later.checkpoint_id])
        self.assertEqual(listed[1].manifest.game_date, "2024-01-05")
        self.assertEqual(listed[2].manifest.checksum_sha256, "bbb")

    def test_laboratory_fork_records_parent_and_checkpoint(self):
        """ID 01: a laboratory fork is a new branch with an explicit parent and checkpoint."""
        career, production, _ = self.registry.register_career("wycombe", self.manifest)
        lab, checkpoint = self.registry.fork_branch(production, self.manifest, label="lab-1")
        self.assertIs(lab.kind, BranchKind.LABORATORY)
        self.assertEqual(lab.parent_branch_id, production.branch_id)
        self.assertEqual(lab.checkpoint_id, checkpoint.checkpoint_id)
        self.assertEqual(lab.career_id, career.career_id)
        self.assertEqual(checkpoint.branch_id, production.branch_id)
        branches = self.store.list_branches(career.career_id)
        self.assertEqual({b.branch_id for b in branches}, {production.branch_id, lab.branch_id})

    def test_production_cannot_fork_production(self):
        _, production, _ = self.registry.register_career("wycombe", self.manifest)
        with self.assertRaises(ValueError):
            self.registry.fork_branch(production, self.manifest, kind=BranchKind.PRODUCTION)

    def test_no_cross_branch_history_merge(self):
        """ID 01: observations recorded under branch A are not listed for branch B."""
        career, branch_a, _ = self.registry.register_career("wycombe", self.manifest)
        branch_b, _ = self.registry.fork_branch(branch_a, self.manifest, label="lab")
        for branch, seq in ((branch_a, 1), (branch_a, 2), (branch_b, 3)):
            obs = Observation.create("bridge:/game", {"date": fx.GAME_DATE, "seq": seq}, career_id=career.career_id, branch_id=branch.branch_id, session_id="s", game_date=fx.GAME_DATE, game_time=fx.GAME_TIME, schema_version="v", sequence=seq)
            self.store.insert_observation(obs)
        a_ids = {o.observation_id for o in self.store.list_observations(branch_id=branch_a.branch_id)}
        b_ids = {o.observation_id for o in self.store.list_observations(branch_id=branch_b.branch_id)}
        self.assertEqual(len(a_ids), 2)
        self.assertEqual(len(b_ids), 1)
        self.assertEqual(a_ids & b_ids, set())

    def test_observation_against_unregistered_branch_is_refused(self):
        career, _, _ = self.registry.register_career("wycombe", self.manifest)
        obs = Observation.create("bridge:/game", {}, career_id=career.career_id, branch_id="branch-unknown", session_id="s", game_date=None, game_time=None, schema_version="v")
        with self.assertRaises(StoreError):
            self.store.insert_observation(obs)

    def test_matches_registration_reports_each_mismatch(self):
        career, _, _ = self.registry.register_career("wycombe", self.manifest)
        ok, problems = self.registry.matches_registration(career, fx.status_payload(), 90001, 742)
        self.assertTrue(ok)
        self.assertEqual(problems, [])
        ok, problems = self.registry.matches_registration(career, fx.status_payload(build="24.5.0"), 90002, 743)
        self.assertFalse(ok)
        self.assertEqual(len(problems), 3)

    def test_new_id_prefix_and_uniqueness(self):
        a, b = new_id("snap"), new_id("snap")
        self.assertTrue(a.startswith("snap-") and b.startswith("snap-"))
        self.assertNotEqual(a, b)

    def test_manifest_json_round_trip(self):
        data = self.manifest.to_json()
        self.assertEqual(SaveManifest.from_json(data), self.manifest)


if __name__ == "__main__":
    unittest.main()
