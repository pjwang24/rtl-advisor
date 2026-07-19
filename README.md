# RTL Advisor

**RTL Advisor helps engineers find, verify, and measure promising RTL
optimizations before committing to expensive synthesis iterations.**

I am building RTL Advisor as an evidence-backed, pre-synthesis assistant for
SystemVerilog. It connects source-level findings to isolated candidate rewrites,
formal equivalence, and synthesis measurements instead of treating an ML or
language-model suggestion as proof.

> **Current status:** research prototype for generated and explicitly approved
> open RTL. The evaluation, formal, synthesis, CLI, and Codex workflows operate,
> but no recommendation model is approved for production use.

## Why this project exists

RTL structure can affect timing, area, and cell count, but engineers often learn
which choices mattered only after synthesis or physical design. Synthesis may
optimize many coding differences away, while reports rarely explain the result
as a clear source-level action.

The difficult problem is not generating another rewrite. It is determining:

- whether the rewrite preserves behavior;
- whether synthesis already performs the optimization;
- whether the benefit survives different synthesis settings; and
- whether evidence from previous designs applies to the current RTL.

RTL Advisor is intended to answer those questions before presenting a change as
useful.

## Intended workflow

```text
Generated or approved open RTL
              |
              v
     Parse, lint, and analyze
              |
              v
  Rules and calibrated ML model
              |
              v
 Source-linked engineering result
       |                 |
       |                 +--> No change / synthesis likely handles it
       v
Isolated candidate rewrite
              |
              v
      Formal equivalence
              |
              v
 Same-flow synthesis comparison
```

The original source is never modified automatically. The CLI is the authority
for decisions, hashes, proofs, and measurements; Codex explains the CLI result
but cannot override it.

The intended engineer-facing outcomes are:

- **Recommended** — evidence supports reviewing the proposed change.
- **Synthesis likely handles this** — a source rewrite is unlikely to improve
  the implementation.
- **Target-flow confirmation needed** — the result depends on technology,
  constraints, or synthesis settings.
- **No change recommended** — no supported candidate cleared the checks.
- **Analysis unavailable** — the design, tools, or model are outside the
  evidence currently available.

## What works now

The repository currently provides:

- deterministic generation of nine RTL transformation families;
- PySlang and Verilator linting;
- Yosys-based RTL-to-RTL formal equivalence;
- intentionally incorrect variants that must fail equivalence;
- Yosys/ABC synthesis against a pinned Nangate45 library;
- delay, area, and cell-count comparisons with recorded provenance;
- stronger synthesis recipes and OpenROAD physical cross-checks;
- isolated candidate workspaces and source-integrity checks;
- a versioned JSON CLI for terminal and automation clients;
- a Codex plugin backed by the same CLI results; and
- a local read-only dashboard for frozen evaluation evidence.

The complete regression currently contains **179 passing tests**.

### Current evidence

All current model evidence comes from generated RTL. It demonstrates the
evaluation system; it does not establish accuracy on arbitrary engineer RTL.

| Evidence | Result |
|---|---:|
| Generated calibration cases | 936 |
| Equivalent candidates proven in the full robustness sweep | 2,808/2,808 |
| Stronger-synthesis runs completed | 3,744/3,744 |
| Standard-flow benefits retained under the stronger flow | 314/391 (80.3%) |
| Cases containing a flow-robust opportunity | 199/936 |
| OpenROAD cases completed | 26/27 |
| Yosys/OpenROAD candidate-action agreement | 80.8% |
| V2.2 release score | 68.4%, below the frozen 70% requirement |

V2, V2.1, and V2.2 remain diagnostic-only. Live recommendations and candidate
generation stay disabled because no model has passed its release checks.

## Why more RTL data is required

The ML model does not memorize complete files. It learns relationships between
pre-synthesis structural features and measured changes in delay, area, and cell
count. Exact source text may be new, but the model can only be trusted when the
relevant structure is represented by sufficiently similar, independently
validated examples.

Raw RTL volume alone is not enough. I need a broader evidence set containing:

1. Diverse open RTL blocks with different architectures, sizes, coding styles,
   parameters, memories, state machines, clocks, and resets.
2. Multiple useful, neutral, and harmful candidate variants for supported
   transformation families. Variants may be generated; they do not all need to
   be authored manually.
3. Formal-equivalence results for every candidate used as equivalent PPA
   training data.
4. Baseline and candidate synthesis measurements using identical libraries,
   constraints, and tool settings.
5. Results from more than one synthesis recipe so the model can learn when a
   synthesis tool already handles a rewrite.
6. Entire designs and design families reserved for final testing and never used
   to tune the same model.

Yosys performs the current equivalence checks. EQY is planned for more scalable
sequential and block-level proofs; a commercial LEC flow can provide additional
validation later. Equivalence proves that a candidate matches its baseline for
the modeled behavior—it does not prove that the baseline meets its specification.

No finite training set can cover every possible RTL structure. A production
advisor must detect unfamiliar inputs and return **Analysis unavailable** rather
than force a prediction.

## What remains for the MVP

I consider the MVP complete only when one supported workflow operates from end
to end on realistic open RTL:

1. Pin diverse open-source designs, licenses, revisions, compile context, and
   source hashes without modifying upstream checkouts.
2. Run a blind review before exposing synthesis outcomes to the advisor.
3. Generate candidate variants in isolated workspaces.
4. Prove RTL-to-RTL equivalence and add RTL-to-netlist equivalence where the
   design and cell models permit it.
5. Synthesize baseline and candidate under at least two reproducible recipes.
6. Train a new model using design-separated calibration and test populations.
7. Reject unsupported transformation families and unfamiliar RTL structures.
8. Promote the model only after frozen accuracy, safety, physical-evidence, and
   formal-proof requirements pass.
9. Expose the complete review, candidate, proof, and synthesis-confirmation flow
   consistently through the terminal and Codex plugin.

Testing should begin with manageable open blocks before moving to larger
sequential IP. Generated RTL remains useful for controlled experiments, but it
cannot be the only evidence supporting the MVP.

## How engineers can interact with it

### Terminal and automation

The stable agent interface currently supports capability discovery, read-only
review, isolated candidate preparation, and formal verification:

```bash
rtl-advisor agent capabilities --json
rtl-advisor agent review /absolute/path/to/design.sv \
  --top design_top \
  --objective timing \
  --json
rtl-advisor agent candidate <run-id> --finding <finding-id> --json
rtl-advisor agent verify <run-id> --candidate <candidate-id> --json
```

The current capability result reports that live recommendations are unavailable,
so the candidate command remains locked unless a future model passes the release
requirements.

### Codex plugin

The plugin translates an engineer's request into the same versioned CLI
operations and explains the unchanged result in plain language. It contains a
skill, not an MCP server, and does not independently generate a recommendation.

```bash
codex plugin marketplace add .
codex plugin add rtl-advisor@personal
```

After installation, an engineer can ask:

> Use RTL Advisor to review this approved open RTL module for timing and explain
> whether synthesis likely handles the finding.

The plugin checks tool and model readiness first. It preserves unavailable,
unsupported, failed, and stale results instead of replacing them with Codex's
opinion.

### Generated demonstration

Prerequisites include Python 3.13, `uv`, Yosys/ABC, and Verilator.

```bash
uv sync --no-editable
uv run --no-editable rtl-advisor setup

uv run --no-editable rtl-advisor corpus generate \
  --family adder_reduction_association \
  --suite development

uv run --no-editable rtl-advisor lint \
  corpus/development/dev_aa_0001
uv run --no-editable rtl-advisor equivalence \
  corpus/development/dev_aa_0001
uv run --no-editable rtl-advisor synth \
  corpus/development/dev_aa_0001
```

Synthesis is allowed only after the required, hash-matched equivalence proof
exists. Generated corpora, model artifacts, synthesis outputs, the Liberty file,
and the OpenROAD checkout are intentionally excluded from Git.

## Current boundaries

I do not consider the project ready for:

- recommendations on proprietary production RTL;
- automatic in-place RTL changes;
- blocking code reviews or releases based on predicted PPA;
- replacing signoff synthesis, timing analysis, formal verification, or
  engineering judgment; or
- claiming a PPA improvement before measuring it under a stated flow.

Development uses generated or explicitly approved open RTL. Measured evidence
remains separate from predictions, and a candidate is called behavior-preserving
only after a current, hash-matched formal proof.

## Documentation

- [V1 implementation plan](implementation%20plan/v1.md)
- [V2 implementation plan](implementation%20plan/v2.md)
- [V2.3 model plan](implementation%20plan/v2.3.md)
- [Full synthesis-robustness plan](implementation%20plan/synthesis%20robustness%20full%20calibration%20v1.md)
- [Codex plugin plan](implementation%20plan/codex%20plugin%20v1.md)
- [Progress updates](progress%20updates/)
