# Known limitations — MVP V1

RTL Advisor `0.2.0a1` is a developer preview with a deliberately narrow safety
boundary.

## RTL scope

- The released transformation only balances an unbalanced continuous assignment
  containing at least three unsigned, equal-width, fixed-width addends.
- The top must be self-contained and combinational. Sequential state, clocks,
  resets, parameter overrides, black boxes, and block-level assumptions are not
  modeled.
- Mixed signedness, implicit truncation, macros, functions, generated spans,
  side effects, multiple drivers, ambiguous source locations, and unresolved
  packages are rejected.
- Original files are not edited. A candidate is a copied design with one
  isolated source change.

## Formal scope

- The MVP uses direct Yosys RTL-to-RTL equivalence with two-state bit-vector
  semantics. It is not a four-state simulation proof.
- A pass proves the candidate matches the baseline for the modeled module; it
  does not prove that either version meets the design specification.
- A failed or incomplete proof blocks synthesis measurement. EQY, sequential
  equivalence, RTL-to-technology-netlist equivalence, Conformal, and other
  commercial LEC flows are deferred.
- Unsupported constructs and tool timeouts may return an incomplete result
  rather than a pass or fail.

## Synthesis scope

- Measurements require the Yosys 0.63 release line (the exact release or a
  digest-pinned `0.63+N` bundle build), an adjacent `yosys-abc` 1.01
  executable, the pinned Nangate45 Liberty file, and the recorded standard or
  stronger recipe. The full version strings and executable hashes are recorded
  and must remain stable during each stage.
- Delay is a synthesis estimate, not routed timing. Area and cell counts are
  library-mapped estimates. Power is not measured.
- Results do not predict Cadence Genus, Synopsys Design Compiler, place and
  route, signoff timing, or a company target flow.
- The four final classifications apply only to the recorded recipes and
  thresholds. A target-flow decision still belongs to the engineer.

## Evidence limits

- The generated end-to-end example formally passes and currently returns
  `synthesis_handles`; both recipes are neutral.
- The frozen open-RTL release gate is blocked at 0/2 qualifying modules. The
  screen ended before candidate synthesis or PPA inspection, and no project or
  transformation was substituted after an outcome was observed.
- Generated fixtures validate mechanics and negative controls, but they do not
  establish accuracy or usefulness on unseen production RTL.
- Proprietary RTL, Arm CSS/SoC-scale designs, and commercial EDA comparisons
  are outside the MVP.

## ML and Codex limits

- The research V2.2 ML model is diagnostic-only. It cannot select sites, enable
  candidate creation, or determine a final result.
- Codex invokes and explains the CLI. It cannot turn a formal failure into a
  pass or change a synthesis classification.
- Unseen RTL outside the deterministic rule returns unsupported; the MVP does
  not force a prediction.

## Interface and packaging limits

- The dashboard is a local, read-only artifact viewer. It has no RTL upload,
  authentication, multi-user access, job queue, cancellation, or recovery.
- The dashboard server accepts loopback hosts only. It is not a remotely served
  or authenticated internal web application in this preview.
- CLI and Codex run tools; the browser only polls stored records.
- Run records contain local absolute paths and are intended for the machine that
  produced them. Normalized evidence fields support interface comparison, but
  whole JSON documents are not path-portable.
- Third-party RTL, the Liberty file, tool installations, and large evidence are
  excluded from the wheel.
- No project license is present until the owner confirms the proposed license.
