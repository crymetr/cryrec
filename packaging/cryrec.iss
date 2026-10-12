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
AppSupportURL=https://github.com/crymetr/cryrec
; PrepareToInstall waits on the app mutex instead. Never force-kill: it could be recording.
CloseApplications=no
RestartApplications=no

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
; After a silent in-app update, start the new version again.
Filename: "{app}\CryRec.exe"; Flags: nowait; Check: WizardSilent

[Code]
const
  AppMutex = 'CryRec-singleton-A38983BB-4076-4045-A6FA-5273C2036256';

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  I: Integer;
begin
  Result := '';
  // In-app update: the app exits right after launching us. Give it ~10 s.
  for I := 1 to 20 do
  begin
    if not CheckForMutexes(AppMutex) then
      Exit;
    Sleep(500);
  end;
  Result := 'CryRec is still running. Stop any recording, close CryRec, then run setup again.';
end;
