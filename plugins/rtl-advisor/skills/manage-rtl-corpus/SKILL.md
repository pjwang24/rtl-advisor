---
name: manage-rtl-corpus
description: Validate frozen open-RTL corpus tranches, append explicitly approved tranches to Corpus Registry V1, run deterministic compile-context qualification, and audit lineage-aware dataset coverage. Use for RTL Advisor corpus size, diversity, qualification, registration, frozen tranche locks, blocked references, and coverage gaps. Do not use for reviewing proprietary RTL or treating parameter variants as independent designs.
---

# Manage RTL Corpus

Use RTL Advisor's registry and frozen tranche contracts as the authority. Keep
the primary dataset unit fixed to `independent_design_lineage`; references,
parameter configurations, and implementation variants are inventory, not three
interchangeable measures of dataset size.

## Workflow

1. Locate this skill directory and invoke the shared runner at
   `../analyze-rtl/scripts/run_rtl_advisor.py` for every operation.
2. Use `corpus coverage` for read-only questions about size, qualification,
   diversity, or gaps. Apply only filters stated by the engineer.
3. Use `corpus validate <lock>` to validate an already-frozen tranche before
   registration. Validation does not create or alter the lock.
4. Before `corpus register`, confirm the engineer explicitly requested the
   append-only registry mutation. Do not infer registration permission from a
   request to inspect or validate a tranche.
5. Before `corpus qualify`, confirm the tranche contains generated or
   explicitly approved open RTL, the local upstream roots are authorized, and
   the engineer explicitly requested qualification. Qualification may append
   source-pinned, build-reproduced, or blocked registry states; it never edits
   upstream RTL and keeps candidate synthesis disabled.
6. Explain the compact result and link its artifacts. Do not load full registry
   histories or build logs unless a named failure requires diagnosis.

## Commands

```bash
python3 <skill-dir>/../analyze-rtl/scripts/run_rtl_advisor.py corpus coverage \
  [--tier A|B|C|D] \
  [--category <category>] \
  [--split unassigned|development|calibration|evaluation|release_holdout] \
  [--qualification-state <state>] \
  [--qualification-status active|blocked|rejected] \
  [--registry-dir <path>]

python3 <skill-dir>/../analyze-rtl/scripts/run_rtl_advisor.py corpus validate \
  <tranche-lock.json>

python3 <skill-dir>/../analyze-rtl/scripts/run_rtl_advisor.py corpus register \
  <tranche-lock.json> [--registry-dir <path>]

python3 <skill-dir>/../analyze-rtl/scripts/run_rtl_advisor.py corpus qualify \
  <tranche-lock.json> <qualification-plan.json> [--registry-dir <path>]
```

Filters may be repeated. Registration and qualification are content-addressed
and append-only; a hash or identity conflict is a stop condition, not
permission to overwrite an existing record.

## Interpretation boundaries

- Lead with independent design-lineage count when answering “how large is the
  dataset?” Report reference and variant inventory separately.
- A parameter variant never increases the independent-design count.
- `qualified_design_lineage_count` includes only active references at
  `reference_qualified` or later.
- Blocked and rejected records remain visible but do not count as qualified.
- Coverage gaps describe registry metadata. They do not prove missing
  behavioral diversity within represented designs.
- A qualification pass establishes the frozen compile context and source
  integrity. It is not a candidate proof and is not target-flow PPA evidence.
- Exit code `4` with `completed_with_blockers` is a trusted partial outcome;
  report the blocked count and stop before downstream qualification stages.

Treat schema, document-type, semantic-hash, normalized-command, or exit-code
disagreement as untrusted automation and stop. Never upload corpus sources or
browse for replacements during this workflow.
