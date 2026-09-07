# RTL Advisor Progress Update — July 23, 2026

## Outcome

Realistic RTL Evidence Slice V1 is complete. RTL Advisor now supports a
registry-selected curated OpenTitan arbiter alternative, proves it under a
strengthened same-cycle P2 contract, measures the complete frozen parameter
matrix with two Yosys/ABC recipes, and cross-checks N=8 and N=16 with a pinned
Nangate45 OpenROAD flow.

This is credible evidence for one reference/alternative pair. It does not make
RTL Advisor a general RTL optimizer.

## What was implemented

- Added a transformation registry with versioned transformation, candidate
  origin, proof-level, contract-hash, and registry-hash validation.
- Preserved `adder_reduction_association` and Agent V1 behavior.
- Registered `same_cycle_arbiter_topology` for the pinned OpenTitan
  `prim_arbiter_ppc` and `prim_arbiter_tree` pair.
- Allowed Agent V2 `review` to accept the qualified corpus-reference manifest
  and expose N=1, 4, 8, and 16 as separate findings.
- Made candidate verification select P1 or P2 from the registered contract.
- Kept measurement independent of the proof backend while requiring a current
  hash-linked formal pass.
- Added isolated candidate workspaces, source diffs, and the required
  transformation/reference/configuration/proof/measurement metadata.
- Added a clean-repeat study driver and one complete result publisher.
- Added a versioned OpenROAD M2 driver with:
  - exact pinned ORFS `flow/` materialization from Git objects;
  - Nangate45;
  - a 10 ns clock;
  - fixed die calculation;
  - 35% target placement utilization;
  - complete route, timing, area, and DRC acceptance checks.
- Updated the Codex skill for registered deterministic and curated P1/P2
  candidates.
- Left ML and the read-only dashboard behavior unchanged.

## Formal result

All four valid configurations passed the strengthened P2 contract in both clean
repeats:

- Reset asserted initially, released after one clock, and never reasserted.
- Requests may arrive under arbitrary protocol-legal traffic.
- A request remains asserted until granted.
- Payload remains stable while the request is outstanding.
- `ready_i` is unconstrained.
- `valid_o` and `gnt_o` are compared every cycle.
- `idx_o` and `data_o` are compared whenever output is valid.

The incorrect reset, mask-state, grant, and data-selection controls each
produced a counterexample in both repeats. The older restricted P2 proof remains
in place; the strengthened proof is a new version.

## Measured result

The standard and stronger Yosys/ABC recipes produced the same classification
for every frozen point and reproduced exactly across clean repeats.

| Configuration | Yosys/ABC timing | Yosys/ABC area | Result |
| --- | ---: | ---: | --- |
| N=1 | 0.00% | 0.00% | Synthesis handles |
| N=4 | 14.35% better | 10.57% worse | Reject: area guardrail exceeded |
| N=8 | 21.20% better | 2.38% better | Measured improvement |
| N=16 | 27.43% better | 0.52% better | Measured improvement |

The M2 runs completed route with zero reported DRC violations. Both repeats
produced identical area and delay values.

| Configuration | OpenROAD timing | OpenROAD area | Cross-flow result |
| --- | ---: | ---: | --- |
| N=8 | 29.35% better | 1.69% better | Agrees with M0/M1 |
| N=16 | 0.90% worse | 6.60% better | Neutral for timing; disagrees with M0/M1 |

The final actions are therefore:

- N=1: keep the reference; synthesis handles the difference.
- N=4: reject the alternative because it violates the area guardrail.
- N=8: the alternative may be recommended for this frozen configuration in the
  pinned flows.
- N=16: do not recommend; report cross-flow disagreement.

## Reproducibility

- P2 status, tool identity, and all negative-control outcomes match.
- M0/M1 normalized metrics and canonical netlist hashes match exactly.
- M2 direction matches between repeats.
- M2 baseline and candidate area/delay drift is 0%, below the 2% limit.
- Counterexample waveform hashes are not required to match because a solver may
  emit different valid failing traces.

The authoritative result is:

`artifacts/realistic-rtl-evidence-slice-v1/result.json`

Its semantic hash is:

`d82cfd86f2354862c186aa1a19ea1d3cc486720d9ca8b2cf28f7ea7996c3bf7e`

## Physical-flow correction retained for audit

The first M2 attempt used `remove_from_collection` in its SDC. The pinned
OpenROAD build rejected that command before floorplanning. That failure remains
in the version-1 attempt artifacts; the accepted version-2 recipe names the
arbiter input ports explicitly and was executed twice from clean immutable ORFS
snapshots.

## Claim boundary

Allowed for N=8:

- “Repeatable Yosys/ABC improvement for this configuration.”
- “Confirmed by the pinned OpenROAD cross-check.”

Not allowed:

- Genus or commercial-flow improvement.
- Production timing, area, or power improvement.
- General unseen-RTL optimization.
- A credible `same_cycle_arbiter_topology` family claim.

The family gate remains open because this slice contains only one qualified
pair from one upstream lineage. It still needs ten qualified pairs, three
independent lineages, at least three pre-registered M2 samples, at least 95%
reproducibility, no recommended formal failures or regressions, and at least one
M0/M1/M2-confirmed improvement.

## Validation

- All 376 repository tests pass, including registry, Agent, formal, synthesis,
  OpenROAD, packaging-interface, and dashboard API coverage.
- Plugin skill and plugin manifest validation pass.
- The source plugin is cache-busted as
  `0.2.0-alpha.1+codex.20260723200524`; it was not reinstalled because this
  repository plugin is not present in the currently configured local
  marketplace list.
- The `0.2.0a1` wheel and source distribution build offline.
- The wheel installs outside the repository and imports all new registry,
  realistic-evidence, study, OpenROAD, and result modules.
- The package contains the frontend assets and excludes large evidence,
  third-party RTL, and tool installations.

## Next move

Pre-register nine more same-cycle arbiter reference/alternative pairs across at
least two additional upstream lineages before inspecting candidate PPA. In
parallel, continue the broader Tier A expansion toward 50–100 qualified modules
without counting parameter configurations or variants as new references.

## Arbiter Family Credibility Wave — implementation checkpoint

The next wave is now frozen in
`examples/corpus/arbiter_family_v1/family-study.json`. It contains ten distinct
pairs, four upstream lineages, four configurations per pair, five ordered M2
samples, and three ordered reserves. No new candidate PPA has been inspected.

Implemented at this checkpoint:

- Pinned BaseJump STL to
  `b48037e28544425839dbd617d45b1a82631bc1a9`.
- Recorded BaseJump license SHA-256
  `2e77070466e0c6d653e29ae4dfbc1a549cf1aa0b320237de0421db176dff87c0`.
- Added a generic transformation-executor registry and routed the completed
  OpenTitan Agent V2 flow through it.
- Registered executor IDs/versions for all ten frozen references.
- Added `rtl-advisor-family-study-v1` and
  `rtl-advisor-family-evidence-v1`.
- Added cohort-order, duplicate-pair, pre-PPA visibility, registry-staleness,
  repeatability, family-gate, and product-promotion tests.
- Added `rtl-advisor study validate/run/report`.
- Implemented isolated P1 source candidates for OpenTitan
  `prim_arbiter_fixed` and BaseJump `bsg_arb_fixed`.
- Confirmed both candidates and upstream baselines pass local Verilator lint.
- Preserved honest `formal_inconclusive` status under native Yosys, whose
  frontend cannot elaborate the upstream SystemVerilog constructs. The pinned
  yosys-slang container gate remains required.

The first pre-PPA study checkpoint verified all ten primary source/license
hashes, found all ten executors, and recorded three implemented candidates with
seven still awaiting P2 qualification. Its status is
`candidate_qualification_pending`; its semantic hash is
`7b6b2fc6c264edff37145e2bc96a16db1c37123b3a9c80b47019a650ab8ee34c`.

The seven new P2 pair-specific candidates and formal miters are not complete.
Their executors fail closed before measurement. Docker Desktop is currently
unreachable, so no pinned-container formal or OpenROAD work has been claimed at
this checkpoint.

### Later pre-PPA candidate checkpoint

Three of the seven new P2 paths now have deterministic isolated candidates,
pair-specific same-cycle miters, and incorrect controls:

- PULP `rr_arb_tree`.
- PULP `stream_arbiter_flushable`.
- verilog-axis `arbiter`.

All three baseline/candidate pairs are locally lint-clean after recording a
narrow waiver for the pinned verilog-axis baseline's existing width warnings.
The candidate does not inherit those warnings. The focused family execution
suite now has six passing tests. No PPA was made visible, and none of these
paths is marked safe until the pinned SBY/yosys-slang proofs and controls pass.
