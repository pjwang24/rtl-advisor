# RTL Advisor

RTL Advisor is a reproducible harness for generating SystemVerilog variants,
proving logical equivalence, measuring synthesis results, and benchmarking
pre-synthesis feedback.

The current implementation covers Step 1 (project setup), Step 2 (the first
generated lint and formal-equivalence case), Step 3 (mapped delay/area
comparison), and Step 4A (hierarchy-preserving RTL graphs plus the first
pre-synthesis resource-sharing rule), and Step 4B (blinded Codex-only and
hybrid analysis). Step 5A adds the reusable family registry and the second
end-to-end transformation family. Step 5B1 adds priority selection and the
first case where hybrid direction prediction outperforms Codex-only. Step
5B2a adds common-operand mux placement and bidirectional tradeoff findings.
Step 5B2b-1 adds decode factoring and calibrates recommendations against an
exactly neutral synthesis result. Step 5B2b-2 adds comparator selection and a
case where hybrid feedback prevents a harmful source-level optimization. Step
5B2b-3 adds wide variable-shift analysis and guarded shift-amount narrowing.
Step 5B2b-4 adds signed-width analysis and redundant-extension calibration.
Step 5B2b-5 completes the nine-family set with population-count restructuring.
Step 5C adds safe patch validation, Step 5D accepts the full 68-case corpus,
and Step 6 completes the five-arm smoke and 240-call held-out pilot.

```bash
uv sync --no-editable
uv run --no-editable rtl-advisor setup
uv run --no-editable pytest
```

`--no-editable` avoids a macOS managed-environment issue where Python 3.13
skips editable-package `.pth` files carrying the hidden filesystem flag.
After changing CLI source code, refresh the installed command with:

```bash
uv sync --no-editable --reinstall-package rtl-advisor
```

Use JSON output for automation:

```bash
uv run --no-editable rtl-advisor setup --json
```

Generate and validate the first case:

```bash
uv run --no-editable rtl-advisor corpus generate
uv run --no-editable rtl-advisor lint corpus/development/dev_rs_0001
uv run --no-editable rtl-advisor equivalence corpus/development/dev_rs_0001
uv run --no-editable rtl-advisor synth corpus/development/dev_rs_0001
uv run --no-editable rtl-advisor graph corpus/development/dev_rs_0001 --variant v0
uv run --no-editable rtl-advisor analyze corpus/development/dev_rs_0001 --variant v0 --mode rules
uv run --no-editable rtl-advisor analyze corpus/development/dev_rs_0001 --variant v0 --mode codex --effort xhigh
uv run --no-editable rtl-advisor analyze corpus/development/dev_rs_0001 --variant v0 --mode hybrid --effort xhigh
```

Generate and validate the adder-association family:

```bash
uv run --no-editable rtl-advisor corpus generate --family adder_reduction_association --suite development
uv run --no-editable rtl-advisor lint corpus/development/dev_aa_0001
uv run --no-editable rtl-advisor equivalence corpus/development/dev_aa_0001
uv run --no-editable rtl-advisor synth corpus/development/dev_aa_0001
uv run --no-editable rtl-advisor analyze corpus/development/dev_aa_0001 --variant v0 --mode rules
```

Generate and validate the priority-selection family:

```bash
uv run --no-editable rtl-advisor corpus generate --family priority_selection --suite development
uv run --no-editable rtl-advisor lint corpus/development/dev_pr_0001
uv run --no-editable rtl-advisor equivalence corpus/development/dev_pr_0001
uv run --no-editable rtl-advisor synth corpus/development/dev_pr_0001
uv run --no-editable rtl-advisor analyze corpus/development/dev_pr_0001 --variant v0 --mode rules
```

Generate and validate the mux-placement family:

```bash
uv run --no-editable rtl-advisor corpus generate --family mux_placement --suite development
uv run --no-editable rtl-advisor lint corpus/development/dev_mp_0001
uv run --no-editable rtl-advisor equivalence corpus/development/dev_mp_0001
uv run --no-editable rtl-advisor synth corpus/development/dev_mp_0001
uv run --no-editable rtl-advisor analyze corpus/development/dev_mp_0001 --variant v0 --mode rules
```

Generate and validate the decode-factoring family:

```bash
uv run --no-editable rtl-advisor corpus generate --family decode_factoring --suite development
uv run --no-editable rtl-advisor lint corpus/development/dev_df_0001
uv run --no-editable rtl-advisor equivalence corpus/development/dev_df_0001
uv run --no-editable rtl-advisor synth corpus/development/dev_df_0001
uv run --no-editable rtl-advisor analyze corpus/development/dev_df_0001 --variant v0 --mode rules
```

Generate and validate the comparator-selection family:

```bash
uv run --no-editable rtl-advisor corpus generate --family comparator_selection --suite development
uv run --no-editable rtl-advisor lint corpus/development/dev_cs_0001
uv run --no-editable rtl-advisor equivalence corpus/development/dev_cs_0001
uv run --no-editable rtl-advisor synth corpus/development/dev_cs_0001
uv run --no-editable rtl-advisor analyze corpus/development/dev_cs_0001 --variant v0 --mode rules
```

Generate and validate the variable-shift family:

```bash
uv run --no-editable rtl-advisor corpus generate --family variable_shift --suite development
uv run --no-editable rtl-advisor lint corpus/development/dev_vs_0001
uv run --no-editable rtl-advisor equivalence corpus/development/dev_vs_0001
uv run --no-editable rtl-advisor synth corpus/development/dev_vs_0001
uv run --no-editable rtl-advisor analyze corpus/development/dev_vs_0001 --variant v0 --mode rules
```

Generate and validate the width/signedness family:

```bash
uv run --no-editable rtl-advisor corpus generate --family width_signedness --suite development
uv run --no-editable rtl-advisor lint corpus/development/dev_ws_0001
uv run --no-editable rtl-advisor equivalence corpus/development/dev_ws_0001
uv run --no-editable rtl-advisor synth corpus/development/dev_ws_0001
uv run --no-editable rtl-advisor analyze corpus/development/dev_ws_0001 --variant v0 --mode rules
```

Generate and validate the popcount/saturation family:

```bash
uv run --no-editable rtl-advisor corpus generate --family popcount_saturation --suite development
uv run --no-editable rtl-advisor lint corpus/development/dev_pc_0001
uv run --no-editable rtl-advisor equivalence corpus/development/dev_pc_0001
uv run --no-editable rtl-advisor synth corpus/development/dev_pc_0001
uv run --no-editable rtl-advisor analyze corpus/development/dev_pc_0001 --variant v0 --mode rules
```

Explicitly emit and validate a candidate patch in an isolated workspace:

```bash
uv run --no-editable rtl-advisor analyze corpus/development/dev_vs_0001 --variant v0 --mode rules --emit-patch --patch-candidate v1
```

The command never modifies corpus RTL. It normalizes the candidate into a
disposable copy, emits a unified diff, runs Verilator lint, proves equivalence,
and maps both versions with Yosys/ABC. Synthesis runs only after lint and formal
success. The command returns failure for rejected patches and preserves the
counterexample and stage records.

Generate and validate the complete deterministic suites:

```bash
uv run --no-editable rtl-advisor corpus generate-suite --suite development
uv run --no-editable rtl-advisor corpus validate-suite --suite development
uv run --no-editable rtl-advisor corpus generate-suite --suite heldout
uv run --no-editable rtl-advisor corpus validate-suite --suite heldout
```

The development allocation contains 32 cases; held-out contains four cases per
family for 36 total. Held-out identifiers are opaque hashes, and their widths
and seeds are disjoint from development. Every case has one baseline, three
equivalent candidates, and one inequivalent control.

Run or resume the bounded smoke benchmark and rebuild its report:

```bash
uv run --no-editable rtl-advisor benchmark run --suite smoke --arm all
uv run --no-editable rtl-advisor benchmark report --suite smoke
```

The completed full campaign uses `--suite pilot`: 36 rules records and 240
model records, including repeat measurements. It caches completed runs and
preserves explicit failures. Reports are regenerated solely from the immutable
JSON records under `artifacts/benchmarks`.

The first family contains a baseline (`v0`), three equivalent resource-sharing
rewrites (`v1`-`v3`), and an intentionally incorrect negative control (`n0`).
Equivalence succeeds only when all four expected proof outcomes are observed.

The synthesis command refuses unproven candidates, maps the baseline and
equivalent candidate against the pinned Nangate45 library, and writes delay,
area, cell-count, provenance, mapped-netlist, and comparison artifacts.

The graph command deliberately stops before flattening or technology mapping.
It retains module instances, typed operator nodes, bit-level dependencies,
Yosys source locations, provenance, deterministic hashes, and cache keys. The
rules analyzer currently detects duplicate adders or multipliers feeding a
result mux and recommends selecting operands before the shared operator. Its
finding records the structural evidence, source location, confidence, expected
area/cell direction, and the risk that the rewrite can worsen timing.

For the generated `dev_rs_0001` case, v0 receives one recommendation at
`rtl/v0.sv:17`; v1, which already implements the sharing pattern, receives
a reverse timing-oriented mux-placement recommendation.

Codex-only receives blinded RTL plus the registered transformation catalog.
Hybrid receives the same input plus sanitized rules-only findings. Neither
input contains case IDs, variant IDs, synthesis metrics, or outcome labels.
Every Codex run is ephemeral, read-only, isolated from user configuration, and
constrained by a checked JSON response schema. JSONL events are retained for
audit, and a result is rejected if Codex invokes a command, web search, MCP
tool, or other non-reasoning item. Invalid responses and timeouts produce
explicit failure records rather than silently disappearing.

The live Sol/xhigh smoke run found the resource-sharing opportunity in v0 in
both Codex-only and hybrid modes. Hybrid cited the correct source region;
Codex-only was off by one line, providing an early measurable accuracy
difference. On v1, both modes recognized the existing area-oriented sharing
and proposed the reverse timing/area tradeoff with explicit area risk. These
are predictions only; the model-visible inputs contain no synthesis labels.

The v1 implementation plan is complete. Hybrid xhigh has the best held-out
actionable-accuracy point estimate, but it does not satisfy the preregistered
materiality and confidence rule; see the final pilot report under
`artifacts/benchmarks/pilot/report.md`.

Corpus families are registered independently from the CLI. The adder-
association family contains one three-level serial baseline, three equivalent
two-level balanced trees, and a separate inequivalent `n0` control. Development
and held-out generation use disjoint default widths and seeds; held-out case
IDs are deterministic hashes that do not reveal the transformation family.

For the permanent WIDTH=16 `dev_aa_0001` case, all five variants pass lint,
v1-v3 are formally equivalent, and `n0` is formally inequivalent. Compared
with the serial baseline, every balanced candidate improves mapped delay by
8.88% and cell count by 5.58% while increasing area by 8.51%. The serial-chain
rule fires on v0 and not on the balanced candidate. Live Sol/xhigh Codex-only
and hybrid runs both recommend reassociation without seeing those labels; the
Codex-only arm also identifies a valid intermediate-width opportunity.

The priority-selection family contains a low-index-priority `if/else` baseline,
three equivalent case/ternary/decoded candidates, and a separate altered-
priority control. All variants lint, v1-v3 are formally equivalent, and `n0`
is formally inequivalent. Compared with v0, candidates improve area by
22.34-28.74% and cells by 12.21-38.93%, but worsen delay by 21.51-28.34%.
This evidence corrected the deep-mux rule to predict area/cell improvement and
uncertain timing. In live blinded runs, Codex-only incorrectly predicted delay
improvement; hybrid predicted area/cell improvement and uncertain timing,
matching the observed directions without receiving synthesis labels.

The mux-placement family contains a baseline with two mutually exclusive
additions sharing one operand, three equivalent candidates that select the
differing operand before one adder, and an inequivalent control. At WIDTH=16,
every candidate improves delay by 13.41% and cells by 0.73%, with only 0.93%
area regression. The rules engine distinguishes common-operand mux placement
from generic arithmetic sharing and can describe both transformation
directions. Codex-only identifies the rewrite as resource sharing; hybrid uses
the more precise mux-placement category. Both model arms predict the cell
benefit and keep timing uncertain, while the structural rule predicts the
observed delay improvement.

The decode-factoring family repeats two opcode comparisons in its baseline and
provides shared-decode, `case`, and masked-decode equivalents. Direct factoring
in v1 is exactly neutral after mapping, showing why a structural cleanup should
not automatically be advertised as a PPA improvement. The v2 `case` rewrite
improves area by 8.90% and cells by 26.36% but worsens delay by 19.35%. The v3
masked-decode rewrite improves delay by 7.84% and cells by 29.09% with a 5.74%
area cost, satisfying the timing-benefit guardrail. Codex-only overpredicts the
direct factoring benefit; hybrid inherits the calibrated rule and correctly
predicts neutral delay, area, and cell count. Neither model selected v3, which
creates a useful ranking-regret example for the benchmark.

The comparator-selection family contains two parallel unsigned comparisons
followed by a one-bit result mux. Its three equivalent candidates select two
WIDTH-bit operands before one comparator. Despite appearing to share hardware,
all three candidates are harmful under the benchmark guardrails. The direct
shared form worsens delay by 6.33%, area by 18.30%, and cells by 0.99%. The
other encodings worsen delay by about 0.88% and area by 11.78-12.53% while
reducing cells by 5.94%. The calibrated rule therefore advises retaining the
parallel comparisons unless target synthesis proves otherwise. Codex-only
confidently recommends the harmful rewrite and predicts area/cell improvement;
hybrid returns no actionable recommendation, matching the measured decision
without receiving synthesis labels.

The variable-shift family starts with a WIDTH-bit data value shifted by a
WIDTH-bit amount. Its equivalent candidates preserve the required zero result
for amounts greater than or equal to WIDTH while narrowing the barrel-shifter
control, decoding fixed shifts, or using a guarded staged network. At WIDTH=16,
the guarded narrow and staged forms improve delay by about 16.0%, area by
7.99%, and cells by 8.26%. The fully decoded form improves delay by 14.85% but
increases area by 38.62% and cells by 47.93%. Codex-only finds the correct
guarded rewrite but leaves cell direction uncertain; hybrid predicts all three
observed improvement directions. Both remain blinded to synthesis labels.

The width/signedness family explicitly sign-extends two WIDTH-bit operands to
2*WIDTH before a signed comparison. Natural-width direct and typed signed
comparisons map exactly identically at WIDTH=16: delay, area, and cell-count
changes are all 0.00%. A manual sign-split alternative worsens delay by 9.49%,
area by 12.82%, and cells by 7.84%. The rule reconstructs the natural source
width from repeated sign bits and recommends the direct form for clarity and
sizing safety without promising PPA improvement. Codex-only overpredicts all
three directions; hybrid predicts neutral PPA exactly.

The popcount/saturation family counts 16 independent input bits using a serial
accumulator baseline. A balanced tree improves delay by 15.71% but worsens area
by 21.96% and cells by 13.51%. Chunked accumulation improves delay by 3.89%
with 13.32% area regression, while the paired form improves delay by 12.49%
with 21.03% area regression. None satisfies the default benefit guardrails.
The calibrated rule presents balancing as a timing-driven area tradeoff.
Codex-only incorrectly predicts improvement in every metric; hybrid predicts
better delay, worse area, and uncertain cell count.

Safe patch emission is opt-in through `--emit-patch`. The accepted
`dev_vs_0001` v1 patch passes all three gates and reproduces its measured PPA
improvements. An exercised negative control passes lint, fails equivalence,
skips synthesis, returns a rejected record, and leaves every source file
byte-for-byte unchanged.

The permanent suite acceptance gate passes all 32 development and all 36
held-out cases. Every case passes lint, all three candidates prove equivalent,
every negative control proves inequivalent, and mapped synthesis records three
comparisons. The full corpus therefore contains 68 cases, 340 RTL variants,
272 formal candidate/control results, and 204 mapped candidate comparisons.

The test suite currently contains 153 passing tests.

The approved v1 implementation plan is stored in
[`implementation plan/v1.md`](implementation%20plan/v1.md).

## V2.1 recovery workflow

The V2.1 workflow is versioned separately from the current live default. It uses
generated RTL only, kernel-only pre-synthesis features, PySlang syntax facts,
family nearest-neighbor OOD evidence, grouped-out-of-fold random forests, and a
locked OpenROAD physical cross-check.

```bash
rtl-advisor benchmark diagnose-v2
rtl-advisor benchmark openroad-lock-v2 \
  --image openroad/orfs:latest \
  --orfs-root third_party/OpenROAD-flow-scripts
rtl-advisor benchmark openroad-run-v2 --workers 2
rtl-advisor benchmark openroad-report-v2

rtl-advisor corpus generate-suite-v21 --split calibration-v21
rtl-advisor corpus generate-suite-v21 --split heldout-v21
rtl-advisor corpus validate-suite-v21 \
  --split calibration-v21 --synthesize --workers 4
rtl-advisor corpus validate-suite-v21 --split heldout-v21 --workers 4
rtl-advisor model train-v21

# These commands remain blocked until calibration, formal, model, and physical
# prerequisites all pass. Blind synthesis starts only after lock + unseal.
rtl-advisor benchmark lock-v21
rtl-advisor benchmark run-v21 --synthesis-workers 4
rtl-advisor benchmark report-v21
```

The immutable implementation contract is in
`implementation plan/v2.1.md`; execution status is recorded in
`progress updates/july 14th.md`.

## V2.2 family-risk workflow

V2.2 preserves the V2.1 feature, direction, OOD, ranking, formal, and passing
physical-evidence stacks. It replaces only the global eligibility gate with
grouped-OOF family classifiers and a jointly constrained family-threshold
policy.

```bash
rtl-advisor model train-v22
rtl-advisor benchmark diagnose-v22

# Live analysis and blind locking reject diagnostic-only calibration results.
rtl-advisor analyze-v22 path/to/case --mode safe
rtl-advisor benchmark lock-v22
```

The frozen calibration produced a safe diagnostic frontier—37.4% opportunity
recall, 99.4% specificity, 4.4% harmful recommendations, and 68.4% balanced
actionable accuracy—but missed the 70% calibration gate. Consequently no V2.2
blind suite, lock, synthesis, or unseal artifact was created. The contract and
exact evidence are recorded in `implementation plan/v2.2.md` and
`progress updates/july 14th.md`.

The frozen failure diagnostic shows that all 86 covered opportunities already
select a measured-best candidate and measured-oracle reranking recovers no new
cases. The 144 misses are 131 below-threshold cases plus 13 opportunities in
unsupported families. The next iteration therefore targets eligibility-score
separation and family support, not candidate-ranking replacement.

## V2.3 stacked-eligibility recovery

V2.3 is preregistered in `implementation plan/v2.3.md`. It adds 384 generated,
formally checked calibration cases across the seven families that need support
or better score separation. Stacked family models may use only live-available
kernel features and leakage-checked pre-synthesis PPA predictions. The unchanged
V2.2 risk optimizer is evaluated on the same 936-case reference distribution,
and no V2.3 held-out suite may be generated until calibration and a targeted
OpenROAD delta audit both pass.

## Synthesis-redundancy pilot

The V1 synthesis-redundancy benchmark tests whether RTL benefits measured with
the standard Yosys/ABC recipe remain useful after stronger Yosys optimization.
It uses 27 generated calibration cases, requires current RTL-to-RTL equivalence
proofs for all 81 candidates, and runs 108 baseline/candidate synthesis jobs.

```bash
rtl-advisor benchmark synthesis-redundancy-v1 --workers 4
```

All 108 runs passed. Of 21 candidates that were useful under the standard
recipe, 15 remained useful under stronger synthesis, six were removed, and
seven additional candidates were useful only under the stronger recipe. This
71.4% survival result supports continuing only with families whose benefit is
repeatable across synthesis settings. The frozen experiment is documented in
`implementation plan/synthesis redundancy v1.md`.

## Local frontend

Frontend V1 is a dependency-free, local-only dashboard over frozen V2.2 model
evaluation results. It provides a plain-language release-readiness overview,
results by RTL pattern, a filterable 936-case explorer, generated RTL and
candidate drill-down, and a preview of the future live-analysis workflow.

```bash
PYTHONPATH=src .venv/bin/python -m rtl_advisor frontend
```

Open `http://127.0.0.1:8765`. The server binds only to localhost by default,
loads no external assets, and rejects every mutation request. Live RTL analysis
remains visibly locked until V2.3 meets its model-quality targets and passes its
OpenROAD delta audit. The frozen boundary is documented in
`implementation plan/frontend v1.md`.
