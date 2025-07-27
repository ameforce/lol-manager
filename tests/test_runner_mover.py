import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from src.ErrorCode import RegistryReadError, RegexMatchError, WindowNotFoundError, WindowHandleNotInitialized


class DummyApp:
    def __init__(self, hwnd, title=""):
        self.hwnd = hwnd
        self._title = title

    def move(self, desktop):
        pass


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        # create stub winreg module
        self.winreg_stub = types.SimpleNamespace(HKEY_CLASSES_ROOT=1)
        sys.modules['winreg'] = self.winreg_stub
        from src import Runner as runner_module  # import after inserting stub
        self.winreg_patcher = patch.object(runner_module, 'winreg', self.winreg_stub)
        self.winreg_patcher.start()
        self.Runner = runner_module.Runner

    def tearDown(self):
        self.winreg_patcher.stop()
        del sys.modules['winreg']

    def test_missing_registry_entry_raises_error(self):
        self.winreg_stub.OpenKey = MagicMock(side_effect=OSError('missing'))
        runner = self.Runner.__new__(self.Runner)
        runner._Runner__registry_paths = {'opgg': (1, 'dummy')}
        runner._Runner__exe_paths = {'opgg': None}
        with self.assertRaises(RegistryReadError):
            runner.get_path_from_reg(['opgg'])

    def test_bad_value_raises_regex_error(self):
        self.winreg_stub.OpenKey = MagicMock(return_value='key')
        self.winreg_stub.QueryValueEx = MagicMock(return_value=('bad', None))
        self.winreg_stub.CloseKey = MagicMock()
        runner = self.Runner.__new__(self.Runner)
        runner._Runner__registry_paths = {'opgg': (1, 'dummy')}
        runner._Runner__exe_paths = {'opgg': None}
        with patch('src.Runner.re.search', return_value=None):
            with self.assertRaises(RegexMatchError):
                runner.get_path_from_reg(['opgg'])


class MoverTestCase(unittest.TestCase):
    def setUp(self):
        # stub modules for pyvda and win32gui before importing Mover
        self.pyvda_stub = types.SimpleNamespace()
        self.pyvda_stub.AppView = DummyApp
        self.pyvda_stub.get_apps_by_z_order = lambda: []
        self.pyvda_stub.VirtualDesktop = lambda idx: f"desktop{idx}"
        sys.modules['pyvda'] = self.pyvda_stub

        self.win32gui_stub = types.SimpleNamespace()
        self.win32gui_stub.GetWindowText = lambda hwnd: getattr(hwnd, '_title', '')
        sys.modules['win32gui'] = self.win32gui_stub

        from src.Mover import Mover
        self.Mover = Mover

    def tearDown(self):
        del sys.modules['pyvda']
        del sys.modules['win32gui']

    def test_update_app_view_by_title_not_found(self):
        # get_apps_by_z_order returns apps without target titles
        self.pyvda_stub.get_apps_by_z_order = lambda: [DummyApp(1, 'other')]
        with self.assertRaises(WindowNotFoundError):
            self.Mover()

    def test_move_windows_handle_not_initialized(self):
        # prevent init from populating handles
        with patch.object(self.Mover, 'update_app_view_by_title', return_value=None):
            mover = self.Mover()
        with self.assertRaises(WindowHandleNotInitialized):
            mover.move_windows_to_game_desktop()


if __name__ == '__main__':
    unittest.main()
