# RTL Advisor Progress Update — July 12, 2026

## Current roadmap position

- Steps 1 through 4B are complete.
- Step 5A is complete: family registry, suite isolation, and adder association.
- Step 5B1 is complete: priority selection and deep-mux analysis.
- Step 5B2a is complete: common-operand mux placement and bidirectional rules.
- Step 5B2b-1 is complete: decode factoring, repeated-comparison analysis, and neutral-result calibration.
- Step 5B2b-2 remains: four transformation families and safe patch validation.
- Step 6 remains: the five-arm blinded benchmark and final report.

## What was implemented today

- Added `priority_selection` as the third registered RTL family.
- Added one low-index-priority `if/else` baseline.
- Added three equivalent implementations using `casez`, nested ternaries, and explicit grants.
- Added a separate `n0` control with intentionally incorrect priority.
- Added `priority_selection.mux_depth.v1`, which detects a deep, wide mux dependency chain.
- Preserved deterministic generation and opaque/disjoint held-out defaults.
- Added corpus, lint, formal-equivalence, graph, rule, and synthesis integration coverage.
- Increased the automated test suite from 26 to 30 passing tests.

## Formal and synthesis evidence

- All five priority-selection variants pass Verilator lint.
- v1, v2, and v3 are formally equivalent to v0.
- `n0` is formally inequivalent and produces a counterexample.
- v1 versus v0: delay worsens 23.60%, area improves 27.66%, cells improve 38.93%.
- v2 versus v0: delay worsens 21.51%, area improves 22.34%, cells improve 12.21%.
- v3 versus v0: delay worsens 28.34%, area improves 28.74%, cells improve 38.93%.

The result is deliberately non-obvious: the source-level priority chain looks
deep, but the mapped baseline is faster. Alternative encodings are much
smaller but slower. The structural rule therefore predicts area and cell-count
improvement while marking delay as uncertain.

## Blinded model result

- Codex-only found the priority-selection opportunity with 0.90 confidence but incorrectly predicted delay improvement.
- Hybrid found the opportunity with 0.85 confidence and predicted area/cell improvement with uncertain delay.
- Hybrid matched every observed direction without seeing synthesis labels.
- Both runs passed the no-tool audit and schema validation.

## Next move

Continue Step 5B2 with the remaining four families: comparator selection,
variable shifting, width/signedness, and popcount/saturation. After those
families stabilize, implement safe temporary patch generation and
lint/formal/synthesis acceptance before beginning Step 6.

## Additional progress: mux placement

- Added `mux_placement` as the fourth registered RTL family.
- Added an unfactored baseline with two additions sharing a common operand.
- Added three equivalent candidates that move selection before one shared adder.
- Added an intentionally incorrect `n0` selector control.
- Added common-operand detection to distinguish mux placement from generic resource sharing.
- Added the reverse pre-operation mux rule for timing/area tradeoff analysis.
- Increased the test suite from 30 to 34 passing tests.

All five variants lint successfully, v1-v3 are formally equivalent, and `n0`
is formally inequivalent. Each WIDTH=16 candidate improves delay by 13.41% and
cell count by 0.73% with 0.93% area regression, satisfying the benchmark's
timing-benefit guardrail.

Codex-only found the correct rewrite with 0.98 confidence but categorized it
as generic resource sharing. Hybrid found the same rewrite with 0.96
confidence and retained the more precise mux-placement transformation. Both
runs remained blinded, schema-valid, and tool-free.

## Additional progress: decode factoring

- Added `decode_factoring` as the fifth registered RTL family.
- Added a baseline that repeats two opcode comparisons in both data and hit logic.
- Added shared-decode, `case`, and masked-decode equivalent candidates.
- Added an intentionally incorrect `n0` opcode control.
- Added `decode.repeated_compare.v1`, which groups exact duplicate comparisons and recommends shared decode signals without promising a PPA gain.
- Increased the test suite from 34 to 38 passing tests.

All variants lint successfully, v1-v3 are formally equivalent, and `n0` is
formally inequivalent. Direct factoring in v1 maps identically to v0: delay,
area, and cell-count changes are all exactly 0.00%. The v2 `case` candidate
improves area by 8.90% and cells by 26.36% but worsens delay by 19.35%. The v3
masked-decode candidate improves delay by 7.84% and cells by 29.09% with 5.74%
area regression, satisfying the timing-benefit guardrail.

Codex-only found the repeated-decode opportunity with 0.98 confidence but
predicted area and cell-count improvement for direct factoring. Hybrid used the
calibrated structural result at 0.68 confidence and correctly predicted neutral
delay, area, and cell count. Both runs were blinded, schema-valid, and
tool-free. Neither selected the beneficial v3 candidate, leaving a concrete
ranking-regret case for Step 6.
