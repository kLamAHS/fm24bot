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
Restart validation passed once: process 28168 was closed through FM's UI, process 21328 was launched normally, and the same test copy loaded. Both signatures resolved again; player and team heap pointers changed. See `restart-comparison.json`. The module happened to retain its base, so relocation of the module itself was not empirically tested. A signed RIP-displacement unit test covers the arithmetic. No cross-save or game-update stability claim is made.

Before decoded reads, the on-disk executable must match SHA-256 `e1059eee82fa7832188831521a3fa633ec3260dd98d03da48b16661147e3ab48`. A new binary is rejected even if its signatures appear to match. The raw process reader remains available for new-build investigation.
