from __future__ import annotations

import logging
from typing import Optional, Sequence

from lolmanager.core.lcu_client import LcuClient, completed_champ_select_champion_ids


def _lcu_status_label(result: object) -> str:
    status = getattr(result, "status", None)
    return str(getattr(status, "value", status or "unknown"))


def choose_available_pick_index(
    *,
    pick_pool: Sequence[tuple[str, str]],
    candidate_ids: dict[int, int],
    unavailable_ids: frozenset[int],
    current_index: int,
) -> Optional[int]:
    """Prefer the first configured champion not already banned or locked."""
    if not pick_pool or not candidate_ids:
        return current_index if 0 <= current_index < len(pick_pool) else None
    for index, _candidate in enumerate(pick_pool):
        champion_id = candidate_ids.get(index)
        if champion_id is not None and champion_id > 0 and champion_id not in unavailable_ids:
            return index
    return None


def _resolve_pick_pool_champion_ids(
    lcu: Optional[LcuClient],
    pick_pool: Sequence[tuple[str, str]],
    logger: logging.Logger,
) -> dict[int, int]:
    resolver = getattr(lcu, "resolve_champ_select_champion_id_decision", None)
    if not callable(resolver):
        return {}
    resolved: dict[int, int] = {}
    for index, (champion_name, _configured_ban) in enumerate(pick_pool):
        try:
            result = resolver(champion_name)
        except Exception as exc:
            logger.debug("LCU 예비 픽 ID 조회 실패(%s): %s", champion_name, exc)
            return {}
        if not getattr(result, "ok", False):
            logger.debug(
                "LCU 예비 픽 ID 조회 미완료(%s,outcome=%s).",
                champion_name,
                _lcu_status_label(result),
            )
            return {}
        try:
            champion_id = int(getattr(result, "value", 0) or 0)
        except (TypeError, ValueError):
            return {}
        if champion_id <= 0:
            return {}
        resolved[index] = champion_id
    return resolved


def _reconcile_pick_pool_availability(
    lcu: Optional[LcuClient],
    pick_pool: Sequence[tuple[str, str]],
    *,
    current_index: int,
    candidate_ids: Optional[dict[int, int]],
    logger: logging.Logger,
    snapshot: Optional[object] = None,
) -> tuple[int, dict[int, int]]:
    """Read completed bans/picks even before the local pick action begins."""
    snapshot_fn = getattr(lcu, "get_champ_select_snapshot", None)
    if not callable(snapshot_fn):
        return current_index, candidate_ids or {}
    if candidate_ids is None:
        candidate_ids = _resolve_pick_pool_champion_ids(lcu, pick_pool, logger)
    if not candidate_ids:
        return current_index, candidate_ids
    if snapshot is None:
        try:
            result = snapshot_fn()
        except Exception as exc:
            logger.debug("LCU 밴/픽 가용성 조회 실패: %s", exc)
            return current_index, candidate_ids
        if not getattr(result, "ok", False):
            return current_index, candidate_ids
        snapshot = getattr(result, "value", None)
    raw = getattr(snapshot, "raw", None)
    if not isinstance(raw, dict):
        return current_index, candidate_ids
    selected = choose_available_pick_index(
        pick_pool=pick_pool,
        candidate_ids=candidate_ids,
        unavailable_ids=completed_champ_select_champion_ids(raw),
        current_index=current_index,
    )
    if selected is None:
        logger.warning("설정된 모든 픽 후보가 이미 밴 또는 확정 픽 상태입니다.")
        return current_index, candidate_ids
    return selected, candidate_ids
