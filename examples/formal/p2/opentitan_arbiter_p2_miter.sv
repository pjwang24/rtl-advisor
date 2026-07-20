`ifndef MUTATION_KIND
`define MUTATION_KIND 0
`endif

module opentitan_arbiter_p2_miter #(
  parameter integer Mutation = `MUTATION_KIND
) (
  input logic clk_i,
  input logic req_chk_i,
  input logic [3:0] req_i,
  input logic [31:0] data_i,
  input logic ready_i
);
  // Formal contract: reset is asserted at time zero and then released.
  wire formal_rst_ni = !$initstate;

  // Formal contract: the request and payload vectors are sampled after reset
  // and remain stable. ready_i remains unconstrained on every cycle.
  logic [3:0] formal_req_q;
  logic [31:0] formal_data_q;
  logic formal_inputs_locked_q;
  wire [3:0] formal_req = formal_inputs_locked_q ? formal_req_q : req_i;
  wire [31:0] formal_data = formal_inputs_locked_q ? formal_data_q : data_i;
  always_ff @(posedge clk_i or negedge formal_rst_ni) begin
    if (!formal_rst_ni) begin
      formal_req_q <= '0;
      formal_data_q <= '0;
      formal_inputs_locked_q <= 1'b0;
    end else if (!formal_inputs_locked_q) begin
      formal_req_q <= req_i;
      formal_data_q <= data_i;
      formal_inputs_locked_q <= 1'b1;
    end
  end

  logic [3:0] gold_gnt;
  logic [1:0] gold_idx;
  logic gold_valid;
  logic [7:0] gold_data;
  prim_arbiter_ppc gold_dut (
    .clk_i,
    .rst_ni(formal_rst_ni),
    .req_chk_i,
    .req_i(formal_req),
    .data_i(formal_data),
    .gnt_o(gold_gnt),
    .idx_o(gold_idx),
    .valid_o(gold_valid),
    .data_o(gold_data),
    .ready_i
  );

  wire gate_rst_ni = Mutation == 1 ? 1'b1 : formal_rst_ni;
  wire gate_ready = Mutation == 2 ? ready_i ^ gold_valid : ready_i;
  logic [3:0] gate_raw_gnt;
  logic [1:0] gate_idx;
  logic gate_valid;
  logic [7:0] gate_data;
  prim_arbiter_tree gate_dut (
    .clk_i,
    .rst_ni(gate_rst_ni),
    .req_chk_i,
    .req_i(formal_req),
    .data_i(formal_data),
    .gnt_o(gate_raw_gnt),
    .idx_o(gate_idx),
    .valid_o(gate_valid),
    .data_o(gate_data),
    .ready_i(gate_ready)
  );
  wire [3:0] gate_gnt = Mutation == 3 ? '0 : gate_raw_gnt;

  always_comb begin
    assert (gold_valid == gate_valid);
    assert (gold_gnt == gate_gnt);
    if (gold_valid && gate_valid) begin
      assert (gold_idx == gate_idx);
      assert (gold_data == gate_data);
    end
  end
endmodule
