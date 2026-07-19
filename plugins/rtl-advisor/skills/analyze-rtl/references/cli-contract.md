# RTL Advisor agent CLI contract

Use `scripts/run_rtl_advisor.py` instead of invoking ad hoc commands. The runner
accepts only the four operations below, forces JSON output, validates schema
version `1`, checks the document type and semantic hash, and preserves the CLI
exit code.

## Operations

```text
capabilities
review <input> --objective timing|area|balanced [--top <module>] [-I <dir>] [-D <define>]
candidate <run-id> --finding <finding-id>
verify <run-id> --candidate <candidate-id>
```

Set `RTL_ADVISOR_CONFIG` for a nondefault configuration or pass runner
`--config`. Set `RTL_ADVISOR_BIN` only when an approved executable must override
normal discovery. The runner never uses shell interpolation.

## Shared fields

Every successful agent document contains:

- `schema_version`: `1`.
- `document_type`: operation-specific identifier.
- `flow_version`: `rtl-advisor-agent-v1`.
- `status`: operation state.
- `semantic_hash`: SHA-256 of the document without this field.
- `command`: normalized reproducible CLI argument array.
- `artifacts`: stable local paths where applicable.

Every structured CLI error uses document type `rtl-advisor.agent.error` and
contains `error.code` and `error.message`.

## Capabilities

Document type: `rtl-advisor.agent.capabilities`.

Check `analysis.live_recommendation_ready`, `tools`, `models`, `input_forms`, and
`operations` before review or candidate work. `implemented: true` does not mean
`available: true`.

## Review

Document type: `rtl-advisor.agent.review`.

Important fields:

- `run_id`, `objective`, and `profile`.
- `status`, `decision`, and `status_reason`.
- `input.design_hash`, source hashes, compile context, and source integrity.
- `findings` with stable finding/candidate IDs and source locations.
- `evidence` with analysis hash, gate state, and model release status.
- `candidate_generation_allowed`.
- `limitations` and artifacts.

Only `status: completed`, `decision: recommended`, and
`candidate_generation_allowed: true` permit candidate preparation.

## Candidate

Document type: `rtl-advisor.agent.candidate`.

`status: prepared` means an isolated candidate and unified diff exist. It does
not mean the candidate is equivalent or safe. Confirm `safe: false` until
verification passes.

## Verification

Document type: `rtl-advisor.agent.verification`.

Only `status: passed` together with `safe: true`, passing source-integrity
records, and `formal.status: passed` supports describing the candidate as
behavior-preserving for the checked inputs.

## Exit codes

- `0`: capabilities succeeded, review completed, candidate prepared, or proof
  passed.
- `2`: invalid request, missing artifact, malformed result, or structured agent
  error.
- `3`: review blocked or unavailable.
- `4`: candidate preparation or verification did not pass.

Treat any other exit code as an unexpected execution failure.
