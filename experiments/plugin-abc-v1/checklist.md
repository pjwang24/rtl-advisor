# RTL Advisor plugin A/B/C checklist

## Completed

- [x] Freeze the A/B/C protocol, metrics, guardrails, and product decision rule.
- [x] Freeze 12 held-out generated cases and 12 pinned open-source Tier-A cases.
- [x] Hash-lock the manifest, oracle, schemas, framework, and run packets.
- [x] Validate all 24 source hashes and open-tranche references.
- [x] Compile all generated cases with Yosys and lint them with Verilator.
- [x] Run Arm A — Codex only — twice on GPT-5.6-sol xhigh.
- [x] Run Arm B — RTL Advisor plugin only — twice on GPT-5.6-sol xhigh.
- [x] Run Arm C — hybrid Codex plus plugin — twice on GPT-5.6-sol xhigh.
- [x] Run targeted third-pass tie-breakers for Arm A and Arm C disagreements.
- [x] Independently recheck baseline hashes and rerun generated P1 formal.
- [x] Reject one-recipe generic ABC output as incomplete product measurement.
- [x] Publish the machine-readable scorecard, repeat comparison, tie-break
  resolution, evidence audits, and technical HTML report.
- [x] Confirm zero harmful recommendations and zero source mutations.
- [x] Make the product decision: keep the plugin as an optional bounded
  evidence executor; do not make hybrid the default yet.

## Current result

- Plugin-only: 95.83% validated decision accuracy, 100% evidence completion,
  100% decision/evidence reproducibility, zero harmful recommendations.
- Hybrid: same first-order accuracy and evidence metrics, but only 70.83%
  decision reproducibility and no validated coverage gain over plugin-only.
- Codex-only: broader hypothesis generation, but its generic one-recipe
  measurements did not meet the frozen two-recipe product contract.
- Three released-family candidates passed formal and both pinned recipes; all
  were neutral because synthesis handled them.
- No measured PPA improvement was found.

## Next benchmark work

- [ ] Replace free-form `no_change`, `unsupported`, and `inconclusive` choices
  with a deterministic decision table.
- [ ] Add frozen mux/common-expression and priority/decode transforms with
  negative controls.
- [ ] Connect the existing P2/P3 proof machinery to corpus candidate and
  measurement operations.
- [ ] Freeze a sealed evaluation set containing measured wins, neutral cases,
  regressions, and sequential boundaries.
- [ ] Capture wall time and tool-invocation counts consistently.
- [ ] Re-run A/B/C only after the above contract changes are frozen.
- [ ] Test Yosys/ABC correlation against Genus or another target flow on the
  company-controlled machine.

