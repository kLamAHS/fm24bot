# Lazy fitness refresh investigation

The previously observed condition/sharpness differences after reload are caused,
at least in the controlled Gretna comparison, by FM refreshing cached fitness
when its player list is opened. No process writes, game-time advancement, or
player edits were used in this experiment.

## Evidence

On February 17 at 08:00, 18 supported Gretna players were captured before opening
their club, again while the search results were displayed, and after opening the
Players tab. All 18 fitness blocks and timestamps were unchanged during the idle
interval. After the player list opened, 15 of the 18 fitness blocks changed and
all 18 timestamps became the current game timestamp. The third file is named
`morale-gretna-capture-fitness-after-club.json`; its actual capture occurred after
the Players tab had opened, not just the club overview.

Complete Player +0x150 is a four-byte packed timestamp in the same representation
as the signature-resolved game date. It changed from `2f4ae807` (Feb 16) to
`3012e807` (Feb 17, 08:00) for the controlled 18-player group. Archived Feb 7
captures show the same pattern across restart: `254ae807` before their screen was
opened and `2600e807` afterwards, matching that save's current timestamp.

Jonny Jamieson (61038032) changed from condition 7800 / sharpness 9000 to
condition 9630 / sharpness 8450. The numeric in-game Fitness tab independently
displayed 9630 / 8450 / fatigue 0 afterwards. Screenshot:
`ui/fitness-jonny-jamieson-numeric.png`.

## API policy

Player `readiness.status` is `current` only when this timestamp equals all four
bytes of the current game timestamp. It is `stale` when they differ, or `unknown`
if the cached timestamp cannot be decoded. `updated_on` is its calendar date,
or null. Time-of-day decoding is deliberately separate from equality checking.

Condition and match sharpness are null unless the timestamp is current. Position
ratings and morale are independent and still returned. Raw cached values remain
available in research evidence. The bridge never opens a screen or forces an
update to make a memory observation appear fresh.

This is conservative: 29 Wycombe players retained midnight timestamps at 08:00
even after their squad screen was shown. Their cached values might still be
valid; timestamp inequality does not prove a numerical difference. The API
withholds them because it cannot certify their freshness at the current time.
Matching timestamps likewise do not establish an atomic snapshot across players
or live-match readiness support.

Confidence: high for the observed lazy-update behavior; conservative inference
for freshness policy. Tested on 18 players, two save dates and existing restart
captures. Same-day timestamp inequality is covered by a regression test. A new
live restart of the changed public contract remains to be performed.
