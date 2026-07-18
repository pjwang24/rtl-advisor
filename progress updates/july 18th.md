# RTL Advisor Progress Update — July 18, 2026

## Frontend V1

The first local RTL Advisor frontend is implemented and running. It establishes
the stable UI/API boundary now, while correctly keeping live RTL analysis locked
behind the failed V2.2 calibration gate.

### Product surfaces

- Added a calibration command center with the frozen balanced actionable
  accuracy, opportunity coverage, abstention specificity, harmful recommendation
  rate, distance to the 70% gate, and physical-evidence status.
- Added family-readiness rows for all nine optimization families, including
  opportunity counts, coverage, support status, and direct navigation into the
  corresponding evidence.
- Added the complete V2.2 failure decomposition: 131 below-threshold misses, 13
  unsupported opportunities, four harmful recommendations, 86 measured-best
  covered cases, and zero ranking misses.
- Added a filterable, searchable, paginated explorer over all 936 frozen
  calibration cases.
- Added case drill-down with the generated baseline SystemVerilog, topology,
  decision, OOD result, and all three candidate predictions.
- Candidate cards compare predicted and measured calibration delay, area, and
  cell-count improvement and display selection, measured-best, eligibility,
  generation, lint, and formal status.
- Added a live-workspace preview showing the intended source-linked analysis
  experience and the exact V2.3 calibration and OpenROAD gates that must pass
  before file upload or filelist execution is enabled.
- Added responsive desktop, tablet, and mobile layouts without external assets,
  fonts, frameworks, analytics, or network dependencies.

### Versioned read-only API

Added API V1:

```text
GET /api/v1/health
GET /api/v1/contract
GET /api/v1/overview
GET /api/v1/cases?family=&category=&q=&limit=&offset=
GET /api/v1/cases/{case_id}
```

- The adapter reads only the hash-frozen V2.2 summary, calibration report, and
  failure diagnostic.
- Evidence is rejected unless `blind_labels_used` is exactly false.
- Case source resolution accepts only a known diagnostic case ID and a generated
  calibration manifest. Absolute paths and traversal components are rejected.
- Every mutation request returns HTTP 405. V2.2 cannot be used to analyze live
  RTL through the frontend because its model is diagnostic-only.
- Static responses apply restrictive content-security, MIME, framing, and
  referrer headers.
- The server binds to `127.0.0.1` by default and uses only the Python standard
  library.

### Commands and contract

Launch command:

```bash
PYTHONPATH=src .venv/bin/python -m rtl_advisor frontend
```

Default address:

```text
http://127.0.0.1:8765
```

The frozen frontend contract is stored in `implementation plan/frontend v1.md`
with SHA-256:

```text
a0f35cc2964fe6b7778ca21beb1a11648abcccb14aa072807f29da8e9a885641
```

### Verification

- API health returned `ready`, `read_only: true`, source version `v22`, and
  source status `calibration_gate_failed`.
- The overview endpoint reproduced 936 cases, 2,808 candidate rows, 68.412%
  balanced actionable accuracy, 37.391% opportunity recall, 99.433% specificity,
  and 4.444% harmful recommendations from the frozen evidence.
- Filtering `unsupported_family` returned exactly 13 cases.
- Case drill-down returned the generated 90-line resource-sharing baseline and
  all three candidate records with sealed-blind provenance.
- A real POST request returned HTTP 405 and the explicit read-only/V2.3 message.
- HTML, CSS, and JavaScript returned HTTP 200 with the expected restrictive
  headers; JavaScript syntax validation passed.
- The complete repository regression contains **148 tests and passes in full**.

The in-app browser surface was unavailable in this session, so verification used
the running localhost server, real HTTP requests, DOM/static-source inspection,
JavaScript syntax checks, API contract tests, and the full Python regression.

## Next frontend integration

Frontend V1 remains read-only while V2.3 is developed. Once V2.3 passes
calibration and its physical delta audit, the next frontend increment will add:

1. Local authorized RTL/filelist submission with immutable run identifiers.
2. Progress events for parse, lint, feature extraction, deterministic analysis,
   candidate generation, and formal verification.
3. Source-linked deterministic recommendations and abstention evidence using the
   existing API V1 decision/candidate view model.
4. Opt-in isolated candidate emission and Codex explanation, with neither able to
   modify the original RTL or deterministic decision.

## Internal-dashboard terminology revision

The user-facing dashboard copy was revised to remove model-development jargon
and present each number as an engineering decision:

- `Balanced actionable accuracy` is shown as **Overall decision score**, with an
  explanation that it gives equal weight to finding useful changes and correctly
  recommending no change.
- `Opportunity coverage` is shown as **Useful changes found**, including the
  concrete count: 86 of 230 cases where synthesis found a useful improvement.
- `Abstention specificity` is shown as **Correct no-change decisions**, including
  the concrete count: 702 of 706 cases where no candidate met the targets.
- `Harmful recommendation rate` is shown as **Incorrect recommendations**,
  including the concrete count: four of 90 recommendations; lower is better.
- `Unsupported family`, `below threshold`, `correct abstention`, `OOD`, and
  `measured best` are displayed as **more training data needed**, **confidence too
  low**, **correct no-change decision**, **input range**, and **best synthesis
  result**.

The overview was also toned down from a marketing-style command center to an
internal model-readiness page. It now leads with the release status, explains why
live use is disabled, and uses “RTL pattern,” “evaluation data,” and “release
checks” consistently.

## Synthesis redundancy pilot V1

The stronger-synthesis calibration pilot is implemented and complete. Its
purpose is to answer the core product question: does an RTL rewrite retain
implementation value after a synthesis tool is given a stronger opportunity to
perform the same optimization automatically?

### Frozen experiment

- Added `implementation plan/synthesis redundancy v1.md` and froze the
  experiment before running the new synthesis recipe.
- Deterministically selected 27 generated calibration cases: three cases from
  each of the nine RTL families.
- Selected covered improvements, missed improvements, and correct no-change
  decisions where those categories existed; missing categories use a seeded,
  hash-stable fill.
- Frozen every selected manifest, RTL source hash, formal-equivalence result,
  standard-synthesis result, and standard mapped-netlist hash.
- Plan hash:
  `8d09a997c8d8af4e590822322f0fbb556b67d4e667efba9d6f128bdb3f4091a8`.
- Used generated calibration RTL only. No company RTL, held-out labels, or blind
  benchmark data were used.

### Implementation

- Added `rtl-advisor benchmark synthesis-redundancy-v1` with one-to-eight
  synthesis workers and JSON output support.
- Added a stronger Yosys recipe that runs the normal coarse optimization,
  applies aggressive resource sharing and full optimization, and then uses the
  same Nangate45 library, input driver, output load, flip-flop mapping, and
  constrained ABC mapping as the standard flow.
- Added immutable per-variant results, caching, provenance, run summaries,
  mapped cell signatures, case/family summaries, and JSON/Markdown reports under
  `artifacts/synthesis-redundancy/v1`.
- Added explicit outcomes for benefits that survive, benefits removed by the
  stronger recipe, effectively identical synthesized results, neutral PPA
  results, and recipe-dependent tradeoffs.
- Corrected the report audit so candidates that become useful only under the
  stronger recipe are not incorrectly counted as survivors of the standard
  result.

### Formal and synthesis result

- All 81 candidate comparisons have current successful RTL-to-RTL formal
  equivalence proofs.
- All 108 stronger-synthesis runs passed: 27 baselines plus 81 equivalent RTL
  candidates.
- The standard recipe found at least one useful candidate in 13 of 27 cases.
- The stronger recipe found at least one useful candidate in 12 of 27 cases.
- The standard recipe classified 21 of 81 candidates as useful.
- Fifteen of those 21 remained useful under stronger synthesis, for a 71.4%
  candidate survival rate.
- Six standard-flow benefits were removed by stronger synthesis.
- Seven candidates became useful only under the stronger recipe, demonstrating
  that marginal PPA conclusions can depend on synthesis settings.
- The stronger recipe found 22 useful candidates in total.
- Thirty-seven candidates produced effectively unchanged PPA and the same
  mapped cell mix.
- Final outcome counts: 15 retained benefits, six removed benefits, 37 absorbed
  results, and 23 recipe-dependent tradeoffs.

### Product conclusion

The data rejects both extreme assumptions. Synthesis does automatically absorb
many source rewrites, but it does not erase every useful RTL choice: 15 of 21
standard-flow benefits remained useful under the stronger recipe. The product
should therefore avoid generic style advice and focus on rewrite families with
repeatable post-synthesis value.

The strongest next step is a commercial-tool replication on generated RTL only.
Run the retained families through an approved Cadence Genus flow with matched
constraints, then keep only findings whose direction is stable across standard
Yosys, stronger Yosys, and Genus. Comparator selection and variable shifting
showed no useful candidate after stronger synthesis in this 27-case sample and
should not be presented as broad optimization promises without more evidence.

The final report is `artifacts/synthesis-redundancy/v1/report.md` with semantic
report hash:

```text
ee4339bce2f618153dbbaeb100b77d01a1e89c1418a42c4cbf9643a4553740b3
```
