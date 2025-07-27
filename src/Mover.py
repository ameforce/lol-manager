from src.ErrorCode import WindowNotFoundError, WindowHandleNotInitialized
from pyvda import AppView, get_apps_by_z_order, VirtualDesktop

import win32gui


class Mover:
    """특정 창을 가상 데스크톱으로 이동시키는 클래스입니다.

    기본적으로 모든 창을 가상 데스크톱 3번으로 이동하며,
    생성자에서 'opgg-electron-app'과 'league of legends' 창을
    탐색해 해당 창들의 핸들을 저장합니다.

    ``update_app_view_by_title`` 메서드는 현재 열린 창들의 제목을
    확인해 각 핸들을 갱신하며, 원하는 창을 찾지 못하면
    :class:`WindowNotFoundError` 예외가 발생할 수 있습니다.

    ``move_windows_to_game_desktop`` 메서드는 저장된 핸들을 사용해
    창을 가상 데스크톱 3으로 이동합니다. 이때 창 핸들이
    초기화되지 않은 경우 :class:`WindowHandleNotInitialized` 예외가
    발생합니다.
    """

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
