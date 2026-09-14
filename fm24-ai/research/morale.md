# Player morale validation

Exact build: Windows x64 Steam FM 24.4.2+2081827, build 18129188, subject to the existing executable SHA-256 gate.

## Field and discovery

The morale field is one byte at **complete Player +0x25F** (Person -0x278 +0x25F). It is resolved through the existing signature-derived registry and validated Player RTTI; no absolute heap address is stored in production. The byte is read again before returning a player and a detected change rejects the observation.

No morale offset was found in the two existing local research snapshots. We independently captured bounded 0x378-byte Player/Person regions for all 31 known squad IDs. Before searching those bytes, we transcribed the squad's Morale column, distinct from Playing Time Happiness. Matching all records across 12 distinct labels left only offset 0x25F, interpreted as either signed or unsigned one-byte data. All positive values are identical under those interpretations; signedness beyond the observed domain is not established. Wider integer and float candidates did not match. The implementation treats it as a byte and accepts only confirmed values.

`morale-candidates-first.json`, `ui-morale-observations.json` and `morale-candidate-matches.json` preserve the raw capture, independent ground truth and candidate comparison. A separate census of 26,223 supported Player records found values 1..20 only. That range check is **not UI validation** of the population. Subsequent direct Gretna UI evidence completed all 20 labels as described below.

## Confirmed English labels

| Byte | UI label |
|---:|---|
| 1 | Abysmal |
| 2 | Extremely Poor |
| 3 | Very Poor |
| 4 | Poor |
| 5 | Quite Poor |
| 6 | Fairly Poor |
| 7 | Slightly Poor |
| 8 | Fair |
| 9 | Fairly Okay |
| 10 | Okay |
| 11 | Fairly Good |
| 12 | Quite Good |
| 13 | Good |
| 14 | Really Good |
| 15 | Very Good |
| 16 | Extremely Good |
| 17 | Excellent |
| 18 | Superb |
| 19 | Exceptional |
| 20 | Perfect |

All 20 values are now independently confirmed. Bytes outside 1..20, incomplete reads and detected concurrent changes are rejected. No interpolation, fallback label or partial squad response is used. The production offset and read-only access mechanism did not change in this follow-up.

The public model adds `morale` (the confirmed English label) and `morale_rating` (the original ordinal byte). The rating is neither a percentage nor a guarantee that differences between adjacent values have equal gameplay meaning. Labels do not follow the client's locale. Scouting knowledge is not modeled: memory may reveal information hidden by FM's scouting UI, so consumers should not interpret this API as a scouting-visibility filter.

## Save comparison

Both disposable copies were loaded through FM's normal UI without advancing time or saving:

* February 17, 2024, 08:00: all 31 squad labels matched, spanning 12 levels. Stryjek's profile independently showed Extremely Poor.
* February 7, 2024, 00:00: all 31 labels matched the earlier squad UI; 22 players had different morale bytes. Four players at byte 17 displayed Excellent, adding the thirteenth confirmed level.

Examples: Ravizzoli changed from Very Good (15) on February 7 to Perfect (20) on February 17; Tafazolli from Really Good (14) to Superb (18); Grimmer from Excellent (17) to Good (13). These are comparisons of two saved snapshots, not a simulation or causal experiment.

The initial twelve-label API correctly rejected the earlier squad's previously unconfirmed value 17. The final API was restarted after that new mapping was validated. Historical `morale-api-first.json` and `morale-api-earlier-first.json` therefore precede that one-label update. Final-code evidence begins with `morale-api-earlier.json`.

James Henry's February 17 profile displayed Scouting Required in place of morale; it was not used to infer a label. Stryjek's existing editor attribute/fitness dialog was inspected and closed with Escape; no fields were changed. Its fitness tab did not provide morale. Screenshots are retained under `ui/morale-*`.

Confidence is high for this field and all 20 listed labels in these two snapshots of one career. Other builds, independent careers, other languages and observations during simulation remain unvalidated.

## Prior thirteen-label restart checkpoint

Final API PID **13452** remained running while the February 7 copy was replaced with February 17 and FM was fully exited and relaunched, **PID 41840 -> 33644**. The return load rejected changed club context and reattached. Process absence was independently verified; observations returned 503 during absence and unloaded startup. After the test copy loaded, the API reattached automatically with a new connection UUID. API PID, executable path and start time were unchanged throughout this final sequence.

All 31 morale labels and ratings matched across the full restart and were checked again against the visible squad screen. Sample Player addresses changed:

| Player | Before restart | After restart |
|---|---|---|
| Jude Bellingham | 0x1173C5490 | 0x115073EE0 |
| Ryan Tafazolli | 0x116E0AA08 | 0x114AF3DD8 |
| Franco Ravizzoli | 0x11673AA10 | 0x1143DA480 |

The complete records immediately before and after restart were **not all identical**: Wakely's condition was 99.9 -> 100.0, and Mellor's condition/sharpness were 97.06/85.2 -> 97.69/84.23. These are the same post-reload fitness differences documented during the prior readiness work. All 31 complete records after restart matched this turn's initial snapshot. Morale did not differ between the two February 17 captures. The cause of FM's post-load fitness updates remains unresolved; no new settled-state guarantee is claimed.

Final results:

* **35 offline tests passed**, including four morale tests covering both independent UI datasets, unconfirmed values, malformed byte lengths and a changing-byte regression.
* **85 live API/Python checks passed** after restart, including every squad morale label.
* The direct validation command passed all previous identity, attribute, readiness, position and date comparisons plus **31 morale comparisons**.
* **20 lifecycle checks passed**. The separate full-record difference diagnostic is retained rather than counted as an equality success.
* Both original saves and both disposable copies retained their initial SHA-256 hashes. No Continue or save command was issued.

Evidence: `offline-morale-tests.txt`, `api-morale-validation.json`, `validation-morale-after-restart.json`, `morale-lifecycle-comparison.json`, `morale-api-*.json`, `morale-candidates-*.json`, `save-integrity-after-morale.json` and `ui/morale-squad-after-restart.png`.

The temporary API helper was stopped and port 8765 verified closed. FM remains on the February 17 test copy at 08:00, showing Squad / Selection Info. The existing screen-ID display preference remains enabled. The next section records completion of the remaining seven labels. Independent-career coverage remains open before broader claims of support.

## Complete twenty-label follow-up

A read-only search grouped the seven unmapped values by contract club, making Gretna FC 2008 (ID 61014159) a useful target. Its February 17 squad showed Scouting Required, so those hidden labels were not used as UI evidence. The existing Personal Details editor dialog also lacked a morale field and was closed without edits. Loading the earlier disposable copy through the normal UI exposed the Morale column while the manager was already on vacation. No vacation setting, scouting setting or player value was changed.

All 21 visible Gretna names/labels were transcribed into `ui-morale-gretna-observations.json`. Eighteen supported pure Player records were captured in bounded 0x378-byte regions, with repeated morale reads and matching contract club ID. The club check resolved an ambiguous name match. Bryan Gilfillan's unsupported player/staff type and the two loaned-in players Carter Jenkins and Paddy Meechan were excluded from this capture; no unsupported layout was silently decoded. A subsequent bounded identity/type check recorded Gilfillan (5204869) with RTTI Person offset **0x368**, Jenkins contracted to Ayr United (1542), and Meechan contracted to Hamilton Academical (1572); see `morale-gretna-exclusions.json`. No decoder for the 0x368 layout was added. This is not an implementation of a general other-club squad endpoint.

| Newly confirmed byte | Label | UI examples |
|---:|---|---|
| 1 | Abysmal | Robbie Ivison (61090013) |
| 3 | Very Poor | Dan Carmichael (61038325), Liam Short (2000229207) |
| 4 | Poor | Ronan Kearney (61085235) |
| 5 | Quite Poor | Dean Brotherston (61069617), Scott McLellan (2000226341) |
| 7 | Slightly Poor | David Cox (5219769), Vinnie Parker (61090322) |
| 9 | Fairly Okay | Douglas Simpson (2000296964) |
| 19 | Exceptional | Clayton Ruddick (2000296960) |

Ivison and Ruddick's individual profile screens separately confirmed their names, IDs and morale. The eighteen-player capture spans 12 morale levels, including five previously confirmed ones. Together with the original 62 Wycombe observations, the offline evidence has **80 independent UI observations across 49 distinct players and all 20 labels**. The discovery census remains research evidence, not population-wide UI validation.

### Restart, return load and checks

The updated API ran continuously as **PID 19848** while FM was fully exited and relaunched, **PID 33644 -> 36240**, and the February 7 test copy was loaded again. Process absence was verified separately; exit/startup observations returned 503. Reattachment used a new UUID. All 18 Gretna Player heap addresses changed and every morale label/rating matched before and after restart. The Gretna squad screen was rechecked and captured again.

The same API then handled the normal return load of the February 17 test copy. The first status request rejected the stale context, and subsequent requests reattached. All 31 Wycombe labels matched the corresponding dated UI evidence. Gretna's 18 morale bytes were unchanged across these two saves, and the February 17 API returned the previously validated labels; its hidden February 17 UI is not claimed as independent confirmation.

**Thirteen Gretna full models differed across restart, entirely in condition and/or match sharpness.** For example, Ivison's condition was 96.1 before and 61.0 after; Scott McLellan's was 63.2 before and 4.0 after. These captures were made at different points relative to loading and UI navigation. The cause and timing have not been established, and numeric Gretna fitness was not independently checked against an editor screen. All differences are preserved in the lifecycle report. This follow-up validates morale and does not establish full-model equality, correct numeric fitness for this new cohort, or a settled-state signal.

Final checks passed: **36 offline tests**, **121 live API/Python checks**, **78 lifecycle checks**, and the direct validation command's existing identity/attribute/readiness/date/position comparisons plus 31 Wycombe morale comparisons. Both original saves and both disposable copies retain their expected SHA-256 hashes. No Continue or save command was issued. The temporary API helper was stopped and port 8765 verified closed. FM remains idle on the February 17 test copy at 08:00, Squad / Selection Info.

Evidence: `morale-gretna-capture-*.json`, `ui-morale-gretna-observations.json`, `ui/morale-gretna-*.png`, `ui/morale-ivison-abysmal.png`, `ui/morale-ruddick-exceptional.png`, `morale-complete-api-*.json`, `morale-complete-lifecycle-comparison.json`, `offline-morale-complete-tests.txt`, `api-morale-complete-validation.json`, `validation-morale-complete.json`, `save-integrity-after-morale-complete.json` and `morale-complete-helper-stopped.json`.

A suitable next bounded investigation is post-load fitness behavior, followed by independent-career validation. Fixtures, finances and match state remain separate, unimplemented subsystems.
