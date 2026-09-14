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

* `Player`: id, name, first_name, surname, date_of_birth (ISO date), age (integer), age_as_of (ISO in-game reference date), attributes (nine integer display values), positions (ratings >=15), position_ratings (14 integer values, 1..20), condition and match_sharpness (percentages, raw/100), morale (confirmed English label), morale_rating (ordinal byte).
* `Manager`: id, name.
* `Club`: id, name, squad (list of Player).
* `Game`: date (ISO in-game calendar date).

The game-date AOB resolves a separate four-byte global. Person birth dates store a year-specific 1-based ordinal and year. Age compares calendar month/day rather than comparing ordinal days from different leap/non-leap years. Batch reads share one date and check it again at the end; standalone player reads check the date before and after decoding. Both date and birth bytes are reread. A detected rollover rejects the observation. The unvalidated February 29 birthday convention on February 28 in a non-leap year fails closed. No time-of-day field is exposed.

Unknown fields are omitted rather than filled with invented values. Morale accepts all 20 independently confirmed byte/label pairs (1..20); other values reject the entire player or containing squad/club observation. See morale.md for validation and scouting-visibility scope. The roster matches the observed squad screen and can include loaned-out players. Returned objects are observations, not a simultaneous game snapshot: the game is never paused or suspended by the bridge. Bounds, registry bytes, identity, attributes, the contiguous readiness block, morale byte and roster references are reread to detect many concurrent changes, but reads across all players are not atomic. Use while the game is idle. Two saves of the same career were tested; reads during simulation have not been validated. Two Wycombe fitness records updated during idle navigation after reload despite unchanged displayed game time. Thirteen Gretna records also differed in condition and/or sharpness across a later restart; those non-morale fields were not independently validated numerically in Gretna. No settled-state flag is implemented.

The HTTP service reattaches after detected process exit or invalidated state on a subsequent request, with a two-second retry cooldown between attachment attempts. It returns 503 for unavailable observations and never returns a cached squad as fresh. Each successful attachment gets a UUID exposed as `session_id`; it identifies the bridge connection, not a saved game. `/status` checks the manager/club chain and current date as well as the registry because the live reload reused the original Person registry addresses. Continuous-service save reload/reconnect and one full-process restart were exercised; see `dates.md` for the expanded model's restart evidence. Multiple FM processes, multiple human managers and unemployed managers require explicit handling beyond this prototype. See `lifecycle.md` for the earlier status-check regression found and fixed during save-reload testing.
