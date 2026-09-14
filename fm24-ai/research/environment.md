# Environment discovery — 2026-09-13

Observed executable: `D:\SteamLibrary\steamapps\common\Football Manager 2024\fm.exe`.
File version **24.4.2**, product version **24.4.2+2081827**. UI main menu also displays 24.4.2.
Steam app 2252570, installed build ID **18129188** (local app manifest).
Initial PID **28168**, process name **fm.exe**, native x64 confirmed with IsWow64Process2 and PE machine 0x8664.
Initial module base 0x140000000; image size 494956544. This is an observation, never a resolver constant.
See `modules-initial.json` for loaded modules; these include Windows and game runtime libraries.

Python: `C:\Python314\python.exe`, 3.14.2, 64-bit. Git available.
Visual Studio Build Tools 2022 17.14.23 installed. No .NET SDK returned by `dotnet --list-sdks`.
Cheat Engine 7.5.0.7431 installed in `C:\Program Files\Cheat Engine 7.5`; its x64 process is running.
No FM24 CT found in Downloads, Documents, redirected OneDrive folders, Cheat Engine directory, or D drive searches.
One unrelated CT exists and was not used. No third-party binaries installed or executed.

Read-only attach succeeded with mask **0x410** (PROCESS_QUERY_INFORMATION | PROCESS_VM_READ).
ReadProcessMemory returned `MZ` at the discovered image base. VirtualQueryEx returned committed/read-only image memory.
Toolhelp module snapshots returned access denied. PSAPI module enumeration succeeded using the same 0x410 handle; no elevation or privilege changes.

FM initially at main menu, no save loaded. Original save found under redirected OneDrive Documents.
A byte-for-byte test copy was created in the workspace `work/Bridge Test.fm`; its SHA-256 is recorded separately.
The bridge never reads save file contents; this copy is solely for FM's normal Load Game UI.
