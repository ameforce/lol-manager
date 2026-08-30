from __future__ import annotations

from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import re
import sys
from pathlib import Path
from typing import Optional

from lolmanager.platform.paths import project_root, user_data_dir
from lolmanager.platform.runtime import is_frozen


LOG_FORMAT = "[%(asctime)s] [%(levelname)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

_RUNTIME_HANDLER_MARKER = "_lolmanager_runtime_log_handler"
_SECRET_FIELD_NAME = (
    r"[A-Za-z0-9_.-]*"
    r"(?:secret|token|password|jwt|authorization[-_]?key|auth[-_]?key)"
    r"[A-Za-z0-9_.-]*"
)
_QUOTED_SECRET_FIELD_VALUE = re.compile(
    rf"((?:\\?\"|')\s*{_SECRET_FIELD_NAME}\s*(?:\\?\"|')\s*[:=]\s*(?:\\?\"|'))"
    r"([^\\\"'\r\n]*)"
    r"((?:\\?\"|'))",
    re.IGNORECASE,
)
_UNQUOTED_SECRET_FIELD_VALUE = re.compile(
    rf"(\b{_SECRET_FIELD_NAME}\b\s*[:=]\s*)(?!\\?\"|')([^\s,;&\]\}}]+)",
    re.IGNORECASE,
)
_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(--(?:riotclient-|remoting-)?auth-token=)[^\s\"]+"), r"\1<redacted>"),
    (re.compile(r"(Authorization:\s*Basic\s+)[A-Za-z0-9+/=]+", re.IGNORECASE), r"\1<redacted>"),
    (re.compile(r"eyJ[A-Za-z0-9_\-.]{20,}"), "<redacted-jwt>"),
)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return redact_sensitive(rendered)


def redact_sensitive(value: object) -> str:
    text = str(value)
    text = _QUOTED_SECRET_FIELD_VALUE.sub(r"\1<redacted>\3", text)
    text = _UNQUOTED_SECRET_FIELD_VALUE.sub(r"\1<redacted>", text)
    for pattern, repl in _SENSITIVE_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def runtime_log_dir() -> Path:
    if is_frozen():
        return user_data_dir() / "logs"
    return project_root() / "logs"


def runtime_log_path(log_dir: Optional[Path] = None, *, now: Optional[datetime] = None) -> Path:
    base = Path(log_dir) if log_dir is not None else runtime_log_dir()
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return base / f"runtime-{stamp}.log"


def _remove_previous_runtime_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if bool(getattr(handler, _RUNTIME_HANDLER_MARKER, False)):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


def configure_runtime_logging(
    *,
    debug: bool = False,
    log_dir: Optional[Path] = None,
    logger_name: Optional[str] = None,
) -> Path:
    path = runtime_log_path(log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if debug else logging.INFO
    formatter = RedactingFormatter(LOG_FORMAT, LOG_DATE_FORMAT)
    logger = logging.getLogger(logger_name) if logger_name else logging.getLogger()
    logger.setLevel(level)

    if not logger.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    for handler in logger.handlers:
        handler.setLevel(level)
        if handler.formatter is None:
            handler.setFormatter(formatter)

    _remove_previous_runtime_handlers(logger)
    file_handler = RotatingFileHandler(
        path,
        maxBytes=DEFAULT_MAX_BYTES,
        backupCount=DEFAULT_BACKUP_COUNT,
        encoding="utf-8",
    )
    setattr(file_handler, _RUNTIME_HANDLER_MARKER, True)
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return path


def install_exception_logger(logger_name: str = "lolmanager") -> None:
    previous_hook = sys.excepthook

    def _hook(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            logging.getLogger(logger_name).critical(
                "처리되지 않은 예외로 CLI가 종료됩니다.",
                exc_info=(exc_type, exc, tb),
            )
        previous_hook(exc_type, exc, tb)

    sys.excepthook = _hook
