# Positions, condition and match sharpness

Build: the same SHA-256-gated Steam 24.4.2+2081827 executable. Initial session PID 21328, test save at 17 February 2024, 08:00.

## Discovery and independent UI validation
Read candidate position bytes at complete-player+0x208 from the attributed fm_scouter research. Read candidate fitness fields from the additional source recorded in `sources.md`. Captured all 31 roster candidates before implementing the decoder or viewing numeric detail panels (`position-candidates.json`).

FM's normal squad position column was manually transcribed for 30 fully visible rows. Beryly Lubala's final role was clipped and that row was excluded. The positions with ratings >=15 match every transcribed row exactly. These are display-level familiar positions, not best tactical role or match eligibility.

The existing in-game editor exposes numeric fields. Opened its player detail dialog, viewed Fitness and Positions tabs, and dismissed each dialog with Escape. No input fields were focused or changed, and no editing action was applied. Runtime memory access remained strictly read-only.

| Player | UI condition | UI sharpness | UI fatigue | Position fields |
|---|---:|---:|---:|---:|
| Ryan Tafazolli | 7540 | 9900 | 422 | 14 exact ratings |
| Franco Ravizzoli | 10000 | 5050 | -500 | 14 exact ratings |
| Jude Bellingham | 8632 | 10000 | 436 | 14 exact ratings |

All 51 numeric checks passed: 42 position ratings plus nine fitness values. Fatigue is retained only as research evidence; its broader interpretation is not part of this API. Screenshots and independently transcribed values are saved in `ui/` and `ui-readiness-observations.json`.

## Memory fields

All offsets are relative to the complete player object, located by its validated Person subobject and RTTI back-offset.

| Offset | Type | Field | Validation/confidence |
|---|---|---|---|
| 0x1F4 | int16 LE | match sharpness, 0..10000 | three numeric UI matches; high for tested build/save |
| 0x1F6 | int16 LE | fatigue | three numeric UI matches including negative; research only |
| 0x1F8 | int16 LE | physical condition, 0..10000 | three numeric UI matches; high for tested build/save |
| 0x208 | 15 uint8 slots | positional familiarity | 14 mapped fields, three full numeric profiles; 30 displayed role sets |

Position order: GK, unused legacy SW, DL, DC, DR, DM, ML, MC, MR, AML, AMC, AMR, ST, WBL, WBR. Values must be 1..20 for mapped slots. The legacy slot is ignored and never exposed. Each slot's offset is its index added to +0x208.

## Public contract
`positions` lists codes with familiarity >=15. `position_ratings` contains all 14 verified codes and integer ratings. `condition` and `match_sharpness` normalize FM's 0..10000 scale into 0..100 by dividing by 100; for example Jude's condition is 86.32. This normalization is an API convention, not a claim that the normal skin displays exact percentages. Hearts and injury icons are not used to infer numbers.

Readiness is captured as a bounded contiguous block and reread before returning the player. Unexpected ranges or changing bytes produce an error; no clamping, fabricated defaults or partial squad is returned. The bridge never suspends FM, so this is still not an atomic snapshot across players.

22 offline tests passed after implementation and the status regression fix. The expanded live HTTP/Python test passed 35 checks, including numeric UI comparisons and consistent connection IDs. Reports are in `validation-readiness-before-reload.json`, `validation-readiness-after-reload.json` and `api-readiness-validation.json`.

Reload and cross-save validation are recorded in `lifecycle.md` and API lifecycle checkpoint reports. Two further numeric Fitness profiles (Jack Wakely and D'Mani Mellor) provided six successful post-reload UI checks, bringing numeric fitness coverage to five players. Their temporary post-load values and later updates are preserved in the evidence, not treated as immutable save values. No in-match or game-time progression test has yet been performed. An injury can coexist with a high condition value; condition does not imply availability.
