# Game date, birth date and age

Validated on Windows x64 Steam FM 24.4.2+2081827, executable SHA-256 recorded in `signatures.md`, using two disposable copies of the same Wycombe career. The bridge exposes `Game.date`, `Player.date_of_birth`, `Player.age` and `Player.age_as_of`. No computer-clock fallback or guessed time-of-day field is used.

## Discovery and encoding

The existing local fm_scouter research supplied the current-date AOB and Person birth-date offsets. Its code was inspected, not executed or copied; attribution is in `sources.md`. Captured raw bytes and nearby instructions before implementing the decoder: `date-candidates-first.json`.

The date AOB `83 F2 01 8B 05 ?? ?? ?? ?? 66 09` produced exactly one hit. Signed RIP resolution at displacement +5, next instruction +9, located a four-byte global. Observed instruction RVA `0x20B566B`, target RVA `0x631D5BC`. Production code resolves the signature from live executable sections; these RVAs are evidence only.

| Location/type | Interpretation | Evidence |
|---|---|---|
| Date global, uint32 LE | High 16 bits year; low 9 bits 1-based day of year | Two game dates and a full restart |
| Person+0x44, uint16 LE | 1-based day of birth within birth year | Four independently viewed profiles |
| Person+0x46, uint16 LE | Birth year | Same four profiles |
| Calculated integer | Age on the captured in-game calendar date | Four profiles and 31 squad ages; birthday across saves |

Game date raw `3012e807` is year 2024/day 48, February 17. The earlier copy gives `2600e807`, year 2024/day 38, February 7. Bits 9..15 were 9 and 0 respectively. FM displayed 08:00 and 00:00, but the encoding of these bits is unresolved and the API omits time.

## Independent UI comparisons

Profile dates were transcribed from FM's UI and preserved in `ui-date-observations.json`; screenshots are in `ui/`. The later save was February 17, 2024.

| Player / ID | Birth date shown | Raw ordinal / year | Age shown |
|---|---|---|---:|
| D'Mani Mellor / 28115830 | 2000-09-20 | 264 / 2000 | 23 |
| Jack Grimmer / 61031508 | 1994-01-25 | 25 / 1994 | 30 |
| Jude Bellingham / 29232937 | 2003-06-29 | 180 / 2003 | 20 |
| Christian Eriksen / 27010680 | 1992-02-14 | 45 / 1992 | 32 |

These cover different ages, birthdays before and after the current date, and both leap and non-leap birth years. Mellor's September date checks conversion beyond February in a leap birth year. The General Info squad view supplied all 31 ages, independently transcribed in `ui-squad-ages.json`. All matched. This does not establish that each of those 31 exact birth dates was independently viewed.

`validation-dates-before-reload.json` contains 54 date-section comparisons: three date/squad-context checks, five identity/date/age checks on each of four profiles, and 31 squad ages. All passed. Existing identity, attribute, readiness and position checks also passed.

## Birthday across saves

Kept API process 35048 running and used FM's normal Load Game interface:

1. February 17 test copy: Eriksen age 32, born February 14, 1992.
2. February 7 earlier test copy: Eriksen age 31, same birth date. Independently opened his profile; `ui/date-eriksen-feb7.png` shows age, birth date, ID and game date together. Six comparisons passed in `validation-dates-earlier.json`.
3. Returned to February 17: API returned age 32 again.

Both loads reused the existing FM process 21328. The first `/status` request after each direct load rejected the changed manager/club context, and the following request reattached and returned the correct game date and age. The original registry addresses being reused did not cause a stale connected status. The API service process stayed running throughout.

This compares dates on opposite sides of the birthday using pre-existing snapshots. No Continue action, birthday-day simulation or save command was issued. Unit tests cover the exact day-before/day-of/day-after calculation; live exact-midnight rollover remains untested.

## Full restart with the API running

Exited FM normally, verified PID 21328 absent, and relaunched the installed executable as PID 41840. The API process retained the same PID, executable path and start time throughout; evidence is in `date-api-process-start.json` and `date-api-process-before-stop.json`.

While FM was absent and while the new process was starting without a loaded save, `/status` reported disconnected and `/game`, `/squad` and the player route returned 503. The file named `date-api-restarted-menu.json` was actually captured during the startup splash, before reaching the menu; it is evidence of unloaded startup, not a separate menu-state check. The menu was then observed and the explicitly named February 17 test copy loaded.

The next API checkpoint reattached with a new connection UUID and FM PID. All 31 complete Player models were identical to the pre-restart checkpoint, including birth date, age, age reference date, readiness and attributes. Eriksen's date/age fields also matched. All 54 date-section comparisons and the existing UI validation passed again in `validation-dates-after-restart.json`. All 53 live API/Python checks then passed on PID 41840.

All four date-profile heap addresses changed. For example, Jude's complete-player address changed from `0x116AF8940` to `0x1173C5490`; the date signature and target RVAs resolved again unchanged. The module retained base `0x140000000`, so module-base relocation was not empirically exercised. `date-lifecycle-comparison.json` records all 15 checks and the exact profile addresses.

The temporary API helper was stopped after testing, and port 8765 was verified closed. Both original saves and both disposable copies retained their original hashes (`save-integrity-after-dates.json`). FM remains on the February 17 test copy at 08:00.

## Rejection behavior and limits

Calendar conversion validates year 1..9999 and the appropriate 365/366-day limit, including century leap rules. Invalid dates, truncated reads, future birth dates, ambiguous/out-of-module signature targets, unloaded registries and detected changes are rejected. Age compares month/day, not ordinal numbers from years with different leap status. Birth bytes are reread; single-player and batch observations check the game date before and after decoding. These checks do not create an atomic snapshot or detect a change away and back between reads.

FM's February 29 birthday convention on February 28 in a non-leap year is not established. That specific case raises an unavailable observation instead of guessing. Other leap-date arithmetic has automated coverage, but no February 29-born player was inspected live.

Confidence is high for the documented exact build and career snapshots. Independent careers, other builds, simulation/rollover, time-of-day decoding, and generated players with unusual birth-date data remain outside verified scope. A current calendar date is not a save identifier or a promise that post-load fitness updates have settled; the prior readiness investigation documented such updates.

The final suite has 31 offline tests, including nine date tests using captured bytes, independent UI expectations and rejection cases. Reproduce with `python -m unittest discover -s tests -v`, `python main.py validate`, and `python -m tests.live_smoke` with the later test copy loaded.
