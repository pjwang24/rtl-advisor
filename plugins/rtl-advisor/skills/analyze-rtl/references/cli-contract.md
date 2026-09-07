# RTL Advisor Agent V2 CLI contract

Use `scripts/run_rtl_advisor.py`. The runner always requests Agent schema `2`,
requires run schema `rtl-advisor-run-v1`, validates the document type and
semantic hash, and preserves the CLI exit code.

## Operations

```text
capabilities
review <input> --objective timing|area|balanced [--top <module>] [-I <dir>] [-D <define>]
candidate <run-id> --finding <finding-id>
verify <run-id> --candidate <candidate-id>
measure <run-id> --candidate <candidate-id>
report <run-id>
evidence explore [--workflow-id <workflow-id>] [--run-id <run-id>] [--profile standard|stronger] [--objective timing|area|balanced] [--classification improved|neutral|regressed] [--decision measured_improvement|synthesis_handles|flow_dependent|regression] [--transformation <id>] [--source-kind agent_v2_run|family_study] [--output-dir <dir>]
corpus coverage [--tier A|B|C|D] [--category <category>] [--split <split>] [--qualification-state <state>] [--qualification-status active|blocked|rejected] [--registry-dir <dir>]
corpus validate <tranche-lock.json>
corpus register <tranche-lock.json> [--registry-dir <dir>]
corpus qualify <tranche-lock.json> <qualification-plan.json> [--registry-dir <dir>]
workflow prepare <input> --input-kind generated_rtl|explicitly_approved_open_rtl|qualified_corpus_reference --objective timing|area|balanced --authorized-through review|candidate|verify|measure (--prompt-file <file> | --proposal-file <json> --confirmation-file <file>) [--top <module>] [-I <dir>] [-D <define>] [--first-eligible | --finding-id <id>] [--output-dir <dir>] [--start] [--compact]
workflow start <request.json> --authorization <authorization.json> [--compact]
workflow status <workflow-id> [--compact]
workflow resume <workflow-id> --authorization <authorization.json> [--compact]
workflow report <workflow-id> [--compact]
workflow batch <manifest.json> --authorized-through review|candidate|verify|measure (--prompt-file <file> | --proposal-file <json> --confirmation-file <file>) [--first-eligible] [--output-dir <dir>] [--jobs 1|2|3|4]
```

Set `RTL_ADVISOR_CONFIG` or pass `--config` for a nondefault configuration.
Set `RTL_ADVISOR_BIN` only to select an approved local executable. The runner
never uses shell interpolation.

## Shared fields

Every successful document contains:

- `schema_version: 2`.
- `run_schema: rtl-advisor-run-v1`.
- `flow_version: rtl-advisor-agent-v2`.
- Operation-specific `document_type`.
- `status`, `semantic_hash`, normalized `command`, and artifact paths.

Records are append-only and hash-linked. A changed source or compile context
invalidates candidate, proof, and measurement evidence.

Workflow operations use workflow schema V1 and orchestrate the unchanged Agent
V2 stages. Their compact summary records the authorization ceiling, completed
stages, final decision, safety flag, next action, normalized Agent commands,
and artifact paths. Full Agent records and logs remain local and are loaded only
when requested or needed to diagnose a structured failure.

`--compact` returns `rtl-advisor-workflow-digest-v1`, a sub-4 KB deterministic
projection of the immutable full summary. It carries the action mapping, scope,
source locations, canonical rationale, finding and candidate IDs, formal and
measurement states, per-profile classifications, evidence completeness, one
reproduction command, minimal artifact links, and parent summary/state hashes.
The full summary remains the default for backward compatibility.

The batch manifest uses this shape; item order is output order and IDs must be
unique:

```json
{
  "schema_version": 1,
  "schema": "rtl-advisor-workflow-batch-manifest-v1",
  "document_type": "rtl-advisor.workflow.batch-manifest",
  "items": [
    {
      "item_id": "alu-01",
      "input": {
        "kind": "explicitly_approved_open_rtl",
        "path": "rtl/alu.sv",
        "top": "alu",
        "include_dirs": ["rtl/include"],
        "defines": ["SYNTHESIS=1"]
      },
      "objective": "balanced",
      "expected_source_sha256": "<64 lowercase hex characters>"
    }
  ]
}
```

Relative manifest paths resolve from the manifest directory. Batch execution
discovers capabilities once, prepares hash-linked child requests, deduplicates
identical workflow IDs, continues after bounded per-item failures, and emits one
`rtl-advisor-workflow-batch-summary-v1` in manifest order. A malformed manifest,
authorization basis, or capability result stops the batch. `--jobs` defaults to
1 and is bounded to 4. One authorization ceiling and selection policy apply to
the entire batch; split mixed ceilings into separate manifests.

`workflow prepare` returns `rtl-advisor.workflow.preparation` unless `--start`
is supplied, in which case it returns the normal compact workflow summary. The
preparation command performs the capabilities check before reading the input,
normalizes the compile context, hashes authorization evidence without embedding
direct prompt text, and writes request, authorization, and capability snapshots.
Candidate-capable ceilings require `--first-eligible` or `--finding-id`.

Evidence exploration uses evidence schema V1 and is read-only with respect to
the source evidence. Its compact result contains cohort counts, small bar-chart
rows, a scatter specification, and artifact links. Point-level measurements
remain in `chart-data.json`. A `partial` result exits `4` because one or more
invalid sources were excluded; `empty` is a valid exit-`0` result.

Corpus operations use corpus schema V1. `coverage` and `validate` are read-only.
`register` and `qualify` explicitly declare `registry_mutation: append_only`;
they require engineer authorization immediately before execution. Coverage
uses `independent_design_lineage` as its primary population and reports
reference, implementation-variant, and parameter-variant inventory separately.
A qualification result with `completed_with_blockers` exits `4` and remains a
trusted partial result.

## Stage contract

- Review: `candidate_available` means a registered deterministic source site or
  curated reference alternative was found. It is not a recommendation.
- Candidate: `candidate_prepared` means an isolated candidate and diff exist.
  It remains unproven.
- Verification: the backend is selected from the candidate's registered P1,
  P2, or future P3 proof contract. Only `formal_passed` with `safe: true`
  supports describing the candidate as equivalent under the recorded
  assumptions, latency relation, and comparison scope.
- Measurement: requires the current passing proof and records `standard` and
  `stronger` Yosys/ABC results. Final decisions are `measured_improvement`,
  `synthesis_handles`, `flow_dependent`, or `regression`.
- Report: derives a view of stored artifacts and never rewrites them. It also
  reports eligible, prepared, formally checked, measured, and terminal counts.
  `status: incomplete` or `decision: incomplete` means one or more eligible
  sites is missing a candidate, proof, or required measurement; it never
  supports a positive conclusion.

## Exit codes

- `0`: operation completed and, for verification, formal passed.
- `2`: invalid request, missing/malformed/stale artifact, or structured error.
- `4`: candidate preparation, formal verification, measurement, or corpus
  qualification completed with a trusted blocker state.

Treat any other exit code as an unexpected execution failure. Never reinterpret
a stored result based on Codex judgment.
