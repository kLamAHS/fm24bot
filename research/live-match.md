# Live viewer investigation

Work in progress on the exact gated Steam build; all external access is read-only 0x410. No game functions were executed. Existing VS dumpbin disassembled bounded copies of runtime code saved as inert COFF data under work/.

The unique complete MATCH_CONTROLLER_MANAGER@fmmatchviewer is reached through module RVA 0x6364C68. Its +8 tree sentinel and +0x10 count own an MSVC map. Tree nodes have left +0, parent +8, right +0x10, nil byte +0x19, uint64 session key +0x20, controller +0x28. Select complete GAME_LIVE_MATCH_CONTROLLER, excluding GAME_LIVE_LATEST_SCORES_CONTROLLER. GAME_MATCH_SESSION at the session-manager global is a separate simulation copy, not the displayed frame.

The live controller +0x20 points to a wrapper, whose first pointer leads to an implementation. Implementation +0x1C0 is the decoded viewer GAME_MATCH. +0x1B0 is an initial snapshot; controller +0x18 is another initial copy. Code at RVAs 0x12AAEC0 and 0x127D470 confirms the +0x20 / +0 / +0x1C0 chain. GAME_MATCH has an observed bound 0xE640, backed by constructor writes at +0xE630. Do not attribute neighboring allocations to it.

GAME_MATCH +0x28 has the session key. +0x930 points to match statistics, whose +0x10 is FIXTURE_RESULT, +0x50 and +0x58 are home and away statistics (observed stride 0x280). The result's +0x64 and +0x68 score bytes agree with the fixture decoder. Result +8/+0x10 identify the two database TEAM objects.

**A stale cache was rejected.** Controller +0x5D8 points to a statistics copy that remained at half-time 3–0 after the viewer showed 55:03 and 3–1. Only viewer GAME_MATCH +0x930 followed the visible goal and xG change. Earlier captures named `pre-kickoff` were acquired too late and equal the half-time state; those labels are not evidence of zero-minute validation.

Two paused observations so far:

| Field | Half-time | 55:03 | Candidate decoder |
|---|---:|---:|---|
| Score | 3–0 | 3–1 | result +0x64/+0x68 uint8 |
| xG | 0.88–0.17 | 0.88–1.11 | team stats +0x60 float32, rounded to 2 decimals |
| Shots | 5–4 | 5–6 | +0x161 uint8 |
| On target | 3–2 | 3–3 | +0x162 uint8 |
| Corners | 0–1 | 1–2 | +0x218 uint8 (also a matching +0x1DC counter; distinguish with further observations) |
| Fouls | 7–5 | 10–5 | +0x21B uint8 |
| Yellow cards | 1–1 | 2–1 | +0x21D uint8 |
| Passing completion | 87–83% | 86–85% | completed +0xDE / attempted +0xDC, uint16 |
| Possession | 57–43% | 54–46% | candidate share of completed passes, rounded; further validation needed |

Clock: GAME_MATCH +0xA84 is a signed tick count at 4 ticks/second, 240 ticks/minute. +0xCDE4 is the displayed second byte and +0xCDE5 the minute byte. Half-time has display 45:00 while raw A84 is 11882; at 55:03 raw A84 is 13213. Disassembly 0x1AD19D0 divides A84 by 240 and remainder by 4, applies period flags, and stores these displayed bytes. Period/replay flags remain under investigation; do not infer phase from raw simulation-frame +0x9E8.

Team stats +0x240/+0x248 hold a vector of 26 GAME_MATCH_PLAYER_STATS pointers, observed stride 0x100. +0x10 uint32 is the Person registry index, not the public FM UID; unused slots have 0xFFFFFFFF. +0x76 uint16 /100 is a candidate rating; +0x78 is another rating, zero for unused bench players. Shirt number +0x7A; team side +0x7B. +0x7D condition and +0x7E sharpness bytes are candidates, not yet numerically corroborated. +0x50/+0x54 are candidate substitution frame indices, -1 when absent.

GAME_MATCH +0xCEA8 and +0xCF38 have 18 pointers per side to MATCH_PLAYER@simatch. Their +0x28 is the Person subobject (RTTI ACTUAL_PLAYER@db offset 0x278); **do not add another 0x278**. Person +8 is the internal index; +0xC is public UID. The match roster order differs from tactics display order and can retain a prior lineup at half-time; resolve identities by index rather than by UI row.

Freshly opened detailed player-statistics tables corroborated all 22 starter ratings and 36 shirt numbers. The bottom ratings bar had stale values (Bellingham 7.9, McCleary 7.7, Odobert 7.7) until the detailed table was opened; the fresh table showed 7.7, 7.4, 7.6, matching memory rounding. Bench ratings must remain null instead of exposing the default 6.7. Evidence: `ui/match-peterborough-half-time-player-stats.png`, `ui/match-peterborough-half-time-away-stats.png`, `ui/match-peterborough-minute55.png`, and paired `match-details-*.json` captures.
