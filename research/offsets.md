# Validated runtime offsets

These layouts apply only to Windows x64 Steam 24.4.2+2081827 (build 18129188), with the executable hash enforced by bridge/profile.py. Hex offsets are relative to the named object, never reusable heap addresses. The linked subsystem reports record discovery methods, UI evidence, confidence and limits. Confidence means corroborated on this build and these career snapshots; it does not imply all builds or careers.

## Core identities, players and ownership

| Base | Offset | Type | Meaning and empirical scope |
|---|---:|---|---|
| Person | 08 | u32 | Internal registry index; not array offset or public UID |
| Person | 0C | u32 | Public FM UID; validated profiles and all 31 squad identities |
| Person | 44/46 | u16/u16 | Birth ordinal/year; four exact dates and 31 ages, birthday across saves |
| Person | 58/60/68 | pointers | First/surname/common-name wrappers; 31 squad names, accented names included |
| Name wrapper | 0 | pointer | Direct string entry |
| String entry | 0 / 4 | u32 / UTF-8 bytes | Byte length, then text and NUL; checked lengths/terminators |
| Person | 70 | pointer | Primary NATION; Nation 0C UID, 18 name entry; six UI nationality checks |
| Person | C8 | pointer | Employment contract; typed and owner checked |
| Person | D0 | pointer | Optional holder whose first pointer is a loan contract |
| Complete Player | 150 | packed timestamp | Freshness of readiness cache; must equal current game timestamp |
| Complete Player | 1F4/1F6/1F8 | i16/i16/i16 | Sharpness/fatigue/condition; numeric UI checks on five profiles. Fatigue is research only |
| Complete Player | 208 | 15 bytes | Positional familiarity; legacy SW slot omitted; 14 supported slots |
| Complete Player | 217 | 54 bytes | Attribute block; 47 visible attributes below, seven other bytes omitted |
| Complete Player | 25F | u8 | Morale 1–20; 80 UI observations across 49 players and all labels |
| Full contract | 08/10 | pointers | Owner Person / Team |
| Full contract | 18 | i32 | Native weekly GBP; employment salary versus loan contribution explicitly distinguished |
| Full contract | 3C/40 | packed dates | Start and end; 40 agreements including nine loans read, selected UI terms compared |
| Team | 30 | pointer | Club |
| Team | 38/40 | pointer vector | Roster; all 31 names match, includes some loaned-out players |
| Club | 0C/C0 | u32 / pointer | UID and direct name entry |

Supported Person subobjects are identified through bounded MSVC RTTI. Pure Player back-offset is 278, ordinary non-player F8, human non-player 450, support staff 88. Do not subtract a Player offset from another Person class. Support staff display names live at complete object 30/38, not the generic Person name offsets.

Readiness is normalized to percentages by dividing by 100 and withheld when stale. An injury can coexist with high condition. Positions list familiar codes with ratings >=15; this does not prove eligibility. Details: [readiness](readiness.md), [dates](dates.md), [morale](morale.md), and bridge/players.py, bridge/contracts.py.

## Player attributes

All fields below are u8 values in 1–100, displayed as max(1, (raw + 2) // 5). The lower clamp was separately checked on two players with raw 1/2. Each field has at least two independent UI comparisons; six profiles contribute 220 comparisons. General Player attributes are not filtered by scouting visibility. See attribute-validation-expanded.json and tests/test_attributes.py.

| Field | Block offset | Complete Player offset | UI comparisons |
|---|---:|---:|---:|
| crossing | 00 | 217 | 4 |
| dribbling | 01 | 218 | 4 |
| finishing | 02 | 219 | 4 |
| heading | 03 | 21A | 4 |
| long_shots | 04 | 21B | 4 |
| marking | 05 | 21C | 4 |
| off_the_ball | 06 | 21D | 6 |
| passing | 07 | 21E | 6 |
| penalty_taking | 08 | 21F | 6 |
| tackling | 09 | 220 | 4 |
| vision | 0A | 221 | 6 |
| handling | 0B | 222 | 2 |
| aerial_reach | 0C | 223 | 2 |
| command_of_area | 0D | 224 | 2 |
| communication | 0E | 225 | 2 |
| kicking | 0F | 226 | 2 |
| throwing | 10 | 227 | 2 |
| anticipation | 11 | 228 | 6 |
| decisions | 12 | 229 | 6 |
| one_on_ones | 13 | 22A | 2 |
| positioning | 14 | 22B | 6 |
| reflexes | 15 | 22C | 2 |
| first_touch | 16 | 22D | 6 |
| technique | 17 | 22E | 6 |
| flair | 1A | 231 | 6 |
| corners | 1B | 232 | 4 |
| teamwork | 1C | 233 | 6 |
| work_rate | 1D | 234 | 6 |
| long_throws | 1E | 235 | 4 |
| eccentricity | 1F | 236 | 2 |
| rushing_out | 20 | 237 | 2 |
| punching_tendency | 21 | 238 | 2 |
| acceleration | 22 | 239 | 6 |
| free_kick_taking | 23 | 23A | 6 |
| strength | 24 | 23B | 6 |
| stamina | 25 | 23C | 6 |
| pace | 26 | 23D | 6 |
| jumping_reach | 27 | 23E | 6 |
| leadership | 28 | 23F | 6 |
| balance | 2A | 241 | 6 |
| bravery | 2B | 242 | 6 |
| aggression | 2D | 244 | 6 |
| agility | 2E | 245 | 6 |
| natural_fitness | 32 | 249 | 6 |
| determination | 33 | 24A | 6 |
| composure | 34 | 24B | 6 |
| concentration | 35 | 24C | 6 |

## Expanded subsystems

These reports are the authoritative detailed offset tables and include bounds, typed owners, unknowns and rejected candidates.

| Subsystem | Production owner chain | Detailed layout and evidence |
|---|---|---|
| Finances | Club+150 → CLUB_FINANCE (owner at +08) | [Finances](finances.md) |
| Fixtures/results | AOB → rule wrapper+08 → competition manager → calendar day buckets | [Fixtures](fixtures.md) |
| Staff | Club-owned team, medical, coaching, recruitment and board collections | [Staff](staff.md) |
| Tactics | AOB → tactics manager → human record → team tree → selected creator | [Tactics](tactics.md) |
| Inbox | Human+338 → holder pointer vector → NEWS_ITEM base | [Inbox](inbox.md) |
| Training | AOB → training manager → human record → team tree → weeks | [Training](training.md) |
| Scouting | Human+108 vector of stored report records | [Scouting](scouting.md) |
| Shortlists | Human+210 vector → kind-zero lists → C0 entries | [Scouting](scouting.md) |
| Transfer targets | Human+370 → holder+B48 groups → group+08 target vector | [Scouting](scouting.md) |
| Match viewer | AOB → controller map → live controller+20 → wrapper → impl+1C0 | [Match](live-match.md) |

The integrated restart from FM PID 45464 to 5464 passed 32 checks, including relocation of manager memory and equality of every returned data model. The match route was inactive in that comparison. Loading the second save additionally checked empty shortlists/targets and found the inbox's FF uninitialized-time sentinel, now handled explicitly. See observation-expanded-restart-comparison.json and observation-checkpoint-expanded-second-save-cold-fixed.json. Active-match coverage is separately recorded in live-match.md. Earlier milestones remain in experiments.md and their dated evidence; they do not limit the current field map.
