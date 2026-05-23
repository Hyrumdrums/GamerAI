; Inno Setup script for the GamerAI Windows agent.
; Build agent.exe first (see build.bat), then compile this with Inno Setup 6.
;
;   ISCC.exe installer.iss /DMyAppVersion=1.1.29
;
; CI passes /DMyAppVersion=<value-of-AGENT_VERSION-in-agent.py> so the
; installer banner, Add/Remove Programs DisplayVersion, and output
; filename all track the agent binary's real version. Manual local
; builds without -D fall back to the placeholder below — the banner
; will read "0.0.0-dev" which is the visible signal that this is a
; dev build and not a CI artifact.
;
; Output: Output\GamerAI-Agent-Setup.exe

#define MyAppName "GamerAI Agent"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif
#define MyAppPublisher "GamerAI"
#define MyAppURL "https://example.com/gamerai"
#define MyAppExeName "agent.exe"

[Setup]
AppId={{A4F9B2E0-4E1B-4B1F-9C7D-C0FFEE5BADA55}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\GamerAI Agent
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=GamerAI-Agent-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#MyAppExeName}
; Tell Inno to use Restart Manager to detect agent.exe held open by
; the tray instance and ask it to close cleanly before install or
; uninstall. RM-aware apps get a friendly shutdown signal; non-aware
; apps get a brute-force taskkill fallback in [UninstallRun] below.
CloseApplications=force
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
; Startup is intentionally default-checked. The agent's value to the
; network is "online when idle"; an install that never runs is dead
; weight. Friction-respecting: the box is uncheckable for users who
; want strict opt-in, and the agent re-prompts on first run if the
; box was unchecked at install (see resolve_api_token / pair flow).
Name: "startupicon"; Description: "Run on Windows startup (recommended — the agent only contributes while running)"; GroupDescription: "Startup:"

[Files]
Source: "dist\agent.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "config.json";    DestDir: "{app}"; Flags: ignoreversion onlyifdoesntexist
Source: "README_addendum.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "tray.ico";       DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}";              Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\tray.ico"
Name: "{group}\{#MyAppName} (tray)";       Filename: "{app}\{#MyAppExeName}"; Parameters: "--tray"; IconFilename: "{app}\tray.ico"
Name: "{group}\Edit configuration";         Filename: "notepad.exe"; Parameters: """{app}\config.json"""
Name: "{group}\View logs folder";           Filename: "explorer.exe"; Parameters: """%APPDATA%\GamerAI\logs"""
Name: "{group}\Uninstall {#MyAppName}";    Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";         Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\tray.ico"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}";         Filename: "{app}\{#MyAppExeName}"; Parameters: "--tray"; IconFilename: "{app}\tray.ico"; Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: postinstall skipifsilent nowait

[UninstallRun]
; Step 1: retire the agent's bearer with the coordinator BEFORE Inno
; deletes agent.exe. Without this, state.json still works as a
; credential against the coordinator until someone manually revokes
; it server-side — even an "Add or remove programs" uninstall would
; leave a live token on disk. Best-effort: the agent exits 0 on any
; network failure, so an offline / coordinator-down uninstall never
; blocks. runhidden suppresses the console window. waituntilterminated
; keeps the (~2-3 s) network call ahead of [UninstallDelete] so the
; file that the agent reads exists when we call it.
;
; The --unpair branch in agent.py:main bypasses the single-instance
; check, so this runs successfully even when the tray instance is
; still alive and holding the IPC socket.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--unpair"; Flags: runhidden waituntilterminated; RunOnceId: "unpair"

; Step 2: kill any running agent.exe processes — including the tray
; instance that's likely still alive when the user uninstalls without
; closing first. Without this, the tray agent keeps heartbeating
; with a now-revoked token (401 spam) until the user reboots or
; manually ends the task. taskkill /F because the tray instance
; doesn't always respond to WM_CLOSE. Best-effort: '|| exit 0' so a
; "no process found" exit code doesn't fail the uninstall.
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#MyAppExeName} /T"; Flags: runhidden waituntilterminated; RunOnceId: "killtray"

[UninstallDelete]
; Wipe everything the agent writes outside Inno's install manifest so
; "uninstall" actually means uninstall:
;
; - %APPDATA%\GamerAI\  — state.json (bearer token!), config.json,
;                        sd\ (downloaded models, can be ~1.5 GB),
;                        logs\.
;   Leaving state.json behind on a machine that's being sold or handed
;   off would leak a working credential. Worth wiping unconditionally.
;
; - %LOCALAPPDATA%\GamerAI\ — update staging, boot/trace logs.
;
; - Auto-updater stash files in the install dir — the in-place
;   updater leaves agent.exe.previous / agent.exe.may12-stale /
;   config.json.bak-* siblings next to the live agent.exe. They're
;   not in Inno's install manifest so Inno doesn't touch them; without
;   these patterns ~48 MB of stale binaries linger in Program Files.
Type: filesandordirs; Name: "{userappdata}\GamerAI"
Type: filesandordirs; Name: "{localappdata}\GamerAI"
Type: files;          Name: "{app}\agent.exe.*"
Type: files;          Name: "{app}\config.json.bak-*"
; Best-effort removal of the install dir itself once its files are
; gone. A locked Explorer handle on %LOCALAPPDATA%\Programs\GamerAI
; Agent\ has previously left it as a stale empty dir; dirifempty
; deletes it if no handle is squatting, no-ops otherwise.
Type: dirifempty;     Name: "{app}"
