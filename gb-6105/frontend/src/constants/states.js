export const States = Object.freeze({
  queued: "等待执行", done: "已完成", failed: "执行失败",
});

export const EvidenceStates = Object.freeze({
  intact: "证据完整", repairing: "待修·暂停下载", repaired: "已恢复",
});

export const RepairStates = Object.freeze({
  replaying: "卷宗重放中", verified: "已核验通过", rejected: "核验未过",
});

export const RepairSources = Object.freeze({
  retained: "同批留存件", replay: "原始卷宗重放",
});

export const Actions = Object.freeze({
  create_case: "登记资料", enqueue: "提交执行", done: "执行完成", failed: "执行失败", review: "复核签署",
  evidence_mismatch: "证据待修", evidence_gap: "证据缺行", evidence_repair_submit: "留存件修复",
  evidence_replay_request: "卷宗重放", evidence_repaired: "证据恢复", evidence_replay_reject: "重放未过",
});
