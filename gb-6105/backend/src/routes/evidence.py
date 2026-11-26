from fastapi import APIRouter, Depends

from src.controllers import evidence as controller
from src.controllers.access import identity, operator
from src.schemas.evidence import EvidenceOverview, GapInput, RepairEntry, RetainedRepairInput

router = APIRouter(prefix="/runs", tags=["Evidence"])


@router.get("/{run_id}/evidence", response_model=EvidenceOverview, dependencies=[Depends(identity)])
def overview(run_id: int):
    return controller.evidence_overview(run_id)


@router.get("/{run_id}/evidence/snapshot", dependencies=[Depends(identity)])
def snapshot(run_id: int):
    return controller.retained_snapshot(run_id)


# 演示入口：制造桶证据缺行以复现「标待修、停下载」。生产可由操作员复核后触发。
@router.post("/{run_id}/evidence/gap", response_model=dict, dependencies=[Depends(operator)])
def make_gap(run_id: int, form: GapInput, user=Depends(operator)):
    return controller.make_gap(run_id, form, user)


@router.post("/{run_id}/evidence/repairs/retained", response_model=RepairEntry, status_code=201)
def repair_with_retained(run_id: int, form: RetainedRepairInput, user=Depends(operator)):
    return controller.submit_retained(run_id, form, user)


@router.post("/{run_id}/evidence/repairs/replay", response_model=RepairEntry, status_code=202)
def repair_with_replay(run_id: int, user=Depends(operator)):
    return controller.request_replay(run_id, user)
