---
name: analyze-rtl
description: Review generated or explicitly approved open RTL and qualified RTL Advisor corpus references through deterministic compact single- or multi-input workflows, including authorized candidate preparation, formal verification, pinned synthesis measurement, and evidence-backed recommendations. Use for SystemVerilog review, synthesis-handling questions, candidate requests, formal safety, or measured MVP reports.
---

# Analyze RTL

Use RTL Advisor as the execution and evidence authority. Codex selects bounded
inputs and authorization; the CLI computes identities, executes stages, and
returns the decision. Never reinterpret or replace its result.

## Safety boundary

- Process only generated RTL, explicitly approved open RTL, or a qualified
  corpus-reference manifest. Ask before touching source whose status is unclear.
- Keep RTL local; do not browse, upload it, or modify the input or compile context.
- Candidate generation requires an explicit request and stays in the CLI artifact
  workspace.
- Call a candidate safe only when the returned digest records
  `formal_status: formal_passed` and `safe: true`.
- Measurement requires that current formal pass. Preserve unsupported, blocked,
  failed, stale, partial, and inconclusive states.

## Normal route

Use `scripts/run_rtl_advisor.py` once. Do not run a separate `capabilities`
command: compact workflows perform and validate capability discovery internally.

For one input, run:

```bash
python3 <skill-dir>/scripts/run_rtl_advisor.py workflow prepare \
  <absolute-input> \
  --input-kind generated_rtl|explicitly_approved_open_rtl|qualified_corpus_reference \
  --objective timing|area|balanced \
  --authorized-through review|candidate|verify|measure \
  --prompt-file <exact-local-prompt-file> \
  [--top <module>] [-I <absolute-dir>] [-D <define>] \
  [--first-eligible] --start --compact
```

For multiple inputs, write an ordered V1 batch manifest containing only item IDs,
input paths and kinds, objectives, compile options, and optional expected source
hashes, then run:

```bash
python3 <skill-dir>/scripts/run_rtl_advisor.py workflow batch \
  <absolute-manifest.json> \
  --authorized-through review|candidate|verify|measure \
  --prompt-file <exact-local-prompt-file> \
  [--first-eligible] [--output-dir <absolute-dir>] [--jobs 1|2|3|4]
```

Use the default `--jobs 1`; use 2–4 only when the engineer explicitly wants
bounded parallel execution. When lower latency is an explicit objective and
the manifest items are independent workflows, use `--jobs 4`. Candidate-capable ceilings require
`--first-eligible`. Use `--proposal-file` plus `--confirmation-file` when a short
confirmation authorizes a previously shown proposal.

The runner validates schema, semantic hashes, status, exit code, and batch order.
On success, use only the returned digest or batch summary. Do not reopen stage
artifacts, reports, logs, source, or capability payloads unless the engineer asks
for exact detail.

## Response contract

Lead with `action`, then state the decision, scope, source location, deterministic
rationale, formal/measurement status, per-profile classifications, and evidence
completeness. Link the minimal returned artifacts and give the returned
reproduction command for a single workflow.

Do not infer PPA, safety, or whole-design coverage beyond those fields. A source
finding, formal equivalence, and measured synthesis outcome are separate claims.
Only `measured_improvement` maps to a change recommendation; `no_change`,
`synthesis_handles`, and `regression` map to no change; `unsupported` stays
unsupported; every incomplete, unproven, formal-nonpass, or flow-dependent state
is inconclusive.

Evidence incomplete means no positive recommendation is permitted.

## Failure and debug route

- Exit `2`: report the bounded error code and message. Treat schema, hash,
  document-type, or exit-code disagreement as untrusted.
- Exit `4`: preserve the returned blocker or partial state and make no positive
  recommendation.
- Never fall back automatically to individual `review`, `candidate`, `verify`,
  `measure`, or `report` calls. Use them only when the engineer explicitly asks
  for stage-level debugging or a structured workflow failure requires diagnosis.
- Read [references/result-interpretation.md](references/result-interpretation.md)
  only for an unfamiliar returned state. Read
  [references/cli-contract.md](references/cli-contract.md) only for exact schema,
  batch-manifest, or debug-command details.
