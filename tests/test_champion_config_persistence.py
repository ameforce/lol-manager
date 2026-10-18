from __future__ import annotations

import json
from pathlib import Path

import pytest

from lolmanager.core.champion_config import ChampionConfig, ChampionConfigError


def _write_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "top": {"champion": "말파이트", "ban": "피오라"},
                "mid": {"champion": "아리", "ban": "제드"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "payload",
    [b"not-json", b"[]", b'{"top": []}', b'{"aram": null}', b"\xff"],
)
def test_invalid_config_is_reported_without_replacing_the_saved_file(
    tmp_path: Path, payload: bytes
) -> None:
    path = tmp_path / "champion_config.json"
    path.write_bytes(payload)

    with pytest.raises(ChampionConfigError):
        ChampionConfig(path)

    assert path.read_bytes() == payload


def test_stale_config_save_preserves_concurrent_changes_and_deletions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "champion_config.json"
    _write_config(path)
    first = ChampionConfig(path)
    second = ChampionConfig(path)

    first.data.pop("top")
    first.save()
    second.set("mid", "오리아나", ban_champion="르블랑")

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "top" not in saved
    assert saved["mid"] == {"champion": "오리아나", "ban": "르블랑"}
