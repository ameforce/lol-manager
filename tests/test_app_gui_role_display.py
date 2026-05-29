from __future__ import annotations

from lolmanager.core.opgg_counter_recommendations import AUTO_BAN_LABEL
from lolmanager.gui.app_gui import compact_role_ban_label_for_main_ui


def test_compact_role_ban_label_shortens_detailed_auto_recommendation() -> None:
    assert (
        compact_role_ban_label_for_main_ui(
            "자동 추천 (현재 최고: 블라디미르, 2티어, 45.8%, score 61.0)"
        )
        == "블라디미르 2T 45.8%"
    )


def test_compact_role_ban_label_keeps_fallback_and_manual_bans() -> None:
    assert compact_role_ban_label_for_main_ui(AUTO_BAN_LABEL) == "자동 추천"
    assert compact_role_ban_label_for_main_ui("제드") == "제드"
