from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lolmanager.core.runtime_logging import configure_runtime_logging


class RuntimeLoggingTests(unittest.TestCase):
    def test_runtime_log_file_redacts_sensitive_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger_name = "lolmanager-test-runtime-log"
            logger = logging.getLogger(logger_name)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
            logger.addHandler(logging.NullHandler())
            old_propagate = logger.propagate
            logger.propagate = False

            path = configure_runtime_logging(
                debug=True, log_dir=Path(tmp), logger_name=logger_name
            )
            try:
                logger.info(
                    "Authorization: Basic abc123 --riotclient-auth-token=secret "
                    "eyJaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                )
                for handler in logger.handlers:
                    handler.flush()

                text = path.read_text(encoding="utf-8")
            finally:
                for handler in list(logger.handlers):
                    logger.removeHandler(handler)
                    handler.close()
                logger.propagate = old_propagate

        self.assertIn("Authorization: Basic <redacted>", text)
        self.assertIn("--riotclient-auth-token=<redacted>", text)
        self.assertIn("<redacted-jwt>", text)
        self.assertNotIn("abc123", text)
        self.assertNotIn("secret", text)


if __name__ == "__main__":
    unittest.main()
