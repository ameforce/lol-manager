from src.ErrorCode import RegistryReadError, ExecutionError, RegexMatchError

import subprocess
import winreg
import re


class Runner:
    def __init__(self) -> None:
        self.__registry_paths = {
            'opgg': (winreg.HKEY_CLASSES_ROOT, r'opgg\shell\open\command'),
            'lol':  (winreg.HKEY_CLASSES_ROOT, r'riotclient\shell\open\command')
        }

        self.__exe_paths = {
            'opgg': None,
            'lol': None
        }

        self.get_path_from_reg(['opgg', 'lol'])
        return

    def get_path_from_reg(self, targets: list) -> None:
        for target in targets:
            try:
                hive, subkey = self.__registry_paths[target]
                registry_key = winreg.OpenKey(hive, subkey)
                value, reg_type = winreg.QueryValueEx(registry_key, '')
                winreg.CloseKey(registry_key)
            except OSError as error:
                raise RegistryReadError(f'레지스트리 값을 읽는 중 오류가 발생했습니다: {error}')

            match = re.search(r'"([^"]+)"', value)
            if not match:
                raise RegexMatchError('정규식이 일치하지 않는 상태')
            self.__exe_paths[target] = match.group(1)
        return

    def run(self) -> None:
        for key in self.__exe_paths.keys():
            if self.__exe_paths[key] is None:
                raise ExecutionError(f'실행해야 하는 경로가 초기화 되지 않았습니다: {key}')
            subprocess.Popen([self.__exe_paths[key], '--launch-product=league_of_legends', '--launch-patchline=live'])
        return
