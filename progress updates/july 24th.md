# RTL Advisor Progress Update — July 24, 2026

## Outcome

The Arbiter Family Credibility Wave V1 study is complete. The family remains
`study_only`: the usable M2 samples reproduced cleanly across the two frozen
repeats, but the selected OpenTitan, PULP, and BaseJump M2 configurations
regressed under the frozen Yosys/ABC and OpenROAD flows, and the verilog-axis
`axis_arb_mux` sample remains blocked by the same clock-port adapter mismatch in
both repeats.

## What changed

- Finished the repeat-2 M2 run and collected the frozen summary.
- Confirmed repeat-1 and repeat-2 match on the usable M2 cases.
- Kept the verilog-axis `axis_arb_mux` sample honestly blocked instead of
  forcing a misleading pass.
- Updated the wave implementation plan and the authoritative project checklist
  to reflect the completed study.

## Evidence summary

- Formal coverage remains intact for all ten frozen pairs.
- The frozen M2 cohort produced four repeatable outcomes:
  - OpenTitan fixed priority: regression.
  - PULP `rr_arb_tree`: regression.
  - verilog-axis `axis_arb_mux`: blocked because the wrapper still does not
    resolve the expected clock port.
  - BaseJump round-robin: regression.
- The two M2 repeats reproduced exactly on the usable cases.

## Claim boundary

The study does not support a product-preview recommendation. The family gate is
closed because the measured configurations do not produce the required pattern
of improvement, and one frozen sample remains blocked rather than measured.

## Next move

If we continue this wave, the next useful step is to fix or replace the
verilog-axis clock-port adapter, then decide whether to extend the family with a
new lineage that has a better chance of producing at least two distinct,
formally safe improvements.
