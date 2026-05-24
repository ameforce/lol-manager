## LOLManager

League of Legends 클라이언트(Windows) 자동화 도구입니다.
클라이언트 창 **가로 폭(width)** 에 맞춰 템플릿 세트를 자동 선택해 버튼/텍스트를 이미지 매칭으로 탐지합니다.

- 주요 흐름(템플릿 기반): 대전 찾기/수락 → 챔피언 선택(픽/밴) → 게임 종료 후 다음 큐

### 요구사항

- Windows 환경(LoL 클라이언트 + `pywinauto` 기반)
- Python **3.14+**
- `uv`(권장)

### 설치

프로젝트 루트에서 한 번만 실행:

```bat
uv sync
```

### 실행 (GUI, 권장)

GUI(시작/중지/로그/설정)를 실행합니다.

```bat
uv run lolmanager
```

또는(프로젝트 루트 배치, 동일 동작):

```bat
run_uv_main.bat
```

또는(직접 실행):

```bat
uv run python -m lolmanager
```

> 참고: `lolmanager`는 **인자가 하나라도 있으면 CLI로 동작**합니다.  
> 예) `uv run lolmanager --help` 는 GUI가 아니라 CLI 도움말을 출력합니다.

### 실행 (CLI)

```bat
uv run lolmanager-cli --help
```

- `--debug`: DEBUG 로그(템플릿 매칭/ROI 등) 출력
- `--config-gui`: 설정 GUI를 열고 종료(편집만 하고 메인 자동화는 실행하지 않음)

또는(동일 동작):

```bat
uv run lolmanager --cli
```

### 설정 (champion/ban/pick_coord/reserve_picks)

설정 편집기는 아래 중 하나로 실행합니다.

```bat
uv run lolmanager-config
```

또는:

```bat
uv run python -m lolmanager.gui.config_gui
```

설정 파일 기본 경로:

- `%APPDATA%\LOLManager\champion_config.json`
- (예외) `%APPDATA%`가 없으면 `%LOCALAPPDATA%\LOLManager\champion_config.json`

첫 실행 시에는 아래 중 하나에 `champion_config.json`이 있으면 **자동으로 기본 경로로 마이그레이션**합니다.

- 현재 작업 디렉터리
- exe 폴더(또는 `_internal` 하위)
- 프로젝트 루트(개발 모드)

설정 구조(요약):

- 각 role(`top/jungle/mid/adc/support`)에 대해
  - `champion`(필수): 픽 대상 챔피언 이름
  - `ban`(선택): 밴 대상 챔피언 이름
  - `pick_coord`(선택): `[x, y]` 픽 좌표(비우면 기본값 사용)
  - `reserve_picks`(선택): 예비 픽(최대 2개를 사용)

### GUI 창 모드/스냅 동작(환경 변수)

- `LOLMANAGER_FRAMELESS`
  - 기본값: `True`(프레임리스 ON)
  - OFF 값: `0/false/no/n/off`
  - ON 값: `1/true/yes/y/on`
- `LOLMANAGER_SNAP_GAP`
  - 프레임리스 모드에서 스냅 간격(px), 범위: -50 ~ 200
- `LOLMANAGER_SNAP_TOL_PX`
  - 스냅 허용 오차(px), 범위: 0 ~ 50

예) 프레임리스 끄기:

```bat
set LOLMANAGER_FRAMELESS=0
uv run lolmanager
```

### exe 빌드 (PyInstaller, 콘솔 창 없음)

```bat
scripts\build_exe.bat
```

- 결과물: 프로젝트 루트의 `LOLManager.exe` (원본 산출물 `dist\LOLManager.exe` → 루트로 이동)
- 로그: `logs\build_exe_last.log`
- 빌드 완료 후: `scripts\uninstall_shortcuts.bat` → `scripts\install_shortcuts.bat` 자동 실행(바로가기 갱신)

### 바로가기 설치/제거

바로가기 설치(바탕화면 + 시작 메뉴):

```bat
scripts\install_shortcuts.bat
```

> 참고: `scripts\install_shortcuts.bat`는 `LOLManager.exe`가 필요합니다. 없으면 먼저 `scripts\build_exe.bat`를 실행하세요.

바로가기 제거:

```bat
scripts\uninstall_shortcuts.bat
```

작업 표시줄 고정은 Windows 정책/버전에 따라 자동화가 막힐 수 있어, 필요 시 **시작 메뉴에서 수동으로 고정**하세요.

### 문제 해결

- op.gg 파싱이 막혀 Playwright 경로로 넘어가는데 브라우저가 없을 때:

```bat
uv run python -m playwright install chromium
```

- `tkinter` import 실패(설정 GUI 실행 불가):
  - Tk 지원이 포함된 Python 배포판이 필요합니다.

- `images 하위에 사용할 해상도 폴더가 없습니다`:
  - `src\lolmanager\resources\images\1280\...` 같은 템플릿 폴더가 존재하는지 확인하세요.
