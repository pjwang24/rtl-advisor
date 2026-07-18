# RTL Advisor Progress Update — July 14, 2026

## Current state

V2 implementation and the locked 72-case blind benchmark are complete. The primary V2 system is **not promoted**: it met the actionable-accuracy and safety targets but abstained on every held-out case, so it missed the opportunity, direction, exact-best, and regret-improvement gates. The random-forest challenger is the strongest v2.1 starting point, but it also missed the locked direction, exact-best, and regret gates.

- Implementation plan: `implementation plan/v2.md`
- Benchmark lock: `artifacts/benchmarks/v2/benchmark-lock.json`
- Lock hash: `39fbef7db51025cacaed40307568f2506a5cc37ed8abfb031aaa27b184e3ef2e`
- Locked model-call budget: 264 GPT-5.6 Sol calls at xhigh effort
- Locked benchmark records: 480 total across six arms, including 48 stability repeats
- Final stored-record report: `artifacts/benchmarks/v2/report.json`
- Final run summary: `artifacts/benchmarks/v2/run-summary.json`
- Report SHA-256: `0136d55c686018caebbc3b77c1fda6b3b001ad72f304ce95024b887dd323ff92`
- Campaign classification: `rerun`; 480/480 records executed, 479 passed, one timed out, and zero cached substitutions were used

## What was implemented

### Live RTL frontend and safety gate

- PySlang-based SystemVerilog parsing/lint with file, filelist, include-directory, define, canonical-path, and content-hash handling.
- Yosys pre-mapping graph extraction for authorized live RTL.
- Fixed pre-mapping feature schema covering operator widths/counts, arithmetic/control depth, fan-in/fanout, dynamic shifts, signedness and width conversions, repeated structures, and local cones.
- Balanced, timing-first, and area-first profiles with conservative lower-bound eligibility and explicit abstention.
- Dependency-free JSON evaluation of the calibrated shallow-tree gate.
- Fail-closed behavior for missing models, out-of-distribution inputs, lint failures, unsupported designs, and inconclusive candidate verification.

### Generated topology corpus

- Nine optimization families.
- 360 calibration cases and 72 held-out cases.
- Five variants per case: baseline `v0`, equivalent alternatives `v1`–`v3`, and inequivalent control `n0`.
- Deterministic pairwise-coverage selection with seed `20260714` and at least 85% legal pair coverage.
- The legal support envelope excludes constant-function comparison/decode points and multiplier cones that exceed the bounded open-source formal capacity. Resource-sharing and mux-placement multiplication is represented up to 8 bits and three branches; larger widths and four-way structures remain represented by other operations.

### Formal and synthesis evidence

- Final calibration suite hash: `e235bacce5396addeabda711dc64ffdf09a2c9241f5fc8c6daa8ba161bb521bb`
- Calibration validation: 360/360 passed lint, positive equivalence, negative-control rejection, and `v0`–`v3` Nangate45 synthesis.
- Final held-out suite hash: `e272f3dea863f26aea401dbbae0ad66d7f16605a8d2adb77686cf0e18054b47b`
- Held-out prevalidation: 72/72 passed lint and formal checks with synthesis explicitly disabled.
- Across both suites: 1,296 positive candidate proofs and 432 negative-control rejection proofs.
- Pinned Liberty SHA-256: `8d540a4d4cf6d09d27c87ad067857a9c0c2eeb023ab7a56e058cd3113db4e9b1`.

### Models

- 1,080 calibration rows: three registered templates per calibration case.
- Gate model hash: `078411083fd5a48aeb57f7e5458b26ed8a975c7043fff3147bda724120e33d59`.
- Gate artifact SHA-256: `8f43700176dd5f26c61fb24f6d8b57ed3694931144cd7c525da9022fabc1bd25`.
- Random-forest challenger SHA-256: `f2dfec8d1f69c163aa1465feb170dab669ac07e390ab3e867601ab1fbc4e346f`.
- Rule coverage was expanded for subtract-based resource sharing and compare-based mux placement.
- Only 15 calibration cases use registered training-only topology context: 13 intentional zero-excess shift boundaries and two equality-to-zero structures normalized away by Yosys. Live analysis remains rule-driven and does not use that fallback.

### Candidate and explanation layers

- Isolated candidate workspaces; original RTL is never edited.
- PySlang, Verilator, and whole-design Yosys equivalence required before a candidate is accepted.
- Generated-corpus templates map safely to the registered `v1`–`v3` alternatives; ambiguous generic rewrites abstain.
- GPT-5.6 Sol xhigh explanation layer with schema validation, no-tool audit, no synthesis-label exposure, and immutable gate decision/candidate fields.

### Locked benchmark runner

- Six arms: V1 rules, V1 Codex xhigh, V1 hybrid xhigh, V2 calibrated gate, V2 safe advisor xhigh, and V2 random-forest challenger.
- Exact 264-call accounting with no retries.
- Blind synthesis starts only after lock verification and unseal recording.
- Hash checks cover suites, calibration rows, gate, challenger, feature schema, profiles, ruleset, prompt/response contracts, and tool versions.
- Stored-record reporting includes actionable accuracy, harmful recommendation rate, opportunity coverage, exact-best accuracy, normalized regret, direction metrics, per-family metrics, 10,000-sample paired bootstrap, candidate-generation yield, reliability, usage, latency, and stability.

## Verification

- Full automated regression: **105 tests passed**.
- Benchmark lock was reverified against the current runtime and tool versions.
- The final report was rebuilt solely from the 480 stored records and reproduced byte-for-byte.
- Codex CLI locked version: `codex-cli 0.144.1`.
- Yosys locked version: `0.63`.
- Verilator locked version: `5.046`.

## Blind benchmark execution

- The suite was first unsealed at `2026-07-14T17:47:12.645937+00:00` after successful lock verification, then all 72 held-out cases and their four equivalent variants were synthesized.
- The first fresh campaign encountered a host-sandbox infrastructure error when nested Codex CLI calls attempted to start. That partial campaign was stopped and archived at `artifacts/benchmarks/v2/archives/20260714T174904Z`.
- The benchmark was rerun outside that host sandbox with the same immutable lock, suites, models, prompts, arm allocation, call budget, and no-retry rule. The final result is therefore explicitly labeled `rerun`, not `fresh_blind_run`.
- All 480 locked records executed, including exactly 264 GPT-5.6 Sol xhigh calls.
- Final execution result: 479 passed records, one failed record, zero cached records.
- The only failed record was `v2_safe_advisor_xhigh` on case `v2_d57a621274b9163a`; its Codex call reached the locked 600-second timeout and was recorded without retry.
- Model usage: 4,055,461 input tokens, 1,057,280 cached-input tokens, 369,121 output tokens, and 241,996 reasoning-output tokens.
- Stability: 21/24 complete case-arm repeat groups were identical for decision and candidate, or 87.5%.

## Actionable accuracy versus direction accuracy

| Arm | Actionable accuracy | Direction accuracy | Direction coverage | Opportunity coverage | Harmful recommendation rate | Exact-best accuracy | Normalized regret |
|---|---:|---:|---:|---:|---:|---:|---:|
| V1 rules | 51.4% | 49.4% | 36.6% | 46.7% | 81.1% | 8.1% | 0.128 |
| V1 Codex xhigh | 27.8% | 31.5% | 67.6% | 66.7% | 83.3% | 5.0% | 0.086 |
| V1 hybrid xhigh | 31.9% | 39.6% | 62.0% | 66.7% | 82.5% | 5.3% | 0.086 |
| V2 calibrated gate | 79.2% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% | 0.208 |
| V2 safe advisor xhigh | 78.9%* | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% | 0.211 |
| V2 random-forest challenger | **91.7%** | **51.9%** | **12.5%** | **60.0%** | **0.0%** | **33.3%** | **0.100** |

\*The V2 advisor micro result uses its 71 completed primary records; reliability was 98.6% because one of 72 calls timed out.

The actionable metric rewards a correct abstention when no eligible transformation exists. Only 15 of the 72 held-out cases contained an eligible optimization, so the calibrated gate's all-abstain policy scored 79.2% actionable accuracy while providing no optimization value. This is why actionable accuracy must be read together with opportunity coverage and direction coverage.

The V2 advisor improved actionable accuracy over V1 hybrid by 47.8 percentage points; the preregistered paired 95% bootstrap interval was `[32.5, 62.3]` percentage points. That improvement is statistically clear but operationally incomplete because it came entirely from conservative abstention. No V2 primary candidate was recommended or emitted, so candidate acceptance and formal-yield evidence are empty rather than positive.

## Promotion decision

The primary V2 system passed:

- Macro and micro actionable accuracy of at least 65%.
- At least a 10-point improvement over V1 hybrid with the paired confidence interval above zero.
- Harmful recommendation rate no greater than 10%.
- The absolute normalized-regret ceiling of 0.25.

It failed:

- Opportunity coverage of at least 50%: observed 0%.
- Direction accuracy of at least 70% at 90% direction coverage: observed 0% at 0% coverage.
- Exact-best accuracy of at least 60%: observed 0%.
- Ranking regret at least 30% below V1 hybrid: V2 regret was higher, not lower.
- Non-vacuous candidate verification yield: no primary candidates were emitted.

The random-forest challenger passed actionable accuracy, harmful-rate safety, and opportunity coverage, but failed direction accuracy, direction coverage, exact-best accuracy, and regret improvement. It remains a challenger and is not promoted into the live V2 path.

## Next move — diagnosis, v2.1, and physical cross-check

1. Diagnose why the calibrated gate rejected all 15 positive opportunities, separating interval conservatism, calibration-to-blind shift, out-of-distribution rejection, source mapping, and candidate-ranking errors.
2. Use the RF challenger as an error-analysis instrument, especially its nine safe positive recommendations, without tuning on this now-unsealed blind suite.
3. Design v2.1 against a newly generated blind suite. Adjust class balance and explicitly optimize selective risk, opportunity coverage, direction coverage, exact-best selection, and calibration—not actionable accuracy alone.
4. Run the planned 27-case, 108-run OpenROAD physical-design cross-check to test whether the Yosys/ABC labels preserve PPA direction after placement and routing. Docker/OpenROAD intervention may be needed at that stage.

## V2.1 recovery implementation

The approved recovery contract is now frozen in `implementation plan/v2.1.md`.
V2 remains unchanged and unpromoted. V2.1 is separately versioned and remains
diagnostic-only unless its calibration, physical, and blind gates pass.

### Frozen V2 diagnosis

- Added `rtl-advisor benchmark diagnose-v2` and immutable postmortem artifacts under `artifacts/benchmarks/v2`.
- Preserved the original V2 report SHA-256 `0136d55c686018caebbc3b77c1fda6b3b001ad72f304ce95024b887dd323ff92` byte-for-byte.
- Confirmed 204/204 detected candidates were rejected as out of domain because calibration used wrapper-level graph facts while live inference used kernel-level facts.
- Confirmed one rule-level missed opportunity: the equality-to-zero comparator case `v2_dac48735a0bde7f3`.
- Corrected shadow metrics show the RF challenger at 60% opportunity recall, 0% harmful recommendations, 80% balanced actionable accuracy, 77.8% tie-aware exact-best accuracy, and 0.131 conditional normalized regret.
- Corrected RF direction accuracy is 88.9% delay, 11.1% area, 55.6% cell count, and 51.9% overall. The old all-case direction-coverage denominator was mathematically incompatible with safe abstention.

### V2.1 corpus and formal/synthesis evidence

- Generated 648 new, unique topology signatures with seed `20260715`, all disjoint from the 432 V2 signatures.
- Calibration suite: 576 cases, 64 per family, suite hash `387be0e729e114b5071937f9e7e60c3ffece665df07d7f6c925f64c18bc2ac6e`.
- Blind suite: 72 cases, eight per family, suite hash `d41ec90bbf7f7448b39e8a706c8e9577d1fbc99bcf9b8e64dba4095d9068fb6b`.
- Every suite record freezes its manifest and all five RTL hashes. Blind selection uses pairwise topology coverage with a V2-calibration-only opportunity-propensity tie-breaker.
- Added the sparse adder widths/counts and priority widths/request counts from the plan, including dynamically generated 20-request RTL.
- Calibration validation: 576/576 passed Verilator lint, `v1`–`v3` equivalence, required `n0` inequivalence, and `v0`–`v3` Nangate45 synthesis.
- Blind prevalidation: 72/72 passed the same lint/formal contract with synthesis disabled. The 20-request held-out priority case also passed all proof expectations.
- No V2.1 blind synthesis summary exists, and no V2.1 lock or unseal record exists.

### Feature and rule repair

- Added a V2.1 feature schema that elaborates `kernel_top` only. Across all 2,808 training rows, `module_count` is exactly one and `register_count` is exactly zero.
- Added PySlang AST facts alongside Yosys graph facts, including equality/inequality-to-zero, relational, conditional, if, and case counts.
- Added cast-aware zero recognition so `$signed('0)` and `$unsigned('0)` remain visible.
- Added the conservative equality-to-zero comparator-selection rule and recovered the frozen V2 miss plus the signed V2.1 cases.
- Only the 39 intentional zero-excess variable-shift boundary cases use registered training-only topology context; comparator fallbacks are eliminated.
- Added family-specific median/IQR nearest-neighbor OOD models with leave-one-topology-out 95th-percentile thresholds and auditable nearest-topology/contributing-feature evidence.

### V2.1 models and calibration gate

- Recomputed kernel-only features for all 360 V2 calibration cases and combined them with all 576 V2.1 calibration cases.
- Training table: 2,808 candidate rows across 936 topology groups; combined training hash `c01424dd880679244681dd0e1618deecd618dd756cc6625a4c032846e8b44e4e`.
- Implemented three 500-tree RF regressors, three balanced 500-tree direction classifiers, and one 500-tree eligibility classifier, with grouped out-of-fold policy selection and final refits.
- Direction calibration passes at full coverage: 80.9% delay, 81.1% area, and 84.5% cell-count accuracy.
- The risk policy is not feasible under the frozen joint constraints. Its safest retained frontier uses threshold `0.9117502856445018`, achieves 8.3% opportunity recall, 99.9% abstention specificity, 5.0% harmful recommendations, and 54.1% balanced actionable accuracy.
- Model summary status is `calibration_gate_failed`. The model is explicitly diagnostic-only; live V2.1 analysis and blind-lock creation reject it.
- Calibration report: `artifacts/models/v21/calibration-report.md` with hash `a0c9db6ba81e3cf8ea5e3bd0befa5c8c7c14aff73308acbbb4df5957f79d7b8d`.
- Calibration-only family diagnosis is stored in `artifacts/models/v21/risk-diagnostics.md` with hash `90688b69f7b98ae0d98b5fa9cc7756caec5fecc3871e1144e3d2c69c99ad753b`. Popcount reaches 90.6% safe recall and priority selection reaches 54.9%, while most other families cannot yet provide useful safe coverage. The next-version target is therefore family-aware selective-risk calibration, not replacement of the already viable direction stack.

### Physical cross-check and benchmark implementation

- Added OpenROAD plan, immutable lock, resumable runner, audited manual retry, metrics parser, report, and physical-gate commands.
- Locked official image ID `sha256:adea233b85997ff0b16809c43a41a6bf0ec6b0998c64c98dca2703024a35d514`, ORFS commit `036d106273e66855cd5214d49518fd0f0df7de61`, and OpenROAD lock hash `beba77be31ee7d8e6b4d979985751889799dab32467495f08a3c6e2cb440c21b`.
- The full 108-run, two-worker pilot completed as a fresh, no-retry pass. Of 108 runs, 104 are usable and four are unusable; all usable runs completed routing with finite timing and zero DRC violations.
- The four unusable runs are all variants of one `popcount_saturation` case, `v2_19cd06c4775717d7`. Its 1,178 I/O pins exceed the 700 positions available on the locked 100 µm die, so all four variants stop at I/O placement. An audited retry was not run because the same locked geometry would deterministically fail again.
- The frozen physical-evidence gate passes with 26/27 complete cases (minimum 24), including three complete cases in every family except `popcount_saturation`, which has the required two. Candidate-action agreement is 80.77% (minimum 80%); delay, area, and cell-count direction agreement are 79.49%, 83.33%, and 83.33%, respectively (minimum 75% each).
- The physical report is `artifacts/openroad/v2/report.md`; its semantic report hash is `6dcb060959df8e41f1c919fe794b5631f6d3b055759febab88e1fe16692f0643`. The JSON artifact SHA-256 is `3b1c65cd3aad92adffbc369df608902316608b2487619e8744b45e69897c6d34`.
- Four records from an accidentally overlapping superseded-lock launch were preserved under `artifacts/openroad/v2/superseded-overlap-20260714` and are excluded by lock-hash enforcement.
- Implemented V2.1 point, risk, and safe-advisor inference; immutable bundle/OOD/metadata validation; candidate emission; non-authoritative Codex explanations; and hard failure preservation without decision override.
- Implemented the exact 480-record/264-call V2.1 runner, corrected micro/macro/direction/tie/regret metrics, paired bootstrap, stability/reliability/emission checks, physical-gate dependency, and all promotion gates.
- Blind locking and execution remain correctly blocked by the failed calibration policy even though the physical gate passes. No V2.1 blind lock, synthesis summary, unseal record, or benchmark result was created.
- Fixed a report-path defect found only after the pilot: the OpenROAD report now uses the frozen V2 ±1% neutral-band direction classifier. A report-construction regression test covers this path.
- The expanded repository regression contains 129 tests and passes in full after the V2.1 and physical-report changes.

## V2.2 family-aware selective-risk recovery

The policy-only V2.2 contract is frozen in `implementation plan/v2.2.md` with
SHA-256 `d82073eb045ab6b9ae87e21a59d3339d6b58b08981d4af51994b792d9a3d467c`.
V2.2 preserves the passing V2.1 feature, rule, regression, direction, OOD,
ranking, formal, and physical stacks and changes only eligibility calibration.

### Frozen inputs and implementation

- Hash-locked the 2,808 V2.1 calibration rows, 2,808 aligned grouped-OOF predictions, V2.1 prediction bundle/metadata/OOD/policy, and the passing OpenROAD report. The resulting V2.2 input-lock hash is `287a72227fa4b941861b1d0156bd98f7362284e85d8f79f42c4328d859e084de`.
- Implemented one 500-tree grouped-OOF random-forest eligibility classifier per supported family with seed `20260716`, maximum depth 12, minimum leaf size 3, square-root feature sampling, balanced-subsample weights, and five topology-group folds.
- Frozen a ten-opportunity support floor. `arithmetic_resource_sharing` has four opportunities, `comparator_selection` has nine, and `variable_shift` has zero, so those families use constant abstention rather than unstable classifiers.
- Implemented complete per-family threshold frontiers and an exact hierarchical SciPy/HiGHS MILP optimizer. It selects one threshold per family while enforcing global harmful rate at most 5%, global specificity at least 90%, balanced actionable accuracy at least 70%, supported-family specificity at least 80%, and the registered family harmful-rate guard.
- Implemented versioned `model train-v22`, `analyze-v22`, and `benchmark lock-v22` paths; hash-verified bundle loading; unsupported-family abstention; and a hard diagnostic-only guard for both live analysis and blind locking.

### V2.2 calibration outcome

- Calibration status: **FAIL**. The exact full constrained problem is infeasible, so the optimizer retained the frozen safety frontier rather than loosening a gate.
- Safety frontier: 37.39% opportunity recall (86/230), 99.43% abstention specificity (702/706), 4.44% harmful recommendations (4/90), and 68.41% balanced actionable accuracy.
- Every safety and family constraint passes. Only the 70% balanced actionable-accuracy gate fails.
- Relative to V2.1, opportunity recall improves from 8.3% to 37.4%, harmful rate improves from 5.0% to 4.4%, and balanced actionable accuracy improves from 54.1% to 68.4%—a 14.3 percentage-point gain.
- With specificity fixed at 99.43%, V2.2 needs eight additional correctly covered opportunity cases to clear the 70% balanced gate.
- `popcount_saturation` reaches 100% safe recall, 100% specificity, and 0% harmful recommendations. `priority_selection` reaches 62.0% recall, 87.9% specificity, and 8.3% harmful recommendations. The remaining supported families are still coverage-limited.
- V2.2 policy hash: `656ea85626aa19174a16e0ebd4c02f71471d285557a823b915f5de8ab2676bfa`.
- V2.2 metadata hash: `a6dfc3e561def117b7a1947f52d70799a138548904b482d82304f877d2daf42e`.
- Calibration report: `artifacts/models/v22/calibration-report.md`, semantic report hash `0c5569423c741ba51112cebe0b1c0aa9b491770e22e9fd3e8ff956951962aa2f`.

### Blind and regression status

- A real `benchmark lock-v22` attempt was rejected because the calibration gate failed.
- No `heldout-v22` suite, V2.2 benchmark lock, blind synthesis summary, unseal record, or benchmark result was created.
- The expanded repository regression contains 137 tests and passes in full after the V2.2 changes.

## V2.2 failure diagnosis and V2.3 recovery plan

### Reproducible failure diagnostic

- Added `rtl-advisor benchmark diagnose-v22`, which hash-verifies the frozen
  V2.1/V2.2 calibration inputs, rejects non-calibration or blind-labeled rows,
  reproduces the V2.2 aggregate metrics, and classifies every one of the 936
  reference cases.
- The diagnostic confirms 230 opportunity cases, 86 covered opportunities, 144
  missed opportunities, 90 recommendations, and four harmful recommendations.
- All 86 covered opportunities selected a measured-best candidate. Substituting
  measured-oracle ranking at the frozen thresholds recovers zero additional
  opportunities, so candidate ranking is not the V2.2 bottleneck.
- The 144 misses decompose into 131 cases where no candidate clears the family
  threshold and 13 cases in unsupported families. There are no
  `ranking_selected_ineligible` or `covered_suboptimal` cases.
- Oracle thresholding can cover at most 217 opportunities in the currently
  supported families. The 13 unsupported opportunities comprise four
  arithmetic-resource-sharing and nine comparator-selection cases.
- A leave-one-topology-out OOD overlay rejects only three of the 86 covered
  recommendations, leaving 83 safe covered opportunities. OOD is therefore a
  secondary guard rather than the primary coverage loss.
- Safe-best candidates account for 358 of 2,808 rows (12.7%). Measured
  eligibility accounts for 391 rows.
- Missed opportunities are not clustered immediately below the thresholds:
  zero are within 0.01, ten are within 0.05, and 34 are within 0.10. Simple
  threshold relaxation cannot supply the required recovery while preserving the
  frozen risk constraints.

### Per-family score separation

| Family | Case ROC AUC | Case AP | Candidate ROC AUC | Candidate AP |
|---|---:|---:|---:|---:|
| adder_reduction_association | 0.795 | 0.762 | 0.804 | 0.576 |
| arithmetic_resource_sharing | 0.500 | 0.038 | 0.500 | 0.013 |
| comparator_selection | 0.500 | 0.087 | 0.500 | 0.087 |
| decode_factoring | 0.908 | 0.619 | 0.928 | 0.600 |
| mux_placement | 0.819 | 0.774 | 0.792 | 0.669 |
| popcount_saturation | 1.000 | 1.000 | 1.000 | 1.000 |
| priority_selection | 0.846 | 0.922 | 0.957 | 0.904 |
| variable_shift | n/a | n/a | n/a | n/a |
| width_signedness | 0.920 | 0.498 | 0.919 | 0.418 |

- Diagnostic report: `artifacts/models/v22/failure-diagnostics.md`.
- Diagnostic semantic hash:
  `c12ff14887e946054650564adb050270f7d7c6d7bf053ddb992f0032f1a6027f`.
- Diagnostic JSON SHA-256:
  `c3c5289bd98f939b62e54e8dc909663f1ce3e9f5034fa69c1e092c73c2526d81`.
- Diagnostic Markdown SHA-256:
  `bfad98a42191dd46b846b9a41c046fb1aabfc1eb0da6cd7af3fc666e6c96fbba`.

### Frozen V2.3 direction

- The V2.3 implementation contract is frozen in `implementation plan/v2.3.md`
  with SHA-256
  `7f834b20f0742ebb63db82ff6a42629e7f4c4142374c2c21e2de61519764b356`.
- V2.3 targets score separation and support while retaining the V2.1 PPA,
  direction, ranking, candidate, formal, and Codex boundaries and the V2.2 risk
  constraints.
- The plan adds exactly 384 generated calibration cases across the seven
  families that need recovery. It adds no calibration cases for the already
  perfect popcount family or the zero-opportunity variable-shift family.
- The new family eligibility models append leakage-checked, pre-synthesis PPA
  predictions, utility/margin features, direction confidences, and within-case
  relative predictions to the existing kernel-only features.
- Targeted expansion cases supply training support, but the calibration gate is
  still measured on the identical 936-case reference distribution. This keeps
  V2.3 metrics directly comparable with V2.2 and prevents an enriched training
  mix from inflating the gate.
- V2.3 must cover at least 94 reference opportunities while retaining all V2.2
  safety gates. If calibration passes, a maximum 56-run OpenROAD delta audit is
  required before a new 72-case held-out suite may be generated.
- The repository regression now contains 141 tests and passes in full after the
  V2.2 diagnostic command, case taxonomy, ranking diagnostics, and documentation
  changes.
