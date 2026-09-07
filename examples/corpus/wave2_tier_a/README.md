# Wave 2 Tier A frozen tranche

This directory pre-registers the first 12 module lineages before any candidate
synthesis result is inspected. The lock fixes ordering, upstream revisions,
license and archive hashes, primary source hashes, categories, proof levels,
and intended variant families.

The three downloaded source trees remain under ignored `corpus/upstream/` and
are not redistributed by this repository. A `conditional` license disposition
means the open-source license was identified and hashed for local research; it
does not represent company legal approval for product redistribution.

References may finish Wave 2 as qualified, rejected, or blocked. A failed or
neutral result stays in the tranche and is never silently replaced with a more
favorable design.

## Final gate result

Wave 2 qualified 8 of 12 references and explicitly blocked four:

- Qualified: OpenTitan `prim_arbiter_ppc`; PULP `spill_register`,
  `stream_register`, `fifo_v3`, and `rr_arb_tree`; verilog-axis
  `axis_register`, `axis_pipeline_register`, and `axis_pipeline_fifo`.
- Blocked: OpenTitan `prim_fifo_sync`, `prim_count`, and `prim_packer_fifo`
  because their FPV dependency graphs were not reproduced; PULP `stream_mux`
  because no independent behavior test or property set was reproduced.

All 12 build, lint, and complete both pinned baseline-only Yosys/ABC recipes.
Behavior evidence consists of 30 passing upstream cocotb tests, four passing
PULP property-proof tasks, the OpenTitan P2 arbiter-pair proof, and the
verilog-axis P3 pipeline proof. The frozen behavior plan is
[`behavior.plan.json`](behavior.plan.json); immutable run evidence stays under
the ignored `artifacts/corpus-behavior/` tree.
