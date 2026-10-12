; CryRec installer. Per-user install, no admin needed.
#define AppVersion GetEnv("CRYREC_VERSION")

[Setup]
AppId={{A38983BB-4076-4045-A6FA-5273C2036256}
AppName=CryRec
AppVersion={#AppVersion}
AppPublisher=crymetr
DefaultDirName={autopf}\CryRec
DefaultGroupName=CryRec
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=CryRec-{#AppVersion}-setup
SetupIconFile=..\cryrec.ico
UninstallDisplayIcon={app}\CryRec.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\CryRec.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\CryRec"; Filename: "{app}\CryRec.exe"
Name: "{group}\Uninstall CryRec"; Filename: "{uninstallexe}"
Name: "{autodesktop}\CryRec"; Filename: "{app}\CryRec.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CryRec.exe"; Description: "Start CryRec"; Flags: nowait postinstall skipifsilent
