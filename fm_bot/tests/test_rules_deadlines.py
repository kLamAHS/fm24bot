"""Tests for pending actions and the Continue gate (CAL 01, spec 12.4)."""
from __future__ import annotations

import unittest

from ..bridge_client.transport import FakeTransport
from ..rules.authority import AuthorityMode
from ..rules.capabilities import ACTION_REQUIREMENTS, CapabilityRegistry
from ..rules.competitions import CompetitionRules, RulesProfileRegistry
from ..rules.deadlines import (
    CLASSIFICATION_INFORMATIONAL, CLASSIFICATION_MANDATORY_CONFIRMED, CLASSIFICATION_MANDATORY_HEURISTIC, CLASSIFICATION_UNCLASSIFIED,
    CLASSIFICATION_UNRESOLVED_READ, MANDATORY_EVENT_PATTERNS, MANDATORY_EVENT_PATTERNS_VERSION, ContinueGate, DecisionBoundary, LineupStatus,
    PendingAction, PendingActionsObservation, classify_event_type, continue_gate, next_decision_boundary, pending_actions, registration_actions,
    resolve_read_actions,
)
from ..state.records import ConsistencyStatus
from ..state.status import Observed
from ..state.store import Store
from ..state.views import upcoming_fixtures
from . import fixtures as fx

ROUTES = ["/squad", "/fixtures", "/inbox", "/tactics"]


def build_snapshot(*, inbox=None, game_date=fx.GAME_DATE, routes=ROUTES, fixtures=None):
    overrides = {}
    if inbox is not None:
        overrides["/inbox"] = FakeTransport.envelope(inbox, session_id=fx.SESSION)
    if fixtures is not None:
        overrides["/fixtures"] = FakeTransport.envelope(fixtures, session_id=fx.SESSION)
    snap = fx.snapshot_for(routes, overrides=overrides, game_date=game_date, require_valid=False)
    return snap.store, snap


def inbox_with(changes):
    inbox = fx.inbox_payload()
    for message in inbox["messages"]:
        if message["id"] in changes:
            message.update(changes[message["id"]])
    inbox["unread_count"] = sum(1 for m in inbox["messages"] if m["unread"])
    return inbox


def full_registry(*, inbox_text: bool = False) -> CapabilityRegistry:
    """Everything progress.continue needs, provided by a (test) UI adapter."""
    registry = CapabilityRegistry.from_status(fx.status_payload(), supported_builds=[fx.BUILD])
    for name in ACTION_REQUIREMENTS["progress.continue"]:
        registry.provide(name, "ui_adapter", "test double")
    if inbox_text:
        registry.provide("inbox_text", "ui_adapter", "test double")
    return registry


def rules_with_deadline(date: str, competition_id: int = 33, *, resolved: bool = False) -> CompetitionRules:
    rules = CompetitionRules.unknown(competition_id, "current", competition_name="Cup")
    rules.source = "operator"
    rules.registration_windows = Observed.available_value([{"opens": "2024-02-10", "closes": date, "kind": "cup_squad", "resolved": resolved}], "operator", what="registration_windows")
    return rules


class PatternTests(unittest.TestCase):
    def test_patterns_are_versioned_and_match_known_types(self):
        self.assertTrue(MANDATORY_EVENT_PATTERNS_VERSION.startswith("mandatory-event-patterns/"))
        self.assertIn("offer", classify_event_type("news_item_transfer_offer"))
        self.assertEqual(classify_event_type("news_item_board_meeting_request"), ["board_meeting", "request"])
        self.assertEqual(classify_event_type("news_item_training"), [])
        self.assertEqual(classify_event_type(None), [])
        self.assertTrue(all(p == p.lower() for p in MANDATORY_EVENT_PATTERNS))


class PendingActionTests(unittest.TestCase):
    def test_fixture_inbox_yields_two_heuristic_blockers(self):
        _, snap = build_snapshot()
        actions = {a.action_id: a for a in pending_actions(snap)}
        self.assertEqual(actions["inbox:501"].classification, CLASSIFICATION_MANDATORY_HEURISTIC)
        self.assertTrue(actions["inbox:501"].blocks_continue)
        self.assertEqual(actions["inbox:501"].requires_capability, "inbox_text")
        self.assertEqual(actions["inbox:501"].text_status, "not_decoded")
        self.assertEqual(actions["inbox:503"].matched_patterns, ["board_meeting", "request"])
        self.assertEqual(actions["inbox:502"].classification, CLASSIFICATION_INFORMATIONAL)
        self.assertFalse(actions["inbox:502"].blocks_continue)
        self.assertIsInstance(actions["inbox:501"].to_json(), dict)

    def test_read_mandatory_looking_message_still_blocks(self):
        """CAL 01: a read message that looks like a decision is not proven answered; it blocks and names pending_actions."""
        _, snap = build_snapshot(inbox=inbox_with({501: {"unread": False}}))
        actions = {a.action_id: a for a in pending_actions(snap)}
        self.assertEqual(actions["inbox:501"].classification, CLASSIFICATION_UNRESOLVED_READ)
        self.assertTrue(actions["inbox:501"].blocks_continue)
        self.assertFalse(actions["inbox:501"].resolved)
        self.assertEqual(actions["inbox:501"].requires_capability, "pending_actions")

    def test_unread_unmatched_message_is_unclassified(self):
        _, snap = build_snapshot(inbox=inbox_with({502: {"unread": True, "event_type": "news_item_something_new"}}))
        actions = {a.action_id: a for a in pending_actions(snap)}
        self.assertEqual(actions["inbox:502"].classification, CLASSIFICATION_UNCLASSIFIED)
        self.assertFalse(actions["inbox:502"].blocks_continue)

    def test_text_provider_overrides_heuristic(self):
        _, snap = build_snapshot()
        provider = lambda m: {"requires_decision": m["id"] == 501, "text": "offer for Vokes", "options": ["accept", "reject"], "deadline": "2024-02-19"} if m["id"] in (501, 503) else None  # noqa: E731
        actions = {a.action_id: a for a in pending_actions(snap, provider)}
        self.assertEqual(actions["inbox:501"].classification, CLASSIFICATION_MANDATORY_CONFIRMED)
        self.assertEqual(actions["inbox:501"].options, ["accept", "reject"])
        self.assertEqual(actions["inbox:501"].deadline_date, "2024-02-19")
        self.assertEqual(actions["inbox:503"].classification, CLASSIFICATION_INFORMATIONAL)
        self.assertTrue(actions["inbox:503"].resolved)

    def test_no_inbox_route_yields_nothing(self):
        _, snap = build_snapshot(routes=["/fixtures"])
        self.assertEqual(pending_actions(snap), [])

    def test_registration_actions_window(self):
        registry = RulesProfileRegistry(Store.memory())
        registry.store_profile(rules_with_deadline("2024-02-22"))
        registry.store_profile(rules_with_deadline("2024-03-30", competition_id=14))
        _, snap = build_snapshot()
        contexts = registry.contexts_for(upcoming_fixtures(snap, 2))
        actions = registration_actions(contexts, snap.game_date)
        by_id = {a.action_id: a for a in actions}
        self.assertTrue(by_id["registration_deadline:33:current:2024-02-22"].blocks_continue)
        self.assertFalse(by_id["registration_deadline:14:current:2024-03-30"].blocks_continue)
        self.assertEqual(registration_actions(contexts, None), [])
        past = registration_actions(registry.contexts_for(upcoming_fixtures(snap, 1)), "2024-02-23")
        self.assertEqual(past, [])


class ContinueGateTests(unittest.TestCase):
    def test_cal01_unread_required_decision_blocks_continue_until_resolved(self):
        registry = full_registry()
        _, snap = build_snapshot()
        gate = continue_gate(snap, pending_actions(snap), [], LineupStatus("not_required"), authority_mode=AuthorityMode.CLUB_AUTONOMY, capabilities=registry)
        self.assertIsInstance(gate, ContinueGate)
        self.assertFalse(gate.allowed)
        self.assertTrue(any("inbox:501" in b for b in gate.blockers))
        self.assertTrue(any("inbox:503" in b for b in gate.blockers))
        self.assertIn("inbox_text", gate.missing_capabilities.missing)
        self.assertIn("inbox:501", gate.missing_capabilities.reasons["inbox_text"])
        self.assertIn("inbox:503", gate.missing_capabilities.reasons["inbox_text"])
        # resolved: the messages are no longer unread and a text provider confirms nothing is pending
        _, resolved = build_snapshot(inbox=inbox_with({501: {"unread": False}, 503: {"unread": False}}))
        provider = lambda m: {"requires_decision": False}  # noqa: E731
        gate = continue_gate(resolved, pending_actions(resolved, provider), [], LineupStatus("not_required"), authority_mode=AuthorityMode.CLUB_AUTONOMY, capabilities=registry)
        self.assertTrue(gate.allowed, gate.to_json())
        self.assertEqual(gate.blockers, [])
        self.assertFalse(gate.missing_capabilities.blocked)

    def test_cal01_read_but_unconfirmed_messages_stay_visible(self):
        """CAL 01 / OBS 02: read mandatory-looking messages keep blocking Continue; "read" is not "resolved".

        The registry supplying ``pending_actions`` is not enough on its own: a
        resolution has to be *observed*. Without an observation the gate is
        blocked and the messages are visible as blockers; with an observation
        from a registry that does not support ``pending_actions`` the
        observation is ignored (and said so); a stale observation is not
        current for the snapshot and proves nothing.
        """
        _, snap = build_snapshot(inbox=inbox_with({501: {"unread": False}, 503: {"unread": False}}))
        pending = pending_actions(snap)
        gate = continue_gate(snap, pending, [], LineupStatus("not_required"), authority_mode=AuthorityMode.CLUB_AUTONOMY, capabilities=full_registry())
        self.assertFalse(gate.allowed, gate.to_json())
        self.assertEqual(sorted(b.split(":")[1] for b in gate.blockers), ["501", "503"])
        self.assertTrue(all("cannot be verified" in b for b in gate.blockers))
        self.assertFalse(gate.missing_capabilities.blocked, "the capability is provided; what is missing is the observation")
        self.assertTrue(all(p.blocks_continue and not p.resolved for p in gate.pending if p.kind == "inbox_message" and p.message_id in (501, 503)))
        # without the capability the read messages also surface pending_actions as the missing subsystem
        without = CapabilityRegistry.from_status(fx.status_payload(), supported_builds=[fx.BUILD])
        for name in ("ui_action_adapter", "inbox_metadata"):
            without.provide(name, "ui_adapter", "test double")
        gate = continue_gate(snap, pending, [], LineupStatus("not_required"), authority_mode=AuthorityMode.CLUB_AUTONOMY, capabilities=without)
        self.assertFalse(gate.allowed)
        self.assertIn("pending_actions", gate.missing_capabilities.missing)
        self.assertIn("inbox:501", gate.missing_capabilities.reasons["pending_actions"])
        # an observation is ignored when the capability is unsupported
        observation = PendingActionsObservation(frozenset({"inbox:501", "inbox:503"}), "ui:pending_actions", fx.GAME_DATE, fx.GAME_TIME, "2026-01-01T00:00:00Z")
        gate = continue_gate(snap, pending, [], LineupStatus("not_required"), authority_mode=AuthorityMode.CLUB_AUTONOMY, capabilities=without, pending_observation=observation)
        self.assertFalse(gate.allowed)
        self.assertTrue(any("ignored" in n and "pending_actions" in n for n in gate.notes), gate.notes)
        # an observation from another in-game moment proves nothing
        stale = PendingActionsObservation(frozenset({"inbox:501", "inbox:503"}), "ui:pending_actions", fx.GAME_DATE, "09:00")
        gate = continue_gate(snap, pending, [], LineupStatus("not_required"), authority_mode=AuthorityMode.CLUB_AUTONOMY, capabilities=full_registry(), pending_observation=stale)
        self.assertFalse(gate.allowed)
        self.assertTrue(any("not the snapshot game time" in n for n in gate.notes), gate.notes)
        undated = PendingActionsObservation(frozenset({"inbox:501", "inbox:503"}), "ui:pending_actions", fx.GAME_DATE, None)
        self.assertFalse(continue_gate(snap, pending, [], LineupStatus("not_required"), authority_mode=AuthorityMode.CLUB_AUTONOMY, capabilities=full_registry(), pending_observation=undated).allowed)
        self.assertTrue(all(p.blocks_continue and not p.resolved for p in pending if p.message_id in (501, 503)), "the caller's pending list is never mutated")

    def test_cal01_read_messages_resolve_only_on_a_current_pending_actions_observation(self):
        """CAL 01: with pending_actions supported and a current observation naming the message, the gate opens."""
        _, snap = build_snapshot(inbox=inbox_with({501: {"unread": False}, 503: {"unread": False}}))
        pending = pending_actions(snap)
        observation = PendingActionsObservation(frozenset({"inbox:501"}), "ui:pending_actions", fx.GAME_DATE, fx.GAME_TIME)
        gate = continue_gate(snap, pending, [], LineupStatus("not_required"), authority_mode=AuthorityMode.CLUB_AUTONOMY, capabilities=full_registry(), pending_observation=observation)
        self.assertFalse(gate.allowed, "503 was not reported resolved")
        self.assertEqual([b.split(":")[1] for b in gate.blockers], ["503"])
        resolved = {p.action_id: p for p in gate.pending}
        self.assertTrue(resolved["inbox:501"].resolved)
        self.assertFalse(resolved["inbox:501"].blocks_continue)
        self.assertIn("reported resolved by ui:pending_actions", resolved["inbox:501"].description)
        self.assertFalse(resolved["inbox:503"].resolved)
        both = PendingActionsObservation(frozenset({"inbox:501", "inbox:503"}), "ui:pending_actions", fx.GAME_DATE, fx.GAME_TIME)
        gate = continue_gate(snap, pending, [], LineupStatus("not_required"), authority_mode=AuthorityMode.CLUB_AUTONOMY, capabilities=full_registry(), pending_observation=both)
        self.assertTrue(gate.allowed, gate.to_json())
        self.assertEqual(gate.blockers, [])
        self.assertEqual(len([n for n in gate.notes if "reported resolved" in n]), 2)
        self.assertIsInstance(both.to_json()["resolved_action_ids"], list)
        # an unread message is never resolved by the observation: it still has to be read and answered
        _, unread = build_snapshot()
        actions, notes = resolve_read_actions(unread, pending_actions(unread), both, capabilities=full_registry())
        self.assertTrue(all(a.blocks_continue and not a.resolved for a in actions if a.message_id in (501, 503)))
        self.assertEqual(notes, [])

    def test_missing_capabilities_without_registry_come_from_snapshot(self):
        _, snap = build_snapshot()
        gate = continue_gate(snap, pending_actions(snap), [], None)
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.missing_capabilities.blocked_action, "progress.continue")
        self.assertIn("ui_action_adapter", gate.missing_capabilities.missing)
        self.assertIn("pending_actions", gate.missing_capabilities.missing)
        self.assertNotIn("inbox_metadata", gate.missing_capabilities.missing)
        self.assertIn("reported unresolved by the bridge", gate.missing_capabilities.reasons["inbox_text"])

    def test_missing_inbox_route_blocks(self):
        _, snap = build_snapshot(routes=["/fixtures"])
        gate = continue_gate(snap, [], [], LineupStatus("not_required"), capabilities=full_registry())
        self.assertFalse(gate.allowed)
        self.assertTrue(any("inbox metadata" in b for b in gate.blockers))
        self.assertIn("inbox_metadata", gate.missing_capabilities.missing)

    def test_invalid_snapshot_blocks(self):
        _, snap = build_snapshot(inbox=inbox_with({501: {"unread": False}, 503: {"unread": False}}))
        snap.consistency = ConsistencyStatus.TIME_CHANGED
        snap.consistency_reasons = ["game time advanced during collection"]
        gate = continue_gate(snap, [], [], LineupStatus("not_required"), capabilities=full_registry())
        self.assertFalse(gate.allowed)
        self.assertIn("not consistent", gate.blockers[0])

    def _quiet(self, game_date):
        return build_snapshot(inbox=inbox_with({501: {"unread": False}, 503: {"unread": False}}), game_date=game_date)

    def test_unverified_lineup_blocks_execution_modes_before_fixture_day(self):
        _, snap = self._quiet("2024-02-19")     # cup fixture is 2024-02-20
        pending = pending_actions(snap, lambda m: {"requires_decision": False})
        for mode in (AuthorityMode.SCOPED_EXECUTION, AuthorityMode.CLUB_AUTONOMY):
            gate = continue_gate(snap, pending, [], LineupStatus("unverified", unverified_players=[1004]), authority_mode=mode, capabilities=full_registry())
            self.assertFalse(gate.allowed, mode)
            self.assertIn("lineup for 2024-02-20T19:45", gate.blockers[0])
        gate = continue_gate(snap, pending, [], LineupStatus("unverified"), authority_mode=AuthorityMode.ADVISE, capabilities=full_registry())
        self.assertTrue(gate.allowed)
        self.assertTrue(any(n.startswith("advisory: lineup") for n in gate.notes))

    def test_verified_lineup_for_next_fixture_passes(self):
        _, snap = self._quiet("2024-02-20")
        pending = pending_actions(snap, lambda m: {"requires_decision": False})
        fixture = upcoming_fixtures(snap, 1)[0]
        gate = continue_gate(snap, pending, [], LineupStatus("verified", fixture_identity=fixture.identity), authority_mode=AuthorityMode.CLUB_AUTONOMY, capabilities=full_registry())
        self.assertTrue(gate.allowed, gate.to_json())
        gate = continue_gate(snap, pending, [], {"status": "verified", "fixture_identity": "some other fixture"}, authority_mode=AuthorityMode.CLUB_AUTONOMY, capabilities=full_registry())
        self.assertFalse(gate.allowed)
        self.assertIn("not for the next fixture", gate.blockers[0])

    def test_lineup_far_from_fixture_does_not_block(self):
        _, snap = self._quiet("2024-02-17")     # three days before the cup tie
        pending = pending_actions(snap, lambda m: {"requires_decision": False})
        gate = continue_gate(snap, pending, [], LineupStatus("unverified"), authority_mode=AuthorityMode.CLUB_AUTONOMY, capabilities=full_registry())
        self.assertTrue(gate.allowed, gate.to_json())

    def test_ineligible_lineup_blocks_in_every_mode_on_fixture_day(self):
        _, snap = self._quiet("2024-02-20")
        pending = pending_actions(snap, lambda m: {"requires_decision": False})
        gate = continue_gate(snap, pending, [], LineupStatus("ineligible", reasons=["1004 suspended"]), authority_mode=AuthorityMode.ADVISE, capabilities=full_registry())
        self.assertFalse(gate.allowed)
        self.assertIn("1004 suspended", gate.blockers[0])

    def test_registration_deadline_within_window_blocks(self):
        registry = RulesProfileRegistry(Store.memory())
        registry.store_profile(rules_with_deadline("2024-02-22"))
        _, snap = self._quiet("2024-02-17")
        contexts = registry.contexts_for(upcoming_fixtures(snap, 1))
        pending = pending_actions(snap, lambda m: {"requires_decision": False})
        gate = continue_gate(snap, pending, contexts, LineupStatus("not_required"), capabilities=full_registry())
        self.assertFalse(gate.allowed)
        self.assertIn("registration_deadline:33:current:2024-02-22", gate.blockers[0])
        self.assertEqual(gate.next_boundary.kind, "fixture")   # fixture on 02-20 precedes the 02-22 deadline
        registry.store_profile(rules_with_deadline("2024-02-22", resolved=True))
        gate = continue_gate(snap, pending, registry.contexts_for(upcoming_fixtures(snap, 1)), LineupStatus("not_required"), capabilities=full_registry())
        self.assertTrue(gate.allowed, gate.to_json())

    def test_missing_rules_profile_is_a_note_not_a_block(self):
        _, snap = self._quiet("2024-02-17")
        contexts = RulesProfileRegistry(Store.memory()).contexts_for(upcoming_fixtures(snap, 1))
        gate = continue_gate(snap, pending_actions(snap, lambda m: {"requires_decision": False}), contexts, LineupStatus("not_required"), capabilities=full_registry())
        self.assertTrue(gate.allowed)
        self.assertTrue(any("rules profile missing" in n for n in gate.notes))
        self.assertIn("next_boundary", gate.to_json())


class BoundaryTests(unittest.TestCase):
    def test_next_fixture_is_the_boundary(self):
        _, snap = build_snapshot()
        boundary = next_decision_boundary(snap)
        self.assertIsInstance(boundary, DecisionBoundary)
        self.assertEqual((boundary.kind, boundary.date, boundary.time), ("fixture", "2024-02-20", "19:45"))
        self.assertFalse(boundary.truncated_horizon)

    def test_earlier_pending_deadline_wins(self):
        _, snap = build_snapshot()
        action = PendingAction("inbox:501", "inbox_message", CLASSIFICATION_MANDATORY_CONFIRMED, True, "offer expires", "bridge:/inbox", deadline_date="2024-02-19")
        boundary = next_decision_boundary(snap, [action])
        self.assertEqual((boundary.kind, boundary.date, boundary.identity), ("pending_action", "2024-02-19", "inbox:501"))
        stale = PendingAction("inbox:9", "inbox_message", CLASSIFICATION_MANDATORY_CONFIRMED, True, "old", "bridge:/inbox", deadline_date="2024-02-01")
        self.assertEqual(next_decision_boundary(snap, [stale]).kind, "fixture")

    def test_rules_deadline_before_fixture(self):
        registry = RulesProfileRegistry(Store.memory())
        registry.store_profile(rules_with_deadline("2024-02-18"))
        _, snap = build_snapshot()
        boundary = next_decision_boundary(snap, [], registry.contexts_for(upcoming_fixtures(snap, 1)))
        self.assertEqual((boundary.kind, boundary.date), ("deadline", "2024-02-18"))

    def test_unknown_when_nothing_scheduled(self):
        fixtures = fx.fixtures_payload()
        fixtures["fixtures"] = [f for f in fixtures["fixtures"] if f["status"] == "played"]
        _, snap = build_snapshot(fixtures=fixtures, game_date="2024-12-20")
        boundary = next_decision_boundary(snap)
        self.assertEqual(boundary.kind, "unknown")
        self.assertTrue(boundary.truncated_horizon)
        snap.game_date = None
        self.assertEqual(next_decision_boundary(snap).kind, "unknown")


if __name__ == "__main__":
    unittest.main()
