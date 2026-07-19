# Result interpretation

Lead with one of these engineer-facing states and retain the CLI reason.

| CLI state | Engineer-facing wording | Required action |
| --- | --- | --- |
| `recommended` | Recommended | Review the proposed isolated change and its evidence. |
| `synthesis_likely_handles` | Synthesis likely handles this | Do not promise implementation benefit; source cleanup is optional. |
| `target_flow_confirmation` | Target-flow confirmation needed | Confirm with the approved synthesis flow and constraints. |
| `no_change` | No change recommended | Leave the RTL unchanged for this finding. |
| `unsupported` | Analysis unavailable | Explain the unsupported input, construct, or tool condition. |
| `failed`, blocked status, or agent error | Analysis unavailable | Explain the exact model, tool, hash, or artifact problem. |

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

## Diagnostic-only models

When `model_release_status` is `diagnostic_only`, do not present its eligibility,
ranking, or predicted PPA as a recommendation. It is acceptable to explain that
the structural observation was found during evaluation, followed by the reason
live advice is unavailable.

## Candidate states

- `prepared`: an isolated diff exists; it is unproven.
- `rejected`: preparation, lint, integrity, or formal work did not pass.
- `passed` plus `safe: true`: current hash-matched formal equivalence passed.

Formal equivalence proves behavior for the checked baseline and candidate. It
does not prove PPA benefit, synthesis robustness, or production readiness.

## Language

Prefer “analysis unavailable,” “useful opportunities found,” “findings the tool
could evaluate,” and “incorrect recommendations.” Avoid exposing internal model
policy terms unless the engineer asks for implementation details.
