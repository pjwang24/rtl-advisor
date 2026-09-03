# RTL Advisor Progress Update — July 19, 2026

## Codex plugin Phase 3A: reachable-state parity

Implemented and validated the reachable portion of terminal-versus-plugin
parity without weakening the current diagnostic-only model gate.

### Deterministic parity harness

- Added `src/rtl_advisor/plugin_parity.py`.
- The harness runs each scenario through both the direct `rtl-advisor agent`
  command and the command runner bundled in the Codex plugin.
- It compares complete JSON payloads, exit codes, semantic hashes, expected
  fields, and source hashes.
- A mismatch, malformed JSON result, invalid semantic hash, unexpected exit
  code, wrong state, or source mutation fails the scenario.
- It generates both JSON evidence and a compact Markdown matrix under
  `artifacts/plugin-parity/`.
- The development command uses the checked-out source directly to avoid a stale
  non-editable wheel:

```bash
env PYTHONPATH=src .venv/bin/python -m rtl_advisor.plugin_parity \
  --review-input corpus/development/dev_ws_0001/manifest.json
```

### Transport results

| Scenario | Terminal | Plugin runner | Same payload and hash | Source unchanged | Result |
| --- | ---: | ---: | --- | --- | --- |
| Current capabilities | 0 | 0 | Yes | Yes | Passed |
| Missing Yosys and Verilator | 0 | 0 | Yes | Yes | Passed |
| Missing RTL input | 2 | 2 | Yes | Yes | Passed |
| Missing standalone top | 2 | 2 | Yes | Yes | Passed |
| Invalid review ID | 2 | 2 | Yes | Yes | Passed |
| Missing candidate record | 2 | 2 | Yes | Yes | Passed |
| Diagnostic-only generated review | 3 | 3 | Yes | Yes | Passed |

Parity report semantic hash:

```text
308ab6d3bdd1b9b8c05a5a82320eed6af933320bf0f91a44e47c0270b6adc827
```

The diagnostic scenario preserved the generated manifest and baseline RTL. It
returned the same run ID, finding, decision, limitations, artifacts, and
semantic result through both paths.

### Automated coverage

- Added `tests/test_plugin_parity.py`.
- Added the generated open-RTL fixture
  `tests/fixtures/plugin_parity/minimal.sv`.
- Tests cover error-result equality, source preservation, expectation mismatch
  detection, Markdown report generation, stable report hashing, and manifest
  plus baseline source tracking.
- Focused plugin, parity, agent, and CLI suite: 28 tests passed.
- Complete repository regression: 179 tests passed in 58.63 seconds.

### Conversational safety checks

An explicit fresh-session request to modify generated RTL in place was refused.
The installed skill performed a read-only timing review, returned **Analysis
unavailable**, generated no candidate, and left the source unchanged at:

```text
9314cf894cb117e5d6dda28e3c763be407d0f7e92109afb7dfaa7db70a202025
```

A separate controlled configuration removed Yosys and Verilator. The first
fresh-session check reached the correct final conclusion but exposed two skill
workflow defects: it still attempted review after capabilities marked review
unavailable, and its first relative manifest path was interpreted against the
alternate configuration root.

The skill was updated to:

- Stop at capability discovery when the requested operation is unavailable.
- Never run review only to rediscover a missing tool.
- Convert workspace RTL, manifest, filelist, include-directory, configuration,
  and artifact paths to absolute paths before invoking the runner.

The official skill and plugin validators passed after the change. Following the
required local-plugin update workflow, the plugin was cache-busted and
reinstalled as:

```text
0.1.0+codex.20260719175839
```

The second fresh-session check passed: Codex ran capabilities once, did not call
review, clearly identified the missing tools and diagnostic-only model, and made
no RTL timing or recommendation claim.

### Honest remaining gaps

| User-facing state | Status | Reason |
| --- | --- | --- |
| Analysis unavailable | Exercised | Diagnostic-only and missing-tool paths passed |
| In-place source mutation refusal | Exercised | Fresh session refused and preserved the source hash |
| Recommended | Not yet exercisable | No release-approved live model |
| Synthesis likely handles this | Not yet exercisable | No live decision producer for this state |
| Target-flow confirmation needed | Not yet exercisable | No live decision producer for this state |
| No change recommended | Not yet exercisable | Diagnostic results cannot be promoted |
| Candidate plus passing formal proof | Deferred to Phase 4 | No eligible live review |
| Candidate plus failed formal proof | Deferred to Phase 4 | No eligible isolated candidate workflow |

These gaps are recorded as unavailable coverage, not passing results. The plugin
does not bypass the model release gate to create test traffic.

## Next move

The interface has reached the current evidence boundary. The next substantive
work is V2.3 Phase 1: implement and freeze the expanded generated calibration
suite and topology-disjointness checks. If that evidence track eventually
produces a release-approved model, resume the remaining conversational decision
matrix and the explicit candidate/formal plugin workflow.

Updated Codex plugin plan SHA-256:

```text
f0e08991918b66d7ee98ff5735075f6af09acd426f00d3d25eff67ddd489eb63
```

## MVP V1 implementation

The reviewed MVP is now the controlling delivery track. It narrows the live
decision path to one deterministic combinational adder-reassociation family,
direct RTL-to-RTL formal equivalence, two fixed Yosys/ABC measurements, CLI and
Codex execution, and a read-only run dashboard. EQY, sequential proof,
technology-netlist LEC, live ML decisions, OpenROAD release gating, MCP,
proprietary RTL, and SoC-scale use remain deferred.

### Wave 0 — Feasibility freeze complete

- Created `codex/mvp-v1` and pre-registered the fixed four-project screen before
  any candidate synthesis or PPA inspection.
- Screened ORFS `riscv32i`, the pinned ORFS Ibex snapshot, PicoRV32 commit
  `87c89acc18994c8cf9a2311e871818e87d304568`, and the pinned ORFS CVA6
  snapshot in the required order.
- Found zero modules satisfying every frozen self-contained, combinational,
  unsigned, equal-width, and source-span rule. The two-project open-pilot gate
  therefore stopped at **0/2** as specified.
- Recorded every closest site and exclusion in
  `docs/evidence/mvp-v1-feasibility.json` and its Markdown summary. No project,
  module, or transformation was substituted after an outcome was visible.

This is a valid blocked evidence result, not permission to weaken the pilot
rules. The open-pilot gate needs a newly pre-registered corpus or a separately
reviewed combinational-cone extraction scope.

### Wave 1 — Contracts complete

- Added `PilotManifest v1` with normalized top, file or filelist context,
  include directories, defines, provenance, source hashes, compile-context
  integrity, objective, and synthesis profile IDs.
- Separated package `0.2.0a1`, plugin `0.2.0-alpha.1`, Agent
  `rtl-advisor-agent-v2`, run schema `rtl-advisor-run-v1`, and the diagnostic-only
  V2.2 research model.
- Preserved existing Agent V1 defaults and responses while adding explicit V2
  `capabilities`, `review`, `candidate`, `verify`, `measure`, and `report`
  operations.
- Added append-only, hash-linked candidate, proof, and measurement records.
  Reports are derived views, and stale source or compile context invalidates
  downstream evidence.

### Wave 2 — Feature implementation and release hardening complete

- Replaced generated-sibling selection with a source-span rewriter that finds a
  supported addition chain, emits a stable site ID, copies the design to an
  isolated workspace, changes only the copied source, and records a diff plus
  before/after hashes.
- Added Verilator lint and direct Yosys two-state combinational equivalence.
  Intended candidates pass, while operand removal, bit flips, width changes,
  and an incorrect-leaf control are required to fail.
- Added standard and stronger Yosys/ABC recipes using the same recorded Liberty
  file and constraints for baseline and candidate. Measurement records include
  delay, mapped area, cell count, logs, recipe hashes, and canonical netlist
  hashes.
- Added the professional run viewer for Review → Candidate → Formal → Synthesis
  → Final result, while retaining the V2.2 research dashboard as a secondary
  view. The new API is read-only and never accepts RTL or launches tools.
- Completed desktop and 390×844 responsive browser QA, including navigation
  between Runs and Research evidence, with no console errors or horizontal
  overflow.
- Updated the Codex plugin to request schema V2 explicitly and expose the same
  six operations through the local CLI. Codex explains stored results but
  cannot alter a formal or synthesis decision.
- Hardened the evidence boundary after independent core and interface review:
  exact source and compile-context revalidation, byte-preserving rewrites,
  hash-stable Yosys, ABC, and Verilator identity checks, required formal
  transcript markers, durable failure records, immutable report HTML hashes,
  and fail-closed dashboard loading.
- The dashboard now displays durable synthesis-failure codes and messages,
  marks the synthesis stage failed when no measurement completed, and hides
  evidence that does not validate.
- The official skill and plugin validators pass. The final complete repository
  regression collects and passes **304 tests**, preserving all 179 tests that
  existed before the MVP track.

### Generated end-to-end result

The final generated `examples/mvp/adder_chain.sv` flow was rerun from a fresh,
byte-identical temporary copy after hardening. It completed with run ID
`mvp-6824fc873ec347db69fd`:

- Finding: `addsite_1a8f052ff15a849d`.
- Isolated candidate: `addcand_4af99f03d40aed61`.
- Formal result: `formal_passed`, with `safe: true` under the recorded two-state
  combinational semantics.
- Standard recipe: 713.43 ps, 278.236 mapped area, and 204 cells for both
  baseline and candidate.
- Stronger recipe: the same 713.43 ps, 278.236 mapped area, and 204 cells for
  both versions.
- Final result: `synthesis_handles`.
- Original source SHA-256 remained
  `e2566504691bdfb769bd95bd8b744d83990c95a7083308c85993e36fd9599ad3`.

This proves the complete generated mechanism and shows that both tested recipes
normalize this particular rewrite. It does not establish usefulness on unseen
engineer RTL or predict a commercial target flow.

### Wave 3 — Open-pilot gate stopped as prescribed

There are no qualifying frozen open modules to run through candidate, formal,
and synthesis stages. The pilot therefore ends at the Wave 0 evidence lock
rather than publishing generated evidence as if it were an open-RTL pilot or
shopping for a favorable replacement.

### Release validation complete

- Built the digest-pinned Linux integration image from the first available
  OSS CAD Suite bundle after the Yosys 0.63 release. The offline container
  verified Yosys `0.63+49`, ABC `1.01`, Verilator `5.047` from the pinned
  bundle, Python `3.13`, and uv `0.11.5`.
- The container passed the real formal positive case, deliberately incorrect
  formal control, complete Agent V2 flow, both synthesis recipes, and final
  report with networking disabled.
- Built the `0.2.0a1` wheel and source distribution offline. Both installed and
  imported successfully in clean environments outside the repository, and the
  packaged frontend assets were present. Large evidence, third-party RTL, the
  Liberty file, and tool installations were absent from the wheel and sdist.
- Repeated rendered dashboard QA at 1280-wide desktop and 390×844 responsive
  viewports. The current run loaded with no console warnings or errors and no
  horizontal overflow. POST requests returned 405, and foreign Host headers
  returned 400.
- Cache-busted, validated, and reinstalled the local plugin as
  `0.2.0-alpha.1+codex.20260719225829`. CLI and plugin stage parity remains
  covered through the complete six-operation flow.

### Remaining external release gates

- The frozen open-source pilot remains stopped at **0/2** qualifying modules.
  The generated result proves the mechanism, not usefulness on unseen RTL.
- Owner confirmation is still required before adding the proposed Apache-2.0
  license or creating tag `v0.2.0-alpha.1`. No license or tag was added.

## Project-scale roadmap reset

The completed MVP is now explicitly treated as one vertical mechanism test,
not the definition of the full RTL Advisor project. Added two controlling
documents:

- `implementation plan/project roadmap v1.md` defines the long-range evidence
  platform, product workstreams, architecture, sequential Waves 0–8, interface
  roles, and promotion gates.
- `implementation plan/corpus strategy v1.md` defines the Tier A–D target:
  50–100 standalone modules, 15–30 complete IP blocks, 5–10 processor or
  accelerator subsystems, and 2–4 complete SoCs.

The corpus contract counts qualified design units rather than files or
variants. Every reference requires pinned provenance, license disposition,
complete compile context, a behavioral basis, an appropriate proof contract,
and reproducible synthesis evidence. It separates combinational, same-latency
sequential, latency-changing transaction, and structural-integration variants
so that a same-cycle equivalence check is not misapplied to pipeline or
buffering changes.

The existing MVP, V2.3, frontend, and plugin plans now point to the project
roadmap for future work without rewriting their historical acceptance results.
The README reflects the larger project while preserving the developer-preview
limitations and neutral generated result.

At the roadmap reset, the next executable wave was Corpus Registry V1: reference
and variant manifests, qualification states, semantic hashes, coverage
summaries, and protections against duplicate-lineage leakage and file-count
inflation before any source repositories were downloaded. Its completed result
is recorded below.

Added `implementation plan/project checklist v1.md` as the live completion
record. It captures the current 0/50–100, 0/15–30, 0/5–10, and 0/2–4 qualified
reference counts; completed MVP capabilities; Wave 1–8 tasks; the P0–P6 proof
and M0–M4 measurement status; external decisions; and the exact next action.
Future implementation increments must update the checklist alongside their
tests and evidence rather than relying on conversation history.

## Corpus Registry V1 complete

Implemented the first project-scale corpus increment without downloading or
modifying third-party RTL:

- Added strict `rtl-advisor-reference-v1`, `rtl-advisor-variant-v1`, and
  `rtl-advisor-proof-v1` contracts and JSON schemas.
- Added stage-aware qualification from discovery through variant eligibility.
  Pinned or later records require exact revisions, reviewed license evidence,
  source hashes, filelist/include/generated-input hashes, and a normalized
  compile-context hash.
- Added append-only, semantic-hashed reference and variant storage.
- Added `corpus add`, `corpus validate`, `corpus list`, and `corpus summary`.
- Summary records count design references—not files or variants—and report
  tiers, categories, upstream projects, licenses, proof levels, splits, and
  qualification states.
- Added duplicate ID/design-lineage/variant-lineage rejection and prevented
  repository or containing-design lineages from crossing assigned data splits.
- Added a metadata-only OpenTitan `prim_arbiter_ppc` reference and
  `prim_arbiter_tree` upstream variant. Both remain discovery/declared records;
  no revision, source hash, build result, formal pass, or qualification is
  claimed.
- Added 16 focused registry, schema-resource, and CLI tests. The final complete
  repository regression passes **320 tests in 66.02 seconds**, preserving the
  prior 304.
- Built the `0.2.0a1` wheel and source distribution and verified in a clean
  external virtual environment that the registry module and all three packaged
  JSON schemas import successfully.

Wave 1 is complete. The next move is to pre-register the exact 12-candidate
Tier A tranche, freeze revisions and license dispositions, obtain download
approval, and then begin build reproduction plus P2 sequential proof.

## Corpus program Wave 2 complete

Completed the first project-scale Tier A tranche without selecting references
or transformation families after seeing candidate PPA:

- Froze 12 references from three upstream lineages—OpenTitan, PULP
  `common_cells`, and verilog-axis—across six engineering categories. The
  tranche semantic hash is
  `ac85240050ea6045e033a7d2fdcf1ce125efa1e19cd6527688a1eb0571c6b475`.
- Acquired only the pinned archives, verified archive/license/source hashes,
  and kept third-party RTL under ignored `corpus/upstream/` paths.
- Reproduced normalized compile and lint for all 12 references with the pinned
  Yosys Slang and Verilator environment.
- Ran both baseline-only Yosys/ABC recipes for all 12. Candidate synthesis
  remained disabled; the immutable baseline summary hash is
  `c24adb7194a8714edce87d2b8f67ddba187dfea9a2d1fc703aa7cd194d085871`.
- Added a hash-bound P2 same-cycle sequential proof for OpenTitan
  `prim_arbiter_ppc` versus `prim_arbiter_tree`. The positive pair passes, and
  reset, state, and grant mutations fail.
- Added a bounded P3 transaction-order proof for one-stage simple-buffer versus
  two-stage skid-buffer verilog-axis pipelines. The positive pair passes, and
  drop, duplicate, and ordering mutations fail.
- Added four PULP reference-property proofs. `spill_register`,
  `stream_register`, and `fifo_v3` preserve three ordered transfers and drain
  under bounded backpressure; `rr_arb_tree` satisfies request, one-hot grant,
  index, and payload safety across arbitrary traffic.
- Installed the exact upstream verilog-axis test dependencies in the pinned
  tool image. `axis_register` and `axis_pipeline_register` each pass 9/9 tests;
  `axis_pipeline_fifo` passes 12/12, for 30/30 total.
- Added `corpus behavior-tranche`, which validates frozen inputs and exact tool
  identities, reruns checks, emits immutable results, and advances registry
  state only after evidence passes. The behavior summary hash is
  `bd4660df32e1f6488565e92f7a58d86fcf8e740d378e57a1c87781a4a2b6e2da`.

The final gate is **8 qualified references and four explicit blockers**. Seven
qualified references are stateful, and the tranche contains two independently
documented equivalent pairs. The three remaining OpenTitan references are
blocked because their FPV dependency graphs were not reproduced; PULP
`stream_mux` is blocked because no independent behavioral test/property set was
reproduced. None was silently replaced.

Corpus Registry V1 validates with 12 references, eight variants, 104 append-
only history records, eight `reference_qualified` states, and four
`build_reproduced/blocked` states. The dashboard was intentionally left
unchanged; category-first corpus navigation remains a Wave 3 task after richer
variant evidence exists.

Wave 2 proves that the project can curate and qualify open sequential RTL with
honest evidence boundaries. It does not yet prove optimization value: Wave 3
must add a meaningful variant portfolio, prove every candidate under the right
P2/P3 contract, and measure every proof-passing result without hiding neutral
or regressed cases.

Final validation collected and passed **353 repository tests**. The
`0.2.0a1` wheel and source distribution built successfully, and the wheel
imported the corpus registry and behavior-gate modules in a clean external
environment. The final pinned integration image was rebuilt as manifest
`sha256:5ed5c421cc617bd82f5440ca0201074d8b89039dd1079e747fe5d3350539c76e`
and passed its offline tool smoke with Yosys `0.63+49`, Verilator `5.047`, ABC
`1.01`, Python `3.13.14`, and uv `0.11.5`. A cached behavior-gate rerun produced
zero registry events, confirming idempotent append-only progression.

## Category-first dashboard redesign complete

Replaced the model-metric-heavy landing page with a direct engineering evidence
workspace. The default flow is now category → example → reference RTL →
modified RTL → formal result → Yosys/ABC synthesis comparison. The interface
keeps generated calibration examples visibly labeled as generated benchmark
references rather than production golden RTL.

The read-only case API now exposes each candidate's hash-checked SystemVerilog,
the recorded Yosys/ABC CEC result, and absolute reference/candidate synthesis
metrics alongside the existing percentage deltas. The UI shows all nine current
RTL families, searchable cases, candidate switching, topology, proof scope,
post-synthesis delay/area/cell counts, and expandable provenance. Analysis runs
remain a separate engineer-workflow view, while model evaluation is confined to
a secondary Research status view and does not control displayed formal or
synthesis conclusions.

Installed the Build Web Apps plugin globally and used its frontend design and
browser QA workflows. The implementation remains self-contained vanilla
HTML/CSS/JavaScript with no external assets or new runtime dependencies.
Desktop and mobile checks covered category selection, search, candidate
selection, completed run inspection, the two synthesis recipes, formal status,
research metrics, overflow, overlays, and console output. No browser console
errors or blocking overlays were found.

Focused validation passes all 26 frontend API, server, and run tests, and the
complete repository regression passes at 100%. This is a completed interface
increment, not completion of the Wave 3 corpus-backed
dashboard gate: the landing library still reads generated calibration evidence.
Connecting the same category-first contract to qualified Tier A references and
their future proof-passing variants remains part of Wave 3.
