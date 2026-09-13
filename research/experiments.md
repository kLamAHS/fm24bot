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
