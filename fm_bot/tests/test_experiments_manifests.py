"""Tests for fm_bot.experiments.manifests (spec 13.1, EXP 01, BOT 002/010)."""
from __future__ import annotations

import unittest

from ..experiments.manifests import (
    MANIFEST_VERSION,
    REQUIRED_RUN_FIELDS,
    Difference,
    RunManifest,
    TrialManifest,
    differences_from_parent,
    is_unrecorded,
    parent_chain,
    run_manifest_for,
    unrecorded,
    validate,
    validate_trial,
)
from ..state.identity import BranchKind, CareerRegistry, SaveManifest
from ..state.store import Store
from . import fixtures as fx

ENVIRONMENT = {"build_hash": "abc123", "database_config": {"database": "24.3", "mods": []}, "loaded_leagues": ["England League One"], "detail_settings": {"detail": "full"}}


def registered(store: Store):
    return CareerRegistry(store).register_career("t", SaveManifest(fx.BUILD, fx.MANAGER["id"], fx.CLUB["id"], fx.GAME_DATE, fx.GAME_TIME))


def full_manifest(store: Store, environment=ENVIRONMENT):
    career, branch, checkpoint = registered(store)
    return run_manifest_for(branch=branch, checkpoint=checkpoint, experiment_id="exp-1", policy_version="policy/1", authority_profile_version=3, treatment={"name": "pressing_high"}, environment=environment, model_versions={"lineup": "v2"}), branch, checkpoint


class RunManifestTests(unittest.TestCase):
    def test_complete_manifest_validates_and_round_trips(self):
        store = Store.memory()
        manifest, branch, checkpoint = full_manifest(store)
        self.assertEqual(validate(manifest), [])
        self.assertEqual(manifest.build, fx.BUILD)
        self.assertEqual((manifest.club_id, manifest.manager_id), (fx.CLUB["id"], fx.MANAGER["id"]))
        self.assertEqual(manifest.starting_checkpoint_id, checkpoint.checkpoint_id)
        self.assertEqual(manifest.manifest_version, MANIFEST_VERSION)
        again = RunManifest.from_json(manifest.to_json())
        self.assertEqual(again, manifest)
        self.assertEqual(again.fingerprint(), manifest.fingerprint())

    def test_unrecorded_environment_is_explicit_and_fails_validation(self):
        store = Store.memory()
        manifest, _, _ = full_manifest(store, environment=None)
        self.assertTrue(is_unrecorded(manifest.database_config))
        self.assertTrue(is_unrecorded(manifest.loaded_leagues))
        self.assertIsNone(manifest.build_hash)   # unknown, not fabricated
        problems = validate(manifest)
        self.assertEqual(len(problems), 3)
        self.assertTrue(all("unrecorded" in p for p in problems))
        self.assertEqual(unrecorded("x"), {"status": "unrecorded", "reason": "x"})

    def test_missing_required_fields_are_listed_by_name(self):
        store = Store.memory()
        manifest, _, _ = full_manifest(store)
        manifest.policy_version = None
        manifest.treatment = {}
        manifest.authority_profile_version = None
        problems = validate(manifest)
        self.assertIn("policy_version: missing", problems)
        self.assertIn("authority_profile_version: missing", problems)
        self.assertTrue(any(p.startswith("treatment:") for p in problems))
        for name in REQUIRED_RUN_FIELDS:
            self.assertTrue(hasattr(manifest, name))

    def test_finish_records_wall_time_and_validity(self):
        store = Store.memory()
        manifest, _, _ = full_manifest(store)
        self.assertIsNone(manifest.technically_valid)
        manifest.finish(elapsed_wall_seconds=12.5, technically_valid=False, reasons=["wrong_save_identity: game date differs"], validity={"restored": True, "identity_confirmed": False})
        self.assertEqual(manifest.elapsed_wall_seconds, 12.5)
        self.assertFalse(manifest.technically_valid)
        self.assertEqual(manifest.validity, {"restored": True, "identity_confirmed": False, "technically_valid": False})
        self.assertIsNotNone(manifest.finished_at)
        self.assertEqual(validate(manifest), [])

    def test_finished_run_without_validity_decision_is_a_problem(self):
        store = Store.memory()
        manifest, _, _ = full_manifest(store)
        manifest.finished_at = manifest.started_at
        self.assertIn("technically_valid: undecided after the run finished", validate(manifest))

    def test_fingerprint_ignores_timing_but_not_treatment(self):
        store = Store.memory()
        manifest, _, _ = full_manifest(store)
        before = manifest.fingerprint()
        manifest.finish(elapsed_wall_seconds=1.0, technically_valid=True)
        self.assertEqual(manifest.fingerprint(), before)
        manifest.treatment = {"name": "control"}
        self.assertNotEqual(manifest.fingerprint(), before)


class LineageTests(unittest.TestCase):
    def test_parent_chain_walks_to_root_and_manifest_records_it(self):
        store = Store.memory()
        registry = CareerRegistry(store)
        career, main, ck = registered(store)
        lab1, ck1 = registry.fork_branch(main, ck.manifest, label="lab1")
        lab2, ck2 = registry.fork_branch(lab1, ck1.manifest, kind=BranchKind.LABORATORY, label="lab2")
        self.assertEqual(parent_chain(store, lab2.branch_id), [main.branch_id, lab1.branch_id])
        self.assertEqual(parent_chain(store, main.branch_id), [])
        manifest = run_manifest_for(branch=lab2, checkpoint=ck2, experiment_id="e", policy_version="p", authority_profile_version=1, treatment={"name": "t"}, environment=ENVIRONMENT, parent_chain_ids=parent_chain(store, lab2.branch_id))
        self.assertEqual(manifest.parent_chain, [main.branch_id, lab1.branch_id])
        self.assertEqual(manifest.career_id, career.career_id)


class TrialManifestTests(unittest.TestCase):
    def test_trial_maps_to_checkpoint_build_policy_treatment_and_outcomes(self):
        """EXP 01: every trial resolves to checkpoint, build, policy, treatment and outcomes."""
        store = Store.memory()
        run, branch, checkpoint = full_manifest(store)
        run.finish(elapsed_wall_seconds=3.0, technically_valid=True)
        trial = TrialManifest("trial-1", "exp-1", checkpoint.checkpoint_id, run, "treatment", differences_from_parent(run, checkpoint), {"points_per_match": 1.5, "result": "defeat"}, True, [])
        mapping = trial.mapping()
        self.assertEqual(mapping["checkpoint_id"], checkpoint.checkpoint_id)
        self.assertEqual(mapping["build"], fx.BUILD)
        self.assertEqual(mapping["policy_version"], "policy/1")
        self.assertEqual(mapping["treatment"], {"name": "pressing_high"})
        self.assertEqual(mapping["outcomes"]["result"], "defeat")
        self.assertTrue(mapping["technically_valid"])
        self.assertEqual(validate_trial(trial), [])
        again = TrialManifest.from_json(trial.to_json())
        self.assertEqual(again.to_json(), trial.to_json())
        self.assertEqual(again.differences, trial.differences)

    def test_differences_against_checkpoint_and_against_parent_run(self):
        store = Store.memory()
        run, branch, checkpoint = full_manifest(store)
        diffs = differences_from_parent(run, checkpoint)
        self.assertEqual([d.field for d in diffs], ["treatment"])   # identity matches; treatment is the intended difference
        parent_run = RunManifest.from_json(run.to_json())
        parent_run.treatment = {"name": "control"}
        parent_run.policy_version = "policy/0"
        diffs = differences_from_parent(run, checkpoint, parent_run)
        self.assertEqual({d.field for d in diffs}, {"treatment", "policy_version"})
        self.assertIn(Difference("policy_version", "policy/0", "policy/1"), diffs)
        run.club_id = 1
        self.assertIn("club_id", {d.field for d in differences_from_parent(run, checkpoint)})

    def test_trial_with_incomplete_run_cannot_be_counted(self):
        store = Store.memory()
        run, branch, checkpoint = full_manifest(store, environment=None)
        trial = TrialManifest("trial-2", "", checkpoint.checkpoint_id, run, "control", [])
        problems = validate_trial(trial)
        self.assertIn("experiment_id: missing", problems)
        self.assertTrue(any(p.startswith("run.database_config") for p in problems))


if __name__ == "__main__":
    unittest.main()
