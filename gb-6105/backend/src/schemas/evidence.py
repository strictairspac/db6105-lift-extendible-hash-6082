from pydantic import BaseModel, Field


class RetainedRepairInput(BaseModel):
    # 同批留存的完整桶证据（含 summary、metrics、rows）。
    result: dict


class GapInput(BaseModel):
    # 演示用：从桶证据中抽走的「目录追踪」行数。
    count: int = Field(default=1, strict=True, ge=1, le=64)


class RepairEntry(BaseModel):
    id: int
    run_id: int
    source: str
    status: str
    submitted_by: str
    created_at: int
    updated_at: int
    payload: dict | None
    payload_digest: str | None
    detail: str | None


class EvidenceOverview(BaseModel):
    run_id: int
    evidence_state: str
    digest: str | None
    downloadable: bool
    repairs: list[RepairEntry]
