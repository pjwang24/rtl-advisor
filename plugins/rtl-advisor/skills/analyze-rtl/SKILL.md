---
name: analyze-rtl
description: Review generated or explicitly approved open RTL through the local RTL Advisor CLI, explain pre-synthesis timing or area findings, show whether the evidence supports a change, prepare isolated candidates on request, and run hash-matched formal equivalence. Use for SystemVerilog files, RTL Advisor manifests or run IDs, synthesis-handling questions, evidence explanations, candidate requests, and formal-safety checks.
---

# Analyze RTL

Use RTL Advisor as the execution and evidence engine. Translate the engineer's
request into the stable `rtl-advisor agent` interface, validate its JSON, and
explain the unchanged result in plain engineering language.

Do not create an independent recommendation, change a decision, or infer missing
PPA evidence from the source.

## Safety boundary

- Analyze only generated RTL or open RTL that the engineer explicitly approves.
- Ask before processing source that may be proprietary when approval is not clear.
- Keep analysis local. Do not browse, upload RTL, or contact external services.
- Treat review as read-only and never modify the input source or compile context.
- Prepare a candidate only after an explicit engineer request.
- Keep every candidate in the CLI-provided artifact workspace.
- Call a candidate safe only when `agent verify` returns `status: passed`,
  `safe: true`, and current source-integrity checks.
- Preserve blocked, unsupported, diagnostic-only, failed, and stale states.

## Workflow

1. Locate the skill directory and use `scripts/run_rtl_advisor.py` for every
   operation. Do not guess shell commands or scrape human-formatted output.
2. Run `capabilities` before selecting a workflow.
3. Check schema version, tool availability, supported input form, model release
   state, and operation availability.
4. Confirm that the input is generated or explicitly approved and that its top
   module or manifest is known.
5. Use timing, area, or balanced from the engineer's request. Default to
   balanced only when the choice does not materially change the requested task.
6. Run a read-only review and retain its JSON result, run ID, semantic hash,
   normalized command, and artifact paths.
7. Explain the exact decision, source location, reason, likely tradeoff,
   evidence, and limitation. Read `references/result-interpretation.md` before
   presenting a result whose state is unfamiliar.
8. Stop after the explanation unless the engineer explicitly requests a
   candidate.
9. Before candidate preparation, confirm that the review reports
   `candidate_generation_allowed: true`. Use its selected finding ID.
10. After preparation, report that the candidate is isolated and unproven.
    Show or link the CLI-produced diff; do not call it behavior-preserving.
11. Run `verify` only when requested. Report safe only from the passing formal
    result and preserve the proof and source hashes.

## Commands

Run from any directory inside the RTL Advisor checkout. The runner resolves the
approved local executable and always requests versioned JSON.

```bash
python3 <skill-dir>/scripts/run_rtl_advisor.py capabilities
```

Review a generated case or manifest:

```bash
python3 <skill-dir>/scripts/run_rtl_advisor.py review path/to/manifest.json \
  --objective timing
```

Review an approved source file:

```bash
python3 <skill-dir>/scripts/run_rtl_advisor.py review path/to/top.sv \
  --top top_module --objective balanced
```

Prepare the one candidate selected by an eligible review:

```bash
python3 <skill-dir>/scripts/run_rtl_advisor.py candidate <run-id> \
  --finding <finding-id>
```

Verify the prepared candidate:

```bash
python3 <skill-dir>/scripts/run_rtl_advisor.py verify <run-id> \
  --candidate <candidate-id>
```

Read `references/cli-contract.md` when exact arguments, JSON fields, or exit
codes are needed.

## Explanation contract

For a normal review, answer these questions:

1. Where is the finding?
2. What structure did RTL Advisor observe?
3. Why might it affect timing or area?
4. Does the evidence say synthesis likely handles it?
5. What action, if any, is supported?
6. What evidence and limitations govern the conclusion?

Lead with the action state. Use **Recommended**, **Synthesis likely handles
this**, **Target-flow confirmation needed**, **No change recommended**, or
**Analysis unavailable**. Do not expose internal policy vocabulary when a plain
description is available.

Keep predicted improvement separate from measured synthesis. Never turn a
diagnostic model result into a recommendation. If capabilities report that no
live model is ready, say so directly and present any returned findings only as
diagnostic observations.

## Failure handling

- On exit code `2`, report the structured error code and message.
- On exit code `3`, report the review as unavailable or blocked and preserve its
  reason and limitations.
- On exit code `4`, report that candidate preparation or verification failed;
  never claim safety.
- If JSON is malformed, has an unsupported schema, has the wrong document type,
  or fails its semantic hash, stop and report that the automation result cannot
  be trusted.
- If source hashes changed after review, require a new review rather than
  reusing the run ID.
- If a tool or model is missing, do not substitute Codex analysis.

Always include the normalized CLI command and the relevant artifact paths so an
engineer can reproduce the result directly in a terminal.
