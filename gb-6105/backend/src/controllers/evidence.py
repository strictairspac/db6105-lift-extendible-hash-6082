from fastapi import HTTPException
from sqlalchemy import select

from src.config.resources import cache, engine
from src.constants.evidence import EvidenceState
from src.models.evidence import evidence_snapshots
from src.services import evidence, runs


def _wake_worker():
    try:
        cache.lpush("workbench:wake", "replay")
        cache.ltrim("workbench:wake", 0, 99)
    except Exception:
        # 数据库里的修复登记是持久队列，worker 也会轮询。
        pass


def evidence_overview(run_id):
    with engine.connect() as db:
        run = runs.get(db, run_id)
        snapshot = db.execute(
            select(evidence_snapshots.c.digest).where(evidence_snapshots.c.run_id == run_id)
        ).first()
        repairs = evidence.list_repairs(db, run_id)
    state = run["evidence_state"]
    return {
        "run_id": run_id,
        "evidence_state": state,
        "digest": run["digest"],
        "downloadable": state in (EvidenceState.INTACT, EvidenceState.REPAIRED),
        "has_snapshot": snapshot is not None,
        "repairs": repairs,
    }


def make_gap(run_id, form, user):
    with engine.begin() as db:
        return evidence.simulate_gap(db, run_id, form.count, user["username"])


def submit_retained(run_id, form, user):
    with engine.begin() as db:
        return evidence.submit_retained(db, run_id, form.result, user["username"])


def request_replay(run_id, user):
    with engine.begin() as db:
        repair = evidence.request_replay(db, run_id, user["username"])
    _wake_worker()
    return repair


def retained_snapshot(run_id):
    """取回同批留存件，便于直接以留存件发起恢复。"""
    with engine.connect() as db:
        row = db.execute(
            select(evidence_snapshots).where(evidence_snapshots.c.run_id == run_id)
        ).mappings().first()
        if row is None:
            raise HTTPException(404, "该批没有同批留存件")
        return {"run_id": run_id, "result": row["result"], "digest": row["digest"]}
