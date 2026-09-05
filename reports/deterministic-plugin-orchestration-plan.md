# Deterministic Plugin Orchestration Plan

**Date:** 2026-09-01  
**Status:** Phases 1–7 implemented; installed-plugin acceptance passed September 4, 2026
**Product goal:** Turn RTL optimization into a reproducible, evidence-backed workflow that proves correctness and exposes PPA regressions before they reach production.

## Decision

Evolve the existing `rtl-advisor` Codex plugin into the product's orchestration
surface. Do not create a second plugin.

Codex should translate an engineer's intent into a small, versioned workflow
request, launch the local RTL Advisor flow, monitor it, and explain the final
immutable result. Deterministic code must remain responsible for RTL findings,
candidate generation, formal proof, synthesis measurement, classification, and
report generation.

This architecture reduces token usage, improves reproducibility, and prevents a
language model from silently changing an engineering decision.

## Current foundation

The repository already contains the essential pieces:

- `plugins/rtl-advisor/.codex-plugin/plugin.json` defines the existing plugin.
- `plugins/rtl-advisor/skills/analyze-rtl/SKILL.md` defines the released safety
  and evidence workflow.
- `plugins/rtl-advisor/skills/analyze-rtl/scripts/run_rtl_advisor.py` resolves
  the local executable, invokes only supported Agent V2 operations, validates
  schemas and semantic hashes, and preserves structured exit states.
- The CLI exposes the deterministic sequence:
  `capabilities -> review -> candidate -> verify -> measure -> report`.
- Candidate availability is registry-driven; the diagnostic ML model cannot
  unlock a candidate or change a final decision.
- Candidate, proof, measurement, and report artifacts are hash-linked and
  append-only.

Phase 1 adds four workflow schemas and deterministic contract validation in
`src/rtl_advisor/workflow_contract.py`. Phase 2 adds the persistent state
machine in `src/rtl_advisor/workflow_runner.py`, public CLI workflow commands,
and plugin-runner support. Phase 3 adds the deterministic preparation adapter
that turns bounded Codex intent into the initial request and authorization
documents without asking Codex to calculate hashes or IDs.

## Responsibility boundary

### Codex owns

1. Interpret the engineer's request.
2. Confirm source authorization when it is not already explicit.
3. Resolve the requested objective to `timing`, `area`, or `balanced`.
4. Normalize the engineer's explicit permission to an `authorized_through`
   ceiling: `review`, `candidate`, `verify`, or `measure`.
5. Launch or resume the deterministic workflow.
6. Read the compact summary rather than raw logs or complete intermediate
   artifacts.
7. Explain the unchanged result, limitations, and supported next action.

Codex must not invent a transformation, override a gate, infer missing PPA, or
reclassify a stored result.

### Deterministic RTL Advisor code owns

1. Environment and tool capability checks.
2. Path, schema, compile-context, and source-hash validation.
3. Rules-only review and registered-alternative lookup.
4. Finding eligibility and deterministic candidate selection.
5. Candidate generation inside the artifact workspace.
6. P1/P2 contract selection and formal verification.
7. Both pinned Yosys/ABC measurement recipes.
8. Improvement, synthesis-handled, flow-dependent, and regression
   classification.
9. Artifact persistence, semantic hashes, normalized commands, and provenance.
10. JSON, HTML, CSV, and chart-ready report generation.
11. Safe stop, resume, and idempotent reuse of completed stages.

## Target workflow

```text
Engineer request
      |
      v
Codex creates a constrained workflow request
      |
      v
capabilities -> input validation -> deterministic review
                                      |
                    +-----------------+------------------+
                    |                                    |
              no candidate                       candidate available
                    |                                    |
               final report                    authorization gate
                                                         |
                                                         v
                                              candidate preparation
                                                         |
                                                         v
                                                   formal proof
                                             +-----------+-----------+
                                             |                       |
                                      failed/inconclusive          passed
                                             |                       |
                                        final report             measurement
                                                                     |
                                                                     v
                                                               classification
                                                                     |
                                                                     v
                                                        immutable report + charts
                                                                     |
                                                                     v
                                                     Codex explains compact summary
```

## Authorization model

Authorization is a separate, hash-linked record bound to the normalized
workflow request. The deterministic engine never interprets free-form prompt
text. Codex proposes an explicit stage ceiling, while the authorization records
either the direct prompt hash or a confirmed proposal and confirmation hash.

| `authorized_through` | Permitted stages |
| --- | --- |
| `review` | Capabilities, validation, review, report |
| `candidate` | Review and selected candidate preparation |
| `verify` | Review, candidate preparation, and formal proof |
| `measure` | Review, candidate preparation, formal proof, two measurement recipes, and report |

A request such as “review this RTL” must stop after review. A request such as
“optimize this RTL and prove and measure the result” may authorize the complete
flow up front. An ambiguous request such as “optimize this” must not silently
authorize proof or measurement. Codex presents the exact proposed stages and
records the engineer's confirmation.

A resume operation using the same authorization cannot expand the stage
ceiling. A new explicit, hash-linked authorization may extend the same workflow
but may not narrow it or change the workflow request identity.

## Proposed deterministic interface

Put orchestration logic in the core package, not in the prompt or plugin script.
The plugin runner remains a thin invocation and validation adapter.

Proposed CLI operations:

```text
rtl-advisor agent workflow prepare <input> --input-kind <kind> --objective <objective> --authorized-through <stage> --prompt-file <file> [--start] --schema-version 1 --json
rtl-advisor agent workflow start <request.json> --authorization <authorization.json> --schema-version 1 --json
rtl-advisor agent workflow status <workflow-id> --schema-version 1 --json
rtl-advisor agent workflow resume <workflow-id> --authorization <authorization.json> --schema-version 1 --json
rtl-advisor agent workflow report <workflow-id> --schema-version 1 --json
```

The existing individual Agent V2 operations remain available for debugging and
backward compatibility.

### Workflow request

The versioned request is produced from normalized local input and contains only
bounded inputs. It does not itself grant permission:

```json
{
  "schema_version": 1,
  "schema": "rtl-advisor-workflow-request-v1",
  "document_type": "rtl-advisor.workflow.request",
  "workflow_id": "workflow-<content-derived-id>",
  "input": {
    "kind": "explicitly_approved_open_rtl",
    "path": "/absolute/path/to/input-or-manifest",
    "top": "optional_top",
    "sha256": "<source-sha256>",
    "compile_context_hash": "<compile-context-sha256>",
    "include_dirs": [],
    "defines": []
  },
  "objective": "balanced",
  "semantic_hash": "<request-semantic-hash>"
}
```

File lists, include directories, and definitions should be stored in a
validated local manifest rather than expanded into the conversation.

The authorization document references the request semantic hash and records
`authorized_through`, its prompt or proposal-confirmation basis, its issuance
time, and its own semantic hash. Authorization through `candidate`, `verify`,
or `measure` also freezes either a specific finding ID or the deterministic
`first_eligible` selection policy before candidate preparation.

### Compact workflow summary

Codex should normally receive a small `workflow-summary.json`, not the complete
review, proof, synthesis logs, or measurement records. It should contain:

- workflow ID and current stage;
- terminal or waiting status;
- decision and plain reason code;
- selected finding and candidate IDs when present;
- `safe` only when the current hash-matched formal result passed;
- M0 and M1 classifications and the aggregate candidate decision;
- limitations and excluded coverage;
- required next action or authorization;
- semantic hash, normalized commands, and artifact paths;
- links to the generated HTML report and chart data.

Full evidence remains locally inspectable and is loaded only when the engineer
asks for detail or when a structured failure requires diagnosis.

## Token-reduction rules

1. Never send raw RTL to the model when a manifest and deterministic finding
   summary are sufficient.
2. Never stream formal or synthesis logs into the model during a successful
   run.
3. Parse tool output in code and expose stable enums, counts, and concise
   reasons.
4. Generate tables, CSV, and visualization payloads directly from immutable
   evidence.
5. Reuse completed content-addressed stages instead of asking Codex to repeat
   analysis.
6. Load a source excerpt or log tail only for an exact finding or failure.
7. Keep skill instructions short; place schemas and state logic in code.
8. Measure plugin token usage in the existing A/B/C evaluation framework and
   track it alongside correctness and task completion.

## Plugin skill structure

The plugin should remain a small collection of intent-level skills. Fine-grain
execution steps should not become separate skills because each additional skill
adds routing and context overhead.

### Keep and evolve

- `analyze-rtl`: the main engineer-facing entry point and safety authority. It
  launches the workflow, preserves approval gates, and explains final evidence.

### Add only when the workflows are implemented

- `manage-rtl-corpus`: qualify, register, freeze, and audit open-reference
  corpus entries and dataset coverage.
- `explore-rtl-evidence`: inspect existing immutable runs, compare cohorts, and
  launch the local dashboard without creating or changing recommendations.

Candidate generation, verification, measurement, classification, and report
generation should remain CLI stages used by `analyze-rtl`, not independent
skills.

Shared runner code and schemas should live once in the plugin package. Skills
should reference the shared implementation rather than carry duplicate scripts.

## Failure and resume behavior

- Capability failure stops before review.
- Unsupported or unauthorized input stops before source analysis.
- A review without an eligible candidate ends as a valid terminal result.
- Candidate preparation requires recorded authorization and the selected
  eligible finding.
- Source or compile-context hash changes invalidate downstream evidence.
- Formal failure or inconclusive proof stops before measurement.
- Measurement requires the current formal-passed artifact.
- Schema, document-type, semantic-hash, or exit-code disagreement stops the
  workflow as untrusted.
- Automatic retries are limited to explicitly identified transient execution
  failures and may not change inputs, recipes, timeouts, or selection criteria.
- Resume begins at the first incomplete valid stage and never rewrites a
  completed artifact.

## Delivery phases

### Phase 1 — Workflow contract

- [x] Define workflow request, authorization, state, and compact-summary schemas.
- [x] Define terminal, waiting, failed, and resumable states.
- [x] Map stage authorization and transitions to existing Agent V2 evidence
  requirements.
- [x] Add contract and transition tests before implementing the runner.

### Phase 2 — Core deterministic state machine

- [x] Add a core workflow module and CLI commands.
- [x] Invoke existing Agent V2 functions without duplicating decision logic.
- [x] Add idempotent stage detection and content-addressed resume.
- [x] Produce compact summaries and immutable stage and state history.

### Phase 3 — Plugin integration

- [x] Extend `run_rtl_advisor.py` to accept only the documented workflow commands.
- [x] Validate workflow schemas, semantic hashes, document types, and exit codes.
- [x] Update `analyze-rtl` to prefer the workflow command while preserving the
  individual-command fallback during migration.
- [x] Keep explicit authorization boundaries in the skill.
- [x] Add a deterministic intent-to-request preparation command so Codex never
  calculates workflow IDs, source hashes, compile-context hashes, or semantic
  hashes.

### Phase 4 — Evidence exploration

- [x] Generate dashboard and visualization payloads directly from stored reports.
- [x] Add the read-only `explore-rtl-evidence` skill only after its CLI/API contract
  is stable.
- [x] Return filtered compact aggregates to Codex; keep full point-level evidence
  behind local artifact links.

Phase 4 adds `rtl-advisor agent evidence explore`, the versioned
`rtl-advisor-evidence-exploration-v1` and
`rtl-advisor-evidence-chart-data-v1` contracts, exact bounded filters, compact
bar/scatter specifications, and immutable local point data. The explorer reads
only evidence already validated by the dashboard adapter, requires every chart
point to carry a current formal pass and measurement semantic hash, and reports
invalid excluded sources as `partial` instead of silently treating the cohort
as complete. Profile and candidate classifications now share the measurement
engine's `classify_recipe` and `aggregate_measurements` authority. Family-study
`M0`/`M1` names remain recorded as provenance while their comparable roles are
normalized to `standard`/`stronger` for cross-cohort filtering.

### Phase 5 — Corpus workflow

- [x] Implement deterministic corpus qualification and coverage summaries.
- [x] Add `manage-rtl-corpus` after the commands and schemas are stable.
- [x] Preserve lineage-aware counting so parameter variants do not inflate dataset
  size.

Phase 5 adds the Agent-facing `corpus validate`, `corpus register`,
`corpus qualify`, and `corpus coverage` commands. The mutation paths are
explicit, content-addressed, and append-only; coverage and frozen-lock
validation are read-only. Four versioned result schemas separate tranche
validation, registration, qualification, and coverage so clients can enforce
their different authorization and exit-state contracts.

The coverage contract fixes `independent_design_lineage` as the primary
population unit. References, implementation variants, and parameter variants
remain separately visible inventory and cannot inflate that count. The current
registry audit reports 12 independent design lineages across 3 repository
lineages, 8 variants, and 8 fully qualified lineages. Four lineages are blocked
before reference qualification; tiers B-D and four RTL categories currently
have no coverage.

### Phase 6 — Evaluation and release

- [x] Add end-to-end tests for every terminal and blocked state.
- [x] Run plugin parity tests between direct CLI and plugin-orchestrated execution.
- [x] Compare token usage, task completion, decision agreement, and latency in the
  existing plugin A/B/C framework.
- [x] Package and reinstall the plugin through the repository's release process (after the Phase 7 gates passed).

The Phase 6 state matrix now covers the valid review terminals (`no_change` and
`unsupported`), all authorization ceilings, both formal non-pass terminals, all
five measurement decisions, capability blocking, stale-input failure, and
untrusted semantic-hash rejection. The transport harness also exercises
`corpus validate`, `register`, `coverage`, and fail-closed `qualify` against an
isolated registry. All 12 direct-CLI/plugin-runner parity scenarios pass with
identical payloads, semantic hashes, exit codes, and unchanged source hashes;
the evidence is in `artifacts/plugin-parity/phase6.json`.

The frozen A/B/C correctness evidence passes the Phase 6 quality gate:
plugin-only averages 95.83% validated decision accuracy, 100% evidence
completion, 100% decision reproducibility, and zero harmful or unproven
recommendations. Four fresh instrumented runs now capture authoritative Codex
`turn.completed` token counters, wall time, task completion, result paths, and
event streams under `experiments/plugin-abc-v1/instrumented/phase6` without
overwriting the frozen historical runs.

The measured efficiency gate fails. Across two repetitions, Arm A averages
1,492,047 total tokens and 581.155 seconds; Arm B averages 2,338,730 total
tokens and 942.022 seconds. That is a 56.75% token increase and a 62.09%
latency increase for the plugin arm, rather than the required 25% token
reduction. Cached context does not reverse the conclusion: after subtracting
cached input, Arm B's mean input-plus-output volume is 160,810 tokens versus
143,887 for Arm A, an 11.76% increase. Both arms completed every task, and the
instrumented plugin decisions have 100% repeat agreement and 100% agreement
with the frozen validated plugin decisions.

The event streams explain the direction without changing the engineering
decision: Arm A averages 55 completed commands and about 168 KB of command
output, while Arm B averages 113 commands and about 412 KB. The deterministic
gates are working, but Codex is still orchestrating and rereading too many
individual per-case payloads. One earlier B2 attempt hit the account usage
ceiling before `turn.completed`; its diagnostics and complete draft remain
preserved but are excluded from token calculations because the API emitted no
authoritative usage counter.

The local `personal` marketplace was repaired to point at this repository, and
the plugin-creator development flow validated, cache-busted, and installed
`0.2.0-alpha.1+codex.20260902050619` for the fresh-thread benchmark. This is not
a release approval: `experiments/plugin-abc-v1/evaluations/phase6-evaluation.json`
now has status `failed` and `release_ready: false`, so no second release
cache-bust, tag, or publication was performed.

### Phase 7 — Latency and token reduction

- [x] Add a sub-4 KB `rtl-advisor-workflow-digest-v1` projection while keeping
  the full summary as the backward-compatible default.
- [x] Add ordered `workflow batch` orchestration with one capability discovery,
  content-addressed child workflows, duplicate workflow suppression, bounded
  `--jobs 1..4`, and per-item failure continuation.
- [x] Add hash-linked batch manifest, request, authorization, and summary V1
  schemas.
- [x] Route the plugin's success path through one compact single-workflow or one
  batch command; remove the separate capability call and prohibit automatic
  fallback to individual stages.
- [x] Extend JSONL telemetry with command waves, command categories, agent-message
  bytes, command-output bytes, and combined output bytes.
- [x] Add a 25% wall-time reduction release gate alongside the existing 25%
  total-token reduction and unchanged quality/agreement gates.
- [x] Keep Phase 7 telemetry and evaluation outputs under a separate `phase7`
  path so frozen Phase 6 evidence is not overwritten.
- [x] Run two fresh matched A/B repetitions and reinstall only if every Phase 7
  release gate passes.

The digest deterministically maps `measured_improvement` to
`recommend_change`; `no_change`, `synthesis_handles`, and `regression` to
`no_change`; `unsupported` to `unsupported`; and all pending, unproven,
formal-nonpass, incomplete, or flow-dependent outcomes to `inconclusive`.
Finding locations, rationale, formal status, profile classifications, evidence
completeness, and parent hashes are derived from immutable stage evidence rather
than model text.

The batch manifest preserves the caller's explicit input kind and objective for
each item. Relative paths resolve from the manifest directory, optional expected
source hashes fail only their item, malformed global authorization or capability
evidence stops the entire batch, and results are emitted in manifest order even
when bounded parallel execution is requested. The default remains sequential.

Phase 7 release evidence is produced with:

```bash
python3 scripts/plugin_abc_measured.py --phase phase7 --arm A --repetition 1
python3 scripts/plugin_abc_measured.py --phase phase7 --arm A --repetition 2
python3 scripts/plugin_abc_measured.py --phase phase7 --arm B --repetition 1
python3 scripts/plugin_abc_measured.py --phase phase7 --arm B --repetition 2
python3 scripts/plugin_phase7_eval.py
```

No cache-bust or reinstall is permitted unless the Phase 7 evaluator reports at
least 25% lower total tokens, at least 25% lower wall time, 100% task completion,
100% agreement with frozen plugin decisions, 100% repeat agreement, and the
unchanged quality gate.

The accepted source-pinned candidate passed those gates on September 2, 2026.
Against the two fresh Arm A controls, Arm B reduced mean total tokens from
1,991,760.5 to 448,552.5 (77.48%), mean uncached token volume from 142,672.5 to
75,816.5 (46.86%), and mean wall time from 637.565 seconds to 445.519 seconds
(30.12%). It also reduced mean commands from 78.5 to 10.5 and mean command waves
from 47.5 to 9.0. Task completion, agreement with the frozen plugin decisions,
and measured repeat agreement were all 100%; the unchanged quality gate passed
at 95.83% validated decision accuracy with no harmful or unproven
recommendations.

The accepted pair uses one isolated artifact root: repetition one measures a
cold four-job batch and repetition two measures content-addressed replay. An
earlier replay exposed `workflow batch` calling `start` on existing workflow
IDs; the engine now performs validated authorization progression through
`resume` and reuses immutable evidence. Superseded measurements remain listed
in telemetry with explicit exclusion reasons, while a quota-limited control
attempt is preserved separately with usage marked unavailable. The fresh-session
workspace source-pin canary passed before measurement, and both admitted plugin
runs record the same source-tree hash.

The measured skill-tree hash
`e10ffc8c3a55127e0e90a911218aacff3ba35f32b93703a20d00712a57040570`
matches both admitted plugin measurements. Direct CLI/plugin-runner parity also
passed all 12 scenarios. A post-gate release-metadata check then required one
explicit fail-closed sentence (`Evidence incomplete means no positive
recommendation is permitted.`); this copy-only change did not alter a command,
workflow rule, or implementation. The final source-pin canary passed with hash
`508e5f39d943cb1997c7c487739a8cbdb56e9666d3a9b3c51d36f5efd48387f9`,
and 95 release-critical tests passed. The plugin was cache-busted, validated,
and reinstalled from the repository's `personal` marketplace as
`0.2.0-alpha.1+codex.20260903154742`.

## Acceptance criteria

The architecture is ready when:

1. Identical authorized inputs, configuration, and toolchain produce the same
   workflow ID, decision, and semantic summary hash.
2. Re-running or resuming a completed stage does not duplicate work or modify
   evidence.
3. No candidate is generated outside recorded authorization.
4. No candidate is called safe without a current formal pass and matching
   hashes.
5. No measurement runs without that formal pass.
6. Codex cannot change finding eligibility, classification, or the final
   report.
7. Successful runs require only the compact workflow request and summary in
   model context.
8. Direct CLI and plugin-orchestrated executions produce equivalent immutable
   results.
9. Dashboard labels and charts are derived from the same classification fields
   as the report.
10. Token usage is materially lower than the current multi-command orchestration
    baseline without reducing gate compliance or explanation accuracy.

## Recommended next implementation slice

The batch adapter and installed-plugin acceptance are complete. A future
performance release should repeat the matched 24-case A/B protocol with two
repetitions and the existing quality, token, latency, and agreement gates.
The acceptance smoke below does not replace that release experiment.

## Installed-plugin fresh-thread acceptance — September 4, 2026

**Result: passed for the representative candidate-only workflow.** Before work,
`codex/mvp-v1` was clean at published commit
`9b3b3e10449abdffba64ee475c5476d76a517fab`; fetching origin confirmed identical
local and remote heads, with zero commits ahead or behind. The installed plugin
was enabled at `0.2.0-alpha.1+codex.20260903154742`, and its entire plugin tree
matched the repository byte-for-byte.

The acceptance prompt requested balanced-PPA review of generated
`g01_add8_left.sv`, top `g01_add8_left`, and preparation of the first eligible
isolated candidate. It explicitly withheld formal and measurement permission.
The fresh `codex exec --ephemeral` thread used the Phase 7 model and effort
(`gpt-5.6-sol`, `xhigh`) with the installed plugin available through normal user
configuration. Unlike the release benchmark's workspace source pin, no skill
text was injected into developer instructions. All new evidence is under
`experiments/plugin-abc-v1/evaluations/installed-plugin-acceptance/`.

The successful thread `01a0701e-c381-7a91-b133-9b1c074601ff` read the installed
`analyze-rtl/SKILL.md` exactly once, then called its installed
`scripts/run_rtl_advisor.py workflow prepare` exactly once with
`--authorized-through candidate --first-eligible --start --compact`. The
recorded prompt hash matched the authorization basis. Only capabilities,
review, and candidate stages were persisted. The returned digest recorded
`candidate_prepared`, `action: inconclusive`, `safe: false`,
`evidence_complete: false`, and formal/measurement `not_run`. The final response
preserved these fields and requested verification authorization as its next
action. There were no separate capability calls, individual-stage fallbacks,
or commands after the successful runner call. No RTL or full stage artifact
was loaded into the fresh thread's context.

Authoritative `turn.completed` usage and process wall time were recorded with
the existing Phase 7 parser and command diagnostics:

| Measurement | Fresh acceptance | Checked-in Phase 7 Arm B mean |
| --- | ---: | ---: |
| Workload | 1 case, through candidate | 24 cases, through measure |
| Total tokens (input + output) | 59,805 | 448,552.5 |
| Uncached volume (input + output − cached input) | 31,133 | 75,816.5 |
| Wall time, seconds | 35.981 | 445.519 |
| Commands / command waves | 2 / 2 | 10.5 / 9.0 |
| Command output bytes | 6,861 | 49,490 |

The returned digest was 2,323 bytes, below the 4 KiB target. Input usage was
58,500 tokens (28,672 cached); output usage was 1,305 tokens. Reasoning tokens
are retained as a counter and are not added again to output usage. The two
command outputs comprise the skill and digest. The workloads and configuration
loading differ, so these totals do **not** establish a performance improvement
or regression against Phase 7. Its frozen release evaluation and reported
77.48% total-token, 46.86% uncached-token, and 30.12% latency reductions remain
historical evidence. This acceptance completes 1/1 assigned workflow; it does
not supply a new 24-case decision-agreement or PPA result.

A separate deterministic replay of the same authorized command took 0.273
seconds and preserved the workflow ID, digest semantic hash, source hash, and
the bytes of all 33 artifact files, with no additional artifacts. That replay
measures local execution only and has no model token counter. The acceptance
auditor inspected authorization and artifact metadata after the measured thread
ended; those audit reads are outside the model measurement.

Two earlier attempts remain preserved. Attempt 001 omitted the required top
module and returned compact `top_required` after one runner call; its completed
turn recorded 59,605 total and 11,349 uncached tokens. Its process wall time was
not durably captured and is unavailable. Attempt 002 hit the account usage
limit before any plugin command; no authoritative usage exists. Neither is
treated as a successful sample. The missing-top case exposed the recorder's
assumption that every JSON result contained a digest artifact path. The new
acceptance harness now validates document type, semantic hash, and runner exit
code before looking up artifacts, and records failures with usage unavailable
when no completed-turn counter exists. Providing the top corrected the test
input; no change to the installed plugin, RTL, or deterministic engine was
supported by the evidence.

Evidence and reproduction:

- [Acceptance record](../experiments/plugin-abc-v1/evaluations/installed-plugin-acceptance/attempt-003/acceptance.json)
- [Exact prompt](../experiments/plugin-abc-v1/evaluations/installed-plugin-acceptance/attempt-003/prompt.txt)
- [Fresh-thread event stream](../experiments/plugin-abc-v1/evaluations/installed-plugin-acceptance/attempt-003/codex-events.jsonl)
- [Final response](../experiments/plugin-abc-v1/evaluations/installed-plugin-acceptance/attempt-003/response.md)
- [Replay audit](../experiments/plugin-abc-v1/evaluations/installed-plugin-acceptance/attempt-003/replay.json)

```bash
.venv/bin/python scripts/plugin_installed_acceptance.py
```

The harness allocates a new attempt and isolated artifact directory each time.
Optional `--replay-record <acceptance.json>` audits local reuse once and refuses
to overwrite an existing replay record. Runtime artifacts remain local under
the repository's existing `artifacts/` ignore rule; compact records, event
streams, prompts, and response text retain the auditable handoff.

Validation: **118 tests passed**, covering the 95-test release-critical workflow,
runner, parity, telemetry, evaluation, and corpus set, plus 19 existing framework
and evidence-exploration tests and four acceptance-recorder regression cases.
The full slow integration suite was not rerun. Frozen Phase 6/7 evidence and
the installed plugin were not modified; new work consists of the acceptance
harness, its tests, new acceptance evidence, and this report update.
