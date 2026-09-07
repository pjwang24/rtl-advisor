# MVP V1 open-RTL feasibility result

Status: **blocked before candidate synthesis**.

The complete pre-registered corpus contains no self-contained combinational top with an unambiguous direct assignment of three or more unsigned equal-width addends. No candidate synthesis or PPA result was inspected, so the frozen selection protocol remains uncontaminated.

The lock records each pinned checkout or upstream snapshot revision, closest
source path and SHA-256, license evidence, eligible-site list, and exclusion.
Because no source reached structural eligibility, there was no candidate top to
compile; each compile command is explicitly recorded as not run rather than
silently treated as passing.

| Project | Closest site | Exclusion |
|---|---|---|
| ORFS `riscv32i` | `alu.v:14`, `a2 + b2 + alucont[2]` | 33/33/1-bit operands |
| ORFS Ibex | `ibex_multdiv_fast.sv:159` | Signed, generated context, sequential module |
| PicoRV32 | `picorv32.v:2265` | Equal-width site inside a sequential module |
| ORFS CVA6 | `iteration_div_sqrt_mvp.sv:59` | 25/25/1-bit operands |

The developer-preview engine can be completed and validated with generated fixtures, but the two-project open-pilot release gate cannot pass without a new pre-registered corpus or a separately reviewed decision to support combinational-cone extraction. The current implementation must not quietly relax the eligibility rules.

The machine-readable lock is [mvp-v1-feasibility.json](mvp-v1-feasibility.json).
