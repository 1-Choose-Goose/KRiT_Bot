#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #error SourceDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif

#define AppName "КРиТ · управление"

[Setup]
AppId={{8C863116-A6F1-49D8-9627-0FE918B45BD4}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=ЦДО «КРиТ»
DefaultDirName={localappdata}\Programs\KRiTManagement
DefaultGroupName={#AppName}
UsePreviousAppDir=yes
DisableDirPage=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=SetupKrit
SetupIconFile={#SourceDir}\_internal\krit_management\assets\app_icon.ico
WizardImageFile={#SourceDir}\_internal\krit_management\assets\logo_full_1024.png
WizardSmallImageFile={#SourceDir}\_internal\krit_management\assets\app_icon_256.png
WizardImageBackColor=$FFFFFF
WizardSmallImageBackColor=$FFFFFF
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\KRiTManagement.exe
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\KRiTManagement.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\KRiTManagement.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\KRiTManagement.exe"; WorkingDir: "{app}"; Description: "Запустить {#AppName}"; Flags: nowait postinstall skipifsilent
