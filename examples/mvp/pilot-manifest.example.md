# PilotManifest v1 example

A checked-in, universally valid JSON manifest is intentionally impossible: the
contract binds canonical absolute source paths, per-file SHA-256 values, and a
compile-context hash. Those values change with the checkout location and source
revision. Generate the final JSON on the machine that will run the pilot.

The example below creates `pilot-manifest.local.json` beside an eligible source
file. Replace every provenance value and the source/top names with the frozen
open-source pilot information. The generated `adder_chain.sv` fixture is useful
for pipeline testing, but it does not satisfy the two-project open-pilot gate.

```python
from __future__ import annotations

import json
from pathlib import Path

from rtl_advisor.mvp_schema import (
    PilotProvenanceV1,
    build_pilot_manifest,
)

pilot_dir = Path("/absolute/path/to/frozen-open-pilot")
manifest, _design = build_pilot_manifest(
    base=pilot_dir,
    top="eligible_combinational_top",
    files=("rtl/eligible_combinational_top.sv",),
    include_dirs=(),
    defines=(),
    objective="balanced",
    provenance=PilotProvenanceV1(
        project="upstream-project-name",
        source_url="https://github.com/owner/project",
        revision="full-pinned-commit-sha",
        license="SPDX-license-identifier",
        license_path="LICENSE",
    ),
)

output = pilot_dir / "pilot-manifest.local.json"
output.write_text(
    json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(output)
```

The emitted JSON contains exactly these MVP fields:

```text
schema_version: 1
document_type: rtl-advisor.pilot-manifest
top
files or filelist
include_dirs
defines
objective: timing | area | balanced
provenance: project, source_url, revision, license, license_path
source_hashes
compile_context_hash
synthesis_profiles: [standard, stronger]
```

Do not hand-edit derived hashes. Loading the manifest recalculates them and
rejects stale source or compile context. MVP manifests do not accept parameter
overrides, clocks, reset models, black-box assumptions, or sequential semantics.
