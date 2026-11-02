#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif

#define MyAppName "myDATA Classifier"
#define MyAppExeName "myDATA Classifier.exe"

[Setup]
AppId={{8CC7589D-8531-4D61-917B-ED77EDAD83FB}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=myDATA Classifier
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=installer-output
OutputBaseFilename=myDATA-Classifier-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
AppMutex=myDATAClassifier-8CC7589D-8531-4D61-917B-ED77EDAD83FB
LicenseFile=LICENSE
SetupIconFile=static\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "dist\myDATA Classifier\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// Τα δεδομένα (%LOCALAPPDATA%\myDATA Classifier, βλ. windows_launcher.user_data_dir)
// ζουν έξω από το {app}, οπότε η απεγκατάσταση τα αφήνει. Ερώτηση με προεπιλογή «No»·
// σε silent απεγκατάσταση δεν σβήνεται τίποτα.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    exit;
  DataDir := ExpandConstant('{localappdata}\{#MyAppName}');
  if not DirExists(DataDir) then
    exit;
  if SuppressibleMsgBox('Also delete all {#MyAppName} data (database, settings, logs)?'
      + #13#10#13#10 + DataDir + #13#10#13#10
      + 'This cannot be undone. Choose No to keep the data for a future reinstall.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
    if not DelTree(DataDir, True, True, True) then
      MsgBox('Could not delete ' + DataDir + '. Delete it manually.', mbError, MB_OK);
end;
