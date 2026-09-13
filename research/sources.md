# Research sources

Inspected https://github.com/dgarfias/fm_scouter at commit `f70ded1f11b75db93be32275435754b9d458b379`.
Source snapshot kept under workspace `work/fm_scouter`; not a runtime dependency and never executed.
Files examined: `fm_scout/scanner.py`, `offsets.py`, `player.py`, `memory.py`.
This is a Linux/Proton FM24 24.4.2 reader. Its offset comments cite FMCET24.CT; that original CT has not yet been located or verified here.
The repo does not provide a conventional open-source license. Our reader is independently implemented; structure facts and signatures are attributed here.

Candidate facts to validate locally:

* dbtRoot candidate from `48 8D 0D ?? ?? ?? ?? 48 8D 15`, signed RIP displacement at +3, instruction length 7.
* Person registry: `[dbtRoot+0x68] -> [+0x80] -> vector begin/end`.
* Person subobject: UID +0x0C, DOB +0x44/+0x46, name-entry pointers +0x58/+0x60/+0x68.
* MSVC complete-object locator at `[vtable-8]`, subobject back-offset at locator+4; player candidate 0x278.
* Player attributes: complete player object+0x217, 54 bytes, candidate display rounding `(raw+2)//5`.
* String bytes follow 4-byte entry header; wrapper variants require separate evidence.
* Human manager signature and club chain available for a later experiment after one player is validated.

Also examined FMSuperScout's README: it targets FM26 and requires an injected BepInEx plugin. Rejected as unsuitable for this project's read-only external-process constraint.

## Readiness follow-up
Inspected [PhilipArmstead/Football-Manager-Squad-Analyzer](https://github.com/PhilipArmstead/Football-Manager-Squad-Analyzer/blob/b94f7780defa73e723b74e2d38584cd8e06ec897/NOTES.md), commit `b94f7780defa73e723b74e2d38584cd8e06ec897`. Its notes identify candidate sharpness/condition fields at player+0x1F4/+0x1F8 and fatigue at +0x1F6, plus the same position byte order. Local source snapshot is in workspace `work/squad-analyzer-research`. Some source functions write game memory; none of this source was built, imported or executed. Only structure facts were used as leads and independently checked against FM's numeric UI.

The earlier fm_scouter source gives position block +0x208 and the 15-slot order. The legacy sweeper slot has no corresponding field in the inspected FM UI and is deliberately omitted from the public model. `ui-readiness-observations.json` is the independent numeric ground truth for the implementation.

## Date follow-up

Revisited the same local fm_scouter snapshot, `fm_scout/scanner.py` (current-date resolver) and the Person offsets. Its current-date signature `83 F2 01 8B 05 ?? ?? ?? ?? 66 09` and nine-bit ordinal mask supplied the lead. Its real-world date fallback was deliberately excluded. The squad-analyzer snapshot contains a candidate absolute date address, which was treated only as corroborating research, never as a production constant; its date conversion code was not copied.

The independently implemented date reader was compared with FM's two test-save dates, four player birth-date profiles and 31 squad ages. Gregorian conversion uses Python's standard calendar/date tools. The sources remain unexecuted and are not dependencies. Exact raw bytes and uncertainty are recorded in `dates.md`.
