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
| Person | 0x44/0x46 | uint16/uint16 | birth ordinal/year | 3 DOB candidates agree; not public API yet |
| vtable | -8 | pointer | MSVC complete-object locator | module bounds checked |
| locator | +4 | uint32 | subobject back-offset | 0x278 player, 0xF8 staff; human observed 0x450 |
| Player complete object | 0x217 | 54 bytes | attributes | individual fields below |
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
Cross-save tests: not performed. Restart status: passed one full exit/relaunch of the same test save (PID 28168 -> 21328). All three sample player heap addresses changed; all 32 identity/attribute assertions and all 31 squad models remained identical. See `restart-comparison.json`. Attribute interpretation outside the validated displayed 1–20 range is deliberately rejected.

Confidence applies only to this exact executable and save. The nine attribute fields were decoded for all 31 roster members, but their values were compared directly with UI only on the three documented profiles. Finishing was visible on two of those profiles. The common-name path is still a candidate. A bulk decode of the wider 26,223-player index encountered an unvalidated display attribute and was rejected; whole-database attribute coverage is not established.
Unknown/unimplemented: condition, morale decoding, full position representation, age/game date, contracts beyond club resolution, fixtures, finances and match state. No offsets invented for them.
