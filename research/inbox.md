# Human inbox — metadata decoder, text investigation pending

Windows Steam 24.4.2+2081827; observations on PID 45464, independent FM24 Match Observation save at 2024-02-07. The API uses only the current-human ownership chain, with no whole-process scans and no UI actions.

| Owner | Offset | Type | Field / validation |
|---|---:|---|---|
| Complete HUMAN_NON_PLAYER | 0x338 | pointer | Inbox vector holder |
| Holder | 0/8 | pointers | Begin/end of news-item pointers; 75 items |
| NEWS_ITEM base | 0x90 | Person pointer | Sender; must belong to current registry |
| NEWS_ITEM base | 0xA0 | packed game date/time | Date and quarter-hour time encoding |
| NEWS_ITEM base | 0xA8 | uint32 | Message ID; unique within current inbox |
| NEWS_ITEM base | 0xB0 bit 0 | bit | Read flag |
| NEWS_ITEM base | 0xB3 | uint8, 0–14 or FF | Minutes subtracted from nominal time; clamp at midnight. FF means exact time not initialized. |
| Complete SUPPORT_STAFF | 0x30/0x38 | name wrapper pointers | UI first/surname |

The sender reference to SUPPORT_STAFF is its embedded Person at +0x88. Names in the complete support object match Sarah McHugh, Robert Dobson and Jack Wright. Reading adjacent generic Person-name offsets would incorrectly return other names. ACTUAL_NON_PLAYER, HUMAN_NON_PLAYER and ACTUAL_PLAYER sender identities use the already validated Person layout.

Discovery: one-off RTTI research located NEWS_MANAGER at global RVA 0x63651C0. Its +0x20 vector contains 3,140 world news items and is **not the manager inbox**. Code at RVA 0x34A8460 follows a human-owned +0x338 container for message lookup. The production decoder follows that owned chain and checks that every concrete item has a nonvirtual offset-zero NEWS_ITEM@db base in bounded MSVC RTTI. Different message classes share this base; subclass-specific contents remain undecoded.

UI validation: 47 independently transcribed date/time/sender combinations across January 23–February 2, spanning all twelve observed senders. The first three message openings produced unread counts 74, 73 and 72. The captured bytes show exactly two further transitions at B0: 32→33 for the transfer deadline and recruitment meeting items. The two midnight messages display 00:00 even with minute adjustments of 11 and 14. Seven offline tests cover this evidence, sender membership, hierarchy validation, duplicate IDs, changed records, changed containers, future dates and invalid minute adjustments.

Evidence: `inbox-read-trace-third-read.json`, `inbox-human-initial.json`, `inbox-human-third-read.json`, `inbox-sender-layout.json`, and `ui/inbox-*.png`. The full restart from PID 45464 to 5464 reproduced all 75 records exactly, including 72 unread messages.

Second-save validation on February 17 found an initialization edge case. The newest COMP_MATCHDAY_PREVIEW_NEWS record had B3=FF immediately after loading, while the other 74 messages had valid adjustments. Opening the inbox changed it to 9 and displayed 07:51 for the 08:00 nominal slot. Reloading the unchanged save reproduced FF. The decoder now returns time=null and time_status=not_initialized for this sentinel, preserving the other valid message metadata and the six unread messages. It never invokes the game's getter or opens the UI. All other out-of-range adjustments are rejected. Evidence: inbox-second-save-raw.json, inbox-second-save-opened-raw.json and inbox-read-trace-second-save-cold.json. The integrated second-save cold check returns HTTP 200 for all 15 sampled routes. Eight offline inbox tests cover the original comparisons and this cold-load regression.

`GET /inbox` currently returns message IDs, dates, times, unread flags, sender identities and engine event-type names. **Subjects and bodies are null with text_status=not_decoded.** Text research found formatted headlines in UI objects and string entries, not in the base news records. Reading a currently rendered widget does not establish a reliable complete-inbox text source. See `inbox-text-candidates.json` and `inbox-title-references.json` for remaining leads. Attachments, response requirements, subjects and bodies are still being investigated.

All ownership and record bytes are repeated before return, with registry/date/current-human checks. Changes fail the observation; the API returns 503 instead of mixing inbox states.
