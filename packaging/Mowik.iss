#ifndef MyAppVersion
  #define MyAppVersion "2.3.0"
#endif

#define MyAppName "Mówik"
#define MyAppPublisher "Igor Szuniewicz"
#define MyAppExeName "Mowik.exe"
#define MyAppUrl "https://github.com/Szunias/Mowik"

[Setup]
AppId={{F1B3A512-30EB-4D79-90CF-2B04CC16573B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppUrl}
AppSupportURL={#MyAppUrl}/issues
AppUpdatesURL={#MyAppUrl}/releases/latest
AppContact={#MyAppUrl}/issues
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription=Instalator aplikacji Mówik
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\Mowik
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
DisableWelcomePage=no
AllowNoIcons=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
WizardStyle=modern dynamic
WizardSizePercent=110
SetupIconFile=assets\Mowik.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
LicenseFile=LICENSE.txt
OutputDir=release
OutputBaseFilename=Mowik-{#MyAppVersion}-Setup
SourceDir=..
Compression=lzma2/normal
SolidCompression=no
LZMANumBlockThreads=1
CloseApplications=force
CloseApplicationsFilter={#MyAppExeName}
RestartApplications=no
AppMutex=Local\MowikLocalDictation
SetupLogging=yes
UsePreviousAppDir=yes
UsePreviousGroup=yes
UsePreviousTasks=yes
Uninstallable=yes

[Languages]
Name: "polish"; MessagesFile: "compiler:Languages\Polish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Utwórz skrót na &pulpicie"; GroupDescription: "Dodatkowe skróty:"; Flags: unchecked
Name: "autostart"; Description: "Uruchamiaj Mówika po &zalogowaniu do Windows"; GroupDescription: "Uruchamianie:"; Flags: checkedonce

[Files]
Source: "dist\Mowik\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{userstartup}\Mowik.lnk"
Type: files; Name: "{userstartup}\Mówik.lnk"

[Icons]
Name: "{autoprograms}\Mówik\Mówik"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Comment: "Lokalne dyktowanie push-to-talk"
Name: "{autoprograms}\Mówik\Centrum Mówika"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--settings"; WorkingDir: "{app}"; Comment: "Ustawienia Mówika"
Name: "{autoprograms}\Mówik\Odinstaluj Mówika"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Mówik"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Mowik"; ValueData: """{app}\{#MyAppExeName}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Uruchom Mówika"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (not WizardIsTaskSelected('autostart')) then
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Mowik');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Mowik');
end;
