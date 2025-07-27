"""자동으로 대전을 수락하는 기능을 제공한다."""

import logging
import time
from pathlib import Path
from typing import Tuple

import pyautogui
from screeninfo import get_monitors


class AutoPicker:
    """이미지 서치를 이용해 매칭을 자동으로 수락하는 클래스."""

    def __init__(self, wait_seconds: int = 30) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        self.wait_seconds = wait_seconds
        self.assets_dir = Path("assets")
        self.find_match_img: Path | None = None
        self.ready_img: Path | None = None

    def _select_assets(self, width: int, height: int) -> None:
        """해상도에 맞는 이미지 리소스를 설정한다."""
        resolution_dir = self.assets_dir / f"{width}x{height}"
        find_match = resolution_dir / "find_match.png"
        ready = resolution_dir / "ready.png"

        if not find_match.exists() or not ready.exists():
            self.logger.warning(
                "해상도별 템플릿을 찾지 못했습니다: %s", resolution_dir
            )
            find_match = self.assets_dir / "find_match.png"
            ready = self.assets_dir / "ready.png"

        self.find_match_img = find_match
        self.ready_img = ready

    @staticmethod
    def _imagesearch(
        path: Path,
        region: Tuple[int, int, int, int] | None = None,
        confidence: float = 0.8,
    ) -> Tuple[int, int] | None:
        """화면에서 이미지를 찾는다."""
        location = pyautogui.locateCenterOnScreen(
            str(path), confidence=confidence, region=region
        )
        if location:
            return int(location.x), int(location.y)
        return None

    @staticmethod
    def _find_league_window() -> Tuple[pyautogui.Window, int, Tuple[int, int, int, int]]:
        """리그 오브 레전드 창의 위치와 모니터를 찾는다."""
        windows = pyautogui.getWindowsWithTitle("League of Legends")
        if not windows:
            raise RuntimeError("League of Legends window not found")

        window = windows[0]
        region = (window.left, window.top, window.width, window.height)

        center_x = window.left + window.width // 2
        center_y = window.top + window.height // 2

        monitor_index = 0
        for idx, monitor in enumerate(get_monitors()):
            if monitor.x <= center_x < monitor.x + monitor.width and monitor.y <= center_y < monitor.y + monitor.height:
                monitor_index = idx
                break

        return window, monitor_index, region

    def _wait_for_match(self, region: Tuple[int, int, int, int]) -> bool:
        """지정된 시간 동안 '준비완료' 버튼을 기다린다."""
        start = time.time()
        while time.time() - start < self.wait_seconds:
            pos = self._imagesearch(self.ready_img, region=region)
            if pos:
                pyautogui.click(pos)
                return True
            time.sleep(1)
        return False

    def run(self) -> bool:
        """자동 매칭 수락을 실행한다."""
        try:
            window, monitor_idx, region = self._find_league_window()
            self.logger.info(
                "League window found on monitor %d at %s",
                monitor_idx,
                region,
            )
        except RuntimeError as exc:
            self.logger.error(exc)
            return False

        self._select_assets(window.width, window.height)
        if self.find_match_img is None or self.ready_img is None:
            self.logger.error("이미지 리소스가 설정되지 않았습니다")
            return False
        if not self.find_match_img.exists() or not self.ready_img.exists():
            self.logger.error("Template images not found in assets directory")
            return False

        pos = self._imagesearch(self.find_match_img, region=region)
        if not pos:
            self.logger.warning("'Find Match' button not found")
            return False

        pyautogui.click(pos)
        self.logger.info("Clicked 'Find Match' button, waiting for match")

        if self._wait_for_match(region):
            self.logger.info("Accepted match")
            return True

        self.logger.warning("Match not found within timeout")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    AutoPicker().run()
