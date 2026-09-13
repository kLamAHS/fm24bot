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

## 6. Multi-player validation
Enabled FM's normal preference "Show screen IDs in Title Bar to assist skinning". The title bar confirmed Jude Bellingham 29232937, Ryan Tafazolli 28028684 and Franco Ravizzoli 14185872. The profiles provide deliberately different values, including acceleration 15/7/8 and passing 17/13/5. Transcribed UI observations before comparison; screenshots retained. All 32 checks passed: six name/UID assertions plus 26 visible attributes. Goalkeeper finishing was not visible and was excluded from UI assertions. Date-of-birth candidates agree on all three profiles, but date decoding remains outside the public model.

## 7. Human manager, club and roster
Resolved the human-manager signature and complete-object vector. The human Person subobject was found by registry membership and matching RTTI at back-offset 0x450. It identifies Liam Boyd, 2002077023, confirmed in the title bar. Full-contract -> team -> club resolves Wycombe Wanderers, UID 742, confirmed in UI.

A bounded 0x700-byte inspection around the team pointer revealed a vector at +0x38/+0x40 containing 31 player references. Every member resolved through the global person registry; all 31 names matched the squad screen exactly, including accented names. Other apparent vectors were beyond the target object's established extent and were not adopted. The roster includes loaned-out players shown in this squad view, and is not a statement about match eligibility. Jude's parent full contract points to F.C. Málaga City, which is why player parent contracts were not used to reconstruct the current team's squad.

## 8. Full process restart
Captured `validation-before-restart.json` and `squad-before-restart.json` in PID 28168. Quit FM using its normal UI; confirmed the process was absent and attachment failed. Relaunched the existing executable and selected the most recent test save. New PID 21328; same game date, 17 February 2024 at 08:00. Repeated validation and full squad read. All checks passed; all 31 returned player models were identical. Sample complete-player addresses changed:

| Player | Before | After |
|---|---|---|
| Jude Bellingham | 0x11234A940 | 0x116AF8940 |
| Ryan Tafazolli | 0x111DAF648 | 0x116501E18 |
| Franco Ravizzoli | 0x1116BA990 | 0x115DE9A00 |

Reopened Jude's profile after restart and confirmed the UID and nine attributes again; screenshot retained. `restart-comparison.json` records the comparison. This proves one same-save restart, not universal stability.

## 9. Public API and guard checks
17 offline unit tests passed, covering read-only access mask, Windows structure layout, invalid ranges/vectors, overlapping and cross-chunk signature matches, negative RIP displacement, unsupported executable rejection, changing/stale registry rejection, and HTTP loopback/origin/method guards.

An actual loopback HTTP server, attached to FM PID 21328, passed 25 integration checks. The three profiles matched UI observations through HTTP, manager and club matched, both squad routes returned 31 consistent players, and the Python UID lookup matched HTTP. Invalid IDs, unknown routes, unsupported match data and write requests returned the documented errors. This temporary test server was closed normally after testing. See `api-live-validation.json`; rerun with `python -m tests.live_smoke`.

Strict bulk decoding of the 26,223 pure-player entries encountered an invalid display attribute and refused to return an incomplete collection. This does not affect the 31-player roster or the three validated profiles; it limits whole-database decoding. `player_ids()` provides the index for further investigation. No rounding or fallback value was invented to suppress the failure.

## 10. Save integrity and limits
SHA-256 checks after the restart matched the initial test-copy hash for both the original save and named test save. See `save-integrity-after.json`. No game time was advanced or save command issued. The screen-ID display preference remains enabled. No software was installed, no privileges changed, and no process mutation API was used.

Unimplemented subsystems remain explicit: age/game date, positions, condition, morale, fixtures, finances, match and AI actions. Next bounded investigation should validate condition/position fields against several squad profiles, then a second disposable save and save reload while the service is running. Do not treat plausible candidate fields as confirmed.

## 11. Positions and readiness increment

Completed the bounded next investigation. Captured all 31 position/nearby fitness candidate blocks before UI numeric inspection or decoder implementation. Compared 30 fully visible squad position sets and three complete numeric position/fitness panels: Ryan Tafazolli, Franco Ravizzoli and Jude Bellingham. All 51 numeric assertions and 30 role sets agree. Implemented 14 position ratings, familiar positions (>=15), condition and match sharpness with strict bounds and a repeated contiguous-block read. Raw fatigue is research evidence only. The legacy sweeper slot remains omitted. Field-level evidence, scales and attribution are in `readiness.md` and `sources.md`.

The expanded localhost API and Python model passed 35 live checks. Added connection UUIDs to distinguish successful reattachments. No AI actions or additional unverified observation subsystems were added.

## 12. Continuous-service save reload and status fix

Kept API PID 37092 running while FM PID 21328 unloaded to the main menu, loaded a second disposable February 7 snapshot, then directly loaded the original February 17 test copy. Menu observations returned 503 without stale data. Each detected invalidation was followed by a fresh bridge connection. The earlier-save profile independently confirmed Jude's name, ID, nine attributes and displayed positions; its numeric fitness menu was unavailable with the manager on vacation, so no exact earlier-date fitness UI claim is made.

The direct return load reused the Person registry and player addresses, revealing that registry-only `/status` validation was insufficient. The manager request correctly rejected a changed club pointer; the next request reattached. Updated `/status` to validate manager/club context too and added a regression test for this observed case. Final code passed 22 offline tests and all 35 live API checks. Preserved pre-fix lifecycle evidence is clearly distinguished from final-code tests in `lifecycle.md`.

Immediately after return, two players differed slightly in condition/sharpness. During idle navigation they returned to prior values, leaving all 31 full models identical to before unload. Independent numeric UI checks on Jack Wakely and D'Mani Mellor validated all six fitness values in the later state. The game date/time did not change; the internal FM update trigger is unresolved. A successful read is not proof that post-load values have settled.

The lifecycle comparison passed 14 checks. Both original files and both disposable copies retain their original hashes. No Continue, save or fitness-edit action was issued. FM is left on the February 17 test copy. Next bounded work can investigate age/game date or morale, with independent UI validation; full-process HTTP-service restart and independent-career testing remain open.

## 13. Game date and birth-date increment

Revisited the existing local research snapshots for date leads. Captured the date AOB, target bytes and all 31 roster DOB candidates before implementing the decoder. Exactly one current-date hit resolved at module+0x20B566B to module+0x631D5BC; raw `3012e807` decoded as 2024/day 48 using a nine-bit ordinal mask, matching February 17. Unknown bits were left uninterpreted and no real-world clock fallback was adopted.

Independently inspected Mellor, Grimmer, Bellingham and Eriksen profiles, plus all 31 squad ages in General Info. Their birth dates and calculated ages matched; the four profiles span leap/non-leap birth years and birthdays before/after the current date. Added Game.date and Player.date_of_birth/age/age_as_of to Python and the localhost API. Date and DOB consistency checks reject changes and invalid calendar values. Batch reads share one in-game date. The unvalidated February 29 birthday convention on February 28 of a non-leap year remains an explicit rejection. See `dates.md` for encoding, object count, confidence and all evidence files.

## 14. Birthday and continuous-API full restart

Kept API PID 35048 running while FM PID 21328 loaded the earlier February 7 copy and then returned to February 17. The earlier date global held `2600e807` (2024/day 38). Eriksen's UI showed age 31 and DOB February 14, 1992; the API matched and returned age 32 after the later copy was restored. Both first post-reload status requests correctly rejected changed club context and subsequent requests reattached. No game-time advancement was required.

Exited FM through its UI and verified the process absent. The API returned disconnected status and 503 observations, including /game. Relaunched installed FM as PID 41840; unloaded startup also returned unavailable observations. Loaded the explicitly named February 17 test copy from the menu. The API reattached automatically with a new UUID while its own PID/path/start time remained unchanged. All 31 full player models matched the pre-restart checkpoint. The four date-profile heap addresses changed and all three signatures resolved again. All old UI validation and 54 new date-section comparisons passed; 53 live API/Python checks passed after restart.

The final offline suite has 31 passing tests, including the captured cross-save birthday regression. Lifecycle evidence passes 15 checks. The API helper was stopped and port 8765 closed; all four save hashes remain unchanged. FM is left on the February 17 test copy at 08:00. Morale is a suitable next bounded investigation; simulation/rollover, independent-career coverage and match observations remain open. No observation-layer action controller was added.
