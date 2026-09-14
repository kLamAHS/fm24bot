# Training observation — active research

Build: Steam Windows x64 24.4.2+2081827, executable hash gated by the bridge. Initial training observations were captured in PID 45464. The later integrated restart to PID 5464 preserved the full public training model, including controlled settings changes; the second snapshot and final baseline passed too. See observation-expanded-restart-comparison.json and observation-checkpoint-final-baseline.json. All process access is read-only 0x410. Normal UI experiments use `work/FM24 Match Observation.fm` without saving.

## Implemented schedule

`FMBridge.training()` and `GET /training` read the current team's committed schedule. The reader returns the owned range of weeks, its current week and three sessions per day. It never assumes a fixed calendar horizon. The stored name omits UI category prefixes and the Modified suffix. Match cells identify a match session; fixture identity is available separately through `/fixtures`.

| Owner | Offset | Type | Meaning and evidence |
|---|---:|---|---|
| Module | global RVA 0x63651C8 | pointer | TRAINING_MANAGER, RTTI vtable RVA 0x5B04B38 |
| Training manager | 0x20 / 0x28 | pointer vector | Per-human records; bounded to 32 by the reader |
| Human training record | 0x68 | Person pointer | Current HUMAN virtual Person base, complete HUMAN +0x450 |
| Human training record | 0x70 | Club pointer | Current club ownership |
| Human training record | 0x40 / 0x48 | MSVC map head/count | Team-keyed records |
| Team map node | 0x20 | Team pointer | Exact team key |
| Team map node | 0x48 / 0x50 | pointer vector | Committed weekly schedules, 39 observed |
| Week | 0x00 + 10 × day + 4 | three uint16 | Session codes, Monday through Sunday |
| Week | 0x48 | direct string entry pointer | Stored schedule name |
| Week | 0x50 | packed date | Monday of the week; time bits discarded |

The unique RIP-relative signature is `48 8B 0D ?? ?? ?? ?? 4C 89 FA E8 ?? ?? ?? ?? 48 85 C0 74 20 48 8B 48 08 48 63 51 04 48 8D 4C 10 08`. Displacement +3, RIP length 7; hit RVA 0x9AA87. Runtime code at RVA 0x38F0760 (size 0x23C) independently confirms manager +0x20/+0x28, HUMAN +0x450, record +0x68 and the Team map at +0x40 with node key +0x20. No game code is called by the bridge.

Five full weeks (105 cells, Jan 29–Mar 3) were transcribed from `ui/training-calendar-february.png`. All dates and session positions match, including Travel around away fixtures and Rest around home fixtures. The February 19 first session was changed from Physical to Overall through the normal training editor. `training-team-pending.json` is byte-for-byte identical to the initial owned schedule while the edit is pending. After Confirm, only that session code changes 6→0; the name pointer and two metadata flag bytes also change. The latter flags are not decoded.

Label confidence varies: frequently repeated Rest, Travel, Match, Recovery, Routines, Match Focus and Gegenpress sessions have several independent week records; some labels appear only once in this five-week sample and need further cross-save validation. Unknown labels remain null with `not_decoded` status. All 39 weeks are exposed with that explicit distinction. `tests/test_training.py` contains the independent UI transcriptions and corrupted/racing structure checks.

## Individual programs

Current squad identity comes from the already validated Team +0x38/+0x40 vector and Person registry membership. Pure PLAYER pointers are reconstructed only after confirming the Person virtual-base offset of 0x278.

| Owner | Offset | Type | Meaning and validation |
|---|---:|---|---|
| PLAYER | 0x170 | nullable pointer | Additional-focus record |
| Focus record | 0x06 | uint8 | Focus code, **not uint16**; adjacent progress bytes are separate |
| PLAYER | 0x1C2 | uint8 | Configured intensity; 0xFF Automatic, 2 Double Intensity |

Murić's focus None→Quickness allocates the focus record and sets code 9. Ravizzoli's GK Technique→Quickness changes the existing record's code 23→9. Murić and Ravizzoli both independently change intensity 0xFF→2 after selecting Double Intensity. Code 23 has one UI observation and remains subject to wider validation. Automatic is a configuration setting; the UI's effective Normal Intensity may be derived from condition and team defaults. The API does not equate these.

Focus dates/progress, other focus labels, normal/half/rest intensity codes, effective automatic intensity and training position/role/duty still require validation. Evidence: `training-focus-change.json`, `training-focus-franco-quickness.json`, `training-intensity-change.json`, `training-intensity-franco-double.json`, `training-individual-children.json` and `training-read-trace-programs.json`.

## Rejected current-rating candidate

Human training record +0x30/+0x38 is a map of 40 internal player indices. Its node key is uint32 at +0x1C, count at +0x20, and four floats at +0x24. The last float matches the six best/worst Overview ratings and most Individual rows. It is **not a trustworthy current rating**: Murić is 7.15 here versus 7.20 in Individual training; injured McCarthy and Sadlier retain 8.10 and 6.65 here while the UI shows dashes. Opening Individual does not update these stored floats. Current ratings are therefore null with `not_decoded` status in the public API. They were removed from the initial implementation after this counterexample.

The separate 33-record vector at human training record +0 is **not the weekly schedule**; its items include packed dates and vectors of training improvement descriptors. Manager +0x90 is a UID-keyed 38-entry map with seven compact values per player, apparently condition history; that interpretation is unvalidated and is not exported. RTTI `PLAYER_TRAINING_INFORMATION` identifies two UI information objects, not a database-wide collection. These are research leads only.

## Outstanding

Remaining decoding limits are additional focus/intensity/position labels, the current-rating calculation, training units and responsibility. The supported fields have completed second-save and full process restart validation; those live regressions complement the recorded-read tests.
