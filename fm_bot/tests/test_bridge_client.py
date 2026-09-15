"""Tests for the typed bridge client and transports (spec 3.1, BOT 001, OBS 01)."""
from __future__ import annotations

import unittest

from ..bridge_client.client import BridgeClient, BridgeUnavailable
from ..bridge_client.schemas import SCHEMA_VERSION, validate_payload
from ..bridge_client.transport import FakeTransport, HttpTransport, RawResponse, TransportError
from ..state.identity import CareerRegistry, SaveManifest
from ..state.records import QualityStatus, Visibility, payload_hash
from ..state.store import Store
from . import fixtures as fx


def registered_client(transport=None):
    store = Store.memory()
    career, branch, _ = CareerRegistry(store).register_career("t", SaveManifest(fx.BUILD, 90001, 742, fx.GAME_DATE, fx.GAME_TIME))
    client = BridgeClient(transport or fx.transport(), store, context={"career_id": career.career_id, "branch_id": branch.branch_id})
    return store, branch, client


class EnvelopeTests(unittest.TestCase):
    def test_successful_envelope_is_unwrapped(self):
        client = BridgeClient(fx.transport())
        response = client.game()
        self.assertTrue(response.ok)
        self.assertEqual(response.http_status, 200)
        self.assertEqual(response.data, {"date": fx.GAME_DATE, "time": fx.GAME_TIME})
        self.assertEqual(response.session_id, fx.SESSION)
        self.assertEqual(response.observed_at, "2026-09-14T12:00:00+00:00")
        self.assertEqual(response.schema_version, SCHEMA_VERSION)
        self.assertEqual(response.schema_problems, [])
        self.assertIs(response.quality, QualityStatus.VALIDATED)
        self.assertEqual(response.require(), response.data)

    def test_payload_hash_covers_the_whole_envelope(self):
        client = BridgeClient(fx.transport())
        response = client.game()
        self.assertEqual(response.payload_hash, payload_hash({"observed_at": "2026-09-14T12:00:00+00:00", "session_id": fx.SESSION, "data": response.data}))

    def test_typed_routes_hit_the_expected_paths(self):
        transport = fx.transport()
        client = BridgeClient(transport)
        client.squad(); client.finances(); client.transfer_targets(); client.player(1001)
        self.assertEqual(transport.calls, ["/squad", "/finances", "/transfer-targets", "/players/1001"])

    def test_player_id_must_be_positive_int(self):
        client = BridgeClient(fx.transport())
        for bad in (0, -1, True, "1001"):
            with self.assertRaises(ValueError):
                client.player(bad)  # type: ignore[arg-type]


class ConnectionStateTests(unittest.TestCase):
    def test_http_200_with_connected_false_is_disconnected(self):
        """OBS 01: an HTTP 200 /status body with connected:false disables actions."""
        client = BridgeClient(fx.transport(overrides={"/status": (200, fx.status_payload(connected=False, reason="no supported save"))}))
        state = client.connection_state()
        self.assertFalse(state["connected"])
        self.assertFalse(state["build_supported"])
        self.assertEqual(state["reason"], "no supported save")
        self.assertIsNone(state["session_id"])
        self.assertIsNone(state["build"])
        self.assertTrue(client.status().ok)   # the read itself succeeded; connection is a separate check

    def test_unsupported_build_is_reported(self):
        """OBS 01: a connected bridge on an unsupported build is not usable for actions."""
        client = BridgeClient(fx.transport(overrides={"/status": (200, fx.status_payload(build="24.5.0+9999999"))}))
        state = client.connection_state()
        self.assertTrue(state["connected"])
        self.assertFalse(state["build_supported"])
        self.assertIn("24.5.0+9999999", state["reason"])
        self.assertEqual(state["build"], "24.5.0+9999999")

    def test_supported_build_reports_capabilities(self):
        client = BridgeClient(fx.transport())
        state = client.connection_state()
        self.assertTrue(state["connected"])
        self.assertTrue(state["build_supported"])
        self.assertIsNone(state["reason"])
        self.assertEqual(state["session_id"], fx.SESSION)
        self.assertEqual(state["game_date"], fx.GAME_DATE)
        self.assertIn("player_attributes_47", state["capabilities"])
        self.assertIn("inbox_text", state["unresolved"])
        self.assertEqual(client.last_status["build"], fx.BUILD)

    def test_status_http_error_is_disconnected(self):
        client = BridgeClient(fx.transport(overrides={"/status": (503, {"error": "unavailable", "message": "decoder starting"})}))
        state = client.connection_state()
        self.assertFalse(state["connected"])
        self.assertIn("503", state["reason"])
        self.assertIsNone(client.last_status)

    def test_supported_builds_are_configurable(self):
        client = BridgeClient(fx.transport(overrides={"/status": (200, fx.status_payload(build="25.0.0"))}), supported_builds=("25.0.0",))
        self.assertTrue(client.connection_state()["build_supported"])


class FailureTests(unittest.TestCase):
    def test_503_is_unavailable_quality(self):
        client = BridgeClient(fx.transport(overrides={"/match": (503, {"error": "unavailable", "message": "no match in progress"})}))
        response = client.match()
        self.assertFalse(response.ok)
        self.assertEqual(response.http_status, 503)
        self.assertIsNone(response.data)
        self.assertIs(response.quality, QualityStatus.UNAVAILABLE)
        self.assertIn("no match in progress", response.error)
        with self.assertRaises(BridgeUnavailable) as ctx:
            response.require()
        self.assertEqual(ctx.exception.http_status, 503)

    def test_unknown_route_is_404_unavailable(self):
        client = BridgeClient(fx.transport())
        response = client.get("/players/9999")
        self.assertFalse(response.ok)
        self.assertEqual(response.http_status, 404)
        self.assertIs(response.quality, QualityStatus.UNAVAILABLE)

    def test_transport_error_is_error_quality(self):
        client = BridgeClient(fx.transport(overrides={"/game": TransportError("bridge unreachable")}))
        response = client.game()
        self.assertFalse(response.ok)
        self.assertIsNone(response.http_status)
        self.assertIs(response.quality, QualityStatus.ERROR)
        self.assertEqual(response.error, "bridge unreachable")
        self.assertEqual(client.errors[-1]["route"], "/game")

    def test_200_without_data_member_is_an_error(self):
        client = BridgeClient(fx.transport(overrides={"/game": (200, {"unexpected": True})}))
        response = client.game()
        self.assertFalse(response.ok)
        self.assertIs(response.quality, QualityStatus.ERROR)

    def test_schema_mismatch_is_recorded_not_fabricated(self):
        broken = dict(fx.finances_payload())
        broken["balance"] = "ten million"
        del broken["as_of"]
        client = BridgeClient(fx.transport(overrides={"/finances": FakeTransport.envelope(broken, session_id=fx.SESSION)}))
        response = client.finances()
        self.assertTrue(response.ok)
        self.assertIs(response.quality, QualityStatus.SCHEMA_MISMATCH)
        self.assertEqual(len(response.schema_problems), 2)
        self.assertEqual(response.data["balance"], "ten million")   # stored as received, never coerced
        self.assertEqual(client.errors[-1]["schema_problems"], response.schema_problems)

    def test_per_player_failure_does_not_abort_bulk_read(self):
        overrides = {"/players/1002": (503, {"error": "unavailable"}), "/players/1003": TransportError("reset")}
        client = BridgeClient(fx.transport(overrides=overrides))
        results = client.players([1001, 1002, 1003, 1004])
        self.assertEqual(sorted(results), [1001, 1002, 1003, 1004])
        self.assertTrue(results[1001].ok)
        self.assertFalse(results[1002].ok)
        self.assertFalse(results[1003].ok)
        self.assertTrue(results[1004].ok)
        self.assertEqual(results[1004].data["id"], 1004)


class JournalingTests(unittest.TestCase):
    def test_every_read_journals_an_observation_with_hash_and_sequence(self):
        store, branch, client = registered_client()
        first = client.game()
        second = client.squad()
        failed = client.get("/players/9999")
        observations = store.list_observations(branch_id=branch.branch_id)
        self.assertEqual([o.source for o in observations], ["bridge:/game", "bridge:/squad", "bridge:/players/9999"])
        self.assertEqual([o.sequence for o in observations], [1, 2, 3])
        self.assertEqual(client.sequence, 3)
        self.assertEqual(observations[0].observation_id, first.observation_id)
        self.assertIs(observations[0].visibility, Visibility.PRIVILEGED)
        self.assertIs(observations[2].quality, QualityStatus.UNAVAILABLE)
        self.assertEqual(observations[2].http_status, 404)
        stored = store.get_observation(second.observation_id)
        self.assertEqual(stored.payload["data"], second.data)
        self.assertEqual(stored.payload_hash, payload_hash(stored.payload))
        self.assertEqual(stored.session_id, fx.SESSION)
        self.assertEqual(len(store.journal_entries(kind="observation")), 3)

    def test_game_time_context_is_recorded_on_observations(self):
        store, _, client = registered_client()
        response = client.squad(game_date=fx.GAME_DATE, game_time=fx.GAME_TIME)
        stored = store.get_observation(response.observation_id, with_payload=False)
        self.assertEqual((stored.game_date, stored.game_time), (fx.GAME_DATE, fx.GAME_TIME))

    def test_transport_error_is_journaled_too(self):
        store, branch, client = registered_client(fx.transport(overrides={"/game": TransportError("down")}))
        response = client.game()
        self.assertIsNotNone(response.observation_id)
        stored = store.get_observation(response.observation_id, with_payload=False)
        self.assertIs(stored.quality, QualityStatus.ERROR)
        self.assertEqual(stored.error, "down")

    def test_without_store_nothing_is_journaled(self):
        client = BridgeClient(fx.transport())
        self.assertIsNone(client.game().observation_id)


class TransportTests(unittest.TestCase):
    def test_http_transport_refuses_non_loopback_base_url(self):
        for url in ("http://192.168.1.10:8765", "https://127.0.0.1:8765", "http://example.com:8765"):
            with self.assertRaises(ValueError, msg=url):
                HttpTransport(url)
        self.assertEqual(HttpTransport("http://127.0.0.1:8765/").base_url, "http://127.0.0.1:8765")
        HttpTransport("http://localhost:8765")

    def test_http_transport_refuses_query_strings(self):
        transport = HttpTransport()
        with self.assertRaises(ValueError):
            transport.get("/squad?full=1")
        with self.assertRaises(ValueError):
            transport.get("/squad#x")

    def test_fake_transport_scripts_sequences_and_repeats_last(self):
        transport = FakeTransport({"/game": [(200, {"n": 1}), (200, {"n": 2})]})
        self.assertEqual([transport.get("/game").body["n"] for _ in range(4)], [1, 2, 2, 2])
        self.assertEqual(transport.get("/nope").status, 404)
        self.assertEqual(transport.calls, ["/game"] * 4 + ["/nope"])

    def test_fake_transport_supports_callables_raw_and_exceptions(self):
        transport = FakeTransport({"/a": lambda: (201, {"x": 1}), "/b": RawResponse(200, {"y": 2}), "/c": TransportError("boom")})
        self.assertEqual(transport.get("/a").status, 201)
        self.assertEqual(transport.get("/b").body, {"y": 2})
        with self.assertRaises(TransportError):
            transport.get("/c")

    def test_fake_transport_bodies_are_copies(self):
        transport = FakeTransport({"/a": (200, {"list": [1]})})
        transport.get("/a").body["list"].append(2)
        self.assertEqual(transport.get("/a").body, {"list": [1]})


class SchemaTests(unittest.TestCase):
    def test_fixture_world_matches_contracts(self):
        self.assertEqual(validate_payload("/squad", fx.squad_payload()), [])
        self.assertEqual(validate_payload("/fixtures", fx.fixtures_payload()), [])
        self.assertEqual(validate_payload("/inbox", fx.inbox_payload()), [])
        self.assertEqual(validate_payload("/players/1001", fx.player_payload(*fx.SQUAD_SPEC[0])), [])
        self.assertEqual(validate_payload("/status", fx.status_payload()), [])

    def test_bool_is_not_an_int_and_attributes_are_bounded(self):
        player = fx.player_payload(*fx.SQUAD_SPEC[0])
        player["age"] = True
        player["attributes"]["pace"] = 21
        problems = validate_payload("/players/1001", player)
        self.assertTrue(any("'age' is a bool" in p for p in problems))
        self.assertTrue(any("outside 1..20" in p for p in problems))

    def test_unknown_route_is_a_problem(self):
        self.assertEqual(validate_payload("/nope", {}), ["unknown route /nope"])


if __name__ == "__main__":
    unittest.main()
