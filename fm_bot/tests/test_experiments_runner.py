"""Tests for fm_bot.experiments.runner (spec 4.1, 13.1, 13.2, BOT 010)."""
from __future__ import annotations

import unittest

from ..bridge_client.client import BridgeClient
from ..execution.adapter import FakeAdapter
from ..experiments.manifests import validate, validate_trial
from ..experiments.runner import (
    AdapterRestorer,
    INVALID_RESTORE_FAILED,
    INVALID_TREATMENT_NOT_APPLIED,
    INVALID_WRONG_SAVE_IDENTITY,
    JOURNAL_SIMULATION,
    JOURNAL_TRIAL,
    LAB_LOCK_NAME,
    LAB_REQUIRED_CAPABILITIES,
    LaboratoryBusy,
    LaboratoryRunner,
    ProductionRestoreRefused,
    ProgressionResult,
    RestoreResult,
    SimulatedRollout,
    TreatmentApplication,
    TrialReport,
    characterize_repeatability,
)
from ..rules.capabilities import CapabilityRegistry
from ..state.identity import BranchKind, CareerRegistry, SaveManifest
from ..state.records import Experiment
from ..state.snapshot import SnapshotCollector
from ..state.status import MissingCapabilityReport
from ..state.store import Store
from . import fixtures as fx

ENVIRONMENT = {"build_hash": "deadbeef", "database_config": {"database": "24.3"}, "loaded_leagues": ["England League One"], "detail_settings": {"detail": "full"}}


class FakeRestorer:
    name = "fake_restorer"

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[tuple[str, str]] = []

    def restore(self, checkpoint, branch):
        self.calls.append((checkpoint.checkpoint_id, branch.branch_id))
        return RestoreResult(self.ok, checkpoint.checkpoint_id, {"fake": True}, None if self.ok else "fake restore failed")


class Lab:
    """A laboratory with the fixture world loaded at (or away from) the checkpoint."""

    def __init__(self, *, game_date: str = fx.GAME_DATE, capabilities: bool = True, restorer=None, adapter=None):
        self.store = Store.memory()
        self.registry = CareerRegistry(self.store)
        self.career, self.main, self.checkpoint = self.registry.register_career("lab", SaveManifest(fx.BUILD, fx.MANAGER["id"], fx.CLUB["id"], fx.GAME_DATE, fx.GAME_TIME, path="C:/saves/lab.fm"))
        self.adapter = adapter or FakeAdapter()
        self.client = BridgeClient(fx.transport(game_date=game_date), self.store, context={"career_id": self.career.career_id, "branch_id": self.main.branch_id})
        self.collector = SnapshotCollector(self.client, self.store)
        self.capabilities = CapabilityRegistry.from_status(fx.status_payload(), supported_builds=(fx.BUILD,))
        if capabilities:
            self.capabilities.provide("save_restore", "operator", "validated save/load workflow")
            self.capabilities.provide("career_registration", "registry")
            self.capabilities.provide("ui_action_adapter", "fake")
        self.restorer = FakeRestorer() if restorer is None else restorer
        self.runner = LaboratoryRunner(self.store, self.registry, self.capabilities, self.adapter, self.collector, restorer=self.restorer, owner_id="lab-a")
        self.applied: list[dict] = []
        self.progressed = 0

    def experiment(self, treatment=None) -> Experiment:
        return Experiment("exp-press", "high pressing improves goal difference", treatment or {"name": "pressing_high", "tactic": "counter-02"}, {"name": "control"}, self.main.branch_id, self.checkpoint.checkpoint_id, {"method": "matched_checkpoint"}, "policy/1", smallest_useful_effect=0.3, stopping_rule="20 paired trials")

    def applier(self, applied: bool = True):
        def apply(context, treatment):
            self.applied.append(treatment)
            context.adapter.selected_tactic_id = treatment.get("tactic", context.adapter.selected_tactic_id)
            readback = context.adapter.readback("selected_tactic")
            return TreatmentApplication(applied and readback.available, {"readback": readback.to_json()}, None if applied else "tactic readback did not match")
        return apply

    def progression(self, ok: bool = True):
        def progress(context):
            self.progressed += 1
            return ProgressionResult(ok, "2024-02-24", "22:00", steps=2, error=None if ok else "continue button not found")
        return progress

    @staticmethod
    def outcomes(context):
        return {"result": "defeat", "goals_for": 0, "goals_against": 2, "injuries": ["1015"]}


class GateTests(unittest.TestCase):
    def test_read_only_bridge_alone_cannot_run_rollouts(self):
        lab = Lab(capabilities=False)
        report = lab.runner.run_trial(lab.experiment(), lab.applier(), lab.progression(), Lab.outcomes)
        self.assertIsInstance(report, MissingCapabilityReport)
        self.assertTrue(report.blocked)
        for name in LAB_REQUIRED_CAPABILITIES:
            self.assertIn(name, report.missing)
        self.assertEqual(report.blocked_action, "laboratory.run_trial")
        self.assertEqual(lab.store.list_branches(lab.career.career_id), [lab.main])   # nothing forked
        self.assertEqual(lab.restorer.calls, [])

    def test_adapter_without_ui_capability_blocks(self):
        lab = Lab(adapter=FakeAdapter(capabilities=()))
        report = lab.runner.readiness()
        self.assertEqual(report.missing, ["ui_action_adapter"])

    def test_production_restore_is_refused_before_any_input(self):
        lab = Lab()
        with self.assertRaises(ProductionRestoreRefused):
            lab.runner.restore_checkpoint(lab.main, lab.checkpoint)
        self.assertEqual(lab.restorer.calls, [])
        branch, ck = lab.registry.fork_branch(lab.main, lab.checkpoint.manifest)
        self.assertTrue(lab.runner.restore_checkpoint(branch, ck).ok)
        self.assertEqual(lab.store.journal_entries("laboratory.restore")[0]["ref_id"], branch.branch_id)

    def test_one_game_instance_guard(self):
        lab = Lab()
        self.assertTrue(lab.store.acquire_lock(LAB_LOCK_NAME, "other-runner"))
        with self.assertRaises(LaboratoryBusy):
            lab.runner.run_trial(lab.experiment(), lab.applier(), lab.progression(), Lab.outcomes)
        lab.store.release_lock(LAB_LOCK_NAME, "other-runner")
        report = lab.runner.run_trial(lab.experiment(), lab.applier(), lab.progression(), Lab.outcomes)
        self.assertIsInstance(report, TrialReport)
        self.assertIsNone(lab.store.lock_owner(LAB_LOCK_NAME))   # released afterwards


class TrialTests(unittest.TestCase):
    def test_valid_trial_forks_restores_treats_progresses_and_records(self):
        lab = Lab()
        experiment = lab.experiment()
        report = lab.runner.run_trial(experiment, lab.applier(), lab.progression(), Lab.outcomes, environment=ENVIRONMENT, authority_profile_version=2, model_versions={"lineup": "v1"})
        self.assertIsInstance(report, TrialReport)
        self.assertTrue(report.technically_valid, report.invalidity_reasons)
        # laboratory branch forked from main at the starting checkpoint
        self.assertIs(report.branch.kind, BranchKind.LABORATORY)
        self.assertEqual(report.branch.parent_branch_id, lab.main.branch_id)
        self.assertEqual(lab.restorer.calls, [(lab.checkpoint.checkpoint_id, report.branch.branch_id)])
        # manifest complete and mapped (EXP 01)
        self.assertEqual(validate(report.run), [])
        self.assertEqual(validate_trial(report.trial), [])
        mapping = report.trial.mapping()
        self.assertEqual(mapping["checkpoint_id"], lab.checkpoint.checkpoint_id)
        self.assertEqual(mapping["build"], fx.BUILD)
        self.assertEqual(mapping["policy_version"], "policy/1")
        self.assertEqual(mapping["treatment"]["name"], "pressing_high")
        self.assertEqual(report.run.parent_chain, [lab.main.branch_id])
        self.assertEqual(report.run.authority_profile_version, 2)
        self.assertEqual(report.run.model_versions, {"lineup": "v1"})
        self.assertIsNotNone(report.run.elapsed_wall_seconds)
        self.assertEqual(report.run.validity, {"restored": True, "identity_confirmed": True, "treatment_applied": True, "progressed": True, "outcomes_collected": True, "technically_valid": True})
        # treatment applied through the adapter and confirmed by readback
        self.assertEqual(lab.applied, [experiment.treatment])
        self.assertEqual(lab.adapter.selected_tactic_id, "counter-02")
        self.assertEqual(report.trial.treatment_confirmation["confirmation"]["readback"]["value"]["tactic_id"], "counter-02")
        self.assertEqual(lab.progressed, 1)
        # a defeat and an injury are outcomes, not invalidity
        self.assertEqual(report.outcomes["result"], "defeat")
        stored = lab.store.get_experiment("exp-press")
        self.assertEqual(stored.outcomes[report.trial.trial_id]["result"], "defeat")
        self.assertTrue(stored.validity_flags[report.trial.trial_id])
        journal = lab.store.journal_entries(JOURNAL_TRIAL)
        self.assertEqual(journal[0]["body"]["parent_checkpoint_id"], lab.checkpoint.checkpoint_id)
        self.assertEqual(len(lab.store.journal_entries("laboratory.treatment")), 1)

    def test_second_trial_gets_its_own_branch_and_no_history_merge(self):
        lab = Lab()
        first = lab.runner.run_trial(lab.experiment(), lab.applier(), lab.progression(), Lab.outcomes)
        second = lab.runner.run_trial(lab.experiment({"name": "control"}), lab.applier(), lab.progression(), Lab.outcomes, arm="control")
        self.assertNotEqual(first.branch.branch_id, second.branch.branch_id)
        self.assertEqual(second.branch.parent_branch_id, lab.main.branch_id)
        self.assertEqual(second.trial.arm, "control")
        stored = lab.store.get_experiment("exp-press")
        self.assertEqual(set(stored.outcomes), {first.trial.trial_id, second.trial.trial_id})

    def test_wrong_save_identity_is_technically_invalid(self):
        lab = Lab(game_date="2024-03-01")   # a later save is loaded instead of the checkpoint
        report = lab.runner.run_trial(lab.experiment(), lab.applier(), lab.progression(), Lab.outcomes)
        self.assertFalse(report.technically_valid)
        self.assertTrue(report.invalidity_reasons[0].startswith(INVALID_WRONG_SAVE_IDENTITY))
        self.assertIn("2024-03-01", report.invalidity_reasons[0])
        self.assertEqual(lab.applied, [])            # no treatment sent to the wrong save
        self.assertEqual(lab.progressed, 0)
        self.assertEqual(report.outcomes, {})
        self.assertFalse(lab.store.get_experiment("exp-press").validity_flags[report.trial.trial_id])
        self.assertFalse(report.run.validity["identity_confirmed"])

    def test_failed_treatment_application_is_technically_invalid(self):
        lab = Lab()
        report = lab.runner.run_trial(lab.experiment(), lab.applier(applied=False), lab.progression(), Lab.outcomes)
        self.assertFalse(report.technically_valid)
        self.assertTrue(report.invalidity_reasons[0].startswith(INVALID_TREATMENT_NOT_APPLIED))
        self.assertEqual(lab.progressed, 0)
        self.assertFalse(report.run.validity["treatment_applied"])

    def test_applier_exception_is_recorded_not_raised(self):
        lab = Lab()

        def broken(context, treatment):
            raise RuntimeError("tactics screen not found")

        report = lab.runner.run_trial(lab.experiment(), broken, lab.progression(), Lab.outcomes)
        self.assertFalse(report.technically_valid)
        self.assertIn("tactics screen not found", report.invalidity_reasons[0])

    def test_failed_restore_is_technically_invalid(self):
        lab = Lab(restorer=FakeRestorer(ok=False))
        report = lab.runner.run_trial(lab.experiment(), lab.applier(), lab.progression(), Lab.outcomes)
        self.assertFalse(report.technically_valid)
        self.assertTrue(report.invalidity_reasons[0].startswith(INVALID_RESTORE_FAILED))
        self.assertEqual(lab.applied, [])

    def test_default_adapter_restorer_fails_closed(self):
        """No adapter implements load_save yet; the trial is invalid rather than assumed restored."""
        lab = Lab()
        lab.runner.restorer = AdapterRestorer(lab.adapter)
        report = lab.runner.run_trial(lab.experiment(), lab.applier(), lab.progression(), Lab.outcomes)
        self.assertFalse(report.technically_valid)
        self.assertTrue(report.invalidity_reasons[0].startswith(INVALID_RESTORE_FAILED))
        self.assertEqual(lab.adapter.inputs, [])

    def test_failed_progression_is_technical_but_empty_outcomes_are_flagged(self):
        lab = Lab()
        report = lab.runner.run_trial(lab.experiment(), lab.applier(), lab.progression(ok=False), Lab.outcomes)
        self.assertFalse(report.technically_valid)
        self.assertIn("progression_failed", report.invalidity_reasons[0])
        lab2 = Lab()
        report2 = lab2.runner.run_trial(lab2.experiment(), lab2.applier(), lab2.progression(), lambda ctx: {})
        self.assertFalse(report2.technically_valid)
        self.assertIn("outcomes_unavailable", report2.invalidity_reasons[0])


class SimulationTests(unittest.TestCase):
    def test_simulated_rollout_is_never_an_fm_experiment(self):
        lab = Lab()
        experiment = lab.experiment()
        rollout = SimulatedRollout(lab.store, lambda exp: {"goal_difference": 1.2}, simulator_version="sim/0.1")
        result = rollout.run(experiment)
        self.assertFalse(result.is_fm_experiment)
        self.assertEqual(result.kind, JOURNAL_SIMULATION)
        self.assertEqual(result.outcomes, {"goal_difference": 1.2})
        self.assertEqual(len(lab.store.journal_entries(JOURNAL_SIMULATION)), 1)
        self.assertEqual(lab.store.journal_entries(JOURNAL_TRIAL), [])
        self.assertIsNone(lab.store.get_experiment(experiment.experiment_id))
        self.assertEqual(lab.store.list_branches(lab.career.career_id), [lab.main])
        self.assertEqual(experiment.outcomes, {})


class RepeatabilityTests(unittest.TestCase):
    def test_identical_outcomes_are_not_independent_replicates(self):
        same = [{"result": "win", "score": "2-1"}] * 4
        report = characterize_repeatability(same)
        self.assertEqual(report.verdict, "repeats")
        self.assertEqual(report.effective_units, 1)
        self.assertFalse(report.independent)

    def test_varied_and_partial_and_insufficient(self):
        varied = [{"score": "2-1"}, {"score": "0-0"}, {"score": "1-3"}]
        self.assertEqual(characterize_repeatability(varied).verdict, "varies")
        self.assertEqual(characterize_repeatability(varied).effective_units, 3)
        partial = characterize_repeatability([{"score": "2-1"}, {"score": "2-1"}, {"score": "0-0"}])
        self.assertEqual(partial.verdict, "partially_repeats")
        self.assertEqual(partial.effective_units, 2)
        self.assertEqual(characterize_repeatability([{"score": "2-1"}]).verdict, "insufficient_runs")
        self.assertEqual(characterize_repeatability([]).effective_units, 0)


if __name__ == "__main__":
    unittest.main()
