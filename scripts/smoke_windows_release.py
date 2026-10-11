"""Verify the GUI child's output contract using the actual windowed executable."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        parser.error("version must use X.Y.Z")
    if os.name != "nt":
        parser.error("this smoke test requires Windows")

    root = Path(__file__).resolve().parents[1]
    executable = root / "dist" / "release" / f"LOLManager-v{args.version}.exe"
    # --help exits during argument parsing, before configuration, networking,
    # external applications or any League-client interaction can run.
    result = subprocess.run(
        [str(executable), "--cli", "--help"],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=45,
        check=True,
    )
    output = result.stdout.decode("utf-8", errors="strict")
    if "usage: lolmanager-cli" not in output or "--continue-after-game" not in output:
        raise RuntimeError("Packaged CLI help is missing from the inherited pipe")
    if not any("\uac00" <= char <= "\ud7a3" for char in output):
        raise RuntimeError("Packaged CLI help lost its Korean text")
    print(f"Verified {executable.name}: CLI startup and UTF-8 Korean output")


if __name__ == "__main__":
    main()
