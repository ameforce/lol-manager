from src.ErrorCode import (
    ExecutionError,
    RegistryReadError,
    RegexMatchError,
    WindowHandleNotInitialized,
    WindowNotFoundError,
)

from src.Runner import Runner
from src.Mover import Mover
from src.AutoPicker import AutoPicker

import logging


def main() -> None:
    logging.basicConfig(
        level = logging.INFO,
        format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)

    try:
        logger.info('설치 경로 추출 중...')
        runner = Runner()
        logger.info('설치 경로 추출 완료')
    except (RegistryReadError, RegexMatchError) as e:
        logger.error(e)
        exit(e.code)

    try:
        logger.info('실행 파일들 실행 중...')
        runner.run()
        logger.info('실행 파일들 실행 완료')
    except ExecutionError as e:
        logger.error(e)
        exit(e.code)

    mover = Mover()
    try:
        logger.info('실행 창 이동 중...')
        mover.move_windows_to_game_desktop()
        logger.info('실행 창 이동 완료')
    except (WindowNotFoundError, WindowHandleNotInitialized) as e:
        logging.error(e)
        exit(e.code)

    autopicker = AutoPicker()
    logger.info('자동 매칭 수락 기능 실행 중...')
    if autopicker.run():
        logger.info('자동 매칭 수락 완료')
    else:
        logger.warning('자동 매칭 수락 실패')

    return


if __name__ == '__main__':
    main()
