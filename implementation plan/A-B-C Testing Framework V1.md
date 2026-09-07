# RTL Advisor A/B/C Testing Framework V1

## Decision this experiment answers

Does the RTL Advisor plugin improve Codex's ability to produce safe,
evidence-backed RTL PPA decisions, or does its narrow MVP boundary suppress
useful engineering reasoning?

This is a product comparison, not a model-training benchmark. The tested
plugin contains deterministic support for width-safe unsigned combinational
adder reassociation; ML confidence is not used for candidate selection or a
final decision.

## Frozen arms

| Arm | Access | Required behavior |
|---|---|---|
| A — Codex only | RTL, compile context, ordinary shell and EDA tools | Analyze broadly. Do not read RTL Advisor skills, code, artifacts, or other arms. |
| B — Plugin only | Same inputs plus the installed RTL Advisor skill and CLI | Follow the released skill exactly. Do not add unsupported rewrites using unaided reasoning. |
| C — Hybrid | Same inputs, ordinary tools, and the installed skill and CLI | Reason broadly, but delegate supported candidates, formal, and measurement to RTL Advisor. Never override formal or synthesis evidence. |

All arms use GPT-5.6-sol at xhigh reasoning effort, the same objective, a
maximum of three candidates per case, and a 20-minute case budget. An arm may
return `no_change`, `unsupported`, or `inconclusive`; those are legitimate
engineering decisions.

## Frozen corpus

The scorecard contains 24 cases:

- 12 new generated cases that were not used in the earlier telemetry test.
- 12 pre-registered Tier-A open-source modules from the frozen OpenTitan,
  PULP common_cells, and verilog-axis snapshots.

The six case groups are:

1. Unsigned combinational arithmetic.
2. Mixed procedural and continuous logic.
3. Mux, priority, and decode structure.
4. Width, signedness, and semantic boundaries.
5. No-change and harmful-rewrite traps.
6. Sequential, buffering, and pipeline boundaries.

The manifest fixes source order, source hashes, top modules, compile-context
references, objectives, provenance, and licenses before arm output is seen.
The evaluator oracle is stored separately and is never included in an arm
packet.

## Isolation and repetitions

- Arms cannot inspect another arm's run directory.
- Arms cannot inspect the evaluator oracle.
- The earlier `telemetry_reduce` case is excluded from scoring because the
  plugin and Codex have already seen it.
- Each arm is intended to run twice independently. A third run is required
  only for case-level decision disagreement.
- The first pass is reported separately and cannot be labeled a final product
  benchmark until the repeat pass is complete.

## Independent evidence gate

An agent's prose is never treated as proof. For each proposed candidate, the
coordinating evaluator must:

1. Confirm the baseline hash and that the original source is unchanged.
2. Compile or lint the baseline and isolated candidate under the same context.
3. Run the required formal relation.
4. Run baseline and candidate through the same pinned synthesis recipes when
   formal passes and the case is eligible for MVP measurement.
5. Derive the final decision from normalized evidence.

Direct combinational candidates use RTL-to-RTL Yosys equivalence. Stateful or
latency-changing candidates require the case's declared P2/P3 contract; an
arm must not recommend them when that contract is unavailable.

## Primary metrics

| Metric | Definition | Initial target |
|---|---|---:|
| Validated decision accuracy | Cases whose final action matches independent evidence | >= 85% |
| Robust opportunity recall | Known useful/supported opportunities that produce a safe validated result | >= 80% |
| Correct no-change rate | Trap or neutral cases where the arm avoids a harmful/unproven recommendation | >= 90% |
| Evidence completion | Actionable claims with complete hash-matched proof and measurement | >= 95% |
| Harmful recommendation rate | Recommended candidates that fail formal or regress | 0% |

Secondary metrics are unsupported-scope clarity, source-link accuracy,
reproduction rate, wall time, tool invocations, and explanation quality. Model
confidence is diagnostic only and is not a primary product metric.

## Hard guardrails

Any of the following is a release-blocking failure:

- A harmful candidate is recommended.
- A candidate is presented as safe without the required proof.
- Source RTL is modified in place.
- Measurement is fabricated, stale, or run under mismatched inputs.
- A result claims whole-design coverage while excluding unsupported logic.
- Cases or outcomes are omitted after results are known.

## Product decision rule

Arm C becomes the default Codex experience only when it has:

- zero harmful recommendations;
- opportunity recall no more than 5 percentage points below Arm A;
- at least 10 percentage points more evidence completion than Arm A;
- at least 15 percentage points more validated coverage than Arm B;
- at least 95% reproducibility; and
- no more than 2x Arm A runtime unless the additional time produces material
  formal or synthesis evidence.

If C misses those gates, the scorecard determines whether to broaden the
plugin, keep it as an optional proof tool, or remove it from the default flow.

## Execution checklist

- [x] Freeze experiment protocol and metrics.
- [x] Create 12 held-out generated cases.
- [x] Reuse the 12-case frozen open-source Tier-A tranche.
- [ ] Validate all case hashes and compile-context references.
- [ ] Launch isolated A, B, and C first-pass agents.
- [ ] Independently verify proposed candidates.
- [ ] Publish the complete first-pass scorecard.
- [ ] Run the required independent repeat pass.
- [ ] Resolve disagreements with a pre-declared third pass.
- [ ] Make the plugin product decision.

