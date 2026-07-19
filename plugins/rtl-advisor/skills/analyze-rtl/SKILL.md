---
name: analyze-rtl
description: Review generated or explicitly approved open combinational RTL through the local RTL Advisor CLI, prepare an isolated supported adder-reassociation candidate on request, run hash-matched RTL equivalence, measure two pinned Yosys/ABC recipes, and explain the immutable report. Use for SystemVerilog files, RTL Advisor pilot manifests or run IDs, synthesis-handling questions, candidate requests, formal-safety checks, and measured MVP reports.
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
- Call a candidate safe only when `agent verify` returns `status: formal_passed`,
  `safe: true`, and current source-integrity checks.
- Preserve blocked, unsupported, diagnostic-only, failed, and stale states.

## Workflow

1. Locate the skill directory and use `scripts/run_rtl_advisor.py` for every
   operation. Do not guess shell commands or scrape human-formatted output.
2. Run `capabilities` before selecting a workflow.
3. Check Agent V2 and run-artifact schema versions, tool availability,
   transformation support, and operation availability. If the requested operation reports
   `available: false`, stop before calling it and explain the reported missing
   prerequisite. Do not run a review merely to rediscover a missing tool.
4. Confirm that the input is generated or explicitly approved and that its top
   module or manifest is known. Resolve every user-supplied RTL, manifest,
   filelist, include-directory, configuration, and artifact path to an absolute
   workspace path before passing it to the runner. A nondefault configuration
   can have a different root, so do not rely on its relative-path resolution.
5. Use timing, area, or balanced from the engineer's request. Default to
   balanced only when the choice does not materially change the requested task.
6. Run a rules-only, read-only review and retain its JSON result, run ID, semantic hash,
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
11. Run `verify` only when requested. Report safe only from `formal_passed`,
    `safe: true`, and current source hashes.
12. Run `measure` only after a current passing proof and only when requested.
    Preserve both recipe results. Do not describe either as target-flow PPA.
13. Use `report` to aggregate stored artifacts without changing prior records.

## Commands

Run from any directory inside the RTL Advisor checkout. From an engineer's
separate RTL workspace, use an installed `rtl-advisor` executable and pass an
absolute `--config` path or set `RTL_ADVISOR_CONFIG`. The runner resolves only
the approved local executable and always requests versioned JSON.

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

Measure the proven candidate and derive the report:

```bash
python3 <skill-dir>/scripts/run_rtl_advisor.py measure <run-id> \
  --candidate <candidate-id>
python3 <skill-dir>/scripts/run_rtl_advisor.py report <run-id>
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

Lead with the action state. Use **Candidate available**, **Formally equivalent**,
**Measured improvement in both Yosys recipes**, **Synthesis handles this in the
tested recipes**, **Results depend on the recipe**, **Regression measured**, or
**Analysis unavailable**. For an incomplete run, lead with **Evidence incomplete**,
list the missing sites or stages from the report, and make no
positive recommendation. Do not expose internal policy vocabulary when a plain
description is available.

The MVP review is deterministic and rules-only. Keep a structural finding,
formal safety, and measured synthesis as three separate claims. The V2.2 model
remains diagnostic-only and cannot unlock candidates or change the report.

## Failure handling

- On exit code `2`, report the structured error code and message.
- On exit code `4`, preserve the returned stage state. Candidate preparation,
  verification, or measurement did not pass, or a report is incomplete; never
  claim safety or a positive final result.
- If JSON is malformed, has an unsupported schema, has the wrong document type,
  or fails its semantic hash, stop and report that the automation result cannot
  be trusted.
- If source hashes changed after review, require a new review rather than
  reusing the run ID.
- If capabilities report that the requested operation is unavailable, stop at
  capability discovery. If a later command reports a missing tool or model,
  stop that workflow. Never substitute Codex analysis or change a stored decision.

Always include the normalized CLI command and the relevant artifact paths so an
engineer can reproduce the result directly in a terminal.
