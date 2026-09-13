# Experiment log

## 1. Read-only Windows access
OpenProcess with 0x410 succeeded; module header, PE headers, IsWow64Process2 and VirtualQueryEx verified. Toolhelp module snapshot was denied; PSAPI worked on the existing handle. No administrative rights requested for process reads.

## 2. Controlled save
Main menu initially. Copied original Wycombe save into workspace, hashed it, then placed a separate named `FM24 Bridge Test 2026-09-13.fm` in FM's games folder after automatic approval. No overwrite. FM's filename box rejects full paths, so normal folder selection was used. Loaded test copy: 17 February 2024, 08:00.

## 3. Registry candidates
Broad source signature produced 5,521 code matches. At main menu no valid registry; correctly rejected.
With save loaded, two roots passed superficial pointer/count checks:

* module+0x642ECD0 -> 82,953 objects; sample RTTI back-offset 0xF8 (staff Person subobjects).
* module+0x642EC78 -> 29,502 objects; sample RTTI back-offset 0, name fields invalid. Rejected.

Requiring recognized Person subobject RTTI metadata disambiguated the first root without selecting a match by order.
Accepted instruction at module+0x91DCC. Production resolution rescans and validates; these RVAs are observations only.

## 4. Name representation
Direct UTF-8 at namePointer+4 failed. Read wrapper first pointer:
sample Person 0x1113D5108 +0x58 -> 0xD6D5BA68 -> 0x10DC3440C.
Entry has uint32 length 7 followed by `Michael` and NUL. Surname wrapper -> length 8, `Reiziger`.
Implemented explicit wrapper, bounded length and terminator checks. No heuristic fallback to printable garbage.

## 5. First player
Jude Bellingham Person 0x11234ABB8; complete player 0x11234A940 (back-offset 0x278).
UID candidate 29232937 at Person+0x0C. Nine raw attribute bytes decode to the nine values visible on profile. UI attributes: acceleration 15, pace 14, passing 17, finishing 16, technique 17, decisions 15, vision 16, work rate 18, strength 13.
DOB raw day 180/year 2003 agrees with displayed June 29, 2003 if interpreted as 1-based ordinal; full date encoding remains candidate pending broader validation.
See `bellingham-initial.json` for exact addresses/raw bytes. Unique ID UI check and multi-player validation pending at this checkpoint.
