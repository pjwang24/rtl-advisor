---
name: explore-rtl-evidence
description: Explore and visualize existing immutable RTL Advisor measurements, regressions, candidate outcomes, recipe sensitivity, and cohort coverage through compact read-only chart specifications. Use for questions about stored evidence, plots, comparisons, or regression distributions. Do not use to review new RTL, create candidates, run formal proof, or launch synthesis.
---

# Explore RTL Evidence

Use RTL Advisor's stored, hash-validated evidence as the only data source. This
skill is read-only with respect to RTL, candidates, proofs, measurements, and
their classifications.

## Workflow

1. Locate this skill directory and invoke the shared runner at
   `../analyze-rtl/scripts/run_rtl_advisor.py`.
2. Run `evidence explore` with exact filters from the engineer's request. Keep
   the filter set empty only when a full-cohort overview is requested.
3. Read the compact `rtl-advisor.evidence.exploration` result. Do not load
   `chart-data.json` into model context unless the engineer requests exact
   points or a structured failure requires inspection.
4. Explain counts and chart meanings without changing the stored profile
   classification or aggregate candidate decision.
5. Link the compact exploration and point-level dataset artifacts. When the
   engineer asks to view the interactive charts, use the existing local
   frontend and its `/?view=explore` route.

## Command

```bash
python3 <skill-dir>/../analyze-rtl/scripts/run_rtl_advisor.py evidence explore \
  [--workflow-id <workflow-id>] \
  [--run-id <run-id>] \
  [--profile standard|stronger] \
  [--objective timing|area|balanced] \
  [--classification improved|neutral|regressed] \
  [--decision measured_improvement|synthesis_handles|flow_dependent|regression] \
  [--transformation <id>] \
  [--source-kind agent_v2_run|family_study]
```

Filters may be repeated. The result contains small bar-chart rows and a scatter
spec; full scatter points remain in the referenced dataset artifact. Family
study labels `M0` and `M1` are retained in `recorded_profile` and normalized to
the comparable roles `standard` and `stronger` in `profile`.

## Interpretation boundaries

- `classification` describes one synthesis profile: `improved`, `neutral`, or
  `regressed`.
- `decision` combines the two pinned profiles: `measured_improvement`,
  `synthesis_handles`, `flow_dependent`, or `regression`.
- An aggregate regression means at least one profile was `regressed`; do not
  imply both profiles regressed unless the profile counts and points show that.
- Every plotted point must have `formal_status: formal_passed`, `safe: true`,
  and a valid measurement semantic hash.
- Positive delay, area, and cell-count percentages mean the candidate reduced
  that measured cost.
- Results apply only to the recorded recipes and Liberty file, not a target
  implementation flow.

Treat `status: partial` and exit code `4` as an evidence-quality warning:
invalid sources were excluded, so report the exclusion and avoid complete-cohort
claims. Treat schema, document-type, semantic-hash, or exit-code disagreement as
untrusted automation and stop.
