"use strict";

const state = {
  overview: null,
  analytics: null,
  analyticsLoading: null,
  runs: [],
  runsLoaded: false,
  runsLoading: null,
  cases: [],
  activeView: "library",
  family: null,
  caseId: null,
  caseDetail: null,
  candidateId: null,
  runId: null,
  searchTimer: null,
  caseQueryToken: 0,
  caseDetailToken: 0,
  exploreFilters: {
    profile: "",
    objective: "",
    classification: "",
    decision: "",
    transformation: "",
    query: "",
  },
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = text;
  return element;
}

async function getJSON(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload?.error?.message || `Request failed (${response.status})`);
  return payload;
}

function humanize(value) {
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function shortHash(value) {
  const text = String(value || "");
  return text ? `${text.slice(0, 10)}…${text.slice(-6)}` : "—";
}

function formatNumber(value, digits = 2) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "—";
}

function formatDelta(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  window.setTimeout(() => toast.classList.remove("visible"), 3400);
}

function switchView(view) {
  if (!["library", "runs", "explore", "research"].includes(view)) return;
  state.activeView = view;
  $$(".nav-link").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $$(".app-view").forEach((section) => section.classList.toggle("active", section.id === `${view}-view`));
  window.history.replaceState(null, "", `#${view}`);
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (view === "explore") ensureAnalytics();
  if (view === "runs") ensureRuns();
}

function categorySymbol(family) {
  const symbols = {
    adder_reduction_association: "+",
    arithmetic_resource_sharing: "Σ",
    comparator_selection: "≷",
    decode_factoring: "D",
    mux_placement: "M",
    popcount_saturation: "#",
    priority_selection: "P",
    variable_shift: "⇄",
    width_signedness: "W",
  };
  return symbols[family] || "◇";
}

function renderCategories() {
  const families = state.overview?.families || [];
  const container = $("#category-list");
  container.replaceChildren();
  $("#category-count").textContent = `${families.length} TYPES`;
  families.forEach((family) => {
    const button = node("button", `category-item${state.family === family.id ? " active" : ""}`);
    button.type = "button";
    button.dataset.family = family.id;
    button.append(node("span", "category-icon", categorySymbol(family.id)));
    const copy = node("span", "category-copy");
    copy.append(node("strong", "", family.name), node("small", "", `${family.opportunity_count} measured opportunities`));
    button.append(copy, node("span", "category-total", String(family.case_count)));
    button.addEventListener("click", () => selectFamily(family.id));
    container.append(button);
  });
}

async function selectFamily(family) {
  state.family = family;
  state.caseId = null;
  state.caseDetail = null;
  state.candidateId = null;
  renderCategories();
  $("#detail-content").hidden = true;
  $("#detail-placeholder").hidden = false;
  await loadCases();
}

function caseStatus(item) {
  if (item.opportunity && item.covered) return ["Useful change found", "good"];
  if (item.opportunity) return ["Useful change missed", "warn"];
  if (!item.supported) return ["More evidence needed", "warn"];
  return ["No useful change", ""];
}

function renderCases() {
  const container = $("#example-list");
  container.replaceChildren();
  $("#example-count").textContent = `${state.cases.length} ${state.cases.length === 1 ? "CASE" : "CASES"}`;
  $("#example-empty").hidden = state.cases.length !== 0;
  state.cases.forEach((item, index) => {
    const button = node("button", `example-item${state.caseId === item.case_id ? " active" : ""}`);
    button.type = "button";
    const top = node("div", "example-top");
    top.append(node("code", "", item.case_id), node("span", "mini-chip", `#${String(index + 1).padStart(2, "0")}`));
    button.append(top, node("h3", "", item.opportunity ? "Measured opportunity" : "No measured improvement"));
    const meta = node("div", "example-meta");
    const [label, tone] = caseStatus(item);
    meta.append(node("span", `mini-chip ${tone}`, label));
    meta.append(node("span", "mini-chip", item.decision === "recommend" ? "Advisor selected" : "Advisor did not select"));
    if (item.out_of_domain) meta.append(node("span", "mini-chip warn", "New topology"));
    button.append(meta);
    button.addEventListener("click", () => loadCase(item.case_id));
    container.append(button);
  });
}

async function loadCases() {
  const requestToken = ++state.caseQueryToken;
  const requestFamily = state.family;
  const query = $("#case-search").value.trim();
  const params = new URLSearchParams({ family: state.family || "", limit: "200", offset: "0" });
  if (query) params.set("q", query);
  try {
    const payload = await getJSON(`/api/v1/cases?${params}`);
    if (requestToken !== state.caseQueryToken || requestFamily !== state.family) return;
    state.cases = payload.items || [];
    renderCases();
    if (!state.caseId && state.cases.length) await loadCase(state.cases[0].case_id);
  } catch (error) {
    state.cases = [];
    renderCases();
    showToast(error.message);
  }
}

function preferredCandidate(candidates) {
  return candidates.find((candidate) => candidate.measured_best && candidate.formal?.status === "equivalent")
    || candidates.find((candidate) => candidate.formal?.status === "equivalent")
    || candidates[0]
    || null;
}

async function loadCase(caseId) {
  const requestToken = ++state.caseDetailToken;
  state.caseId = caseId;
  renderCases();
  $("#detail-placeholder").hidden = true;
  $("#detail-content").hidden = false;
  $("#detail-title").textContent = "Loading evidence…";
  try {
    const detail = await getJSON(`/api/v1/cases/${encodeURIComponent(caseId)}`);
    if (requestToken !== state.caseDetailToken || caseId !== state.caseId) return;
    state.caseDetail = detail;
    const candidate = preferredCandidate(state.caseDetail.candidates || []);
    state.candidateId = candidate?.template_id || null;
    renderCaseDetail();
  } catch (error) {
    $("#detail-title").textContent = "Evidence unavailable";
    $("#detail-description").textContent = error.message;
    showToast(error.message);
  }
}

function selectedCaseCandidate() {
  return (state.caseDetail?.candidates || []).find((candidate) => candidate.template_id === state.candidateId)
    || preferredCandidate(state.caseDetail?.candidates || []);
}

function measuredResult(candidate) {
  if (candidate?.formal?.status !== "equivalent") {
    return { tone: "negative", icon: "×", title: "Candidate is not proven safe", summary: "Keep the reference RTL. Synthesis evidence cannot replace a passing equivalence proof." };
  }
  if (candidate?.synthesis?.status !== "passed") {
    return { tone: "warning", icon: "!", title: "Synthesis evidence is incomplete", summary: "Formal passed, but no complete same-flow measurement is available." };
  }
  if (candidate.measured_eligible) {
    return { tone: "positive", icon: "✓", title: "Useful change measured in this pinned flow", summary: "The candidate met the recorded benchmark target. Confirm it in the intended implementation flow before using it." };
  }
  const deltas = Object.values(candidate.measured || {}).map(Number);
  if (deltas.every((value) => Math.abs(value) < 0.01)) {
    return { tone: "neutral", icon: "=", title: "Synthesis already handles this structure", summary: "The candidate and reference produced the same recorded result. No RTL change is justified here." };
  }
  if (Number(candidate.measured_utility) < 0) {
    return { tone: "negative", icon: "×", title: "Candidate did not meet the measured target", summary: "At least one cost outweighed the benefit. This variant should not be recommended from this evidence." };
  }
  return { tone: "warning", icon: "!", title: "Mixed result in the pinned synthesis flow", summary: "The measurements trade off against each other. Keep the reference unless the project objective supports that tradeoff." };
}

function appendDefinitionList(container, entries) {
  container.replaceChildren();
  entries.forEach(([label, value]) => container.append(node("dt", "", label), node("dd", "", value ?? "—")));
}

function renderTopology(topology) {
  const entries = Object.entries(topology || {}).map(([key, value]) => [humanize(key), String(value)]);
  appendDefinitionList($("#topology-list"), entries.length ? entries : [["Topology", "Not recorded"]]);
}

function metricRow(label, baseline, candidate, delta, unit, digits) {
  const row = node("tr");
  const deltaNumber = Number(delta);
  const tone = deltaNumber > 0.05 ? "good" : deltaNumber < -0.05 ? "bad" : "neutral";
  const result = tone === "good" ? "Better" : tone === "bad" ? "Worse" : "Same";
  row.append(
    node("td", "", label),
    node("td", "", `${formatNumber(baseline, digits)}${unit}`),
    node("td", "", `${formatNumber(candidate, digits)}${unit}`),
    node("td", `delta ${tone}`, formatDelta(delta)),
  );
  const resultCell = node("td");
  resultCell.append(node("span", `table-result ${tone}`, result));
  row.append(resultCell);
  return row;
}

function renderSynthesis(candidate) {
  const body = $("#synthesis-table");
  body.replaceChildren();
  const synthesis = candidate?.synthesis || {};
  const baseline = synthesis.baseline || {};
  const modified = synthesis.candidate || {};
  const measured = candidate?.measured || {};
  body.append(
    metricRow("Critical delay", baseline.critical_delay_ps, modified.critical_delay_ps, measured.delay, " ps", 2),
    metricRow("Total cell area", baseline.area_total, modified.area_total, measured.area, "", 3),
    metricRow("Cell count", baseline.cell_count, modified.cell_count, measured.cell_count, "", 0),
  );
  $("#synthesis-flow").textContent = synthesis.flow_version ? synthesis.flow_version.toUpperCase() : "EVIDENCE UNAVAILABLE";
  $("#synthesis-note").textContent = synthesis.status === "passed"
    ? `${synthesis.liberty_name || "Recorded Liberty"}; values apply only to ${synthesis.flow_version || "the recorded Yosys/ABC flow"}, not production PPA.`
    : "No complete synthesis record is available for this candidate.";
}

function renderCaseDetail() {
  const detail = state.caseDetail;
  const candidate = selectedCaseCandidate();
  if (!detail || !candidate) return;
  const baseline = detail.rtl || {};
  const candidateRTL = candidate.rtl || {};
  $("#detail-family").textContent = detail.case.family_name;
  $("#detail-case-id").textContent = detail.case.case_id;
  $("#detail-title").textContent = `${detail.case.family_name} · ${candidate.template_id.toUpperCase()}`;
  $("#detail-description").textContent = `${detail.case.opportunity ? "Measured opportunity" : "No eligible measured improvement"} · generated ${baseline.split || "benchmark"}`;

  const select = $("#candidate-select");
  select.replaceChildren();
  (detail.candidates || []).forEach((item) => {
    const suffix = item.measured_best ? " · measured best" : "";
    select.append(new Option(`${item.template_id}${suffix}`, item.template_id));
  });
  select.value = candidate.template_id;

  const result = measuredResult(candidate);
  const banner = $("#result-banner");
  banner.className = `result-banner ${result.tone === "positive" ? "" : result.tone}`;
  $("#result-icon").textContent = result.icon;
  $("#result-title").textContent = result.title;
  $("#result-summary").textContent = result.summary;

  $("#baseline-file").textContent = baseline.file || "reference.sv";
  $("#candidate-file").textContent = candidateRTL.file || `${candidate.template_id}.sv`;
  $("#baseline-code").textContent = baseline.source || "Source unavailable.";
  $("#candidate-code").textContent = candidateRTL.source || "Candidate source unavailable.";

  const formalPassed = candidate.formal?.status === "equivalent";
  $("#formal-pill").textContent = formalPassed ? "PASSED" : humanize(candidate.formal?.status || "unavailable").toUpperCase();
  $("#formal-pill").className = `state-pill ${formalPassed ? "pass" : "fail"}`;
  $("#formal-mark").textContent = formalPassed ? "✓" : "×";
  $("#formal-title").textContent = formalPassed ? "RTL behavior proven equivalent" : "Equivalence not established";
  $("#formal-copy").textContent = formalPassed ? "The reference and modified variants match under the recorded proof semantics." : "Do not use this candidate without a passing proof.";
  $("#formal-backend").textContent = candidate.formal?.backend || "—";
  $(".proof-summary").classList.toggle("failed", !formalPassed);
  $("#case-formal-stage").classList.toggle("incomplete", !formalPassed);
  $("#case-synthesis-stage").classList.toggle("incomplete", candidate.synthesis?.status !== "passed");
  $("#case-result-stage").classList.toggle("incomplete", result.tone === "warning" || result.tone === "negative");

  renderTopology(baseline.topology);
  renderSynthesis(candidate);
  appendDefinitionList($("#provenance-list"), [
    ["Case", detail.case.case_id],
    ["Reference SHA-256", baseline.sha256],
    ["Candidate SHA-256", candidateRTL.sha256],
    ["Evidence hash", detail.provenance?.diagnostic_hash],
    ["Synthesis backend", candidate.synthesis?.backend],
    ["Yosys", candidate.synthesis?.yosys_version],
  ]);
}

function renderResearch() {
  const metrics = state.overview?.metrics || {};
  $("#metric-found").textContent = `${metrics.covered_opportunity_count ?? "—"} / ${metrics.opportunity_count ?? "—"}`;
  $("#metric-left-alone").textContent = `${metrics.correct_no_change_count ?? "—"} / ${metrics.no_change_case_count ?? "—"}`;
  $("#metric-wrong").textContent = String(metrics.harmful_count ?? "—");
  $("#research-summary-copy").textContent = `${metrics.covered_opportunity_count ?? 0} useful changes were found, while ${Math.max(0, Number(metrics.opportunity_count || 0) - Number(metrics.covered_opportunity_count || 0))} were missed. That is why ML stays outside the MVP decision path.`;
}

function svgNode(tag, attributes = {}) {
  const element = document.createElementNS("http:" + "//www.w3.org/2000/svg", tag);
  Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
  return element;
}

function populateExploreSelect(selector, values) {
  const select = $(selector);
  const first = select.options[0];
  select.replaceChildren(first);
  values.forEach((value) => select.append(new Option(humanize(value), value)));
}

async function ensureAnalytics() {
  if (state.analytics) {
    renderExplore();
    return state.analytics;
  }
  if (state.analyticsLoading) return state.analyticsLoading;
  $("#ppa-empty").hidden = false;
  $("#ppa-empty").textContent = "Loading verified measurements…";
  state.analyticsLoading = getJSON("/api/analytics/v1")
    .then((payload) => {
      state.analytics = payload;
      populateExploreSelect("#explore-profile", payload.filters?.profiles || []);
      populateExploreSelect("#explore-objective", payload.filters?.objectives || []);
      populateExploreSelect("#explore-classification", payload.filters?.classifications || []);
      populateExploreSelect("#explore-decision", payload.filters?.decisions || []);
      populateExploreSelect("#explore-transformation", payload.filters?.transformations || []);
      renderExplore();
      return payload;
    })
    .catch((error) => {
      $("#ppa-empty").hidden = false;
      $("#ppa-empty").textContent = `Could not load measured evidence: ${error.message}`;
      showToast(error.message);
      return null;
    })
    .finally(() => { state.analyticsLoading = null; });
  return state.analyticsLoading;
}

function filteredExploreRows() {
  const rows = state.analytics?.measurements || [];
  const query = state.exploreFilters.query.toLowerCase();
  return rows.filter((row) => {
    if (state.exploreFilters.profile && row.profile !== state.exploreFilters.profile) return false;
    if (state.exploreFilters.objective && row.objective !== state.exploreFilters.objective) return false;
    if (state.exploreFilters.classification && row.classification !== state.exploreFilters.classification) return false;
    if (state.exploreFilters.decision && row.decision !== state.exploreFilters.decision) return false;
    if (state.exploreFilters.transformation && row.transformation_id !== state.exploreFilters.transformation) return false;
    if (!query) return true;
    return [row.run_id, row.top, row.candidate_id, row.transformation_id, row.configuration_id, row.reference_id]
      .some((value) => String(value || "").toLowerCase().includes(query));
  });
}

function decisionTone(decision) {
  return {
    measured_improvement: "good",
    synthesis_handles: "neutral",
    flow_dependent: "warn",
    regression: "bad",
  }[decision] || "neutral";
}

function classificationTone(classification) {
  return {
    improved: "good",
    neutral: "neutral",
    regressed: "bad",
  }[classification] || "neutral";
}

function showPpaTooltip(row, target, pointerEvent) {
  const tooltip = $("#ppa-tooltip");
  tooltip.replaceChildren();
  tooltip.append(
    node("strong", "", row.top || row.run_id),
    node("span", "tooltip-profile", `${humanize(row.profile)} profile · ${humanize(row.classification)}`),
    node("span", "tooltip-decision", `Candidate decision · ${humanize(row.decision)}`),
    node("code", "", row.candidate_id),
    node("span", "", `Delay ${formatDelta(row.delay_improvement_percent)} · Area ${formatDelta(row.area_improvement_percent)} · Cells ${formatDelta(row.cell_count_improvement_percent)}`),
    node("p", "tooltip-reason", row.classification_reason || "No classification explanation recorded."),
    node("p", "tooltip-decision-reason", row.candidate_decision_reason || ""),
    node("small", "", row.drill_available ? "Open the complete run evidence" : `${row.configuration_id || "Aggregate record"} · ${row.artifact_path}`),
  );
  const wrap = $("#ppa-chart").getBoundingClientRect();
  const bounds = target.getBoundingClientRect();
  const x = pointerEvent?.clientX ?? (bounds.left + bounds.width / 2);
  const y = pointerEvent?.clientY ?? bounds.top;
  tooltip.style.left = `${Math.max(8, Math.min(wrap.width - 298, x - wrap.left + 12))}px`;
  tooltip.style.top = `${Math.max(8, y - wrap.top - 140)}px`;
  tooltip.hidden = false;
}

function hidePpaTooltip() {
  $("#ppa-tooltip").hidden = true;
}

function openExploreRun(runId) {
  switchView("runs");
  loadRun(runId);
}

function scatterPoint(row, x, y) {
  const attributes = {
    class: `scatter-point ${classificationTone(row.classification)}`,
    transform: `translate(${x} ${y})`,
    tabindex: "0",
    role: row.drill_available ? "button" : "img",
    "aria-label": `${row.top || row.run_id}, ${humanize(row.profile)} profile ${humanize(row.classification)}, delay ${formatDelta(row.delay_improvement_percent)}, area ${formatDelta(row.area_improvement_percent)}, candidate ${humanize(row.decision)}. ${row.classification_reason || ""}`,
  };
  const group = svgNode("g", attributes);
  group.append(svgNode("circle", { class: "point-hit", r: 13 }));
  if (row.classification === "regressed") {
    group.append(svgNode("polygon", { class: "point-mark", points: "0,-7 6,5 -6,5" }));
  } else if (row.classification === "neutral") {
    group.append(svgNode("rect", { class: "point-mark", x: -5, y: -5, width: 10, height: 10, rx: 1 }));
  } else {
    group.append(svgNode("circle", { class: "point-mark", r: 5.5 }));
  }
  group.addEventListener("pointerenter", (event) => showPpaTooltip(row, group, event));
  group.addEventListener("pointermove", (event) => showPpaTooltip(row, group, event));
  group.addEventListener("pointerleave", hidePpaTooltip);
  group.addEventListener("focus", () => showPpaTooltip(row, group));
  group.addEventListener("blur", hidePpaTooltip);
  if (row.drill_available) {
    group.addEventListener("click", () => openExploreRun(row.run_id));
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openExploreRun(row.run_id);
      }
    });
  }
  return group;
}

function scatterDomain(values) {
  const finite = values.map(Number).filter(Number.isFinite);
  if (!finite.length) return [-1, 1];
  let minimum = Math.min(0, ...finite);
  let maximum = Math.max(0, ...finite);
  if (minimum === maximum) {
    minimum -= 1;
    maximum += 1;
  }
  const padding = Math.max((maximum - minimum) * 0.1, 0.5);
  return [minimum - padding, maximum + padding];
}

function classificationPolicyForRows(rows) {
  const objectives = [...new Set(rows.map((row) => row.objective).filter(Boolean))];
  if (objectives.length !== 1) return null;
  return state.analytics?.classification_policies?.[objectives[0]] || null;
}

function thresholdLabel(boundary) {
  const operator = boundary.operator === "<=" ? "≤" : "<";
  const value = Number(boundary.value);
  return `${boundary.label} ${operator} ${value > 0 ? "+" : ""}${value.toFixed(0)}%`;
}

function renderScatter(rows) {
  const svg = $("#ppa-scatter");
  svg.replaceChildren();
  hidePpaTooltip();
  const plotted = rows.filter((row) => Number.isFinite(Number(row.area_improvement_percent)) && Number.isFinite(Number(row.delay_improvement_percent)));
  $("#ppa-row-count").textContent = `${plotted.length} ${plotted.length === 1 ? "POINT" : "POINTS"}`;
  $("#ppa-empty").hidden = plotted.length !== 0;
  $("#ppa-empty").textContent = "No measured observations match these filters.";
  svg.hidden = plotted.length === 0;
  if (!plotted.length) {
    $("#ppa-policy-note").textContent = "No profile classifications match these filters.";
    return;
  }

  const width = 760;
  const height = 430;
  const margin = { top: 24, right: 24, bottom: 60, left: 72 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const policy = classificationPolicyForRows(plotted);
  const boundaries = policy?.regression_boundaries || [];
  const xBoundaries = boundaries.filter((boundary) => boundary.axis === "x").map((boundary) => boundary.value);
  const yBoundaries = boundaries.filter((boundary) => boundary.axis === "y").map((boundary) => boundary.value);
  const [xMin, xMax] = scatterDomain([...plotted.map((row) => row.area_improvement_percent), ...xBoundaries]);
  const [yMin, yMax] = scatterDomain([...plotted.map((row) => row.delay_improvement_percent), ...yBoundaries]);
  const xScale = (value) => margin.left + (Number(value) - xMin) / (xMax - xMin) * plotWidth;
  const yScale = (value) => margin.top + plotHeight - (Number(value) - yMin) / (yMax - yMin) * plotHeight;

  const policyNote = $("#ppa-policy-note");
  if (policy) {
    policyNote.textContent = `${policy.label} profile regression rule: ${policy.regressed}`;
  } else {
    policyNote.textContent = "Select one objective to display its regression boundaries.";
  }

  const thresholdZones = svgNode("g", { class: "threshold-zones" });
  boundaries.forEach((boundary) => {
    if (boundary.axis === "x") {
      const x = xScale(boundary.value);
      thresholdZones.append(svgNode("rect", {
        class: "threshold-zone",
        x: margin.left,
        y: margin.top,
        width: Math.max(0, x - margin.left),
        height: plotHeight,
      }));
    } else {
      const y = yScale(boundary.value);
      thresholdZones.append(svgNode("rect", {
        class: "threshold-zone",
        x: margin.left,
        y,
        width: plotWidth,
        height: Math.max(0, margin.top + plotHeight - y),
      }));
    }
  });
  svg.append(thresholdZones);

  const grid = svgNode("g", { class: "chart-grid" });
  for (let index = 0; index <= 4; index += 1) {
    const xValue = xMin + (xMax - xMin) * index / 4;
    const yValue = yMin + (yMax - yMin) * index / 4;
    const x = xScale(xValue);
    const y = yScale(yValue);
    grid.append(svgNode("line", { x1: x, y1: margin.top, x2: x, y2: margin.top + plotHeight }));
    grid.append(svgNode("line", { x1: margin.left, y1: y, x2: margin.left + plotWidth, y2: y }));
    const xLabel = svgNode("text", { x, y: height - 33, "text-anchor": "middle" });
    xLabel.textContent = `${xValue > 0 ? "+" : ""}${xValue.toFixed(1)}%`;
    grid.append(xLabel);
    const yLabel = svgNode("text", { x: margin.left - 12, y: y + 3, "text-anchor": "end" });
    yLabel.textContent = `${yValue > 0 ? "+" : ""}${yValue.toFixed(1)}%`;
    grid.append(yLabel);
  }
  svg.append(grid);
  const thresholdLines = svgNode("g", { class: "threshold-lines" });
  boundaries.forEach((boundary, index) => {
    if (boundary.axis === "x") {
      const x = xScale(boundary.value);
      thresholdLines.append(svgNode("line", { x1: x, y1: margin.top, x2: x, y2: margin.top + plotHeight }));
      const label = svgNode("text", { x: x + 5, y: margin.top + 13 + index * 12 });
      label.textContent = thresholdLabel(boundary);
      thresholdLines.append(label);
    } else {
      const y = yScale(boundary.value);
      thresholdLines.append(svgNode("line", { x1: margin.left, y1: y, x2: margin.left + plotWidth, y2: y }));
      const label = svgNode("text", { x: margin.left + 7, y: y - 6 });
      label.textContent = thresholdLabel(boundary);
      thresholdLines.append(label);
    }
  });
  svg.append(thresholdLines);
  svg.append(svgNode("line", { class: "zero-line", x1: xScale(0), y1: margin.top, x2: xScale(0), y2: margin.top + plotHeight }));
  svg.append(svgNode("line", { class: "zero-line", x1: margin.left, y1: yScale(0), x2: margin.left + plotWidth, y2: yScale(0) }));
  const xTitle = svgNode("text", { class: "axis-title", x: margin.left + plotWidth / 2, y: height - 7, "text-anchor": "middle" });
  xTitle.textContent = "Total cell-area improvement";
  svg.append(xTitle);
  const yTitle = svgNode("text", { class: "axis-title", x: 15, y: margin.top + plotHeight / 2, transform: `rotate(-90 15 ${margin.top + plotHeight / 2})`, "text-anchor": "middle" });
  yTitle.textContent = "Critical-delay improvement";
  svg.append(yTitle);
  const marks = svgNode("g", { class: "chart-marks" });
  plotted.forEach((row) => marks.append(scatterPoint(row, xScale(row.area_improvement_percent), yScale(row.delay_improvement_percent))));
  svg.append(marks);
}

function renderExploreLegend(rows) {
  const legend = $("#ppa-legend");
  legend.replaceChildren();
  legend.append(node("span", "legend-label", "Point classification"));
  const available = new Set(state.analytics?.filters?.classifications || rows.map((row) => row.classification));
  ["improved", "neutral", "regressed"].forEach((classification) => {
    if (!available.has(classification)) return;
    const active = state.exploreFilters.classification === classification ? " active" : "";
    const button = node("button", `legend-item ${classificationTone(classification)}${active}`);
    button.type = "button";
    button.append(node("span", "", ""), node("b", "", humanize(classification)));
    button.addEventListener("click", () => {
      state.exploreFilters.classification = classification;
      $("#explore-classification").value = classification;
      renderExplore();
    });
    legend.append(button);
  });
}

function renderOutcomeBars(rows) {
  const container = $("#outcome-bars");
  container.replaceChildren();
  const byCandidate = new Map();
  rows.forEach((row) => byCandidate.set(`${row.run_id}:${row.candidate_id}`, row));
  const counts = {};
  byCandidate.forEach((row) => { counts[row.decision] = (counts[row.decision] || 0) + 1; });
  const maximum = Math.max(1, ...Object.values(counts));
  ["measured_improvement", "synthesis_handles", "flow_dependent", "regression"].forEach((decision) => {
    if (!counts[decision]) return;
    const item = node("div", "outcome-bar");
    const label = node("div", "outcome-bar-label");
    label.append(node("span", "", humanize(decision)), node("strong", "", String(counts[decision])));
    const track = node("div", "outcome-bar-track");
    const fill = node("span", decisionTone(decision));
    fill.style.width = `${counts[decision] / maximum * 100}%`;
    track.append(fill);
    item.append(label, track);
    container.append(item);
  });
  if (!container.children.length) container.append(node("p", "section-note", "No measured candidates match these filters."));
}

function renderReproducibility() {
  const record = state.analytics?.reproducibility?.[0];
  const list = $("#repeat-facts");
  list.replaceChildren();
  if (!record) {
    $("#repeat-status").textContent = "UNAVAILABLE";
    $("#repeat-note").textContent = "No family reproducibility record is available.";
    return;
  }
  const summary = record.summary || {};
  $("#repeat-status").textContent = humanize(record.status).toUpperCase();
  [["Configurations", summary.cohort_configuration_count], ["Exact measured results", summary.exact_measured_result_count], ["Repeated limitations", summary.repeated_measurement_limitation_count], ["Mismatches", summary.mismatch_count]].forEach(([label, value]) => list.append(node("dt", "", label), node("dd", "", value ?? "—")));
  $("#repeat-note").textContent = `${record.study_id} · ${shortHash(record.semantic_hash)} · ${record.artifact_path}`;
}

function exploreTableRow(row) {
  const tableRow = node("tr", "explore-measurement-row");
  if (row.drill_available) tableRow.tabIndex = 0;
  const runCell = node("td");
  runCell.append(
    node("code", "", row.configuration_id || row.run_id),
    node("small", "", row.reference_id || row.top || "Unknown source"),
  );
  tableRow.append(
    runCell,
    node("td", "", humanize(row.transformation_id)),
    node("td", "", humanize(row.profile)),
  );
  const classificationCell = node("td");
  classificationCell.append(node("span", `mini-chip ${classificationTone(row.classification)}`, humanize(row.classification)));
  tableRow.append(
    classificationCell,
    node("td", `delta ${Number(row.delay_improvement_percent) > 0 ? "good" : Number(row.delay_improvement_percent) < 0 ? "bad" : "neutral"}`, formatDelta(row.delay_improvement_percent)),
    node("td", `delta ${Number(row.area_improvement_percent) > 0 ? "good" : Number(row.area_improvement_percent) < 0 ? "bad" : "neutral"}`, formatDelta(row.area_improvement_percent)),
    node("td", `delta ${Number(row.cell_count_improvement_percent) > 0 ? "good" : Number(row.cell_count_improvement_percent) < 0 ? "bad" : "neutral"}`, formatDelta(row.cell_count_improvement_percent)),
    node("td", "classification-reason", row.classification_reason || "—"),
  );
  const outcomeCell = node("td");
  outcomeCell.append(
    node("span", `mini-chip ${decisionTone(row.decision)}`, humanize(row.decision)),
    node("small", "candidate-decision-reason", row.candidate_decision_reason || ""),
  );
  tableRow.append(outcomeCell);
  if (row.drill_available) {
    tableRow.addEventListener("click", () => openExploreRun(row.run_id));
    tableRow.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openExploreRun(row.run_id);
      }
    });
  }
  return tableRow;
}

function renderExploreTable(rows) {
  const body = $("#explore-table-body");
  body.replaceChildren();
  const sorted = [...rows].sort((left, right) => Number(right.delay_improvement_percent || 0) - Number(left.delay_improvement_percent || 0));
  sorted.forEach((row) => body.append(exploreTableRow(row)));
  if (!sorted.length) {
    const row = node("tr");
    const cell = node("td", "", "No measured observations match these filters.");
    cell.colSpan = 9;
    row.append(cell);
    body.append(row);
  }
  $("#explore-table-count").textContent = `${sorted.length} ${sorted.length === 1 ? "ROW" : "ROWS"}`;
}

function renderExplore() {
  if (!state.analytics) return;
  const rows = filteredExploreRows();
  const runIds = new Set(rows.map((row) => row.run_id));
  const candidateIds = new Set(rows.map((row) => `${row.run_id}:${row.candidate_id}`));
  $("#explore-run-count").textContent = String(runIds.size);
  $("#explore-candidate-count").textContent = String(candidateIds.size);
  $("#explore-observation-count").textContent = String(rows.length);
  const reproducibility = state.analytics.reproducibility?.[0]?.summary;
  const rate = Number(reproducibility?.measured_exact_match_rate);
  $("#explore-repeat-rate").textContent = Number.isFinite(rate) ? `${(rate * 100).toFixed(1)}%` : "—";
  renderScatter(rows);
  renderExploreLegend(rows);
  renderOutcomeBars(rows);
  renderReproducibility();
  renderExploreTable(rows);
}

function csvCell(value) {
  let text = value === null || value === undefined ? "" : String(value);
  if (/^[=+\-@]/.test(text) && !Number.isFinite(Number(text))) text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
}

function exportExploreCSV() {
  const rows = filteredExploreRows();
  if (!rows.length) {
    showToast("No filtered measurements to export.");
    return;
  }
  const columns = ["source_kind", "study_id", "reference_id", "configuration_id", "repeat_id", "run_id", "candidate_id", "top", "transformation_id", "objective", "profile", "classification", "classification_reason", "decision", "candidate_decision_reason", "baseline_delay_ps", "candidate_delay_ps", "delay_improvement_percent", "baseline_area", "candidate_area", "area_improvement_percent", "baseline_cell_count", "candidate_cell_count", "cell_count_improvement_percent", "formal_status", "safe", "recipe_hash", "measurement_semantic_hash", "artifact_path"];
  const content = [columns.map(csvCell).join(","), ...rows.map((row) => columns.map((column) => csvCell(row[column])).join(","))].join("\n");
  const url = URL.createObjectURL(new Blob([`${content}\n`], { type: "text/csv;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "rtl-advisor-measurements.csv";
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function runTone(run) {
  if (run.decision === "measured_improvement") return "good";
  if (run.decision === "regression" || run.state === "failed") return "bad";
  if (run.decision === "flow_dependent" || run.state === "incomplete") return "warn";
  return "";
}

function renderRuns() {
  const container = $("#run-list");
  container.replaceChildren();
  $("#run-count").textContent = String(state.runs.length);
  $("#run-list-count").textContent = `${state.runs.length} RUNS`;
  state.runs.forEach((run) => {
    const button = node("button", `run-item${state.runId === run.run_id ? " active" : ""}`);
    button.type = "button";
    const top = node("div", "run-top");
    top.append(node("code", "", run.run_id), node("span", `mini-chip ${runTone(run)}`, humanize(run.state)));
    button.append(top, node("h3", "", run.top || "Unknown top"));
    const meta = node("div", "run-meta");
    meta.append(node("span", "mini-chip", run.objective), node("span", "mini-chip", run.outcome?.label || humanize(run.decision)));
    button.append(meta);
    button.addEventListener("click", () => loadRun(run.run_id));
    container.append(button);
  });
}

async function ensureRuns() {
  if (state.runsLoaded) {
    renderRuns();
    return state.runs;
  }
  if (state.runsLoading) return state.runsLoading;
  const container = $("#run-list");
  container.replaceChildren(node("div", "detail-placeholder", "Loading run index…"));
  state.runsLoading = getJSON("/api/runs/v1")
    .then((payload) => {
      state.runs = payload.items || [];
      state.runsLoaded = true;
      renderRuns();
      return state.runs;
    })
    .catch((error) => {
      container.replaceChildren(node("div", "detail-placeholder", error.message));
      showToast(error.message);
      return [];
    })
    .finally(() => { state.runsLoading = null; });
  return state.runsLoading;
}

async function loadRun(runId) {
  state.runId = runId;
  renderRuns();
  $("#run-placeholder").hidden = true;
  $("#run-content").hidden = false;
  $("#run-content").replaceChildren(node("div", "detail-placeholder", "Loading run evidence…"));
  try {
    const [detail, diffs] = await Promise.all([
      getJSON(`/api/runs/v1/${encodeURIComponent(runId)}`),
      getJSON(`/api/runs/v1/${encodeURIComponent(runId)}/diff`),
    ]);
    renderRunDetail(detail.run, diffs.items || []);
  } catch (error) {
    $("#run-content").replaceChildren(node("div", "detail-placeholder", error.message));
    showToast(error.message);
  }
}

function addRunFacts(container, run) {
  const facts = node("div", "run-facts");
  [["Run", run.run_id], ["Top", run.top], ["Objective", humanize(run.objective)]].forEach(([label, value]) => {
    const item = node("div", "run-fact");
    item.append(node("span", "", label), label === "Run" ? node("code", "", value) : node("strong", "", value));
    facts.append(item);
  });
  container.append(facts);
}

function renderRunStages(container, stages) {
  const rail = node("ol", "stage-rail");
  (stages || []).forEach((stage) => {
    const complete = stage.status === "complete";
    const item = node("li", complete ? "" : "incomplete");
    item.append(node("span", "", complete ? "✓" : "—"), node("b", "", stage.label), node("small", "", humanize(stage.status)));
    rail.append(item);
  });
  container.append(rail);
}

function renderRecipeCards(container, measurement) {
  const grid = node("div", "recipe-grid");
  const recipes = measurement?.measurements || {};
  ["standard", "stronger"].forEach((name) => {
    const recipe = recipes[name];
    const card = node("article", "recipe-card");
    const header = node("header");
    header.append(node("strong", "", humanize(name)), node("span", "", recipe ? humanize(recipe.classification) : "Not run"));
    card.append(header);
    if (recipe) {
      const list = node("dl");
      [["Metric", "Reference", "Modified"], ["Delay", `${formatNumber(recipe.baseline?.metrics?.critical_delay_ps)} ps`, `${formatNumber(recipe.candidate?.metrics?.critical_delay_ps)} ps`], ["Area", formatNumber(recipe.baseline?.metrics?.area_total, 3), formatNumber(recipe.candidate?.metrics?.area_total, 3)], ["Cells", formatNumber(recipe.baseline?.metrics?.cell_count, 0), formatNumber(recipe.candidate?.metrics?.cell_count, 0)]].forEach(([label, first, second]) => list.append(node("dt", "", label), node("dd", "", first), node("dd", "", second)));
      card.append(list);
    } else card.append(node("p", "section-note", "No synthesis measurement recorded."));
    grid.append(card);
  });
  container.append(grid);
}

function renderRunDetail(run, diffs) {
  const container = $("#run-content");
  container.replaceChildren();
  const outcome = node("article", "run-outcome");
  const banner = node("div", `result-banner ${run.outcome?.tone === "positive" ? "" : run.outcome?.tone || "neutral"}`);
  const toneIcon = run.outcome?.tone === "positive" ? "✓" : run.outcome?.tone === "negative" ? "×" : run.outcome?.tone === "warning" ? "!" : "=";
  banner.append(node("span", "result-icon", toneIcon));
  const copy = node("div");
  copy.append(node("p", "eyebrow", "FINAL RESULT"), node("h3", "", run.outcome?.label || humanize(run.decision)), node("p", "", run.outcome?.summary || "Run evidence is incomplete."));
  banner.append(copy);
  outcome.append(banner);
  container.append(outcome);
  addRunFacts(container, run);
  renderRunStages(container, run.stages);

  const candidate = run.candidates?.[0];
  const finding = candidate?.finding || run.findings?.[0];
  const sourceSection = node("section", "section-block");
  const sourceHeading = node("div", "section-heading");
  const sourceCopy = node("div");
  sourceCopy.append(node("p", "eyebrow", "SOURCE REVIEW"), node("h3", "", finding ? "Candidate expression" : "No supported source pattern"));
  sourceHeading.append(sourceCopy, node("span", "evidence-chip", finding?.source?.line ? `LINE ${finding.source.line}` : "REVIEWED"));
  sourceSection.append(sourceHeading, node("p", "section-note", finding?.reason || "The MVP rule did not find a safe unsigned, equal-width addition chain."));
  if (finding) {
    const pair = node("div", "run-expression");
    [["REFERENCE", finding.original_expression], ["CANDIDATE", finding.replacement_expression]].forEach(([label, value]) => {
      const card = node("div", "expression-card");
      card.append(node("span", "", label), node("code", "", value));
      pair.append(card);
    });
    sourceSection.append(pair);
  }
  container.append(sourceSection);

  if (candidate) {
    const proof = node("section", "section-block");
    const formalPassed = candidate.formal?.status === "formal_passed" || candidate.formal?.formal?.status === "passed";
    const heading = node("div", "section-heading");
    const headingCopy = node("div");
    headingCopy.append(node("p", "eyebrow", "LOGICAL SAFETY"), node("h3", "", "Formal equivalence"));
    heading.append(headingCopy, node("span", `state-pill ${formalPassed ? "pass" : "fail"}`, formalPassed ? "PASSED" : "NOT PASSED"));
    proof.append(heading, node("p", "section-note", candidate.formal?.formal?.semantics || "Two-state combinational RTL proof."));
    container.append(proof);

    const synthesis = node("section", "section-block");
    const synthesisHeading = node("div", "section-heading");
    const synthesisCopy = node("div");
    synthesisCopy.append(node("p", "eyebrow", "SAME-FLOW COMPARISON"), node("h3", "", "Yosys / ABC synthesis"));
    synthesisHeading.append(synthesisCopy, node("span", "evidence-chip", candidate.measurement?.decision ? humanize(candidate.measurement.decision).toUpperCase() : "NOT RUN"));
    synthesis.append(synthesisHeading);
    renderRecipeCards(synthesis, candidate.measurement);
    container.append(synthesis);
  }

  if (diffs.length) {
    const diffSection = node("section", "section-block");
    const heading = node("div", "section-heading");
    const headingCopy = node("div");
    headingCopy.append(node("p", "eyebrow", "ISOLATED CHANGE"), node("h3", "", "Candidate diff"));
    heading.append(headingCopy, node("span", "evidence-chip", shortHash(diffs[0].sha256)));
    diffSection.append(heading, node("pre", "diff-box", diffs[0].content));
    container.append(diffSection);
  }
}

async function initialize() {
  try {
    const overview = await getJSON("/api/v1/overview");
    state.overview = overview;
    state.family = overview.families?.find((family) => family.id === "adder_reduction_association")?.id || overview.families?.[0]?.id || null;
    renderCategories();
    renderRuns();
    renderResearch();
    $("#loading-state").classList.add("hidden");
    const requested = window.location.hash.slice(1);
    switchView(["library", "runs", "explore", "research"].includes(requested) ? requested : "library");
    await loadCases();
  } catch (error) {
    $("#loading-state p").textContent = `Could not load local evidence: ${error.message}`;
    showToast(error.message);
  }
}

$$(".nav-link").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
$("[data-view-link='library']").addEventListener("click", (event) => { event.preventDefault(); switchView("library"); });
$("#candidate-select").addEventListener("change", (event) => { state.candidateId = event.target.value; renderCaseDetail(); });
$("#case-search").addEventListener("input", () => {
  window.clearTimeout(state.searchTimer);
  state.searchTimer = window.setTimeout(() => { state.caseId = null; loadCases(); }, 220);
});
[["#explore-profile", "profile"], ["#explore-objective", "objective"], ["#explore-classification", "classification"], ["#explore-decision", "decision"], ["#explore-transformation", "transformation"]].forEach(([selector, key]) => {
  $(selector).addEventListener("change", (event) => {
    state.exploreFilters[key] = event.target.value;
    renderExplore();
  });
});
$("#explore-search").addEventListener("input", (event) => {
  state.exploreFilters.query = event.target.value.trim();
  renderExplore();
});
$("#explore-reset").addEventListener("click", () => {
  state.exploreFilters = { profile: "", objective: "", classification: "", decision: "", transformation: "", query: "" };
  ["#explore-profile", "#explore-objective", "#explore-classification", "#explore-decision", "#explore-transformation", "#explore-search"].forEach((selector) => { $(selector).value = ""; });
  renderExplore();
});
$("#explore-export").addEventListener("click", exportExploreCSV);
window.addEventListener("hashchange", () => {
  const view = window.location.hash.slice(1);
  if (view && view !== state.activeView) switchView(view);
});

initialize();
