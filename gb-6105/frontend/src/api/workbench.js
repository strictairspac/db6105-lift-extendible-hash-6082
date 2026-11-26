import { request } from "./http.js";

export const api = {
  config: () => request("/workbench"),
  login: data => request("/auth/login", {method: "POST", data}),
  cases: () => request("/cases"),
  createCase: data => request("/cases", {method: "POST", data}),
  runs: () => request("/runs"),
  run: (id, signal) => request(`/runs/${id}`, {signal}),
  enqueue: (id, data) => request(`/cases/${id}/runs`, {method: "POST", data}),
  review: id => request(`/runs/${id}/review`),
  submitReview: (id, data) => request(`/runs/${id}/review`, {method: "POST", data}),
  export: id => request(`/runs/${id}/export`, {raw: true}),
  evidence: id => request(`/runs/${id}/evidence`),
  evidenceSnapshot: id => request(`/runs/${id}/evidence/snapshot`),
  makeGap: (id, count) => request(`/runs/${id}/evidence/gap`, {method: "POST", data: {count}}),
  repairRetained: (id, result) =>
    request(`/runs/${id}/evidence/repairs/retained`, {method: "POST", data: {result}}),
  repairReplay: id => request(`/runs/${id}/evidence/repairs/replay`, {method: "POST"}),
  audit: () => request("/audit", {signal: AbortSignal.timeout(5000)}),
};
