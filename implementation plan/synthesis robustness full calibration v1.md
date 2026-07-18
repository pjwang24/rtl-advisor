# RTL Advisor Full-Calibration Synthesis Robustness V1

## 1. Objective

Measure whether RTL candidate benefits survive a stronger synthesis recipe over
the complete generated calibration population, then produce flow-robust labels
for the next recommendation model.

This is a calibration-only experiment. It uses no company RTL, proprietary RTL,
held-out RTL, blind labels, or commercial synthesis results. Yosys/ABC is an
open-source calibration backend, not a substitute for Cadence Genus or signoff.

The experiment expands the completed 27-case synthesis-redundancy pilot to all
936 frozen V2/V2.1 calibration cases.

## 2. Frozen population

Use exactly the cases in the hash-verified V2.2 failure diagnostic:

```text
artifacts/models/v22/failure-diagnostics.json
```

Required population:

- 360 `calibration-v2` cases.
- 576 `calibration-v21` cases.
- 936 cases total.
- Nine registered RTL families.
- Baseline `v0` plus equivalent candidates `v1`-`v3` per case.
- 3,744 stronger-synthesis runs.
- 2,808 candidate comparisons and flow-robust labels.

Sort the plan by family and case ID. Freeze every case ID, family, topology
signature, diagnostic category, manifest hash, source hash, current formal-proof
hash, standard-synthesis result hash, and standard mapped-netlist hash before any
new full-sweep synthesis starts.

Also freeze the V2.1 calibration training table:

```text
artifacts/models/v21/calibration-rows.json
```

It must contain exactly 2,808 rows aligned one-to-one by case ID and template ID
with the candidate population. Its existing kernel features are copied into the
new training table without modification.

Once the plan exists, dependency drift is an error. Do not replace, regenerate,
or silently omit a case after stronger-synthesis results are visible.

## 3. Correctness prerequisite

Every candidate must have a current successful RTL-to-RTL formal-equivalence
record:

- Status is `equivalent`.
- The expected result was met.
- Baseline and candidate hashes match the manifest.
- The proof artifact hash matches the frozen plan.

The full sweep never runs an unproven candidate. Formal equivalence establishes
behavioral agreement for the generated candidate; it does not establish PPA
value or production suitability.

## 4. Synthesis recipes

Compare each candidate with the baseline separately inside two recipes. Both
recipes use the same Yosys binary, pinned Nangate45 Liberty file, driving cell,
output load, flattening, flip-flop mapping, and constrained non-fast ABC mapping.

### 4.1 Standard recipe

Reuse the frozen `yosys-abc-nangate45-v2` artifacts. Do not rerun or overwrite
them. The standard recipe already includes Yosys coarse and fine optimization,
normal resource sharing, width reduction, technology mapping, and constrained
ABC mapping.

### 4.2 Stronger recipe

Use the same stronger recipe as the completed 27-case pilot. After the normal
coarse stage, run:

```text
share -aggressive
opt -full
clean
```

Then complete the normal fine stage, flip-flop mapping, and constrained ABC
mapping. Retain scripts, constraints, logs, statistics, mapped netlists, tool
versions, source hashes, and cache keys.

The runner must be resumable. A cached result is reusable only when its plan,
flow, source, top module, tool, library, and constraint hashes match.

## 5. PPA comparison

For both recipes, compare each candidate with that recipe's `v0` result:

- Delay improvement:
  `(baseline_delay - candidate_delay) / baseline_delay * 100`.
- Area improvement:
  `(baseline_area - candidate_area) / baseline_area * 100`.
- Cell-count improvement:
  `(baseline_cells - candidate_cells) / baseline_cells * 100`.

A candidate is useful under one recipe when either condition holds:

- Delay improves by at least 3% and area does not worsen by more than 10%.
- Area improves by at least 5% and delay does not worsen by more than 2%.

Classify each metric direction using a fixed ±1% neutral band:

- Greater than 1%: `improve`.
- Less than -1%: `degrade`.
- Otherwise: `neutral`.

Two directions are compatible when they match or either direction is neutral.
Delay and area must be compatible across recipes for a candidate to receive the
flow-robust useful label. Cell-count compatibility is reported but is not a
label gate.

## 6. Frozen candidate labels

Assign exactly one candidate class in this order:

1. `robust_useful`: useful under both recipes with compatible delay and area
   directions.
2. `flow_conflict`: useful under both recipes but delay or area direction
   conflicts across recipes.
3. `absorbed_by_stronger_synthesis`: useful under the standard recipe but not
   useful under the stronger recipe.
4. `stronger_recipe_only`: not useful under the standard recipe but useful under
   the stronger recipe.
5. `synthesis_absorbed`: useful under neither recipe, stronger-recipe delay,
   area, and cell count are all neutral, and the baseline/candidate mapped cell
   histograms match.
6. `not_useful`: every remaining candidate.

A case contains a flow-robust opportunity when at least one candidate is
`robust_useful`.

When multiple robust candidates exist, retain all labels. Also identify the
best robust candidate using the existing measured balanced-profile utility
within the stronger recipe, breaking ties by template ID. This experiment does
not retrain a model or select a recommendation threshold.

## 7. Training table

Write one row per candidate with:

- Original V2.1 kernel features and their source feature-schema hash.
- Case ID, family, topology signature, training split, template ID, and
  transformation ID.
- Original standard-flow targets and eligibility label.
- Standard and stronger delay, area, and cell-count improvements.
- Per-recipe useful flags.
- Per-metric direction labels and compatibility flags.
- Candidate class, `robust_eligible`, and `robust_best`.
- Source plan hash and semantic row hash.

Write both JSON and JSON Lines. The table must contain exactly 2,808 unique
`(case_id, template_id)` keys, align exactly with the frozen calibration table,
and contain no held-out cases.

The flow-robust target is additive. Do not overwrite the original V2.1
calibration rows or reinterpret old benchmark results.

## 8. Report

Report globally and for every family:

- Case, candidate, proof, and synthesis-run counts.
- Standard useful candidates and cases.
- Stronger-recipe useful candidates and cases.
- Robust useful candidates and opportunity cases.
- Standard useful candidates retained as robust.
- Counts for all six candidate classes.
- Delay, area, and cell-count direction compatibility.
- Number of robust opportunity and non-opportunity cases available for future
  training.
- Families with at least ten robust opportunity cases and ten robust
  non-opportunity cases.

The report must use plain product language alongside exact internal labels. It
must state that the population is generated calibration RTL and that no blind or
commercial-tool evidence was used.

## 9. Interpretation rules

Use the completed sweep to choose the next model scope:

- A family is eligible for flow-robust model training only when it has at least
  ten robust opportunity cases and ten robust non-opportunity cases.
- A family below that support floor remains research-only or constant no-change
  until new generated calibration data is added.
- A candidate useful in only one recipe is target-dependent evidence, not a
  production recommendation.
- The experiment does not promote the advisor. A new sealed blind evaluation,
  block-scale open-RTL testing, and approved commercial-tool replication remain
  required.

The later production-pilot target is at least 80% retention of issued
recommendations across supported synthesis configurations. This full sweep
measures the available target population; it does not claim that the current
advisor achieves that target.

## 10. Artifacts and command

Write new artifacts without modifying the 27-case pilot or prior model data:

```text
artifacts/synthesis-robustness/full-calibration-v1/plan.json
artifacts/synthesis-robustness/full-calibration-v1/runs/<case>/<variant>/
artifacts/synthesis-robustness/full-calibration-v1/run-summary.json
artifacts/synthesis-robustness/full-calibration-v1/training-rows.json
artifacts/synthesis-robustness/full-calibration-v1/training-rows.jsonl
artifacts/synthesis-robustness/full-calibration-v1/report.json
artifacts/synthesis-robustness/full-calibration-v1/report.md
```

Add:

```text
rtl-advisor benchmark synthesis-robustness-full-v1 --workers 8
```

The command creates or verifies the frozen plan, resumes matching cached runs,
requires all 3,744 stronger-synthesis results, builds the training table, and
generates the report solely from frozen inputs and stored results.

## 11. Completion criteria

The sweep is complete only when:

- The plan contains exactly 936 cases and 3,744 runs.
- All 2,808 candidate proof dependencies are current and frozen.
- All 3,744 stronger-synthesis runs pass.
- All 2,808 training rows align with the original calibration table.
- No blind or held-out case is present.
- Every artifact has a semantic hash independent of timestamps and absolute
  paths where practical.
- The report can be rebuilt from the frozen plan and stored run artifacts.
- The full repository regression passes.

The next action after completion is to use the support and label distribution to
decide whether V2.3 should train all families, narrow its scope, or add targeted
generated calibration data before any new blind suite is created.
