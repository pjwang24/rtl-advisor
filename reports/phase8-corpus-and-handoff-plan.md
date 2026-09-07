# Phase 8 Corpus and Handoff Plan

> **Status:** selection contract drafted; no new upstream source, tranche, or
> registry mutation is authorized by this document.
>
> **Primary counting unit:** `independent_design_lineage`. Source files,
> parameter configurations, generated alternatives, and variants are reported
> separately and never increase the primary dataset count.

## Outcome

Phase 8 will deliver a clean, reproducible RTL Advisor handoff while expanding
the evidence corpus enough to test the deterministic workflow outside its
current Tier A concentration. It will preserve the thin Codex orchestrator,
versioned deterministic scripts and schemas, explicit authorization stages,
compact output defaults, and immutable hash-addressed evidence established by
Phases 1–7.

Phase 8 is not an ML implementation phase. Its dataset and split discipline
will make a later ML-readiness decision possible, but no model may enter the
live recommendation path unless repository-disjoint held-out evidence exposes
a specific deterministic limitation and demonstrates a safe, repeatable gain
over the deterministic baseline.

## Authoritative baseline

The current local immutable coverage artifact is
`artifacts/corpus-workflow-v1/coverage/coverage-e5300e7da775de6f2acf/coverage.json`.
Its registry snapshot semantic hash is
`28c975bb4eef53c1077e0ab27159455244e8fc5b3051aee3e9d149f1f20b1386`;
the artifact semantic hash is
`dd49a210d309a940fdfe8c54c759991713091b79542f20002669c3ad8bcd75fa`.
The ignored artifact remains local evidence and is not redistributed in the
repository. It reports:

| Measure | Current state | Phase 8 minimum |
| --- | ---: | ---: |
| Independent design lineages | 12 | 20 total candidates after append |
| Active qualified lineages | 8 | 16 |
| Upstream repository lineages | 3 | 6 |
| Tiers represented | A only | A plus representative B and C |
| Dataset splits populated | development only | development, calibration, evaluation, release holdout |
| Missing categories | 4 | 0 |
| Blocked references | 4 | 0 unresolved; each requalified, rejected, or retained with a reviewed terminal disposition |

The Phase 7 and installed-plugin acceptance results remain the performance and
behavior baselines. Corpus growth must not rewrite those artifacts or relax
their release gates.

## Existing blocker disposition

The four blocked Wave 2 references remain excluded from qualified coverage.
Their build success is useful evidence, but it is not a behavioral baseline.
Phase 8 must resolve them without substituting a weaker test after seeing PPA.

| Reference | Current state | Evidence gap | Required terminal disposition |
| --- | --- | --- | --- |
| OpenTitan `prim_fifo_sync` | `build_reproduced`, blocked | Pinned FPV dependency graph was not reproduced | Reproduce the exact pinned FPV core and dependencies with recorded tool/context hashes, or retain/reject it with the failed dependency evidence |
| OpenTitan `prim_count` | `build_reproduced`, blocked | Pinned FPV dependency graph was not reproduced | Same as `prim_fifo_sync`; an ad hoc replacement test cannot silently become the upstream baseline |
| OpenTitan `prim_packer_fifo` | `build_reproduced`, blocked | Pinned FPV dependency graph was not reproduced | Same as `prim_fifo_sync`; preserve the original frozen source and failed attempt |
| PULP `stream_mux` | `build_reproduced`, blocked | `Bender.yml` identifies the compile context but no independent behavior test or property set was reproduced | Reproduce upstream behavioral evidence or freeze a reviewed specification-derived P1 property suite with mutation controls; otherwise retain/reject it as blocked |

Requalification is an append-only registry operation and requires an explicit
engineer request plus authorized local upstream roots. Until that happens, the
formal disposition is **carry forward as blocked and do not count toward the
qualified target**.

## Expansion tranche contract

Freeze a tranche of 10–12 candidates before candidate proof or synthesis
outcomes are inspected. This provides room for honest blockers while requiring
at least eight new active qualified lineages.

The tranche must:

- add at least three repository lineages not present in Wave 2;
- cover all four current metadata gaps: `decode_address_routing`,
  `memory_request_tracking`, `processor_accelerator_stage`, and
  `generated_hierarchy_cdc`;
- include at least two complete Tier B IP contexts and two Tier C subsystem
  contexts rather than relabeling standalone modules;
- include exact revisions, archive and license hashes, complete file order,
  generated inputs, parameters, clocks, resets, black boxes, and tool versions;
- declare behavioral and proof contracts before candidate PPA is visible;
- retain blocked, neutral, and regressed results without replacement; and
- keep all third-party RTL and large evidence outside the wheel and plugin.

The existing discovery list in Corpus Strategy V1 is the candidate pool, not
registration approval. A practical review slate is:

| Candidate source family | Intended tier | Primary coverage purpose | Proposed split role |
| --- | --- | --- | --- |
| PULP AXI complete cut/router/interconnect contexts | B | Decode/routing, bus adapters, hierarchy | calibration |
| lowRISC Ibex frozen pipeline/load-store context | C | Processor stages and memory request tracking | evaluation |
| OpenHW CV32E40P frozen pipeline context | C | Independent processor-stage lineage | release holdout |
| OpenHW CVFPU/FPnew frozen execution context | C | Independent accelerator-stage lineage | release holdout |
| A reviewed CDC/generated-hierarchy family from an approved source | A or B | Generated hierarchy, reset/clock/CDC boundary | development or a repository-isolated holdout |

BaseJump STL already participates in the separate frozen arbiter-family study
and may be considered as an additional repository lineage, but copying those
records into Corpus Registry V1 still requires a new frozen tranche and must
not be presented as unseen held-out evidence.

## Split isolation

Split assignment happens before proof and PPA. Corpus Registry V1 already
rejects a repository lineage or containing design that crosses assigned
splits; Phase 8 will use that validation as a release gate.

Rules:

1. Preserve all current Wave 2 references and their variants in
   `development`.
2. Assign every new repository lineage wholly to one of `calibration`,
   `evaluation`, or `release_holdout`; no repository may straddle roles.
3. Keep every design lineage, parameter family, generated family, containing
   IP/subsystem, and all variants with its parent split.
4. Use calibration only for thresholds and report phrasing. Do not tune on
   evaluation or release-holdout outcomes.
5. Open the release holdout only after workflow, proof, classification, and
   report logic are frozen. Any post-holdout logic change requires a new
   holdout or an explicit non-held-out label.

## Qualification and evidence sequence

For an explicitly approved tranche:

1. Freeze the source and selection lock before candidate PPA is available.
2. Run deterministic `corpus validate` against the lock.
3. Append the validated lock with explicit `corpus register` authorization.
4. Run deterministic `corpus qualify` with authorized local roots and
   candidate synthesis disabled.
5. Stop on hash, schema, source, command, or exit-code disagreement.
6. Establish the declared behavioral baseline and mutation controls.
7. Only then authorize candidate creation, proof, and pinned measurement as
   separate stages.
8. Publish compact coverage and terminal-result digests; load full logs only
   to diagnose named failures.

Qualification proves source integrity and compile context. It does not prove a
candidate correct or establish PPA value.

## Clean handoff definition

The Phase 7 MVP can be handed off after its pull request is merged and tagged.
The broader Phase 8 handoff is complete only when one documented command can
produce a hash-validated handoff manifest containing:

- release commit, package/plugin version, and installed plugin identity;
- corpus snapshot hash and coverage summary;
- tranche locks, source/license provenance, split assignment, and leakage
  validation;
- blocker dispositions and qualified-reference counts;
- release-critical test and deterministic parity results;
- Phase 7-method token, uncached-token, latency, completion, agreement, and
  quality comparisons;
- immutable artifact paths and reproduction commands;
- known limitations and unsupported proof/PPA claims; and
- an ML-readiness decision with the deterministic baseline retained.

The command must be read-only with respect to frozen evidence. It may create a
new content-addressed handoff manifest, but it must fail rather than overwrite
or reinterpret an existing artifact.

## Release gates

Phase 8 is accepted only when all of the following are true:

- at least 16 active qualified independent design lineages are present across
  at least six repository lineages;
- the four current category gaps are represented by qualified references;
- Tier B and Tier C compile contexts are qualified without counting their
  child files as independent references;
- every blocked Wave 2 reference has a reviewed terminal disposition;
- registry validation proves repository and containing-design split isolation;
- release-critical tests and installed deterministic transport parity pass;
- the Phase 7 methodology reports no unexplained correctness, authorization,
  completion, frozen-agreement, repeat-agreement, token, or latency regression;
- old immutable evidence hashes still match; and
- the handoff manifest reconstructs on a clean checkout with documented local
  evidence prerequisites.

## Authorization checkpoint

The next mutation requires the engineer to approve a concrete source slate,
exact revisions, licenses, split assignment, and local upstream roots. Before
that approval, permitted work is limited to read-only audits, source-selection
review, schema/test preparation, and the handoff verifier. No tranche may be
registered or qualified from this planning document alone.
