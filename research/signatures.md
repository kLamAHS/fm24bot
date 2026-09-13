# Signatures and resolution

Scan only readable executable PE sections without the writable flag. Principal section on this executable is `.sdata`, ~89.6 MB. Section locations are obtained from the live PE header.

Database source AOB: `48 8D 0D ?? ?? ?? ?? 48 8D 15`.
Signed displacement at match+3; root = match+7+disp32.
Source pattern is broad (5,521 matches). Filter targets to main image; follow `[root+0x68] -> [+0x80] -> {begin,end}`. Require aligned bounded vector and recognized RTTI back-offsets on at least 14 of 16 sampled Person records. Require exactly one surviving root, never the first match.

Observed accepted instruction RVA: 0x91DCC. Root RVA: 0x642ECD0. Registry size: 82,953 people. No permanent absolute address used.

Human manager AOB: `48 8B 35 ?? ?? ?? ?? 48 8B 56 18 4C 8B 76 20 49 29 D6 B0 01 49 83 FE 10`.
Global pointer from match+7+signed displacement at +3. Dereference, then manager vector at root+0x18/+0x20. Identify embedded Person by global registry membership plus matching RTTI back-offset. Exactly one human manager supported in this phase.
Observed instruction RVA 0x3A278B1, global RVA 0x6365118. Human complete object to Person delta 0x450 discovered dynamically.

Squad requires no signature or process-wide search: manager -> contract -> team; roster vector at team+0x38/+0x40. Resolution and object/registry checks precede decoding.
Initial restart validation passed: process 28168 was closed through FM's UI, process 21328 was launched normally, and the same test copy loaded. Both original signatures resolved again; player and team heap pointers changed. See `restart-comparison.json`. A second full restart, 21328 -> 41840, passed with the expanded date/readiness model and API process continuously running. All three signatures resolved again. Two snapshots of one career were also tested. The module retained its base, so relocation of the module itself was not empirically tested. A signed RIP-displacement unit test covers the arithmetic. Independent-career and game-update stability are not established.

Before decoded reads, the on-disk executable must match SHA-256 `e1059eee82fa7832188831521a3fa633ec3260dd98d03da48b16661147e3ab48`. A new binary is rejected even if its signatures appear to match. The raw process reader remains available for new-build investigation.

## Current game date

AOB: `83 F2 01 8B 05 ?? ?? ?? ?? 66 09`. The MOV instruction begins at match+3; signed displacement is at match+5, and the target is match+9+disp32. One hit was observed at instruction RVA `0x20B566B`, targeting a four-byte global at RVA `0x631D5BC`. The production reader scans and resolves these dynamically; it does not use either RVA as an address constant.

Interpret the little-endian uint32 as year in bits 16..31 and a 1-based ordinal day in bits 0..8. Bits 9..15 remain uninterpreted. Raw `3012e807` gave 2024, day 48 (February 17); `2600e807` gave 2024, day 38 (February 7), matching the two test-save UIs. These samples have different unknown flag bits and different UI times; two samples do not establish a time encoding.

Require exactly one distinct valid in-module target, a valid Gregorian year/day, a loaded Person registry and unchanged bytes on a repeated read. No real-world clock fallback. See `dates.md` for per-field reload and restart evidence.
