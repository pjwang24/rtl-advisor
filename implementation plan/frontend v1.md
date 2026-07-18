# RTL Advisor Frontend V1 Contract

## Objective

Frontend V1 provides a local interface for inspecting trusted RTL Advisor
evidence before the V2.3 model is allowed to analyze live designs. It establishes
a versioned API and interaction model without weakening the calibration, formal,
physical, or blind-data boundaries.

The first release is deliberately read-only. It visualizes frozen V2.2 model
evaluation results and previews the live workflow. Live RTL upload, filelist
execution, candidate emission, and Codex explanation stay disabled until a
deployable model passes the V2.3 quality targets and OpenROAD validation.

## Product surfaces

### System overview

- Overall decision score and distance to the 70% release target.
- Useful changes found: how many measured improvement cases the model identified.
- Correct no-change decisions: how often the model avoided unnecessary changes.
- Incorrect recommendations: how many recommendations failed the measured
  improvement targets.
- Per-pattern improvement-find rate, training status, and release checks.
- Explicit calibration provenance and confirmation that blind labels are sealed.

### Case explorer

- Search and filtering by case, RTL pattern, and result.
- Paginated access to all 936 grouped-OOF calibration cases.
- Case decision, whether synthesis found a useful improvement, training status,
  highest model confidence, and whether the input resembles the training set.
- Generated baseline SystemVerilog and topology metadata.
- Predicted-versus-measured delay, area, and cell-count evidence for `v1`-`v3`.
- Selected-candidate, measured-best, lint, and formal status.

### Live workspace preview

- Shows the intended RTL review and source-linked finding experience.
- States the exact V2.3 model-quality and OpenROAD checks blocking live execution.
- Does not accept files, source text, filelists, or analysis requests.

## API V1

The local server exposes:

```text
GET /api/v1/health
GET /api/v1/contract
GET /api/v1/overview
GET /api/v1/cases?family=&category=&q=&limit=&offset=
GET /api/v1/cases/{case_id}
```

Every payload includes `api_version: "v1"` and a numeric schema version. API V1
is an adapter over versioned advisor evidence; frontend code must not import or
interpret model bundle internals directly.

All mutation methods return HTTP 405 while the application is read-only. Live
analysis will be added as a separately specified endpoint after V2.3 approval,
without changing the stored-evidence response fields already consumed by the UI.

## Security and data boundary

- Bind to `127.0.0.1` by default.
- Use no external JavaScript, CSS, font, analytics, or image dependency.
- Apply restrictive content-security, framing, referrer, and MIME headers.
- Serve only package-local frontend assets.
- Resolve case source only from a diagnostic case identifier and generated
  calibration manifest; reject traversal and arbitrary filesystem paths.
- Reject evidence whose `blind_labels_used` field is not exactly false.
- Never expose company RTL, held-out RTL labels, credentials, or model binaries.

## Implementation

The server uses the Python standard library so the existing dependency-free core
remains intact. Static HTML, CSS, and JavaScript live inside the Python package.

Launch from the project root:

```bash
PYTHONPATH=src .venv/bin/python -m rtl_advisor frontend
```

The default address is:

```text
http://127.0.0.1:8765
```

Optional `--host` and `--port` flags change the bind address. Binding beyond
localhost is an explicit operator choice and is not required for normal use.

## V2.3 integration boundary

After V2.3 passes model evaluation and the OpenROAD delta audit:

1. Add an asynchronous local-analysis submission endpoint with immutable run IDs.
2. Accept authorized files or filelists without copying data outside the project.
3. Stream lint, parse, feature, model, candidate, and formal stage status.
4. Return the existing decision/candidate view model with V2.3 provenance.
5. Keep candidate emission opt-in and isolated from source RTL.
6. Preserve Codex as explanation-only and unable to change deterministic fields.

No frontend feature can unlock live analysis by bypassing the advisor's own
deployability check.
