"""Tests for information modes and visibility masks (spec 1.2, VIS 01)."""
from __future__ import annotations

import unittest

from ..state.records import Visibility
from ..state.status import ValueStatus
from ..state.visibility import (
    DEFAULT_MASK, FeatureLineageError, InformationMode, VisibilityMask, apply_mode, assert_features_allowed, mode_of_model,
)
from . import fixtures as fx


def player():
    return fx.player_payload(*fx.SQUAD_SPEC[3])


class ApplyModeTests(unittest.TestCase):
    def test_bridge_observed_keeps_everything(self):
        record = apply_mode("player", player(), InformationMode.BRIDGE_OBSERVED)
        self.assertEqual(record.fields, player())
        self.assertEqual(record.masked, [])
        self.assertTrue(record.get("attributes").available)

    def test_manager_visible_removes_attributes_and_records_lineage(self):
        """VIS 01: privileged attributes are removed before any feature can be built from them."""
        record = apply_mode("player", player(), InformationMode.MANAGER_VISIBLE)
        self.assertNotIn("attributes", record.fields)
        self.assertNotIn("position_ratings", record.fields)
        self.assertNotIn("morale_rating", record.fields)
        self.assertIn("name", record.fields)
        self.assertIn("morale", record.fields)
        self.assertIn("contracts", record.fields)
        masked = {m.field: m.reason for m in record.masked}
        self.assertIn("privileged field removed", masked["attributes"])
        self.assertIs(record.mode, InformationMode.MANAGER_VISIBLE)
        observed = record.get("attributes")
        self.assertIs(observed.status, ValueStatus.UNSUPPORTED)
        self.assertEqual(observed.source, "mask:manager_visible")
        self.assertIsNone(observed.value)

    def test_unknown_visibility_is_unavailable_in_manager_visible_mode(self):
        """Fields not declared in the mask (first_name, surname, age_as_of) are unknown, so unavailable."""
        record = apply_mode("player", player(), InformationMode.MANAGER_VISIBLE)
        masked = {m.field: m.reason for m in record.masked}
        for name in ("first_name", "surname", "age_as_of", "readiness"):
            self.assertNotIn(name, record.fields)
            self.assertIn("visibility unknown", masked[name])
        self.assertIs(record.get("first_name").status, ValueStatus.UNSUPPORTED)

    def test_absent_field_is_missing_not_masked(self):
        record = apply_mode("player", {"id": 1, "name": "x"}, InformationMode.MANAGER_VISIBLE)
        self.assertIs(record.get("attributes").status, ValueStatus.MISSING)

    def test_other_club_player_loses_contract_and_morale(self):
        """Another club's terms, morale and fitness are not shown to the manager without a report."""
        payload = fx.other_club_player()
        own = apply_mode("player", payload, InformationMode.MANAGER_VISIBLE)
        other = apply_mode("player", payload, InformationMode.MANAGER_VISIBLE, other_club=True)
        self.assertIn("contracts", own.fields)
        for name in ("contracts", "morale", "condition", "match_sharpness"):
            self.assertNotIn(name, other.fields, name)
        self.assertIn("name", other.fields)
        self.assertIn("positions", other.fields)
        self.assertIn("visibility unknown", {m.field: m.reason for m in other.masked}["contracts"])

    def test_other_club_flag_does_not_matter_in_bridge_observed_mode(self):
        record = apply_mode("player", fx.other_club_player(), InformationMode.BRIDGE_OBSERVED, other_club=True)
        self.assertIn("contracts", record.fields)

    def test_wildcard_entities_are_visible(self):
        record = apply_mode("finances", fx.finances_payload(), InformationMode.MANAGER_VISIBLE)
        self.assertIn("balance", record.fields)
        self.assertNotIn("club_id", record.fields)   # not declared: unknown
        fixture = apply_mode("fixture", fx.fixtures_payload()["fixtures"][0], InformationMode.MANAGER_VISIBLE)
        self.assertEqual(fixture.masked, [])

    def test_mask_declaration_bumps_version_and_changes_outcome(self):
        mask = VisibilityMask()
        self.assertEqual(mask.version, 1)
        mask.declare("player", "first_name", Visibility.VISIBLE)
        self.assertEqual(mask.version, 2)
        record = apply_mode("player", player(), InformationMode.MANAGER_VISIBLE, mask)
        self.assertIn("first_name", record.fields)
        self.assertEqual(record.mask_version, 2)
        self.assertIs(DEFAULT_MASK[("player", "attributes")], Visibility.PRIVILEGED)
        self.assertNotIn(("player", "first_name"), DEFAULT_MASK)   # declare never mutates the default


class FeatureGuardTests(unittest.TestCase):
    def test_privileged_features_are_refused_in_manager_visible_mode(self):
        """VIS 01: 'player.attributes' cannot enter a manager-visible feature set."""
        features = {"player.attributes": {"pace": 14}, "player.age": 24}
        with self.assertRaises(FeatureLineageError) as ctx:
            assert_features_allowed(features, InformationMode.MANAGER_VISIBLE)
        self.assertIn("player.attributes", str(ctx.exception))
        self.assertNotIn("player.age", str(ctx.exception))

    def test_unknown_and_malformed_feature_names_are_refused(self):
        with self.assertRaises(FeatureLineageError):
            assert_features_allowed({"player.first_name": "x"}, InformationMode.MANAGER_VISIBLE)
        with self.assertRaises(FeatureLineageError):
            assert_features_allowed({"attributes": {}}, InformationMode.MANAGER_VISIBLE)

    def test_visible_features_pass_and_bridge_observed_allows_all(self):
        assert_features_allowed({"player.age": 24, "finances.balance": 1, "fixture.date": "x"}, InformationMode.MANAGER_VISIBLE)
        assert_features_allowed({"player.attributes": {}, "anything": 1}, InformationMode.BRIDGE_OBSERVED)

    def test_declared_mask_extends_the_allowed_set(self):
        mask = VisibilityMask()
        mask.declare("player", "scouted_attributes", Visibility.VISIBLE)
        assert_features_allowed({"player.scouted_attributes": {}}, InformationMode.MANAGER_VISIBLE, mask)


class ModelModeTests(unittest.TestCase):
    def test_privileged_model_cannot_be_relabeled_manager_visible(self):
        """VIS 01: a model trained on bridge-observed data is not usable when manager-visible is requested."""
        self.assertFalse(mode_of_model("bridge_observed", InformationMode.MANAGER_VISIBLE))
        self.assertTrue(mode_of_model("manager_visible", InformationMode.MANAGER_VISIBLE))

    def test_bridge_observed_request_accepts_either_lineage(self):
        self.assertTrue(mode_of_model("manager_visible", InformationMode.BRIDGE_OBSERVED))
        self.assertTrue(mode_of_model("bridge_observed", InformationMode.BRIDGE_OBSERVED))


if __name__ == "__main__":
    unittest.main()
