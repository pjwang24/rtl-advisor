# RTL Advisor Project Checklist V1

> **Last reviewed:** August 31, 2026
> **Controlling plans:** [Project Roadmap V1](project%20roadmap%20v1.md) and
> [Corpus Strategy V1](corpus%20strategy%20v1.md)
> **Current wave:** Wave 3 — Arbiter Family Credibility Wave V1
> **Current product status:** Developer preview; not ready for production RTL
> recommendations

## How to maintain this checklist

- `[x]` means the item is implemented and has durable evidence in the
  repository.
- `[ ]` means the item is not complete. An annotation may identify it as the
  current task, blocked, deferred, or requiring external approval.
- Update this file in the same change that completes a listed item.
- Link the implementation, test, evidence artifact, or progress entry before
  checking an item.
- Count only qualified reference design units toward Tier A–D. Dependencies,
  generated cases, parameter variants, rewrites, forks, and vendored copies do
  not inflate those totals.
- A failed or neutral experiment may complete an evidence task. It does not
  count as a successful optimization.
- Never convert `formal_inconclusive`, simulation-only evidence, or an
  unsupported proof scope into a formal pass.

## Project snapshot

| Item | Current state |
| --- | --- |
| Completed vertical slice | Generated combinational flow plus one realistic P2/M0/M1/M2 OpenTitan arbiter study |
| Generated result | Formal pass; both pinned Yosys/ABC recipes neutral (`synthesis_handles`) |
| Qualified open references | Tier A: 8, Tier B: 0, Tier C: 0, Tier D: 0 |
| First Tier A tranche | 12/12 terminal: 8 qualified and 4 explicitly blocked |
| Live recommendation authority | Registered deterministic rules and curated alternatives only; ML remains diagnostic-only |
| Formal capability | P1 combinational, P2 same-cycle sequential, and first bounded P3 transaction proof operational |
| Next missing proof capability | P4 partitioned subsystem proof |
| Immediate implementation | Implement and formally qualify the frozen ten-pair arbiter family before exposing candidate PPA |
| Source downloads | Four pinned upstream snapshots acquired and hash-verified; not redistributed |
| Dashboard | Category-first generated-evidence library, read-only run viewer, family-study PPA explorer, and secondary research view complete; broader qualified-corpus landing adapter pending |

## 1. Direction and governance

- [x] Freeze the completed narrow developer preview in
  [MVP V1](MVP%20V1.md).
- [x] Define the long-range module → IP → subsystem → SoC program in
  [Project Roadmap V1](project%20roadmap%20v1.md).
- [x] Define Tier A–D admission, counting, proof, measurement, and split rules
  in [Corpus Strategy V1](corpus%20strategy%20v1.md).
- [x] Separate qualified references from variants and negative controls.
- [x] Make the dashboard, Codex plugin, ML research, and older implementation
  plans subordinate to the project roadmap where they conflict.
- [x] Record the project-scale reset in
  [July 19 progress](../progress%20updates/july%2019th.md).
- [x] Create this authoritative checklist.
- [x] Review and update this checklist at every completed wave through Wave 2.

## 2. Completed MVP vertical slice

- [x] Preserve Agent V1 behavior while adding explicit Agent V2 operations.
- [x] Implement a source-span rewriter for the supported unsigned, fixed-width
  addition-chain family.
- [x] Keep the upstream/input source byte-identical and edit an isolated copy.
- [x] Record source-linked findings, diffs, hashes, and append-only stage
  artifacts.
- [x] Lint the candidate in the baseline compile context.
- [x] Run direct two-state Yosys RTL-to-RTL combinational equivalence.
- [x] Require deliberately incorrect combinational candidates to fail formal.
- [x] Gate measurement on a current `formal_passed` artifact.
- [x] Run identical baseline/candidate standard and stronger Yosys/ABC recipes.
- [x] Classify measured improvement, synthesis handling, flow disagreement, and
  regression without hiding neutral or harmful outcomes.
- [x] Provide the CLI, Codex plugin orchestration, immutable JSON/static HTML,
  read-only run API, and run viewer.
- [x] Build and install the `0.2.0a1` wheel and source distribution outside the
  repository.
- [x] Pass the pinned offline container integration flow and 304-test repository
  regression recorded in [July 19 progress](../progress%20updates/july%2019th.md).
- [x] Publish the generated result as neutral rather than claiming an
  optimization.
- [x] Stop the pre-registered narrow open pilot at 0/2 qualifying modules
  without benchmark shopping.

## 3. Wave 0 — Strategic reset

- [x] Establish the Tier A target of 50–100 standalone modules.
- [x] Establish the Tier B target of 15–30 complete IP blocks.
- [x] Establish the Tier C target of 5–10 processor or accelerator subsystems.
- [x] Establish the Tier D target of 2–4 complete SoCs.
- [x] Define ten recognizable engineering categories.
- [x] Define P0–P6 proof levels.
- [x] Define M0–M4 measurement levels.
- [x] Record an initial upstream discovery set without treating it as admitted
  corpus data.
- [x] Keep all source downloads and PPA inspection outside Wave 0.

## 4. Wave 1 — Corpus Registry V1

> **Status: complete; 320-test full regression passed**

### Schemas and storage

- [x] Implement `CorpusReferenceManifest v1` for Tier A–D references.
- [x] Implement `CorpusVariantManifest v1` with parent and lineage identity.
- [x] Implement a versioned proof-contract record with P0–P6 scope.
- [x] Record upstream URL, exact revision, license, attribution, and content
  hashes.
- [x] Record complete source/filelist, include, define, parameter, generated
  input, clock, reset, memory, and black-box context.
- [x] Record source, filelist, include-tree, generated-input, and normalized
  compile-context hashes before permitting `source_pinned` status.
- [x] Record upstream tests, reference model, assertions, specifications, and
  known failures.
- [x] Add deterministic semantic hashing and append-only registry storage in
  [corpus_registry.py](../src/rtl_advisor/corpus_registry.py).
- [x] Publish strict reference, variant, and proof
  [JSON schemas](../src/rtl_advisor/schemas/) in the installable package.
- [x] Preserve existing `PilotManifest v1` and stored MVP run compatibility.

### Qualification state machine

- [x] Implement `discovered`.
- [x] Implement `license_reviewed`.
- [x] Implement `source_pinned`.
- [x] Implement `build_reproduced`.
- [x] Implement `behavior_baselined`.
- [x] Implement `reference_qualified`.
- [x] Implement `variant_eligible`.
- [x] Require explicit rejection or blocked reasons without deleting the
  attempted record.

### CLI and reporting

- [x] Add `rtl-advisor corpus add`.
- [x] Add `rtl-advisor corpus validate`.
- [x] Add `rtl-advisor corpus list`.
- [x] Add `rtl-advisor corpus summary`.
- [x] Report coverage by tier, category, upstream lineage, license, proof level,
  and qualification status.
- [x] Keep third-party RTL and large evidence outside the wheel and plugin.

### Safety and acceptance tests

- [x] Reject raw file-count inflation: a multi-file reference counts once.
- [x] Reject duplicate record IDs and declared design/variant lineages.
- [ ] Add source-content similarity detection for undeclared fork, rename, and
  vendored aliases after Wave 2 source acquisition.
- [x] Reject mutable or missing upstream revisions at `source_pinned` and later
  states.
- [x] Reject incomplete license and attribution records at
  `license_reviewed` and later states.
- [x] Reject incomplete pinned dependency-hash contexts.
- [x] Prevent a reference and its variants/configurations from crossing a data
  split boundary.
- [x] Add metadata-only
  [OpenTitan arbiter-pair fixtures](../examples/corpus/opentitan_arbiter_pair/).
- [x] Reconstruct registry fixtures identically in a clean temporary workspace.
- [x] Preserve all existing 304 tests, add 16 registry/schema/CLI tests, and
  pass the complete 320-test regression.

## 5. Wave 2 — First Tier A tranche and sequential proof

> **Status: complete; 8/12 qualified and four explicitly blocked**

### Source freeze and qualification

- [x] Pre-register exactly 12 Tier A candidates before variant PPA inspection.
- [x] Cover three upstream repositories and six categories.
- [x] Obtain approval for the required pinned source downloads.
- [x] Record license disposition before copying or modifying RTL.
- [x] Pin revisions and source/license hashes.
- [x] Reproduce build/lint for all 12, 30 upstream tests for three references,
  formal property checks for four references, and baseline synthesis for all 12.
- [x] End every candidate as qualified, rejected, or explicitly blocked.
- [x] Qualify 8/12 references, including seven stateful modules and two
  documented equivalent implementation pairs.

### Proof foundation

- [x] Add clock, reset, parameter, and formal-assumption support.
- [x] Integrate an independently reviewed equivalent flow for P2
  same-latency sequential equivalence.
- [x] Add state, reset, grant, drop, duplicate, and ordering negative controls.
- [x] Build the first P3 ready/valid transaction-equivalence prototype.
- [x] Invalidate proofs when source, configuration, assumptions, or tools
  change.

## 6. Wave 3 — Complete Tier A

> **Status: current; tranche and transformation-family freeze is next**

### Realistic RTL Evidence Slice V1

- [x] Add a versioned transformation registry for deterministic rewrites,
  curated upstream alternatives, and future reviewed Codex candidates.
- [x] Preserve the legacy adder transformation and Agent V1/V2 compatibility.
- [x] Register `same_cycle_arbiter_topology` for the frozen OpenTitan
  `prim_arbiter_ppc`/`prim_arbiter_tree` pair.
- [x] Freeze N=1, 4, 8, and 16 before candidate measurement.
- [x] Strengthen P2 with initial reset, held requests, stable outstanding
  payloads, unconstrained ready, and valid-qualified payload comparison.
- [x] Pass all four valid P2 configurations twice.
- [x] Require incorrect reset, mask-state, grant, and data-selection controls
  to produce counterexamples.
- [x] Run M0/M1 on every frozen configuration and reproduce normalized metrics
  and netlist hashes exactly.
- [x] Run M2 at N=8 and N=16 twice using the pinned Nangate45 OpenROAD flow,
  10 ns clock, fixed die, and 35% target utilization.
- [x] Reproduce M2 result direction with 0% area/delay drift.
- [x] Publish N=1 neutral, N=4 rejected regression, N=8 confirmed
  improvement, and N=16 cross-flow disagreement.
- [x] Keep the family gate closed at one pair and one lineage.
- [x] Keep ML and the dashboard decision path unchanged.
- [x] Document the implementation and result in
  [Realistic RTL Evidence Slice V1](Realistic%20RTL%20Evidence%20Slice%20V1.md).

### Arbiter Family Credibility Wave V1

- [x] Freeze ten distinct pairs across OpenTitan, PULP, verilog-axis, and
  BaseJump before new candidate PPA.
- [x] Pin BaseJump to
  `b48037e28544425839dbd617d45b1a82631bc1a9` and record source/license hashes.
- [x] Freeze four configurations per pair without counting configurations as
  references.
- [x] Freeze five M2 samples and an ordered pre-PPA fallback list.
- [x] Add the generic executor contract and remove OpenTitan-specific dispatch
  from the completed Agent V2 path.
- [x] Add immutable family-study/evidence contracts, repeat comparison, and
  separate research/product gates.
- [x] Add `study validate`, `study run`, and `study report`.
- [x] Implement isolated OpenTitan-fixed and BaseJump-fixed P1 candidates.
- [x] Keep PPA locked when formal is inconclusive or a pair-specific miter is
  not implemented.
- [x] Complete the seven new P2 candidate implementations and miters.
- [x] Pass all valid P1/P2 configurations in the pinned container.
- [x] Require all applicable incorrect controls to produce counterexamples.
- [x] Freeze all formally passing candidate sources before PPA.
- [x] Run every passing configuration through M0/M1 twice.
- [x] Run the five frozen M2 samples twice.
- [x] Publish the complete evidence matrix and evaluate both promotion gates.
- [x] Keep the dashboard and ML outside the decision path.
- [x] Add a read-only, hash-linked M0/M1 family-study explorer with filters,
  area-versus-delay plots, repeatability facts, exact rows, and CSV export.
- [x] Document the wave in
  [Arbiter Family Credibility Wave V1](Arbiter%20Family%20Credibility%20Wave%20V1.md).

- [ ] Qualify 50–100 standalone modules.
- [ ] Cover at least eight engineering categories.
- [ ] Cover at least five independent upstream lineages.
- [ ] Add meaningful variants for arbitration and priority structures.
- [ ] Add meaningful variants for FIFO, skid, spill, and fall-through buffering.
- [ ] Add meaningful variants for ready/valid register placement and bypass.
- [ ] Add meaningful variants for decode and address-map structures.
- [ ] Add meaningful variants for mux/demux, routing, and bus conversion.
- [ ] Add meaningful variants for arithmetic/datapath organization.
- [ ] Add proofable state-machine, counter, queue, and scoreboard variants.
- [ ] Run M0/M1 on every proof-passing candidate.
- [ ] Run a frozen M2 OpenROAD sample.
- [ ] Publish positive, neutral, regressed, failed, and unsupported results.
- [ ] Connect the category-first dashboard landing view to qualified corpus
  records. The generated-evidence UI and interaction contract are complete.
- [ ] Populate reference RTL → variant diff → proof → per-flow synthesis tables
  from qualified Wave 3 variants. The same view is complete for generated
  calibration candidates.
- [x] Keep model evaluation in a secondary Research status view and outside the
  engineer result path.
- [ ] Pass every Tier A completion gate in Corpus Strategy V1.

## 7. Wave 4 — Complete Tier B

> **Status: not started**

- [ ] Pre-register and qualify 15–30 complete IP blocks.
- [ ] Cover at least three protocol families and five upstream lineages.
- [ ] Support multi-file dependency graphs, packages, parameter matrices,
  memories, and legal black boxes.
- [ ] Integrate upstream IP tests and protocol references.
- [ ] Implement P2/P3 block proof and proof partitioning.
- [ ] Add hierarchical synthesis reporting and source-linked critical cones.
- [ ] Reproduce every reference/variant result from clean containers.

## 8. Wave 5 — Complete Tier C

> **Status: not started**

- [ ] Pre-register and qualify 5–10 processor or accelerator subsystems.
- [ ] Include at least two processor and two accelerator lineages.
- [ ] Identify stage and transaction boundaries.
- [ ] Support latency/refinement maps for pipeline-depth changes.
- [ ] Add architectural scoreboards for multi-cycle execution units.
- [ ] Add assume-guarantee partition contracts.
- [ ] Complete one same-latency stage rewrite through proof and measurement.
- [ ] Complete one latency-changing candidate through transaction proof and
  measurement.

## 9. Wave 6 — Complete Tier D

> **Status: not started**

- [ ] Pre-register 2–4 complete SoCs from distinct lineages.
- [ ] Qualify OpenTitan or record a complete rejection/blocker.
- [ ] Select remaining SoCs before inspecting variant PPA.
- [ ] Record generator inputs and generated-output provenance.
- [ ] Compare bus, interrupt, clock, reset, memory-map, and parameter
  connectivity.
- [ ] Reuse Tier B/C partition proofs at SoC scope.
- [ ] Reproduce clean SoC builds and smoke workloads.
- [ ] Run hierarchical synthesis and selected physical/target-flow partitions.
- [ ] Scope every conclusion to checked properties and partitions.

## 10. Wave 7 — Model benchmark and promotion

> **Status: not started; current V2.2 model remains diagnostic-only**

- [ ] Reach enough independent, proof-qualified evidence to justify fitting.
- [ ] Freeze repository-, lineage-, topology-, hierarchy-, and SoC-disjoint
  evaluation splits.
- [ ] Compare deterministic rules, ML, and Codex on identical opportunities.
- [ ] Measure correct recommendations, harmful changes, missed opportunities,
  proof yield, flow disagreement, and calibration.
- [ ] Keep Tier D and independent-category release sets sealed until policy and
  thresholds are frozen.
- [ ] Demonstrate that ML adds safe coverage over deterministic rules.
- [ ] Pass release gates before allowing ML into live candidate selection.
- [ ] Keep formal and measured PPA authoritative after any model promotion.

## 11. Wave 8 — Internal productization

> **Status: not started**

- [ ] Harden CLI/plugin installation, version migration, and artifact retention.
- [ ] Define privacy, source-retention, audit, and approval policies.
- [ ] Add optional MCP connections only for approved internal documentation,
  design registries, artifact stores, schedulers, and EDA results.
- [ ] Add commercial synthesis and LEC adapters on company-controlled machines.
- [ ] Add authorization before any proprietary RTL or internal service access.
- [ ] Run a limited internal engineer pilot.
- [ ] Measure engineer task completion, result comprehension, trust, and target
  flow correlation.
- [ ] Define and pass production-readiness gates.

## 12. Capability matrix

| Capability | Status | Evidence or next requirement |
| --- | --- | --- |
| Deterministic combinational finding | Complete for one family | MVP V1 |
| Isolated source rewrite | Complete for one family | MVP V1 |
| P1 combinational equivalence | Complete | Yosys positive and negative controls |
| P2 sequential equivalence | Complete for the strengthened arbiter contract | Two exact repeats plus reset/state/grant/data negative controls |
| P3 latency-aware transaction proof | Prototype complete | verilog-axis one-stage/two-stage ordered-transaction proof plus drop/duplicate/order controls |
| P4 subsystem partition proof | Not started | Wave 5 |
| P5 SoC integration evidence | Not started | Wave 6 |
| P6 commercial LEC | Deferred | Approved company machine |
| M0/M1 Yosys/ABC | Complete for the frozen arbiter matrix | Exact normalized reproduction at N=1/4/8/16 |
| M2 OpenROAD | Complete for the frozen arbiter sample | Exact N=8/N=16 reproduction; only N=8 agrees with M0/M1 |
| M3/M4 target flows | Deferred | Company tools, libraries, and authorization |
| CLI Agent V2 | Registry and qualified-reference flow complete | Broader corpus-family dispatch remains Wave 3 |
| Codex plugin | Updated for registered P1/P2 candidates | Reinstall/new-task pickup required for local testing |
| Read-only run viewer | Complete for MVP | Category-first corpus view remains Wave 3 |
| MCP | Not required locally | Optional Wave 8 integration |
| Live ML recommendations | Disabled | Requires Wave 7 promotion |

## 13. External decisions and blockers

- [ ] **Owner decision:** Confirm the project license before adding a license or
  release tag.
- [x] **Wave 2 approval completed:** Download the first pinned upstream source
  tranche after its exact metadata lock was reviewed.
- [ ] **License review required later:** Confirm acceptability of Solderpad,
  mixed-license, deprecated, and strongly reciprocal sources before inclusion
  or redistribution.
- [ ] **External machine required later:** Run Genus, Conformal, or other
  commercial target-flow checks.
- [ ] **Evidence blocker:** Eight open references are now qualified, but the
  corpus does not yet contain the 50–100 Tier A references or measured variant
  portfolio needed for an unseen-RTL usefulness claim.

## 14. Next action

Continue Wave 3 in this order:

1. Pre-register nine more qualified same-cycle arbiter pairs across at least
   two additional upstream lineages before inspecting candidate PPA.
2. Freeze the next Tier A references and transformation families, then expand
   from 8 to 50–100 qualified modules across at least eight categories and five
   independent upstream lineages.
3. Add meaningful same-latency and latency-changing variants, then apply P2 or
   P3 according to each declared contract.
4. Measure every proof-passing candidate under identical M0/M1 recipes and
   publish improvements, neutral results, regressions, and blockers.
5. Populate the category-first dashboard only from those immutable records.

Keep the dashboard read-only until the qualified corpus evidence adapter is
implemented.
