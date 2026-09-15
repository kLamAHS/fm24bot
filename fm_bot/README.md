# fm_bot: the club management bot

`fm_bot` is the club management bot described in
[docs/FM24_Bot_Design_Specification.md](../docs/FM24_Bot_Design_Specification.md).
It observes Football Manager 2024 through the read-only bridge in this
repository (`bridge/`, `api/`, `structures/`), keeps its own append-only
journal in SQLite, plans with explicit, reviewable baselines, and executes
supported changes through a separate UI adapter under a persistent authority
profile. Everything the bot cannot observe stays *unavailable*: null is not
zero, unknown is not false, and an unavailable estimate is a valid result.

The bridge and its tests (`tests/`) are untouched by the bot and are run
separately; their save-specific expectations are preserved.

## What is implemented, by delivery phase (spec 16.2)

| Phase | Deliverables in this tree | State of the exit gate |
| --- | --- | --- |
| 0 Observation foundation | `bridge_client/` (loopback transport, envelope/schema checks, per-read observation journal); `state/` (career, branch and checkpoint identity with continuity checks; exact `Money` with calendar-aware recurrence; `Observed` value statuses; consistent snapshot protocol with retries; SQLite store with append-only journal, intent transitions and locks; visibility masks) | OBS 01-03, ID 01 and VIS 01 pass offline against the scripted fixture bridge. |
| 1 Advisory club planner | `rules/` (per-action capability gates, eligibility provider contract, versioned competition rules profiles, mandatory-deadline heuristics and the Continue gate); `planning/roles.py`, `lineup.py` (exact Hungarian assignment with independent re-check and Hall-violator explanations), `minutes.py`, `objective.py`, `planner.py` (propose, evaluate, reconcile, decisions, intents); `interface/` (operator status, decision and action explanations, versioned settings, notifications); `python -m fm_bot plan / status / explain` | Advice is traceable to snapshot, settings and model versions; unavailable prerequisites block execution with a named `MissingCapabilityReport`. SEL 01-02 and CAL 01 pass offline. |
| 2 Controlled execution | `execution/adapter.py` (adapter protocol, versioned screen models and workflows, environment validation, `FakeAdapter` with fault injection, fail-closed `WindowsAdapter` stub); `lifecycle.py` (intents, idempotency keys, validation, expiry on context change); `executor.py` (single UI writer, preflight, EXECUTING persisted before the first input, bounded navigation retries, never-retried consequential steps, Stop); `verification.py` (effect established by independent readback, bridge corroboration); `reconciliation.py` (restart and uncertainty recovery without re-dispatch, permitted compensations only proposed); `rules/authority.py`; `orchestrator.py` (four run levels, manager lock, Stop, polling policy); `tests/test_narrow_loop.py` | The spec 16.3 narrow loop (identify, collect, propose, apply, verify, recover from an injected interruption) passes end to end **against the fake adapter only**. ACT 01-03, REC 01 and AUD 01 pass offline. No real UI workflow has been validated. |
| 3 Experimental laboratory | `experiments/manifests.py` (run and trial manifests with unrecorded-field statuses), `runner.py` (laboratory branches, restore through a `CheckpointRestorer`, treatment application, progression, outcome collection, repeatability characterisation, simulated rollouts kept apart from FM trials), `leakage.py` (as-of joins, branch isolation, fold-scoped fitting), `splits.py` (grouped and chronological splits, single-use final holdout), `evaluation.py` (metrics as `Observed`, clustered bootstrap, evidence gate) | EXP 01-02 pass offline with a fake restorer. The default `AdapterRestorer` fails closed: no adapter implements save loading, so every real trial is technically invalid until a validated restorer exists. |
| 4 Shared sporting and finance planning | `planning/finance.py` (commitment ledger reconciled to the observed payroll aggregate, calendar-exact cash-flow engine with enumerated uncertain receipts, risk policy, feasibility constraints with UNKNOWN as a non-pass), `negotiation.py` (complete-term offer parser, reservation package, negotiation state machine, clamp-to-reservation counter heuristic), `recruitment.py` (packages, marginal contribution by full re-solve, versatility, shadow values, trade-off table, succession gaps), `models/voi.py` (scouting value of information) | FIN 01-02 pass offline. Nothing has been run on live scenarios. `set.training` exists as an executable workflow on the fake adapter; there is no training scheduling baseline. |
| 5 Match and social coverage | `interactions/inbox.py` (inbox items, text provider contract, mandatory classification), `choices.py` (policy-ranked dialogue options with numeric limits outside any model), `language_model.py` (evidence-wrapped requests, schema validation, usage limits, no provider), `promises.py`; registration deadlines in `rules/deadlines.py`; `orchestrator.match_level` records match observations and refuses every live action (MAT 01) | MAT 01-02 pass offline. Substitutions, set pieces and verified match events are **not** implemented: the bridge's match timeline is unclassified (`match_event_order` unresolved), so `match.substitute` is capability-blocked. |
| 6 Model improvement and autonomy | `models/baselines.py` (rolling, exponentially weighted and opponent-adjusted ridge forecasts with widened intervals in unfamiliar contexts), `calibration.py` (Brier, log score, reliability, coverage), `registry.py` (immutable versions, release gate, baseline fallback), `dynamics.py` (fatigue fits that refuse unidentifiable parameters), `fracdiff.py` (admission test against the accounting baseline) | MOD 01 passes offline. No held-out benchmark has been run, no model is released, and the club-autonomy season-completion gate has not been attempted. `club_autonomy` exists only as an authority mode. |

Every threshold, weight and interval in the tree is a module-level constant
with a version string, and every solution that is not exact is labelled a
heuristic in its docstring. Timing figures (lock staleness, step timeouts,
poll intervals, solver budgets) are engineering budgets, not measurements.

## How to run

The bridge and the bot run in **separate interpreters**. The bridge decodes
memory on Windows next to the game; the bot only ever talks to it over the
loopback JSON API and never reads memory itself.

1. Bridge (Windows, supported FM24 build, save loaded and idle), in its own
   interpreter, from the repository root:

   ```text
   python -m api.server
   ```

   (or double-click `start-api.cmd`). It listens on `http://127.0.0.1:8765`
   only. See the top-level README for the supported build and routes.

2. Bot, in a second Python 3.11+ interpreter (standard library only; on
   Windows or, for offline work, anywhere), from the repository root:

   ```text
   python -m fm_bot status                      # connection, career, authority, next action, prerequisites
   python -m fm_bot register --label "Wycombe main" --confirm-lineage
   python -m fm_bot confirm-lineage [--reason TEXT]   # vouch that the loaded save is the registered career
   python -m fm_bot snapshot [--json]           # one consistent snapshot and its status
   python -m fm_bot plan [--json]               # advisory plan with capability reports
   python -m fm_bot config get [name] / config set <name> <value> [--reason TEXT]
   python -m fm_bot run --once [--authority observe|advise|scoped|autonomy]
   python -m fm_bot reconcile                   # settle intents left in flight by an earlier process
   python -m fm_bot explain <decision_id|action_id> [--evidence] [--json]
   ```

   Global options: `--db PATH` (default `fm_bot.sqlite3`), `--bridge-url URL`
   (loopback only), `--adapter fake|windows` (default `fake`; `windows` fails
   closed and says why). Exit codes: 0 ok, 1 game or bridge state (a
   disconnected bridge, an inconsistent snapshot, a stop for identity
   resolution), 2 setup or usage, 3 another bot instance holds the manager
   lock.

   The default authority mode is `advise`: proposals are recorded, nothing is
   sent to the game. Execution needs `config set authority_mode scoped`, an
   enabled family such as `config set action_families '["tactics"]'`, and a
   UI adapter that reports the workflow's capabilities. Today only the fake
   adapter does.

3. Tests. The bot suite (offline, no bridge needed):

   ```text
   python3 -m unittest discover -s fm_bot/tests -t . -p "test_*.py"
   python3 -m unittest fm_bot.tests.test_narrow_loop -v          # the spec 16.3 loop
   python3 -m unittest fm_bot.tests.test_acceptance_map -v       # the map below, checked against the spec
   ```

   The bridge's own suite is separate (`python -m unittest discover -s tests -v`)
   and has one Windows-only expectation.

## Acceptance-test map (spec section 14)

`fm_bot/tests/test_acceptance_map.py` holds this map and asserts that every
ID in the spec's table is claimed by at least one test whose own docstring or
name (or its class's) names the ID. Entries are `file::class::test`.

| ID | Requirement | Tests |
| --- | --- | --- |
| OBS 01 | Respect bridge connection and build status | `test_bridge_client.py::ConnectionStateTests::test_http_200_with_connected_false_is_disconnected`, `::test_unsupported_build_is_reported`; `test_snapshot.py::ConnectionTests::test_disconnected_stops_without_retry_loops`, `::test_unsupported_build_stops_without_retry` |
| OBS 02 | Preserve missing-value semantics | `test_views.py::ReadinessTests::test_stale_readiness_is_stale_not_a_number`; `test_views.py::FinanceViewTests::test_null_money_stays_null`; `test_views.py::PlayerStateTests::test_default_eligibility_is_missing`; `test_status.py::ObservedInvariantTests::test_require_raises_unavailable_with_status_and_reason` |
| OBS 03 | Reject inconsistent snapshots | `test_snapshot.py::InjectedChangeTests::test_time_change_between_reads_is_retried_then_accepted`, `::test_session_change_during_collection_is_rejected`, `::test_identity_change_on_reread_stops_immediately`, `::test_action_critical_field_change_is_retried` |
| ID 01 | Preserve career and branch lineage | `test_identity.py::RegistryTests::test_reloading_earlier_and_later_checkpoints_are_distinct`, `::test_no_cross_branch_history_merge`, `::test_laboratory_fork_records_parent_and_checkpoint` |
| VIS 01 | Enforce information mode before inference | `test_visibility.py::ApplyModeTests::test_manager_visible_removes_attributes_and_records_lineage`; `test_visibility.py::FeatureGuardTests::test_privileged_features_are_refused_in_manager_visible_mode`; `test_visibility.py::ModelModeTests::test_privileged_model_cannot_be_relabeled_manager_visible` |
| FIN 01 | Preserve money units and timing | `test_units.py::MoneyArithmeticTests::test_mixed_currency_raises`, `::test_mixed_period_raises`; `test_units.py::TotalOverTests::test_weekly_wage_over_a_month_is_calendar_exact`, `::test_weekly_and_monthly_only_combine_after_expansion`, `::test_double_counting_is_visible_through_counts`; `test_finance.py::MoneyTimingTests::test_monthly_instalments_follow_calendar_month_ends` |
| FIN 02 | Bound complete commitments | `test_finance.py::PackageFeasibilityTests::test_deal_inside_transfer_budget_but_below_cash_reserve_is_rejected`; `test_negotiation.py::AcceptanceTests::test_deal_within_budget_but_outside_cash_reserve_cannot_be_accepted` |
| SEL 01 | Submit only verified legal selections | `test_planning_lineup.py::EligibilityGateTests::test_sel01_observed_ineligible_player_is_never_assigned_in_either_mode`; `test_rules_eligibility.py::VerifiedEligibleTests::test_sel01_confirmed_ineligible_players_are_false_with_reason`, `::test_sel01_loaned_out_player_detected_from_bridge_contracts` |
| SEL 02 | Report infeasibility | `test_planning_lineup.py::InfeasibilityTests::test_sel02_two_goalkeeper_slots_but_one_goalkeeper` |
| ACT 01 | Validate target and fresh context | `test_execution_executor.py::PreflightTests::test_act01_changed_tactic_expires_queued_action_without_input`; `test_narrow_loop.py::FreshContextTests::test_act01_tactic_changed_between_snapshot_and_execution_expires_the_intent` |
| ACT 02 | Prevent uncertain duplicate effects | `test_execution_reconciliation.py::Act02Tests::test_timeout_after_acceptance_reconciles_without_second_dispatch`; `test_narrow_loop.py::InterruptionTests::test_rec01_act02_process_dies_after_executing_and_restart_reconciles_without_a_second_dispatch`, `::test_act02_absent_effect_after_timeout_is_failed_with_evidence_and_never_retried`, `::test_act02_unreadable_readback_keeps_the_intent_uncertain_across_a_restart` |
| ACT 03 | Respect human control | `test_execution_executor.py::HumanControlTests::test_act03_stop_cancels_queue_and_blocks_input`; `test_orchestrator.py::ExecutionTests::test_act03_stop_cancels_queued_work_and_prevents_input`; `test_narrow_loop.py::HumanControlTests::test_act03_stop_pressed_before_execution_sends_no_input`, `::test_act03_focus_loss_pauses_before_the_first_input` |
| REC 01 | Resume safely after interruption | `test_execution_reconciliation.py::RestartTests::test_rec01_in_flight_intent_is_reconciled_never_requeued`; `test_orchestrator.py::ConnectionTests::test_rec01_in_flight_intent_is_reconciled_on_connect_without_input`; `test_narrow_loop.py::InterruptionTests::test_rec01_act02_process_dies_after_executing_and_restart_reconciles_without_a_second_dispatch` |
| MAT 01 | Gate live decisions on timeline validity | `test_orchestrator.py::MatchLevelTests::test_mat01_live_match_only_records_observations` |
| MAT 02 | Verify participants and match rules | `test_capabilities.py::MatchParticipantTests::test_mat02_retained_remnants_and_unknown_substitution_rules_block_match_actions_only`; `test_planning_lineup.py::MatchRulesTests::test_mat02_unknown_substitution_rules_block_submission_and_name_the_capability` |
| CAL 01 | Handle mandatory game workflow | `test_rules_deadlines.py::ContinueGateTests::test_cal01_unread_required_decision_blocks_continue_until_resolved`, `::test_cal01_read_but_unconfirmed_messages_stay_visible` |
| EXP 01 | Produce reproducible manifests | `test_experiments_manifests.py::TrialManifestTests::test_trial_maps_to_checkpoint_build_policy_treatment_and_outcomes` |
| EXP 02 | Prevent future-information leakage | `test_experiments_leakage.py::DetectLeakageTests::test_deliberately_contaminated_dataset_fails` |
| MOD 01 | Gate model releases | `test_models_registry.py::ResolutionTests::test_failed_calibration_falls_back`, `::test_unsupported_feature_schema_in_context_falls_back` |
| AUD 01 | Explain every executed action | `test_interface.py::ExplainActionTests::test_aud01_executed_action_resolves_to_inputs_limits_decision_and_evidence`; `test_narrow_loop.py::HappyPathTests::test_identify_collect_propose_authorise_apply_verify` |

These are offline contract and fault-injection tests. They precede, and do
not replace, live UI trials; the spec's engineering gate of 100 varied
successful end-to-end cases per newly enabled consequential workflow has not
been run for any workflow.

## Explicit limits

* **No Windows UI adapter has been validated.** `execution.adapter.WindowsAdapter`
  is a fail-closed stub: it reports no capabilities, identifies no screen,
  sends no input and answers every readback `unsupported`. FM's accessibility
  support is untested. Every executable workflow (`select_validated_tactic`,
  `submit.lineup`, `set.training`, `commit.contract`, `navigate`) exists only
  against the fake screen model.
* **Fake adapter only.** The narrow loop, interruption, Stop and expiry tests
  prove the control logic, not the game. The tactic catalog used by the loop
  test is a fixture; a real catalog entry must map to observed settings and a
  tested workflow (spec 17.2). Nothing in the bridge exposes a catalog.
* **No live match control.** The `/match` feed's timeline is unclassified and
  `match_event_order` is unresolved, so the orchestrator only records match
  observations and refuses every live action. Same-match resume after a
  restart is untested.
* **No language model provider is bundled.** `interactions.language_model`
  defines the request contract, evidence wrapping, schema validation and
  usage limits; `NoLanguageModel` and a scripted test double are the only
  implementations. Numeric limits are enforced outside any model.
* **No save restoration in production, and none validated in the laboratory.**
  Production recovery never loads an old save. The laboratory runner needs a
  `CheckpointRestorer` that no adapter provides yet, so real trials are
  technically invalid until one is validated.
* **Capabilities the bridge does not decode stay blocked.** Injury,
  suspension, loan-absence and registration eligibility, inbox text, pending
  actions, contract clauses, competition rules and tactic readback all need
  providers. Without them `submit.lineup`, `respond.inbox`, `commit.contract`
  and `progress.continue` are blocked by name. Advice keeps flowing.
* **No performance claims.** Wall-clock figures in the tree are budgets.
  Nothing has been benchmarked, and no season has been completed under any
  autonomy mode.

## Dependency policy

* The bot is **Python 3.11+ standard library only**. There is no
  `requirements.txt` for `fm_bot`, and no third-party import anywhere in the
  package: no numpy, scipy, OR-Tools, pywinauto or HTTP client. Exact money
  arithmetic uses integers and `fractions.Fraction`; the lineup solver is a
  pure-Python Hungarian method with an independent feasibility re-check;
  statistics use `statistics` and seeded `random`.
* Optional integrations (an accessibility library for the Windows adapter, a
  language model provider) are imported lazily inside a function and fail
  closed with an explicit status. Their absence is reported, never raised at
  import time, and never converted into a default value.
* The bot never writes game memory, never suspends the process, never uses a
  memory editor, and only talks to the bridge over loopback. The UI adapter is
  the single writer to the game and is a separate component from the bridge
  and the planner.
* The spec's candidate later tools (OR-Tools, SciPy, a regularised modelling
  library, BoTorch) remain proposals. Adopting one requires confirming Windows
  and interpreter compatibility, pinning an exact version, and keeping the
  standard-library baseline as the fallback the release gate can resolve to.
