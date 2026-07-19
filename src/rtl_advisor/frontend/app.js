"use strict";

const state = {
  overview: null,
  runs: [],
  selectedRunId: null,
  runDetail: null,
  runDiffs: [],
  runArtifacts: null,
  selectedCandidateId: null,
  evidenceTab: "commands",
  currentView: "runs",
  pageSize: 25,
  offset: 0,
  caseTotal: 0,
  filters: { family: "", category: "", q: "" },
  searchTimer: null,
};

const viewMeta = {
  runs: ["READ-ONLY EVIDENCE", "Analysis runs"],
  overview: ["MODEL EVALUATION", "Model readiness"],
  cases: ["EVALUATION DATA", "Evaluation cases"],
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
  const pill = $("#global-gate-pill");
  if (view === "runs") {
    pill.replaceChildren(node("span", "pulse-dot green"), node("span", "", "Local · read only"));
  } else {
    pill.replaceChildren(node("span", "pulse-dot amber"), node("span", "", "V2.2 · research only"));
  }
  if (view === "cases") loadCases();
  if (view === "runs") loadRuns(true);
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
  $("#nav-case-count").textContent = overview.evidence.case_count;
  renderMetrics(overview);
  renderFamilies(overview.families);
  renderGates(overview.gates);
  renderFailures(overview.failure_categories);
}

function shortHash(value) {
  if (!value) return "—";
  const text = String(value);
  return text.length > 16 ? `${text.slice(0, 12)}…${text.slice(-4)}` : text;
}

function formatTime(value) {
  if (!value) return "Unknown time";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value) : new Intl.DateTimeFormat(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  }).format(date);
}

function renderRunList(payload) {
  state.runs = payload.items || [];
  $("#nav-run-count").textContent = payload.count;
  $("#run-list-count").textContent = `${payload.count} ${payload.count === 1 ? "RUN" : "RUNS"}`;
  $("#source-status").textContent = `${payload.count} valid run${payload.count === 1 ? "" : "s"} · ${payload.invalid.length} invalid`;
  const list = $("#run-list");
  list.replaceChildren();
  $("#run-empty").hidden = payload.count !== 0;

  state.runs.forEach((run) => {
    const button = node("button", `run-list-item tone-${run.outcome.tone}`);
    button.type = "button";
    button.dataset.runId = run.run_id;
    button.classList.toggle("active", run.run_id === state.selectedRunId);
    const top = node("div", "run-list-top");
    top.append(node("span", `run-state state-${run.state}`, run.outcome.label), node("time", "", formatTime(run.updated_at)));
    const title = node("strong", "", run.top || "Unknown top");
    const meta = node("div", "run-list-meta");
    meta.append(node("code", "", run.run_id), node("span", "", `${humanize(run.objective)} · ${run.candidate_count} candidate${run.candidate_count === 1 ? "" : "s"}`));
    button.append(top, title, meta);
    button.addEventListener("click", () => selectRun(run.run_id));
    list.append(button);
  });

  const invalid = $("#run-invalid");
  if (payload.invalid.length) {
    invalid.hidden = false;
    invalid.textContent = `${payload.invalid.length} run director${payload.invalid.length === 1 ? "y was" : "ies were"} hidden because its evidence failed validation.`;
  } else {
    invalid.hidden = true;
    invalid.textContent = "";
  }

  const validRunIds = new Set(state.runs.map((run) => run.run_id));
  if (state.selectedRunId && !validRunIds.has(state.selectedRunId)) {
    clearRunDetail(payload.invalid.length
      ? "The selected run is no longer available because its evidence failed validation."
      : "Select a run to review its evidence.");
  }
}

function clearRunDetail(message) {
  state.selectedRunId = null;
  state.selectedCandidateId = null;
  state.runDetail = null;
  state.runDiffs = [];
  state.runArtifacts = null;
  $("#run-detail").hidden = true;
  $("#run-detail-empty").hidden = false;
  $("#run-detail-empty p").textContent = message;
}

function stageSymbol(status) {
  return status === "complete" ? "✓" : status === "failed" ? "×" : status === "blocked" ? "—" : status === "active" ? "•" : "○";
}

function renderStages(stages) {
  const rail = $("#stage-rail");
  rail.replaceChildren();
  stages.forEach((stage) => {
    const item = node("li", `stage-item stage-${stage.status}`);
    item.append(node("span", "stage-marker", stageSymbol(stage.status)), node("strong", "", stage.label), node("small", "", humanize(stage.status)));
    rail.append(item);
  });
}

function selectedCandidate() {
  const candidates = state.runDetail?.candidates || [];
  return candidates.find((item) => item.candidate_id === state.selectedCandidateId) || candidates[0] || null;
}

function renderFinding(run, candidate) {
  const finding = candidate?.finding || run.findings?.[0];
  const status = $("#finding-status");
  const content = $("#finding-content");
  content.replaceChildren();
  if (!finding) {
    status.textContent = "NO SUPPORTED SITE";
    status.className = "status-chip muted";
    content.append(node("p", "empty-copy", "The conservative MVP rule found no eligible unsigned, equal-width combinational addition chain."));
    return;
  }
  status.textContent = "CANDIDATE TO EVALUATE";
  status.className = "status-chip progress";
  const source = finding.source || {};
  const location = node("div", "finding-location");
  location.append(node("code", "", `${source.file || "source"}:${source.line || "?"}`), node("span", "", finding.transformation_id ? humanize(finding.transformation_id) : "Adder reassociation"));
  const reason = node("p", "finding-reason", finding.reason || "A balanced expression may reduce arithmetic depth.");
  const expression = node("div", "expression-pair");
  const before = node("div");
  before.append(node("span", "", "ORIGINAL"), node("code", "", finding.original_expression || "—"));
  const after = node("div");
  after.append(node("span", "", "CANDIDATE"), node("code", "", finding.replacement_expression || "—"));
  expression.append(before, after);
  content.append(location, reason, expression);
}

function renderFormal(candidate) {
  const status = $("#formal-status");
  const content = $("#formal-content");
  content.replaceChildren();
  const verification = candidate?.formal;
  const formal = verification?.formal || {};
  if (!verification) {
    status.textContent = "NOT RUN";
    status.className = "status-chip muted";
    content.append(node("p", "empty-copy", candidate ? "Formal verification is the next required stage." : "Prepare a candidate before formal verification."));
    return;
  }
  const passed = verification.status === "formal_passed";
  const failed = verification.status === "formal_failed";
  status.textContent = passed ? "PASSED" : failed ? "FAILED" : "INCONCLUSIVE";
  status.className = `status-chip ${passed ? "positive" : failed ? "negative" : "warning"}`;
  const result = node("div", `proof-result ${passed ? "positive" : failed ? "negative" : "warning"}`);
  result.append(node("span", "proof-icon", passed ? "✓" : failed ? "×" : "!"));
  const copy = node("div");
  copy.append(node("strong", "", passed ? "Equivalent under recorded semantics" : failed ? "Equivalence was not proven" : "Proof did not complete"));
  copy.append(node("p", "", formal.detail || verification.limitations?.[0] || "Two-state combinational RTL equivalence using Yosys."));
  result.append(copy);
  const facts = node("dl", "proof-facts");
  [["Backend", formal.backend || "Yosys equivalence"], ["Semantics", formal.semantics || "Two-state RTL"], ["Evidence hash", shortHash(verification.semantic_hash)]].forEach(([label, value]) => {
    facts.append(node("dt", "", label), node("dd", "", value));
  });
  content.append(result, facts);
}

function metricValue(value, suffix = "") {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(number >= 100 ? 1 : 2)}${suffix}` : "—";
}

function renderMeasurements(candidate) {
  const grid = $("#recipe-grid");
  grid.replaceChildren();
  const measurement = candidate?.measurement;
  const measurementFailures = candidate?.measurement_failures || [];
  const profiles = measurement?.measurements || {};
  if (!measurement && measurementFailures.length) {
    measurementFailures.forEach((failure) => {
      const error = failure?.error || {};
      const card = node("article", "recipe-card result-regressed");
      const head = node("div", "recipe-head");
      head.append(node("strong", "", "Synthesis failed"), node("span", "", error.code ? humanize(error.code) : "Tool error"));
      card.append(head, node("p", "empty-copy", error.message || "The synthesis attempt failed without a recorded error message."));
      grid.append(card);
    });
  }
  ["standard", "stronger"].forEach((profileName) => {
    const profile = profiles[profileName];
    const card = node("article", `recipe-card ${profile ? `result-${profile.classification}` : ""}`);
    const head = node("div", "recipe-head");
    head.append(node("strong", "", humanize(profileName)), node("span", "", profile ? humanize(profile.classification) : "Not run"));
    card.append(head);
    if (!profile) {
      card.append(node("p", "empty-copy", "No synthesis evidence recorded."));
    } else {
      const table = node("div", "recipe-metrics");
      const comparison = profile.comparison || {};
      [["Delay", comparison.critical_delay_ps, " ps"], ["Area", comparison.area_total, ""], ["Cells", comparison.cell_count, ""]].forEach(([label, metric, suffix]) => {
        const row = node("div");
        row.append(node("span", "", label));
        row.append(node("code", "", `${metricValue(metric?.baseline, suffix)} → ${metricValue(metric?.candidate, suffix)}`));
        const improvement = Number(metric?.improvement_percent);
        row.append(node("strong", Number.isFinite(improvement) && improvement > 0 ? "metric-positive" : Number.isFinite(improvement) && improvement < 0 ? "metric-negative" : "", Number.isFinite(improvement) ? `${improvement > 0 ? "+" : ""}${improvement.toFixed(2)}%` : "—"));
        table.append(row);
      });
      card.append(table);
      const recipeHash = profile.recipe?.recipe_hash;
      if (recipeHash) card.append(node("code", "recipe-hash", `recipe ${shortHash(recipeHash)}`));
    }
    grid.append(card);
  });
  $("#measurement-caveat").textContent = measurement?.limitations?.[0] || "Measurements apply only to the pinned Yosys/ABC recipes and are not target-flow PPA.";
}

function renderDiff(candidate) {
  const item = state.runDiffs.find((entry) => entry.candidate_id === candidate?.candidate_id);
  $("#candidate-diff").textContent = item?.content || (candidate ? "The candidate record exists, but no readable diff artifact was found." : "No candidate diff has been recorded.");
}

function renderEvidence() {
  $$(".evidence-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.evidence === state.evidenceTab));
  const list = $("#evidence-list");
  list.replaceChildren();
  const candidate = selectedCandidate();
  if (state.evidenceTab === "commands") {
    const commands = state.runArtifacts?.commands || [];
    if (!commands.length) list.append(node("p", "empty-copy", "No reproduction command has been recorded."));
    commands.forEach((item) => {
      const row = node("div", "evidence-row command-row");
      row.append(node("span", "evidence-label", humanize(item.stage)), node("code", "", item.command));
      list.append(row);
    });
  } else if (state.evidenceTab === "hashes") {
    const hashes = {
      review: state.runDetail?.semantic_hash,
      candidate: candidate?.semantic_hashes?.candidate,
      formal: candidate?.semantic_hashes?.verification,
      measurement: candidate?.semantic_hashes?.measurement,
    };
    Object.entries(hashes).forEach(([label, value]) => {
      if (!value) return;
      const row = node("div", "evidence-row");
      row.append(node("span", "evidence-label", humanize(label)), node("code", "", value));
      list.append(row);
    });
    if (!list.children.length) list.append(node("p", "empty-copy", "No semantic hashes were recorded."));
  } else {
    const artifacts = state.runArtifacts?.items || [];
    if (!artifacts.length) list.append(node("p", "empty-copy", "No artifacts were found."));
    artifacts.forEach((item) => {
      const row = node("div", "evidence-row artifact-row");
      const copy = node("div");
      copy.append(node("strong", "", item.path), node("small", "", `${item.kind} · ${item.size_bytes.toLocaleString()} bytes`));
      row.append(copy, node("code", "", shortHash(item.sha256)));
      if (item.preview) {
        const preview = node("details", "artifact-preview");
        preview.append(node("summary", "", "View text preview"), node("pre", "", item.preview));
        row.append(preview);
      }
      list.append(row);
    });
  }
}

function renderSelectedCandidate() {
  const candidate = selectedCandidate();
  renderFinding(state.runDetail, candidate);
  renderFormal(candidate);
  renderMeasurements(candidate);
  renderDiff(candidate);
  renderEvidence();
}

function renderRunDetail(run) {
  state.runDetail = run;
  $("#run-detail-empty").hidden = true;
  $("#run-detail").hidden = false;
  const outcome = $("#run-outcome");
  outcome.className = `run-outcome tone-${run.outcome.tone}`;
  $("#run-outcome-label").textContent = run.outcome.label;
  const mixedDetail = run.completion?.mixed_outcomes
    ? " Candidate outcomes differ; review each site before acting."
    : "";
  $("#run-outcome-summary").textContent = `${run.outcome.summary} ${run.outcome.detail}${mixedDetail}`;
  $("#run-id").textContent = run.run_id;
  $("#run-top").textContent = run.top || "—";
  $("#run-objective").textContent = humanize(run.objective);
  renderStages(run.stages || []);

  const select = $("#candidate-select");
  select.replaceChildren();
  if (run.candidates.length) {
    run.candidates.forEach((candidate, index) => select.append(new Option(`Candidate ${index + 1} · ${humanize(candidate.status)}`, candidate.candidate_id)));
    if (!run.candidates.some((candidate) => candidate.candidate_id === state.selectedCandidateId)) {
      const decisive = run.candidates.find((candidate) => candidate.status === run.decision);
      state.selectedCandidateId = (decisive || run.candidates[0]).candidate_id;
    }
    select.value = state.selectedCandidateId;
    select.hidden = false;
  } else {
    state.selectedCandidateId = null;
    select.hidden = true;
  }
  $("#artifact-count").textContent = `${run.artifact_count} FILES`;
  renderSelectedCandidate();
}

async function selectRun(runId) {
  state.selectedRunId = runId;
  state.selectedCandidateId = null;
  $$(".run-list-item").forEach((item) => item.classList.toggle("active", item.dataset.runId === runId));
  try {
    const [detail, diffs, artifacts] = await Promise.all([
      getJSON(`/api/runs/v1/${encodeURIComponent(runId)}`),
      getJSON(`/api/runs/v1/${encodeURIComponent(runId)}/diff`),
      getJSON(`/api/runs/v1/${encodeURIComponent(runId)}/artifacts`),
    ]);
    if (state.selectedRunId !== runId) return;
    state.runDiffs = diffs.items || [];
    state.runArtifacts = artifacts;
    renderRunDetail(detail.run);
  } catch (error) {
    showToast(error.message);
  }
}

async function loadRuns(autoSelect = true) {
  try {
    const payload = await getJSON("/api/runs/v1");
    renderRunList(payload);
    if (autoSelect && payload.items.length) {
      const next = payload.items.some((item) => item.run_id === state.selectedRunId)
        ? state.selectedRunId
        : payload.items[0].run_id;
      await selectRun(next);
    }
  } catch (error) {
    showToast(error.message);
    $("#run-empty").hidden = false;
    $("#run-empty p").textContent = `Could not load run evidence: ${error.message}`;
  }
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
  $("#candidate-select").addEventListener("change", (event) => {
    state.selectedCandidateId = event.target.value;
    renderSelectedCandidate();
  });
  $$(".evidence-tab").forEach((tab) => tab.addEventListener("click", () => {
    state.evidenceTab = tab.dataset.evidence;
    renderEvidence();
  }));
}

async function initialize() {
  const [runsResult, overviewResult] = await Promise.allSettled([
    getJSON("/api/runs/v1"),
    getJSON("/api/v1/overview"),
  ]);
  if (runsResult.status === "fulfilled") {
    renderRunList(runsResult.value);
    if (runsResult.value.items.length) {
      const next = runsResult.value.items.some((item) => item.run_id === state.selectedRunId)
        ? state.selectedRunId
        : runsResult.value.items[0].run_id;
      await selectRun(next);
    }
  } else {
    $("#run-empty").hidden = false;
    $("#run-empty p").textContent = `Could not load run evidence: ${runsResult.reason.message}`;
    showToast(runsResult.reason.message);
  }
  if (overviewResult.status === "fulfilled") {
    renderOverview(overviewResult.value);
  } else {
    $("#nav-case-count").textContent = "—";
  }
  if (runsResult.status === "fulfilled" || overviewResult.status === "fulfilled") {
    $("#loading-screen").classList.add("hidden");
    $$(".view").forEach((view) => view.classList.remove("loading"));
    if (state.currentView === "cases") loadCases();
  } else {
    $("#loading-screen p").textContent = "Could not load local evidence.";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $$(".view").forEach((view) => view.classList.add("loading"));
  wireEvents();
  initialize();
  window.setInterval(() => {
    if (state.currentView === "runs" && document.visibilityState === "visible") loadRuns(true);
  }, 5000);
});
