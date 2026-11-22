from __future__ import annotations

from contextlib import contextmanager
import logging
import threading
from typing import Iterator

from lolmanager.core.lcu_client import LcuClient, PHASE_CHAMP_SELECT


@contextmanager
def monitor_champ_select_swaps(
    lcu: LcuClient, logger: logging.Logger
) -> Iterator[None]:
    if not isinstance(lcu, LcuClient):
        yield
        return

    stop = threading.Event()
    # Keep the swap requests independent of pick/ban confirmation and image scans.
    client = LcuClient(lockfile=lcu.lockfile)

    def monitor() -> None:
        with client.session:
            while not stop.is_set():
                delay = 0.75
                try:
                    phase = client.get_gameflow_phase(max_age_sec=0)
                    if phase == PHASE_CHAMP_SELECT:
                        delay = 0.2
                        for kind in client.respond_to_champ_select_swaps():
                            logger.info(
                                "%s",
                                "라인 스왑 거절 완료"
                                if kind == "position"
                                else "픽 순서 스왑 수락 완료",
                            )
                    elif phase is not None:
                        client.clear_champ_select_swaps()
                except Exception:
                    logger.debug(
                        "챔피언 선택 스왑 상태를 읽지 못했습니다.", exc_info=True
                    )
                stop.wait(delay)

    worker = threading.Thread(target=monitor, name="champ-select-swaps", daemon=True)
    worker.start()
    lcu.background_swap_monitor = True
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=1.0)
        lcu.background_swap_monitor = False
