from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("workflow", ["ci.yml", "release.yml"])
@pytest.mark.parametrize("step", ["Validate lockfile", "Install locked dependencies", "Test source"])
def test_validation_failure_propagates_to_the_workflow_step(workflow, step):
    text = (ROOT / ".github" / "workflows" / workflow).read_text("utf-8")
    block = next(block for block in text.split("- name: ") if block.startswith(step + "\n"))
    # Each validation command must be its own step, so a later native command
    # cannot overwrite its exit status before GitHub's PowerShell footer runs.
    command = re.search(r"^\s+run: (uv .+)$", block, re.MULTILINE)
    assert command is not None
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    python = str(sys.executable).replace("'", "''")
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"function uv {{ & '{python}' -c 'import sys; sys.exit(29)' }}\n"
        + command.group(1)
        + "\nif (Test-Path -LiteralPath variable:\\LASTEXITCODE) { exit $LASTEXITCODE }\n"
    )
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, timeout=15)
    assert result.returncode == 29, result.stderr
