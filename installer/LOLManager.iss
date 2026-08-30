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
CloseApplications=no
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
Source: "installer-managed.marker"; DestDir: "{app}"; DestName: ".lolmanager-installer-managed"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\{#MyAppName}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{#MyAppName} 실행"; Flags: nowait postinstall skipifsilent; Check: not IsUpdaterInstallMode
Filename: "{app}\{#MyAppExeName}"; Flags: nowait skipifnotsilent; Check: IsUpdaterInstallMode

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"

[Code]
const
  SYNCHRONIZE = $00100000;
  WAIT_OBJECT_0 = $00000000;
  WAIT_TIMEOUT = $00000102;

function OpenProcess(dwDesiredAccess: LongWord; bInheritHandle: Boolean; dwProcessId: LongWord): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(hHandle: THandle; dwMilliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function HasCommandLineSwitch(const Expected: String): Boolean;
var
  Index: Integer;
begin
  Result := False;
  for Index := 1 to ParamCount do
  begin
    if CompareText(ParamStr(Index), Expected) = 0 then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

function UpdateCommandLineValue(const Name: String): String;
var
  Index: Integer;
  Parameter: String;
  Prefix: String;
begin
  Result := '';
  Prefix := '/' + Name + '=';
  for Index := 1 to ParamCount do
  begin
    Parameter := ParamStr(Index);
    if CompareText(Copy(Parameter, 1, Length(Prefix)), Prefix) = 0 then
    begin
      Result := Copy(Parameter, Length(Prefix) + 1, Length(Parameter));
      Exit;
    end;
  end;
end;

function IsUpdaterInstallMode(): Boolean;
begin
  Result := HasCommandLineSwitch('/LOLMANAGERUPDATEMODE');
end;

function UpdateBootstrapWaitPid(): Integer;
var
  RawPid: String;
begin
  Result := 0;
  RawPid := UpdateCommandLineValue('LOLMANAGERWAITPID');
  if RawPid <> '' then
    Result := StrToIntDef(RawPid, 0);
end;

function WaitForUpdateBootstrapExit(): String;
var
  ProcessId: Integer;
  ProcessHandle: THandle;
  WaitResult: LongWord;
begin
  Result := '';
  ProcessId := UpdateBootstrapWaitPid();
  if ProcessId <= 0 then
  begin
    Result := '업데이트 모드의 원본 LOLManager PID가 올바르지 않습니다.';
    Exit;
  end;

  ProcessHandle := OpenProcess(SYNCHRONIZE, False, ProcessId);
  if ProcessHandle = 0 then
    Exit;
  try
    WaitResult := WaitForSingleObject(ProcessHandle, 60000);
    if WaitResult = WAIT_TIMEOUT then
      Result := '원본 LOLManager 종료 대기 시간이 초과되었습니다.'
    else if WaitResult <> WAIT_OBJECT_0 then
      Result := '원본 LOLManager 종료 상태를 확인하지 못했습니다.';
  finally
    CloseHandle(ProcessHandle);
  end;
end;

procedure WriteUpdateSuccessResult();
var
  ResultPath: String;
  TargetVersion: String;
  Payload: String;
begin
  if not IsUpdaterInstallMode() then
    Exit;
  ResultPath := UpdateCommandLineValue('LOLMANAGERRESULT');
  TargetVersion := UpdateCommandLineValue('LOLMANAGERTARGETVERSION');
  if (ResultPath = '') or (TargetVersion = '') then
    Exit;
  Payload := '{"schema_version":1,"status":"success","target_version":"' +
    TargetVersion +
    '","message":"silent installer completed.","recorded_at_unix":0}' + #13#10;
  if not SaveStringToFile(ResultPath, Payload, False) then
    Log('업데이트 성공 결과를 저장하지 못했습니다: ' + ResultPath);
end;

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
  if IsUpdaterInstallMode() then
  begin
    Result := WaitForUpdateBootstrapExit();
    Exit;
  end;
  Result := StopRunningLOLManager();
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    WriteUpdateSuccessResult();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopRunningLOLManager();
end;
