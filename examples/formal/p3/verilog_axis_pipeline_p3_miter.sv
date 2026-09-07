`ifndef MUTATION_KIND
`define MUTATION_KIND 0
`endif

module verilog_axis_pipeline_p3_miter #(
  parameter integer Mutation = `MUTATION_KIND
) (
  input wire clk,
  input wire stall_i
);
  // Contract: reset is active on the first formal step.  A shared sink may
  // stall for at most two consecutive cycles, which makes the bounded drain
  // assertion meaningful without requiring cycle-aligned outputs.
  wire formal_rst = $initstate;
  reg [1:0] stall_run_q;
  always @(posedge clk) begin
    if (formal_rst) begin
      stall_run_q <= 0;
    end else begin
      assume (!stall_i || stall_run_q < 2);
      stall_run_q <= stall_i ? stall_run_q + 1'b1 : 0;
    end
  end
  wire sink_ready = !stall_i;

  // Three arbitrary but distinguishable transactions.  Distinct sequence
  // tags let the checker expose drops, duplicates, and reordering directly.
  (* anyconst *) reg [5:0] arbitrary_a;
  (* anyconst *) reg [5:0] arbitrary_b;
  (* anyconst *) reg [5:0] arbitrary_c;
  wire [7:0] token_0 = {arbitrary_a, 2'b00};
  wire [7:0] token_1 = {arbitrary_b, 2'b01};
  wire [7:0] token_2 = {arbitrary_c, 2'b10};
  reg [1:0] source_index_q;
  reg gold_took_q;
  reg gate_took_q;
  wire source_active = source_index_q < 3;
  wire [7:0] source_data = source_index_q == 0 ? token_0 :
                           source_index_q == 1 ? token_1 : token_2;

  wire gold_in_ready;
  wire gate_in_ready;
  wire gold_in_valid = source_active && !gold_took_q;
  wire gate_in_valid = source_active && !gate_took_q;
  wire gold_accept = gold_in_valid && gold_in_ready;
  wire gate_accept = gate_in_valid && gate_in_ready;
  wire both_took = (gold_took_q || gold_accept) &&
                   (gate_took_q || gate_accept);

  always @(posedge clk) begin
    if (formal_rst) begin
      source_index_q <= 0;
      gold_took_q <= 0;
      gate_took_q <= 0;
    end else if (source_active) begin
      if (both_took) begin
        source_index_q <= source_index_q + 1'b1;
        gold_took_q <= 0;
        gate_took_q <= 0;
      end else begin
        gold_took_q <= gold_took_q || gold_accept;
        gate_took_q <= gate_took_q || gate_accept;
      end
    end
  end

  wire [7:0] gold_data;
  wire gold_valid;
  axis_pipeline_register #(
    .DATA_WIDTH(8),
    .KEEP_ENABLE(0),
    .LAST_ENABLE(0),
    .ID_ENABLE(0),
    .DEST_ENABLE(0),
    .USER_ENABLE(0),
    .REG_TYPE(1),
    .LENGTH(1)
  ) gold_dut (
    .clk,
    .rst(formal_rst),
    .s_axis_tdata(source_data),
    .s_axis_tkeep(1'b1),
    .s_axis_tvalid(gold_in_valid),
    .s_axis_tready(gold_in_ready),
    .s_axis_tlast(1'b0),
    .s_axis_tid(8'b0),
    .s_axis_tdest(8'b0),
    .s_axis_tuser(1'b0),
    .m_axis_tdata(gold_data),
    .m_axis_tkeep(),
    .m_axis_tvalid(gold_valid),
    .m_axis_tready(sink_ready),
    .m_axis_tlast(),
    .m_axis_tid(),
    .m_axis_tdest(),
    .m_axis_tuser()
  );

  wire [7:0] gate_raw_data;
  wire gate_raw_valid;
  axis_pipeline_register #(
    .DATA_WIDTH(8),
    .KEEP_ENABLE(0),
    .LAST_ENABLE(0),
    .ID_ENABLE(0),
    .DEST_ENABLE(0),
    .USER_ENABLE(0),
    .REG_TYPE(2),
    .LENGTH(2)
  ) gate_dut (
    .clk,
    .rst(formal_rst),
    .s_axis_tdata(source_data),
    .s_axis_tkeep(1'b1),
    .s_axis_tvalid(gate_in_valid),
    .s_axis_tready(gate_in_ready),
    .s_axis_tlast(1'b0),
    .s_axis_tid(8'b0),
    .s_axis_tdest(8'b0),
    .s_axis_tuser(1'b0),
    .m_axis_tdata(gate_raw_data),
    .m_axis_tkeep(),
    .m_axis_tvalid(gate_raw_valid),
    .m_axis_tready(sink_ready),
    .m_axis_tlast(),
    .m_axis_tid(),
    .m_axis_tdest(),
    .m_axis_tuser()
  );

  reg [2:0] gold_out_count_q;
  reg [2:0] gate_out_count_q;
  reg [4:0] cycle_count_q;
  wire gold_output = gold_valid && sink_ready;
  wire gate_output = gate_raw_valid && sink_ready;
  wire [7:0] expected_gold = gold_out_count_q == 0 ? token_0 :
                             gold_out_count_q == 1 ? token_1 : token_2;
  wire [7:0] expected_gate = gate_out_count_q == 0 ? token_0 :
                             gate_out_count_q == 1 ? token_1 : token_2;
  wire [7:0] gate_observed_data =
      Mutation == 2 && gate_out_count_q == 1 ? token_0 :
      Mutation == 3 && gate_out_count_q == 0 ? token_1 : gate_raw_data;
  wire gate_observed_valid =
      Mutation == 1 && gate_out_count_q == 1 ? 1'b0 : gate_raw_valid;

  always @(posedge clk) begin
    if (formal_rst) begin
      gold_out_count_q <= 0;
      gate_out_count_q <= 0;
      cycle_count_q <= 0;
    end else begin
      cycle_count_q <= cycle_count_q + 1'b1;
      if (gold_output) begin
        assert (gold_out_count_q < 3);
        assert (gold_data == expected_gold);
        gold_out_count_q <= gold_out_count_q + 1'b1;
      end
      if (gate_observed_valid && sink_ready) begin
        assert (gate_out_count_q < 3);
        assert (gate_observed_data == expected_gate);
        gate_out_count_q <= gate_out_count_q + 1'b1;
      end
      if (cycle_count_q == 15) begin
        assert (source_index_q == 3);
        assert (gold_out_count_q == 3);
        assert (gate_out_count_q == 3);
      end
    end
  end
endmodule
