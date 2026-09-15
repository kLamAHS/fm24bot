"""Tests for fm_bot.interface: controls (settings), status view, explanations (AUD 01) and notifications (spec 15.1, 15.3)."""
from __future__ import annotations

import logging
import unittest

from ..execution.lifecycle import IntentFactory
from ..interface import controls, explain, notify, status
from ..interface.controls import KEY_AUTHORITY_PROFILE, Settings, SettingsError, parse_setting_value
from ..interface.explain import ActionExplanation, ExplanationError, explain_action, explain_decision, render_action, render_decision, render_decision_evidence, strongest_alternatives
from ..interface.notify import CallbackSink, LogSink, MemorySink, Notification, NotificationKind, NotificationPolicy, Notifier, material_plan_change
from ..interface.status import active_career, build_view, latest_snapshot, render_evidence, render_text, set_active_career, stop_state
from ..rules.authority import AuthorityMode
from ..state.identity import CareerRegistry, SaveManifest, new_id
from ..state.records import ActionState, Decision
from ..state.status import MissingCapabilityReport
from ..state.store import Store
from ..state.units import Money, Period
from ..state.visibility import InformationMode
from . import fixtures as fx
from .execution_fixtures import harness


def decision(snapshot_id: str, *, kind: str = "plan.next_decision", selected=None, candidates=None, constraints=None, forecasts=None, model_versions=None) -> Decision:
    return Decision(new_id("dec"), "club-objective-v1", snapshot_id, candidates or [], constraints or [], forecasts or [], selected, ["because"], {}, "bridge_observed", model_versions or {}, kind=kind)


# ---------------------------------------------------------------------------
# controls
# ---------------------------------------------------------------------------


class SettingsTests(unittest.TestCase):
    def test_defaults_load_unsaved_and_save_persists_versions(self):
        store = Store.memory()
        settings = Settings.load(store)
        self.assertEqual(settings.version("authority_mode"), 0)
        self.assertIs(settings.authority_mode(), AuthorityMode.ADVISE)
        self.assertIs(settings.information_mode(), InformationMode.BRIDGE_OBSERVED)
        self.assertEqual(settings.poll_intervals()["idle_seconds"], 5.0)
        saved = settings.save()
        self.assertEqual(set(saved), set(controls.SETTING_NAMES))
        self.assertTrue(all(v == 1 for v in saved.values()))
        self.assertEqual(settings.authority_profile_version, 1)
        self.assertEqual(settings.save(), {}, "saving again changes nothing")
        reloaded = Settings.load(store)
        self.assertEqual(reloaded.stamp(), settings.stamp())
        self.assertEqual(reloaded.authority_profile().version, 1)

    def test_change_bumps_version_journals_and_recomposes_authority_profile(self):
        store = Store.memory()
        settings = Settings.load(store)
        settings.save()
        item = settings.change("authority_mode", "scoped", reason="operator enabled execution")
        self.assertEqual(item.value, "scoped_execution")
        self.assertEqual(item.version, 2)
        self.assertEqual(settings.authority_profile_version, 2, "an authority input re-stores the profile")
        settings.change("development_emphasis", 0.6)
        self.assertEqual(settings.authority_profile_version, 2, "a non-authority setting leaves the profile version alone")
        entries = store.journal_entries(kind="settings.changed")
        self.assertEqual(entries[0]["body"]["name"], "authority_mode")
        self.assertEqual(entries[0]["body"]["previous"], "advise")
        self.assertEqual(entries[0]["body"]["reason"], "operator enabled execution")
        stored = store.get_setting(KEY_AUTHORITY_PROFILE)
        self.assertEqual(stored[0]["mode"], "scoped_execution")
        self.assertEqual(stored[1], 2)
        self.assertEqual(Settings.load(store).authority_mode(), AuthorityMode.SCOPED_EXECUTION)

    def test_invalid_values_are_refused_without_change(self):
        store = Store.memory()
        settings = Settings.load(store)
        settings.save()
        for name, value in (("authority_mode", "god"), ("information_mode", "psychic"), ("action_families", ["laboratory", "nonsense"]), ("development_emphasis", 1.5), ("cash_reserve", -5), ("spending_limits", {"max_weekly_wage_commitment": Money.native_gbp(10, Period.ONCE).to_json()}), ("spending_limits", {"allow_continue": "yes"}), ("poll_intervals", {"idle_seconds": 0}), ("lm_limits", {"max_calls": -1}), ("delegation", {"nonsense": "assistant"}), ("club_objective", {"w_dev": -1})):
            with self.subTest(name=name, value=value):
                with self.assertRaises(SettingsError):
                    settings.change(name, value)
        with self.assertRaises(SettingsError):
            settings.change("no_such_setting", 1)
        self.assertEqual(settings.stamp(), Settings.load(store).stamp(), "nothing was persisted")
        self.assertEqual(store.journal_entries(kind="settings.changed"), [])

    def test_money_limits_keep_currency_and_period(self):
        store = Store.memory()
        settings = Settings.load(store)
        settings.change("spending_limits", {"max_weekly_wage_commitment": 4000, "max_total_fee_commitment": Money.native_gbp(250_000).to_json(), "allow_continue": True})
        settings.change("cash_reserve", 1_000_000)
        profile = settings.authority_profile()
        self.assertEqual(profile.limits.max_weekly_wage_commitment, Money.native_gbp(4000, Period.WEEKLY))
        self.assertEqual(profile.limits.max_total_fee_commitment, Money.native_gbp(250_000))
        self.assertEqual(profile.limits.min_cash_reserve, Money.native_gbp(1_000_000))
        self.assertTrue(profile.limits.allow_continue)
        self.assertEqual(settings.cash_reserve(), Money.native_gbp(1_000_000))

    def test_decisions_record_setting_versions(self):
        store = Store.memory()
        settings = Settings.load(store)
        settings.save()
        settings.change("action_families", "selection, tactics")
        record = decision("snap-x", model_versions={"planner": "p-1"})
        settings.stamp_decision(record)
        self.assertEqual(record.model_versions["planner"], "p-1")
        self.assertEqual(record.model_versions["settings:permitted_action_families"], "2")
        self.assertEqual(record.model_versions[KEY_AUTHORITY_PROFILE], str(settings.authority_profile_version))
        self.assertEqual(settings.get("action_families"), ["selection", "tactics"])

    def test_parse_setting_value(self):
        self.assertEqual(parse_setting_value("x", "0.4"), 0.4)
        self.assertEqual(parse_setting_value("x", '{"a": 1}'), {"a": 1})
        self.assertEqual(parse_setting_value("x", "scoped"), "scoped")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class StatusTests(unittest.TestCase):
    def test_view_without_a_career_says_so_in_football_language(self):
        store = Store.memory()
        view = build_view(store)
        text = render_text(view)
        self.assertIn("no career registered", text)
        self.assertIn("advising only", text)
        self.assertIn("Stop: not engaged", text)
        for jargon in ("sqlite", "solver", "hungarian", "snapshot_id", "journal"):
            self.assertNotIn(jargon, text.lower())
        self.assertIsNone(active_career(store))

    def test_view_reads_career_snapshot_stop_and_last_execution_from_the_store(self):
        h = harness()
        career = h.store.get_career(h.career_id)
        branch = h.store.get_branch(h.branch_id)
        set_active_career(h.store, career.career_id, branch.branch_id, lineage_confirmed=True)
        found = active_career(h.store)
        self.assertEqual((found[0].career_id, found[1].branch_id, found[2]), (career.career_id, branch.branch_id, True))
        snap = h.snapshot()
        self.assertEqual(latest_snapshot(h.store, branch.branch_id).snapshot_id, snap.snapshot_id)
        h.store.journal(status.JOURNAL_STOP, {"reason": "operator stop", "at": "now"})
        self.assertTrue(stop_state(h.store)["engaged"])
        h.store.journal(status.JOURNAL_STOP_CLEARED, {"at": "later"})
        self.assertFalse(stop_state(h.store)["engaged"])
        h.store.journal(status.JOURNAL_EXECUTION, {"action_id": "act-1", "state": "CONFIRMED", "reason": "readback equals the intended state"}, "act-1")
        report = MissingCapabilityReport("submit.lineup")
        report.add("eligibility_injury", "injury status per player")
        view = build_view(h.store, prerequisites=[report], next_action={"kind": "select_validated_tactic", "authority_scope": "tactics.select", "description": "switch to the counter"}, continue_gate={"allowed": False, "blockers": ["unread board request"]})
        self.assertEqual(view.game_date, fx.GAME_DATE)
        self.assertEqual(view.career["career_id"], career.career_id)
        self.assertTrue(view.career["lineage_confirmed"])
        self.assertEqual(view.last_execution["state"], "CONFIRMED")
        text = render_text(view)
        self.assertIn("Managing: t (club 742, manager 90001)", text)
        self.assertIn("Game day: 2024-02-17 10:00", text)
        self.assertIn("Next: select_validated_tactic (tactics.select) - switch to the counter", text)
        self.assertIn("Calendar: waiting - unread board request", text)
        self.assertIn("Cannot submit.lineup yet: needs eligibility_injury", text)
        self.assertIn("Last action: CONFIRMED", text)
        evidence = render_evidence(view)
        self.assertIn(snap.snapshot_id, evidence)
        self.assertIn("settings:", evidence)
        self.assertEqual(view.to_json()["version"], status.STATUS_VIEW_VERSION)

    def test_id01_an_unconfirmed_lineage_names_the_command_that_confirms_it(self):
        """ID 01 / spec 5.1: the one thing the operator must do is named on the screen, and it is the command that confirms the
        registered career - not a second registration, which would fork the history."""
        h = harness()
        career, branch = h.store.get_career(h.career_id), h.store.get_branch(h.branch_id)
        set_active_career(h.store, career.career_id, branch.branch_id, lineage_confirmed=False)
        text = render_text(build_view(h.store))
        self.assertIn("lineage not yet confirmed", text)
        self.assertIn(f"run `{status.CONFIRM_LINEAGE_COMMAND}`", text)
        self.assertIn("registering again would start a second career", text)
        set_active_career(h.store, career.career_id, branch.branch_id, lineage_confirmed=True)
        self.assertNotIn("lineage", render_text(build_view(h.store)))

    def test_the_bridge_stop_and_last_action_lines_follow_the_journal_past_a_page_limit(self):
        """Spec 15.1: the operator sees the CURRENT connection state, the CURRENT Stop state and the LAST execution result. A
        connection entry is written on every connect, so after a few hours of five-second polling a kind has far more entries than
        any page of the journal; the view therefore reads the newest entry of a kind, never the end of a page of the oldest ones. A
        deliberately tiny page stands in for those hours (no need to write ten thousand rows)."""
        store = Store.memory()
        for index in range(4):
            store.journal(status.JOURNAL_CONNECTION, {"connected": index < 3, "build_supported": True, "reason": f"connect {index}", "session_id": f"s{index}"})
            store.journal(status.JOURNAL_EXECUTION, {"action_id": f"act-{index}", "state": "CONFIRMED" if index < 3 else "UNCERTAIN", "reason": f"result {index}"}, f"act-{index}")
            store.journal(status.JOURNAL_STOP, {"reason": f"stop {index}", "at": "now"})
        unclamped = store.journal_entries

        def one_page(*args, **kwargs):
            kwargs["limit"] = 2          # the journal holds more entries of each kind than one page can return
            return unclamped(*args, **kwargs)
        store.journal_entries = one_page
        self.assertEqual(status.last_connection(store)["reason"], "connect 3")
        self.assertFalse(status.last_connection(store)["connected"], "the operator sees the state the bridge is in now")
        self.assertEqual(status.last_execution(store)["action_id"], "act-3")
        self.assertEqual(status.last_execution(store)["state"], "UNCERTAIN")
        self.assertEqual(status.stop_state(store)["reason"], "stop 3")
        text = render_text(build_view(store))
        self.assertIn("Bridge: disconnected (connect 3)", text)
        self.assertIn("Last action: UNCERTAIN - result 3", text)
        self.assertIn("Stop: ENGAGED", text)
        self.assertIn("stop 3", text)

    def test_connection_text_distinguishes_unknown_disconnected_and_unsupported(self):
        store = Store.memory()
        self.assertIn("not checked", render_text(build_view(store)))
        self.assertIn("disconnected", render_text(build_view(store, connection={"connected": False, "reason": "no save"})))
        self.assertIn("not supported", render_text(build_view(store, connection={"connected": True, "build_supported": False, "reason": "build 25"})))
        self.assertIn("Bridge: connected", render_text(build_view(store, connection={"connected": True, "build_supported": True})))


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


class ExplainDecisionTests(unittest.TestCase):
    def test_decision_view_shows_selection_alternatives_outcomes_constraints_and_sources(self):
        h = harness()
        snap = h.snapshot()
        candidates = [{"id": "a", "kind": "advise.lineup", "score": 0.9}, {"id": "b", "kind": "advise.lineup", "score": 0.7}, {"id": "c", "kind": "advise.lineup", "score": 0.8}, {"id": "d", "kind": "advise.lineup"}]
        constraints = [{"name": "wage_headroom", "status": "fail", "reason": "over the weekly wage budget", "binding": True}, {"name": "transfer_budget", "status": "pass", "reason": "within budget"}]
        forecasts = [{"forecast_target": "points_at_fixture", "point_estimate": 1.4, "interval": [0.6, 2.2], "status": "available", "method": "rolling_average"}, {"forecast_target": "readiness", "status": "unavailable", "reason": "required_current_readiness_observation_missing"}]
        record = decision(snap.snapshot_id, selected=candidates[0], candidates=candidates, constraints=constraints, forecasts=forecasts, model_versions={"settings:authority_profile": "3", "settings:club_objective": "1", "planner": "planner-baseline-0.1"})
        h.store.insert_decision(record)
        explanation = explain_decision(record.decision_id, h.store)
        self.assertEqual(explanation.selected["id"], "a")
        self.assertEqual([c["id"] for c in explanation.alternatives], ["c", "b", "d"], "best score first; unscored last")
        self.assertEqual([c["name"] for c in explanation.binding_constraints], ["wage_headroom"])
        self.assertEqual([c["name"] for c in explanation.other_constraints], ["transfer_budget"])
        self.assertEqual(explanation.expected_outcomes[1]["status"], "unavailable")
        self.assertEqual(explanation.game_date, fx.GAME_DATE)
        self.assertEqual({s.observation_id for s in explanation.source_timestamps}, set(snap.observation_ids))
        self.assertTrue(all(s.observed_at for s in explanation.source_timestamps))
        self.assertEqual(explanation.authority_profile_version, "3")
        self.assertEqual(explanation.model_versions, {"planner": "planner-baseline-0.1"})
        self.assertEqual(explanation.gaps, [])
        text = render_decision(explanation)
        self.assertIn("Chosen: advise.lineup", text)
        self.assertIn("Strongest alternatives:", text)
        self.assertIn("points_at_fixture: 1.4 (range [0.6, 2.2])", text)
        self.assertIn("readiness: unavailable (required_current_readiness_observation_missing)", text)
        self.assertIn("What bound the choice:", text)
        evidence = render_decision_evidence(explanation)
        self.assertIn("authority_profile v3", evidence)
        self.assertIn("bridge:/tactics", evidence)
        self.assertEqual(strongest_alternatives(record, count=1)[0]["id"], "c")

    def test_missing_snapshot_is_a_gap_unless_strict(self):
        store = Store.memory()
        CareerRegistry(store).register_career("t", SaveManifest(fx.BUILD, 90001, 742, fx.GAME_DATE, fx.GAME_TIME))
        record = decision("snap-missing")
        with self.assertRaises(ExplanationError):
            explain_decision("dec-unknown", store)
        explanation = explain_decision(record, store)
        self.assertTrue(explanation.gaps and "snapshot" in explanation.gaps[0])
        self.assertIn("Gaps in the record", render_decision(explanation))
        with self.assertRaises(ExplanationError):
            explain_decision(record, store, strict=True)


class ExplainActionTests(unittest.TestCase):
    def test_aud01_executed_action_resolves_to_inputs_limits_decision_and_evidence(self):
        h = harness()
        snap = h.snapshot()
        h.store.put_setting(KEY_AUTHORITY_PROFILE, h.profile.to_json())          # v1
        h.store.put_setting(KEY_AUTHORITY_PROFILE, h.profile.to_json())          # v2
        h.store.put_setting(KEY_AUTHORITY_PROFILE, h.profile.to_json())          # v3 == the harness profile version
        record = decision(snap.snapshot_id, kind="execution.select_validated_tactic", selected={"kind": "select_validated_tactic", "parameters": {"tactic_catalog_id": "counter-02"}}, model_versions={"settings:authority_profile": "3"})
        h.store.insert_decision(record)
        intent = h.ready(h.tactic_intent(snap, decision_id=record.decision_id), snap)
        report = h.executor().run_next(h.snapshot)
        self.assertIs(report.state, ActionState.CONFIRMED, report.reason)
        explanation = explain_action(intent.action_id, h.store)
        self.assertIsInstance(explanation, ActionExplanation)
        self.assertEqual(explanation.state, "CONFIRMED")
        self.assertEqual(explanation.inputs["parameters"]["tactic_catalog_id"], "counter-02")
        self.assertEqual(explanation.limits["status"], "available")
        self.assertEqual(explanation.limits["authority_profile_version"], 3)
        self.assertEqual(explanation.limits["mode"], "scoped_execution")
        self.assertEqual(explanation.decision.decision_id, record.decision_id)
        self.assertEqual(explanation.decision.snapshot_id, snap.snapshot_id)
        self.assertEqual([t["to"] for t in explanation.transitions], ["VALIDATED", "QUEUED", "EXECUTING", "VERIFYING", "CONFIRMED"])
        self.assertEqual(len(explanation.inputs_sent), 2)
        self.assertEqual(explanation.verdict["kind"], "confirmed")
        self.assertTrue(explanation.before_evidence and explanation.after_evidence)
        self.assertTrue(all(s.observed_at and s.source for s in explanation.after_evidence))
        text = render_action(explanation)
        self.assertIn("Authorised under profile v3", text)
        self.assertIn("UI inputs sent: 2", text)
        self.assertIn("Verified by selected_tactic_matches_catalog: confirmed", text)
        body = explanation.to_json()
        self.assertEqual(body["idempotency_key"], intent.idempotency_key)

    def test_missing_links_fail_loudly(self):
        h = harness()
        snap = h.snapshot()
        with self.assertRaisesRegex(ExplanationError, "not in the store"):
            explain_action("act-nope", h.store)
        orphan = h.tactic_intent(snap, decision_id="dec-not-stored")
        with self.assertRaisesRegex(ExplanationError, "references decision"):
            explain_action(orphan.action_id, h.store)
        no_decision = IntentFactory(h.store).create("set.training", "training.set", snap, {"routes": ["/squad"]}, {"settings": {"intensity": "Double"}}, verification="training_settings_reread")
        with self.assertRaisesRegex(ExplanationError, "records no decision"):
            explain_action(no_decision.action_id, h.store)
        other = h.snapshot()
        record = decision(other.snapshot_id)
        h.store.insert_decision(record)
        mismatched = h.tactic_intent(snap, decision_id=record.decision_id, tactic_id="counter-02", name=None)
        with self.assertRaisesRegex(ExplanationError, "built on snapshot"):
            explain_action(mismatched.action_id, h.store)

    def test_limits_missing_when_profile_version_was_never_stored(self):
        h = harness()
        snap = h.snapshot()
        record = decision(snap.snapshot_id)
        h.store.insert_decision(record)
        intent = h.ready(h.tactic_intent(snap, decision_id=record.decision_id), snap)
        explanation = explain_action(intent.action_id, h.store)
        self.assertEqual(explanation.limits["status"], "missing")
        self.assertEqual(explanation.limits["authority_profile_version"], 3)
        self.assertIn("Authority limits: missing", render_action(explanation))


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------


class NotifyTests(unittest.TestCase):
    def test_unchanged_poll_produces_no_notification(self):
        sink = MemorySink()
        notifier = Notifier([sink])
        for _ in range(50):
            notifier.poll_unchanged()
        self.assertEqual(sink.delivered, [])
        self.assertEqual(notifier.history, [])

    def test_five_meaningful_events_reach_every_sink_and_the_journal(self):
        store = Store.memory()
        seen: list[Notification] = []
        memory = MemorySink()
        logger = logging.getLogger("fm_bot.test.notify")
        notifier = Notifier([memory, CallbackSink(seen.append), LogSink(logger)], store=store)
        report = MissingCapabilityReport("respond.inbox")
        report.add("inbox_text", "not decoded")
        previous = decision("s", selected={"kind": "advise.lineup", "player_ids": [1, 2]})
        current = decision("s", selected={"kind": "advise.lineup", "player_ids": [1, 3]})
        with self.assertLogs(logger, level="INFO") as logs:
            self.assertIsNotNone(notifier.required_action("the calendar cannot move on", ["unread board request"], ref_id="b1"))
            self.assertIsNotNone(notifier.unsupported_workflow(report, ref_id="b1"))
            self.assertIsNotNone(notifier.plan_changed(previous, current))
            self.assertIsNotNone(notifier.completed("act-1", "switch tactic"))
            self.assertIsNotNone(notifier.failed("switch tactic", "UNCERTAIN: timeout", ref_id="act-2"))
        self.assertEqual([n.kind for n in memory.delivered], list(NotificationKind))
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(logs.output), 5)
        self.assertEqual(len(store.journal_entries(kind="notification")), 5)
        self.assertIsNone(notifier.required_action("x", []), "no blockers, no notification")
        self.assertIsNone(notifier.unsupported_workflow(MissingCapabilityReport("advise.lineup")), "nothing missing, no notification")

    def test_identical_repeats_are_suppressed_but_changes_are_not(self):
        sink = MemorySink()
        notifier = Notifier([sink])
        notifier.required_action("calendar", ["a", "b"], ref_id="b1")
        notifier.required_action("calendar", ["b", "a"], ref_id="b1")
        self.assertEqual(len(sink.delivered), 1)
        self.assertEqual(notifier.suppressed, 1)
        notifier.required_action("calendar", ["a", "b", "c"], ref_id="b1")
        self.assertEqual(len(sink.delivered), 2)
        relaxed = Notifier([sink], NotificationPolicy(suppress_repeats=False))
        relaxed.completed("act-1", "x")
        relaxed.completed("act-1", "x")
        self.assertEqual(len(sink.delivered), 4)

    def test_policy_can_disable_kinds(self):
        sink = MemorySink()
        notifier = Notifier([sink], NotificationPolicy(enabled=frozenset({NotificationKind.FAILED})))
        self.assertIsNone(notifier.completed("act-1", "x"))
        self.assertIsNotNone(notifier.failed("x", "y"))
        self.assertEqual([n.kind for n in sink.delivered], [NotificationKind.FAILED])

    def test_material_plan_change_compares_selected_action_and_binding_constraints(self):
        same = decision("s", selected={"kind": "advise.lineup", "player_ids": [1, 2], "score": 0.5})
        rescored = decision("s", selected={"kind": "advise.lineup", "player_ids": [1, 2], "score": 0.9})
        self.assertEqual(material_plan_change(same, rescored), [], "a different score for the same action is not material")
        changed = decision("s", selected={"kind": "advise.lineup", "player_ids": [1, 3]})
        self.assertTrue(any("selected action changed" in r for r in material_plan_change(same, changed)))
        bound = decision("s", selected=same.selected, constraints=[{"name": "wage_headroom", "status": "fail"}])
        self.assertTrue(any("binding constraints changed" in r for r in material_plan_change(same, bound)))
        self.assertEqual(material_plan_change(None, decision("s")), [], "a first decision selecting nothing is not a change")
        self.assertTrue(material_plan_change(None, same))
        self.assertEqual(notify.NOTIFY_POLICY_VERSION, NotificationPolicy().version)
        self.assertEqual(explain.EXPLAIN_VERSION, "interface.explain/1")


if __name__ == "__main__":
    unittest.main()
