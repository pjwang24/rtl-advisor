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

## Stage contract

- Review: `candidate_available` means a deterministic supported source site was
  found. It is not a recommendation.
- Candidate: `candidate_prepared` means an isolated candidate and diff exist.
  It remains unproven.
- Verification: only `formal_passed` with `safe: true` supports describing the
  candidate as equivalent under the recorded two-state combinational semantics.
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
- `4`: candidate preparation, formal verification, or measurement did not pass.

Treat any other exit code as an unexpected execution failure. Never reinterpret
a stored result based on Codex judgment.
