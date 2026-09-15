"""Tests for missing-value semantics (spec 5.3, OBS 02)."""
from __future__ import annotations

import unittest

from ..state.status import (
    MissingCapabilityReport, Observed, Unavailable, ValueStatus, contradicted, missing, null, stale, unsupported,
)


class ValueStatusTests(unittest.TestCase):
    def test_only_available_is_usable(self):
        self.assertTrue(ValueStatus.AVAILABLE.usable)
        for status in (ValueStatus.NULL, ValueStatus.MISSING, ValueStatus.STALE, ValueStatus.UNSUPPORTED, ValueStatus.CONTRADICTED):
            self.assertFalse(status.usable, status)


class ObservedInvariantTests(unittest.TestCase):
    def test_available_value_carries_value_and_provenance(self):
        obs = Observed.available_value(93.0, "obs-1", observed_at="2026-01-01T00:00:00Z", game_time="2024-02-17 10:00", what="condition")
        self.assertTrue(obs.available)
        self.assertEqual(obs.require(), 93.0)
        self.assertEqual(obs.source, "obs-1")
        self.assertEqual(obs.what, "condition")
        self.assertIsNone(obs.reason)

    def test_non_available_status_cannot_carry_a_value(self):
        """Null is not zero and stale is not a number: a non-available observation never smuggles a value."""
        with self.assertRaises(ValueError):
            Observed(0, ValueStatus.NULL, what="balance")
        with self.assertRaises(ValueError):
            Observed(93.0, ValueStatus.STALE, what="condition")
        self.assertIsNone(Observed.unavailable(ValueStatus.NULL, "balance").value)

    def test_unavailable_refuses_available_status(self):
        with self.assertRaises(ValueError):
            Observed.unavailable(ValueStatus.AVAILABLE, "x")

    def test_require_raises_unavailable_with_status_and_reason(self):
        """OBS 02: requiring a stale value raises with the specific status instead of guessing."""
        obs = Observed.unavailable(ValueStatus.STALE, "condition", "cache dated 2024-02-10", source="obs-9")
        self.assertFalse(obs.available)
        with self.assertRaises(Unavailable) as ctx:
            obs.require()
        self.assertIs(ctx.exception.status, ValueStatus.STALE)
        self.assertEqual(ctx.exception.what, "condition")
        self.assertEqual(ctx.exception.reason, "cache dated 2024-02-10")
        self.assertIn("condition is stale: cache dated 2024-02-10", str(ctx.exception))

    def test_unavailable_is_a_lookup_error(self):
        self.assertTrue(issubclass(Unavailable, LookupError))

    def test_map_only_applies_to_available_values(self):
        doubled = Observed.available_value(2, "s", what="n").map(lambda v: v * 2)
        self.assertEqual(doubled.require(), 4)
        self.assertEqual(doubled.source, "s")
        untouched = missing("n", "not collected").map(lambda v: v * 2)
        self.assertIs(untouched.status, ValueStatus.MISSING)
        self.assertIsNone(untouched.value)

    def test_helpers_produce_the_named_status(self):
        self.assertIs(null("balance").status, ValueStatus.NULL)
        self.assertIs(missing("balance").status, ValueStatus.MISSING)
        self.assertIs(stale("condition").status, ValueStatus.STALE)
        self.assertIs(unsupported("inbox.text").status, ValueStatus.UNSUPPORTED)
        self.assertIs(contradicted("age").status, ValueStatus.CONTRADICTED)
        self.assertEqual(missing("balance", "route not collected", source="snap-1").source, "snap-1")

    def test_to_json_keeps_status_explicit(self):
        data = stale("condition", "old cache", source="obs-3").to_json()
        self.assertEqual(data["status"], "stale")
        self.assertIsNone(data["value"])
        self.assertEqual(data["reason"], "old cache")
        self.assertEqual(data["what"], "condition")

    def test_observed_is_immutable(self):
        obs = Observed.available_value(1)
        with self.assertRaises(Exception):
            obs.value = 2  # type: ignore[misc]


class MissingCapabilityReportTests(unittest.TestCase):
    def test_empty_report_is_not_blocked(self):
        report = MissingCapabilityReport("advise.lineup")
        self.assertFalse(report.blocked)
        self.assertEqual(report.to_json(), {"blocked_action": "advise.lineup", "missing": [], "reasons": {}})

    def test_add_names_capability_once_and_keeps_latest_reason(self):
        report = MissingCapabilityReport("submit.lineup")
        report.add("eligibility_injury", "no provider registered")
        report.add("eligibility_injury", "provider withdrawn")
        report.add("ui_action_adapter", "not installed")
        self.assertTrue(report.blocked)
        self.assertEqual(report.missing, ["eligibility_injury", "ui_action_adapter"])
        self.assertEqual(report.reasons["eligibility_injury"], "provider withdrawn")
        self.assertEqual(report.to_json()["missing"], ["eligibility_injury", "ui_action_adapter"])


if __name__ == "__main__":
    unittest.main()
