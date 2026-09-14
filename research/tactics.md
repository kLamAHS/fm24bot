# Selected tactic and current lineup

Build: Windows Steam 24.4.2+2081827, exact executable SHA gate. Memory access remains read-only (0x410). All formation, mentality and selection experiments used normal FM controls in the independent observation save.

## Resolution and layout

The instruction sequence `48 8B 0D ?? ?? ?? ?? 48 8B 95 C8 21 00 00 E8 ?? ?? ?? ?? 48 85 C0 74 0A 0F B6 40 19 88 85 58 22 00 00` resolves one typed `TACTICS_MANAGER` global. Observed RVA: `0x6374BD8`. RIP displacement starts at 3, next instruction at 7. The production resolver scans executable, non-writable module sections and validates the target type. Absolute heap addresses are never public or retained as permanent roots.

| Owner | Offset | Type | Meaning |
|---|---:|---|---|
| Tactics manager | 0x18/0x20 | vector bounds | Human tactic records |
| Human tactic record | 0x68 | pointer | Complete human object; must equal current manager |
| Human tactic record | 0/8 | MSVC map head/count | Entries keyed by Team internal index |
| Map node | 0x20 | uint32 | Team internal index, not FM UID |
| Map node | 0x28 | inline object | Team tactic selection value |
| Team selection | 0x1010/0x1018 | vector bounds | One to three tactic creator pointers |
| Team selection | 0x1028 | uint8 | Selected slot, zero based |
| Team selection | 0x1030 | 26 uint32 | Current Person internal indexes; FFFFFFFF means empty |
| Creator | 0x19 | uint8 | Mentality 1–7 |
| Creator | 0x20 | string-entry pointer | Style; null was displayed as Custom |
| Creator | 0x450 | string-entry pointer | Stored tactic name |
| Creator | 0x30 | 11 records, stride 0x48 | Position instructions |
| Position record | 0 | uint64 | Role/duty combination; only observed combinations labelled |
| Position record | 8 | uint32 | Position code; explicit observed-code mapping |
| Creator | 0x428/0x430 | MSVC map head/count | Player-specific instruction vectors, keyed by internal player index |
| Personal map value | 0/8 | vector bounds | Inline records, stride 0x48 |
| Personal record | 0x28 | uint8 | Enabled; position code must also match |

The game getter at RVA 0x1C09820 confirms selection indexes at 0x1030, the base position-record stride, and personal-instruction fallback. Constructor RVA 0x1C08880 initializes all 26 selection entries to FFFFFFFF. These instructions were read from memory and disassembled as inert COFF data with the existing Microsoft tool; no copied code was executed.

The 15 substitute entries are storage slots. Their capacity is **not** a claim that a competition permits 15 substitutes. The stored name excludes mentality prefixes that FM can add to a displayed formation name. The constant 0x218 found at creator offsets 0x10/0x348/0x448 did not change with formation and is not exposed as a formation identifier.

## UI evidence and confidence

- All seven mentalities were selected and captured: Very Defensive=1 through Very Attacking=7. Changing mentality can replace the creator pointer.
- Custom 4-2-3-1, 4-4-2, 4-3-3 DM Wide, 3-4-3 and 5-3-2 DM WB were compared with the pitch and selection table. Position order in storage can differ from the sorted UI table. Evidence: `tactics-ui-changes.json`, selected-tactic captures and `ui/tactics-*.png`.
- A real FM process restart occurred before the resumed 2026-09-14 run: prior PID 36240, new PID 45464. The same signature resolved the new manager and selection objects. Loaded test save: 2024-02-07. All 17 selected player identities, the empty striker and all eleven positions matched FM. This is restart evidence for tactic resolution and the restored custom tactic, not exhaustive coverage of every possible tactic.
- Swapping Stryjek and Murić through the pitch changed the production observation at GK and S1. A personalized Goalkeeper/Defend instruction for Stryjek then overrode the position's Sweeper Keeper/Defend instruction. Production reported `instructions_source: player`. Evidence: `tactics-read-trace-resumed.json`, `tactics-read-trace-goalkeeper-swap.json`, `tactics-read-trace-personal-goalkeeper.json`, and corresponding UI captures.
- Nine offline tests replay these observed reads and cover identity/position matching, empty slots, personalized role selection and its disabled fallback, duplicate players, changed indexes, changed selection/creator data, slot bounds, unknown codes and cyclic ownership maps.

Confidence is high for the owned selection chain, current lineup, tested mentality labels and tested position codes. Unknown role/duty combinations and unknown position codes remain null, with the position code retained. Detailed team instructions, player instruction lists, tactical familiarity, all possible role combinations and additional selected slots are not yet validated.

The reader repeats observed ownership, slot, creator, selection, position and identity bytes before returning, rechecks the Person registry, game time and current human/club context, and raises if anything changed. The API reports an unavailable observation rather than mixing states.
