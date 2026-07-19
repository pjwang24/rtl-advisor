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
