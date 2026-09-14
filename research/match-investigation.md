# Match observation research — in progress

No match decoder is public yet. Normal UI work on test copies exposed the Oxford 3–1 result and played its recorded goals. Independent final UI values: shots 13–14, on target 7–8, xG 2.71–1.00, corners 8–8, fouls 14–12, yellow cards 1–1, passing completion 83%–82%, possession 51%–49%. Screenshot: `ui/match-oxford-result.png`.

The replay showed the final 3–1 score even at minute 10:41. A replay clock paired with this score must not be mistaken for live match state. Normal result UI and goal playback have not established a trustworthy owner chain to current match statistics.

## Leads, not public offsets

`PLAY_FIXTURE_MANAGER` has a module global at RVA 0x6374490; 37 RIP-relative references were recorded in `match-root-xrefs.json`. Candidate AOB: `48 8B 0D ?? ?? ?? ?? 48 8B 41 20 48 8B 49 28 48 29 C1 0F 84`, displacement +3, length 7. Collections at +8/+10, +20/+28 and +38/+40 are empty while idle in the February 17 save, and the first two remained empty while replaying goals. Object fields beyond +0x128 have not been bounded and should not be attributed to the manager; older broad research captures include adjacent allocation bytes.

Heap RTTI scans found multiple `GAME_MATCH_MANAGER` objects. These are simulation managers tied to human or AI staff (+0x28); they are not a unique active-match owner. +0x80 and +0x1C8 point to `GAME_MATCH` objects. Those objects are large and contain player/official references; their presence does not prove that they belong to the current displayed fixture. `FINISHED_MATCH_MANAGER@fmmatchviewer` had no observed live complete instance during the timing of the scans. Broad captures are discovery evidence only, never a runtime resolver.

`FIXTURE_RESULT +0x70` sometimes points to non-polymorphic data, not an established statistics object. Reads beyond its unknown allocation size included adjacent objects. Do not use those apparent child offsets as a validated structure.

## Isolated live test

A new byte-for-byte copy of the February 7 baseline was created as `work/FM24 Match Observation.fm`, SHA-256 `736684315066a5711d1529e1c3fa36dc73f27db08a0754d16f9a448f3a64512a`. It is separate from both original saves and the existing two regression copies. Normal game advancement in this isolated copy is authorized for live-match validation. Only normal FM UI may change the test game's state; all external process handles remain read-only (0x410).

The isolated copy was loaded and the manager returned from the inherited vacation. On February 7 at 00:00, the first two play-manager vectors were empty, while +0x38/+0x40 held 12 FIXTURE_TO_PLAY_QUICK objects, observed stride 0x48 and fixture pointer +8. This queue exists while the manager is outside a match. The test manager was then sent on a three-day vacation (reject all offers) to return on February 10 for Peterborough.

Vacation returned at February 10 00:00. Normal Continue reached 15:00, with the fixture still unplayed. Old inbox items were marked read in the isolated copy. Quick Pick supplied the missing striker and a full healthy starting lineup, and the assistant handled the pep talk. No external process writes were used. The official 18-player roster for each side is recorded in `ui/match-peterborough-lineup.png`.

Before kickoff the +8 collection contains 88 objects: 76 QUICK and 12 FULL. The +0x20 collection is empty and +0x38 contains 122 QUICK objects. FULL has RTTI `.?AVFIXTURE_TO_PLAY_FULL@@`, vtable RVA 0x5AF7508 and observed allocation stride 0x80. +8 is a fixture, +0x50 a DATA_BUFFER, and one FULL has the human manager at +0x70. The other pointers are still under investigation. Older broad FULL captures include neighboring allocations beyond 0x80; those are not owned fields. This scheduler queue alone is not a validated current-match API.
