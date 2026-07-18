#ifndef MyAppVersion
  #error MyAppVersion must be supplied by scripts/build_release.ps1
#endif
#ifndef MyAppVersionQuad
  #error MyAppVersionQuad must be supplied by scripts/build_release.ps1
#endif
#ifndef MySourceExe
  #error MySourceExe must be supplied by scripts/build_release.ps1
#endif

#define MyAppName "LOLManager"
#define MyAppPublisher "ameforce"
#define MyAppExeName "LOLManager.exe"

[Setup]
AppId={{F1E18E34-A5B3-4DE8-8E91-74DC33D66D15}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist\release
OutputBaseFilename=LOLManager-Setup-v{#MyAppVersion}
SetupIconFile=..\src\lolmanager\resources\assets\lolmanager.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
CloseApplications=yes
RestartApplications=no
CloseApplicationsFilter={#MyAppExeName}
VersionInfoVersion={#MyAppVersionQuad}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} installer
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕 화면 바로가기 만들기"; GroupDescription: "추가 바로가기:"; Flags: checkedonce

[Files]
Source: "{#MySourceExe}"; DestDir: "{app}"; DestName: "{#MyAppExeName}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\{#MyAppName}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{#MyAppName} 실행"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"

[Code]
function StopRunningLOLManager(): String;
var
  ResultCode: Integer;
  TaskKillPath: String;
begin
  Result := '';
  TaskKillPath := ExpandConstant('{sys}\taskkill.exe');

  if not Exec(TaskKillPath, '/IM "{#MyAppExeName}"', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode) then
  begin
    Result := '{#MyAppName} 종료 명령을 실행하지 못했습니다.';
    Exit;
  end;

  if (ResultCode <> 0) and (ResultCode <> 128) then
  begin
    Result := '{#MyAppName} 종료 요청을 완료하지 못했습니다.';
    Exit;
  end;

  Sleep(2000);
  if not Exec(TaskKillPath, '/F /IM "{#MyAppExeName}"', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode) then
  begin
    Result := '{#MyAppName} 잔여 프로세스 종료 명령을 실행하지 못했습니다.';
    Exit;
  end;
  if (ResultCode <> 0) and (ResultCode <> 128) then
    Result := '{#MyAppName}을 종료할 수 없습니다. 앱을 닫고 다시 시도하세요.';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := StopRunningLOLManager();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopRunningLOLManager();
end;
