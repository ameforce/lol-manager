from enum import Enum


class ErrorCode(Enum):
    REGISTRY_READ_ERROR             = 1
    REGEX_MATCH_ERROR               = 2

    EXECUTION_ERROR                 = 11

    WINDOW_NOT_FOUND_ERROR          = 21
    WINDOW_HANDLE_NOT_INITIALIZED   = 22


class BaseError(Exception):
    def __init__(self, message: str, code: ErrorCode) -> None:
        super().__init__(message)
        self.code = code
        return


class RegistryReadError(BaseError):
    def __init__(self, message: str) -> None:
        super().__init__(message, ErrorCode.REGISTRY_READ_ERROR)
        return


class RegexMatchError(BaseError):
    def __init__(self, message: str) -> None:
        super().__init__(message, ErrorCode.REGEX_MATCH_ERROR)
        return


class ExecutionError(BaseError):
    def __init__(self, message: str) -> None:
        super().__init__(message, ErrorCode.EXECUTION_ERROR)
        return

class WindowNotFoundError(BaseError):
    def __init__(self, message: str) -> None:
        super().__init__(message, ErrorCode.WINDOW_NOT_FOUND_ERROR)
        return

class WindowHandleNotInitialized(BaseError):
    def __init__(self, message: str) -> None:
        super().__init__(message, ErrorCode.WINDOW_HANDLE_NOT_INITIALIZED)
        return
