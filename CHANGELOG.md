# Changelog

All notable changes are recorded here. The project has not been tagged for
release because project-license confirmation is still pending.

## 0.2.0a1 — Unreleased developer preview

Plugin counterpart: `0.2.0-alpha.1`. Agent protocol:
`rtl-advisor-agent-v2`. Run schema: `rtl-advisor-run-v1`.

### Added

- A frozen 12-reference Tier A tranche spanning OpenTitan, PULP common_cells,
  and verilog-axis, with exact source/archive/license hashes and no
  candidate-PPA-driven replacement.
- `corpus validate-tranche`, source verification, build/lint qualification,
  baseline-only synthesis, P2 proof, P3 proof, and behavior-gate operations.
- An independently reviewed Yosys/SBY same-cycle P2 arbiter-pair proof and a
  bounded transaction-ordered P3 ready/valid pipeline proof.
- Reset, state, grant, drop, duplicate, and ordering negative controls.
- Four PULP reference-property tasks plus exact upstream cocotb dependencies
  for three verilog-axis regressions.
- An immutable behavior plan and result store that qualified 8/12 references,
  retained four explicit blockers, and advanced the append-only registry only
  after evidence passed.

- Corpus Registry V1 reference, variant, and P0–P6 proof contracts with strict
  JSON schemas, qualification-state gates, complete compile-context metadata,
  deterministic semantic hashes, and append-only storage.
- `corpus add`, `corpus validate`, `corpus list`, and `corpus summary` commands
  with tier/category/upstream/license/proof/status coverage and protections
  against file-count inflation, duplicate declared lineages, and split leakage.
- A metadata-only OpenTitan arbiter reference/variant fixture that downloads no
  RTL and correctly counts the upstream alternative as a variant.
- A deterministic rule for unbalanced unsigned, equal-width, fixed-width
  combinational addition chains.
- Source-linked findings, stable site IDs, isolated candidate workspaces, and
  source diffs without changing the original RTL.
- Direct Yosys RTL-to-RTL combinational equivalence with current-hash checks and
  deliberately incorrect negative controls.
- Standard and stronger pinned Yosys/ABC synthesis recipes with identical
  baseline/candidate context, normalized metrics, logs, and netlist hashes.
- Exact Yosys and adjacent ABC executable identities, proof-transcript checks,
  byte-preserving source rewrites, and fail-closed input/tool revalidation.
- Agent V2 `review`, `candidate`, `verify`, `measure`, and `report` operations.
- Append-only candidate, proof, and measurement JSON records; derived static
  HTML reports; Codex orchestration; and a read-only local run dashboard.
- Plain-language dashboard presentation of hash-linked synthesis failures,
  including the recorded error code and message.
- Complete run-level counts and explicit incomplete-evidence reporting so a
  favorable candidate cannot hide a missing, failed, or regressed site.
- `PilotManifest v1`, a frozen feasibility lock, fast Python/package CI, and a
  separate pinned open-source tool-integration smoke workflow.
- A locally exercised, network-disabled tool container pinned to the Yosys
  0.63 release line, ABC 1.01, its Verilator binary, Python 3.13, uv 0.11.5,
  and the recorded Nangate45 Liberty digest.

### Changed

- Existing Agent V1 behavior remains the default; the updated plugin requests
  schema V2 explicitly.
- ML V2.2 remains diagnostic-only and is removed from MVP candidate selection
  and final decisions.
- Results are described as evidence from the recorded Yosys/ABC recipes, not as
  target-flow or production-PPA predictions.

### Evidence

- The first corpus tranche qualifies 8/12 Tier A references, including seven
  stateful modules and two documented equivalent pairs; the other four remain
  explicit blockers.
- All 12 references pass normalized compile/lint and both pinned baseline-only
  Yosys/ABC recipes. Three upstream suites pass 30/30 tests, and four PULP
  property-proof tasks pass in the pinned container.
- P2 positive equivalence and P3 ordered-transaction equivalence pass; all
  seven deliberately incorrect sequential/transaction controls fail.

- The generated end-to-end fixture formally passes and returns
  `synthesis_handles` under both synthesis recipes.
- The pre-registered open-RTL screen found 0 of 2 required qualifying modules.
  The gate stopped before candidate synthesis or PPA inspection; no replacement
  benchmark was selected after observing an outcome.

### Deferred

- A release tag and project license, pending owner confirmation.
- Two qualifying frozen open-source pilot modules.
- EQY, block/subsystem proof, technology-netlist equivalence, target-flow
  validation, live ML decisions, OpenROAD gating, MCP, proprietary RTL, and
  SoC-scale use.
