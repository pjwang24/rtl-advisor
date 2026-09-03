# RTL Advisor Plugin A/B Evaluation V1 — Results

## Decision

The installed plugin is a useful evidence and safety wrapper inside its narrow
supported envelope, but it currently reduces Codex's practical RTL-analysis
coverage when used as the exclusive workflow. On this mixed combinational block,
the plugin rejected the complete module because one unrelated output used
`always_comb`, while unrestricted Codex safely isolated and evaluated the
continuous-assignment adder plus the priority logic.

## Frozen-input integrity

- Before SHA-256: `73b16487216441d24cd7e8f831385429bc40ba02346d8dc8d3ed5af0a17a3e93`
- After SHA-256: `73b16487216441d24cd7e8f831385429bc40ba02346d8dc8d3ed5af0a17a3e93`
- Original input changed: no

## Results

### Arm A — Codex without RTL Advisor

Codex created three isolated candidates. Verilator lint passed for the baseline
and all candidates. Direct Yosys RTL equivalence passed for the balanced adder
and priority candidates; the carry-save experiment did not close its proof and
was rejected.

| Candidate | Area-oriented result | Timing-aware result | Decision |
|---|---|---|---|
| Balanced exact-width adder | Identical: 816 cells, 904.666 area | Identical: 825 cells, 944.300 area, 799.18 ps | Synthesis already handles it |
| Explicit priority formula | 0.85% less area | 0.51% more area and 2.64% slower | Reject for balanced PPA |
| Carry-save adder | 2.44% more area | 0.65% more area and 25.94% slower | Reject; also unproven |

Final action: no RTL change.

### Arm B — Codex with RTL Advisor

RTL Advisor returned `unsupported` with zero eligible sites. It excluded the
whole module under `procedural_or_generated_rtl` because the module contains an
`always_comb` priority encoder, even though the six-term continuous-assignment
adder is separate and source-identifiable.

- Run: `mvp-d8d0e8f6684d2997c8bf`
- Candidate count: 0
- Formal count: 0
- Measurement count: 0
- Final action: analysis unavailable
- Immutable report: `artifacts/agent-v2/runs/mvp-d8d0e8f6684d2997c8bf/report.json`

The plugin correctly preserved the input, recorded hashes and provenance, and
refused to make claims without evidence. The failure was coverage granularity,
not an unsafe or incorrect decision.

## Pre-registered score

| Dimension | Maximum | Without plugin | With plugin |
|---|---:|---:|---:|
| Correctness and safety | 25 | 25 | 25 |
| Evidence quality | 25 | 23 | 12 |
| Recommendation quality | 20 | 20 | 12 |
| Opportunity coverage | 15 | 15 | 2 |
| Reproducibility | 10 | 8 | 10 |
| Engineer usability | 5 | 5 | 4 |
| **Total** | **100** | **96** | **65** |

The control loses reproducibility points because its artifacts are temporary and
its commands contain placeholders. The plugin receives full reproducibility and
safety credit despite not completing PPA analysis because its immutable records,
hashes, and stage gates worked as designed.

## Product implication

Do not remove the plugin, but do not make it Codex's exclusive reasoning boundary.
The product should use a hybrid contract:

1. Codex performs broad source and design-context analysis.
2. RTL Advisor identifies supported sites at source-span granularity, even when
   unrelated regions are unsupported.
3. RTL Advisor owns candidate isolation, formal gating, fixed-flow measurement,
   immutable evidence, and final measured classifications.
4. Unsupported Codex ideas remain clearly labeled hypotheses until they pass the
   same proof and measurement pipeline.

The immediate defect to fix is module-wide procedural exclusion. A procedural
region should exclude only overlapping rewrite spans, not an independent safe
continuous assignment elsewhere in the module.

## Remediation rerun

The source-span eligibility fix was implemented and the frozen plugin arm was
rerun without changing the input.

- Corrected run: `mvp-49dc8c178f736fceadeb`
- Transformation contract: `balanced-unsigned-add-chain-v2`
- Eligible sites: 1
- Excluded regions: 1 (`always_comb` only)
- Candidate diff: addition expression only
- Verilator and pyslang lint: passed
- Whole-module Yosys equivalence: passed
- Standard synthesis: neutral, 855 cells, 966.91 area, 833.23 ps for both
- Stronger synthesis: neutral with the same metrics for both
- Final decision: `synthesis_handles`
- Immutable report: `artifacts/agent-v2/runs/mvp-49dc8c178f736fceadeb/report.json`

After remediation, the plugin reaches the same adder decision as unrestricted
Codex while retaining immutable evidence. It still does not explore the
priority-selection family, so the hybrid Codex-plus-evidence-engine architecture
remains necessary.

## Experiment limitation

This single generated example establishes a real integration flaw but not an
overall product ranking. The next A/B rounds should include a pure supported
adder, multiple mixed combinational blocks, an unsupported sequential block,
and frozen open-source primitives.
