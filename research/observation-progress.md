# Observation coverage — delivery checkpoint

The read-only library and localhost API are implemented and tested for the gated Windows x64 Steam build 24.4.2+2081827. The delivery contains source, regression tests, discovery notes and paired UI/memory evidence. It does not decode every field in FM or include an AI decision maker/action controller.

## Implemented coverage

Player identity, DOB/age, primary nationality, 47 attributes, positions, morale, timestamped readiness, employment/loan terms; current manager/club/squad; basic finances and budgets; calendar-year fixtures/results; club staff; selected tactic/lineup; inbox metadata; committed training calendars and supported player settings; stored scout report metadata/knowledge; shortlists and transfer targets.

Match observations include retained clock/score, team statistics, player identity when decoded, ratings, goals, shots, yellow cards, substitution indicators, retained condition, starting/last positions and opposition starting slots. The active viewer handles virtual players with explicit unresolved identity.

## Completed validation

* **116 offline tests** passed, including six independent match-stat UI panels across two fixtures, numeric condition checks, virtual-player handling, strict object bounds and cold inbox initialization.
* **122 live API checks** passed on the restored February 17 named regression copy: [report](api-observation-validation.json).
* All **15 sampled routes** returned HTTP 200 from the updated running API: [final baseline](observation-checkpoint-final-baseline.json). The additional `/club` route is covered by the live checks.
* Full FM restart **45464 → 5464**, with API PID 42772 continuously running, passed **32 comparison checks**: [report](observation-expanded-restart-comparison.json). All 14 data models matched, shortlist list order normalized, while manager memory relocated and connection UUID changed. Disconnected observations were withheld with 503.
* A second active fixture in the restarted process passed **107 UI-based checks**: [Stevenage report](match-stevenage-after-restart-validation.json). This is a different fixture after restart, not a saved/restored paused match.
* Two saved snapshots of one Wycombe career were checked, including cold empty/non-empty lists and target collections. The cold inbox FF-time sentinel now returns null with `time_status=not_initialized`.
* All four protected original/regression save files retain their original SHA-256 hashes: [integrity report](save-integrity-observation-final.json).

## Explicit limits

Match replay/live classification, exact current simulation condition, red cards, injuries, current opposition formation and generated virtual-player names remain unvalidated or undecoded. Retained clock/score can lead a replay. Retained condition can lag simulation; its public basis and lag flag make this explicit. Starting formation does not establish the current eleven, and last positions may remain after substitution. Possession is labelled as an inference from completed-pass share.

Other unknowns include inbox prose/attachments, full scout prose/recommendation grades, worldwide scouting knowledge, target terms/extra labels, shortlist expiry, staff attributes/responsibilities, contract clauses, secondary nationalities, team instructions/all tactic roles, some training labels/positions, current training ratings/effective intensity, finance breakdowns/debts/scouting budget. Public unknown fields stay null or have an explicit status.

Validation is limited to this executable and two snapshots of one career. Other careers, multiple human managers, unemployed managers, multiple FM processes and future builds require separate validation. Bounded rereads detect many transitions but do not produce atomic snapshots.

The separate `work/FM24 Observation Regression.fm` retains controlled February 7 UI experiments for reproducing non-empty tactic/training/list/target restart checks. Match experiments used independent project copies. The named February 17 baseline is restored; currency/salary preferences are restored to USD/yearly, as recorded in the final delivery log. No protected save was overwritten.
