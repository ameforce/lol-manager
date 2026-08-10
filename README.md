<p align="center">
  <strong>LOLManager</strong><br>
  League of Legends 클라이언트 자동 픽/밴 보조 도구
</p>

<p align="center">
  <img alt="Windows" src="https://img.shields.io/badge/Windows-only-0078D4">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.14%2B-3776AB">
  <img alt="uv" src="https://img.shields.io/badge/uv-ready-4B32C3">
</p>

## 소개

LOLManager는 Windows LoL 클라이언트를 감지해 대전 수락, 챔피언 픽, 밴, 게임 종료 후 다음 큐 흐름을 자동화합니다.

OP.GG 데이터를 주기적으로 캐싱해 내가 픽하려는 챔피언 기준 추천 밴을 계산하고, 설정 GUI에서 티어, 상대 승률, score를 함께 확인할 수 있습니다. `자동 추천 (최고 score)`를 선택하면 밴 단계마다 캐시의 최고 추천 챔피언명만 입력합니다.

## 주요 기능

- LoL 클라이언트 상태 감지 및 대전 수락 자동화
- 포지션별 기본 픽, 밴, 예비 픽 최대 2개 설정
- OP.GG 기반 추천 밴 정렬: 티어 점수 + 낮은 상대 승률 점수
- 설정 GUI 자동 저장 및 오래된 추천 캐시 자동 갱신
- PyInstaller 기반 단일 `LOLManager.exe` 빌드

## 빠른 시작

요구사항:

- Windows
- Python 3.14+
- `uv`
- LoL 클라이언트

```bat
uv sync
uv run lolmanager
```

메인 GUI의 `Start`와 `Stop`은 매칭 자동화 프로세스만 제어합니다. LoL
클라이언트에 따른 창 위치, 표시/숨김, topmost, 종료 감시는 LOLManager GUI가
열려 있는 동안 계속 동작합니다. 인게임 중 자동화를 중지해도 GUI는 게임이
끝날 때까지 숨김 상태를 유지하고, LoL 클라이언트가 다시 표시되면 자동으로
복원되어 클라이언트 옆에 배치됩니다.

설정만 열려면:

```bat
uv run lolmanager-config
```

CLI 도움말:

```bat
uv run lolmanager-cli --help
```

## 개발 검증

테스트는 프로젝트 dev 의존성을 포함해 실행합니다.

```bat
uv run --group dev python -m pytest -q
```

## 설정

설정 GUI에서 포지션별로 아래 값을 고릅니다. 변경 내용은 사용자 액션마다 자동 저장됩니다.

| 항목 | 설명 |
| --- | --- |
| `champion` | 기본으로 픽할 챔피언 |
| `ban` | 직접 지정 밴 또는 `자동 추천 (최고 score)` |
| `pick_coord` | 챔피언 선택 좌표. 비우면 기본값 사용 |
| `reserve_picks` | 기본 픽이 막혔을 때 사용할 예비 픽 최대 2개 |

설정 파일:

```text
%APPDATA%\LOLManager\champion_config.json
```

OP.GG 추천 캐시:

```text
%APPDATA%\LOLManager\opgg_counter_recommendation_cache.json
```

환경 변수:

| 이름 | 기본값 | 설명 |
| --- | --- | --- |
| `LOLMANAGER_CHAMPSELECT_ACTION_CONFIRM_TIMEOUT_SEC` | `2.0` | LCU 픽/밴 선택 및 완료 확인 대기 시간(초) |
| `LOLMANAGER_LEAGUE_CLIENT_EXE` | `C:\Riot Games\League of Legends\LeagueClient.exe` | LeagueClient 실행 파일 경로 |
| `LOLMANAGER_RIOT_CLIENT_SERVICES_EXE` | 자동 탐지 | RiotClientServices 실행 파일 경로 |
| `LOLMANAGER_OPGG_EXE` | 자동 탐지 | OP.GG 실행 파일 경로. `%LOCALAPPDATA%\Programs\OP.GG\OP.GG.exe` 등을 먼저 찾습니다. |
| `LOLMANAGER_ALLOW_UNTRUSTED_APP_PATHS` | 비활성 | `LOLMANAGER_OPGG_EXE`가 표준 설치 위치 밖의 `OP.GG.exe`를 가리킬 때 `1`로 명시 허용 |

## EXE 빌드

```bat
scripts\build_exe.bat
```

빌드 결과:

- `LOLManager.exe`
- `logs\build_exe_last.log`

빌드가 끝나면 바탕화면과 시작 메뉴 바로가기를 갱신합니다.

## 릴리스 빌드

portable EXE, per-user installer, SHA-256 목록을 한 번에 생성합니다. installer 빌드에는 Inno Setup 6이 필요합니다.

```bat
winget install --exact --id JRSoftware.InnoSetup
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_release.ps1
```

`dist\release` 결과:

- `LOLManager-vX.Y.Z.exe`
- `LOLManager-Setup-vX.Y.Z.exe`
- `SHA256SUMS.txt`

installer는 `%LOCALAPPDATA%\Programs\LOLManager`에 사용자 권한으로 설치합니다. 시작 메뉴 바로가기는 항상 만들고, 바탕 화면 바로가기는 기본 선택 항목입니다. 업그레이드와 제거 과정에서 `%APPDATA%\LOLManager`의 설정과 캐시는 삭제하지 않습니다.

릴리스 바이너리는 코드 서명되지 않았으므로 Windows SmartScreen 경고가 표시될 수 있습니다. 다운로드한 파일은 함께 제공되는 `SHA256SUMS.txt`로 검증하세요.

## 바로가기

```bat
scripts\install_shortcuts.bat
scripts\uninstall_shortcuts.bat
```

`install_shortcuts.bat`는 프로젝트 루트의 `LOLManager.exe`가 필요합니다.

## 문제 해결

OP.GG fallback 브라우저가 필요하다는 오류가 나면 Chromium을 설치합니다.

```bat
uv run python -m playwright install chromium
```

`tkinter` import 오류가 나면 Tk 지원이 포함된 Python 배포판을 사용해야 합니다.

이미지 템플릿 오류가 나면 아래 리소스 폴더가 포함되어 있는지 확인합니다.

```text
src\lolmanager\resources\images\1280
```
