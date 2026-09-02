"use strict";

const state = { suites: [], runs: [], selectedRunId: null, pollTimer: null };
const terminalStatuses = new Set(["completed", "infra_failed", "timed_out"]);

const byId = (id) => document.getElementById(id);
const create = (tag, className, text) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `请求失败（HTTP ${response.status}）`);
  }
  return response.json();
}

function showToast(message) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.hidden = false;
  window.setTimeout(() => { toast.hidden = true; }, 3600);
}

function setConnection(ready) {
  const dot = byId("connection-dot");
  dot.className = `connection-dot ${ready ? "is-ready" : "is-down"}`;
  byId("connection-label").textContent = ready ? "平台已就绪" : "平台暂不可用";
}

function showView(name) {
  document.querySelectorAll(".view").forEach((view) => view.classList.remove("is-visible"));
  byId(`view-${name}`).classList.add("is-visible");
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.view === name));
  if (name !== "detail") stopPolling();
  if (name === "runs") loadRuns();
  if (name === "health") loadHealth();
  byId("main-content").focus({ preventScroll: true });
}

function tone(value) {
  if (["passed", "completed"].includes(value)) return "success";
  if (["failed", "infra_failed", "timed_out", "test_failed", "error"].includes(value)) return "danger";
  if (["queued", "unknown", "dispatched"].includes(value)) return "warning";
  return "info";
}

function badge(value) {
  const element = create("span", "badge", value || "—");
  element.dataset.tone = tone(value);
  return element;
}

function formatTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "medium" }).format(new Date(value));
}

function formatDuration(started, finished) {
  if (!started || !finished) return "—";
  const milliseconds = Math.max(0, new Date(finished) - new Date(started));
  return milliseconds < 1000 ? `${milliseconds} ms` : `${(milliseconds / 1000).toFixed(2)} s`;
}

async function loadSuites() {
  const payload = await api("/api/v1/suites");
  state.suites = payload.suites;
  const selects = [byId("suite-filter"), byId("suite-select")];
  selects.forEach((select, index) => {
    const initial = create("option", "", index === 0 ? "全部套件" : "请选择套件");
    initial.value = "";
    select.replaceChildren(initial);
    state.suites.forEach((suite) => {
      const option = create("option", "", `${suite.suite_id} · ${suite.runner_type}`);
      option.value = suite.suite_id;
      select.append(option);
    });
  });
}

async function loadRuns() {
  const statePanel = byId("runs-state");
  const table = byId("runs-table-wrap");
  statePanel.hidden = false;
  statePanel.textContent = "正在加载运行记录…";
  table.hidden = true;
  const params = new URLSearchParams({ limit: "50" });
  if (byId("status-filter").value) params.set("status", byId("status-filter").value);
  if (byId("suite-filter").value) params.set("suite_id", byId("suite-filter").value);
  try {
    const payload = await api(`/api/v1/runs?${params}`);
    state.runs = payload.runs;
    renderRuns();
    statePanel.hidden = payload.runs.length > 0;
    statePanel.textContent = "还没有符合条件的 Run。创建一次测试后，它会出现在这里。";
    table.hidden = payload.runs.length === 0;
  } catch (error) {
    statePanel.textContent = `${error.message}。请确认平台已经启动后重试。`;
  }
}

function renderRuns() {
  const body = byId("runs-body");
  body.replaceChildren();
  state.runs.forEach((run) => {
    const row = document.createElement("tr");
    const suite = create("td", "", run.suite_id);
    const statusCell = create("td"); statusCell.append(badge(run.status));
    const outcomeCell = create("td"); outcomeCell.append(badge(run.outcome));
    const attempt = create("td", "mono", run.latest_attempt_no ? `#${run.latest_attempt_no}` : "—");
    const createdAt = create("td", "", formatTime(run.created_at));
    const actionCell = create("td");
    const action = create("button", "row-button", "查看详情");
    action.type = "button";
    action.addEventListener("click", () => openRun(run.run_id));
    actionCell.append(action);
    row.append(suite, statusCell, outcomeCell, attempt, createdAt, actionCell);
    body.append(row);
  });
}

function renderParameterFields() {
  const fields = byId("parameter-fields");
  fields.replaceChildren();
  const suite = state.suites.find((item) => item.suite_id === byId("suite-select").value);
  if (!suite) return;
  Object.entries(suite.allowed_parameters).forEach(([name, values]) => {
    const label = create("label", "", name);
    const select = document.createElement("select");
    select.dataset.parameter = name;
    select.required = true;
    values.forEach((value) => {
      const option = create("option", "", value);
      option.value = value;
      select.append(option);
    });
    label.append(select);
    fields.append(label);
  });
  byId("suite-hint").textContent = `${suite.runner_type} 套件 · ${Object.keys(suite.allowed_parameters).length} 个可选参数`;
}

async function createRun(event) {
  event.preventDefault();
  const errorBox = byId("form-error");
  const button = byId("submit-run");
  errorBox.hidden = true;
  button.disabled = true;
  button.textContent = "正在提交…";
  const parameters = {};
  document.querySelectorAll("[data-parameter]").forEach((field) => { parameters[field.dataset.parameter] = field.value; });
  try {
    const response = await api("/api/v1/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ suite_id: byId("suite-select").value, parameters }),
    });
    showToast("Run 已受理，平台正在后台执行。202 不代表测试已经通过。");
    await openRun(response.run_id);
  } catch (error) {
    errorBox.textContent = `${error.message}。请检查套件和参数后重试。`;
    errorBox.hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "提交 Run";
  }
}

async function openRun(runId) {
  state.selectedRunId = runId;
  showView("detail");
  await loadRunDetail();
}

async function loadRunDetail() {
  if (!state.selectedRunId) return;
  const detailState = byId("detail-state");
  const detailContent = byId("detail-content");
  detailState.hidden = false;
  detailState.textContent = "正在加载 Run 详情…";
  detailContent.hidden = true;
  try {
    const base = `/api/v1/runs/${state.selectedRunId}`;
    const [run, cases, events, artifacts] = await Promise.all([
      api(base), api(`${base}/cases`), api(`${base}/events`), api(`${base}/artifacts`),
    ]);
    renderDetail(run, cases.cases, events.events, artifacts.artifacts);
    detailState.hidden = true;
    detailContent.hidden = false;
    if (!terminalStatuses.has(run.status)) startPolling(); else stopPolling();
  } catch (error) {
    detailState.textContent = `${error.message}。运行记录可能不存在，或者平台暂时不可用。`;
  }
}

function renderDetail(run, cases, events, artifacts) {
  byId("detail-title").textContent = run.suite_id;
  byId("detail-id").textContent = `Run ${run.run_id}`;
  const summaries = [
    ["状态", run.status, true], ["结果", run.outcome, true],
    ["用例", `${run.case_summary.passed} / ${run.case_summary.total}`, false],
    ["耗时", formatDuration(run.timestamps.started_at, run.timestamps.finished_at), false],
  ];
  const grid = byId("summary-grid"); grid.replaceChildren();
  summaries.forEach(([label, value, isBadge]) => {
    const card = create("article", "summary-card");
    card.append(create("span", "", label));
    if (isBadge) card.append(badge(value)); else card.append(create("strong", "", value));
    grid.append(card);
  });
  renderRows(byId("cases-content"), cases, (item) => {
    const row = create("div", "data-row");
    row.append(create("strong", "", item.node_id), badge(`${item.status} · ${item.duration_ms ?? "—"} ms`));
    if (item.message) row.append(create("pre", "case-message", item.message));
    return row;
  });
  byId("case-count").textContent = `${cases.length} 条`;
  renderRows(byId("gates-content"), run.gates, (item) => {
    const row = create("div", "data-row");
    row.append(create("strong", "", item.gate_type), badge(item.passed ? "passed" : "failed"));
    if (item.reason_codes.length) row.append(create("span", "case-message", item.reason_codes.join(", ")));
    return row;
  });
  renderRows(byId("metrics-content"), run.metrics, (item) => {
    const row = create("div", "data-row"); row.append(create("strong", "", item.name), create("span", "mono", `${item.value} ${item.unit || ""}`)); return row;
  });
  const timeline = byId("events-content"); timeline.replaceChildren();
  if (!events.length) timeline.append(create("li", "empty", "暂无事件"));
  events.forEach((item) => {
    const entry = document.createElement("li"); entry.append(create("strong", "", item.event_type), create("span", "", formatTime(item.created_at))); timeline.append(entry);
  });
  renderRows(byId("artifacts-content"), artifacts, (item) => {
    const row = create("div", "data-row");
    const label = create("div"); label.append(create("strong", "", item.artifact_type), create("div", "muted", formatBytes(item.size_bytes)));
    const actions = create("div", "artifact-actions");
    const base = `/api/v1/runs/${run.run_id}/artifacts/${item.artifact_id}/content`;
    const view = create("a", "text-link", "查看"); view.href = base; view.target = "_blank"; view.rel = "noopener";
    const download = create("a", "text-link", "下载"); download.href = `${base}?download=true`;
    actions.append(view, download); row.append(label, actions); return row;
  });
  renderRows(byId("attempts-content"), run.attempts, (item) => {
    const row = create("div", "data-row"); row.append(create("strong", "", `Attempt #${item.attempt_no}`), badge(item.status));
    row.append(create("span", "muted", `${formatTime(item.started_at)} · exit ${item.exit_code ?? "—"}`)); return row;
  });
}

function renderRows(container, items, renderer) {
  container.replaceChildren();
  container.className = "data-list";
  if (!items.length) { container.append(create("p", "empty", "暂无数据")); return; }
  items.forEach((item) => container.append(renderer(item)));
}

function formatBytes(value) {
  if (value === null || value === undefined) return "大小未知";
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(1)} KiB`;
}

function startPolling() {
  stopPolling();
  state.pollTimer = window.setInterval(() => { if (!document.hidden) loadRunDetail(); }, 2000);
}

function stopPolling() {
  if (state.pollTimer) window.clearInterval(state.pollTimer);
  state.pollTimer = null;
}

async function loadHealth() {
  let live = false; let ready = false;
  try { await api("/health/live"); live = true; } catch (_) { live = false; }
  try { await api("/health/ready"); ready = true; } catch (_) { ready = false; }
  byId("live-status").replaceChildren(badge(live ? "passed" : "failed"));
  byId("ready-status").replaceChildren(badge(ready ? "passed" : "failed"));
  setConnection(ready);
}

async function boot() {
  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
  document.querySelectorAll("[data-view-target]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.viewTarget)));
  byId("open-create").addEventListener("click", () => showView("create"));
  byId("refresh-runs").addEventListener("click", loadRuns);
  byId("refresh-detail").addEventListener("click", loadRunDetail);
  byId("refresh-health").addEventListener("click", loadHealth);
  byId("status-filter").addEventListener("change", loadRuns);
  byId("suite-filter").addEventListener("change", loadRuns);
  byId("suite-select").addEventListener("change", renderParameterFields);
  byId("create-form").addEventListener("submit", createRun);
  try {
    await Promise.all([loadSuites(), loadHealth()]);
    await loadRuns();
  } catch (error) {
    setConnection(false);
    byId("runs-state").textContent = `${error.message}。请确认平台已经启动。`;
  }
}

document.addEventListener("DOMContentLoaded", boot);
