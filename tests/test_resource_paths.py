from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lolmanager.platform import paths
from lolmanager.platform.resolution_detector import select_image_set


@pytest.fixture(autouse=True)
def clear_resource_cache():
    paths.resource_root.cache_clear()
    yield
    paths.resource_root.cache_clear()


def test_source_resources_are_found_from_an_unrelated_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = paths.resource_root()
    assert root == Path(paths.__file__).resolve().parents[1] / "resources"
    assert paths.resource_path("assets", "lolmanager.ico").is_file()
    assert json.loads(paths.resource_path("defaults", "champion_config.json").read_text("utf-8"))
    image_set = select_image_set(1280)
    assert image_set is not None
    assert (image_set / "lobby_find-match-button.png").is_file()


def test_installed_package_resources_do_not_require_a_checkout(tmp_path):
    package = tmp_path / "site-packages" / "lolmanager"
    shutil.copytree(Path(paths.__file__).resolve().parents[1], package)
    script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from lolmanager.platform.paths import resource_path
assert resource_path('assets', 'lolmanager.ico').is_file()
assert resource_path('images', '1280', 'lobby_find-match-button.png').is_file()
assert resource_path('defaults', 'champion_config.json').is_file()
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(package.parent)],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_frozen_resources_use_the_extracted_bundle(tmp_path, monkeypatch):
    bundled = tmp_path / "lolmanager" / "resources"
    bundled.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.resource_root() == bundled
