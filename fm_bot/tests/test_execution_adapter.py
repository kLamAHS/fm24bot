"""Tests for fm_bot.execution.adapter (spec 12.1, ACT 03)."""
from __future__ import annotations

import unittest

from ..execution.adapter import (
    ANY_SCREEN, FAKE_SCREEN_MODEL, FAKE_WORKFLOWS, FAULT_KINDS, STEP_DONE, STEP_ILLEGAL, STEP_NO_FOCUS, STEP_STOPPED, STEP_TIMEOUT,
    STEP_UNEXPECTED_SCREEN, STEP_UNKNOWN_SCREEN, STEP_UNSUPPORTED, AdapterCrash, FakeAdapter, ScreenObservation, StopFlag, UIAdapter, UIStep,
    WindowsAdapter, validate_environment,
)
from ..state.status import ValueStatus
from .execution_fixtures import OFFERS

NAV_TACTICS = UIStep("go", "navigate", ANY_SCREEN, {"target": "tactics"}, "navigation", "tactics")
SELECT = UIStep("select", "select_tactic", "tactics", {"tactic_catalog_id": "counter-02"}, "consequential", "tactics")


class FakeAdapterTests(unittest.TestCase):
    def test_protocol_and_initial_screen(self):
        adapter = FakeAdapter()
        self.assertIsInstance(adapter, UIAdapter)
        observation = adapter.identify_screen()
        self.assertEqual(observation.screen_id, "home")
        self.assertTrue(observation.identified)
        self.assertEqual(observation.method, "fake")
        self.assertIn("ui_action_adapter", adapter.capabilities())

    def test_navigation_then_selection_changes_state_and_readback(self):
        adapter = FakeAdapter()
        self.assertEqual(adapter.perform(NAV_TACTICS).status, STEP_DONE)
        self.assertEqual(adapter.identify_screen().screen_id, "tactics")
        result = adapter.perform(SELECT)
        self.assertTrue(result.ok)
        readback = adapter.readback("selected_tactic")
        self.assertTrue(readback.available)
        self.assertEqual(readback.value, {"tactic_id": "counter-02", "name": "4-2-3-1 Counter"})
        self.assertEqual(len(adapter.inputs), 2)

    def test_selection_is_illegal_on_home_and_sends_nothing(self):
        adapter = FakeAdapter()
        result = adapter.perform(SELECT)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, STEP_UNEXPECTED_SCREEN)
        self.assertEqual(adapter.inputs, [])
        illegal = adapter.perform(UIStep("x", "accept_offer", ANY_SCREEN, {"offer_id": "offer-1"}))
        self.assertEqual(illegal.status, STEP_ILLEGAL)
        self.assertEqual(adapter.inputs, [])

    def test_stop_flag_refuses_next_input(self):
        adapter = FakeAdapter()
        adapter.stop("operator pressed stop")
        result = adapter.perform(NAV_TACTICS)
        self.assertEqual(result.status, STEP_STOPPED)
        self.assertIn("operator pressed stop", result.error)
        self.assertEqual(adapter.inputs, [])
        self.assertEqual(adapter.identify_screen().screen_id, "home")

    def test_no_focus_refuses_input(self):
        adapter = FakeAdapter(focused=False)
        self.assertEqual(adapter.perform(NAV_TACTICS).status, STEP_NO_FOCUS)
        self.assertEqual(adapter.inputs, [])

    def test_unknown_screen_refuses_input(self):
        adapter = FakeAdapter(screen="mystery")
        observation = adapter.identify_screen()
        self.assertIsNone(observation.screen_id)
        self.assertIn("not in the model", observation.reason)
        self.assertEqual(adapter.perform(NAV_TACTICS).status, STEP_UNKNOWN_SCREEN)
        self.assertEqual(adapter.inputs, [])

    def test_fault_timeout_after_success_applies_effect(self):
        adapter = FakeAdapter()
        adapter.perform(NAV_TACTICS)
        adapter.inject("timeout_after_success", on_step=2)
        result = adapter.perform(SELECT)
        self.assertEqual(result.status, STEP_TIMEOUT)
        self.assertEqual(adapter.selected_tactic_id, "counter-02", "the effect landed even though confirmation timed out")

    def test_fault_timeout_before_effect_leaves_state(self):
        adapter = FakeAdapter()
        adapter.perform(NAV_TACTICS)
        adapter.inject("timeout_before_effect", on_step=2)
        self.assertEqual(adapter.perform(SELECT).status, STEP_TIMEOUT)
        self.assertEqual(adapter.selected_tactic_id, "balanced-01")
        self.assertEqual(len(adapter.inputs), 1)

    def test_fault_focus_loss_unexpected_screen_and_crash(self):
        adapter = FakeAdapter()
        adapter.inject("focus_loss", on_step=1)
        self.assertEqual(adapter.perform(NAV_TACTICS).status, STEP_NO_FOCUS)
        self.assertFalse(adapter.has_focus())
        adapter.focused = True
        adapter.inject("unexpected_screen", on_step=2)
        result = adapter.perform(NAV_TACTICS)
        self.assertEqual(result.status, STEP_UNEXPECTED_SCREEN)
        self.assertEqual(result.screen_after.screen_id, "unexpected")
        adapter.inject("crash", on_step=3)
        with self.assertRaises(AdapterCrash):
            adapter.perform(UIStep("home", "navigate", ANY_SCREEN, {"target": "home"}, "navigation"))
        with self.assertRaises(ValueError):
            adapter.inject("meteor")
        self.assertEqual(len(FAULT_KINDS), 5)

    def test_agreement_readback_distinguishes_null_missing_available(self):
        adapter = FakeAdapter(offers=OFFERS)
        self.assertIs(adapter.readback("agreement", {"offer_id": "offer-1"}).status, ValueStatus.NULL)
        self.assertIs(adapter.readback("agreement", {"offer_id": "offer-9"}).status, ValueStatus.MISSING)
        self.assertIs(adapter.readback("nonsense").status, ValueStatus.UNSUPPORTED)
        for step in FAKE_WORKFLOWS["commit.contract"].instantiate({"offer_id": "offer-1"}):
            self.assertTrue(adapter.perform(step).ok, step)
        agreement = adapter.readback("agreement", {"offer_id": "offer-1"})
        self.assertTrue(agreement.available)
        self.assertEqual(agreement.value["agreement_id"], "agr-1")
        again = adapter.perform(UIStep("accept", "accept_offer", "inbox.offer", {"offer_id": "offer-1"}))
        self.assertFalse(again.ok, "the fake UI is back on the inbox; a second acceptance is not legal there")

    def test_readback_fault_reports_status_not_value(self):
        adapter = FakeAdapter()
        adapter.inject_readback("selected_tactic", ValueStatus.STALE, "tactics screen not refreshed")
        readback = adapter.readback("selected_tactic")
        self.assertIs(readback.status, ValueStatus.STALE)
        self.assertIsNone(readback.value)


class ScreenModelTests(unittest.TestCase):
    def test_workflow_instantiation_fills_only_missing_params(self):
        steps = FAKE_WORKFLOWS["select_validated_tactic"].instantiate({"tactic_catalog_id": "counter-02", "catalog_version": 3, "ignored": 1})
        self.assertEqual(steps[0].params, {"target": "tactics"})
        self.assertEqual(steps[1].params, {"tactic_catalog_id": "counter-02", "catalog_version": 3})
        self.assertEqual(steps[1].risk_class, "consequential")
        self.assertEqual(FAKE_WORKFLOWS["select_validated_tactic"].screen_model.version, FAKE_SCREEN_MODEL.version)

    def test_legal_actions_and_transitions(self):
        self.assertTrue(FAKE_SCREEN_MODEL.legal("tactics", "select_tactic"))
        self.assertFalse(FAKE_SCREEN_MODEL.legal("home", "select_tactic"))
        self.assertFalse(FAKE_SCREEN_MODEL.legal(None, "navigate"))
        self.assertTrue(FAKE_SCREEN_MODEL.legal("unexpected", "navigate"), "recovery navigation is legal on the unexpected screen")
        self.assertEqual(FAKE_SCREEN_MODEL.expected_after("inbox.offer", "accept_offer", {}), "inbox")
        self.assertEqual(FAKE_SCREEN_MODEL.expected_after("home", "navigate", {"target": "training"}), "training")

    def test_validate_environment_reports_each_deviation(self):
        good = FakeAdapter().identify_screen()
        self.assertEqual(validate_environment(good, FAKE_SCREEN_MODEL), [])
        bad = ScreenObservation(None, 0.0, {"width": 1280, "height": 720}, 1.25, "de", "purple", False, "now", "fake", "no match")
        problems = validate_environment(bad, FAKE_SCREEN_MODEL)
        joined = " | ".join(problems)
        for fragment in ("scaling 1.25", "1280x720", "language 'de'", "skin 'purple'", "focus", "unidentified"):
            self.assertIn(fragment, joined)
        without_focus = validate_environment(bad, FAKE_SCREEN_MODEL, require_focus=False)
        self.assertEqual(len(without_focus), len(problems) - 1)
        low = ScreenObservation("home", 0.5, {"width": 1920, "height": 1080}, 1.0, "en", "default", True, "now", "screen_recognition")
        self.assertEqual(len(validate_environment(low, FAKE_SCREEN_MODEL)), 1)
        self.assertIn("confidence", validate_environment(low, FAKE_SCREEN_MODEL)[0])

    def test_stop_flag_round_trip(self):
        flag = StopFlag()
        self.assertFalse(flag.is_set())
        flag.set("because")
        self.assertTrue(flag.is_set())
        self.assertEqual(flag.reason, "because")
        flag.clear()
        self.assertFalse(flag.is_set())


class WindowsAdapterTests(unittest.TestCase):
    def test_fail_closed_everywhere(self):
        adapter = WindowsAdapter(library_name="definitely_not_installed_lib")
        self.assertIsInstance(adapter, UIAdapter)
        self.assertEqual(adapter.capabilities(), [])
        observation = adapter.identify_screen()
        self.assertIsNone(observation.screen_id)
        self.assertIn("untested", observation.reason)
        self.assertFalse(adapter.has_focus())
        self.assertEqual(adapter.perform(NAV_TACTICS).status, STEP_UNSUPPORTED)
        self.assertIs(adapter.readback("selected_tactic").status, ValueStatus.UNSUPPORTED)
        self.assertTrue(validate_environment(observation, FAKE_SCREEN_MODEL))


if __name__ == "__main__":
    unittest.main()
