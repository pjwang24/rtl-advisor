# RTL Advisor Progress Update — July 13, 2026

## Current roadmap position

- Steps 1 through 4B are complete.
- Step 5A is complete: family registry, suite isolation, and adder association.
- Step 5B1 is complete: priority selection and deep-mux analysis.
- Step 5B2a is complete: common-operand mux placement and bidirectional rules.
- Step 5B2b-1 is complete: decode factoring and neutral-result calibration.
- Step 5B2b-2 is complete: comparator selection and harmful-rewrite calibration.
- Step 5B2b-3 is complete: variable shifting and guarded shift-amount narrowing.
- Step 5B2b-4 is complete: width/signedness and redundant-extension calibration.
- Step 5B2b-5 is complete: popcount/saturation and timing/area calibration.
- Step 5C is complete: safe temporary patch validation.
- Step 5D is complete: full development and opaque held-out corpus acceptance.
- Step 6A is complete: the five-arm benchmark infrastructure and bounded smoke
  campaign.
- Step 6B is complete: all 240 model runs and the final artifact-only report.

## What was implemented today

- Added `comparator_selection` as the sixth registered RTL family.
- Added a baseline with two parallel unsigned comparisons feeding a one-bit result mux.
- Added three equivalent candidates that select operand pairs before one shared comparator.
- Added an intentionally incorrect `n0` comparison-direction control.
- Added `comparator_selection.output_mux.v1` with operand-width, signedness, comparator, and mux evidence.
- Added corpus, lint, formal-equivalence, graph/rule, and synthesis integration coverage.
- Increased the automated test suite from 38 to 42 passing tests.

## Formal and structural evidence

- All five variants pass Verilator lint.
- v1, v2, and v3 are formally equivalent to v0.
- `n0` is formally inequivalent and produces a counterexample.
- The baseline graph contains two unsigned WIDTH=16 `lt` operators feeding one result mux.
- The direct shared candidate contains one `lt` operator and does not trigger the parallel-comparator rule.

## Synthesis evidence

- v1 versus v0: delay worsens 6.33%, area worsens 18.30%, and cells worsen 0.99%.
- v2 versus v0: delay worsens 0.88%, area worsens 12.53%, and cells improve 5.94%.
- v3 versus v0: delay worsens 0.88%, area worsens 11.78%, and cells improve 5.94%.
- None of the candidates satisfies either benchmark benefit guardrail.

This result is deliberately counterintuitive. Replacing two comparators and a
one-bit result mux with one comparator requires two full-width operand mux
banks. For this WIDTH=16 case and Nangate45 flow, that added input-selection
cost outweighs source-level comparator sharing. The calibrated rule predicts
delay and area degradation, leaves cell count uncertain, and recommends
retaining the parallel comparisons unless target synthesis proves otherwise.

## Blinded model result

- Codex-only reports the comparator-sharing rewrite with 0.97 confidence.
- Codex-only incorrectly predicts area and cell-count improvement with uncertain delay.
- Hybrid returns no actionable finding because the structural evidence predicts worse delay and area.
- Hybrid therefore matches the correct keep-the-baseline decision without receiving synthesis labels.
- Both runs are schema-valid, tool-free, read-only, and audited with synthesis labels hidden.

## Next move

The v1 implementation plan is complete. Use the measured failure families and
confidence intervals to scope a separate v2 calibration plan.

## Additional progress: variable shifting

- Added `variable_shift` as the seventh registered RTL family.
- Added a baseline that shifts WIDTH-bit data by a WIDTH-bit variable amount.
- Added guarded-narrow, decoded fixed-shift, and guarded staged equivalents.
- Added an intentionally incorrect `n0` candidate that drops the out-of-range guard.
- Added `variable_shift.wide_amount.v1`, including data width, amount width, minimum index width, and excess-bit evidence.
- Increased the test suite from 42 to 46 passing tests.

All variants lint successfully, v1-v3 are formally equivalent, and `n0` is
formally inequivalent. At WIDTH=16, v1 improves delay by 16.03%, area by 7.99%,
and cells by 8.26%. The staged v3 result is effectively identical, improving
delay by 16.04%, area by 7.99%, and cells by 8.26%. The decoded v2 alternative
improves delay by 14.85% but worsens area by 38.62% and cells by 47.93%.

Codex-only finds the correct guarded shift rewrite with 0.86 confidence and
predicts delay and area improvement while leaving cell count uncertain. Hybrid
uses the exact 16-bit amount versus 4-bit index evidence, reports 0.84
confidence, and matches all three measured improvement directions. Both runs
are schema-valid, tool-free, read-only, and blinded to synthesis labels.

## Additional progress: width and signedness

- Added `width_signedness` as the eighth registered RTL family.
- Added a baseline with two WIDTH-bit operands explicitly sign-extended to 2*WIDTH before comparison.
- Added direct signed-cast, natural-width typed, and sign-split equivalent candidates.
- Added an intentionally incorrect unsigned `n0` comparison.
- Added `width_signedness.redundant_sign_extension.v1`, which infers natural operand width from repeated sign bits and signed operator parameters.
- Increased the test suite from 46 to 50 passing tests.

All variants lint successfully, v1-v3 are formally equivalent, and `n0` is
formally inequivalent. At WIDTH=16, v1 and v2 map exactly identically to v0:
delay, area, and cell-count changes are all 0.00%. The manual sign-split v3
candidate worsens delay by 9.49%, area by 12.82%, and cells by 7.84%.

Codex-only identifies the safe direct signed comparison with 0.98 confidence
but incorrectly predicts improvement in all three PPA metrics. Hybrid reports
the same transformation at 0.72 confidence and predicts neutral delay, area,
and cell count, exactly matching the mapped result. Both runs are schema-valid,
tool-free, read-only, and blinded to synthesis labels.

## Final v1 pilot result

- Completed all 276 planned records: 36 rules runs, 144 first-pass model runs,
  and 96 repeat model runs.
- Completed all 240 Codex calls with zero terminal failures and zero retries.
- Preserved 276 unique immutable run keys and regenerated the report solely
  from those records.
- Corrected the report audit so repeated cases contribute to stability and
  operations metrics without being triple-weighted in primary accuracy.
- Added direction coverage and per-family results to the final JSON and
  Markdown reports.
- Kept the complete automated suite at 66 passing tests.

On the 36 unique held-out cases, hybrid xhigh has the best actionable-accuracy
point estimate at 0.4722, compared with 0.4167 for rules and 0.3611 for Codex
xhigh. The paired hybrid improvement is 5.56 percentage points over rules
(95% bootstrap interval 0.00 to 13.89) and 11.11 points over Codex xhigh
(interval -5.56 to 27.78). It therefore fails the preregistered materiality and
confidence rule: the evidence does not establish that hybrid is superior.

Rules has the strongest conditional direction accuracy at 0.6296 with full
coverage. Hybrid xhigh reaches 0.5208 with 0.8889 coverage; Codex xhigh reaches
0.3229 with the same coverage. Mean canonical-implementation regret is 4.8423
for rules, 4.8504 for hybrid xhigh, and 5.5839 for Codex xhigh.

Ultra produces no actionable-accuracy gain over xhigh. Its mean latency rises
from 22.68 to 36.67 seconds for Codex-only and from 21.84 to 33.79 seconds for
hybrid, so xhigh remains the default. Exact action/transformation agreement on
the 12 repeated cases is 1.0000 for Codex-only and 0.9167 for hybrid.

All 140 first-pass actionable patch attempts pass lint and formal equivalence.
This is a safety result, not proof of PPA benefit. Across the model campaign,
the stored usage totals are 3,241,155 input tokens, 249,436 output tokens, and
176,130 reasoning-output tokens.

Family results identify the v2 priorities. Variable shifting is the strongest
family at 4/4 actionable decisions for every arm, with perfect hybrid direction
accuracy. Hybrid materially helps the point estimates for comparator selection
and width/signedness by inheriting calibrated structural evidence. Every arm is
0/4 on arithmetic resource sharing, mux placement, and priority selection, and
only 1/4 on adder association. Decode factoring also exposes large ranking
regret because the canonical rewrite is not the best generated encoding.

The correct v1 conclusion is therefore that the evaluation and safety harness
works, hybrid feedback is promising but unproven, and the current advisor is
not production-accurate. A v2 should focus on action gating, family-specific
PPA calibration, and candidate generation/ranking before expanding the corpus.

## Additional progress: benchmark infrastructure and smoke campaign

- Added five fixed benchmark arms: rules, Codex xhigh, Codex ultra, hybrid
  xhigh, and hybrid ultra.
- Added deterministic smoke and pilot plans. The full pilot contains 276 total
  records: 36 rules runs and 240 top-level model runs, including the specified
  repeat measurements.
- Added immutable per-run model directories, latency and token-usage capture,
  one controlled retry, explicit failure records, and cache-safe resumption.
- Added actionable accuracy, direction accuracy, ranking regret, safe-patch
  success, latency, model usage, and run-to-run agreement scoring.
- Added deterministic 2,000-resample bootstrap intervals and the v1 materiality
  thresholds for hybrid and ultra comparisons.
- Added JSON and Markdown reports rebuilt solely from stored run records.
- Increased the automated test suite from 61 to 66 passing tests.

The bounded smoke campaign completed all 20 planned records: four blinded
held-out cases across all five arms, comprising 16 Codex calls and four rules
runs. There were no terminal failures. Hybrid xhigh achieved 0.75 actionable
accuracy, versus 0.50 for rules and 0.50 for Codex xhigh. Rules achieved the
highest direction accuracy at 0.9167; hybrid xhigh reached 0.75 and Codex
xhigh reached 0.3333. The four-case confidence intervals include zero, so this
is an infrastructure result rather than evidence of superiority. Ultra did
not improve actionable accuracy over xhigh in either model-backed mode.

The smoke report is reproducible from the 20 stored records. The finalized
resource-sharing `n0` patch also passes lint, fails formal equivalence with a
counterexample, skips synthesis, and is rejected while leaving corpus RTL
unchanged.

The 36-case pilot rules baseline is also complete with no failures. It records
0.4167 actionable accuracy (95% bootstrap interval 0.25 to 0.5833), 0.6296
direction accuracy, and 24/24 successful lint and equivalence checks for
attempted safe patches. The subsequent 240-call model campaign also completed
without a terminal failure or retry; its final results are recorded above.

## Additional progress: safe patch validation

- Added explicit `--emit-patch` and `--patch-candidate` CLI controls.
- Added deterministic unified-diff emission into an isolated artifact workspace.
- Added sequential lint, formal-equivalence, and mapped-synthesis acceptance gates.
- Added source-integrity checks proving that corpus RTL remains byte-for-byte unchanged.
- Added explicit rejection records, counterexamples, skipped-stage status, and failure exit codes.
- Increased the test suite from 54 to 57 passing tests.

The permanent variable-shift v1 patch is accepted and reproduces its 16.03%
delay, 7.99% area, and 8.26% cell improvements. The exercised resource-sharing
negative control passes lint, fails formal equivalence, skips synthesis, and is
rejected with exit status 1. Both paths retain complete stage artifacts and
leave the original source variants unchanged.

## Additional progress: complete corpus acceptance

- Normalized the original resource-sharing family to the five-variant family contract.
- Added deterministic suite generation for 32 development and 36 held-out cases.
- Allocated four held-out cases per family with opaque hashed identifiers.
- Kept held-out widths and seeds disjoint from development parameters.
- Added suite-wide lint, equivalence, synthesis, and cached-result orchestration.
- Increased the test suite from 57 to 61 passing tests.

All 32 development cases and all 36 held-out cases pass the complete acceptance
gate. Each case contains one baseline, three formally equivalent candidates,
and one formally inequivalent control; synthesis records three mapped candidate
comparisons. In total, the permanent corpus now contains 68 cases, 340 RTL
variants, 272 formal candidate/control outcomes, and 204 mapped comparisons.

## Additional progress: popcount and saturation

- Added `popcount_saturation` as the ninth registered RTL family.
- Added a 16-stage serial population-count baseline.
- Added balanced, four-bit chunked, and paired equivalent structures.
- Added an intentionally incorrect `n0` candidate that omits one input bit.
- Added `popcount.serial_accumulation.v1` and suppressed overlapping generic adder findings.
- Increased the test suite from 50 to 54 passing tests.

All variants lint successfully, v1-v3 are formally equivalent, and `n0` is
formally inequivalent. The balanced v1 candidate improves delay by 15.71% but
worsens area by 21.96% and cells by 13.51%. Chunked v2 improves delay by 3.89%
and cells by 1.35% but worsens area by 13.32%. Paired v3 improves delay by
12.49% while worsening area by 21.03% and cells by 12.16%. None satisfies the
default benchmark benefit guardrails.

Codex-only finds the correct structure with 0.98 confidence but incorrectly
predicts improvement in delay, area, and cell count. Hybrid reports the same
timing-oriented rewrite with 0.96 confidence and correctly predicts better
delay, worse area, and uncertain cell count. Both runs are schema-valid,
tool-free, read-only, and blinded to synthesis labels.
