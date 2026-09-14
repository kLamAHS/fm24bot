# Verified build: 24.4.2+2081827, Steam 18129188

Addresses in experiment JSON are session evidence only. Field locations below are relative to validated objects.

| Base | Offset | Type | Field | Evidence/confidence |
|---|---:|---|---|---|
| Person | 0x0C | uint32 | unique ID | UI matches for 3 people, high in tested save |
| Person | 0x58 | pointer | first-name wrapper | 3 names and 2 staff samples, high |
| Person | 0x60 | pointer | surname wrapper | 3 names and 2 staff samples, high |
| Person | 0x68 | pointer/null | common-name wrapper | source candidate; fallback path, not independently UI tested |
| Name wrapper | 0 | pointer | string entry | observed local pointer chain |
| String entry | 0 | uint32 | UTF-8 byte length | length and terminator validated on every read |
| String entry | 4 | bytes | UTF-8 string | all 31 squad names match UI, including accented names |
| Person | 0x44/0x46 | uint16/uint16 LE | birth ordinal/year | four profile DOBs and 31 squad ages match UI; high in tested snapshots; see dates.md |
| vtable | -8 | pointer | MSVC complete-object locator | module bounds checked |
| locator | +4 | uint32 | subobject back-offset | 0x278 player, 0xF8 staff; human observed 0x450 |
| Player complete object | 0x1F4 | int16 LE | match sharpness, 0..10000 | five numeric UI profiles across reload; public value raw/100 |
| Player complete object | 0x1F6 | int16 LE | fatigue | five numeric UI profiles, including negatives; research only |
| Player complete object | 0x1F8 | int16 LE | physical condition, 0..10000 | five numeric UI profiles across reload; public value raw/100 |
| Player complete object | 0x208 | 15 uint8 slots | positional familiarity | 14 mapped slots, three full numeric profiles and 30 squad position sets; see readiness.md |
| Player complete object | 0x217 | 54 bytes | attributes | individual fields below |
| Player complete object | 0x25F | one byte | morale | 49 players, 80 independent UI observations in two snapshots, all 20 labels; 22 Wycombe changes; two morale-stage restarts; see morale.md |
| Person | 0xC8 | pointer | parent/full contract | source + current manager, Tafazolli/Ravizzoli; medium |
| Contract | 0x10 | pointer | team | source + same objects; medium |
| Team | 0x30 | pointer | club | club ID 742 and name match Wycombe UI; medium |
| Team | 0x38/0x40 | pointer/pointer | roster begin/end | independently discovered in bounded local inspection; all 31 names match UI |
| Club | 0x0C | uint32 | club unique ID | UI 742; one club tested |
| Club | 0xC0 | pointer | direct string entry | Wycombe Wanderers; parent club F.C. Málaga City also observed |

Attribute offsets are relative to the 54-byte attribute block. Stored as uint8, display `(raw + 2) // 5`.

| Field | Block offset | Complete-player offset | Objects UI tested |
|---|---:|---:|---:|
| acceleration | 0x22 | 0x239 | 3 |
| pace | 0x26 | 0x23D | 3 |
| passing | 0x07 | 0x21E | 3 |
| finishing | 0x02 | 0x219 | 2 (not visible on goalkeeper profile) |
| technique | 0x17 | 0x22E | 3 |
| decisions | 0x12 | 0x229 | 3 |
| vision | 0x0A | 0x221 | 3 |
| work rate | 0x1D | 0x234 | 3 |
| strength | 0x24 | 0x23B | 3 |

Sources and discovery methods: `sources.md`, `experiments.md`. UI ground truth: `ui-observations.json`, screenshots in `ui/`. Exact addresses and raw bytes: validation JSON files.
Cross-save tests: two snapshots of the same career (February 17 and February 7), with the API running through both loads. Readiness and position checks passed again after returning to February 17; two temporary post-load fitness differences settled to their prior values and were checked against UI. See `lifecycle.md`. Restart status: passed one full exit/relaunch before readiness was added (PID 28168 -> 21328). All three sample player heap addresses changed; all 32 identity/attribute assertions and all 31 then-current squad models remained identical. See `restart-comparison.json`. Attribute interpretation outside the validated displayed 1–20 range is deliberately rejected.

Confidence applies only to this exact executable and the tested career snapshots. The nine attribute fields were decoded for all 31 roster members, but their values were compared directly with UI only on the three documented profiles. Finishing was visible on two of those profiles. The common-name path is still a candidate. A bulk decode of the wider 26,223-player index encountered an unvalidated display attribute and was rejected; whole-database attribute coverage is not established.

Expanded-model restart follow-up: PID 21328 -> 41840 passed with the API continuously running. All 31 complete models, now including readiness and date/age fields, matched the pre-restart checkpoint. The old UI comparisons and four new date-profile comparisons passed again. See `dates.md` and `date-lifecycle-comparison.json` for exact scope and addresses.
Unknown/unimplemented: game time of day, contracts beyond club resolution, fixtures, finances and match state. Game date is signature-resolved (see signatures.md); age is calculated from that date and birth date. The legacy sweeper position slot is unvalidated and omitted. No offsets invented for unknown fields.

Morale restart follow-up: PID 41840 -> 33644 passed with final API PID 13452 unchanged. All 31 morale labels/ratings matched across restart; the complete model had three fitness differences in two players from its immediate pre-restart state, and all 31 complete models matched the initial snapshot after restart. See `morale.md` and `morale-lifecycle-comparison.json`. That checkpoint confirmed 13 labels. The follow-up below completes the mapping.

Complete morale follow-up: all 20 labels are confirmed after observing 18 supported Gretna players. API PID 19848 remained unchanged through FM PID 33644 -> 36240 and return to the February 17 test copy. All 18 morale readings survived restart; all 18 sampled Player addresses changed. Thirteen Gretna full models differed only in fitness; no complete-model equality or settled-state claim is made. See `morale-complete-lifecycle-comparison.json`.
