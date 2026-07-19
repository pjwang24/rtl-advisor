# Result interpretation

Lead with one of these engineer-facing states and retain the CLI reason.

| CLI state | Engineer-facing wording | Required action |
| --- | --- | --- |
| `candidate_available` | Candidate available | Explain the source-linked structure; do not call it a recommendation. |
| `candidate_prepared` | Candidate prepared | Review the isolated diff; formal proof is still required. |
| `formal_passed` | Formally equivalent | The candidate may proceed to the two fixed synthesis recipes. |
| `formal_failed` | Formal proof failed | Reject the candidate and keep the original RTL. |
| `formal_inconclusive` | Formal proof inconclusive | Treat the candidate as unverified; do not measure or recommend it. |
| `measured_improvement` | Measured improvement in both Yosys recipes | Review the candidate while retaining the stated target-flow limitation. |
| `synthesis_handles` | Synthesis handles this in the tested recipes | Keep the original RTL unless readability alone justifies a change. |
| `flow_dependent` | Results depend on the recipe | Keep the original RTL and confirm separately in the target flow. |
| `regression` | Regression measured | Reject the candidate. |
| `incomplete` | Evidence incomplete | State which eligible sites or stages are missing; do not present a measured improvement or no-change conclusion. |
| `unsupported` or agent error | Analysis unavailable | Explain the exact unsupported input, tool, hash, or artifact condition. |

## Findings

For each finding, state:

1. Source file and line or source span.
2. Observed RTL structure.
3. Possible timing or area relevance.
4. Whether synthesis evidence exists and what it says.
5. Supported action.
6. Evidence type and limitations.

Use “predicted” for model values and “measured” only for synthesis or physical
results actually present in the evidence. Do not convert a prediction into a
target-flow claim.

## Rules and diagnostic-only models

MVP findings come from the released deterministic rule, not from ML. The V2.2
model remains diagnostic-only and cannot select a site, unlock a candidate, or
change a formal or synthesis result. Never present its ranking or predicted PPA
as an MVP recommendation.

## Candidate states

- `candidate_prepared`: an isolated diff exists; it is unproven.
- `formal_failed` or `formal_inconclusive`: the candidate cannot proceed.
- `formal_passed` plus `safe: true`: current hash-matched formal equivalence passed.

Formal equivalence proves behavior for the checked baseline and candidate. It
does not prove PPA benefit, synthesis robustness, or production readiness.

## Language

Prefer “analysis unavailable,” “useful opportunities found,” “findings the tool
could evaluate,” and “incorrect recommendations.” Avoid exposing internal model
policy terms unless the engineer asks for implementation details.
