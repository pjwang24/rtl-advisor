# RTL Advisor Corpus Strategy V1

> **Authority:** This document defines the durable RTL evidence-corpus
> contract for the project. A repository, file, or generated example is not a
> reference design merely because it is open source or synthesizable. It must
> pass the admission process below. The completed [MVP V1](MVP%20V1.md) is one
> vertical mechanism test and does not satisfy this corpus strategy by itself.

## 1. Purpose

Build a curated, hierarchical collection of real RTL designs and meaningful
implementation alternatives. Every retained alternative must be linked to its
frozen reference, checked under an explicit behavioral contract, and measured
under reproducible synthesis settings.

The corpus exists to answer larger engineering questions:

- Which RTL structures still matter after synthesis?
- Which changes help only a particular tool recipe or technology?
- Which recommendations generalize across repositories and hierarchy levels?
- When should RTL Advisor advise no change because synthesis already handles
  the structure?
- How far can module-level evidence be trusted at IP, subsystem, and SoC scale?

Raw file count is not a success metric. A directory containing thousands of
unqualified `.v` files is not a corpus and must never be described as golden
RTL.

## 2. Corpus scale and units

| Tier | Target | Counted unit | Primary purpose |
| --- | ---: | --- | --- |
| A | 50–100 | Standalone, independently buildable modules | Develop transformation, proof, and measurement coverage |
| B | 15–30 | Complete IP blocks, including required dependencies | Test hierarchy, parameters, protocols, and realistic compile contexts |
| C | 5–10 | Processor or accelerator subsystems | Test pipelines, multi-cycle behavior, and partitioned proof |
| D | 2–4 | Complete SoCs | Test integration, generated connectivity, build reproducibility, and system-scale reporting |

Counting rules:

- A multi-file IP counts once at Tier B; its files do not become separate Tier
  A entries unless a module has its own supported build and behavioral
  contract.
- Generated or modified alternatives never increase the reference-design
  count.
- Two configurations of one upstream design share one lineage. They may be
  separate benchmark configurations but cannot be split across training and
  held-out evaluation as independent designs.
- Forks, vendored copies, and renamed derivatives share the upstream lineage
  unless independent development can be demonstrated.
- A complete SoC counts only when its integration top and reproducible source
  dependency graph are included. A collection of disconnected IPs is not a
  Tier D entry.

The target range is a diversity goal, not permission to lower admission
standards. A tier remains incomplete if too few designs qualify.

## 3. Terminology

### Discovered source

An upstream repository or release that may contain useful RTL. Discovery alone
does not authorize download, redistribution, modification, or inclusion.

### Qualified reference

An exact upstream revision and configuration that passes the admission gates
and becomes the immutable correctness baseline for a benchmark lineage. The
term **reference** is preferred over **golden** because equivalence to an
upstream implementation does not prove that the implementation satisfies its
external specification or is PPA-optimal.

### Variant

An isolated alternative derived from, configured from, or independently paired
with a qualified reference. It can be correct, incorrect, neutral, improved,
or regressed. A variant is never silently promoted into the reference set.

### Evidence record

The append-only, hash-linked record containing the reference and variant
identity, proof result, tool context, measurements, warnings, failures, and
derived conclusion.

## 4. Engineering categories

The corpus must cover structures engineers recognize. The initial taxonomy is:

1. Arithmetic and datapath organization.
2. Selection, priority, and arbitration.
3. FIFOs, skid buffers, and ready/valid flow control.
4. Decode, address mapping, and routing.
5. Bus adapters, width conversion, mux/demux, and crossbars.
6. Pipeline boundaries, register placement, and bypass paths.
7. State machines, counters, queues, and scoreboards.
8. Memory interfaces, banking, and request/response tracking.
9. Processor and accelerator stage organization.
10. Generated hierarchy, top-level connectivity, clocks, resets, and CDC
    boundaries.

Clock-domain and reset transformations require dedicated assumptions and are
discovery-only until their proof contracts are reviewed. Category counts must
be published so that a large number of near-duplicate arithmetic modules cannot
hide missing coverage elsewhere.

## 5. Reference admission contract

A discovered source progresses through these states:

```text
discovered
    ↓
license_reviewed
    ↓
source_pinned
    ↓
build_reproduced
    ↓
behavior_baselined
    ↓
reference_qualified
    ↓
variant_eligible
```

### 5.1 Provenance and licensing

Record:

- Canonical upstream repository URL.
- Exact commit, signed tag, or release archive and its content hash.
- License identifier, license-file hash, per-file exceptions, and attribution.
- Whether local modification and redistribution are permitted for the intended
  use. Corporate legal approval remains external to RTL Advisor.
- Upstream project and lineage identifiers used for data splitting.

No source may be copied into the distributable Python wheel or plugin merely
because its repository is public.

### 5.2 Complete compile context

Record and hash:

- Top module and source/filelist order.
- Include directories, defines, parameters, packages, and generated inputs.
- Tool frontend and version.
- Clock and reset definitions.
- Black boxes, memory models, vendor primitives, and environment assumptions.
- Build, lint, simulation, and upstream-test commands.

The exact frozen checkout must reproduce from a clean workspace. A module that
only compiles after undocumented manual editing is rejected.

### 5.3 Behavioral baseline

At least one explicit behavioral basis is required:

- Upstream tests or reference model.
- Assertions or formal properties.
- A published protocol or architectural specification.
- A trusted paired implementation with documented equivalent behavior.

The baseline result and known upstream failures are retained. RTL Advisor must
not repair the reference before freezing it and then present the repaired code
as upstream RTL.

### 5.4 Synthesis baseline

Before variant outcomes are inspected, freeze:

- Supported synthesis profiles and constraints.
- Tool, script, library, and environment hashes.
- Expected output metrics and normalization rules.
- Timeout and failure policy.

Every qualifying reference is measured even if no useful variant is later
found. Failures and exclusions remain visible.

## 6. Variant contract

Allowed variant origins are:

1. A deterministic RTL Advisor rewrite.
2. An upstream parameter or documented alternative implementation.
3. A separately authored open implementation with an explicit behavior match.
4. A manually reviewed, isolated Codex-generated candidate.
5. A deliberately incorrect negative control.

Each variant must record its generation method, parent reference, changed
source spans, complete diff, parameters, hashes, and expected proof contract.
The reference checkout remains byte-identical.

Variants are grouped into four classes:

| Class | Example | Required comparison |
| --- | --- | --- |
| Local combinational | Logic factoring or balanced expression | Direct RTL equivalence |
| Same-latency sequential | Internal state or mux rewrite with unchanged visible cycles | Cycle-aligned sequential equivalence |
| Latency-changing transaction | Added pipeline stage, different FIFO depth, fast/slow execution unit | Ordered transaction or trace equivalence with explicit latency rules |
| Structural integration | Crossbar, generated hierarchy, banking, or top-level connection change | Connectivity checks plus partitioned protocol/property proof |

Simulation is supporting evidence, not a substitute for a required proof. If
the required proof method is unavailable, the result is `proof_unsupported` or
`proof_inconclusive`; it is not treated as equivalent.

## 7. Proof ladder

| Level | Scope | Initial mechanism | Claim allowed |
| --- | --- | --- | --- |
| P0 | Parse, elaborate, lint | Verilator, Yosys, or supported frontend | Build context is usable |
| P1 | Combinational module | Current direct Yosys RTL miter | Equal under recorded two-state combinational semantics |
| P2 | Same-latency sequential module/IP partition | EQY or a reviewed sequential miter with bounded/unbounded strategies | Cycle-aligned equivalence under recorded reset and input assumptions |
| P3 | Different latency or buffering | Transaction scoreboard, refinement mapping, and formal properties | Accepted transactions preserve values, ordering, and permitted latency |
| P4 | Subsystem partitions | Assume-guarantee interfaces and partitioned proofs | Covered partitions preserve their contracts |
| P5 | SoC integration | Generated connectivity comparison, assertions, smoke execution, and selected partition proofs | Recorded integration properties hold; not whole-SoC equivalence |
| P6 | Target commercial flow | Conformal, Formality, or approved equivalent | Tool-specific target-flow LEC result |

EQY is therefore part of the planned sequential proof layer, not a retroactive
claim about the completed combinational MVP. A change in latency cannot be
validated by merely aligning same-cycle outputs in EQY.

Every positive proof suite must include mutation controls such as operand
removal, state corruption, dropped transactions, duplicated transactions,
incorrect reset values, or broken connections, as appropriate to the level.

## 8. Measurement ladder

| Level | Flow | Purpose |
| --- | --- | --- |
| M0 | Pinned Yosys/ABC standard recipe | Fast, reproducible logical synthesis evidence |
| M1 | Pinned stronger Yosys/ABC recipe | Sensitivity check against synthesis normalization |
| M2 | Pinned OpenROAD flow | Placement/routing and physical-timing cross-check |
| M3 | Approved commercial synthesis | Correlate recommendations with the engineer's target synthesis flow |
| M4 | Approved production implementation flow | Establish product-specific PPA evidence |

Every metric is namespaced by flow, library, constraint set, and tool hash. A
Yosys result must never be presented as a Genus, Design Compiler, or post-route
prediction. Neutral and regressed results are retained alongside improvements.

## 9. Data-splitting and ML policy

The training unit is a proof-qualified reference/variant outcome, not a source
file. Dataset splits must be disjoint by:

- Canonical upstream repository and fork lineage.
- Reference design and all of its variants.
- Structural topology or canonical graph family.
- Parameter family when configurations share the same generator.
- Subsystem and containing SoC, preventing child modules from leaking across a
  system-level holdout.

Tier D SoCs and at least one independent source in each supported category stay
outside model fitting as release evaluation. Pilot or benchmark outcomes cannot
be used to tune thresholds and then be reported as held-out results.

ML remains outside the live recommendation path until the corpus has enough
independent proof-qualified outcomes, repository-disjoint evaluation succeeds,
and deterministic rules remain available as a baseline. Codex may orchestrate
and explain evidence; it cannot override proof or measurement outcomes.

## 10. Initial upstream discovery set

These are discovery sources, not yet qualified references:

| Intended tier | Upstream | Initial categories |
| --- | --- | --- |
| A | [OpenTitan primitives](https://github.com/lowRISC/opentitan/tree/master/hw/ip/prim/rtl) | Arbitration, FIFO, counters, datapath primitives |
| A | [PULP common_cells](https://github.com/pulp-platform/common_cells) | Ready/valid stages, FIFO, arbitration, stream topology |
| A | [verilog-axis](https://github.com/alexforencich/verilog-axis) | Pipeline and stream structures; frozen research source because upstream marks it deprecated |
| B | [PULP AXI](https://github.com/pulp-platform/axi) | Cuts, FIFOs, adapters, mux/demux, crossbars |
| B | [OpenTitan TL-UL](https://github.com/lowRISC/opentitan/tree/master/hw/ip/tlul/rtl) | Protocol adapters, sockets, FIFOs, routing |
| C | [lowRISC Ibex](https://github.com/lowRISC/ibex) | Two-/three-stage CPU, prefetch, load/store, mult/div |
| C | [OpenHW CV32E40P](https://github.com/openhwgroup/cv32e40p) | Frozen CPU configuration and four-stage pipeline |
| C | [OpenHW CVFPU](https://github.com/openhwgroup/cvfpu) | Configurable floating-point pipeline |
| D | [OpenTitan](https://github.com/lowRISC/opentitan) | Generated top, TL-UL fabrics, complete SoC integration |

Additional Tier B–D sources must be selected before their synthesis outcomes
are inspected. Strong reciprocal licenses, mixed-license repositories, and
deprecated upstreams require explicit disposition in the registry.

## 11. Corpus completion gates

A tier is complete only when:

- Its target count is met with qualified references, not files or variants.
- At least three independent upstream lineages are present where the tier size
  permits.
- Category and license distributions are published.
- Every reference has reproducible build and baseline records.
- Every published variant has a terminal proof state and, when proof passes,
  terminal measurement states.
- All ties, neutral outcomes, regressions, unsupported cases, and failures are
  included.
- A clean-machine audit reconstructs the registry without relying on developer
  home-directory state.

## 12. Immediate tranche

The first implementation tranche is deliberately smaller than the final Tier A
target while exercising the complete admission process:

- Pre-register 12 Tier A reference candidates.
- Use at least three upstream repositories and four engineering categories.
- Include at least four stateful modules and two upstream-documented
  alternative implementations.
- Freeze source metadata before any reference-versus-variant PPA comparison.
- Qualify, reject, or mark each candidate blocked without replacing an
  unfavorable result.

The first preferred pair is OpenTitan `prim_arbiter_tree` and
`prim_arbiter_ppc`. Preferred follow-ons are PULP spill/stream registers, FIFOs,
round-robin arbitration, and PULP AXI cut/multicut structures. Completing this
tranche validates the registry and proof ladder; it does not complete Tier A.
