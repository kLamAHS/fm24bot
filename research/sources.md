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
