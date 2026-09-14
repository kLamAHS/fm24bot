# Runtime structures and public observations

FMProcess opens a separate handle using PROCESS_QUERY_INFORMATION | PROCESS_VM_READ (0x410). It owns and closes that handle, enumerates modules with PSAPI, reads live PE sections and validates pointers, lengths and container bounds. No process writes, suspension, injected code or remote calls occur.

Database resolves a code signature to the Person registry. FMBridge builds a UID index for supported pure Player records and follows owned pointers for subsequent requests. The observed registry contains 82,953 Person records and 26,223 pure Players. These counts do not mean every profile was independently UI validated; bulk decoding may reject unsupported data.

```text
Executable AOB + signed RIP displacement → global owner
  Database → Person registry → typed Person → complete Player
  Human manager → employment contract → Team → Club
                                       Team → roster Players
  Human → inbox / reports / shortlists / transfer target collections
  Tactics and training managers → human record → team data
  Competition manager → calendar-year fixtures/results
  Match viewer manager → controller → decoded viewer frame → statistics
```

## Public models

Python dataclasses in structures/ carry values and public FM identifiers without memory addresses. API JSON uses their serialized fields. Null and status fields distinguish unknown/unavailable values from real zeroes or empty lists.

| Model | Returned observations |
|---|---|
| Player | Identity, DOB, age/as-of date, primary nationality, 47 attributes, positions, morale, timestamped readiness and employment/loan terms |
| Manager / Club | Manager identity; club identity and current team roster |
| Game | In-game date and time; no invented real-world timezone |
| Finances | Native GBP balance and transfer budget, weekly wage budget/payroll |
| Fixtures | Loaded calendar year, as-of date, teams, competition, kickoff and supported results |
| Staff | Owned staff identities, validated job labels and supported contract terms |
| Tactics | Selected tactic/slot, mentality, supported positions/roles and selected players |
| Inbox | Message IDs, dates, initialized times, unread state, sender and event type; prose remains null |
| Training | Committed calendar, recognized sessions and individual focus/configured intensity; current ratings remain null |
| Scouting / Shortlists / TransferTargets | Stored report metadata/knowledge, owned player lists and target state; unvalidated grades/expiry/terms remain null |
| MatchObservation | Explicit availability and reason; a supported viewer frame carries clock, score, team/player statistics |

Match source is match_viewer, clock_basis is retained_match_statistics and timeline is unclassified. Player condition uses the same retained basis and may lag. MatchFormation contains eleven starting FormationSlot records with basis=starting_lineup, without an inferred formation name. Each MatchPlayer includes starting_position and last_position; a substituted player can retain the latter. Virtual players have null public identity with an explicit identity_status, while their roster place and statistics remain available. Possession is a completed-pass-share estimate, labelled by possession_basis. The selected club roster is not an eligibility list. Stored reports describe prior scouting and are not a live worldwide knowledge estimate. Selected tactics are read from the committed creator, not a saved lineup copy.

## Freshness and consistency

Each reader validates current manager/team/club ownership, supported types, bounded containers and relevant date/registry state. It rereads owner pointers and observed fields before returning. The bridge never freezes the game: observations are not atomic, and undetected changes remain possible. Keep FM idle and retry 503 responses after transitions.

Readiness is a dated cache and is exposed only when its timestamp exactly matches current game time. Cold-loaded inbox messages may have B3=FF; their exact time is null with time_status=not_initialized. The API does not invoke getters or open UI panels to populate such data.

The HTTP service reconnects after detected process exit or invalidated context, with a two-second retry cooldown. It never returns a cached squad as current. Successful responses include observed_at (UTC), session_id (connection UUID) and data. The UUID is not a save identifier. A save reload may reuse registry addresses, so manager/team/club checks are required too.

The integrated process restart reproduced all currently returned non-match data, including controlled tactic/training/list/target changes. A second snapshot exposed the inbox initialization edge case and passed all 15 sampled routes after the fix. Active match and independent-career coverage are reported separately, without implying every undecoded field is supported. Multiple FM processes, multiple human managers and unemployed managers are outside the current supported context.

See [offsets](offsets.md), [signatures](signatures.md), [experiments](experiments.md), [observation progress](observation-progress.md) and the focused subsystem reports for validation scope.
