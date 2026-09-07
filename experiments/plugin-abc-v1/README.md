# RTL Advisor plugin A/B/C experiment

This directory is the durable, pre-registered comparison of Codex alone,
RTL Advisor's released plugin workflow, and a hybrid workflow.

The scored input is [`manifest.json`](manifest.json). Evaluator-only expected
behavior is in `oracle.json`; arm prompts must never expose that file. Arm
records are written under `runs/arm-a`, `runs/arm-b`, and `runs/arm-c` and are
validated and scored by `scripts/plugin_abc.py`.

The 12 open cases are source references into the ignored, locally downloaded
and hash-locked Tier-A corpus. They are not redistributed here. The 12
generated cases are experiment fixtures and are not part of any earlier
training or calibration corpus.

The first pass is useful directional evidence, but the framework requires a
second independent pass before making the final plugin product decision.

