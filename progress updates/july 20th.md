# Progress update — July 20

## A/B/C plugin validation complete

Implemented and executed a pre-registered comparison of three GPT-5.6-sol
xhigh workflows:

- Arm A: Codex with ordinary RTL reasoning and raw EDA tools.
- Arm B: the released RTL Advisor plugin and CLI only.
- Arm C: hybrid broad Codex reasoning plus RTL Advisor evidence execution.

The experiment froze 24 hash-matched cases before arm output: 12 new generated
modules and the existing 12-case Tier-A open-source tranche from OpenTitan,
PULP common_cells, and verilog-axis. The framework now validates source and
packet hashes, confines candidate paths, rejects stale result records, requires
an independent evidence audit before scoring, reruns generated P1 formal, and
distinguishes generic single-recipe ABC output from the product's standard and
stronger pinned recipes.

Two full independent passes were completed for every arm. The plugin-only arm
was 100% reproducible. Codex-only had 45.83% decision reproducibility, and the
hybrid arm had 70.83%. Targeted third passes were run only for disagreements;
three label decisions remain unresolved in each of the Codex-only and hybrid
arms because `no_change`, `unsupported`, and `inconclusive` lack deterministic
semantics.

The scorecard result is:

| Arm | Validated decision accuracy | Pre-registered target evaluation | Correct no-change | Evidence completion | Harmful recommendations |
|---|---:|---:|---:|---:|---:|
| A — Codex only | 83.33% | 0% under the product evidence gate | 100% | 59.02% | 0 |
| B — Plugin only | 95.83% | 75% | 100% | 100% | 0 |
| C — Hybrid | 95.83% | 75% | 100% | 100% | 0 |

The 75% value is evaluation coverage over four pre-registered hypotheses, not
optimization-win recall. No product-measured improvement existed in this
experiment. The three released adder-family candidates passed formal and both
pinned synthesis recipes, but all mapped identically to baseline. Codex also
found broader mux and priority/decode hypotheses. The mux factoring was formal
but only generically measured and neutral; a direct Boolean priority encoder
was formal but regressed generic area and depth, so the baseline was retained.

The product decision is to keep RTL Advisor as a bounded, optional evidence
executor. The hybrid flow does not become the default because it added no
validated coverage over plugin-only, missed the 95% reproducibility gate, and
did not capture consistent runtime. This confirms that the plugin is useful
for repeatable evidence discipline, but it is not yet a generally useful RTL
optimizer.

The next work is deterministic boundary labeling, additional frozen
transformation families, P2/P3 corpus integration, and a sealed benchmark with
real measured wins as well as neutral and regressed cases.

