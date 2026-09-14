# Signatures and stable resolution

The decoder requires executable SHA-256 `e1059eee82fa7832188831521a3fa633ec3260dd98d03da48b16661147e3ab48` before interpreting runtime objects. A different binary is rejected even if patterns appear to match. The raw process reader remains available for separate new-build research.

Scan readable executable PE sections without the writable flag. Section boundaries come from the live PE header; the principal code section in this build is .sdata. Targets are calculated using signed RIP-relative displacements and restricted to the loaded main image. Require one validated owner rather than taking the first byte match.

| Owner | Pattern | Displacement offset / instruction end | Observed global RVA |
|---|---|---|---|
| Database | `48 8D 0D ?? ?? ?? ?? 48 8D 15` | 3 / 7 | 642ECD0 |
| Human manager | `48 8B 35 ?? ?? ?? ?? 48 8B 56 18 4C 8B 76 20 49 29 D6 B0 01 49 83 FE 10` | 3 / 7 | 6365118 |
| Date/time | `83 F2 01 8B 05 ?? ?? ?? ?? 66 09` | 5 / 9 | 631D5BC |
| Fixtures | `4C 8B 25 ?? ?? ?? ?? 49 8B 44 24 28 49 39 44 24 30 0F 84 ?? ?? ?? ??` | 3 / 7 | 6364CD0, alias 6429AD8 |
| Match viewer | `48 8B 0D ?? ?? ?? ?? 48 8B 56 18 E8 ?? ?? ?? ?? 84 C0 74 15 48 8B 0D` | 3 / 7 | 6364C68 |
| Tactics | `48 8B 0D ?? ?? ?? ?? 48 8B 95 C8 21 00 00 E8 ?? ?? ?? ?? 48 85 C0 74 0A 0F B6 40 19 88 85 58 22 00 00` | 3 / 7 | 6374BD8 |
| Training | `48 8B 0D ?? ?? ?? ?? 4C 89 FA E8 ?? ?? ?? ?? 48 85 C0 74 20 48 8B 48 08 48 63 51 04 48 8D 4C 10 08` | 3 / 7 | 63651C8 |

RVAs are recorded evidence, not absolute addresses used by production. The fixture aliases are deduplicated by validated owner identity. All other readers follow the currently resolved human/team/club ownership chains rather than scanning heap memory for familiar-looking records.

The broad database AOB produced 5,521 hits. Filter targets through [root+68] → [database+80] → bounded Person vector, validate sampled RTTI types and require exactly one surviving root. The accepted instruction RVA was 91DCC. The manager AOB leads to a complete-human vector at +18/+20; identify its Person through registry membership and RTTI. Exactly one employed human is supported.

The four-byte date/time global stores year in bits 16–31, ordinal day in bits 0–8 and quarter-hour slot in bits 9–15. Slot zero represents observed midnight; slots 1–72 mean 06:00 + (slot−1)×15 minutes. Unsupported slots fail validation. Forty schedule rows and independent game-time screens corroborate the mapping. See [fixtures](fixtures.md).

Production readers resolve global roots per connection and check current owner identity on every read. Player UID lookup uses the registry index, not repeated process-wide scans. The roster follows manager → contract → team+38/+40. Inbox, scouting, shortlist and target readers similarly resolve their owned containers on each observation; UI changes can replace child objects.

## Restart and reload evidence

Several staged full restarts covered increasingly expanded models. The integrated expansion check kept API PID 42772 running while FM exited and relaunched from PID 45464 to 5464. All 15 sampled routes returned 200 after reattachment; all 14 data models were equal (shortlist list order normalized). Manager heap memory relocated and the connection UUID changed. While FM was absent, observations returned 503 and status reported disconnected.

Loading the February 17 second snapshot invalidated the previous club context and reattached. The initial inbox decoder rejected an FF time sentinel; the corrected cold-load check passed all 15 sampled routes without first opening the inbox. See observation-expanded-restart-comparison.json, expanded-api-disconnected.json and observation-checkpoint-expanded-second-save-cold-fixed.json.

The integrated restart had no active match viewer. Active-match checks are described separately in [match research](live-match.md). The executable retained the same module base across the observed launches, so actual module relocation is not empirically claimed; signed displacement arithmetic is covered by unit tests. Independent careers and future game updates remain unvalidated.
