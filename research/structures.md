# Runtime structures and public model

The implementation reads an independently opened Windows process handle with access mask 0x410. `FMProcess` owns and closes the handle. It enumerates modules using PSAPI and reads live PE sections. `Database` resolves a signature-derived global and a bounded registry of Person pointers. `FMBridge` indexes supported player UIDs once per session, then follows pointers for observations. It rescans code only when resolving a new session, not for each player request.

```text
main executable -> AOB + signed RIP displacement -> database root
  [root+0x68] -> [database+0x80] -> Person vector {begin,end}
  Person vtable -> complete-object locator -> back-offset -> Player

human-manager AOB -> global pointer -> human complete-object vector
  embedded Person -> full contract -> team -> club
                                     team -> roster vector -> players
```

The Person registry had 82,953 entries in this save. Player decoding currently supports RTTI back-offset 0x278 only; 26,223 such entries were indexed. Staff (+0xF8), other Person-derived types, and hybrid player/staff types are not exposed as Player models. The human-manager Person was located dynamically through membership and RTTI; the observed 0x450 offset is not a fixed resolver constant.

Names use wrapper -> entry pointer -> uint32 byte length -> UTF-8 bytes -> NUL. Club name uses a direct entry pointer. Null common names fall back to first name plus surname. Attribute fields use uint8 values in a 54-byte block; exact offsets, discovery and confidence appear in `offsets.md`.

Public frozen dataclasses contain no addresses:

* `Player`: id, name, first_name, surname, attributes (nine integer display values), positions (ratings >=15), position_ratings (14 integer values, 1..20), condition and match_sharpness (percentages, raw/100).
* `Manager`: id, name.
* `Club`: id, name, squad (list of Player).

Unknown fields are omitted rather than filled with invented values. The roster matches the observed squad screen and can include loaned-out players. Returned objects are observations, not a simultaneous game snapshot: the game is never paused or suspended by the bridge. Bounds, registry bytes, identity, attributes, the contiguous readiness block and roster references are reread to detect many concurrent changes, but reads across all players are not atomic. Use while the game is idle. Two saves of the same career were tested; reads during simulation have not been validated. Two fitness records updated during idle navigation after reload despite unchanged displayed game time. No settled-state flag is implemented.

The HTTP service reattaches after detected process exit or invalidated state on a subsequent request, with a two-second retry cooldown between attachment attempts. It returns 503 for unavailable observations and never returns a cached squad as fresh. Each successful attachment gets a UUID exposed as `session_id`; it identifies the bridge connection, not a saved game. `/status` checks the manager/club chain as well as the registry because the live reload reused the original Person registry addresses. Continuous-service save reload/reconnect was exercised; full-process restart with the HTTP service continuously running remains untested. Multiple FM processes, multiple human managers and unemployed managers require explicit handling beyond this prototype. See `lifecycle.md` for evidence and the status-check regression found and fixed during the test.
