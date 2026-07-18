"use strict";

const state = {
  overview: null,
  currentView: "overview",
  pageSize: 25,
  offset: 0,
  caseTotal: 0,
  filters: { family: "", category: "", q: "" },
  searchTimer: null,
};

const viewMeta = {
  overview: ["MODEL EVALUATION", "Model readiness"],
  cases: ["EVALUATION DATA", "Evaluation cases"],
  workspace: ["LIVE RTL WORKSPACE", "Analyze a design"],
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = text;
  return element;
}

function formatPercent(value, digits = 1) {
  return value === null || value === undefined ? "n/a" : `${Number(value).toFixed(digits)}%`;
}

function formatScore(value) {
  return value === null || value === undefined ? "n/a" : Number(value).toFixed(3);
}

function humanize(value) {
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

async function getJSON(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload?.error?.message || `Request failed (${response.status})`);
  }
  return payload;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  window.setTimeout(() => toast.classList.remove("visible"), 3600);
}

function switchView(view) {
  if (!viewMeta[view]) return;
  state.currentView = view;
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
  $$(".view").forEach((item) => item.classList.toggle("active", item.id === `${view}-view`));
  $("#view-eyebrow").textContent = viewMeta[view][0];
  $("#view-title").textContent = viewMeta[view][1];
  if (view === "cases") loadCases();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderMetrics(overview) {
  const metrics = overview.metrics;
  $("#hero-accuracy").textContent = formatPercent(metrics.balanced_actionable_accuracy_percent);
  $("#hero-gap").textContent = metrics.balanced_actionable_gap_points.toFixed(2);
  $("#accuracy-ring").style.setProperty("--accuracy", `${metrics.balanced_actionable_accuracy_percent}%`);

  $("#metric-recall").textContent = formatPercent(metrics.opportunity_recall_percent);
  $("#metric-recall-bar").style.width = `${metrics.opportunity_recall_percent}%`;
  $("#metric-recall-detail").textContent = `${metrics.covered_opportunity_count} of ${metrics.opportunity_count} cases with a useful measured change were found.`;

  $("#metric-specificity").textContent = formatPercent(metrics.abstention_specificity_percent);
  $("#metric-specificity-bar").style.width = `${metrics.abstention_specificity_percent}%`;
  $("#metric-specificity-detail").textContent = `${metrics.correct_no_change_count} of ${metrics.no_change_case_count} cases with no useful change were correctly left unchanged.`;

  $("#metric-harmful").textContent = formatPercent(metrics.harmful_recommendation_rate_percent);
  $("#metric-harmful-bar").style.width = `${metrics.harmful_recommendation_rate_percent}%`;
  $("#metric-harmful-detail").textContent = `${metrics.harmful_count} of ${metrics.recommendation_count} recommendations did not meet the measured targets. Lower is better.`;
  $("#score-definition-text").textContent = `This score gives equal weight to useful changes found (${formatPercent(metrics.opportunity_recall_percent)}) and correct no-change decisions (${formatPercent(metrics.abstention_specificity_percent)}).`;
}

function renderFamilies(families) {
  const container = $("#family-list");
  container.replaceChildren();
  const filter = $("#family-filter");
  const selected = filter.value;
  filter.replaceChildren(new Option("All RTL patterns", ""));

  families.forEach((family) => {
    filter.append(new Option(family.name, family.id));
    const row = node("div", "family-row");
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-label", `Filter cases to ${family.name}`);

    const name = node("div", "family-name", family.name);
    name.append(node("small", "", `${family.opportunity_count} useful-change cases · ${family.covered_count} found`));

    const track = node("div", "family-track");
    const total = family.case_count || 1;
    const covered = node("span", "covered");
    covered.style.width = `${family.covered_count / total * 100}%`;
    const missedCount = Number(family.categories.no_candidate_clears_threshold || 0) + Number(family.categories.unsupported_family || 0);
    const missed = node("span", "missed");
    missed.style.width = `${missedCount / total * 100}%`;
    const harmful = node("span", "harmful");
    harmful.style.width = `${Number(family.categories.harmful_nonopportunity || 0) / total * 100}%`;
    track.append(covered, missed, harmful);

    const score = node("span", "family-score", family.opportunity_recall_percent === null ? "NO USEFUL CHANGES" : formatPercent(family.opportunity_recall_percent, 0));
    const support = node("span", `support-chip ${family.support === "unsupported" ? "unsupported" : ""}`, family.support === "unsupported" ? "MORE DATA" : "TRAINED");
    row.append(name, track, score, support);
    const activate = () => {
      state.filters.family = family.id;
      state.offset = 0;
      filter.value = family.id;
      switchView("cases");
    };
    row.addEventListener("click", activate);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") activate();
    });
    container.append(row);
  });
  filter.value = selected;
}

function renderGates(gates) {
  const container = $("#gate-list");
  container.replaceChildren();
  gates.forEach((gate) => {
    const item = node("div", `gate-item ${gate.passed ? "" : "failed"}`);
    item.append(node("span", "gate-symbol", gate.passed ? "✓" : "!"));
    const copy = node("div", "gate-copy");
    copy.append(node("strong", "", gate.label), node("span", "", gate.target));
    const actual = gate.actual_percent === null ? (gate.passed ? "PASS" : "FAIL") : formatPercent(gate.actual_percent);
    item.append(copy, node("span", "gate-value", actual));
    container.append(item);
  });
}

function renderFailures(categories) {
  const order = [
    ["no_candidate_clears_threshold", "Model confidence too low", ""],
    ["unsupported_family", "More training examples needed", ""],
    ["harmful_nonopportunity", "Incorrect recommendation", "danger"],
    ["covered_best", "Useful change found", "safe"],
  ];
  const maximum = Math.max(...order.map(([key]) => Number(categories[key] || 0)), 1);
  const container = $("#failure-bars");
  container.replaceChildren();
  order.forEach(([key, label, className]) => {
    const wrapper = node("div", "failure-bar");
    const head = node("div", "failure-bar-head");
    head.append(node("span", "", label), node("strong", "", String(categories[key] || 0)));
    const track = node("div", "failure-track");
    const bar = node("span", className);
    bar.style.width = `${Number(categories[key] || 0) / maximum * 100}%`;
    track.append(bar);
    wrapper.append(head, track);
    container.append(wrapper);
  });
}

function renderOverview(overview) {
  state.overview = overview;
  $("#source-status").textContent = `${overview.evidence.case_count} cases · held-out results not used`;
  $("#nav-case-count").textContent = overview.evidence.case_count;
  const pill = $("#global-gate-pill");
  pill.replaceChildren(node("span", "pulse-dot amber"), node("span", "", "V2.2 · not approved for live use"));
  renderMetrics(overview);
  renderFamilies(overview.families);
  renderGates(overview.gates);
  renderFailures(overview.failure_categories);
}

function outcomeLabel(category) {
  const labels = {
    covered_best: "Useful change found",
    no_candidate_clears_threshold: "Confidence too low",
    unsupported_family: "More training data needed",
    harmful_nonopportunity: "Incorrect recommendation",
    true_abstention: "Correct no-change decision",
  };
  return labels[category] || humanize(category);
}

function renderCaseRows(payload) {
  state.caseTotal = payload.pagination.total;
  const body = $("#case-table-body");
  body.replaceChildren();
  payload.items.forEach((item) => {
    const row = document.createElement("tr");
    row.tabIndex = 0;
    row.append(node("td", "case-id", item.case_id));
    row.append(node("td", "case-family", item.family_name));
    const outcomeCell = document.createElement("td");
    outcomeCell.append(node("span", `outcome-chip ${item.category}`, outcomeLabel(item.category)));
    row.append(outcomeCell);
    const decisionCell = document.createElement("td");
    decisionCell.append(node("span", `decision-chip ${item.decision}`, item.decision === "recommend" ? "CHANGE" : "NO CHANGE"));
    row.append(decisionCell);
    row.append(node("td", "score-cell", formatScore(item.max_eligibility_probability)));
    row.append(node("td", "", item.out_of_domain ? "NEW" : "FAMILIAR"));
    row.append(node("td", "row-arrow", "→"));
    const open = () => openCase(item.case_id);
    row.addEventListener("click", open);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter") open();
    });
    body.append(row);
  });
  if (!payload.items.length) {
    const row = document.createElement("tr");
    const cell = node("td", "", "No cases match these filters.");
    cell.colSpan = 7;
    cell.style.textAlign = "center";
    row.append(cell);
    body.append(row);
  }

  $("#case-result-count").textContent = payload.pagination.total;
  const first = payload.pagination.total ? payload.pagination.offset + 1 : 0;
  const last = payload.pagination.offset + payload.items.length;
  $("#pagination-label").textContent = `${first}–${last} of ${payload.pagination.total}`;
  $("#previous-page").disabled = payload.pagination.offset === 0;
  $("#next-page").disabled = !payload.pagination.has_more;
}

async function loadCases() {
  const parameters = new URLSearchParams({
    limit: String(state.pageSize),
    offset: String(state.offset),
  });
  Object.entries(state.filters).forEach(([key, value]) => {
    if (value) parameters.set(key, value);
  });
  try {
    renderCaseRows(await getJSON(`/api/v1/cases?${parameters}`));
  } catch (error) {
    showToast(error.message);
  }
}

function metricCell(metric, predicted, measured) {
  const cell = node("div", "ppa-cell");
  cell.append(node("span", "", metric === "cell_count" ? "CELLS" : metric.toUpperCase()));
  const value = node("strong", predicted >= 0 ? "positive" : "negative", `${predicted >= 0 ? "+" : ""}${predicted.toFixed(2)}%`);
  cell.append(value, node("small", "", `measured ${measured >= 0 ? "+" : ""}${measured.toFixed(2)}%`));
  return cell;
}

function renderCandidate(candidate) {
  const card = node("article", `candidate-card ${candidate.selected ? "selected" : ""} ${candidate.measured_best ? "best" : ""}`);
  const head = node("div", "candidate-head");
  const title = node("div", "candidate-title");
  title.append(node("strong", "", candidate.template_id));
  if (candidate.selected) title.append(node("span", "candidate-tag", "SELECTED"));
  if (candidate.measured_best) title.append(node("span", "candidate-tag best", "BEST SYNTHESIS RESULT"));
  head.append(title, node("span", "candidate-score", `confidence ${formatScore(candidate.eligibility.probability)} / required ${formatScore(candidate.eligibility.threshold)}`));
  const grid = node("div", "ppa-grid");
  ["delay", "area", "cell_count"].forEach((metric) => grid.append(metricCell(metric, candidate.predicted[metric], candidate.measured[metric])));
  const footer = node("div", "candidate-footer");
  const eligibility = candidate.measured_eligible ? "met measured targets" : "did not meet measured targets";
  const stages = node("span", "stage-dots");
  stages.title = "Generation, lint, and formal passed; OpenROAD not run";
  stages.append(node("span", "passed"), node("span", "passed"), node("span", "passed"), node("span", ""));
  footer.append(node("span", "", eligibility), stages);
  card.append(head, grid, footer);
  return card;
}

async function openCase(caseId) {
  const dialog = $("#case-dialog");
  try {
    const detail = await getJSON(`/api/v1/cases/${encodeURIComponent(caseId)}`);
    $("#dialog-case-id").textContent = detail.case.case_id;
    $("#dialog-family").textContent = detail.case.family_name;
    $("#dialog-rtl-file").textContent = `${detail.rtl.file} · ${detail.rtl.top}`;
    $("#dialog-source").textContent = detail.rtl.source;
    const status = $("#dialog-status");
    status.replaceChildren(
      node("span", `outcome-chip ${detail.case.category}`, outcomeLabel(detail.case.category)),
      node("span", "status-title", detail.case.decision === "recommend" ? "CHANGE RECOMMENDED" : "NO CHANGE RECOMMENDED"),
      node("span", "status-detail", detail.case.opportunity ? "synthesis found a useful improvement" : "no candidate met the measured targets"),
    );
    const candidates = $("#dialog-candidates");
    candidates.replaceChildren(...detail.candidates.map(renderCandidate));
    $("#dialog-provenance").textContent = `evaluation record ${String(detail.provenance.diagnostic_hash).slice(0, 12)}…`;
    if (!dialog.open) dialog.showModal();
  } catch (error) {
    showToast(error.message);
  }
}

function wireEvents() {
  $$(".nav-item").forEach((item) => item.addEventListener("click", () => switchView(item.dataset.view)));
  $$('[data-jump]').forEach((item) => item.addEventListener("click", () => switchView(item.dataset.jump)));
  $("#refresh-button").addEventListener("click", initialize);
  $("#family-filter").addEventListener("change", (event) => {
    state.filters.family = event.target.value;
    state.offset = 0;
    loadCases();
  });
  $("#category-filter").addEventListener("change", (event) => {
    state.filters.category = event.target.value;
    state.offset = 0;
    loadCases();
  });
  $("#case-search").addEventListener("input", (event) => {
    window.clearTimeout(state.searchTimer);
    state.searchTimer = window.setTimeout(() => {
      state.filters.q = event.target.value.trim();
      state.offset = 0;
      loadCases();
    }, 180);
  });
  $("#previous-page").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - state.pageSize);
    loadCases();
  });
  $("#next-page").addEventListener("click", () => {
    state.offset += state.pageSize;
    loadCases();
  });
  $("#dialog-close").addEventListener("click", () => $("#case-dialog").close());
  $("#case-dialog").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) event.currentTarget.close();
  });
}

async function initialize() {
  try {
    const overview = await getJSON("/api/v1/overview");
    renderOverview(overview);
    $("#loading-screen").classList.add("hidden");
    $$(".view").forEach((view) => view.classList.remove("loading"));
    if (state.currentView === "cases") loadCases();
  } catch (error) {
    $("#loading-screen p").textContent = `Could not load evidence: ${error.message}`;
    showToast(error.message);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $$(".view").forEach((view) => view.classList.add("loading"));
  wireEvents();
  initialize();
});
