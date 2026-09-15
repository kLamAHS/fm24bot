"""Tests for the authority profile and authorization (spec 1.2, BOT 006)."""
from __future__ import annotations

import json
import unittest

from ..rules.authority import (
    ACTION_FAMILIES, CLUB_WORKFLOW_FAMILIES, Allowed, AuthorityLimits, AuthorityMode, AuthorityProfile, OutsideScope,
    authorize,
)
from ..state.records import ActionIntent
from ..state.status import MissingCapabilityReport
from ..state.units import Money, Period


def intent(scope="tactics.select", kind="select_validated_tactic", parameters=None) -> ActionIntent:
    return ActionIntent("act-1", kind, scope, "career-a", "branch-a", "snap-1", {}, parameters or {}, [], [], "relevant_state_change", "readback", "key-1")


def scoped(*families, **limits) -> AuthorityProfile:
    return AuthorityProfile(AuthorityMode.SCOPED_EXECUTION, set(families), AuthorityLimits(**limits), version=3)


FULL_LIMITS = dict(max_weekly_wage_commitment=Money.native_gbp(5000, Period.WEEKLY), max_total_fee_commitment=Money.native_gbp(500_000), max_total_conditional_commitment=Money.native_gbp(100_000), min_cash_reserve=Money.native_gbp(1_000_000), max_contract_years=3)


class ModeTests(unittest.TestCase):
    def test_observe_mode_allows_no_game_action(self):
        result = authorize(intent(), AuthorityProfile(AuthorityMode.OBSERVE, {"tactics"}))
        self.assertIsInstance(result, OutsideScope)
        self.assertFalse(result.allowed)
        self.assertIn("observe", result.reasons[0])

    def test_advise_mode_allows_no_game_action(self):
        result = authorize(intent(), AuthorityProfile(AuthorityMode.ADVISE, {"tactics"}))
        self.assertFalse(result.allowed)
        self.assertIn("recommendations only", result.reasons[0])

    def test_scoped_execution_allows_enabled_family_only(self):
        profile = scoped("tactics", "selection")
        allowed = authorize(intent("tactics.select"), profile)
        self.assertIsInstance(allowed, Allowed)
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.profile_version, 3)
        self.assertIn("tactics", allowed.notes[0])
        refused = authorize(intent("transfers.offer"), profile)
        self.assertFalse(refused.allowed)
        self.assertIn("'transfers' is not enabled", refused.reasons[0])

    def test_club_autonomy_covers_the_validated_workflow(self):
        profile = AuthorityProfile(AuthorityMode.CLUB_AUTONOMY, set(), AuthorityLimits(allow_continue=True))
        for family in CLUB_WORKFLOW_FAMILIES:
            self.assertTrue(authorize(intent(f"{family}.anything"), profile).allowed, family)

    def test_laboratory_is_never_part_of_club_autonomy(self):
        profile = AuthorityProfile(AuthorityMode.CLUB_AUTONOMY, set(ACTION_FAMILIES), AuthorityLimits(allow_continue=True))
        result = authorize(intent("laboratory.restore", "lab.restore_checkpoint"), profile)
        self.assertFalse(result.allowed)
        self.assertIn("never part of the club autonomy workflow", result.reasons[0])
        self.assertNotIn("laboratory", CLUB_WORKFLOW_FAMILIES)

    def test_laboratory_can_be_enabled_under_scoped_execution(self):
        self.assertTrue(authorize(intent("laboratory.restore", "lab.restore_checkpoint"), scoped("laboratory")).allowed)

    def test_unknown_family_outside_club_workflow_is_refused(self):
        profile = AuthorityProfile(AuthorityMode.CLUB_AUTONOMY, set())
        self.assertFalse(authorize(intent("hacking.memory"), profile).allowed)


class PrerequisiteTests(unittest.TestCase):
    def test_unknown_prerequisite_blocks_even_when_family_enabled(self):
        report = MissingCapabilityReport("submit.lineup")
        report.add("eligibility_injury", "no provider registered")
        result = authorize(intent("selection.submit", "submit.lineup"), scoped("selection"), report)
        self.assertFalse(result.allowed)
        self.assertEqual(result.missing_capabilities, ["eligibility_injury"])
        self.assertIn("unknown prerequisite blocks submit.lineup", result.reasons[0])

    def test_empty_report_does_not_block(self):
        self.assertTrue(authorize(intent("selection.submit"), scoped("selection"), MissingCapabilityReport("submit.lineup")).allowed)


class LimitTests(unittest.TestCase):
    def test_weekly_wage_within_limit_is_allowed(self):
        params = {"weekly_wage": Money.native_gbp(4800, Period.WEEKLY).to_json(), "contract_years": 2}
        result = authorize(intent("contracts.offer", "offer_contract", params), scoped("contracts", **FULL_LIMITS))
        self.assertTrue(result.allowed)

    def test_weekly_wage_over_limit_reports_exact_terms(self):
        params = {"weekly_wage": Money.native_gbp(5001, Period.WEEKLY)}
        result = authorize(intent("contracts.offer", "offer_contract", params), scoped("contracts", **FULL_LIMITS))
        self.assertFalse(result.allowed)
        self.assertEqual(result.exact_terms["weekly_wage"], "GBP 5,001.00/week")
        self.assertIn("exceeds the profile limit GBP 5,000.00/week", result.reasons[0])

    def test_weekly_wage_must_carry_weekly_period(self):
        params = {"weekly_wage": Money.native_gbp(4000, Period.MONTHLY)}
        result = authorize(intent("contracts.offer", "offer_contract", params), scoped("contracts", **FULL_LIMITS))
        self.assertIn("must carry a weekly period", result.reasons[0])

    def test_missing_limit_refuses_the_commitment(self):
        """A money commitment with no configured limit is outside scope, never implicitly unlimited."""
        for key, money in (("weekly_wage", Money.native_gbp(1, Period.WEEKLY)), ("total_fee", Money.native_gbp(1)), ("conditional_total", Money.native_gbp(1))):
            result = authorize(intent("transfers.offer", "make_offer", {key: money}), scoped("transfers"))
            self.assertFalse(result.allowed, key)
            self.assertIn("no", result.reasons[0])
            self.assertIn("limit", result.reasons[0])

    def test_total_fee_limit_and_period(self):
        profile = scoped("transfers", **FULL_LIMITS)
        self.assertTrue(authorize(intent("transfers.offer", "make_offer", {"total_fee": Money.native_gbp(500_000)}), profile).allowed)
        over = authorize(intent("transfers.offer", "make_offer", {"total_fee": Money.native_gbp(500_001)}), profile)
        self.assertIn("total fee GBP 500,001.00 exceeds", over.reasons[0])
        bad_period = authorize(intent("transfers.offer", "make_offer", {"total_fee": Money.native_gbp(1000, Period.MONTHLY)}), profile)
        self.assertIn("one-off total of all guaranteed instalments", bad_period.reasons[0])

    def test_conditional_total_limit(self):
        profile = scoped("transfers", **FULL_LIMITS)
        result = authorize(intent("transfers.offer", "make_offer", {"conditional_total": Money.native_gbp(100_001)}), profile)
        self.assertFalse(result.allowed)
        self.assertIn("conditional commitments", result.reasons[0])

    def test_contract_years_limit(self):
        profile = scoped("contracts", **FULL_LIMITS)
        self.assertTrue(authorize(intent("contracts.offer", "offer_contract", {"contract_years": 3}), profile).allowed)
        result = authorize(intent("contracts.offer", "offer_contract", {"contract_years": 4}), profile)
        self.assertIn("contract length 4 years exceeds", result.reasons[0])
        self.assertEqual(result.exact_terms["contract_years"], 4)
        # Without a configured year limit, contract length alone is not a refusal.
        self.assertTrue(authorize(intent("contracts.offer", "offer_contract", {"contract_years": 9}), scoped("contracts")).allowed)

    def test_release_sale_and_continue_flags(self):
        self.assertFalse(authorize(intent("transfers.release", "release_player"), scoped("transfers")).allowed)
        self.assertTrue(authorize(intent("transfers.release", "release_player"), scoped("transfers", allow_player_release=True)).allowed)
        self.assertFalse(authorize(intent("transfers.sale", "accept_sale"), scoped("transfers")).allowed)
        self.assertTrue(authorize(intent("transfers.sale", "accept_sale"), scoped("transfers", allow_player_sale=True)).allowed)
        self.assertFalse(authorize(intent("progression.continue", "progress.continue"), scoped("progression")).allowed)
        self.assertTrue(authorize(intent("progression.continue", "progress.continue"), scoped("progression", allow_continue=True)).allowed)

    def test_unrecognized_clauses_or_ambiguous_payer_refuse(self):
        profile = scoped("contracts", **FULL_LIMITS)
        result = authorize(intent("contracts.offer", "offer_contract", {"unrecognized_clauses": ["relegation release"]}), profile)
        self.assertIn("unrecognized clauses", result.reasons[0])
        result = authorize(intent("contracts.offer", "offer_contract", {"ambiguous_payer": True}), profile)
        self.assertIn("ambiguous payer", result.reasons[0])

    def test_all_reasons_are_collected(self):
        params = {"weekly_wage": Money.native_gbp(9000, Period.WEEKLY), "total_fee": Money.native_gbp(900_000), "contract_years": 5}
        result = authorize(intent("contracts.offer", "offer_contract", params), scoped("contracts", **FULL_LIMITS))
        self.assertEqual(len(result.reasons), 3)
        self.assertEqual(set(result.exact_terms), {"weekly_wage", "total_fee", "contract_years"})

    def test_money_terms_must_be_money(self):
        with self.assertRaises(TypeError):
            authorize(intent("contracts.offer", "offer_contract", {"weekly_wage": 4000}), scoped("contracts", **FULL_LIMITS))


class ProfileSerialisationTests(unittest.TestCase):
    def test_json_round_trip_preserves_limits_and_families(self):
        profile = AuthorityProfile(AuthorityMode.SCOPED_EXECUTION, {"tactics", "contracts"}, AuthorityLimits(**FULL_LIMITS, allow_continue=True), version=4, delegation={"media": "assistant"}, label="season-1")
        data = json.loads(json.dumps(profile.to_json()))
        restored = AuthorityProfile.from_json(data)
        self.assertEqual(restored, profile)
        self.assertEqual(data["enabled_families"], ["contracts", "tactics"])
        self.assertEqual(data["limits"]["max_weekly_wage_commitment"], {"minor": 500_000, "currency": "GBP", "period": "weekly"})
        self.assertIsNone(AuthorityLimits().to_json()["min_cash_reserve"])

    def test_defaults_and_version_bump(self):
        profile = AuthorityProfile()
        self.assertIs(profile.mode, AuthorityMode.ADVISE)
        self.assertEqual(profile.version, 1)
        self.assertIs(profile.bump(), profile)
        self.assertEqual(profile.version, 2)
        self.assertEqual(AuthorityProfile.from_json({"mode": "observe"}).version, 1)

    def test_authorization_reports_profile_version(self):
        profile = scoped("tactics").bump()
        self.assertEqual(authorize(intent(), profile).profile_version, 4)


if __name__ == "__main__":
    unittest.main()
