"""Tests for the capability registry and per-action gates (spec 3.2, BOT 005)."""
from __future__ import annotations

import unittest

from ..rules.capabilities import ACTION_REQUIREMENTS, PROPOSED_CAPABILITIES, CapabilityRegistry, CapabilityStatus
from . import fixtures as fx


def registry(**kw) -> CapabilityRegistry:
    return CapabilityRegistry.from_status(fx.status_payload(**kw), supported_builds=(fx.BUILD,))


class FromStatusTests(unittest.TestCase):
    def test_bridge_capabilities_are_supported_and_unresolved_are_unsupported(self):
        reg = registry()
        self.assertTrue(reg.connected)
        self.assertEqual(reg.build, fx.BUILD)
        self.assertIs(reg.status("player_attributes_47"), CapabilityStatus.SUPPORTED)
        self.assertEqual(reg.capabilities["player_attributes_47"].provider, "bridge")
        self.assertIs(reg.status("inbox_text"), CapabilityStatus.UNSUPPORTED)
        self.assertIn("unresolved", reg.capabilities["inbox_text"].note)

    def test_proposed_capabilities_start_missing(self):
        reg = registry()
        for name in ("eligibility_injury", "ui_action_adapter", "save_restore", "language_model"):
            self.assertIn(name, PROPOSED_CAPABILITIES)
            self.assertIs(reg.status(name), CapabilityStatus.MISSING)
            self.assertEqual(reg.capabilities[name].provider, "none")
        self.assertIs(reg.status("never-heard-of"), CapabilityStatus.MISSING)

    def test_disconnected_bridge_degrades_nothing_to_supported(self):
        reg = registry(connected=False)
        self.assertFalse(reg.connected)
        self.assertFalse(reg.supported("player_attributes_47"))
        self.assertIs(reg.status("player_attributes_47"), CapabilityStatus.MISSING)   # a disconnected bridge lists nothing
        self.assertTrue(reg.check("advise.lineup").blocked)

    def test_unsupported_build_marks_bridge_capabilities_degraded(self):
        reg = registry(build="24.5.0+1")
        self.assertIs(reg.status("player_attributes_47"), CapabilityStatus.DEGRADED)
        self.assertIn("build unsupported", reg.capabilities["player_attributes_47"].note)
        self.assertFalse(reg.supported("player_attributes_47"))

    def test_connected_false_with_listed_capabilities_is_degraded(self):
        payload = fx.status_payload()
        payload["connected"] = False
        reg = CapabilityRegistry.from_status(payload, supported_builds=(fx.BUILD,))
        self.assertIs(reg.status("team_roster"), CapabilityStatus.DEGRADED)

    def test_no_status_payload_is_all_missing(self):
        reg = CapabilityRegistry.from_status(None, supported_builds=(fx.BUILD,))
        self.assertFalse(reg.connected)
        self.assertEqual(reg.summary()["supported"], [])
        self.assertEqual(set(reg.summary()["missing"]), set(PROPOSED_CAPABILITIES))


class CheckTests(unittest.TestCase):
    def test_gates_block_only_actions_that_need_missing_capabilities(self):
        """Advising a lineup needs only bridge data; submitting it needs eligibility and the UI adapter."""
        reg = registry()
        advise = reg.check("advise.lineup")
        self.assertFalse(advise.blocked)
        self.assertEqual(advise.missing, [])
        submit = reg.check("submit.lineup")
        self.assertTrue(submit.blocked)
        self.assertEqual(submit.blocked_action, "submit.lineup")
        self.assertEqual(submit.missing, ["ui_action_adapter", "eligibility_injury", "eligibility_suspension", "eligibility_loan_absence", "eligibility_registration", "competition_rules"])
        self.assertIn("missing (none)", submit.reasons["eligibility_injury"])

    def test_unsupported_bridge_field_blocks_inbox_responses_only(self):
        reg = registry()
        reg.provide("ui_action_adapter", "ui_adapter", "validated")
        reg.provide("pending_actions", "ui_adapter")
        respond = reg.check("respond.inbox")
        self.assertEqual(respond.missing, ["inbox_text"])
        self.assertIn("unsupported (bridge)", respond.reasons["inbox_text"])
        self.assertFalse(reg.check("progress.continue").blocked)

    def test_observe_needs_nothing(self):
        self.assertFalse(registry().check("observe").blocked)
        self.assertEqual(ACTION_REQUIREMENTS["observe"], [])

    def test_unknown_action_falls_back_to_its_family(self):
        reg = registry()
        self.assertEqual(reg.requirements("advise.lineup.alternative"), [])   # no family "advise" entry
        self.assertEqual(reg.requirements("observe.anything"), [])
        self.assertEqual(reg.requirements("not.a.kind"), [])

    def test_extra_requirements_are_checked_too(self):
        reg = registry()
        report = reg.check("advise.lineup", extra=["language_model"])
        self.assertEqual(report.missing, ["language_model"])

    def test_report_is_serialisable(self):
        report = registry().check("lab.restore_checkpoint")
        self.assertEqual(report.to_json()["missing"], ["save_restore", "career_registration"])


class ProvideWithdrawTests(unittest.TestCase):
    def test_provide_then_withdraw(self):
        reg = registry()
        for name in ("ui_action_adapter", "eligibility_injury", "eligibility_suspension", "eligibility_loan_absence", "eligibility_registration", "competition_rules"):
            reg.provide(name, "ui_adapter", "validated on build")
        self.assertFalse(reg.check("submit.lineup").blocked)
        self.assertEqual(reg.capabilities["ui_action_adapter"].provider, "ui_adapter")
        reg.withdraw("ui_action_adapter", "focus lost")
        report = reg.check("submit.lineup")
        self.assertEqual(report.missing, ["ui_action_adapter"])
        self.assertIs(reg.status("ui_action_adapter"), CapabilityStatus.DEGRADED)
        self.assertEqual(reg.capabilities["ui_action_adapter"].provider, "ui_adapter")   # provider remembered
        self.assertIn("focus lost", report.reasons["ui_action_adapter"])
        self.assertFalse(reg.check("advise.lineup").blocked)   # unrelated work keeps going

    def test_withdraw_unknown_capability_records_degraded_with_no_provider(self):
        reg = registry()
        reg.withdraw("save_restore", "workflow not validated")
        self.assertEqual(reg.capabilities["save_restore"].provider, "none")
        self.assertIs(reg.status("save_restore"), CapabilityStatus.DEGRADED)

    def test_summary_groups_by_status(self):
        reg = registry()
        reg.provide("ui_action_adapter", "ui_adapter")
        summary = reg.summary()
        self.assertIn("ui_action_adapter", summary["supported"])
        self.assertIn("inbox_text", summary["unsupported"])
        self.assertIn("save_restore", summary["missing"])
        self.assertEqual(summary["degraded"], [])


if __name__ == "__main__":
    unittest.main()


class MatchParticipantTests(unittest.TestCase):
    def test_mat02_retained_remnants_and_unknown_substitution_rules_block_match_actions_only(self):
        """MAT 02: the fixture bridge exposes only retained match remnants (last positions, retained condition) and no verified
        event order or substitution rules, so a substitution is blocked naming match_event_order and competition_rules, while
        observing the match and advising a lineup are not. Supplying the UI adapter alone does not unblock it."""
        reg = registry()
        self.assertTrue(reg.supported("match_player_positions"))
        self.assertTrue(reg.supported("match_retained_condition"))
        self.assertTrue(reg.supported("match_viewer"))
        report = reg.check("match.substitute")
        self.assertTrue(report.blocked)
        self.assertEqual(report.blocked_action, "match.substitute")
        for name in ("match_event_order", "competition_rules"):
            self.assertIn(name, report.missing)
        self.assertNotIn("match_viewer", report.missing)
        self.assertFalse(reg.check("advise.lineup").blocked)
        self.assertFalse(reg.check("observe").blocked)
        reg.provide("ui_action_adapter", "ui_adapter:fake")
        self.assertEqual(sorted(reg.check("match.substitute").missing), ["competition_rules", "match_event_order"])
