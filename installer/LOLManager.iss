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

[Icons]
Name: "{userprograms}\{#MyAppName}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{#MyAppName} 실행"; Flags: nowait postinstall skipifsilent; Check: ShouldLaunchLOLManager
Filename: "{app}\{#MyAppExeName}"; Flags: nowait skipifnotsilent; Check: ShouldLaunchLOLManager

[InstallDelete]
Type: files; Name: "{app}\.lolmanager-installer-managed"

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
function SetEnvironmentVariable(const lpName, lpValue: String): Boolean;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

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

function HasExplicitRelaunchRequest(): Boolean;
begin
  Result := CompareText(ExpandConstant('{param:LOLMANAGER_RELAUNCH|0}'), '1') = 0;
end;

function ShouldLaunchLOLManager(): Boolean;
begin
  { Interactive installs may use the post-install launch option. A silent
    install launches only when the direct updater explicitly asks for it. }
  Result := (not WizardSilent()) or HasExplicitRelaunchRequest();
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { An updater installer inherits the running onefile application's private
    PyInstaller environment. Reset it before the installation phase begins,
    which is guaranteed to precede every non-postinstall [Run] entry used by
    silent updater relaunches. }
  if (CurStep = ssInstall) and HasExplicitRelaunchRequest() then
  begin
    if not SetEnvironmentVariable('PYINSTALLER_RESET_ENVIRONMENT', '1') then
      RaiseException('업데이트 후 LOLManager 재실행 환경을 준비하지 못했습니다.');
  end;
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

function IsUpdaterInstallMode(): Boolean;
begin
  Result := HasExplicitRelaunchRequest() and (UpdateBootstrapWaitPid() > 0);
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

function RequireNoResidualLOLManagerProcess(): String;
var
  ResultCode: Integer;
  Output: TExecOutput;
  TaskListPath: String;
  Index: Integer;
begin
  Result := '';
  TaskListPath := ExpandConstant('{sys}\tasklist.exe');
  if not ExecAndCaptureOutput(
    TaskListPath,
    '/FI "IMAGENAME eq {#MyAppExeName}" /FO CSV /NH',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode,
    Output
  ) then
  begin
    Result := '{#MyAppName} 잔여 프로세스 상태를 확인하지 못했습니다.';
    Exit;
  end;
  if ResultCode <> 0 then
  begin
    Result := '{#MyAppName} 잔여 프로세스 상태 확인 명령이 실패했습니다.';
    Exit;
  end;
  if Output.Error then
  begin
    Result := '{#MyAppName} 잔여 프로세스 상태 출력을 완전히 읽지 못했습니다.';
    Exit;
  end;
  for Index := 0 to GetArrayLength(Output.StdOut) - 1 do
  begin
    if Pos('"{#MyAppExeName}"', Output.StdOut[Index]) > 0 then
    begin
      { Updater mode never terminates an unknown residual GUI, automation, or
        in-game instance. Abort safely so its ready state can be retried. }
      Result := '다른 LOLManager가 아직 실행 중입니다. 종료 후 업데이트를 다시 시도하세요.';
      Exit;
    end;
  end;
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
    if Result <> '' then
      Exit;
    { Do not call StopRunningLOLManager here. The updater must never forcibly
      terminate a residual automation or in-game process after its PID exits. }
    Result := RequireNoResidualLOLManagerProcess();
    Exit;
  end;
  Result := StopRunningLOLManager();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopRunningLOLManager();
end;
