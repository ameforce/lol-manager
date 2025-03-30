from src.ErrorCode import WindowNotFoundError, WindowHandleNotInitialized
from pyvda import AppView, get_apps_by_z_order, VirtualDesktop

import win32gui


class Mover:
    """특정 창을 가상 데스크톱으로 이동시키는 클래스입니다."""

    def __init__(self) -> None:
        self.__window_handles = {
            'opgg-electron-app': None,
            'league of legends': None
        }
        self.update_app_view_by_title()

        self.__target_virtual_desktop = 3
        return

    def update_app_view_by_title(self) -> None:
        for target_title in self.__window_handles:
            for app in get_apps_by_z_order():
                window_title = win32gui.GetWindowText(app.hwnd)
                if target_title.lower() in window_title.lower():
                    self.__window_handles[target_title] = app
                    break
            if self.__window_handles[target_title] is None:
                raise WindowNotFoundError(f'창을 찾을 수 없습니다: {target_title}')
        return

    def move_windows_to_game_desktop(self) -> None:
        target_desktop = VirtualDesktop(self.__target_virtual_desktop)

        for target_title in self.__window_handles:
            if self.__window_handles[target_title] is None:
                raise WindowHandleNotInitialized(f'창 핸들 값이 초기화 되지 않았습니다: {target_title}')
            self.__window_handles[target_title].move(target_desktop)
        return
