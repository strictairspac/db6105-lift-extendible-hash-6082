import { api } from "../api/workbench.js";
import { errorText, field } from "../components/common/fields.js";
import { empty, pageHeading, section } from "../components/common/panel.js";
import { resultTable, table } from "../components/common/table.js";
import { RepairSources, RepairStates } from "../constants/states.js";
import { submit } from "../hooks/submit.js";
import { poll } from "../hooks/poll.js";
import { el } from "../utils/dom.js";
import { cell, download, time } from "../utils/format.js";

function evidenceBadge(state) {
  const label = state === "intact" ? "证据完整" : state === "repairing" ? "待修·暂停下载" : "已恢复";
  return el("span", {
    class: `status state-evidence-${state}`, "data-testid": "evidence-state", "data-state": state,
  }, label);
}

function reviewForm(id, saved) {
  const form = el("form", {id: "review-form"},
    field("复核意见", el("textarea", {name: "note", required: true, minlength: 3, maxlength: 500, rows: 3})),
    errorText(), el("button", {type: "submit"}, "签署复核"));
  submit(form, async values => saved(await api.submitReview(id, {note: values.get("note")})));
  return form;
}

function signature(review) {
  return el("div", {"data-testid": "reviewed", class: "signature"},
    el("strong", {}, `已签署 · ${review.actor}`), el("p", {}, review.note), el("time", {}, time(review.created_at)));
}

function repairsTable(repairs) {
  if (!repairs.length) return empty("暂无修复登记。");
  return table(["修复来源", "核验状态", "提交人", "提交时间", "说明"],
    repairs.map(repair => [
      RepairSources[repair.source] || repair.source,
      el("span", {class: `repair-state repair-${repair.status}`}, RepairStates[repair.status] || repair.status),
      repair.submitted_by, time(repair.created_at), repair.detail || "—",
    ]), "修复登记");
}

// 缺行与修复操作仅资料操作员可用；复核员/观察员只读。
function repairControls(runId, overview, role, refresh, feedback) {
  const retainedBtn = el("button", {type: "button"}, "用同批留存件恢复");
  const replayBtn = el("button", {type: "button"}, "按原始卷宗重放");
  const gapBtn = el("button", {class: "quiet", type: "button"}, "模拟桶证据缺行");

  retainedBtn.onclick = async () => {
    retainedBtn.disabled = true; feedback.textContent = "";
    try {
      const snapshot = await api.evidenceSnapshot(runId);
      await api.repairRetained(runId, snapshot.result);
      await refresh();
    } catch (error) { feedback.textContent = error.message; }
    finally { retainedBtn.disabled = false; }
  };
  replayBtn.onclick = async () => {
    replayBtn.disabled = true; feedback.textContent = "";
    try { await api.repairReplay(runId); await refresh(); }
    catch (error) { feedback.textContent = error.message; }
    finally { replayBtn.disabled = false; }
  };
  gapBtn.onclick = async () => {
    gapBtn.disabled = true; feedback.textContent = "";
    try { await api.makeGap(runId, 1); await refresh(); }
    catch (error) { feedback.textContent = error.message; }
    finally { gapBtn.disabled = false; }
  };

  if (role !== "operator") return empty("修复操作由资料操作员发起。");
  return el("div", {class: "repair-actions"},
    ...[overview.has_snapshot ? retainedBtn : null, replayBtn, gapBtn]);
}

export async function evidencePage(config, user, route, signal) {
  const runs = (await api.runs()).filter(item => item.status === "done");
  const run = route.id ? await api.run(route.id) : runs[0];
  if (!run?.result) return el("div", {class: "page"},
    pageHeading(config.modules[3], null), empty("成功执行一个批次后可以查看、签核和下载证据。"),
    el("a", {href: "#runs"}, config.modules[2]));

  let overview = await api.evidence(run.id);
  let review = await api.review(run.id);
  const issue = errorText();
  const repairFeedback = errorText();
  const repairBox = el("div", {class: "stack repair-panel"});
  const signing = el("div", {});
  let polling = false;

  function renderSigning() {
    signing.replaceChildren(...(review
      ? [signature(review)]
      : user.role === "reviewer" ? [reviewForm(run.id, () => refresh())] : [empty("待复核员签署。")]));
  }

  function renderRepair() {
    repairBox.replaceChildren(
      repairFeedback,
      el("p", {class: "repair-hint"}, overview.has_snapshot
        ? "同批留存件已冻结，可同步比对原摘要；原始卷宗重放由 worker 异步核验。"
        : "该批没有同批留存件，只能走原始卷宗重放。"),
      repairControls(run.id, overview, user.role, refresh, repairFeedback),
      section("修复登记", repairsTable(overview.repairs)));
  }

  // 卷宗重放进行中时轮询（仅启动一次），worker 核验完成即刷新并开放下载。
  function startPollingIfNeeded() {
    if (polling || signal.aborted) return;
    if (!overview.repairs.some(repair => repair.status === "replaying")) return;
    polling = true;
    poll(async () => {
      if (signal.aborted) return false;
      const next = await api.evidence(run.id);
      const stillReplaying = next.repairs.some(repair => repair.status === "replaying");
      await refresh(next);
      return stillReplaying;
    }, signal);
  }

  // refresh(nextOverview)：缺行/修复/轮询后重取数据并重绘，保持证据状态与门禁一致。
  async function refresh(nextOverview) {
    const [latestRun, latestOverview, latestReview] = await Promise.all([
      api.run(run.id),
      nextOverview ? Promise.resolve(nextOverview) : api.evidence(run.id),
      api.review(run.id),
    ]);
    Object.assign(run, latestRun);
    overview = latestOverview;
    review = latestReview;
    renderSigning();
    renderRepair();
    root.replaceChildren(...build());
    startPollingIfNeeded();
  }

  const exportButton = el("button", {type: "button", onclick: async () => {
    exportButton.disabled = true;
    issue.textContent = "";
    try {
      await download(await api.export(run.id), `evidence-${run.id}.json`);
    } catch (error) {
      issue.textContent = error.message;
      await refresh();
    } finally {
      exportButton.disabled = false;
    }
  }}, "导出证据");

  const options = el("select", {"aria-label": "选择已完成的批次", onchange: event => {
    location.hash = `#evidence/${event.target.value}`;
  }}, runs.map(item => el("option", {value: item.id, selected: item.id === run.id}, `#${item.id} ${item.case_title}`)));

  function build() {
    if (!overview.downloadable) {
      exportButton.disabled = true;
      exportButton.title = "桶证据与原摘要不一致，待修期间暂停下载";
    } else {
      exportButton.disabled = false;
      exportButton.title = "";
    }
    const repairNotice = overview.evidence_state === "repairing" ?
      el("div", {class: "repair-banner", role: "alert"},
        el("strong", {}, "桶证据与原摘要对不上，已标待修并暂停该批下载；其他批次照常。"),
        el("p", {}, "可从同批留存件或原始卷宗恢复，桶记录和结论都验过原摘要才会重新开放。")) : null;
    return el("div", {class: "page evidence"},
      pageHeading(config.modules[3], run.case_title, exportButton),
      issue, options,
      repairNotice,
      el("section", {class: "feature evidence-summary"},
        el("div", {}, el("h2", {}, "批次判定"), el("p", {}, run.result.summary),
          el("p", {class: "evidence-state-line"}, "证据状态：", evidenceBadge(overview.evidence_state))),
        el("details", {}, el("summary", {}, "证据摘要（原 SHA-256）"), el("code", {}, run.digest))),
      el("dl", {class: "metrics"}, Object.entries(run.result.metrics).map(([label, value]) =>
        el("div", {}, el("dt", {}, label), el("dd", {}, cell(value))))),
      section("业务结果明细", run.result.rows.length ? resultTable(run.result.rows) : empty("此批次无明细。")),
      section("证据核验与修复", repairBox),
      section("复核签署", signing));
  }

  renderSigning();
  renderRepair();
  const root = build();
  startPollingIfNeeded();
  return root;
}
