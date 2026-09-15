"""Tests for separately sourced eligibility (SEL 01, OBS 02, spec 7.1/7.3)."""
from __future__ import annotations

import unittest

from ..rules.eligibility import (
    QUALITY_DECLARED, QUALITY_UI_UNVERIFIED, QUALITY_UI_VERIFIED, BRIDGE_LOAN_SOURCE,
    CompositeProvider, DeclaredEligibilityProvider, EligibilityObservation, EligibilityProvider, NoEligibilityProvider,
    bridge_loan_absence, component_observed, eligibility_summary, freshness_status, verified_eligible,
)
from ..state.status import Observed, ValueStatus
from ..state.views import missing_eligibility, player_state, squad_states, upcoming_fixtures
from . import fixtures as fx

NOW = "2026-01-01T00:00:00Z"


def build_snapshot(routes=("/squad", "/fixtures")):
    snap = fx.snapshot_for(routes)
    return snap.store, snap


def clear(pid, *, game_date=fx.GAME_DATE, game_time=fx.GAME_TIME, quality=QUALITY_UI_VERIFIED, source="ui:squad_screen", fixture_identity=None, registered=True):
    return EligibilityObservation.clear(pid, source=source, observed_at=NOW, game_date=game_date, game_time=game_time, quality=quality, fixture_identity=fixture_identity, registered=registered)


def injured(pid, description="Hamstring strain", *, game_date=fx.GAME_DATE, game_time=fx.GAME_TIME, quality=QUALITY_UI_VERIFIED, source="ui:squad_screen"):
    base = clear(pid, game_date=game_date, game_time=game_time, quality=quality, source=source)
    return EligibilityObservation(pid, None, Observed.available_value({"description": description, "expected_return": "2024-03-10"}, source, NOW, f"{game_date} {game_time}", "injury"), base.suspension, base.loan_absence, base.registration, source, NOW, game_date, game_time, quality)


class FreshnessTests(unittest.TestCase):
    def test_same_moment_is_current(self):
        self.assertEqual(freshness_status("2024-02-17", "10:00", "2024-02-17", "10:00"), (ValueStatus.AVAILABLE, None))

    def test_earlier_day_and_earlier_time_are_stale(self):
        status, reason = freshness_status("2024-02-16", "10:00", "2024-02-17", "10:00")
        self.assertIs(status, ValueStatus.STALE)
        self.assertIn("earlier", reason)
        status, reason = freshness_status("2024-02-17", "09:00", "2024-02-17", "10:00")
        self.assertIs(status, ValueStatus.STALE)

    def test_later_observation_is_not_silently_current(self):
        status, reason = freshness_status("2024-02-18", "10:00", "2024-02-17", "10:00")
        self.assertIs(status, ValueStatus.STALE)
        self.assertIn("later", reason)

    def test_unknown_dates_are_missing(self):
        self.assertIs(freshness_status(None, None, "2024-02-17", "10:00")[0], ValueStatus.MISSING)
        self.assertIs(freshness_status("2024-02-17", "10:00", None, None)[0], ValueStatus.MISSING)

    def test_sel01_time_less_same_day_reading_is_stale_not_current(self):
        """SEL 01 / spec 7.1: a reading with no game time on the snapshot's day may predate the snapshot time; it never verifies."""
        status, reason = freshness_status("2024-02-17", None, "2024-02-17", "15:00")
        self.assertIs(status, ValueStatus.STALE)
        self.assertIn("no game time", reason)
        self.assertIn("15:00", reason)

    def test_sel01_unknown_snapshot_time_cannot_verify_a_same_day_reading(self):
        """SEL 01: when the snapshot's own time is unknown nothing can be compared, so same-day readings are missing verification."""
        status, reason = freshness_status("2024-02-17", "10:00", "2024-02-17", None)
        self.assertIs(status, ValueStatus.MISSING)
        self.assertIn("no game time", reason)
        self.assertIs(freshness_status("2024-02-17", None, "2024-02-17", None)[0], ValueStatus.MISSING)
        # a different day is still reported stale first, whatever the times
        self.assertIs(freshness_status("2024-02-16", None, "2024-02-17", None)[0], ValueStatus.STALE)


class ProviderTests(unittest.TestCase):
    def test_no_provider_is_missing_and_keeps_default_shape(self):
        provider = NoEligibilityProvider()
        self.assertIsInstance(provider, EligibilityProvider)
        result = provider.for_player(1001, None)
        for key in missing_eligibility(1001):
            self.assertIn(key, result)
        self.assertEqual(result["status"], "missing")
        self.assertFalse(result["verified"])
        self.assertEqual(provider(1001, None)["status"], "missing")

    def test_declared_provider_verified_when_current_and_ui_verified(self):
        provider = DeclaredEligibilityProvider(fx.GAME_DATE, fx.GAME_TIME)
        provider.register(clear(1001))
        result = provider.for_player(1001, None)
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["verified"])
        self.assertEqual(result["injury"]["status"], "available")
        self.assertIsNone(result["injury"]["value"])
        self.assertEqual(result["injury"]["quality"], QUALITY_UI_VERIFIED)

    def test_declared_or_unverified_quality_is_unverified_not_verified(self):
        for quality in (QUALITY_DECLARED, QUALITY_UI_UNVERIFIED):
            provider = DeclaredEligibilityProvider(fx.GAME_DATE, fx.GAME_TIME)
            provider.register(clear(1001, quality=quality, source="operator"))
            result = provider.for_player(1001, None)
            self.assertEqual(result["status"], "unverified", quality)
            self.assertFalse(result["verified"])

    def test_earlier_game_time_observation_is_stale(self):
        provider = DeclaredEligibilityProvider(fx.GAME_DATE, fx.GAME_TIME)
        provider.register(clear(1001, game_date="2024-02-16"))
        result = provider.for_player(1001, None)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["injury"]["status"], "stale")
        self.assertIsNone(result["injury"]["value"])
        provider.register(clear(1002, game_time="09:30"))
        self.assertEqual(provider.for_player(1002, None)["status"], "stale")

    def test_positive_injury_is_ineligible_even_when_declared(self):
        provider = DeclaredEligibilityProvider(fx.GAME_DATE, fx.GAME_TIME)
        provider.register(injured(1001, quality=QUALITY_DECLARED, source="operator"))
        result = provider.for_player(1001, None)
        self.assertEqual(result["status"], "ineligible")
        self.assertIn("injury: Hamstring strain", result["reasons"][0])

    def test_partial_observation_is_missing_not_verified(self):
        provider = DeclaredEligibilityProvider(fx.GAME_DATE, fx.GAME_TIME)
        provider.register(clear(1001, registered=None))
        result = provider.for_player(1001, None)
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["registration"]["status"], "missing")
        self.assertEqual(result["injury"]["status"], "available")

    def test_fixture_specific_observation_applies_only_to_that_fixture(self):
        provider = DeclaredEligibilityProvider(fx.GAME_DATE, fx.GAME_TIME)
        cup = "2024-02-20T19:45 home:742 away:804 comp:33"
        provider.register(clear(1001, fixture_identity=cup, registered=False))
        self.assertEqual(provider.for_player(1001, {"identity": cup})["status"], "ineligible")
        self.assertEqual(provider.for_player(1001, {"identity": "other"})["status"], "missing")
        self.assertEqual(provider.for_player(1001, None)["status"], "missing")

    def test_fixture_specific_preferred_over_current(self):
        provider = DeclaredEligibilityProvider(fx.GAME_DATE, fx.GAME_TIME)
        cup = "2024-02-20T19:45 home:742 away:804 comp:33"
        provider.register(clear(1001))
        provider.register(clear(1001, fixture_identity=cup, registered=False))
        self.assertEqual(provider.for_player(1001, {"identity": cup})["status"], "ineligible")
        self.assertEqual(provider.for_player(1001, None)["status"], "verified")

    def test_latest_observation_wins(self):
        provider = DeclaredEligibilityProvider(fx.GAME_DATE, fx.GAME_TIME)
        provider.register(injured(1001, game_date="2024-02-10"))
        provider.register(clear(1001))
        self.assertEqual(provider.for_player(1001, None)["status"], "verified")

    def test_composite_first_non_missing_and_contradiction(self):
        ui = DeclaredEligibilityProvider(fx.GAME_DATE, fx.GAME_TIME)
        operator = DeclaredEligibilityProvider(fx.GAME_DATE, fx.GAME_TIME, name="operator")
        ui.register(clear(1001))
        operator.register(injured(1002, quality=QUALITY_DECLARED, source="operator"))
        composite = CompositeProvider([ui, operator])
        self.assertEqual(composite.for_player(1001, None)["status"], "verified")
        self.assertEqual(composite.for_player(1002, None)["status"], "ineligible")
        self.assertEqual(composite.for_player(1003, None)["status"], "missing")
        operator.register(injured(1001, quality=QUALITY_DECLARED, source="operator"))
        result = composite.for_player(1001, None)
        self.assertEqual(result["injury"]["status"], "contradicted")
        self.assertEqual(result["status"], "missing")
        self.assertFalse(result["verified"])

    def test_invalid_quality_rejected(self):
        with self.assertRaises(ValueError):
            clear(1001, quality="guessed")


class BridgeLoanTests(unittest.TestCase):
    def test_loan_to_other_club_is_positive_absence(self):
        employment = [{"kind": "employment", "club_id": 742, "start_date": "2023-07-01", "end_date": "2025-06-30"}, {"kind": "loan", "club_id": 803, "start_date": "2024-01-01", "end_date": "2024-05-31"}]
        result = bridge_loan_absence(employment, club_id=742, game_date=fx.GAME_DATE, loan_contracts_supported=True)
        self.assertTrue(result.available)
        self.assertIs(result.value, True)
        self.assertEqual(result.source, BRIDGE_LOAN_SOURCE)

    def test_expired_loan_does_not_count(self):
        employment = [{"kind": "employment", "club_id": 742}, {"kind": "loan", "club_id": 803, "start_date": "2023-08-01", "end_date": "2024-01-31"}]
        self.assertIs(bridge_loan_absence(employment, club_id=742, game_date=fx.GAME_DATE, loan_contracts_supported=True).value, False)

    def test_loaned_in_player_is_present(self):
        employment = [{"kind": "employment", "club_id": 803}, {"kind": "loan", "club_id": 742, "start_date": "2024-01-01", "end_date": "2024-05-31"}]
        self.assertIs(bridge_loan_absence(employment, club_id=742, game_date=fx.GAME_DATE, loan_contracts_supported=True).value, False)

    def test_absence_of_loan_is_unsupported_without_capability(self):
        result = bridge_loan_absence([{"kind": "employment", "club_id": 742}], club_id=742, game_date=fx.GAME_DATE, loan_contracts_supported=False)
        self.assertIs(result.status, ValueStatus.UNSUPPORTED)

    def test_unknown_club_id_cannot_tell_direction(self):
        employment = [{"kind": "employment", "club_id": 742}, {"kind": "loan", "club_id": 803}]
        self.assertIs(bridge_loan_absence(employment, club_id=None, game_date=fx.GAME_DATE, loan_contracts_supported=True).status, ValueStatus.MISSING)


class VerifiedEligibleTests(unittest.TestCase):
    def setUp(self):
        self.store, self.snap = build_snapshot()
        self.fixture = upcoming_fixtures(self.snap, 1)[0]
        self.kw = dict(snapshot_game_date=self.snap.game_date, snapshot_game_time=self.snap.game_time, club_id=742, loan_contracts_supported=True)

    def states(self, provider):
        return {s.player_id: s for s in squad_states(self.snap, eligibility=provider, fixture=self.fixture.to_json())}

    def test_default_provider_yields_missing_not_false(self):
        state = self.states(None)[1001]
        result = verified_eligible(state, self.fixture, **self.kw)
        self.assertFalse(result.available)
        self.assertIs(result.status, ValueStatus.MISSING)
        with self.assertRaises(Exception):
            result.require()

    def test_verified_true_only_with_all_four_verified_current(self):
        provider = DeclaredEligibilityProvider(self.snap.game_date, self.snap.game_time)
        provider.register(clear(1001))
        state = self.states(provider)[1001]
        result = verified_eligible(state, self.fixture, **self.kw)
        self.assertTrue(result.available)
        self.assertIs(result.value, True)

    def test_sel01_confirmed_ineligible_players_are_false_with_reason(self):
        provider = DeclaredEligibilityProvider(self.snap.game_date, self.snap.game_time)
        provider.register(injured(1001))
        base = clear(1002)
        provider.register(EligibilityObservation(1002, None, base.injury, Observed.available_value({"description": "1 match ban"}, base.source, NOW, base.injury.game_time, "suspension"), base.loan_absence, base.registration, base.source, NOW, base.game_date, base.game_time, base.quality))
        provider.register(clear(1003, registered=False))
        states = self.states(provider)
        for pid, text in ((1001, "injury"), (1002, "suspension"), (1003, "not registered")):
            result = verified_eligible(states[pid], self.fixture, **self.kw)
            self.assertTrue(result.available, pid)
            self.assertIs(result.value, False)
            self.assertIn(text, result.reason)

    def test_sel01_loaned_out_player_detected_from_bridge_contracts(self):
        payload = fx.player_payload(1099, "Loanee Out", ["ST"], 2, 1000, "Good")
        payload["contracts"].append({"kind": "loan", "club_id": 803, "club_name": "Oxford", "start_date": "2024-01-01", "end_date": "2024-05-31"})
        provider = DeclaredEligibilityProvider(self.snap.game_date, self.snap.game_time)
        state = player_state(payload, eligibility=NoEligibilityProvider()(1099, None))
        result = verified_eligible(state, self.fixture, **self.kw)
        self.assertTrue(result.available)
        self.assertIs(result.value, False)
        self.assertIn("loaned out", result.reason)
        self.assertIn(BRIDGE_LOAN_SOURCE, result.source)
        # a provider reading that says "present" contradicts the bridge -> not trusted either way
        provider.register(clear(1099))
        state = player_state(payload, eligibility=provider(1099, None))
        result = verified_eligible(state, self.fixture, **self.kw)
        self.assertIs(result.status, ValueStatus.CONTRADICTED)

    def test_stale_observation_is_stale_not_true(self):
        provider = DeclaredEligibilityProvider(self.snap.game_date, self.snap.game_time)
        provider.register(clear(1001, game_date="2024-02-16"))
        result = verified_eligible(self.states(provider)[1001], self.fixture, **self.kw)
        self.assertIs(result.status, ValueStatus.STALE)

    def test_sel01_time_less_clear_reading_cannot_verify_a_starter(self):
        """SEL 01: an EligibilityObservation.clear(..., game_time=None) from earlier the same day is stale at 10:00, not verified."""
        provider = DeclaredEligibilityProvider(self.snap.game_date, self.snap.game_time)
        provider.register(clear(1001, game_time=None))
        state = self.states(provider)[1001]
        self.assertEqual(state.eligibility["status"], "stale")
        result = verified_eligible(state, self.fixture, **self.kw)
        self.assertFalse(result.available)
        self.assertIs(result.status, ValueStatus.STALE)
        self.assertIn("no game time", result.reason)
        # the ??:?? placeholder in a stored component key is the same time-less reading
        self.assertTrue(all(state.eligibility[name]["game_time"].endswith("??:??") for name in ("injury", "suspension", "loan_absence", "registration")))
        # a positive absence read without a time is stale like any other unfresh reading: not proven, and not submittable either way
        provider = DeclaredEligibilityProvider(self.snap.game_date, self.snap.game_time)
        provider.register(injured(1002, game_time=None))
        result = verified_eligible(self.states(provider)[1002], self.fixture, **self.kw)
        self.assertFalse(result.available)
        self.assertIs(result.status, ValueStatus.STALE)

    def test_reverification_against_a_later_snapshot_time(self):
        provider = DeclaredEligibilityProvider(self.snap.game_date, self.snap.game_time)
        provider.register(clear(1001))
        state = self.states(provider)[1001]
        self.assertEqual(state.eligibility["status"], "verified")
        later = dict(self.kw, snapshot_game_date="2024-02-18")
        result = verified_eligible(state, self.fixture, **later)
        self.assertIs(result.status, ValueStatus.STALE)

    def test_declared_clear_is_missing_verification(self):
        provider = DeclaredEligibilityProvider(self.snap.game_date, self.snap.game_time)
        provider.register(clear(1001, quality=QUALITY_DECLARED, source="operator"))
        result = verified_eligible(self.states(provider)[1001], self.fixture, **self.kw)
        self.assertIs(result.status, ValueStatus.MISSING)
        self.assertIn("not by a verifying source", result.reason)

    def test_bridge_loan_absence_fills_missing_component_only(self):
        provider = DeclaredEligibilityProvider(self.snap.game_date, self.snap.game_time)
        base = clear(1001)
        provider.register(EligibilityObservation(1001, None, base.injury, base.suspension, Observed.unavailable(ValueStatus.MISSING, "loan_absence", "not shown", base.source), base.registration, base.source, NOW, base.game_date, base.game_time, base.quality))
        state = self.states(provider)[1001]
        self.assertEqual(state.eligibility["status"], "missing")
        result = verified_eligible(state, self.fixture, **self.kw)
        self.assertIs(result.value, True)
        result = verified_eligible(state, self.fixture, **dict(self.kw, loan_contracts_supported=False))
        self.assertIs(result.status, ValueStatus.UNSUPPORTED)

    def test_summary_counts(self):
        provider = DeclaredEligibilityProvider(self.snap.game_date, self.snap.game_time)
        provider.register(clear(1001))
        provider.register(injured(1002))
        provider.register(clear(1003, game_date="2024-02-01"))
        summary = eligibility_summary(self.states(provider).values())
        self.assertEqual(summary["verified"], 1)
        self.assertEqual(summary["ineligible"], 1)
        self.assertEqual(summary["stale"], 1)
        self.assertEqual(summary["missing"], 21)
        self.assertEqual(summary["total"], 24)

    def test_component_observed_roundtrip(self):
        comp, quality = component_observed(missing_eligibility(1), "injury")
        self.assertIs(comp.status, ValueStatus.MISSING)
        self.assertIsNone(quality)


if __name__ == "__main__":
    unittest.main()
