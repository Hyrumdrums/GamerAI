; Inno Setup script for the GamerAI Windows agent.
; Build agent.exe first (see build.bat), then compile this with Inno Setup 6.
;
;   ISCC.exe installer.iss
;
; Output: Output\GamerAI-Agent-Setup.exe

#define MyAppName "GamerAI Agent"
#define MyAppVersion "0.1.0"
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

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "startupicon"; Description: "Run on Windows startup (tray mode)"; GroupDescription: "Startup:"; Flags: unchecked

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

[UninstallDelete]
Type: filesandordirs; Name: "{userappdata}\GamerAI\logs"
