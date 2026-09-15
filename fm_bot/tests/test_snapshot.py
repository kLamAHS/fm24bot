"""Tests for the consistent snapshot protocol (spec 5.2, BOT 003, OBS 01-03)."""
from __future__ import annotations

import unittest

from ..bridge_client.client import BridgeClient
from ..bridge_client.transport import FakeTransport
from ..state.identity import Anchor, CareerRegistry, SaveManifest
from ..state.records import ConsistencyStatus, payload_hash
from ..state.snapshot import CollectionContext, SnapshotCollector, SnapshotRequirements, anchor_of
from ..state.store import Store
from . import fixtures as fx

ROUTES = ["/squad", "/finances", "/fixtures"]


def env(data, session=fx.SESSION):
    return FakeTransport.envelope(data, session_id=session)


class Harness:
    """A registered career with a scripted world; ``collect`` runs the protocol against it."""

    def __init__(self, overrides=None, *, attempts=3):
        self.store = Store.memory()
        self.career, self.branch, _ = CareerRegistry(self.store).register_career("t", SaveManifest(fx.BUILD, 90001, 742, fx.GAME_DATE, fx.GAME_TIME))
        self.transport = fx.transport(overrides=overrides)
        self.client = BridgeClient(self.transport, self.store, context={"career_id": self.career.career_id, "branch_id": self.branch.branch_id})
        self.collector = SnapshotCollector(self.client, self.store, attempts=attempts)

    def context(self, **kw) -> CollectionContext:
        kw.setdefault("lineage_confirmed", True)
        return CollectionContext(self.career.career_id, self.branch.branch_id, **kw)

    def collect(self, routes=ROUTES, *, context=None, **req):
        return self.collector.collect(SnapshotRequirements(routes=list(routes), **req), context or self.context())

    def calls(self, route: str) -> int:
        return self.transport.calls.count(route)


class ConsistentCollectionTests(unittest.TestCase):
    def test_consistent_snapshot_carries_routes_versions_and_anchor(self):
        h = Harness()
        snap = h.collect(player_ids=[1001])
        self.assertTrue(snap.valid)
        self.assertIs(snap.consistency, ConsistencyStatus.CONSISTENT)
        self.assertEqual(snap.attempts, 1)
        self.assertEqual((snap.game_date, snap.game_time, snap.session_id), (fx.GAME_DATE, fx.GAME_TIME, fx.SESSION))
        self.assertEqual((snap.manager_id, snap.club_id), (90001, 742))
        self.assertEqual(set(snap.routes), {"/manager", "/club", "/squad", "/finances", "/fixtures", "/players/1001"})
        self.assertEqual(snap.entity_versions["/squad"], payload_hash(snap.routes["/squad"]))
        self.assertIn("player_attributes_47", snap.capabilities)
        self.assertEqual(snap.continuity["status"], "continuous")
        anchor = anchor_of(snap)
        self.assertEqual((anchor.manager_id, anchor.club_id, anchor.build, anchor.sequence), (90001, 742, fx.BUILD, 1))
        # /game is read before and after the routes; identity routes are reread too.
        self.assertEqual(h.calls("/game"), 2)
        self.assertEqual(h.calls("/manager"), 2)

    def test_collector_journals_the_snapshot_and_its_observations(self):
        h = Harness()
        snap = h.collect()
        stored = h.store.get_snapshot(snap.snapshot_id)
        self.assertIsNotNone(stored)
        self.assertTrue(stored.valid)
        self.assertEqual(stored.routes["/finances"]["balance"], 10909005)
        self.assertEqual(len(h.store.journal_entries(kind="snapshot", ref_id=snap.snapshot_id)), 1)
        self.assertGreater(len(snap.observation_ids), 0)
        self.assertEqual(len(snap.observation_ids), len(set(snap.observation_ids)))
        for observation_id in snap.observation_ids:
            self.assertIsNotNone(h.store.get_observation(observation_id, with_payload=False))

    def test_optional_route_unavailable_keeps_snapshot_valid_with_reason(self):
        h = Harness(overrides={"/match": (503, {"error": "unavailable", "message": "no match viewer"})})
        snap = h.collect(["/squad", "/match"], optional=["/match"])
        self.assertTrue(snap.valid)
        self.assertNotIn("/match", snap.routes)
        self.assertNotIn("/match", snap.entity_versions)
        self.assertEqual(len(snap.consistency_reasons), 1)
        self.assertIn("/match", snap.consistency_reasons[0])

    def test_required_route_unavailable_invalidates(self):
        """A required route answering 503 fails every bounded attempt; the last status is named."""
        h = Harness(overrides={"/finances": (503, {"error": "unavailable", "message": "down"})})
        snap = h.collect()
        self.assertFalse(snap.valid)
        self.assertIs(snap.consistency, ConsistencyStatus.ATTEMPTS_EXHAUSTED)
        self.assertIn("last status route_unavailable", snap.consistency_reasons[0])
        self.assertIn("/finances unavailable", snap.consistency_reasons[1])
        self.assertEqual(h.calls("/finances"), 3)

    def test_action_critical_second_read_is_stable(self):
        h = Harness()
        snap = h.collect(action_critical=["/squad"])
        self.assertTrue(snap.valid)
        self.assertEqual(h.calls("/squad"), 2)


class InjectedChangeTests(unittest.TestCase):
    """OBS 03: injected time, session, identity and same-tick field changes are rejected."""

    def test_time_change_between_reads_is_retried_then_accepted(self):
        """OBS 03: /game moves between the first and second read; the next attempt is consistent."""
        h = Harness(overrides={"/game": [env({"date": fx.GAME_DATE, "time": "10:00"}), env({"date": fx.GAME_DATE, "time": "10:15"})]})
        snap = h.collect()
        self.assertTrue(snap.valid)
        self.assertEqual(snap.attempts, 2)
        self.assertEqual(snap.game_time, "10:15")
        self.assertEqual(h.calls("/status"), 2)

    def test_time_change_is_reported_when_attempts_are_exhausted(self):
        """OBS 03: a clock that keeps moving never yields a snapshot; the reasons name each attempt."""
        ticks = iter(range(100))
        h = Harness(overrides={"/game": lambda: env({"date": fx.GAME_DATE, "time": f"10:{next(ticks):02d}"})})
        snap = h.collect()
        self.assertFalse(snap.valid)
        self.assertIs(snap.consistency, ConsistencyStatus.ATTEMPTS_EXHAUSTED)
        self.assertEqual(snap.attempts, 3)
        self.assertIn("time_changed", snap.consistency_reasons[0])
        self.assertTrue(any(r.startswith("attempt 3:") for r in snap.consistency_reasons))

    def test_session_change_during_collection_is_rejected(self):
        """OBS 03: a route answered by a different bridge session invalidates every attempt."""
        h = Harness(overrides={"/finances": env(fx.finances_payload(), session="other-session")})
        snap = h.collect()
        self.assertFalse(snap.valid)
        self.assertIs(snap.consistency, ConsistencyStatus.ATTEMPTS_EXHAUSTED)
        self.assertIn("last status session_changed", snap.consistency_reasons[0])
        self.assertIn("other-session", snap.consistency_reasons[1])
        self.assertEqual(sum(1 for r in snap.consistency_reasons if "other-session" in r), 3)

    def test_session_change_on_game_reread_is_retried_then_accepted(self):
        """OBS 03: a reconnect between the first and second /game read fails that attempt only."""
        same = {"date": fx.GAME_DATE, "time": fx.GAME_TIME}
        h = Harness(overrides={"/game": [env(same), env(same, session="s2"), env(same)]})
        snap = h.collect()
        self.assertTrue(snap.valid)
        self.assertEqual(snap.attempts, 2)
        self.assertEqual(snap.session_id, fx.SESSION)

    def test_session_change_on_game_reread_is_named_when_exhausted(self):
        same = {"date": fx.GAME_DATE, "time": fx.GAME_TIME}
        h = Harness(overrides={"/game": [env(same), env(same, session="s2")]}, attempts=1)
        snap = h.collect()
        self.assertIs(snap.consistency, ConsistencyStatus.ATTEMPTS_EXHAUSTED)
        self.assertIn("last status session_changed", snap.consistency_reasons[0])
        self.assertIn("-> s2", snap.consistency_reasons[1])

    def test_identity_change_on_reread_stops_immediately(self):
        """OBS 03: the manager changes between identity reads; no retry can repair that."""
        h = Harness(overrides={"/manager": [env({"id": 90001, "name": "Test Manager"}), env({"id": 90002, "name": "Someone Else"})]})
        snap = h.collect()
        self.assertFalse(snap.valid)
        self.assertIs(snap.consistency, ConsistencyStatus.IDENTITY_CHANGED)
        self.assertEqual(snap.attempts, 1)
        self.assertIn("90001/742 -> 90002/742", snap.consistency_reasons[0])

    def test_action_critical_field_change_is_retried(self):
        """OBS 03: a same-tick change to an action-critical route fails the attempt; a stable reread passes."""
        changed = fx.squad_payload()
        changed[0]["morale"] = "Abysmal"
        h = Harness(overrides={"/squad": [env(fx.squad_payload()), env(changed)]})
        snap = h.collect(action_critical=["/squad"])
        self.assertTrue(snap.valid)
        self.assertEqual(snap.attempts, 2)
        self.assertEqual(snap.routes["/squad"][0]["morale"], "Abysmal")

    def test_action_critical_field_churn_exhausts_after_three_attempts(self):
        """OBS 03: a route that differs on every read never produces a stable second read."""
        counter = iter(range(1000))

        def churn():
            squad = fx.squad_payload()
            squad[0]["morale_rating"] = next(counter)
            return env(squad)

        h = Harness(overrides={"/squad": churn})
        snap = h.collect(action_critical=["/squad"])
        self.assertFalse(snap.valid)
        self.assertIs(snap.consistency, ConsistencyStatus.ATTEMPTS_EXHAUSTED)
        self.assertEqual(snap.attempts, 3)
        self.assertEqual(h.calls("/status"), 3)
        self.assertIn("field_changed", snap.consistency_reasons[0])
        self.assertEqual(sum(1 for r in snap.consistency_reasons if "changed between reads" in r), 3)

    def test_action_critical_route_must_be_collected(self):
        h = Harness(attempts=1)
        snap = h.collect(["/squad"], action_critical=["/tactics"])
        self.assertIs(snap.consistency, ConsistencyStatus.ATTEMPTS_EXHAUSTED)
        self.assertIn("last status route_unavailable", snap.consistency_reasons[0])
        self.assertIn("/tactics was not collected", snap.consistency_reasons[1])

    def test_stable_point_false_invalidates(self):
        h = Harness()
        snap = h.collect(context=h.context(stable_point=lambda: False))
        self.assertFalse(snap.valid)
        self.assertIs(snap.consistency, ConsistencyStatus.ATTEMPTS_EXHAUSTED)
        self.assertIn("idle or paused decision point", snap.consistency_reasons[1])
        self.assertEqual(h.calls("/game"), 0)   # nothing is read while FM is not at a decision point


class ConnectionTests(unittest.TestCase):
    def test_disconnected_stops_without_retry_loops(self):
        """OBS 01: connected:false on HTTP 200 ends collection after one /status read."""
        h = Harness(overrides={"/status": (200, fx.status_payload(connected=False))})
        snap = h.collect()
        self.assertFalse(snap.valid)
        self.assertIs(snap.consistency, ConsistencyStatus.DISCONNECTED)
        self.assertEqual(snap.attempts, 1)
        self.assertEqual(h.calls("/status"), 1)
        self.assertEqual(h.calls("/game"), 0)
        self.assertEqual(snap.routes, {})
        self.assertIsNotNone(h.store.get_snapshot(snap.snapshot_id))

    def test_unsupported_build_stops_without_retry(self):
        """OBS 01: an unsupported build is reported once and not retried."""
        h = Harness(overrides={"/status": (200, fx.status_payload(build="24.5.0+1"))})
        snap = h.collect()
        self.assertIs(snap.consistency, ConsistencyStatus.BUILD_UNSUPPORTED)
        self.assertEqual(h.calls("/status"), 1)
        self.assertIn("24.5.0+1", snap.consistency_reasons[0])

    def test_status_transport_error_is_disconnected(self):
        from ..bridge_client.transport import TransportError
        h = Harness(overrides={"/status": TransportError("bridge unreachable")})
        snap = h.collect()
        self.assertIs(snap.consistency, ConsistencyStatus.DISCONNECTED)
        self.assertIn("unreachable", snap.consistency_reasons[0])


class ContinuityTests(unittest.TestCase):
    def test_unknown_lineage_stops_immediately(self):
        """A first observation without a confirmed registration is never treated as continuous."""
        h = Harness()
        snap = h.collect(context=h.context(lineage_confirmed=False))
        self.assertFalse(snap.valid)
        self.assertIs(snap.consistency, ConsistencyStatus.SESSION_CHANGED)
        self.assertEqual(snap.attempts, 1)
        self.assertEqual(h.calls("/status"), 1)
        self.assertEqual(snap.continuity["status"], "unknown_lineage")
        self.assertTrue(snap.continuity["requires_lineage_confirmation"])
        self.assertIn("unknown_lineage", snap.consistency_reasons[0])
        # The routes were read and are kept for the operator, but the snapshot is not valid for decisions.
        self.assertIn("/squad", snap.routes)

    def test_date_reversed_against_previous_anchor_is_identity_resolution(self):
        h = Harness()
        previous = Anchor(h.career.career_id, h.branch.branch_id, fx.SESSION, fx.BUILD, 90001, 742, "2024-03-01", "10:00", sequence=1)
        snap = h.collect(context=h.context(previous_anchor=previous, lineage_confirmed=False))
        self.assertIs(snap.consistency, ConsistencyStatus.IDENTITY_CHANGED)
        self.assertEqual(snap.continuity["status"], "date_reversed")
        self.assertEqual(snap.attempts, 1)

    def test_bot_progression_explains_forward_time(self):
        h = Harness()
        previous = Anchor(h.career.career_id, h.branch.branch_id, fx.SESSION, fx.BUILD, 90001, 742, "2024-02-16", "10:00", sequence=1)
        without = h.collect(context=h.context(previous_anchor=previous, lineage_confirmed=False))
        self.assertFalse(without.valid)
        self.assertEqual(without.continuity["status"], "forward_jump")
        with_progress = h.collect(context=h.context(previous_anchor=previous, lineage_confirmed=False, bot_progressed=True))
        self.assertTrue(with_progress.valid)
        self.assertEqual(anchor_of(with_progress).sequence, 1)

    def test_sequence_advances_across_collections(self):
        h = Harness()
        context = h.context()
        first = h.collect(context=context)
        context.previous_anchor = anchor_of(first)
        second = h.collect(context=context)
        self.assertTrue(second.valid)
        self.assertEqual(anchor_of(second).sequence, 2)


class RegistryBridgeTests(unittest.TestCase):
    def test_capability_registry_reflects_snapshot(self):
        h = Harness()
        registry = h.collector.capability_registry(h.collect())
        self.assertTrue(registry.supported("player_attributes_47"))
        self.assertFalse(registry.supported("inbox_text"))
        self.assertFalse(registry.supported("ui_action_adapter"))

    def test_anchor_of_reconstructs_from_a_stored_snapshot(self):
        h = Harness()
        snap = h.collect()
        stored = h.store.get_snapshot(snap.snapshot_id)
        anchor = anchor_of(stored)
        self.assertEqual((anchor.session_id, anchor.manager_id, anchor.club_id, anchor.game_date), (fx.SESSION, 90001, 742, fx.GAME_DATE))
        disconnected = Harness(overrides={"/status": (200, fx.status_payload(connected=False))}).collect()
        self.assertIsNone(anchor_of(disconnected))


if __name__ == "__main__":
    unittest.main()
