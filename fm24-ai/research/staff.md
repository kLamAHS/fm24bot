# Staff observation

Status: implemented; 87 names observed across Wycombe (20) and Arsenal (67), including senior/youth teams and boards. The integrated FM restart 45464 → 5464 reproduced the public model exactly; the second snapshot and final baseline also returned HTTP 200. See observation-expanded-restart-comparison.json and observation-checkpoint-final-baseline.json. This does not extend coverage to undecoded fields. Screens: `ui/staff-all-feb11.png` and the four `ui/staff-arsenal-*.png` captures. Runtime output: `staff-observations-feb11.json`; recorded decoder reads: `staff-read-trace.json`.

| Owner | Offset | Type | Meaning and evidence |
|---|---|---|---|
| Club | 0x18/0x20 | pointer vector bounds | club teams; each TEAM has matching club at 0x30 |
| Club | 0x60/0x68 | pointer vector bounds | medical staff, including youth staff |
| Club | 0x78/0x80 | pointer vector bounds | coaching/analysis staff |
| Club | 0x90/0x98 | pointer vector bounds | recruitment staff |
| Club | 0x110 | pointer | board holder, whose first two pointers are board-vector bounds |
| Team | 0x80 | pointer | complete manager object; can duplicate a coaching-vector member |
| ACTUAL_NON_PLAYER | 0xf8 | embedded Person | validated by RTTI and registry membership |
| HUMAN_NON_PLAYER | 0x450 | embedded Person | current human manager, checked against registry |
| Person | 0xc8 | pointer | BASIC_CONTRACT or FULL_CONTRACT |
| Contract | 0x08/0x10 | pointers | Person owner and club-owned Team |
| Contract | 0x1c | uint16 | job code; 17 labels have multiple-object UI comparisons |

Both clubs' complete member lists match. Wycombe's Under-21 staff are Ian Gallagher, Craig Smith and Sam Grace. Departments reflect the lists that contain the staff member; coaching includes performance analysts. The decoder checks all list bounds and bytes again after reading, rejects unknown staff classes or foreign team/contract ownership, and deduplicates managers across lists. All output is address-free.

Full contract terms use the same typed decoder as player employment. Basic contracts are shorter: only 0x20 bytes are read, and employment terms are null. Treating a basic contract as a full contract would read the next allocation as dates. Six Wycombe records use the basic class (five board members and the chief doctor), matching UI dashes for salary/expiry. Native GBP salaries can differ from rounded UI amounts (Ian Gallagher 358/displayed 350, Andrew Howard 1178/displayed 1200); no UI formatting formula is assumed.

Job codes promoted after multiple-object comparisons: 2 Coach; 6 Director; 12 Physio; 14 Scout; 16 Head Coach; 20 Assistant Coach; 26 Fitness Coach; 34 Goalkeeping Coach; 38 Chief Doctor; 40 Head of Sports Science; 44 Chief Scout; 48 Sports Scientist; 50 Head Physio; 58 Recruitment Analyst; 60 Performance Analyst; 62 Head Performance Analyst; 88 Technical Director. Other codes remain null with the numeric `job_code` retained. Single-object candidates include 4 Chairperson, 8 Managing Director, 22 Set Piece Coach, 46 Doctor, 64 Head of Youth Development, 66 Owner and 86 Loan Manager. Secondary jobs, coaching qualifications, staff attributes and responsibilities are not yet decoded.

Research warnings: exploratory 0x300-byte Club/Team copies include adjacent allocations. Actual observed object strides are Club 0x170 and Team 0xb8. No field beyond the owned prefix is used by the production staff decoder. Old raw address candidates were invalidated by an earlier save reload even though the PID stayed the same; the comparison club was resolved again through typed, owned contracts. Production reads always resolve live lists.
