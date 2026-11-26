from sqlalchemy import JSON, BigInteger, Column, ForeignKey, Integer, String, Table, UniqueConstraint
from .base import metadata


# 批次完成时自动留存的同批桶证据快照，供缺行恢复使用。
evidence_snapshots = Table(
    "evidence_snapshots", metadata,
    Column("run_id", ForeignKey("runs.id"), primary_key=True),
    Column("result", JSON(none_as_null=True), nullable=False),
    Column("digest", String(64), nullable=False),
    Column("created_at", BigInteger, nullable=False),
)


# 每次修复登记一行；同一批次同一来源只登记一次，重试不重复登记。
evidence_repairs = Table(
    "evidence_repairs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", ForeignKey("runs.id"), nullable=False, index=True),
    Column("source", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("submitted_by", String(48), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    # 提交上来的修复件（留存件）；卷宗重放没有提交体，成功后由 worker 回填重放结果。
    Column("payload", JSON(none_as_null=True), nullable=True),
    Column("payload_digest", String(64), nullable=True),
    Column("detail", String(320), nullable=True),
    UniqueConstraint("run_id", "source", name="uq_evidence_repairs_run_source"),
)
