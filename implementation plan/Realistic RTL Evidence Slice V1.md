# Realistic RTL Evidence Slice V1

> **Status:** implementation complete; broader family gate remains open  
> **Frozen reference:** OpenTitan `prim_arbiter_ppc`  
> **Frozen alternative:** OpenTitan `prim_arbiter_tree`  
> **Scope:** same-cycle arbitration topology under a P2 sequential proof

## Goal

Build the first credible same-cycle optimization study around a realistic,
stateful open-source module. The study must preserve the interface, registers,
and cycle timing; prove the alternative under an explicit traffic contract;
measure every frozen configuration with identical Yosys/ABC recipes; and
cross-check selected configurations with a pinned OpenROAD flow.

This slice validates the reusable evidence pipeline. It does not establish that
RTL Advisor is a general optimizer.

## Product changes

- Replace adder-only candidate dispatch with a transformation registry.
- Support deterministic rewrites, curated upstream alternatives, and future
  reviewed Codex variants.
- Require each transformation to declare a P1, P2, or P3 proof contract.
- Register:
  - `adder_reduction_association` for compatibility.
  - `same_cycle_arbiter_topology` for the OpenTitan comparison.
- Keep `review → candidate → verify → measure → report`.
- Allow Agent V2 review to accept a qualified corpus-reference manifest.
- Select the formal backend from the candidate proof contract.
- Keep measurement independent of the proof backend while requiring a current,
  hash-linked formal pass.
- Record transformation ID/version, candidate origin, reference and
  configuration IDs, proof level, contract and registry hashes, and
  measurement level.
- Preserve Agent V1 and existing Agent V2 artifact compatibility.
- Keep the legacy singular `transformation` capability and add the registry
  `transformations` collection.
- Leave ML and the dashboard decision path unchanged.

## Frozen study

The reference and alternative come from the same pinned OpenTitan revision.
The objective is timing, with a 3% timing threshold and a 10% area guardrail.

| Configuration | Purpose |
| --- | --- |
| `N=1, DW=32, EnDataPort=1` | Expected neutral control |
| `N=4, DW=32, EnDataPort=1` | Small nontrivial arbiter |
| `N=8, DW=32, EnDataPort=1` | M0/M1 and M2 |
| `N=16, DW=32, EnDataPort=1` | M0/M1 and M2 |

No configuration may be added or removed after measurement results are visible.

## P2 proof contract

- Reset is asserted initially, released after one clock, and never reasserted.
- Requests may arrive under arbitrary protocol-legal traffic.
- An asserted request remains asserted until granted.
- Payload remains stable while its request is outstanding.
- `ready_i` remains unconstrained.
- Compare `valid_o` and `gnt_o` every cycle.
- Compare `idx_o` and `data_o` whenever output is valid.
- Treat reset, mask-state, grant, and data-selection defects as negative
  controls; each must produce a counterexample.

The earlier restricted P2 artifact remains historical evidence. The strengthened
contract is a new version and does not overwrite it.

## Measurement

### M0/M1

- Run the standard and stronger pinned Yosys/ABC recipes for every P2-passing
  configuration.
- Use identical sources, parameters, constraints, tools, libraries, and
  environment for each baseline/candidate pair.
- Publish neutral, improved, regressed, flow-dependent, failed, and
  inconclusive results.

### M2

- Run the pinned Nangate45 OpenROAD flow for N=8 and N=16.
- Use a 10 ns clock, fixed-die calculation, and 35% target utilization.
- Materialize the exact pinned ORFS `flow/` tree from Git objects before each
  clean repeat.
- Require a completed route, zero reported DRC violations, and complete timing
  and area metrics.
- Execute the complete study twice.
- Require exact normalized M0/M1 metrics and netlist hashes.
- Require matching M2 result direction and no more than 2% drift in baseline
  or candidate area/delay.

## Claim gates

- A formal pass means only that the alternative is safe under the recorded
  contract.
- Neutral M0/M1 means the tested synthesis recipes handled the difference.
- Improvement in M0 and M1 permits: “repeatable Yosys/ABC improvement for this
  configuration.”
- Matching M2 direction permits: “confirmed by the pinned OpenROAD
  cross-check.”
- M2 disagreement remains a published flow-dependent result and blocks the
  cross-flow claim for that configuration.
- No result is Genus, target-flow, production-PPA, or unseen-RTL evidence.
- The experiment completes honestly even with zero improvements.

The transformation family is not credible until it has ten qualified
reference/variant pairs, three independent upstream lineages, three
pre-registered M2 samples, zero recommended formal failures or regressions, at
least 95% reproducibility, and at least one M0/M1/M2-confirmed improvement.
General optimization claims remain blocked until multiple transformation
families and the 50–100-module Tier A gate are complete.

## Acceptance checklist

### Registry and artifacts

- [x] Register the legacy adder and same-cycle arbiter transformations.
- [x] Validate duplicate IDs, proof levels, candidate origins, contract hashes,
  and registry hashes.
- [x] Preserve Agent V1 and legacy Agent V2 fields.
- [x] Add the required transformation, reference, proof, and measurement
  metadata to new artifacts.

### Candidate and proof

- [x] Load the qualified frozen OpenTitan reference.
- [x] Copy candidate sources only into isolated artifact workspaces.
- [x] Leave the upstream checkout byte-identical.
- [x] Select P2 from the registered transformation contract.
- [x] Implement the strengthened P2 assumptions and comparisons.
- [x] Pass all valid configurations.
- [x] Require all four incorrect controls to produce counterexamples.
- [x] Block measurement after P2 failure or inconclusive status.

### Synthesis and physical evidence

- [x] Run M0/M1 for all four frozen configurations.
- [x] Repeat M0/M1 from clean pinned containers.
- [x] Require exact normalized result reproduction.
- [x] Complete M2 for N=8 and N=16 twice.
- [x] Require M2 direction reproduction and ≤2% metric drift.
- [x] Publish the complete frozen matrix without selecting favorable points.

### Interfaces and release quality

- [x] Keep existing Agent commands and expose qualified-reference findings.
- [x] Keep the dashboard read-only and compatible with the extended records.
- [x] Update the Codex skill after the CLI/artifact contract is final.
- [x] Pass the full repository test suite and packaging checks.
- [x] Update the authoritative project checklist and progress report.

## Expected intervention

User intervention is needed only if Docker Desktop stops, the pinned tool image
or source snapshot becomes unavailable, the project license must be confirmed,
or a final GitHub push/release is requested.
