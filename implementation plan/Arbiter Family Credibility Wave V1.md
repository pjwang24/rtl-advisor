# Arbiter Family Credibility Wave V1

> **Status:** study complete; family gate failed, product preview remains `study_only`  
> **Family:** `same_cycle_arbiter_topology`  
> **Frozen study:** `arbiter-family-credibility-v1`  
> **Decision boundary:** study-only unless the stronger product-preview gate passes

## Goal

Expand the completed OpenTitan PPC/tree experiment into ten distinct,
same-cycle arbiter reference/variant pairs spanning OpenTitan, PULP
`common_cells`, verilog-axis, and BaseJump STL. Each pair must be source-pinned,
structurally distinct, lint-clean, and formally proven under its declared P1 or
P2 contract before any candidate PPA is available.

The wave determined whether the arbiter transformation family has repeatable
value beyond one example. It does not establish general RTL optimization,
commercial-flow value, or production readiness.

## Frozen cohort

The immutable machine-readable cohort is
[family-study.json](../examples/corpus/arbiter_family_v1/family-study.json).
Parameter configurations are measurements of one pair; they never count as
additional references.

| Pair | Reference | Candidate | Proof |
| ---: | --- | --- | --- |
| 1 | OpenTitan `prim_arbiter_ppc` | Upstream `prim_arbiter_tree` | P2 |
| 2 | OpenTitan `prim_arbiter_fixed` | Flat prefix/one-hot selector | P1 |
| 3 | PULP `rr_arb_tree` | Rotated-mask selector | P2 |
| 4 | PULP `stream_arbiter_flushable` | Alternative arbitration/selection cone | P2 |
| 5 | verilog-axis `arbiter` | Hierarchical cyclic encoder | P2 |
| 6 | verilog-axis `axis_arb_mux` | Alternative arbiter and balanced payload selection | P2 |
| 7 | BaseJump `bsg_arb_fixed` | Balanced fixed-priority selector | P1 |
| 8 | BaseJump `bsg_arb_round_robin` | Rotated-mask selector | P2 |
| 9 | BaseJump `bsg_locking_arb_fixed` | Alternative priority cone with original lock state | P2 |
| 10 | BaseJump `bsg_round_robin_n_to_1` | Upstream `use_scan_p=1` alternative | P2 |

The ordered reserves are PULP `prioarbiter`, PULP `rrarbiter`, and BaseJump
`bsg_round_robin_arb`. A reserve is legal only when a primary fails source
qualification or formal proof before any PPA is run.

## Frozen sources and measurements

- OpenTitan:
  `99fb7bd3b2330a1bda61b1d3382198d0c7fee8d7`.
- PULP common_cells:
  `1281545696eb3fcba50ec5b4275993476a3c710e`.
- verilog-axis:
  `48ff7a7e2ef782cf778d47910cf85835c64b1bce`.
- BaseJump STL:
  `b48037e28544425839dbd617d45b1a82631bc1a9`.

Each pair has four frozen widths or port counts. The five M2 selections and
their ordered fallbacks are stored in the machine-readable manifest and cannot
change after PPA becomes visible.

## Product architecture

Agent V2 keeps the engineer workflow:

```text
review → candidate → verify → measure → report
```

The transformation registry declares supported proof levels and references.
The executor registry owns pair-specific execution through:

```text
findings(reference)
prepare_candidate(reference, finding)
verify_candidate(candidate)
measurement_designs(candidate)
negative_controls(candidate)
```

Agent V2 dispatches through executor ID/version records. Adding another pair
must not require a new OpenTitan-specific conditional in Agent code.

Family studies use:

```text
rtl-advisor study validate <family-study.json> --json
rtl-advisor study run <family-study.json> --repeat repeat-1 --json
rtl-advisor study run <family-study.json> --repeat repeat-2 --json
rtl-advisor study report <study-id> --json
```

The immutable schemas are `rtl-advisor-family-study-v1` and
`rtl-advisor-family-evidence-v1`. Existing Agent V1 responses and stored Agent
V2 records remain readable.

## Candidate and formal sequence

For every pair:

1. Verify source, revision, license, dependencies, and test assets.
2. Reproduce compile and lint in the frozen context.
3. Create the candidate only in an isolated workspace.
4. Confirm normalized elaborated RTL is structurally distinct.
5. Freeze source, diff, configuration, proof contract, and hashes.
6. Run P1 for stateless pairs or P2 for stateful/handshake pairs.
7. Require priority, grant, state/reset, and payload controls as applicable to
   produce counterexamples.
8. Retain failed candidates and counterexamples.
9. Permit compile/formal corrections only while PPA is unavailable.
10. Unlock M0/M1/M2 only for a current hash-linked formal pass.

The P2 contract initializes reset, permits arbitrary protocol-legal traffic,
holds blocked requests/payloads stable when required, leaves ready/ack/yumi
unconstrained otherwise, compares handshake/grant behavior every cycle, and
compares index/payload whenever semantically valid.

## Measurement

- Run M0 standard and M1 stronger Yosys/ABC for every passing configuration.
- Run the full study twice from clean pinned environments.
- Require exact normalized M0/M1 results and netlist hashes.
- Run the five frozen Nangate45 M2 samples at 10 ns with fixed-die calculation
  and 35% target utilization.
- Require matching M2 direction and at most 2% area/delay drift.
- Publish improved, neutral, regressed, disagreed, failed, inconclusive, and
  blocked results without filtering.

## Gates

The family credibility gate requires ten proven pairs, at least three
lineages, all five M2 samples completed or honestly blocked, at least 95%
reproducibility, no invalid recommendation, and at least one M0/M1/M2-confirmed
improvement.

`supported_preview` is stricter: two improved pairs from two lineages, two M2
confirmations from two lineages, no M0/M1 regression among recommended
configurations, and a current hash-linked formal pass for every recommendation.
Otherwise the family remains `study_only`.

## Implementation checklist

### Freeze and contracts

- [x] Pin BaseJump master to an exact commit and record the license hash.
- [x] Freeze ten primary pairs, three ordered reserves, four configurations per
  pair, five M2 samples, and M2 fallbacks before new PPA.
- [x] Add immutable family-study validation and forbid pre-freeze PPA fields.
- [x] Add separate family-credibility and product-preview gates.
- [x] Add exact M0/M1 and 2%-bounded M2 repeat comparison.

### Generic product path

- [x] Add an executor registry with duplicate ownership, proof-level, origin,
  version, and registry-hash validation.
- [x] Route the completed OpenTitan Agent V2 path through its executor.
- [x] Preserve legacy transformation registry hashes for existing V2 records.
- [x] Add Agent capability metadata for executor IDs and versions.
- [x] Add `study validate`, `study run`, and `study report`.
- [ ] Complete Agent V2 end-to-end tests for every new qualified reference.

### Pair implementation and proof

- [x] Implement isolated P1 candidates for OpenTitan fixed priority and
  BaseJump fixed priority.
- [x] Lint both P1 candidates against their upstream compile contexts.
- [ ] Pass both P1 candidates in the pinned yosys-slang environment; local
  native Yosys correctly reports unsupported upstream SystemVerilog as
  inconclusive.
- [x] Implement the PULP `rr_arb_tree` P2 candidate/miter and lint both sides.
- [x] Implement the PULP stream-arbiter P2 candidate/miter and lint both sides.
- [x] Implement the verilog-axis arbiter P2 candidate/miter and lint both sides.
- [ ] Implement and freeze the verilog-axis arb-mux P2 candidate/miter.
- [ ] Implement and freeze the BaseJump round-robin P2 candidate/miter.
- [ ] Implement and freeze the BaseJump locking-arbiter P2 candidate/miter.
- [ ] Implement and freeze the BaseJump n-to-1 P2 parameter comparison.
- [ ] Require every applicable negative control to produce a counterexample.
- [ ] Freeze all passing candidates before measurement.

### Evidence and release

- [ ] Execute M0/M1 twice for every proof-passing configuration.
- [ ] Execute the five frozen M2 samples twice.
- [ ] Publish complete family evidence and both gates.
- [ ] Run the complete repository regression and package/install checks.
- [ ] Update the Codex plugin only after the generic Agent V2 path is stable.
- [x] Leave ML diagnostic-only and leave dashboard UX unchanged.

## Current blocker and intervention

The study is complete. One frozen M2 sample remains blocked by a clock-port
adapter mismatch in the verilog-axis `axis_arb_mux` wrapper, so the family
gate stays closed and the product preview remains `study_only`.
