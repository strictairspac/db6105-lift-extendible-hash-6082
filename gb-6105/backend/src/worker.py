import logging
import time

from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.config.resources import cache, engine
from src.domain.scenario import run as run_scenario
from src.services import evidence
from src.services.evidence import REPLAY_DELAY_SECONDS
from src.services.execution import JobFailure, fail, service_one

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worker")


def check():
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    heartbeat = cache.get("workbench:heartbeat")
    return heartbeat is not None and time.time() - float(heartbeat) < 90


def replay_one():
    """重放一条原始卷宗修复任务：按原卷宗、原参数重新生成并按原摘要核验。"""
    with engine.begin() as db:
        repair = evidence.claim_replay(db)
        if repair is None:
            return False
        run_id, parameter, payload = repair["run_id"], repair["parameter"], repair["case_payload"]
    # 在事务外重放，模拟真实重放耗时；此期间留存件可先恢复下载。
    logger.info("Replaying case payload for run %s", run_id)
    time.sleep(REPLAY_DELAY_SECONDS)
    try:
        replayed = run_scenario(payload, parameter)
    except Exception as exc:  # 重放失败不拖垮 worker，登记原因后继续处理下一条。
        logger.exception("Replay failed for repair %s", repair["id"])
        with engine.begin() as db:
            evidence.fail_replay(db, repair, str(exc) or "重放计算异常")
        return True
    with engine.begin() as db:
        # 批次可能已被留存件恢复，重放结果仍照常登记，但不回滚已开放的下载。
        evidence.complete_replay(db, repair, replayed)
    return True


def work():
    while True:
        try:
            cache.set("workbench:heartbeat", time.time(), ex=90)
            try:
                if service_one():
                    continue
                if replay_one():
                    continue
            except JobFailure as exc:
                logger.exception("Execution %s failed", exc.run_id)
                fail(exc)
                continue
            cache.blpop("workbench:wake", timeout=1)
        except (RedisError, SQLAlchemyError):
            logger.exception("Worker connection unavailable")
            time.sleep(2)


if __name__ == "__main__":
    work()
