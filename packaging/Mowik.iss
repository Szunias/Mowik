#ifndef MyAppVersion
  #define MyAppVersion "2.7.2"
#endif

#ifndef MyOutputBaseFilename
  #define MyOutputBaseFilename "Mowik-" + MyAppVersion + "-Setup-UNSIGNED"
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
VersionInfoDescription=Mówik installer
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
ShowLanguageDialog=yes
LanguageDetectionMethod=uilanguage
UsePreviousLanguage=yes
SetupIconFile=assets\Mowik.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
LicenseFile=LICENSE.txt
OutputDir=release
OutputBaseFilename={#MyOutputBaseFilename}
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
ASLRCompatible=yes
DEPCompatible=yes
#ifdef SignedRelease
SignTool=MowikAuthenticode
SignedUninstaller=yes
SignToolRetryCount=3
SignToolRetryDelay=2000
SignToolMinimumTimeBetween=1000
#else
SignedUninstaller=no
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "polish"; MessagesFile: "compiler:Languages\Polish.isl"

[CustomMessages]
english.DesktopIconTask=Create a &desktop shortcut
polish.DesktopIconTask=Utwórz skrót na &pulpicie
english.AdditionalShortcutsGroup=Additional shortcuts:
polish.AdditionalShortcutsGroup=Dodatkowe skróty:
english.AutoStartTask=Start Mówik automatically when I &sign in to Windows
polish.AutoStartTask=Uruchamiaj Mówika po &zalogowaniu do Windows
english.StartupGroup=Startup:
polish.StartupGroup=Uruchamianie:
english.LocalDictationComment=Private local push-to-talk dictation
polish.LocalDictationComment=Lokalne dyktowanie push-to-talk
english.SettingsShortcut=Mówik Settings
polish.SettingsShortcut=Centrum Mówika
english.SettingsComment=Mówik settings
polish.SettingsComment=Ustawienia Mówika
english.UninstallShortcut=Uninstall Mówik
polish.UninstallShortcut=Odinstaluj Mówika
english.LaunchApp=Launch Mówik
polish.LaunchApp=Uruchom Mówika

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIconTask}"; GroupDescription: "{cm:AdditionalShortcutsGroup}"; Flags: unchecked
Name: "autostart"; Description: "{cm:AutoStartTask}"; GroupDescription: "{cm:StartupGroup}"; Flags: unchecked

[Files]
Source: "dist\Mowik\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{userstartup}\Mowik.lnk"
Type: files; Name: "{userstartup}\Mówik.lnk"

[Icons]
Name: "{autoprograms}\Mówik\Mówik"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Comment: "{cm:LocalDictationComment}"
Name: "{autoprograms}\Mówik\{cm:SettingsShortcut}"; Filename: "{app}\{#MyAppExeName}"; Parameters: "--settings"; WorkingDir: "{app}"; Comment: "{cm:SettingsComment}"
Name: "{autoprograms}\Mówik\{cm:UninstallShortcut}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Mówik"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Mowik"; ValueData: """{app}\{#MyAppExeName}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent

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
