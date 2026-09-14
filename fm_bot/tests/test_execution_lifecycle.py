"""Tests for fm_bot.execution.lifecycle (spec 12.2, 12.3, BOT 006, ACT 01)."""
from __future__ import annotations

import unittest

from ..execution.lifecycle import IntentFactory, LifecycleError, context_changes, duplicate_of, enqueue, expire_if_context_changed, idempotency_key, transition, validate
from ..rules.authority import AuthorityMode, AuthorityProfile
from ..rules.capabilities import ACTION_REQUIREMENTS
from ..state.records import ActionState, ConsistencyStatus
from ..state.store import StoreError
from .execution_fixtures import harness


class IntentFactoryTests(unittest.TestCase):
    def test_create_copies_entity_versions_and_builds_key(self):
        h = harness()
        snap = h.snapshot()
        intent = h.tactic_intent(snap, decision_id="dec-7")
        self.assertIs(intent.state, ActionState.PROPOSED)
        self.assertEqual(intent.entity_versions, {"/tactics": snap.entity_versions["/tactics"]})
        self.assertTrue(intent.idempotency_key.startswith(f"{h.branch_id}:dec-7:select_validated_tactic:"))
        self.assertEqual(len(intent.idempotency_key.split(":")[-1]), 16)
        self.assertEqual(intent.required_capabilities, ACTION_REQUIREMENTS["select_validated_tactic"])
        self.assertEqual(h.store.get_intent(intent.action_id).idempotency_key, intent.idempotency_key)
        self.assertEqual(intent.idempotency_key, idempotency_key(h.branch_id, "dec-7", "select_validated_tactic", intent.targets, intent.parameters))

    def test_key_falls_back_to_snapshot_id_without_decision(self):
        h = harness()
        snap = h.snapshot()
        intent = h.factory.create("navigate", "tactics.view", snap, {}, {"target": "tactics"}, verification="navigation_only", risk_class="navigation")
        self.assertIn(f":{snap.snapshot_id}:navigate:", intent.idempotency_key)
        self.assertEqual(intent.entity_versions, {})

    def test_same_decision_never_dispatches_twice(self):
        h = harness()
        snap = h.snapshot()
        first = h.tactic_intent(snap, decision_id="dec-dup")
        with self.assertRaises(StoreError):
            h.tactic_intent(snap, decision_id="dec-dup")
        unsaved = IntentFactory(None).create("select_validated_tactic", "tactics.select", snap, first.targets, first.parameters, verification=first.verification, decision_id="dec-dup")
        self.assertEqual(duplicate_of(h.store, unsaved).action_id, first.action_id)
        self.assertIsNone(duplicate_of(h.store, first))

    def test_refuses_invalid_snapshot_unobserved_route_and_bad_risk_class(self):
        h = harness()
        snap = h.snapshot()
        with self.assertRaises(LifecycleError):
            h.factory.create("select_validated_tactic", "tactics.select", snap, {"routes": ["/inbox"]}, {}, verification="selected_tactic_matches_catalog")
        with self.assertRaises(LifecycleError):
            h.factory.create("navigate", "tactics.view", snap, {}, {}, verification="navigation_only", risk_class="reckless")
        snap.consistency = ConsistencyStatus.TIME_CHANGED
        with self.assertRaises(LifecycleError):
            h.factory.create("navigate", "tactics.view", snap, {}, {}, verification="navigation_only")


class TransitionTests(unittest.TestCase):
    def test_transition_persists_and_refuses_illegal_moves(self):
        h = harness()
        intent = h.tactic_intent(h.snapshot())
        transition(h.store, intent, ActionState.VALIDATED, "ok")
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.VALIDATED)
        with self.assertRaises(StoreError):
            transition(h.store, intent, ActionState.EXECUTING, "skipping the queue")
        kinds = [e["body"]["to"] for e in h.store.journal_entries("intent.transition", intent.action_id)]
        self.assertEqual(kinds, ["VALIDATED"])


class ExpiryTests(unittest.TestCase):
    def test_changed_tactic_on_same_date_expires_intent(self):
        h = harness()
        intent = h.tactic_intent(h.snapshot())
        unchanged = h.snapshot()
        self.assertEqual(context_changes(intent, unchanged), [])
        self.assertFalse(expire_if_context_changed(intent, unchanged, h.store))
        h.adapter.selected_tactic_id = "counter-02"     # someone changed the tactic in the UI; the bridge now reports it
        changed = h.snapshot()
        self.assertEqual(changed.game_date, unchanged.game_date)
        reasons = context_changes(intent, changed)
        self.assertEqual(len(reasons), 1)
        self.assertIn("/tactics changed", reasons[0])
        self.assertTrue(expire_if_context_changed(intent, changed, h.store))
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.EXPIRED)

    def test_branch_mismatch_and_inconsistent_snapshot_count_as_changes(self):
        h = harness()
        snap = h.snapshot()
        intent = h.tactic_intent(snap)
        other = h.snapshot()
        other.branch_id = "branch-other"
        self.assertTrue(any("career/branch" in r for r in context_changes(intent, other)))
        stale = h.snapshot()
        stale.consistency = ConsistencyStatus.SESSION_CHANGED
        self.assertTrue(any("not consistent" in r for r in context_changes(intent, stale)))


class ValidateTests(unittest.TestCase):
    def test_validated_inside_scope_with_capabilities(self):
        h = harness()
        snap = h.snapshot()
        intent = h.tactic_intent(snap)
        result = validate(intent, h.registry, h.profile, snap, h.store)
        self.assertTrue(result.ok)
        self.assertFalse(result.capability_report.blocked)
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.VALIDATED)
        enqueue(h.store, intent)
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.QUEUED)

    def test_missing_capability_blocks_with_named_report(self):
        h = harness(adapter_capabilities=False)
        snap = h.snapshot()
        intent = h.tactic_intent(snap)
        result = validate(intent, h.registry, h.profile, snap, h.store)
        self.assertIs(result.state, ActionState.OUTSIDE_SCOPE)
        self.assertIn("ui_action_adapter", result.capability_report.missing)
        self.assertIn("tactic_selection_ui", result.authorization.missing_capabilities)
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.OUTSIDE_SCOPE)

    def test_advise_profile_is_outside_scope(self):
        h = harness(profile=AuthorityProfile(AuthorityMode.ADVISE))
        snap = h.snapshot()
        intent = h.tactic_intent(snap)
        result = validate(intent, h.registry, h.profile, snap, h.store)
        self.assertIs(result.state, ActionState.OUTSIDE_SCOPE)
        self.assertTrue(any("advise" in r for r in result.reasons))

    def test_stale_context_expires_before_authority(self):
        h = harness()
        intent = h.tactic_intent(h.snapshot())
        h.adapter.selected_tactic_id = "counter-02"
        result = validate(intent, h.registry, h.profile, h.snapshot(), h.store)
        self.assertIs(result.state, ActionState.EXPIRED)
        self.assertIs(h.store.get_intent(intent.action_id).state, ActionState.EXPIRED)
        with self.assertRaises(LifecycleError):
            validate(intent, h.registry, h.profile, h.snapshot(), h.store)


if __name__ == "__main__":
    unittest.main()
