# RTL Advisor Synthesis-Redundancy Pilot V1

## Purpose

This pilot tests whether source-level RTL recommendations retain measurable value
after a capable synthesis flow has optimized both the original RTL and the
equivalent rewrite. It is an existential product check: a rewrite that synthesis
already absorbs must not be presented to an RTL engineer as a PPA improvement.

The pilot uses generated calibration RTL only. It does not use company RTL or any
held-out label. Its open-source Yosys/ABC result is a Genus surrogate and must not
be described as Cadence Genus evidence.

## Case selection

Select exactly three cases from each of the nine registered RTL patterns, for 27
cases total, using the frozen V2.2 calibration diagnostic and seed `20260718`.

For each pattern, select in this order when the category exists:

1. One case where the model found a useful change (`covered_best`).
2. One missed improvement (`no_candidate_clears_threshold` or
   `unsupported_family`).
3. One correct no-change case (`true_abstention`).

If a category does not exist for that pattern, fill the allocation from the next
stable-hash case not already selected. The stable hash covers the seed, family,
category, case ID, and topology signature. Freeze the 27 case IDs and the source
diagnostic hash before new synthesis starts.

## Correctness prerequisite

Every `v1`-`v3` result must have a successful, current RTL-to-RTL formal proof:

- Status is `equivalent`.
- Expected result was met.
- Recorded baseline and candidate RTL hashes match the manifest.

The pilot never synthesizes an unproven rewrite. Existing Yosys/ABC CEC artifacts
are hash-checked and referenced in the pilot record. Formal proof establishes
correctness; it does not establish incremental synthesis value.

## Synthesis recipes

Run the original `v0` and equivalent `v1`-`v3` through two recipes using the same
Yosys binary, Nangate45 Liberty file, driving cell, output load, flattening, and
ABC timing constraints.

### Standard full recipe

Reuse the immutable `yosys-abc-nangate45-v2` results. This recipe already runs
Yosys `synth -flatten -noabc`, including arithmetic normalization, width
reduction, peephole optimization, normal SAT-based resource sharing, full logic
optimization, technology mapping, and a constrained non-fast ABC mapping pass.

### Aggressive-sharing recipe

Run the same synthesis sequence but split the Yosys coarse and fine stages. After
the normal coarse stage, add:

```text
share -aggressive
opt -full
clean
```

Then run the normal fine stage, `dfflibmap`, and the same constrained ABC mapping.
This asks whether stronger synthesis sharing removes representation sensitivity
that was visible in the standard recipe.

All scripts, logs, statistics, mapped netlists, constraints, tool versions,
source hashes, and cache keys are retained.

## Comparison definitions

For every candidate and recipe, report improvement relative to that recipe's
`v0` result:

- Delay improvement: `(baseline_delay - candidate_delay) / baseline_delay`.
- Area improvement: `(baseline_area - candidate_area) / baseline_area`.
- Cell improvement: `(baseline_cells - candidate_cells) / baseline_cells`.

A candidate is a useful balanced-profile change when either condition holds:

- Delay improves by at least 3% and area does not worsen by more than 10%.
- Area improves by at least 5% and delay does not worsen by more than 2%.

A result is materially neutral when absolute delay, area, and cell changes are
all at most 1%. Also record whether the mapped non-internal cell histograms are
identical; this is a structural hint, not a proof of netlist identity.

## Classification

Classify every candidate:

- `survives_aggressive_synthesis`: useful in the aggressive recipe.
- `absorbed_by_aggressive_synthesis`: useful in the standard recipe but neutral
  or not useful after aggressive synthesis.
- `synthesis_absorbed`: neutral after aggressive synthesis with an identical
  mapped-cell histogram.
- `no_material_qor_change`: neutral after aggressive synthesis but the cell
  histogram differs.
- `representation_sensitive_tradeoff`: aggressive synthesis still produces a
  material difference that does not meet the balanced improvement guardrails.

Classify a case as having incremental source-level value only when at least one
candidate survives aggressive synthesis. Report candidate- and case-level
survival rates globally and by RTL pattern.

## Decision rule

This pilot does not promote or reject the product by itself. It determines the
next evidence requirement:

- If most standard-flow improvements disappear, pause V2.3 expansion and focus
  the project on inference blockers, constraints, hierarchy, memories, and
  architectural feedback.
- If meaningful improvements survive across multiple patterns, carry only those
  patterns into a commercial-tool replication.
- In every case, obtain approved Genus evidence on generated RTL before claiming
  incremental value over Genus.

## Artifacts and command

Write without changing existing synthesis results:

```text
artifacts/synthesis-redundancy/v1/plan.json
artifacts/synthesis-redundancy/v1/runs/<case>/<variant>/
artifacts/synthesis-redundancy/v1/report.json
artifacts/synthesis-redundancy/v1/report.md
```

Add:

```text
rtl-advisor benchmark synthesis-redundancy-v1
```

The command creates and hash-verifies the plan, resumes cached aggressive runs,
and builds the report solely from the frozen plan and stored run artifacts.
