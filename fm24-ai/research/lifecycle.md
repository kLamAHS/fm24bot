# API lifecycle and post-load values

On 13 September 2026, kept API process 37092 running on 127.0.0.1:8765 while FM process 21328 loaded two disposable snapshots of the same Wycombe career. The exact executable remains hash-gated. No memory writes, Continue action, save command or fitness edit was issued.

## Observed sequence

| Checkpoint | UI state | API result |
|---|---|---|
| before-unload | February 17, 08:00 test copy | connected, 31 players; connection b28a0d84… |
| unloaded | Unload transition | status initially still connected; subsequent observations 503 |
| at-start-screen | Confirmed main menu | disconnected; all four observation routes 503, no cached data |
| earlier-loaded | February 7, 00:00 earlier test copy | connected, 31 players; new connection 8b254d67… |
| return-first-attempt | February 17 copy loaded again | old status check accepted reused registry; manager read rejected changed club; next request reattached as 4dd6fee5… |
| returned-first | February 17, 08:00 | 31 players; 29 complete models identical, two fitness differences |
| returned-first-idle | Same date/time after idle navigation | all 31 complete player models, manager and club identical to before-unload |

Detailed response bodies and timestamps are in `api-lifecycle-*.json`; the comparison report passes 14 checks. `lifecycle-processes-after.json` records the unchanged helper/FM process identities. These are two snapshots of one career, not independent databases. The earlier copy was made from the user's v02 backup, with source and destination hashes checked before loading.

Jude's earlier memory condition was 84.28 versus 86.32 in the February 17 copy. His ID, name, nine attributes and displayed positions matched the earlier profile. Exact earlier fitness numbers were not UI validated: that save loaded with its manager on vacation and the numeric editor menu did not open. No vacation action was taken. Earlier-save data differences establish fresh observations but do not independently validate every field on that date.

## Reused registry status issue

The full Person registry and the three sample player addresses were reused across these loads. The existing manager/club check correctly rejected stale context and triggered reattachment, but the old `/status` path checked only the registry and briefly reported the old connection as ready. The final code now checks the manager/club chain before reporting connected. A regression test recreates the observed stable-registry/changed-club case; 22 offline tests and 35 live API checks pass with the final code. The preserved lifecycle checkpoints were captured before this small status fix; a second full UI reload cycle was not repeated afterward.

Connection UUIDs identify bridge attachments. They are not save identities, atomic-snapshot tokens or proof that all game updates are complete.

## Fitness changes after loading

| Player / field | Before unload | First read after reload | Later idle read and numeric UI |
|---|---:|---:|---:|
| Jack Wakely condition | 100.00 | 99.90 | 100.00 |
| D'Mani Mellor condition | 97.69 | 97.06 | 97.69 |
| D'Mani Mellor sharpness | 84.23 | 85.20 | 84.23 |

The first post-load values also match the raw candidate capture taken before implementing readiness. The values later changed during idle navigation at the same displayed date/time. Both players are out on loan in the UI. The exact FM update trigger is not established; no background-simulation explanation is claimed as proven.

Opened only the existing Fitness detail tabs and dismissed with Escape. Jack's numeric values were condition 10000, sharpness 10000, fatigue -54. Mellor's were condition 9769, sharpness 8423, fatigue -214. All six match fresh read-only memory observations (`ui-post-reload-fitness.json`, `validation-post-reload-fitness.json`, and screenshots). The three original numeric profiles and all 30 position sets still pass after reload.

The first successful observation after loading can precede these updates. The API deliberately reports current memory and has no fixed delay or invented settled-state flag. Clients should keep FM idle and treat successive observations as potentially changing.

## Integrity and limits

Both originals and both test copies retain their initial SHA-256 (`save-integrity-after-readiness.json`). FM is left on the February 17 test copy, 08:00. The temporary API helper is stopped after verification; no startup service is installed.

Still untested: independent careers, multiple/unemployed human managers, other builds, simulation or matches in progress, and full FM process restart while the HTTP service remains running. The earlier full-process restart predates the new readiness fields. Broader match, morale, age/date and financial structures remain separate research tasks.
