# Canonical write quarantine

Arm A repetition 2 attempt 002 followed the frozen packet's original write path
instead of the stricter instrumented prompt. It rewrote 12 tracked synthesis log
files and created candidate/evidence files for `g06` and `g07` under the canonical
`experiments/plugin-abc-v1/runs/arm-a/r2` tree.

The generated versions are preserved in this directory. The 12 tracked files were
restored byte-for-byte from benchmark-start `HEAD` (`b285ce6`), and the new files
were moved here. A clean diff of the canonical `runs` tree verifies the restoration.

This observation drove two harness changes after the measured `v2` sessions:

- installed-acceptance sessions now receive an effective packet whose only changes
  replace conflicting filesystem isolation rules with the instrumented attempt path;
- the source/tool identity now pins the complete canonical `runs` tree, so any
  canonical evidence mutation makes the post-run audit fail.
