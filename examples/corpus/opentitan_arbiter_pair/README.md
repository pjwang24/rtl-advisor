# OpenTitan arbiter-pair metadata fixture

This directory exercises Corpus Registry V1 without downloading or vendoring
OpenTitan RTL.

- `reference.json` records `prim_arbiter_ppc` as one discovered Tier A reference
  lineage.
- `variant.json` records `prim_arbiter_tree` as an upstream alternative beneath
  that reference. It does not increase the Tier A reference count.

Both records intentionally remain below `source_pinned`: their revision,
license-file hash, source hashes, reproducible commands, and tool versions are
not yet frozen. They must not be described as qualified references or formal
passes.
