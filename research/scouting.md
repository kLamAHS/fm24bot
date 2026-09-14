# Scouting, shortlists and transfer targets

Validated on the gated 24.4.2+2081827 Steam build. All external process access uses 0x410; experiments use normal FM controls in the independent project save. No game functions are called by the reader. The added players and list were subsequently saved under the new project-only `work/FM24 Observation Regression.fm` for restart testing.

## Ownership and layout

Offsets are relative to the complete HUMAN_NON_PLAYER object resolved by the existing manager signature. Person is its +0x450 subobject. No heap addresses are hard-coded in production.

| Chain/offset | Type | Interpretation and evidence |
|---|---|---|
| Human +0x210/+0x218 | pointer vector | Owned shortlist records; list insertion and selection can reorder it |
| List +0 | pointer | Complete Human owner; checked on each observation |
| List +0x24 | u8 | Kind 0: player shortlists, including Default and named lists; kinds 6/8 excluded |
| List +0x30 | pointer | Direct u32-length UTF-8 entry; null names the default list |
| List +0xC0/+0xC8 | pointer vector | Shortlist entries |
| Entry +0 | pointer | Player Person |
| Entry +0x20 | packed date | Added date; both controlled Feb 7 additions agree |
| Entry +0x24 | packed date candidate | Expiry is deliberately not exposed: 1 Month produced Mar 9; Indefinitely produced Jan 1 1900. UI expiry dates need direct checks |
| Human +0x108/+0x110 | pointer vector | 57 stored scouting reports |
| Report +0/+8/+0x10 | pointers | Player Person, scout Person, team |
| Report +0x18 | packed date | Last scouted/completion date; three profiles below |
| Report +0x40 | u8 | Knowledge label: 4 Reasonable, 5 Extensive; other values return null |
| Human +0x370 | pointer | Owned extra data holder |
| Holder +0xB48/+0xB50 | pointer vector | Transfer target groups |
| Group +0 | pointer | Optional direct string entry naming the group |
| Group +8/+0x10 | pointer vector | TRANSFER_TARGET objects, complete RTTI class checked |
| Target +8 | pointer | Player Person |
| Target +0x10 | packed date | Added date; two targets matched Feb 7 UI |
| Target +0x60 | u8 | 4 Transfer; other labels unvalidated |
| Target +0x62 | u8 | 0 Not Started, 4 On Hold; controlled changes on two players |
| Target +0x65 | u8 | 1 Urgent, 2 Normal; controlled priority change and second target |

The target constructor at RVA 0x2927D90 allocates 0x68 bytes and adds the result to a group's vector. Callers at 0x255DB40 and 0x415C040 resolve the actual owned collection through Human +0x370 / holder +0xB48. This corrected an initial investigation that found only UI copies. Closing the target popup and navigating to other profiles leaves the owned records available. Changing priority can replace those records and resets the status in the UI; the reader follows fresh pointers each time.

## UI experiments

Default initially contained no players. Brad Young (29226265) was added for 1 Month; Sam Vokes (29001407) was added Indefinitely. A new empty named list, Bridge Watch, was created. The reader returns exactly those two player lists, with two and zero members. The manager's other list kinds are not mislabeled as player shortlists. Evidence: scouting-list-two-players.json, scouting-lists-named.json and ui/scouting-shortlist-two-players.png.

Brad was added as a Normal, Not Started Transfer target, put On Hold, then changed to Urgent (UI reset Not Started). Sam was independently added as a Normal, Not Started Transfer target, then put On Hold. Sam's two owned captures differ at precisely +0x62, from 0 to 4. Neither experiment advanced game time or submitted an offer. Evidence: scouting-transfer-two-normal/held.json, ui/transfer-target-sam-normal/held.png; Brad's earlier held capture and current owned urgent record.

| Player | Scout in UI | Completed | Knowledge |
|---|---|---|---|
| Brad Young | Scott Mitchell | 2024-01-31 | Extensive |
| Sam Vokes | Lee Harrison | 2024-01-24 | Extensive |
| Farrend Rawson | Bob Rickwood | 2023-07-11 | Reasonable |

All three match the stored report. These are reports and their completion dates, not assertions that the scout's current judgment or the world's knowledge is up to date. The full collection's other 54 report fields were not individually compared with the UI. Knowledge 4 has one direct UI observation, while knowledge 5 has two. Screenshots: ui/scouting-brad-report.png, ui/scouting-sam-report.png and ui/scouting-farrend-report.png.

## Rejected and unresolved interpretations

Report +0x3C is a recommendation-score candidate, not a public grade. Brad's raw score is 55, but the individual scout page shows C− while the combined overview and shortlist show B−. Sam's 90 corresponds to A+ on both pages; Farrend's 45 shows D. These samples do not establish the complete grade transformation. The public recommendation stays null. +0x3F is a knowledge-percentage candidate (100/100/66), with no direct numeric UI validation; it is not exposed. Report prose, stars, estimated costs and interest are still undecoded.

Target terms, automatic limits, expiry and additional enums remain undecoded. `scouting-brad-target-urgent.json` captured a freed old object and is rejected evidence; `scouting-brad-urgent-objects.json` and the later owned captures contain actual replacement objects.

## Verification

Seven offline regression tests replay 562 captured reads and independent UI expectations. They check report dates/scouts, player list filtering, two target statuses, the controlled state change, unknown labels, wrong owners, registry membership, bounded/empty vectors, and mutation rejection. `scouting-read-trace-checked.json` retains the input bytes. `/scouting`, `/shortlists` and `/transfer-targets` require no UI interaction or address-space scanning.

Final lifecycle results are recorded separately in the observation lifecycle report. Field layout validation and restart validation are distinct claims.
