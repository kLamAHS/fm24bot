# FM24 read-only observation bridge

Python objects and a local JSON API for **Football Manager 2024, Windows x64, Steam 24.4.2+2081827 / build 18129188**. Tested with Python 3.14.2 x64. No Cheat Engine dependency or third-party Python packages.

The current implementation has **109 passing offline tests**. All 15 observation routes passed an integrated full FM restart with the same API process running. The 14 data responses matched before and after restart, including non-empty shortlists, transfer targets and changed training/tactic settings. This does not establish full decoding of every game subsystem. Remaining work is tracked in [observation progress](research/observation-progress.md).

## Run

Load a supported save in FM24 and leave the game idle. Double-click `start-api.cmd`, or run `python -m api.server` in this folder. Open [status](http://127.0.0.1:8765/status) to see connection status, implemented capabilities and unresolved fields. The server listens only on **127.0.0.1:8765**. Stop it with Ctrl+C in its terminal. No startup service or scheduled task is installed.

Unknown executable hashes are rejected before decoding. The build check must be revalidated after a game update. Only one employed human manager is currently supported.

## Available observations

| GET route | Data |
|---|---|
| `/status` | Connection, PID, build, capabilities and unresolved fields |
| `/game` | In-game date and time |
| `/manager` | Human manager ID and name |
| `/club` | Club identity and current team roster |
| `/squad` | Current team roster players |
| `/players/{id}` | Identity, birth date, age, primary nationality, 47 attributes, positions, morale, cached readiness and employment/loan terms |
| `/finances` | Native GBP balance, transfer budget, weekly wage budget and payroll |
| `/fixtures` | Current calendar-year club fixtures and results |
| `/staff` | Owned club staff identities, jobs and supported contracts |
| `/tactics` | Selected tactic, mentality, validated positions/roles and selected players |
| `/inbox` | Message IDs, dates/times, sender, event type and unread state |
| `/training` | Committed weekly calendar and supported individual focus/intensity settings |
| `/scouting` | Stored player reports, reporting scout, completion date and supported knowledge labels |
| `/shortlists` | Default and named player shortlists and their members |
| `/transfer-targets` | Manager-owned target players, added date and supported type/status/priority labels |
| `/match` | Active live-viewer frame: clock, score, team statistics and player statistics; explicit unavailable state when no supported viewer exists |

Successful observations have `observed_at` (UTC), `session_id` (connection UUID) and `data`. A session ID changes on reconnect; it is not a save identifier. Names are UTF-8. Missing players return 404, malformed IDs 400, and unavailable or inconsistent reads 503. `/status` returns HTTP 200 with `connected: false` when no supported save is available. Write methods return 405; foreign Host/Origin headers are rejected; responses are not cached. There is no arbitrary-memory endpoint.

## Python

```python
from bridge.session import FMBridge

with FMBridge() as fm:
    print(fm.game())
    for player in fm.squad():
        print(player.id, player.name, player.attributes, player.morale)
    print(fm.tactics())
    print(fm.training())
    print(fm.scouting())
    print(fm.shortlists())
    print(fm.transfer_targets())
    print(fm.match())
```

The other methods correspond to the routes above; use `current_club()` for `/club`. `player_ids()` enumerates supported Player identities from the Person registry. The observed 26,223 IDs are not 26,223 individually UI-validated profiles. `players()` is a strict bulk decoder and may reject records outside the validated model. Player lookup follows the registry instead of repeatedly scanning the entire address space.

## Interpreting the data

* Unknown fields and unvalidated labels stay null or have an explicit status. Null is not zero, false, absent, or unlimited.
* Player attributes are actual memory values; scouting visibility restrictions are not applied to the general Player model. Scouting reports represent the manager's stored reports separately.
* Readiness is a dated cache. Condition and sharpness are withheld unless the cache timestamp exactly matches the game timestamp. A condition percentage does not establish injury status or match eligibility.
* The squad includes players shown in FM's team roster, including some loaned-out players. It is not a match-eligible selection list.
* Money uses native GBP and weekly wage units, independently of display preferences. FM may round displayed currency amounts.
* Match data comes from the decoded viewer frame. Replay/live timeline classification is still unresolved, and the model says so. Player condition, red cards, injuries and opposition formation remain null pending further validation. Possession is inferred from completed-pass share and is identified as an inference in the model.
* Inbox text/attachments, full scout prose/recommendation grades, transfer target terms/extra enums, shortlist expiry, all tactic roles/team instructions, current training ratings, and some training labels remain undecoded. A stored training week name may omit the UI's Modified suffix.

## Validation and safety

All process handles use **PROCESS_QUERY_INFORMATION | PROCESS_VM_READ (0x410)**. There are no process writes, remote allocation, injection, patching, hooks, privilege changes or suspension. UI experiments use independent project saves. No AI decision maker or action controller has been built.

Testing covers two snapshots of the same Wycombe career, players/staff from other clubs in that career, several UI experiments and multiple full FM restarts. Independent careers and other builds remain unvalidated. Reads are bounded and checked for detected changes, but they are not atomic snapshots. Keep FM idle and allow post-load updates to settle.

The latest integrated restart changed FM PID 45464 to 5464 while API PID 42772 stayed running. All 32 comparison checks passed, including relocated manager memory, a new connection UUID and identical public data. `/match` was correctly inactive in this restart test; active-match restart coverage is a separate outstanding check. See [restart comparison](research/observation-expanded-restart-comparison.json) and [disconnection behavior](research/expanded-api-disconnected.json).

```powershell
python -m unittest discover -s tests -v
python main.py find 29232937
python main.py game
python main.py squad
```

Older opt-in live checks use exact stored UI expectations and require the named February 17 regression copy. Advancing time or using another save legitimately changes expected values. Research output includes diagnostic memory addresses; successful API responses and public models do not.

Evidence: [environment](research/environment.md), [sources](research/sources.md), [offsets](research/offsets.md), [signatures](research/signatures.md), [structures](research/structures.md), [experiments](research/experiments.md), [morale](research/morale.md), [readiness](research/readiness.md), [finances](research/finances.md), [fixtures](research/fixtures.md), [staff](research/staff.md), [tactics](research/tactics.md), [inbox](research/inbox.md), [training](research/training.md), [scouting](research/scouting.md), and [match](research/live-match.md).
