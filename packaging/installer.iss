; Inno Setup 6 script for the CS2 Highlights installer (CS2Highlights-Setup-<version>.exe).
;
; Build on Windows from the repository root, after the PyInstaller build:
;   iscc /DAppVersion=0.2.0 /DStagingDir=C:\path\to\ffmpeg-folder packaging\installer.iss
;
; Defines (relative paths are relative to this file's folder):
;   AppVersion   required, the version in cs2hl/__init__.py
;   StagingDir   required, folder with ffmpeg.exe, ffprobe.exe, FFmpeg-LICENSE.txt and
;                FFmpeg-README.txt (the LICENSE and README.txt of the same FFmpeg build)
;   FileVersion  numeric x.y.z[.w] for the setup exe's file properties (default AppVersion)
;   DistDir      PyInstaller output folder (default ..\dist\CS2Highlights)
;   OutputDir    where the setup exe is written (default ..\dist)
;
; HLAE and Counter-Strike 2 are not bundled; the finish page says so.

#if Ver < EncodeVer(6, 3, 0)
  #error Inno Setup 6.3 or later is needed (x64compatible architecture identifier)
#endif
#ifndef AppVersion
  #error Pass the app version: iscc /DAppVersion=x.y.z packaging\installer.iss
#endif
#ifndef StagingDir
  #error Pass the folder holding ffmpeg.exe, ffprobe.exe and their license: iscc /DStagingDir=...
#endif
#ifndef FileVersion
  #define FileVersion AppVersion
#endif
#ifndef DistDir
  #define DistDir "..\dist\CS2Highlights"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

#define AppName "CS2 Highlights"
#define AppExe "CS2Highlights.exe"
#define DemoProgId "CS2Highlights.Demo"
#define DemoVerb "CS2Highlights.Open"

[Setup]
; Never change AppId: upgrades and the uninstaller find the installed copy by it.
AppId={{11622B39-0617-4ACD-A6AF-7981CA60466C}
AppName={#AppName}
AppVersion={#AppVersion}
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
VersionInfoVersion={#FileVersion}
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
; Per-user install into %LOCALAPPDATA%\Programs without admin rights; the first page offers
; "install for all users" (Program Files, needs admin) as well.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; The app is a 64-bit Python build; Qt 6 needs Windows 10 or later.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
ChangesAssociations=yes
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
OutputDir={#OutputDir}
OutputBaseFilename=CS2Highlights-Setup-{#AppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "assocdem"; Description: "Add ""Open with {#AppName}"" to .dem demo files"; GroupDescription: "Demo files:"; Flags: unchecked

[InstallDelete]
; An upgrade replaces the whole runtime folder, so no files from an older build linger.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The app looks for ffmpeg.exe and ffprobe.exe next to its exe.
Source: "{#StagingDir}\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StagingDir}\ffprobe.exe"; DestDir: "{app}"; Flags: ignoreversion
; FFmpeg is GPL v3: its license and build notes (with the source link) ship with it.
Source: "{#StagingDir}\FFmpeg-LICENSE.txt"; DestDir: "{app}\licenses"; Flags: ignoreversion
Source: "{#StagingDir}\FFmpeg-README.txt"; DestDir: "{app}\licenses"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; .dem files get CS2 Highlights in "Open with" and an "Open with CS2 Highlights" right-click
; entry (SystemFileAssociations verbs show whatever app owns .dem). Double-click stays with the
; current default app, such as CS Demo Manager. Parent keys are listed first so the uninstaller,
; which works backwards, removes them last and only when empty.
Root: HKA; Subkey: "Software\Classes\.dem"; Flags: uninsdeletekeyifempty; Tasks: assocdem
Root: HKA; Subkey: "Software\Classes\.dem\OpenWithProgids"; ValueType: string; ValueName: "{#DemoProgId}"; ValueData: ""; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: assocdem
Root: HKA; Subkey: "Software\Classes\{#DemoProgId}"; ValueType: string; ValueName: ""; ValueData: "CS2 demo"; Flags: uninsdeletekey; Tasks: assocdem
Root: HKA; Subkey: "Software\Classes\{#DemoProgId}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExe},0"; Tasks: assocdem
Root: HKA; Subkey: "Software\Classes\{#DemoProgId}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: assocdem
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.dem"; Flags: uninsdeletekeyifempty; Tasks: assocdem
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.dem\shell"; Flags: uninsdeletekeyifempty; Tasks: assocdem
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.dem\shell\{#DemoVerb}"; ValueType: string; ValueName: ""; ValueData: "Open with {#AppName}"; Flags: uninsdeletekey; Tasks: assocdem
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.dem\shell\{#DemoVerb}"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#AppExe},0"; Tasks: assocdem
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.dem\shell\{#DemoVerb}\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: assocdem

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Runs after the installed files are gone, in this order. Anything the user put in {app}
; themselves (an HLAE folder, for example) is kept, as Inno Setup advises; {app} goes when empty.
Type: filesandordirs; Name: "{app}\_internal"
Type: dirifempty; Name: "{app}"

[Messages]
FinishedLabelNoIcons=Setup has finished installing [name] on your computer.%n%nRecording needs Counter-Strike 2 and HLAE (advancedfx.org), which are not included. The app finds HLAE in its usual folders, or you can choose its location in the app.
FinishedLabel=Setup has finished installing [name] on your computer.%n%nRecording needs Counter-Strike 2 and HLAE (advancedfx.org), which are not included. The app finds HLAE in its usual folders, or you can choose its location in the app.
