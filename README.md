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

설정만 열려면:

```bat
uv run lolmanager-config
```

CLI 도움말:

```bat
uv run lolmanager-cli --help
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

## EXE 빌드

```bat
scripts\build_exe.bat
```

빌드 결과:

- `LOLManager.exe`
- `logs\build_exe_last.log`

빌드가 끝나면 바탕화면과 시작 메뉴 바로가기를 갱신합니다.

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
