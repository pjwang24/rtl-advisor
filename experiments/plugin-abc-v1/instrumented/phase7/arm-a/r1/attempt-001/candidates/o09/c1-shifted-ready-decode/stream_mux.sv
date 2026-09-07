// Candidate: expose the one-hot ready decode as a shifted constant.
`include "common_cells/assertions.svh"

module stream_mux #(
  parameter type DATA_T = logic,
  parameter integer N_INP = 0,
  parameter integer SEL_WIDTH = cf_math_pkg::idx_width(N_INP)
) (
  input  DATA_T [N_INP-1:0]     inp_data_i,
  input  logic  [N_INP-1:0]     inp_valid_i,
  output logic  [N_INP-1:0]     inp_ready_o,
  input  logic  [SEL_WIDTH-1:0] inp_sel_i,
  output DATA_T                 oup_data_o,
  output logic                  oup_valid_o,
  input  logic                  oup_ready_i
);
  assign inp_ready_o = N_INP'(oup_ready_i) << inp_sel_i;
  assign oup_data_o  = inp_data_i[inp_sel_i];
  assign oup_valid_o = inp_valid_i[inp_sel_i];

`ifndef COMMON_CELLS_ASSERTS_OFF
  `ASSERT_INIT(n_inp_0, N_INP >= 1, "The number of inputs must be at least 1!")
`endif
endmodule
