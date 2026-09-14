# Club finance observations

Build: Steam Windows x64 24.4.2+2081827; the executable hash gate remains mandatory. These fields were discovered by following the current club's pointers and checking MSVC type metadata, then compared with FM's displayed amounts.

| Path in the full layout | Type | Interpretation | UI comparisons |
|---|---|---|---|
| Club +0x150 | pointer | CLUB_FINANCE object | Wycombe, Bristol Rovers, Arsenal |
| Finance +0x08 | pointer | Owner club | Must equal the requested club |
| Finance +0x14 | int32 | Bank balance in GBP | Three clubs; Wycombe also across two save dates |
| Finance +0x7CC | int32 | Native transfer budget in GBP | Arsenal exact; Wycombe and Bristol each differ from display by GBP 1 |
| Finance +0x810 | int32 | Weekly wage budget in GBP | Three clubs |
| Finance +0x81C | int32 | Weekly current payroll spending in GBP | Wycombe on both dates |

The required complete-object RTTI name is `.?AVCLUB_FINANCE@db@@`. Its vtable RVA is 0x5A54048 in this build. Gretna instead has `.?AVCLUB_FINANCE_BASE@db@@`, a shorter layout; it is rejected, because applying the full-layout offsets reads unrelated objects. Negative bank balances are valid. Negative wage or spending fields are unvalidated and rejected.

The API reports native whole GBP and weekly periods, regardless of display preferences. No exchange-rate or annual-salary approximation enters the public model. The currency table is database root +0x30, registry +0x80; a currency record's +0x38 float32 gives units per GBP. USD was 1.249332070350647. This independently reproduces Wycombe's displayed bank balances ($13,628,970 on February 7 and $13,407,842 on February 17) and annual wage budget ($5,130,892).

## Independent display readings

| Club / date | Bank GBP | Transfer budget displayed GBP | Native transfer GBP | Wage budget GBP/week | Current spending GBP/week |
|---|---:|---:|---:|---:|---:|
| Wycombe / Feb 7 | 10,909,005 | 1,720,250 | 1,720,251 | 78,979 | 431,417 |
| Bristol Rovers / Feb 7 | -1,879,153 | 17,330 | 17,331 | 85,928 | Not independently read |
| Arsenal / Feb 7 | 56,033,809 | 63,785,379 | 63,785,379 | 3,870,924 | Not independently read |

The transfer field's interpretation has strong supporting comparisons, but exact presentation is unresolved. The GBP 1 difference is preserved and disclosed; neither clearing a low bit nor rounding to a multiple of five explains all three clubs. Do not use the API's amount as an exact reproduction of the UI display. Scouting budget, debts, monthly totals, next-season budget and committed-spending distinctions are not promoted.

Evidence: `finance-probe-{earlier,later,bristol-earlier,arsenal-earlier,gretna-earlier}.json`, and matching `ui/finances-*.png`. The raw captures include research addresses; the API does not. Wycombe's amounts also appear on its normal finance summary. Bristol and Arsenal were inspected in the installed in-game editor without typing into fields.

## Editor side effect and recovery

After closing Arsenal's details with Escape and navigating away, FM displayed **"Removed loan as monthly repayment was not set."** No field had been edited. This demonstrates that opening/closing the built-in club editor can trigger automatic validation. The February 7 test copy was immediately reloaded without issuing a save command, discarding the transient state. Subsequent comparisons use normal finance screens. Preserve `ui/editor-automatic-loan-validation.png` as evidence; do not describe that editor path as a guaranteed read-only UI operation.

## Consistency and lifecycle

The reader checks type, owner, pointer identity, amounts and game timestamp twice, and rejects detected changes. This is not an atomic snapshot. The finance object changed address across the two save loads. The integrated FM restart 45464 → 5464 reproduced the public model exactly; the second snapshot and final baseline also returned HTTP 200. See observation-expanded-restart-comparison.json and observation-checkpoint-final-baseline.json. This does not extend coverage to undecoded fields.
