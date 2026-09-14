# Match observations and validation

The supported Steam build exposes retained match statistics through the active viewer. Production access is read-only 0x410. Runtime code was copied into inert COFF files for the already installed VS dumpbin; no game function was executed.

## Ownership and bounds

The AOB in bridge/match.py resolves MATCH_CONTROLLER_MANAGER@fmmatchviewer (observed module RVA 0x6364C68). Its +8 sentinel and +10 count own an MSVC tree. Nodes have left +0, parent +8, right +10, nil byte +19, session key +20 and controller +28. Select GAME_LIVE_MATCH_CONTROLLER; a latest-scores controller is a separate type.

Controller +20 → wrapper +0 → implementation +1C0 → GAME_MATCH. Code at RVAs 0x12AAEC0 and 0x127D470 corroborates the chain. GAME_MATCH is bounded at 0xE640, supported by constructor writes through +E630. Match key +28 and competition +680 must agree with the controller and result. The separate simulation session and initial snapshots are not public sources.

GAME_MATCH +930 → statistics; statistics +10 → FIXTURE_RESULT, +50/+58 → home/away team statistics. Controller +5D8 is a rejected cache: it remained 3–0 at half-time when the owned GAME_MATCH statistics and UI showed 55:03, 3–1. Captures labelled pre-kickoff from that early experiment were late captures, not zero-minute evidence.

Team statistics occupy 0x280 bytes. Their +240/+248 vector had 26 player-stat pointers per side, including 18 populated rows and empty index-FFFFFFFF rows. GAME_MATCH_PLAYER_STATS **allocation size is 0xF8**, established by allocator RVA 0x3ABF800 requesting F8 and constructor writes ending at F6. The earlier observed 0x100 was a pool stride; production reads exactly F8. Readers validate types, owners, bounds and reread observed fields before returning.

## Validated fields

All offsets below are hexadecimal. Confidence is high within these observed cases and this executable gate, not a claim about other careers/builds.

| Owner / offset | Type | Meaning / evidence |
|---|---|---|
| Result +64 / +68 | uint8 | Home/away score; six paused UI panels across two fixtures |
| Game +CDE4 / +CDE5 | uint8 | Retained second/minute; six paired panels and clock code RVA 0x1AD19D0 |
| Game +AA4 | uint32 flags | Validated in-play, half-time and full-time states; other period combinations unknown |
| Team +60 | float32 | xG, rounded to two decimals |
| Team +161 / +162 | uint8 | Shots / on target |
| Team +218 / +21B / +21D | uint8 | Corners / fouls / yellow cards |
| Team +DC / +DE | uint16 | Attempted / completed passes; UI percentage uses nearest integer, half up |
| Player stats +10 | uint32 | Internal Person index, not a public UID |
| Player stats +50 / +54 | int32 | Substitution in/out frame, -1 absent |
| Player stats +64 / +68 | uint32 | Starting / last assigned position codes |
| Player stats +76 / +78 | uint16 | Rating / prior rating; UI rounding, unused bench rating withheld |
| Player stats +7A / +7B | uint8 | Shirt number / home-away side |
| Player stats +7D | uint8 | Retained condition percentage, 0..100; three independent numeric post-match checks and writer code |
| Player stats +7F / +84 / +85 / +9A | uint8 | Goals / shots / on target / yellow cards |

Possession is the observed completed-pass-share percentage; it matches all six panels but remains an **inference**, explicitly labelled `inferred_from_completed_pass_share`. It is not a decoded possession-duration counter.

## UI comparisons and restart

| Fixture | Clock | Score | Shots | xG | Possession |
|---|---|---|---|---|---|
| Wycombe–Peterborough | 45:00 | 3–0 | 5–4 | .88–.17 | 57–43 |
| Wycombe–Peterborough | 55:03 | 3–1 | 5–6 | .88–1.11 | 54–46 |
| Wycombe–Peterborough | 75:31 | 5–1 | 9–7 | 2.14–1.15 | 55–45 |
| Wycombe–Peterborough | 90:00 | 6–1 | 11–11 | 2.76–1.60 | 53–47 |
| Stevenage–Wycombe | 45:00 | 0–2 | 3–5 | .29–1.13 | 55–45 |
| Stevenage–Wycombe | 90:00 | 1–3 | 9–10 | .59–1.80 | 45–55 |

Corresponding `match-details-*.json` and `ui/match-*.png` files retain the evidence. Offline tests assert the independently transcribed panels, goal/card players, substitutions and detailed ratings. Fresh detailed player tables corroborated all 22 Peterborough-fixture starter ratings and 36 shirt numbers; the bottom ratings bar initially held stale values. Eleven Wycombe full-time ratings were independently checked in the Stevenage fixture.

The integrated restart changed FM PID 45464 → 5464 and passed 32 checks across non-match observations. The second fixture ran in PID 5464 on an independent February 17 project save: `match-stevenage-after-restart-validation.json` records 107 passing checks against the active 90:00, 1–3 viewer, including score/clock, team counters, 36-player goals/cards, eleven ratings and starting formation. This validates resolution after process restart and in a different fixture; it does not claim the same paused match was saved/restarted.

## Condition and positions

Writer RVA 0x1B4E640 updates MATCH_PLAYER@simatch +240 (int32, 10,000 units per percentage point) using +244 and baseline +2CA, then divides by 10,000 and stores a byte through actor +16E0 → stats +7D. This supports the field meaning beyond coincidental values. The retained byte can lag the actor: Murić's byte was 98 while the 28:33 actor value was about 94.68. It must not be advertised as an exact current simulation value.

After Stevenage full-time processing, existing numeric Fitness panels showed McCarthy **5900**, Murić **9500**, McCleary **7100** on FM's 0..10000 player scale. Their retained match bytes were **59, 95, 71**. Screenshots `ui/match-stevenage-post-condition-*.png` and `post-match-readiness-raw.json` preserve these independent checks. Detail dialogs were dismissed with Escape without editing/applying values. Production returns a whole percentage, `condition_basis=retained_match_statistics`, `condition_may_lag=true`. Values over 100 are rejected. Match sharpness +7E remains unvalidated.

All eleven Stevenage starting positions were compared with the Opposition panel at 28:33: GK, DCR, DC, DCL, WBR, DM, WBL, MCR, MCL, STCR, STCL. They are returned as slots with `basis=starting_lineup`, not a guessed formation name. +68 is a last assignment: Katongo retained DR after coming off; Edwards changed DCR→DCL. It cannot establish a current eleven by itself.

## Virtual players and timeline limits

Stevenage's unused substitute goalkeeper, shirt 27, is a VIRTUAL_PLAYER@db Person subobject at offset 30. The UI calls him Ady Cornick, but its generated name layout has not been decoded. The registry index now supports this exact type as well as ACTUAL_PLAYER at offset 278. The public row retains statistics with null id/name and `identity_status=virtual_player_identity_not_decoded`. No actual-player name offsets are applied to it. This fixture has 35 resolved identities and one explicit unresolved identity.

During a paused goal replay the visible clock was **05:05**, while retained statistics already held **05:17, 0–1**. Skipping the replay brought the visible clock to 05:17. Controller candidates +128, +1D8, +344, +354 and +13C/+360 changed, but one paired replay is insufficient to promote a classifier. `timeline=unclassified` and `clock_basis=retained_match_statistics` remain explicit. API clock/score must not be treated as the current replay frame.

Red cards and injuries have no positive validated match cases, so remain null. Extra-time phases, replay/live classification, current opposition formation, generated player names and exact current simulation condition remain undecoded. All validation is within two snapshots of one career and the exact build gate.
