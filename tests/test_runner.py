import sys
import types
import importlib
import unittest
from unittest import mock
from src.ErrorCode import RegexMatchError, RegistryReadError

# Helper dummy winreg module
class DummyWinReg:
    def __init__(self, value='"C:\\Program Files\\test.exe"', raise_open=False, raise_query=False):
        self.HKEY_CLASSES_ROOT = object()
        self._value = value
        self._raise_open = raise_open
        self._raise_query = raise_query
        self.opened = False

    def OpenKey(self, hive, subkey):
        if self._raise_open:
            raise OSError('open error')
        self.opened = True
        return object()

    def QueryValueEx(self, key, value_name):
        if self._raise_query:
            raise OSError('query error')
        return (self._value, None)

    def CloseKey(self, key):
        pass


def import_runner(dummy):
    with mock.patch.dict(sys.modules, {'winreg': dummy}):
        import src.Runner as runner_module
        importlib.reload(runner_module)
    return runner_module.Runner


class RunnerTestCase(unittest.TestCase):
    def create_runner(self, RunnerClass):
        with mock.patch.object(RunnerClass, '__init__', lambda self: None):
            runner = RunnerClass()
        return runner

    def test_regex_match_error(self):
        dummy = DummyWinReg(value='C:\\no_quotes.exe')
        RunnerClass = import_runner(dummy)
        runner = self.create_runner(RunnerClass)
        runner._Runner__registry_paths = {'opgg': (dummy.HKEY_CLASSES_ROOT, 'sub')}
        runner._Runner__exe_paths = {'opgg': None}
        with self.assertRaises(RegexMatchError):
            runner.get_path_from_reg(['opgg'])

    def test_registry_read_error(self):
        dummy = DummyWinReg(raise_open=True)
        RunnerClass = import_runner(dummy)
        runner = self.create_runner(RunnerClass)
        runner._Runner__registry_paths = {'opgg': (dummy.HKEY_CLASSES_ROOT, 'sub')}
        runner._Runner__exe_paths = {'opgg': None}
        with self.assertRaises(RegistryReadError):
            runner.get_path_from_reg(['opgg'])


if __name__ == '__main__':
    unittest.main()
