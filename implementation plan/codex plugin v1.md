# RTL Advisor Codex Plugin V1 Implementation Plan

## 1. Objective

Create a repository-owned Codex plugin that lets engineers use RTL Advisor in
plain language while preserving the CLI as the single execution and evidence
engine.

The same analysis must remain available from a terminal. Codex may choose safe
commands, summarize results, and explain evidence, but it must not implement a
separate recommendation algorithm or override the CLI decision.

This interface track is independent from the frozen V2.3 model experiment. The
plugin may expose only capabilities that the installed CLI reports as ready. It
must not make failed or diagnostic-only models appear production-ready.

## 2. Product boundary

```text
Engineer in terminal ───────────────┐
                                    ▼
                              RTL Advisor CLI
                                    │
Engineer talking to Codex ─► Codex plugin/skill
                                    │
                                    └── invokes the same CLI commands
                                                │
                                                ▼
                                      versioned JSON evidence
```

The CLI owns:

- RTL and filelist parsing.
- Environment and tool checks.
- Structural findings and deterministic rules.
- Model readiness and recommendation decisions.
- Candidate generation in an isolated artifact workspace.
- Lint, formal equivalence, synthesis, and provenance.
- Versioned JSON schemas, run identifiers, and exit status.

The Codex plugin owns:

- Translating an engineer's request into supported CLI operations.
- Asking for missing intent such as timing, area, or balanced optimization.
- Explaining findings in plain engineering language.
- Presenting source locations, tradeoffs, evidence strength, and limitations.
- Requesting explicit confirmation before optional candidate-generation work.
- Preserving and linking the exact commands and result artifacts it used.

Codex does not own correctness, eligibility, ranking, PPA truth, or source
mutation.

## 3. Repository structure

Use a repository marketplace and one versioned plugin:

```text
.agents/
└── plugins/
    └── marketplace.json

plugins/
└── rtl-advisor/
    ├── .codex-plugin/
    │   └── plugin.json
    └── skills/
        └── analyze-rtl/
            ├── SKILL.md
            ├── agents/
            │   └── openai.yaml
            ├── scripts/
            │   └── run_rtl_advisor.py
            └── references/
                ├── cli-contract.md
                └── result-interpretation.md
```

Plugin name: `rtl-advisor`.

Initial skill name: `analyze-rtl`.

Do not add an MCP server, app connector, hook, or background service in V1. The
local CLI subprocess is sufficient and keeps the first interface small and
auditable.

The repository marketplace entry must use the standard required installation,
authentication, and category policy fields. The plugin manifest must contain
only supported fields and must not declare companion components that do not
exist.

## 4. Engineer requests supported in V1

The skill should trigger for requests such as:

- "Analyze this generated RTL for timing risks."
- "Review this manifest and tell me whether any rewrite is worthwhile."
- "Explain why RTL Advisor recommends this candidate."
- "Would synthesis probably handle this automatically?"
- "Show the exact evidence behind this finding."
- "Generate the proposed candidate and run equivalence."
- "Compare the baseline and candidate synthesis results."
- "Summarize the analysis in a form I can put in a code review."

V1 inputs:

- A generated RTL Advisor case or manifest.
- An explicitly approved open RTL module supported by the current CLI.
- A previously created RTL Advisor run or evidence artifact.

Filelist, include-directory, macro-definition, and multi-top support enter the
plugin only after the shared CLI supports them through a stable JSON command.
The skill must report an unsupported input honestly instead of approximating a
result from raw source alone.

## 5. Shared CLI automation contract

Terminal users and the Codex skill must receive the same underlying result. Add
a small, stable automation surface rather than teaching the skill to scrape
human-formatted terminal output.

Target interface:

```text
rtl-advisor agent capabilities --json
rtl-advisor agent review <input> --objective timing|area|balanced --json
rtl-advisor agent candidate <run-id> --finding <finding-id> --json
rtl-advisor agent verify <run-id> --candidate <candidate-id> --json
```

The `agent` namespace is an automation API; it is not a second advisor. Each
command delegates to existing versioned analysis, candidate, and verification
modules.

### 5.1 Capabilities

`agent capabilities` must report:

- CLI and schema version.
- Available parsers and supported input forms.
- Tool availability for lint, formal, synthesis, and Codex explanation.
- Model versions and whether each is ready, diagnostic-only, or unavailable.
- Supported objectives and transformation families.
- Whether candidate generation and verification are available.

The plugin always calls this command before choosing a workflow. It must not
infer readiness from files or from a prior conversation.

### 5.2 Review result

`agent review` must return a versioned JSON object containing:

- Run ID, input hashes, compile context, objective, and status.
- Findings with stable IDs and source locations.
- One of: `recommended`, `synthesis_likely_handles`,
  `target_flow_confirmation`, `no_change`, `unsupported`, or `failed`.
- Expected delay, area, and cell-count direction when available.
- Evidence source: deterministic rule, calibrated model, synthesis calibration,
  or formal result.
- Model/readiness limitations written for display without translation.
- Artifact paths and the exact normalized command.

Codex may simplify the wording, but it must preserve the status, values, and
limitations.

### 5.3 Candidate and verification result

Candidate creation must use an artifact workspace and never modify the input.
The response must include original and candidate hashes, a unified diff, and
the validation stages that were requested.

A candidate may be described as behavior-preserving only after a current formal
equivalence result passes and its hashes match. Lint success alone is not
equivalence. Synthesis improvement is not correctness.

## 6. Skill operating procedure

The `analyze-rtl` skill uses this sequence:

1. Locate the repository and resolve the CLI without changing the environment.
2. Call `agent capabilities --json`.
3. Confirm the input is generated or explicitly approved and within the
   reported support boundary.
4. Default to read-only review and ask for an objective only when it materially
   changes the analysis.
5. Run the CLI command and retain its JSON result.
6. Check exit status, schema version, input hashes, and result status before
   explaining anything.
7. Present the source location, decision, reason, likely tradeoff, evidence,
   and limitation in plain language.
8. Generate a candidate only when the engineer explicitly asks for one.
9. Run formal verification before calling a candidate safe.
10. Report the exact command and artifact paths so the result can be reproduced
    directly in a terminal.

If the CLI reports a failed calibration gate, missing tool, unsupported input,
stale evidence, or failed proof, the skill stops that workflow and explains the
specific condition. It must not substitute its own RTL recommendation.

## 7. Command runner

Add one small deterministic script, `run_rtl_advisor.py`, to avoid environment-
specific command guessing. It resolves the executable in this order:

1. Explicit `RTL_ADVISOR_BIN` environment variable.
2. `rtl-advisor` available on `PATH`.
3. The repository's supported `uv run --no-editable rtl-advisor` command.

The runner accepts only the documented `agent` subcommands, passes arguments as
an array without shell interpolation, requires JSON output, preserves the CLI
exit code, and rejects malformed or unsupported schema versions. It does not
contain recommendation logic.

## 8. Safety and data handling

- Use generated or explicitly approved open RTL during plugin development and
  testing.
- Never send RTL to an external service unless the engineer and their company
  policy explicitly permit that deployment.
- Do not browse the web or contact external systems during RTL analysis.
- Do not modify input RTL, filelists, constraints, or build scripts.
- Keep candidate content under the versioned artifact directory.
- Require explicit user intent before candidate generation, synthesis, Docker,
  or other material compute.
- Do not run a held-out or blind benchmark through the conversational skill.
- Do not expose hidden labels, proprietary paths, credentials, or full internal
  logs in explanations.
- Preserve CLI errors and unavailable states rather than converting them into a
  recommendation.

For a company deployment, install the plugin and CLI inside the approved local
or internal environment. The repository plugin does not itself grant permission
to process proprietary RTL.

## 9. Presentation contract

Every normal finding should answer:

1. Where is it?
2. What did RTL Advisor observe?
3. Why might it matter?
4. Would synthesis likely handle it?
5. What action, if any, is recommended?
6. What evidence and limitations support the answer?

Use the following user-facing decisions:

- **Recommended** — evidence supports reviewing the proposed change.
- **Synthesis likely handles this** — source cleanup may help readability, but
  implementation benefit is not expected.
- **Target-flow confirmation needed** — results depend on synthesis settings or
  the target technology.
- **No change recommended** — no safe, useful candidate cleared the release
  criteria.
- **Analysis unavailable** — tools, model readiness, or input support prevented
  a trustworthy decision.

Do not present confidence as a vague percentage without the underlying evidence
or gate. Keep predicted PPA clearly separate from measured synthesis results.

## 10. Validation and acceptance

### 10.1 Package validation

- Scaffold the repository plugin with the official plugin-creator script.
- Generate the skill with the official skill-creator initializer.
- Validate `SKILL.md` with `quick_validate.py`.
- Validate the complete plugin with `validate_plugin.py`.
- Keep the plugin manifest, folder name, marketplace name, and source path
  consistent.
- Leave no placeholder or example-only files in the final package.

### 10.2 CLI/plugin parity

For each reference request, run the equivalent terminal and plugin workflows
against the same generated input. Require:

- Identical CLI run ID and semantic result hash.
- Identical finding IDs, decisions, metrics, and evidence references.
- Identical candidate and proof hashes when candidate generation is requested.
- No source hash changes.
- The plugin explanation introduces no unsupported claim.

### 10.3 Reference scenarios

Test at least these cases:

- A formally equivalent candidate with synthesis benefit that survives the
  stronger recipe.
- A rewrite that synthesis already absorbs.
- A synthesis-recipe-dependent tradeoff requiring target confirmation.
- A candidate that fails formal equivalence.
- A diagnostic-only model that must remain unavailable for live use.
- A missing Yosys or Verilator tool.
- An unsupported filelist or SystemVerilog construct.
- A request to edit source in place, which must be refused.

The plugin is ready for repository installation only when all package, parity,
safety, and regression tests pass.

## 11. Implementation sequence

### Phase 1 — Stable CLI boundary

- Define and test the versioned `agent` JSON schemas.
- Implement `capabilities`, `review`, `candidate`, and `verify` as adapters over
  existing modules.
- Add semantic hashes and source-integrity checks.
- Preserve current commands and outputs for existing users.

### Phase 2 — Repository plugin scaffold

- Create `plugins/rtl-advisor` and its required plugin manifest.
- Create `.agents/plugins/marketplace.json` with the repository entry.
- Initialize `skills/analyze-rtl` and generate its UI metadata.
- Add only the runner and two references listed in this plan.

### Phase 3 — Read-only conversational review

- Implement capability discovery and read-only review.
- Add plain-language result rendering and reproducibility details.
- Exercise generated examples for every user-facing decision state.

### Phase 4 — Candidate and formal workflow

- Add explicit candidate-generation handling.
- Require isolated output, source-integrity checks, lint, and formal proof.
- Add synthesis comparison only when requested and available.

### Phase 5 — Parity and installation validation

- Run package validators and the complete repository regression.
- Run terminal-versus-plugin parity scenarios.
- Install the repository marketplace locally and test in a fresh Codex thread.
- Document only the short installation and first-use commands in the project
  README after validation passes.

### Phase 6 — Later interfaces

After V1 is stable, reuse the same CLI JSON contract for:

- VS Code source annotations.
- CI and code-review comments.
- The internal dashboard's live-analysis workflow.

Do not create separate recommendation logic for any interface.

## 12. V1 completion criteria

Plugin V1 is complete when:

- A repository marketplace installs `rtl-advisor` successfully.
- Codex reliably triggers `analyze-rtl` for the registered request patterns.
- Terminal and conversational workflows produce identical semantic results.
- Read-only review never changes source files.
- Candidate generation requires explicit intent and uses an isolated workspace.
- Every candidate called safe has a current hash-matched equivalence proof.
- Diagnostic-only models remain visibly unavailable.
- All package validators, reference scenarios, and repository tests pass.
- The project README explains the human workflow without requiring model or EDA
  vocabulary.

The next implementation action is Phase 1: add and test the stable CLI
automation boundary before scaffolding the plugin that depends on it.
