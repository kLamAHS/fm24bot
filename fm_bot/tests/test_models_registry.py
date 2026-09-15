"""Tests for fm_bot.models.registry (spec 13.5, MOD 01)."""
from __future__ import annotations

import unittest

from ..models import calibration as cal
from ..models.registry import ModelRegistry, ModelRegistryError, ReleaseContext, feature_names
from ..state.records import ModelVersion, ReleaseState
from ..state.store import Store
from ..state.visibility import InformationMode

BUILD = "24.4.2+2081827"
GOOD = cal.CalibrationReport("result", "1", 300, 0.18, 0.5, 0.02, None, 0.1, {}, "passed")
BAD = cal.CalibrationReport("result", "1", 300, 0.31, 0.9, 0.12, None, -0.2, {}, "failed", ["brier 0.31 above 0.25"])


def model(version="1", *, features=("fixture.date", "player.condition"), mode="manager_visible", builds=(BUILD,), artifact="hash-a", baseline="rolling_average") -> ModelVersion:
    return ModelVersion("result", version, {"features": list(features)}, {"careers": ["c1"]}, mode, list(builds), {}, artifact, ReleaseState.CANDIDATE, baseline)


def context(mode=InformationMode.MANAGER_VISIBLE, build=BUILD, available=None) -> ReleaseContext:
    return ReleaseContext(build, mode, None, frozenset(available) if available is not None else None)


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.store = Store.memory()
        self.registry = ModelRegistry(self.store)

    def test_register_and_get(self):
        self.registry.register(model())
        self.assertEqual(self.registry.get("result", "1").artifact_hash, "hash-a")
        self.assertEqual(feature_names(self.registry.get("result", "1")), ["fixture.date", "player.condition"])

    def test_same_artifact_is_idempotent(self):
        self.registry.register(model())
        self.registry.register(model())
        self.assertEqual(len(self.registry.versions("result")), 1)

    def test_different_artifact_under_same_version_raises(self):
        self.registry.register(model())
        with self.assertRaises(ModelRegistryError):
            self.registry.register(model(artifact="hash-b"))

    def test_feature_names_from_dict_schema(self):
        m = model()
        m.feature_schema = {"player.condition": "float", "fixture.date": "date"}
        self.assertEqual(sorted(feature_names(m)), ["fixture.date", "player.condition"])


class PromotionGateTests(unittest.TestCase):
    def setUp(self):
        self.store = Store.memory()
        self.registry = ModelRegistry(self.store)
        self.registry.register(model())

    def test_all_gates_pass_releases(self):
        result = self.registry.promote("result", "1", GOOD, context())
        self.assertIs(result.release_state, ReleaseState.RELEASED)
        self.assertEqual(self.registry.get("result", "1").release_state, ReleaseState.RELEASED)
        self.assertEqual(self.registry.get("result", "1").calibration["status"], "passed")
        self.assertEqual(self.store.journal_entries("model.gate")[0]["ref_id"], "result:1")

    def test_failed_calibration_stays_candidate(self):
        result = self.registry.promote("result", "1", BAD, context())
        self.assertIs(result.release_state, ReleaseState.CANDIDATE)
        self.assertFalse(result.calibration_passed)
        self.assertTrue(result.schema_supported and result.build_supported)

    def test_failed_calibration_can_be_advisory_only_when_asked(self):
        result = self.registry.promote("result", "1", BAD, context(), allow_advisory=True)
        self.assertIs(result.release_state, ReleaseState.EXPERIMENTAL_ADVISORY)
        self.assertTrue(any("advisory" in r for r in result.reasons))

    def test_inconclusive_calibration_does_not_release(self):
        inconclusive = cal.CalibrationReport("result", "1", 5, None, None, None, None, None, {}, "inconclusive", ["5 samples"])
        self.assertIs(self.registry.promote("result", "1", inconclusive, context()).release_state, ReleaseState.CANDIDATE)

    def test_privileged_feature_blocks_manager_visible_release(self):
        self.registry.register(model("2", features=("player.attributes",), mode="manager_visible"))
        result = self.registry.promote("result", "2", cal.CalibrationReport("result", "2", 300, 0.1, 0.3, 0.01, None, None, {}, "passed"), context())
        self.assertIs(result.release_state, ReleaseState.CANDIDATE)
        self.assertFalse(result.schema_supported)
        self.assertTrue(any("feature schema unsupported" in r for r in result.reasons))

    def test_unsupported_schema_never_becomes_advisory(self):
        self.registry.register(model("2", features=("player.attributes",)))
        result = self.registry.promote("result", "2", cal.CalibrationReport("result", "2", 300, 0.1, 0.3, 0.01, None, None, {}, "passed"), context(), allow_advisory=True)
        self.assertIs(result.release_state, ReleaseState.CANDIDATE)

    def test_build_outside_scope_blocks_release(self):
        result = self.registry.promote("result", "1", GOOD, context(build="25.0.0"))
        self.assertIs(result.release_state, ReleaseState.CANDIDATE)
        self.assertFalse(result.build_supported)

    def test_bridge_observed_model_cannot_release_for_manager_visible(self):
        self.registry.register(model("2", mode="bridge_observed"))
        result = self.registry.promote("result", "2", cal.CalibrationReport("result", "2", 300, 0.1, 0.3, 0.01, None, None, {}, "passed"), context())
        self.assertFalse(result.schema_supported)
        released = self.registry.promote("result", "2", cal.CalibrationReport("result", "2", 300, 0.1, 0.3, 0.01, None, None, {}, "passed"), context(InformationMode.BRIDGE_OBSERVED))
        self.assertIs(released.release_state, ReleaseState.RELEASED)

    def test_report_for_other_version_rejected(self):
        with self.assertRaises(ModelRegistryError):
            self.registry.promote("result", "1", cal.CalibrationReport("result", "9", 300, 0.1, 0.3, 0.01, None, None, {}, "passed"), context())

    def test_retired_cannot_be_promoted(self):
        self.registry.retire("result", "1", "superseded")
        with self.assertRaises(ModelRegistryError):
            self.registry.promote("result", "1", GOOD, context())


class ResolutionTests(unittest.TestCase):
    """MOD 01: failed calibration or unsupported feature schema falls back to baseline."""

    def setUp(self):
        self.store = Store.memory()
        self.registry = ModelRegistry(self.store)
        self.registry.register(model())

    def test_no_released_version_falls_back_to_named_baseline(self):
        resolution = self.registry.resolve("result", context())
        self.assertTrue(resolution.fallback)
        self.assertEqual(resolution.baseline_id, "rolling_average")
        self.assertEqual(resolution.resolved_id, "rolling_average")
        self.assertIn("no released version", resolution.reason)

    def test_failed_calibration_falls_back(self):
        self.registry.promote("result", "1", BAD, context())
        resolution = self.registry.resolve("result", context())
        self.assertTrue(resolution.fallback)
        self.assertIsNone(resolution.version)

    def test_released_version_resolves(self):
        self.registry.promote("result", "1", GOOD, context())
        resolution = self.registry.resolve("result", context())
        self.assertFalse(resolution.fallback)
        self.assertEqual(resolution.resolved_id, "result:1")
        self.assertFalse(resolution.advisory)

    def test_unsupported_feature_schema_in_context_falls_back(self):
        self.registry.promote("result", "1", GOOD, context())
        resolution = self.registry.resolve("result", context(available={"fixture.date"}))
        self.assertTrue(resolution.fallback)
        self.assertIn("player.condition", resolution.reason)

    def test_bridge_observed_model_never_resolves_for_manager_visible(self):
        self.registry.register(model("2", mode="bridge_observed", features=("player.attributes",)))
        self.registry.promote("result", "2", cal.CalibrationReport("result", "2", 300, 0.1, 0.3, 0.01, None, None, {}, "passed"), context(InformationMode.BRIDGE_OBSERVED))
        self.assertFalse(self.registry.resolve("result", context(InformationMode.BRIDGE_OBSERVED)).fallback)
        resolution = self.registry.resolve("result", context(InformationMode.MANAGER_VISIBLE))
        self.assertTrue(resolution.fallback)
        self.assertIn("cannot serve manager_visible", resolution.reason)

    def test_changed_build_falls_back(self):
        self.registry.promote("result", "1", GOOD, context())
        self.assertTrue(self.registry.resolve("result", context(build="25.0.0")).fallback)

    def test_advisory_only_resolves_when_allowed_and_is_marked(self):
        self.registry.promote("result", "1", BAD, context(), allow_advisory=True)
        self.assertTrue(self.registry.resolve("result", context()).fallback)
        resolution = self.registry.resolve("result", context(), allow_advisory=True)
        self.assertFalse(resolution.fallback)
        self.assertTrue(resolution.advisory)

    def test_default_baseline_setting_used_when_model_names_none(self):
        self.registry.register(ModelVersion("other", "1", {"features": ["fixture.date"]}, {}, "manager_visible", [BUILD], {}, "h", ReleaseState.CANDIDATE, None))
        self.assertIsNone(self.registry.resolve("other", context()).baseline_id)
        self.registry.set_default_baseline("other", "explicit_accounting")
        self.assertEqual(ModelRegistry(self.store).resolve("other", context()).baseline_id, "explicit_accounting")


if __name__ == "__main__":
    unittest.main()
