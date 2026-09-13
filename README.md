# FM24 read-only memory bridge

Working prototype for **Football Manager 2024, Windows x64, Steam 24.4.2+2081827 / build 18129188**. No Cheat Engine dependency or third-party Python packages. Tested with Python 3.14.2 x64 on this PC.

The first milestone is complete: selected players' IDs, names and attributes were read directly from memory and compared with FM's UI. The bridge also retrieves the human manager, current club and all 31 players in the observed Wycombe squad view. These observations passed a full FM exit/relaunch of the same test save.

The bridge also reads **positions, position ratings, condition and match sharpness**. Three full numeric profiles and 30 displayed squad position lists match FM's UI. Two additional fitness profiles were checked after a live save reload. See [readiness validation](research/readiness.md).

## Start

1. Run FM24 and load **FM24 Bridge Test 2026-09-13**. Leave the game idle.
2. Open a terminal in this folder, or double-click `start-api.cmd`.
3. Run `python -m api.server` if using a terminal.
4. Open [status](http://127.0.0.1:8765/status), [squad](http://127.0.0.1:8765/squad) or [Jude Bellingham](http://127.0.0.1:8765/players/29232937).

The server listens only on **127.0.0.1:8765**. Stop it with Ctrl+C in its terminal. The integration test server was stopped after verification; no background startup task or service is installed. Running FM without a loaded save produces `connected: false`. Unknown executable hashes are rejected before decoded reads. Do not simply disable the build check after an FM update.

## Read from Python

```python
from bridge.session import FMBridge

with FMBridge() as fm:
    player = fm.player(29232937)
    print(player.id, player.name, player.attributes)
    club = fm.current_club()
    for player in club.squad:
        print(player.id, player.name, player.positions, player.condition)
```

`fm.manager()`, `fm.squad()` and `fm.player_ids()` are also available. The registry supplies player lookup without repeated address-space scans. `fm.players()` attempts strict decoding of every supported player-type record; it currently raises on an unvalidated attribute elsewhere in this save. Use the validated squad or individual lookups. The 26,223 indexed IDs are not 26,223 UI-validated profiles.

The lower-level `bridge.FMProcess` provides attach, modules, bounded byte/string reads, integers, floats, doubles and pointers for further investigation. All process handles use **PROCESS_QUERY_INFORMATION | PROCESS_VM_READ (0x410)**. No writes, remote allocation, injection, patching, hooks, privilege elevation or process suspension are implemented.

## HTTP contract

| GET route | Result |
|---|---|
| `/status` | Connection state, PID, build, capabilities and unresolved fields |
| `/manager` | Human manager ID and name |
| `/club` | Club ID, name and squad |
| `/squad` | List of current team roster players |
| `/players/{id}` | Player ID, names, nine attributes, positions, condition and match sharpness |
| `/game`, `/fixtures`, `/finances`, `/match` | 501: fields not yet validated |

Successful observation routes return `{"observed_at":"UTC timestamp","session_id":"connection UUID","data":...}`. A new bridge connection gets a new session ID, allowing clients to distinguish reconnects; this is not a save identifier or proof that FM has finished all post-load updates. Names are UTF-8. Missing players return 404; malformed IDs return 400; unavailable or inconsistent memory returns 503. `/status` validates the registry and manager/club context, returning HTTP 200 with `connected: false` when unavailable. Write methods return 405. Foreign Host/Origin headers are rejected; responses are not cached. The API has no arbitrary-memory endpoint and no AI/action controller.

`condition` and `match_sharpness` are percentages normalized from FM's numeric 0..10000 fields (divide by 100). `positions` lists familiarity ratings >=15; `position_ratings` includes all 14 mapped positions on FM's 1..20 scale. The legacy sweeper slot is not exposed. Condition does not imply that an injured player is available.

Selected player fields verified against the UI:

```json
{
  "id": 29232937,
  "name": "Jude Bellingham",
  "first_name": "Jude",
  "surname": "Bellingham",
  "positions": ["DM", "MC", "AMC"],
  "condition": 86.32,
  "match_sharpness": 100.0,
  "attributes": {
    "acceleration": 15,
    "pace": 14,
    "passing": 17,
    "finishing": 16,
    "technique": 17,
    "decisions": 15,
    "vision": 16,
    "work_rate": 18,
    "strength": 13
  }
}
```

## Validation and evidence

* **Three profiles:** Jude Bellingham, Ryan Tafazolli and Franco Ravizzoli. 32 UI assertions passed: six name/ID checks and 26 visible attributes. Goalkeeper finishing was not visible and was excluded.
* **31 squad members:** every name matched the squad UI. All nine fields decoded for each member, but individual attribute values were directly compared with UI only on the three profiles above.
* **One full restart:** PID 28168 -> 21328. Sample player heap addresses changed; both signatures resolved again, and all 31 returned player models remained identical.
* **22 offline tests and 35 live API checks passed.** Guard tests include stale registry rejection, unsupported build rejection, invalid readiness values and a reused-registry/changed-club regression. Three numeric detail profiles provide 51 additional raw-field assertions; 30 squad position lists match.
* **Continuous API reload test:** February 17 test copy -> main menu -> February 7 test copy -> February 17 test copy. The same API process rejected unavailable observations and reconnected. All 31 full player models matched the initial snapshot after post-load values settled. Two players had temporary fitness differences; six additional numeric UI checks confirmed their later values. See [lifecycle results](research/lifecycle.md).
* Both original saves and both named test copies retain their original SHA-256. No Continue or save command was issued. FM's screen-ID display preference was enabled for validation and remains enabled.

Reports: [restart comparison](research/restart-comparison.json), [readiness after reload](research/validation-readiness-after-reload.json), [expanded live API validation](research/api-readiness-validation.json), [lifecycle comparison](research/api-lifecycle-comparison.json), [save integrity](research/save-integrity-after-readiness.json), and [profile screenshots](research/ui/).

```powershell
python -m unittest discover -s tests -v
python main.py validate --output research\validation-latest.json
python -m tests.live_smoke
python main.py find 29232937
python main.py squad --output research\squad-latest.json
```

The live validation commands compare against this specific test save's UI observations; advancing time or using another save can legitimately change expected values. Research command output includes memory addresses as diagnostic evidence; the public model and successful API responses do not.

## Scope and next work

This is a validated starting observation layer, not a complete autonomous manager. **Age, morale, fixtures, finances and match state are not implemented.** Date-of-birth, fatigue interpretation and additional source offsets remain in the research log. Bulk database attributes are not fully decoded.

Testing covers two snapshots of the same Wycombe career, one full FM restart for the original identity/attribute fields, and save reloads with the API running for the expanded model. Independent careers, other builds, multiple human managers, unemployment, game-time advancement and continuous-service full-process restart still need validation. The roster includes loaned-out players shown by FM's squad view; it is not a match-eligible selection list. Memory reads are checked for changes but are not an atomic snapshot. Keep FM idle during observations, and allow post-load values to settle; a successful read does not prove the game has stopped updating.

Structure facts and source attribution are documented in [sources](research/sources.md), [offsets](research/offsets.md), [signatures](research/signatures.md), [structures](research/structures.md) and the [experiment log](research/experiments.md). No third-party research code is required or executed by this project.
