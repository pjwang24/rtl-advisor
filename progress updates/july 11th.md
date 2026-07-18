There are 2 major steps left in the six-step v1 roadmap. Step 4 had two substeps, so we have completed five tracked milestones: Steps 1, 2, 3, 4A, and 4B.

| Stage | Status | Accomplishment |
|---|---|---|
| Step 1 | Complete | Python CLI, reproducible environment checks, pinned Nangate45 library, Yosys/ABC/Verilator/Codex detection |
| Step 2 | Complete | Generated greenfield RTL with baseline, equivalent rewrite, and intentionally incorrect variant; lint and formal equivalence flow |
| Step 3 | Complete | Yosys/ABC synthesis with delay, area, cell count, caching, provenance, and mapped netlists |
| Step 4A | Complete | Hierarchy-preserving RTL graph and first resource-sharing recommendation rule |
| Step 4B | Complete | Blinded Codex-only and hybrid analysis with strict schemas, caching, tool-use auditing, and live Sol/xhigh validation |
| Step 5 | Remaining | Expand the corpus, transformation families, rules, and safe patch-validation workflow |
| Step 6 | Remaining | Run and report the five-arm blinded benchmark |

What we have proven so far:

- We can generate our own RTL without company IP.
- Verilator validates syntax and lint.
- Yosys formally proved v1 equivalent to v0.
- Yosys correctly rejected v2 and produced a counterexample.
- Synthesis showed the real tradeoff:
  - v1 area improved by 20.94%.
  - Cell count improved by 30.43%.
  - Delay worsened by 42.19%.
- The hardcoded rule detected the resource-sharing opportunity.
- Codex-only independently detected it without seeing synthesis results.
- Hybrid analysis produced better source localization in the first smoke test.
- Codex also recognized the reverse area-versus-timing tradeoff in v1.
- All 20 automated tests pass.
- Model runs are auditable and rejected if Codex invokes tools or returns invalid output.

The remaining work is substantial despite being only two numbered steps. Step 5 needs coverage across nine RTL transformation families and a target corpus of 32 development plus 36 held-out cases. Step 6 then executes the five benchmark arms, approximately 240 planned Codex runs, statistical comparisons, and a reproducible report.

The current roadmap is recorded in [implementation plan/v1.md](../implementation%20plan/v1.md).
