# Player morale validation

Exact build: Windows x64 Steam FM 24.4.2+2081827, build 18129188, subject to the existing executable SHA-256 gate.

## Field and discovery

The morale field is one byte at **complete Player +0x25F** (Person -0x278 +0x25F). It is resolved through the existing signature-derived registry and validated Player RTTI; no absolute heap address is stored in production. The byte is read again before returning a player and a detected change rejects the observation.

No morale offset was found in the two existing local research snapshots. We independently captured bounded 0x378-byte Player/Person regions for all 31 known squad IDs. Before searching those bytes, we transcribed the squad's Morale column, distinct from Playing Time Happiness. Matching all records across 12 distinct labels left only offset 0x25F, interpreted as either signed or unsigned one-byte data. All positive values are identical under those interpretations; signedness beyond the observed domain is not established. Wider integer and float candidates did not match. The implementation treats it as a byte and accepts only confirmed values.

`morale-candidates-first.json`, `ui-morale-observations.json` and `morale-candidate-matches.json` preserve the raw capture, independent ground truth and candidate comparison. A separate census of 26,223 supported Player records found values 1..20 only. That range check is **not UI validation** of the population or of the seven unmapped labels.

## Confirmed English labels

| Byte | UI label |
|---:|---|
| 2 | Extremely Poor |
| 6 | Fairly Poor |
| 8 | Fair |
| 10 | Okay |
| 11 | Fairly Good |
| 12 | Quite Good |
| 13 | Good |
| 14 | Really Good |
| 15 | Very Good |
| 16 | Extremely Good |
| 17 | Excellent |
| 18 | Superb |
| 20 | Perfect |

Values **1, 3, 4, 5, 7, 9 and 19** have no confirmed UI label in this checkpoint. They are rejected, as are all values outside the table. There is no interpolation, rounding, fallback label or partial squad response. A player with an unmapped morale value causes its observation (including a containing squad/club read) to return unavailable. This deliberately limits coverage outside the tested squad.

The public model adds `morale` (the confirmed English label) and `morale_rating` (the original ordinal byte). The rating is neither a percentage nor a guarantee that differences between adjacent values have equal gameplay meaning. Labels do not follow the client's locale. Scouting knowledge is not modeled: memory may reveal information hidden by FM's scouting UI, so consumers should not interpret this API as a scouting-visibility filter.

## Save comparison

Both disposable copies were loaded through FM's normal UI without advancing time or saving:

* February 17, 2024, 08:00: all 31 squad labels matched, spanning 12 levels. Stryjek's profile independently showed Extremely Poor.
* February 7, 2024, 00:00: all 31 labels matched the earlier squad UI; 22 players had different morale bytes. Four players at byte 17 displayed Excellent, adding the thirteenth confirmed level.

Examples: Ravizzoli changed from Very Good (15) on February 7 to Perfect (20) on February 17; Tafazolli from Really Good (14) to Superb (18); Grimmer from Excellent (17) to Good (13). These are comparisons of two saved snapshots, not a simulation or causal experiment.

The initial twelve-label API correctly rejected the earlier squad's previously unconfirmed value 17. The final API was restarted after that new mapping was validated. Historical `morale-api-first.json` and `morale-api-earlier-first.json` therefore precede that one-label update. Final-code evidence begins with `morale-api-earlier.json`.

James Henry's February 17 profile displayed Scouting Required in place of morale; it was not used to infer a label. Stryjek's existing editor attribute/fitness dialog was inspected and closed with Escape; no fields were changed. Its fitness tab did not provide morale. Screenshots are retained under `ui/morale-*`.

Confidence is high for this field and the listed labels in these two snapshots of one career. Other builds, independent careers, other languages, unmapped levels and observations during simulation remain unvalidated.

## Restart and final verification

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

The temporary API helper was stopped and port 8765 verified closed. FM remains on the February 17 test copy at 08:00, showing Squad / Selection Info. The existing screen-ID display preference remains enabled. A suitable next increment is mapping the seven remaining morale labels using visible UI evidence; independent-career coverage remains open before broader claims of support.
