; Compile with /DAppVersion=<covearchive.__version__> and optional /DAppSource=<onedir>.
#ifndef AppVersion
  #error AppVersion must be passed from covearchive.__version__
#endif
#ifndef OutputDir
  #define OutputDir "dist\installer"
#endif
#ifndef AppSource
  #define AppSource "dist\CoveArchive"
#endif

[Setup]
SourceDir=..
AppId={{963CDB21-8306-4E45-B4C9-EEC4E2337AC2}
AppName=CoveArchive
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher=Cove
DefaultDirName={autopf}\CoveArchive
DefaultGroupName=CoveArchive
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=LICENSE
SetupIconFile=packaging\covearchive.ico
OutputDir={#OutputDir}
MinVersion=10.0
OutputBaseFilename=CoveArchive-{#AppVersion}-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\CoveArchive.exe
CloseApplications=yes
AllowNoIcons=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\CoveArchive"; Filename: "{app}\CoveArchive.exe"
Name: "{autodesktop}\CoveArchive"; Filename: "{app}\CoveArchive.exe"; Tasks: desktopicon
