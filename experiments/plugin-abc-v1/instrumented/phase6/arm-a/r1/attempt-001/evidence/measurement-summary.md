# Raw EDA measurement summary

Method: Yosys 0.63 read the baseline or candidate as SystemVerilog, ran
`synth -noabc`, mapped with ABC's `simple` generic gate set, purged unused
logic, then reported `stat` and `ltp`.  Cell count is the sum of mapped generic
cells.  The baseline and candidate were run independently with the same top and
commands.  Formal equivalence used Yosys `equiv_make`, `equiv_simple`, and
asserting `equiv_status` on the pre-synthesis designs.

| Case | Baseline cells | Candidate cells | Baseline LTP | Candidate LTP | Formal |
| --- | ---: | ---: | ---: | ---: | --- |
| g01 | 100 | 100 | 18 | 18 | passed, 8/8 bits |
| g02 | 626 | 626 | 68 | 68 | passed, 32/32 bits |
| g03 | 340 | 340 | 34 | 34 | passed, 32/32 output bits |
| g04 | 239 | 239 | 35 | 35 | passed, 16/16 bits |
| g05 | 93 | 93 | 31 | 31 | passed, 16/16 bits |
| g07 | 107 | 107 | 19 | 19 | passed, 17/17 compared bits |
| g08 | 221 | 221 | 34 | 34 | passed, 16/16 bits |

The tested rewrites therefore have no mapped area or topological-depth
improvement in this flow; the synthesis flow already obtains the same network.

# Source review disposition

- g06: the ordered `if` chain specifies an eight-way priority encoder; a
  balanced mux rewrite would need to retain that priority and X behavior, with
  no source-local redundant cone to remove.
- g09: the four-operand add is already expressed as a balanced tree.
- g10: nested `sat_add` calls are non-associative because each call saturates.
- g11: the one-entry elastic register has the standard minimal ready/valid
  update condition and data enable.
- g12: the two registered stages establish the externally visible cycle
  behavior; changing their boundary is not a semantics-preserving local PPA
  rewrite.
- o01: the arbitration winner is already produced by a parallel-prefix block;
  surrounding request masking, hold state, grant gating, and data selection
  have distinct protocol roles.
- o02: depth 0, depth 1, and normal-depth generate branches implement distinct
  FIFO contracts; no redundant state or bypass cone was found.
- o03: the duplicated inverse counter, saturation, commit mux, and registered
  sum error are security/semantic boundaries.
- o04: pack and unpack branches already specialize the variable shifts,
  occupancy updates, and clear behavior at elaboration.
- o05: this file is a compatibility wrapper only; it adds no local datapath or
  state to optimize.
- o06: the implementation is one elastic register with a single enable and the
  canonical ready equation.
- o07: pointer wrap, simultaneous push/pop count handling, fall-through, and
  memory enable logic are all semantically active; straightforward textual
  simplifications are synthesis canonicalizations.
- o08: arbitration and payload selection are already a logarithmic generated
  tree; fairness, lock, and external-priority options prevent local removal.
- o09: the design is exactly an indexed data/valid mux plus one-hot ready
  decode.
- o10: bypass, simple-buffer, and skid-buffer branches implement different
  configured contracts; the skid temporary state is required for backpressure.
- o11: the source is a structural parameterized chain of `axis_register`
  stages with no additional functional cone.
- o12: each pipeline stage and the output FIFO decouple ready timing while
  preserving AXI-stream order; no local state-removal rewrite was established.
