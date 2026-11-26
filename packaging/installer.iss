; Inno Setup script for the Windows installer. Built by .github/workflows/build.yml:
;   iscc /DAppVersion=0.1.0 /DSourceDir=dist\Air-Construct packaging\installer.iss
; Owned by: workstream "packaging".

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\Air-Construct"
#endif

[Setup]
AppId={{6C1E4B0E-3F7A-4C55-9C1B-AC0A1F2E7D31}
AppName=Air-Construct
AppVersion={#AppVersion}
AppPublisher=Air-Construct contributors
AppPublisherURL=https://github.com/R3C-0N/Air-Construct
DefaultDirName={autopf}\Air-Construct
DefaultGroupName=Air-Construct
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=Air-Construct-{#AppVersion}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; per-user install by default (no admin prompt), machine-wide on request
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
UninstallDisplayIcon={app}\Air-Construct.exe

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Air-Construct"; Filename: "{app}\Air-Construct.exe"
Name: "{group}\Uninstall Air-Construct"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Air-Construct"; Filename: "{app}\Air-Construct.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Air-Construct.exe"; Description: "{cm:LaunchProgram,Air-Construct}"; Flags: nowait postinstall skipifsilent

[Code]
function WslInstalled(): Boolean;
begin
  Result := FileExists(ExpandConstant('{sys}\wsl.exe'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (not WizardSilent()) and (not WslInstalled()) then
    MsgBox('Air-Construct runs OpenFOAM inside WSL2, which was not found on this PC.' + #13#10 +
           'Install it from an administrator PowerShell with:  wsl --install -d Ubuntu' + #13#10 +
           'then install OpenFOAM inside Ubuntu (see the README).', mbInformation, MB_OK);
end;
