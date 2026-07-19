# RTL Advisor MVP V1 — Reviewed and Corrected Plan

> **Authority:** This is the controlling MVP plan. Where the older V2.3,
> Frontend V1, or Codex Plugin V1 plans conflict with it, this plan takes
> precedence. Execution results are recorded in
> `progress updates/july 19th.md` rather than changing the frozen acceptance
> rules below.

## Summary

Ship a developer-preview pipeline that finds one narrowly supported RTL
pattern, creates an isolated candidate, proves RTL equivalence, measures both
versions through fixed Yosys synthesis recipes, and presents an evidence-backed
result through the CLI, Codex, and a read-only dashboard.

The MVP will demonstrate whether width-safe adder reassociation produces useful
results on a pre-registered open-RTL pilot. A positive synthesis result is not
required to ship: “synthesis already handles this” is a valid and valuable
result.

### Corrections from review

- Remove the requirement to find an open-source improvement; it encouraged
  benchmark shopping.
- Require RTL-to-RTL formal equivalence, not RTL-to-mapped-netlist equivalence.
- Defer EQY and sequential designs because the first transformation is
  combinational.
- Keep ML completely outside candidate selection and final decisions.
- Make the dashboard a professional result viewer; CLI and Codex run the tools.
- Freeze open benchmarks before examining synthesis outcomes.
- Preserve Agent API V1 while introducing an explicit V2 contract.
- Describe results as Yosys/ABC synthesis evidence, not target-flow or
  production-PPA predictions.

## MVP boundary

### Included

- One transformation: balancing or reassociating an unsigned, fixed-width
  combinational addition chain.
- Generated regression cases plus exactly two frozen open-source pilot modules.
- Real source-span rewriting for non-generated RTL.
- Isolated candidate files and source diffs.
- Verilator/Yosys compile and lint.
- Direct Yosys RTL-to-RTL formal equivalence.
- Standard and stronger Yosys/ABC synthesis recipes.
- CLI execution, Codex plugin orchestration, JSON artifacts, static HTML
  reports, and a read-only local dashboard.
- Reproducible tool, source, constraint, and artifact hashes.

### Deferred

- EQY and sequential or block-level proofs.
- RTL-to-technology-netlist equivalence.
- Live ML recommendations or model promotion.
- Six-family V2.3 expansion.
- OpenROAD as a release gate.
- Browser-triggered synthesis, queues, cancellation, and recovery.
- MCP, proprietary RTL, Arm CSS or SoC scale, Genus, and Conformal.

## Wave 0 — Freeze feasibility before building

### Pre-register the screening corpus

Search only this fixed corpus, in order:

1. ORFS `riscv32i` at commit
   `036d106273e66855cd5214d49518fd0f0df7de61`.
2. ORFS Ibex snapshot at the same ORFS revision.
3. PicoRV32 commit `87c89acc18994c8cf9a2311e871818e87d304568`.
4. ORFS CVA6 snapshot at the pinned ORFS revision.

Screening may parse, elaborate, lint, and detect structure. It must not run
candidate synthesis or inspect PPA results.

### Qualifying pilot module

A module qualifies only when it:

- Is a self-contained combinational top.
- Compiles using the normalized file, include, define, and top context.
- Contains a direct assignment with at least three unsigned, equal-width
  addends.
- Has an unambiguous source span.
- Does not require rewriting macros, functions, generated source, unresolved
  packages, sequential state, mixed signedness, or implicit truncation.
- Has clear open-source provenance and license metadata.

Traverse projects in the fixed order and files by sorted path. Freeze the first
qualifying module from each of two different upstream projects. Record the
source revision, license, compile command, eligible sites, exclusions, and
hashes before running candidate synthesis.

If fewer than two modules qualify, stop the open-pilot release gate and report
that the selected family lacks sufficient usable open evidence. Do not inspect
PPA and then change projects or transformation families.

## Wave 1 — Freeze the contracts

### PilotManifest v1

Add a narrow `PilotManifest v1` using capabilities already close to the current
normalized input:

- Top module.
- RTL files or filelist.
- Include directories and defines.
- Objective: `timing`, `area`, or `balanced`.
- Open-source provenance, revision, and license.
- Source and compile-context hashes.
- Synthesis profile IDs.

Parameter overrides, clocks, reset modeling, black-box assumptions, and
sequential semantics are not accepted in MVP manifests.

### Version domains

Keep these versions separate:

- Python package: `0.2.0a1`.
- Plugin release: `0.2.0-alpha.1`.
- Agent protocol: `rtl-advisor-agent-v2`.
- Run artifact schema: `rtl-advisor-run-v1`.
- Research model: remains V2.2 diagnostic-only.

Existing Agent V1 responses remain unchanged. Existing operations default to
schema V1 through the `0.2.x` line; the updated plugin explicitly passes
`--schema-version 2`. New `measure` and `report` operations require schema V2.

### Agent operations

```text
rtl-advisor agent capabilities --schema-version 2 --json
rtl-advisor agent review <input> --top <top> --objective <objective> --schema-version 2 --json
rtl-advisor agent candidate <run-id> --finding <finding-id> --schema-version 2 --json
rtl-advisor agent verify <run-id> --candidate <candidate-id> --schema-version 2 --json
rtl-advisor agent measure <run-id> --candidate <candidate-id> --schema-version 2 --json
rtl-advisor agent report <run-id> --schema-version 2 --json
```

### State progression

```text
candidate_available
        ↓
candidate_prepared
        ↓
formal_passed ────────────────┐
formal_failed                 │
formal_inconclusive           v
                     measured_improvement
                     synthesis_handles
                     flow_dependent
                     regression
```

- `review` uses deterministic rules for the released family.
- Candidate availability is independent of ML readiness.
- Candidate, proof, and measurement artifacts are append-only and hash-linked.
- `measure` requires a current `formal_passed` artifact.
- Any source or compile-context change invalidates later stages.
- `report` derives the final state without modifying earlier records.

## Wave 2 — Parallel core implementation

After schemas and fixtures are frozen, use three subagents plus the coordinating
agent.

### Rewriter and formal lane

- Replace generated-sibling swapping with a real syntax-aware source rewriter.
- Locate the complete addition expression and assign a stable site ID.
- Preserve operand widths, signedness, truncation points, and surrounding
  syntax.
- Produce a deterministic balanced expression.
- Copy the design into the artifact workspace and modify only the copied source.
- Emit a source-linked diff and before/after hashes.
- Lint the candidate using the same compile context as the baseline.
- Prove the complete combinational module with direct Yosys equivalence.
- Record two-state bit-vector semantics and tool limitations.
- Add deliberately incorrect candidates to prove the checker rejects them.

### Synthesis and evidence lane

- Generalize the existing standard and stronger recipes from generated
  manifests to `PilotManifest`.
- Pin Yosys 0.63, ABC behavior, Liberty data, commands, and environment.
- Use the standard recipe as primary and the stronger recipe as a fixed
  sensitivity check.
- Run baseline and candidate with identical constraints.
- Record mapped cell area, delay proxy or logic depth, cell counts, warnings,
  logs, and canonical netlist hashes.
- Publish every eligible site, tie, regression, exclusion, and failure.

### Frontend lane

Build against frozen `rtl-advisor-run-v1` fixtures:

- Preserve the existing HTML, CSS, and JavaScript stack.
- Keep the research dashboard as a secondary **Research evidence** view.
- Add a professional run viewer showing:
  - Review → Candidate → Formal → Synthesis → Final result.
  - Source-linked finding.
  - Candidate diff.
  - Formal result and limitations.
  - Standard and stronger synthesis measurements.
  - Final plain-language conclusion.
  - Commands, hashes, logs, and reproduction information.
- Cover empty, unsupported, failed, running-artifact, and completed states.
- Use rendered browser inspection for visual and responsive QA.

The dashboard exposes read-only endpoints:

```text
GET /api/runs/v1
GET /api/runs/v1/{run_id}
GET /api/runs/v1/{run_id}/diff
GET /api/runs/v1/{run_id}/artifacts
```

It polls artifact records for updates but never starts tools or accepts RTL
uploads.

### Coordinating lane

- Implement the V2 agent contract and run-artifact store.
- Keep Agent V1 regression compatibility.
- Integrate lane work and resolve cross-cutting changes.
- Update the Codex plugin after the CLI contract stabilizes.
- Ensure Codex invokes the CLI and explains its result without changing the
  decision.
- Compare interfaces using normalized evidence fields rather than
  path-sensitive whole-document hashes.

## Measurement rules

Classify each recipe independently.

### Timing objective

- Improved: delay improves by at least 3%, with area regression no greater than
  10%.
- Regressed: delay worsens by at least 3% or area worsens by more than 10%.
- Otherwise: neutral.

### Area objective

- Improved: area improves by at least 5%, with delay regression no greater than
  2%.
- Regressed: area worsens by at least 5% or delay worsens by more than 2%.
- Otherwise: neutral.

### Balanced objective

- Improved: either the timing or area rule passes without violating the other
  metric's guardrail.
- Regressed: either guardrail is violated and neither improvement rule passes.
- Otherwise: neutral.

### Aggregate result

- Both recipes improved: `measured_improvement`.
- Both recipes neutral: `synthesis_handles`.
- Either recipe regressed: `regression`.
- Remaining disagreement: `flow_dependent`.

These conclusions apply only to the pinned Yosys/ABC recipes and library. They
are not claims about Genus, Design Compiler, place-and-route timing, or the
company target flow.

## Wave 3 — Frozen pilot

For each frozen module:

1. Run review with synthesis outcomes unavailable to the advisor.
2. Record every eligible site and exclusion.
3. Generate one deterministic candidate per eligible site.
4. Confirm the original checkout hashes remain unchanged.
5. Lint and formally verify each candidate.
6. Run baseline and passing candidates through both synthesis recipes.
7. Generate immutable stage JSON and derived static HTML reports.
8. Publish the complete result set without selecting only favorable examples.

A zero-win pilot is valid:

- If synthesis neutralizes every candidate, report that synthesis handled the
  tested cases.
- If any candidate regresses, report it and verify the tool does not recommend
  it.
- If a candidate improves under both recipes, permit only the narrow claim that
  RTL Advisor found a repeatable Yosys/ABC improvement.

The release decision depends on evidence completeness and correctness, not on
obtaining a positive result.

## ML and Codex role

ML is not part of the MVP decision path.

- The current V2.2 model remains diagnostic-only.
- It cannot select sites, unlock candidate generation, or determine final
  results.
- Pilot designs remain outside all training and threshold selection.
- Run artifacts are retained as future V2.3 training evidence.
- Future training must split by canonical expression DAG, template family, and
  upstream repository.
- A rules-versus-ML-versus-Codex benchmark becomes a post-MVP experiment after
  enough independent RTL is collected.

Codex remains useful in the MVP for:

- Invoking the same CLI stages.
- Explaining source-linked findings and limitations.
- Summarizing diffs and evidence.
- Guiding engineers through failures.
- Never overriding formal or synthesis results.

## Acceptance tests

### Rewriter

- Positive cases: whitespace, parentheses, three or more operands, explicit
  widths, and multiple eligible sites.
- Rejections: mixed signedness, unsafe truncation, macros, functions, generated
  spans, side effects, sequential logic, and ambiguous locations.
- Original files remain byte-identical.

### Formal

- Every intended-equivalent generated candidate passes.
- Operand removal, bit flips, width changes, and incorrect grouping controls
  fail.
- Timeouts and unsupported constructs return `formal_inconclusive` or
  `unsupported`.
- Changed baseline, candidate, or compile context invalidates the proof.

### Synthesis

- Baseline and candidate use identical inputs except for the isolated patch.
- Recipe and library hashes match.
- Threshold boundaries and all four aggregate outcomes have tests.
- Repeated executions produce equivalent normalized results.

### Interfaces

- Existing Agent V1 contract fixtures remain unchanged.
- V2 CLI and plugin produce equal normalized findings, stage states,
  measurements, and final decisions.
- The dashboard renders the same immutable run evidence.
- Missing Yosys, Verilator, Liberty, or configuration is reported accurately.

### Packaging

- Preserve all 179 existing tests.
- Wheel and sdist install outside the repository.
- Add fast CI for unit, schema, packaging, plugin, and frontend API tests.
- Add a separate pinned Docker tool-integration workflow for formal and
  synthesis smoke tests.
- Keep large evidence, third-party RTL, and tool installations outside the
  wheel.

## Release and documentation

- Create this `implementation plan/MVP V1.md` from the reviewed plan.
- Mark V2.3, Frontend V1, and Codex Plugin V1 as subordinate post-MVP tracks
  where they conflict.
- Update `progress updates/july 19th.md` after every completed wave.
- Work on `codex/mvp-v1` with one reviewed commit per wave.
- Add a concise changelog, example pilot manifest, evidence lock, known
  limitations, and complete README workflow.
- Use Apache-2.0 as the proposed project license; obtain owner confirmation
  before adding the license or tagging the release.
- Release the wheel as `0.2.0a1`, the plugin as `0.2.0-alpha.1`, and tag
  `v0.2.0-alpha.1`.

## Expected user intervention

Only pause for:

- Starting Docker Desktop when the pinned integration environment is needed.
- Approving downloads of pinned open-source inputs or tool images.
- Confirming the project license before release.
- Authenticating the final GitHub push or pull request.

No proprietary RTL or commercial EDA access is required for this MVP.
