# RTL Advisor

RTL Advisor is an experimental pre-synthesis review tool for SystemVerilog. It
tries to identify RTL changes that could improve timing or area, explain the
reason at the source-code level, and verify that a proposed rewrite preserves
the design's behavior.

> **Current status:** research prototype for generated and approved open RTL.
> The evaluation and formal-verification harness is working, but the
> recommendation engine is not yet ready for production RTL decisions.

## The problem

RTL engineers make structural choices that can influence timing, area, and cell
count. The effect is often discovered only after synthesis or physical design,
when changing the RTL is slower and more disruptive.

Existing tools each solve part of the problem:

- Lint finds syntax, safety, and coding problems, but usually does not predict
  implementation value.
- Synthesis performs many optimizations automatically, but it does not always
  explain which source-level choice mattered.
- Timing analysis identifies critical paths after implementation work has
  already started.
- Formal equivalence proves that two designs behave the same, but it does not
  say whether one implementation is faster or smaller.
- A language model can explain RTL and suggest changes, but a suggestion alone
  is not correctness or PPA evidence.

The main bottleneck is therefore not generating another rewrite. It is deciding
which rewrite is safe, useful, and likely to remain useful after a strong
synthesis tool has optimized both versions.

## A simple example

Consider an addition written as a serial expression:

```systemverilog
assign y = a + b + c + d;
```

A tool might suggest a balanced form:

```systemverilog
assign y = (a + b) + (c + d);
```

The balanced form may reduce logic depth, but the real answer depends on widths,
signedness, constraints, technology, and synthesis settings. RTL Advisor is
being built to do more than show the second code sample:

1. Locate the source pattern.
2. Explain the possible timing and area tradeoff.
3. Generate the alternative in an isolated workspace.
4. Prove the candidate equivalent to the baseline.
5. Compare implementation evidence when synthesis is available.
6. State whether the change is recommended, unnecessary, or target-dependent.

The original RTL is never modified automatically.

## Our approach

```text
Generated or approved RTL
          │
          ▼
 Parse, lint, and structural analysis
          │
          ▼
 Rules and calibrated prediction
          │
          ▼
 Source-linked finding and explanation
          │
          ├── no change recommended
          ├── synthesis likely handles it
          ├── target-flow confirmation needed
          └── candidate created in isolation
                         │
                         ▼
                  Formal equivalence
                         │
                         ▼
                Optional synthesis evidence
```

The CLI is the single execution and evidence engine. Codex may translate an
engineer's request into CLI operations and explain the result, but it cannot
override the deterministic decision, formal proof, or measured evidence.

Normal user-facing outcomes are intended to be:

- **Recommended** — evidence supports reviewing the proposed change.
- **Synthesis likely handles this** — the rewrite may improve readability, but
  implementation benefit is not expected.
- **Target-flow confirmation needed** — the result depends on synthesis settings
  or technology.
- **No change recommended** — no safe and useful candidate cleared the release
  checks.
- **Analysis unavailable** — the input, tools, or model are not ready for a
  trustworthy result.

## How engineers will use it

RTL Advisor is designed around several interfaces backed by the same CLI:

- **Terminal:** reproducible commands for analysis, formal verification,
  synthesis comparison, and automation.
- **Codex plugin:** conversational requests such as “analyze this module for
  timing risks” or “generate the candidate and run equivalence.”
- **Editor and code review:** future source annotations and non-blocking review
  comments.
- **Internal dashboard:** model readiness, evaluation evidence, and team-level
  review status.

The local dashboard currently presents frozen evaluation results only. Live RTL
recommendations remain disabled while the model release checks are failing.

The repository-owned plugin design is documented in
[`implementation plan/codex plugin v1.md`](implementation%20plan/codex%20plugin%20v1.md).

## What works today

- Deterministic generation of nine RTL transformation families.
- Verilator and PySlang linting.
- Yosys-based RTL-to-RTL formal equivalence checks.
- Intentional incorrect controls that must fail equivalence.
- Yosys/ABC synthesis against a pinned Nangate45 library.
- Delay, area, and cell-count comparison with immutable provenance.
- Isolated candidate generation that leaves the original RTL unchanged.
- OpenROAD placement-and-routing cross-checks on generated designs.
- Rules, calibrated models, and audited Codex explanations.
- A local, read-only evaluation dashboard.
- Reproducible benchmark plans, hashes, caches, and reports.

The complete repository regression currently contains **153 passing tests**.

## Current evidence

All results below use generated RTL. They demonstrate the evaluation system and
identify the remaining research gap; they do not establish production accuracy.

| Evidence | Current result | What it means |
|---|---:|---|
| Latest synthesis-robustness formal checks | 81/81 passed | Every candidate compared in that pilot was proven equivalent to its baseline. |
| Standard-flow benefits retained under stronger synthesis | 15/21 (71.4%) | Synthesis removes some apparent RTL benefits, but not all of them. |
| Benefits removed by stronger synthesis | 6 | Advice based on one synthesis recipe can be misleading. |
| Candidates useful only under the stronger recipe | 7 | Marginal PPA conclusions can depend on tool settings. |
| OpenROAD complete cases | 26/27 | The generated physical-design cross-check is operational. |
| Yosys/OpenROAD candidate-action agreement | 80.8% | The cheaper synthesis labels often, but not always, preserve the physical conclusion. |
| V1 best high-level decision result | 17/36 (47.2%) | The original rules-plus-Codex advisor was not accurate enough. |
| V2.2 useful changes found | 86/230 (37.4%) | The safer model misses too many real opportunities. |
| V2.2 incorrect recommendations | 4/90 (4.4%) | Recommendation safety is promising on calibration data. |
| V2.2 overall release score | 68.4%, below the 70% requirement | V2.2 correctly remains locked and diagnostic-only. |

The synthesis-robustness pilot used 27 cases and 108 stronger-synthesis runs.
Of the 21 candidates that looked useful under the standard recipe, 15 remained
useful, six were optimized away, and seven different candidates became useful
only under the stronger recipe. This supports flow-aware recommendations, but a
27-case generated pilot is not enough to generalize to large IP or SoC blocks.

The OpenROAD cross-check passed its registered evidence gate: 26 of 27 cases
completed, candidate-action agreement was 80.8%, and delay/area/cell-count
direction agreement ranged from 79.5% to 83.3%. This supports using Yosys as an
experimental label source; it is not a replacement for a target implementation
flow.

## Is it production-ready?

No—not as a trusted recommendation engine.

The project is ready for:

- Generated-RTL research and benchmarking.
- Demonstrating formal candidate validation.
- An opt-in, non-blocking internal evaluation using approved open RTL.
- Developing the CLI, Codex plugin, and dashboard around honest readiness
  states.

The project is not ready for:

- Production recommendations on proprietary block or SoC RTL.
- Automatically changing source RTL.
- Blocking a code review or release based on predicted PPA.
- Replacing signoff synthesis, timing analysis, formal tools, or engineering
  judgment.

Formal equivalence is an important safety gate, but it only proves behavioral
agreement for the checked candidate. It does not prove that the advisor chose a
useful candidate, that the result generalizes, or that the target synthesis flow
will preserve the benefit.

Before a production pilot, the project needs:

1. A much larger synthesis-robust calibration sweep across all generated cases.
2. Better coverage of useful changes without increasing incorrect advice.
3. A newly generated, sealed blind evaluation that is not used during tuning.
4. Replication with an approved commercial synthesis flow such as Genus, using
   generated RTL first.
5. Block-scale testing on diverse approved open designs, including realistic
   filelists, parameters, packages, macros, clocks, and constraints.
6. Engineer review studies measuring whether findings are understandable and
   useful in practice.
7. Internal deployment, security, runtime, and failure-handling validation.

Proposed production-pilot targets include:

- At least 80% of issued recommendations remain useful across the supported
  synthesis configurations.
- At least 50% of robust useful opportunities are found.
- No more than 5% of issued recommendations are harmful.
- At least 75% PPA-direction accuracy where a direction is shown.
- 100% current, hash-matched formal-equivalence success for candidates presented
  as behavior-preserving.

These are targets, not current claims.

## Quick demonstration

Prerequisites include Python 3.13, `uv`, Yosys/ABC, and Verilator. PySlang and
the training dependencies are installed through the project environment.

```bash
uv sync --no-editable
uv run --no-editable rtl-advisor setup
```

Generate and evaluate one adder-association example:

```bash
uv run --no-editable rtl-advisor corpus generate \
  --family adder_reduction_association \
  --suite development

uv run --no-editable rtl-advisor lint \
  corpus/development/dev_aa_0001

uv run --no-editable rtl-advisor equivalence \
  corpus/development/dev_aa_0001

uv run --no-editable rtl-advisor synth \
  corpus/development/dev_aa_0001

uv run --no-editable rtl-advisor analyze \
  corpus/development/dev_aa_0001 \
  --variant v0 \
  --mode rules
```

The equivalence command proves `v1`-`v3` equivalent to `v0` and confirms that
the intentionally incorrect `n0` control is not equivalent. Synthesis is allowed
only after the required proof exists.

Launch the local evaluation dashboard:

```bash
uv run --no-editable rtl-advisor frontend
```

Then open `http://127.0.0.1:8765`. The dashboard is local, read-only, and backed
by frozen V2.2 calibration evidence.

Generated corpora, model artifacts, synthesis outputs, the Liberty file, and the
OpenROAD checkout are intentionally not stored in Git. The CLI creates or
downloads them through the registered workflows.

## Next steps

The evidence track is the production bottleneck:

1. Generalize the 27-case synthesis-redundancy runner into a complete
   calibration sweep and create flow-robust labels.
2. Continue V2.3 calibration using those stronger labels and existing formal
   gates.
3. Run a fresh sealed blind benchmark only after calibration and physical checks
   pass.
4. Prepare a portable generated-RTL Genus handoff for the separate machine.
5. Evaluate diverse approved open blocks before considering proprietary RTL.

The interface track can proceed in parallel without claiming production
readiness:

1. Add a stable machine-readable CLI contract.
2. Build the repository-owned `rtl-advisor` Codex plugin and `analyze-rtl`
   skill.
3. Require terminal-versus-Codex result parity.
4. Reuse the same contract later for VS Code, CI, and the dashboard.

## Project documentation

- [V1 implementation plan](implementation%20plan/v1.md)
- [V2 implementation plan](implementation%20plan/v2.md)
- [V2.1 recovery plan](implementation%20plan/v2.1.md)
- [V2.2 family-risk plan](implementation%20plan/v2.2.md)
- [V2.3 recovery plan](implementation%20plan/v2.3.md)
- [Synthesis-redundancy plan](implementation%20plan/synthesis%20redundancy%20v1.md)
- [Frontend plan](implementation%20plan/frontend%20v1.md)
- [Codex plugin plan](implementation%20plan/codex%20plugin%20v1.md)
- [Progress updates](progress%20updates/)

## Operating principles

- Use generated or explicitly approved open RTL during development.
- Never modify source RTL in place.
- Keep measured evidence separate from predictions and explanations.
- Require formal equivalence before describing a candidate as safe.
- Never use held-out labels to tune the same version being evaluated.
- Fail closed when tools, models, hashes, or evidence are missing or stale.
- Treat commercial synthesis and signoff tools as external validation, not as
  assumptions.
