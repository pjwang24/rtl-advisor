# RTL Advisor Plugin A/B Evaluation V1

## Question

Does adding the RTL Advisor plugin make Codex's pre-synthesis PPA analysis more
useful and trustworthy, or does the plugin's current product boundary suppress
useful RTL engineering work?

## Frozen input

- Generated block: `input/telemetry_reduce.sv`
- Top: `telemetry_reduce`
- Objective: balanced timing and area
- Input SHA-256: `73b16487216441d24cd7e8f831385429bc40ba02346d8dc8d3ed5af0a17a3e93`
- No proprietary or third-party RTL is used.

The block deliberately contains a width-safe six-term addition chain plus
comparison and priority-selection logic. This tests the plugin's supported
family while leaving room for unrestricted Codex to identify other structures.

## Arms

### A — Codex without RTL Advisor

Codex may inspect, rewrite, lint, formally prove, and synthesize the block using
local tools, but must not use the RTL Advisor plugin, CLI, corpus, or prior
artifacts.

### B — Codex with RTL Advisor

Codex must follow the installed `rtl-advisor:analyze-rtl` skill and use its
versioned CLI contract for review, candidate preparation, formal verification,
both synthesis recipes, and reporting. It must not override or broaden the
stored decision.

Both arms use GPT-5.6-sol with xhigh reasoning effort and cannot inspect the
other arm's work.

## Evaluation rubric

| Dimension | Points | Evaluation |
|---|---:|---|
| Correctness and safety | 25 | Original unchanged; RTL semantics and claims are correct; rewrites are not called safe without proof. |
| Evidence quality | 25 | Compile/lint, formal status, synthesis inputs, metrics, and limitations are explicit. |
| Recommendation quality | 20 | Final action follows measured evidence and rejects neutral or harmful changes. |
| Opportunity coverage | 15 | Relevant structures are considered, including an explicit account of unsupported or untested structures. |
| Reproducibility | 10 | Commands, versions, constraints, hashes, and artifacts are sufficient to rerun the result. |
| Engineer usability | 5 | The result is source-linked, concise, and directly actionable. |

A materially unsafe recommendation, modified original input, fabricated metric,
or unreported proof failure disqualifies an arm. A measured improvement is not
required to score well.

## Interpretation boundary

This is one generated-block developer-preview experiment. It can expose workflow
strengths and limitations, but it cannot establish production usefulness. Later
rounds must use frozen open RTL across standalone modules, complete IP blocks,
subsystems, and SoCs.
