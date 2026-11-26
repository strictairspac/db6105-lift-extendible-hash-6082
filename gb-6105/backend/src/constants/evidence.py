from enum import StrEnum


class EvidenceState(StrEnum):
    # 桶证据与原摘要一致，可正常下载
    INTACT = "intact"
    # 证据缺行、与原摘要对不上，检证页面标待修并暂停该批下载
    REPAIRING = "repairing"
    # 修复件已按原摘要核验通过，下载重新开放；原签核与原摘要保持不变
    REPAIRED = "repaired"


class RepairSource(StrEnum):
    # 同批留存件：提交留存的完整桶证据，同步比对原摘要
    RETAINED = "retained"
    # 原始卷宗重放：按原卷宗与参数重新跑一遍目录，由 worker 异步核验
    REPLAY = "replay"


class RepairStatus(StrEnum):
    REPLAYING = "replaying"
    VERIFIED = "verified"
    REJECTED = "rejected"
