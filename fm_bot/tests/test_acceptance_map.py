"""The acceptance-test map for specification section 14.

Every acceptance ID in the spec's table (OBS 01 ... AUD 01) must be claimed
by at least one bot test, and the claim must be visible in that test's own
docstring or name (or its class's), not only in a file's module docstring.
:data:`ACCEPTANCE_MAP` is the reviewed map: it is what ``fm_bot/README.md``
lists, and these tests keep it honest against the spec and the test files.
"""
from __future__ import annotations

import ast
import re
import unittest
from os import path

TESTS_DIR = path.dirname(path.abspath(__file__))
PACKAGE_DIR = path.dirname(TESTS_DIR)
REPO_ROOT = path.dirname(PACKAGE_DIR)
SPEC_PATH = path.join(REPO_ROOT, "docs", "FM24_Bot_Design_Specification.md")
README_PATH = path.join(PACKAGE_DIR, "README.md")

# ID -> "test_file.py::TestClass::test_method" entries. Reviewed by hand; verified by the tests below.
ACCEPTANCE_MAP: dict[str, tuple[str, ...]] = {
    "OBS 01": (
        "test_bridge_client.py::ConnectionStateTests::test_http_200_with_connected_false_is_disconnected",
        "test_bridge_client.py::ConnectionStateTests::test_unsupported_build_is_reported",
        "test_snapshot.py::ConnectionTests::test_disconnected_stops_without_retry_loops",
        "test_snapshot.py::ConnectionTests::test_unsupported_build_stops_without_retry",
    ),
    "OBS 02": (
        "test_views.py::ReadinessTests::test_stale_readiness_is_stale_not_a_number",
        "test_views.py::FinanceViewTests::test_null_money_stays_null",
        "test_views.py::PlayerStateTests::test_default_eligibility_is_missing",
        "test_status.py::ObservedInvariantTests::test_require_raises_unavailable_with_status_and_reason",
        "test_interactions_inbox.py::UnresolvedMandatoryTests::test_obs02_a_reading_that_is_not_current_answers_nothing_but_still_classifies",
    ),
    "OBS 03": (
        "test_snapshot.py::InjectedChangeTests::test_time_change_between_reads_is_retried_then_accepted",
        "test_snapshot.py::InjectedChangeTests::test_session_change_during_collection_is_rejected",
        "test_snapshot.py::InjectedChangeTests::test_identity_change_on_reread_stops_immediately",
        "test_snapshot.py::InjectedChangeTests::test_action_critical_field_change_is_retried",
        "test_execution_executor.py::ActionCriticalFreshnessTests::test_obs03_a_same_tick_change_to_a_target_route_stops_the_input",
        "test_narrow_loop.py::FreshContextTests::test_obs03_a_same_tick_change_to_the_target_route_stops_the_input_end_to_end",
    ),
    "ID 01": (
        "test_identity.py::RegistryTests::test_reloading_earlier_and_later_checkpoints_are_distinct",
        "test_orchestrator.py::ExecutionTests::test_id01_a_restarted_orchestrator_judges_continuity_against_the_witnessed_anchor",
        "test_cli.py::ConfirmLineageTests::test_id01_a_restarted_process_detects_a_save_reloaded_from_an_earlier_point",
        "test_identity.py::RegistryTests::test_no_cross_branch_history_merge",
        "test_identity.py::RegistryTests::test_laboratory_fork_records_parent_and_checkpoint",
    ),
    "VIS 01": (
        "test_visibility.py::ApplyModeTests::test_manager_visible_removes_attributes_and_records_lineage",
        "test_visibility.py::FeatureGuardTests::test_privileged_features_are_refused_in_manager_visible_mode",
        "test_visibility.py::ModelModeTests::test_privileged_model_cannot_be_relabeled_manager_visible",
    ),
    "FIN 01": (
        "test_units.py::MoneyArithmeticTests::test_mixed_currency_raises",
        "test_units.py::MoneyArithmeticTests::test_mixed_period_raises",
        "test_units.py::TotalOverTests::test_weekly_wage_over_a_month_is_calendar_exact",
        "test_units.py::TotalOverTests::test_weekly_and_monthly_only_combine_after_expansion",
        "test_units.py::TotalOverTests::test_double_counting_is_visible_through_counts",
        "test_finance.py::MoneyTimingTests::test_monthly_instalments_follow_calendar_month_ends",
        "test_finance.py::MoneyTimingTests::test_fin01_a_contract_that_has_not_started_explains_none_of_todays_payroll",
        "test_finance.py::CashFlowEngineTests::test_fin01_forecast_receipts_outside_the_horizon_are_bucketed_never_credited_or_dropped",
    ),
    "FIN 02": (
        "test_finance.py::PackageFeasibilityTests::test_deal_inside_transfer_budget_but_below_cash_reserve_is_rejected",
        "test_negotiation.py::AcceptanceTests::test_deal_within_budget_but_outside_cash_reserve_cannot_be_accepted",
    ),
    "SEL 01": (
        "test_planning_lineup.py::EligibilityGateTests::test_sel01_observed_ineligible_player_is_never_assigned_in_either_mode",
        "test_rules_eligibility.py::VerifiedEligibleTests::test_sel01_confirmed_ineligible_players_are_false_with_reason",
        "test_rules_eligibility.py::VerifiedEligibleTests::test_sel01_loaned_out_player_detected_from_bridge_contracts",
    ),
    "SEL 02": (
        "test_planning_lineup.py::InfeasibilityTests::test_sel02_two_goalkeeper_slots_but_one_goalkeeper",
        "test_interactions_promises.py::MinutesTests::test_sel02_two_promises_to_one_player_reserve_the_same_minutes_in_either_ledger_order",
    ),
    "ACT 01": (
        "test_execution_executor.py::PreflightTests::test_act01_changed_tactic_expires_queued_action_without_input",
        "test_narrow_loop.py::FreshContextTests::test_act01_tactic_changed_between_snapshot_and_execution_expires_the_intent",
        "test_execution_executor.py::PreflightTests::test_act01_intent_missing_a_workflow_parameter_is_cancelled_without_input",
    ),
    "ACT 02": (
        "test_execution_reconciliation.py::Act02Tests::test_timeout_after_acceptance_reconciles_without_second_dispatch",
        "test_narrow_loop.py::InterruptionTests::test_rec01_act02_process_dies_after_executing_and_restart_reconciles_without_a_second_dispatch",
        "test_narrow_loop.py::InterruptionTests::test_act02_absent_effect_after_timeout_is_failed_with_evidence_and_never_retried",
        "test_narrow_loop.py::InterruptionTests::test_act02_unreadable_readback_keeps_the_intent_uncertain_across_a_restart",
    ),
    "ACT 03": (
        "test_execution_executor.py::HumanControlTests::test_act03_stop_cancels_queue_and_blocks_input",
        "test_orchestrator.py::ExecutionTests::test_act03_stop_cancels_queued_work_and_prevents_input",
        "test_narrow_loop.py::HumanControlTests::test_act03_stop_pressed_before_execution_sends_no_input",
        "test_narrow_loop.py::HumanControlTests::test_act03_focus_loss_pauses_before_the_first_input",
    ),
    "REC 01": (
        "test_execution_reconciliation.py::RestartTests::test_rec01_in_flight_intent_is_reconciled_never_requeued",
        "test_orchestrator.py::ConnectionTests::test_rec01_in_flight_intent_is_reconciled_on_connect_without_input",
        "test_narrow_loop.py::InterruptionTests::test_rec01_act02_process_dies_after_executing_and_restart_reconciles_without_a_second_dispatch",
        "test_narrow_loop.py::ManagerLockTests::test_rec01_a_heartbeat_older_than_the_stale_threshold_is_taken_over",
    ),
    "MAT 01": (
        "test_orchestrator.py::MatchLevelTests::test_mat01_live_match_only_records_observations",
    ),
    "MAT 02": (
        "test_capabilities.py::MatchParticipantTests::test_mat02_retained_remnants_and_unknown_substitution_rules_block_match_actions_only",
        "test_planning_lineup.py::MatchRulesTests::test_mat02_unknown_substitution_rules_block_submission_and_name_the_capability",
    ),
    "CAL 01": (
        "test_rules_deadlines.py::ContinueGateTests::test_cal01_unread_required_decision_blocks_continue_until_resolved",
        "test_rules_deadlines.py::ContinueGateTests::test_cal01_read_but_unconfirmed_messages_stay_visible",
        "test_orchestrator.py::ExecutionTests::test_cal01_continue_is_confirmed_by_the_game_advancing_then_settles_and_recollects",
        "test_orchestrator.py::ExecutionTests::test_cal01_a_second_continue_follows_the_confirmed_first_one",
        "test_orchestrator.py::ExecutionTests::test_cal01_an_unverifiable_continue_is_closed_explicitly_instead_of_wedging_the_calendar",
    ),
    "EXP 01": (
        "test_experiments_manifests.py::TrialManifestTests::test_trial_maps_to_checkpoint_build_policy_treatment_and_outcomes",
        "test_experiments_preregistration.py::ImmutabilityTests::test_exp01_a_declaration_is_journaled_once_and_cannot_be_edited",
        "test_experiments_preregistration.py::ReportingTests::test_exp01_changing_the_outcome_or_the_seed_makes_the_result_exploratory",
    ),
    "EXP 02": (
        "test_experiments_leakage.py::DetectLeakageTests::test_deliberately_contaminated_dataset_fails",
        "test_experiments_splits.py::ChronologicalSplitTests::test_exp02_a_timed_cutoff_embargoes_a_unit_that_straddles_it",
    ),
    "MOD 01": (
        "test_models_registry.py::ResolutionTests::test_failed_calibration_falls_back",
        "test_models_registry.py::ResolutionTests::test_unsupported_feature_schema_in_context_falls_back",
        "test_models_dynamics.py::GappedLoadRecordTests::test_mod01_a_gap_in_the_load_record_refuses_the_fit_instead_of_misaligning_days",
    ),
    "AUD 01": (
        "test_interface.py::ExplainActionTests::test_aud01_executed_action_resolves_to_inputs_limits_decision_and_evidence",
        "test_narrow_loop.py::HappyPathTests::test_identify_collect_propose_authorise_apply_verify",
        "test_orchestrator.py::InboxAnswerTests::test_aud01_the_executed_answer_resolves_back_to_the_option_that_was_chosen",
    ),
}


def _section_14(spec_text: str) -> str:
    start = spec_text.index("\n## 14 ")
    return spec_text[start:spec_text.index("\n## 15 ", start)]


def spec_acceptance_ids(spec_text: str) -> list[str]:
    """The IDs in the first column of the section 14 table, in spec order."""
    return re.findall(r"^\| ([A-Z]{2,3} \d{2}) \|", _section_14(spec_text), flags=re.M)


def spec_requirements(spec_text: str) -> dict[str, str]:
    """ID -> the requirement wording in the second column of the section 14 table."""
    rows = re.findall(r"^\| ([A-Z]{2,3} \d{2}) \| ([^|]+?) \|", _section_14(spec_text), flags=re.M)
    return {acceptance_id: requirement.strip() for acceptance_id, requirement in rows}


def readme_rows(readme_text: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """The README's acceptance table as ``(id, requirement, entries)``, in README order.

    The table is the one headed ``| ID | Requirement | Tests |``; each Tests
    cell lists ``file::class::test`` entries in backticks, separated by
    ``<br>``. Parsing it is what lets a test refuse README drift.
    """
    header = "| ID | Requirement | Tests |"
    if header not in readme_text:
        return []
    body = readme_text[readme_text.index(header) + len(header):]
    rows: list[tuple[str, str, tuple[str, ...]]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 3 or not re.fullmatch(r"[A-Z]{2,3} \d{2}", cells[0]):
            continue
        entries = tuple(match.group(1).strip() for match in re.finditer(r"`([^`]+)`", cells[2]))
        rows.append((cells[0], cells[1], entries))
    return rows


def _pattern(acceptance_id: str) -> re.Pattern[str]:
    prefix, number = acceptance_id.split(" ")
    return re.compile(rf"{prefix}[ _-]?{number}", re.I)


def _names(acceptance_id: str, *texts: str | None) -> bool:
    pattern = _pattern(acceptance_id)
    return any(pattern.search(text) for text in texts if text)


class _TestFile:
    """The classes, test functions and docstrings of one test module."""

    def __init__(self, filename: str):
        self.filename = filename
        with open(path.join(TESTS_DIR, filename), encoding="utf-8") as handle:
            self.tree = ast.parse(handle.read())
        self.module_doc = ast.get_docstring(self.tree) or ""
        self.classes = {node.name: node for node in self.tree.body if isinstance(node, ast.ClassDef)}

    def function(self, class_name: str, function_name: str) -> ast.FunctionDef | None:
        cls = self.classes.get(class_name)
        if cls is None:
            return None
        return next((n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == function_name), None)

    def specific_hits(self, acceptance_id: str) -> list[str]:
        """Test functions that name the ID in their own or their class's docstring or name."""
        hits = []
        for cls in self.classes.values():
            class_doc = ast.get_docstring(cls) or ""
            for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")):
                if _names(acceptance_id, fn.name, ast.get_docstring(fn), cls.name, class_doc):
                    hits.append(f"{self.filename}::{cls.name}::{fn.name}")
        return hits

    def any_docstring_names(self, acceptance_id: str) -> bool:
        docs = [self.module_doc]
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                docs.append(ast.get_docstring(node) or "")
        return _names(acceptance_id, *docs)


def _test_files() -> dict[str, _TestFile]:
    import glob
    return {path.basename(p): _TestFile(path.basename(p)) for p in sorted(glob.glob(path.join(TESTS_DIR, "test_*.py")))}


def render_map() -> str:
    """The map as a Markdown table (what the README shows)."""
    lines = ["| ID | Tests |", "| --- | --- |"]
    for acceptance_id, entries in ACCEPTANCE_MAP.items():
        lines.append(f"| {acceptance_id} | " + "<br>".join(f"`{e}`" for e in entries) + " |")
    return "\n".join(lines)


class ReadmeTableTests(unittest.TestCase):
    """``fm_bot/README.md``'s acceptance table must say exactly what :data:`ACCEPTANCE_MAP` says.

    The README is the document an operator reads; the map is what the tests
    above check against the spec and the test files. If the two are allowed to
    disagree the README quietly starts claiming coverage that no test has, so
    this compares them entry by entry, in order.
    """

    def setUp(self):
        with open(README_PATH, encoding="utf-8") as handle:
            self.rows = readme_rows(handle.read())

    def test_readme_has_the_acceptance_table(self):
        self.assertTrue(self.rows, f"{README_PATH} has no parsable `| ID | Requirement | Tests |` table")

    def test_readme_lists_the_same_ids_in_the_same_order(self):
        self.assertEqual([row[0] for row in self.rows], list(ACCEPTANCE_MAP))

    def test_readme_lists_exactly_the_mapped_tests_for_every_id(self):
        listed = {row[0]: row[2] for row in self.rows}
        for acceptance_id, entries in ACCEPTANCE_MAP.items():
            with self.subTest(acceptance_id=acceptance_id):
                self.assertIn(acceptance_id, listed, f"{acceptance_id} is missing from the README table")
                self.assertEqual(
                    listed[acceptance_id], entries,
                    f"{acceptance_id}: the README table and ACCEPTANCE_MAP disagree; "
                    f"README has {listed[acceptance_id]}, the map has {entries}",
                )

    def test_readme_requirement_wording_is_the_spec_wording(self):
        with open(SPEC_PATH, encoding="utf-8") as handle:
            requirements = spec_requirements(handle.read())
        for acceptance_id, requirement, _ in self.rows:
            with self.subTest(acceptance_id=acceptance_id):
                self.assertEqual(requirement, requirements[acceptance_id])


class SpecTableTests(unittest.TestCase):
    def test_map_covers_exactly_the_spec_section_14_ids(self):
        with open(SPEC_PATH, encoding="utf-8") as handle:
            ids = spec_acceptance_ids(handle.read())
        self.assertEqual(len(ids), 20, ids)
        self.assertEqual(list(ACCEPTANCE_MAP), ids, "the map must list every section 14 ID, in spec order, and nothing else")
        for acceptance_id, entries in ACCEPTANCE_MAP.items():
            self.assertTrue(entries, f"{acceptance_id} has no test")


class MapAgainstTestFilesTests(unittest.TestCase):
    def setUp(self):
        self.files = _test_files()

    def test_every_mapped_test_exists_and_names_its_id(self):
        for acceptance_id, entries in ACCEPTANCE_MAP.items():
            for entry in entries:
                filename, class_name, function_name = entry.split("::")
                test_file = self.files.get(filename)
                self.assertIsNotNone(test_file, f"{acceptance_id}: {filename} is not under fm_bot/tests")
                fn = test_file.function(class_name, function_name)
                self.assertIsNotNone(fn, f"{acceptance_id}: {entry} does not exist")
                cls = test_file.classes[class_name]
                self.assertTrue(
                    _names(acceptance_id, fn.name, ast.get_docstring(fn), cls.name, ast.get_docstring(cls), test_file.module_doc),
                    f"{acceptance_id}: {entry} does not name the ID in its docstring, its class or its module",
                )

    def test_every_id_is_named_by_a_specific_test_not_only_a_module_docstring(self):
        for acceptance_id in ACCEPTANCE_MAP:
            hits = [hit for test_file in self.files.values() for hit in test_file.specific_hits(acceptance_id)]
            self.assertTrue(hits, f"{acceptance_id} is only mentioned at module level; add a focused test naming it")
            self.assertTrue(set(ACCEPTANCE_MAP[acceptance_id]) & set(hits), f"{acceptance_id}: none of the mapped tests names the ID specifically; candidates: {hits}")

    def test_every_id_appears_in_at_least_one_test_docstring(self):
        """The literal section 14 requirement: every acceptance ID appears in a test docstring under fm_bot/tests."""
        for acceptance_id in ACCEPTANCE_MAP:
            self.assertTrue(any(f.any_docstring_names(acceptance_id) for f in self.files.values()), acceptance_id)

    def test_rendered_map_lists_every_id(self):
        table = render_map()
        for acceptance_id in ACCEPTANCE_MAP:
            self.assertIn(f"| {acceptance_id} |", table)


if __name__ == "__main__":
    print(render_map())
    unittest.main()
