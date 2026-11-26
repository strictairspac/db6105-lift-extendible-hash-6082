from time import time

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.constants.evidence import EvidenceState, RepairSource, RepairStatus
from src.constants.job_state import JobState
from src.domain.evidence import drop_bucket_rows, verify_digest, well_formed
from src.models.cases import cases
from src.models.evidence import evidence_repairs, evidence_snapshots
from src.models.runs import runs
from src.utils.serial import content_digest
from . import audit

# 卷宗重放模拟真实的重放耗时，便于演示两路并发：留存件先核验通过即可开放下载。
REPLAY_DELAY_SECONDS = 5


def _now():
    return int(time())


def list_repairs(db, run_id):
    rows = db.execute(
        select(evidence_repairs)
        .where(evidence_repairs.c.run_id == run_id)
        .order_by(evidence_repairs.c.id)
    ).mappings()
    return [dict(row) for row in rows]


def get_repair(db, run_id, source):
    row = db.execute(
        select(evidence_repairs).where(
            evidence_repairs.c.run_id == run_id,
            evidence_repairs.c.source == source,
        )
    ).mappings().first()
    return dict(row) if row else None


def _assess(run):
    """纯读判定：依据原摘要核对当前桶证据，不写库。"""
    if run["status"] != JobState.DONE or not run["digest"]:
        return run["evidence_state"]
    # 已恢复的批次不再回退，避免后到的损坏件再次封停下载。
    if run["evidence_state"] == EvidenceState.REPAIRED:
        return EvidenceState.REPAIRED
    if verify_digest(run["result"], run["digest"]):
        return EvidenceState.INTACT
    return EvidenceState.REPAIRING


def verify_run(db, run_id):
    """核对当前桶证据与原摘要，返回最新证据状态（纯读，不在只读连接里落库）。

    状态落库由缺行/修复等写事务负责；下载门禁和检证页面以这里的结论为准，
    因此即使列值滞后，页面与下载仍按实时核对结果对待修批次停下载、不影响其他批次。
    """
    run = db.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
    if run is None:
        raise HTTPException(404, "执行批次不存在")
    return _assess(run)


def mark_repairing(db, run_id):
    """把对不上原摘要的批次落库为待修（须在写事务内调用），只影响当前批次。"""
    run = db.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
    if run is None or run["evidence_state"] == EvidenceState.REPAIRED:
        return run["evidence_state"] if run else None
    if _assess(run) == EvidenceState.REPAIRING and run["evidence_state"] != EvidenceState.REPAIRING:
        db.execute(
            runs.update().where(runs.c.id == run_id).values(evidence_state=EvidenceState.REPAIRING)
        )
        audit.record(db, "evidence_mismatch", "system", "run", run_id)
    return _assess(run)


def ensure_downloadable(db, run_id):
    """下载门禁：核验通过（完整或已恢复）才放行，待修批次只停自己。"""
    state = verify_run(db, run_id)
    if state in (EvidenceState.INTACT, EvidenceState.REPAIRED):
        return state
    raise HTTPException(409, "桶证据与原摘要不一致，批次已标待修并暂停下载，请先修复并核验")


def save_snapshot(db, run_id, result, digest):
    """批次完成时留存同批桶证据，作为缺行后的第一恢复来源。"""
    db.execute(evidence_snapshots.insert().values(
        run_id=run_id, result=result, digest=digest, created_at=_now()
    ))


def simulate_gap(db, run_id, count, actor):
    """演示入口：让已签批次的桶证据缺行，触发待修流程（仅用于复现问题）。"""
    run = db.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
    if run is None:
        raise HTTPException(404, "执行批次不存在")
    if run["status"] != JobState.DONE or not run["result"]:
        raise HTTPException(409, "只有执行成功的批次才能制造证据缺行")
    bucket_rows = sum(1 for row in run["result"]["rows"] if row.get("类别") == "目录追踪")
    if not bucket_rows:
        raise HTTPException(409, "该批没有可抽走的桶记录")
    count = max(1, min(int(count), bucket_rows))
    damaged, dropped = drop_bucket_rows(run["result"], count)
    db.execute(
        runs.update().where(runs.c.id == run_id)
        .values(result=damaged, evidence_state=EvidenceState.REPAIRING)
    )
    audit.record(db, "evidence_gap", actor, "run", run_id)
    return {"run_id": run_id, "dropped_rows": dropped, "evidence_state": EvidenceState.REPAIRING}


def _restore(db, run_id, result, digest, repair_id, source_label):
    """用核验通过的修复件恢复桶记录和结论；原签核与原摘要照旧，只更新证据状态。"""
    db.execute(
        runs.update().where(runs.c.id == run_id).values(
            result=result, digest=digest, evidence_state=EvidenceState.REPAIRED
        )
    )
    db.execute(evidence_repairs.update().where(evidence_repairs.c.id == repair_id).values(
        status=RepairStatus.VERIFIED, updated_at=_now()
    ))
    audit.record(db, "evidence_repaired", source_label, "run", run_id)


def submit_retained(db, run_id, candidate, actor):
    """同批留存件恢复：同步比对原摘要，通过即恢复下载。重复提交不重复登记。"""
    run = db.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
    if run is None:
        raise HTTPException(404, "执行批次不存在")
    if run["status"] != JobState.DONE:
        raise HTTPException(409, "批次执行成功后才能修复证据")
    existing = get_repair(db, run_id, RepairSource.RETAINED)
    if existing is not None:
        # 后到的修复件不重复登记，原签核和摘要照旧，直接回原登记结论。
        return existing
    if not well_formed(candidate):
        raise HTTPException(422, "修复件结构不完整，需包含 summary、metrics 和 rows")

    candidate_digest = verify_digest(candidate, run["digest"])
    stamp = _now()
    try:
        result = db.execute(evidence_repairs.insert().values(
            run_id=run_id, source=RepairSource.RETAINED,
            status=RepairStatus.VERIFIED if candidate_digest else RepairStatus.REJECTED,
            submitted_by=actor, created_at=stamp, updated_at=stamp,
            payload=candidate, payload_digest=content_digest(candidate),
            detail=None if candidate_digest else "修复件与原摘要不一致，未恢复下载",
        ))
    except IntegrityError as exc:
        # 并发下唯一约束兜底：同批同来源只登记一次。
        db.rollback()
        repair = get_repair(db, run_id, RepairSource.RETAINED)
        if repair is not None:
            return repair
        raise HTTPException(409, "该批留存件修复已登记") from exc

    repair_id = result.inserted_primary_key[0]
    audit.record(db, "evidence_repair_submit", actor, "evidence_repair", repair_id)
    if candidate_digest:
        _restore(db, run_id, candidate, run["digest"], repair_id, actor)
    return get_repair(db, run_id, RepairSource.RETAINED)


def request_replay(db, run_id, actor):
    """原始卷宗重放：登记后由 worker 异步重放并核验。重复请求不重复登记。"""
    run = db.execute(
        select(runs, cases.c.payload.label("case_payload"))
        .join(cases, cases.c.id == runs.c.case_id)
        .where(runs.c.id == run_id)
    ).mappings().first()
    if run is None:
        raise HTTPException(404, "执行批次不存在")
    if run["status"] != JobState.DONE:
        raise HTTPException(409, "批次执行成功后才能重放卷宗")
    existing = get_repair(db, run_id, RepairSource.REPLAY)
    if existing is not None:
        return existing
    stamp = _now()
    try:
        result = db.execute(evidence_repairs.insert().values(
            run_id=run_id, source=RepairSource.REPLAY, status=RepairStatus.REPLAYING,
            submitted_by=actor, created_at=stamp, updated_at=stamp,
            detail="原始卷宗重放排队中",
        ))
    except IntegrityError:
        db.rollback()
        repair = get_repair(db, run_id, RepairSource.REPLAY)
        if repair is not None:
            return repair
        raise HTTPException(409, "该批卷宗重放已登记")
    repair_id = result.inserted_primary_key[0]
    audit.record(db, "evidence_replay_request", actor, "evidence_repair", repair_id)
    # 缺行批次落库为待修，下载在核验通过前保持暂停。
    mark_repairing(db, run_id)
    return get_repair(db, run_id, RepairSource.REPLAY)


def claim_replay(db):
    """worker 领取一条重放任务，行级锁跳过已被领取的任务。"""
    return db.execute(
        select(evidence_repairs, runs.c.parameter, cases.c.payload.label("case_payload"))
        .join(runs, runs.c.id == evidence_repairs.c.run_id)
        .join(cases, cases.c.id == runs.c.case_id)
        .where(
            evidence_repairs.c.source == RepairSource.REPLAY,
            evidence_repairs.c.status == RepairStatus.REPLAYING,
        )
        .order_by(evidence_repairs.c.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).mappings().first()


def complete_replay(db, repair, replay_result):
    """worker 重放完成后按原摘要核验桶记录和结论，通过才开放下载。"""
    run_id = repair["run_id"]
    run = db.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
    matched = bool(run) and verify_digest(replay_result, run["digest"])
    repair_id = repair["id"]
    if matched:
        _restore(db, run_id, replay_result, run["digest"], repair_id, "worker")
    else:
        db.execute(evidence_repairs.update().where(evidence_repairs.c.id == repair_id).values(
            status=RepairStatus.REJECTED, updated_at=_now(),
            payload=replay_result,
            payload_digest=content_digest(replay_result),
            detail="卷宗重放结果与原摘要不一致，未恢复下载",
        ))
        audit.record(db, "evidence_replay_reject", "worker", "evidence_repair", repair_id)
    return get_repair(db, run_id, RepairSource.REPLAY)


def fail_replay(db, repair, reason):
    """重放过程本身出错：登记失败原因，不开放下载，等待重试或改走留存件。"""
    db.execute(evidence_repairs.update().where(evidence_repairs.c.id == repair["id"]).values(
        status=RepairStatus.REJECTED, updated_at=_now(),
        detail=f"卷宗重放失败：{reason}"[:320],
    ))
    audit.record(db, "evidence_replay_reject", "worker", "evidence_repair", repair["id"])
