module pulp_stream_register_behavior (
  input logic clk_i,
  input logic rst_ni,
  input logic ready_i
);
  (* init = 0 *) logic reset_released;
  always_ff @(posedge clk_i) reset_released <= 1'b1;
  always_comb assume (rst_ni == reset_released);
  logic [1:0] accepted_i;
  logic [1:0] accepted_o;
  logic [1:0] stall_count;
  wire valid_i = accepted_i < 3;
  wire data_i = accepted_i == 1;
  logic ready_o;
  logic valid_o;
  logic data_o;

  stream_register dut (
    .clk_i, .rst_ni, .clr_i(1'b0), .testmode_i(1'b0),
    .valid_i, .ready_o, .data_i, .valid_o, .ready_i, .data_o
  );

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      accepted_i <= 0;
      accepted_o <= 0;
      stall_count <= 0;
    end else begin
      if (valid_i && ready_o) accepted_i <= accepted_i + 1;
      if (valid_o && ready_i) begin
        assert (accepted_o < 3);
        assert (data_o == (accepted_o == 1));
        accepted_o <= accepted_o + 1;
      end
      if (valid_o && !ready_i) stall_count <= stall_count + 1;
      else stall_count <= 0;
      assume (stall_count < 2);
      assert (accepted_o <= accepted_i);
    end
  end

  logic [4:0] cycle_count;
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) cycle_count <= 0;
    else begin
      cycle_count <= cycle_count + 1;
      if (cycle_count >= 15) begin
        assert (accepted_i == 3);
        assert (accepted_o == 3);
      end
    end
  end
endmodule

module pulp_spill_register_behavior (
  input logic clk_i,
  input logic rst_ni,
  input logic ready_i
);
  (* init = 0 *) logic reset_released;
  always_ff @(posedge clk_i) reset_released <= 1'b1;
  always_comb assume (rst_ni == reset_released);
  logic [1:0] accepted_i;
  logic [1:0] accepted_o;
  logic [1:0] stall_count;
  wire valid_i = accepted_i < 3;
  wire data_i = accepted_i == 1;
  logic ready_o;
  logic valid_o;
  logic data_o;

  spill_register #(.Bypass(1'b0)) dut (
    .clk_i, .rst_ni, .valid_i, .ready_o, .data_i,
    .valid_o, .ready_i, .data_o
  );

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      accepted_i <= 0;
      accepted_o <= 0;
      stall_count <= 0;
    end else begin
      if (valid_i && ready_o) accepted_i <= accepted_i + 1;
      if (valid_o && ready_i) begin
        assert (accepted_o < 3);
        assert (data_o == (accepted_o == 1));
        accepted_o <= accepted_o + 1;
      end
      if (valid_o && !ready_i) stall_count <= stall_count + 1;
      else stall_count <= 0;
      assume (stall_count < 2);
      assert (accepted_o <= accepted_i);
    end
  end

  logic [4:0] cycle_count;
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) cycle_count <= 0;
    else begin
      cycle_count <= cycle_count + 1;
      if (cycle_count >= 15) begin
        assert (accepted_i == 3);
        assert (accepted_o == 3);
      end
    end
  end
endmodule

module pulp_fifo_v3_behavior (
  input logic clk_i,
  input logic rst_ni,
  input logic ready_i
);
  (* init = 0 *) logic reset_released;
  always_ff @(posedge clk_i) reset_released <= 1'b1;
  always_comb assume (rst_ni == reset_released);
  logic [1:0] accepted_i;
  logic [1:0] accepted_o;
  logic [1:0] stall_count;
  logic [7:0] data_o;
  logic full_o;
  logic empty_o;
  logic [1:0] usage_o;
  wire push_i = accepted_i < 3 && !full_o;
  wire pop_i = ready_i && !empty_o;
  wire [7:0] data_i = accepted_i == 0 ? 8'h31 :
                          accepted_i == 1 ? 8'ha5 : 8'h7e;

  fifo_v3 #(.DATA_WIDTH(8), .DEPTH(4)) dut (
    .clk_i, .rst_ni, .flush_i(1'b0), .testmode_i(1'b0),
    .full_o, .empty_o, .usage_o, .data_i, .push_i, .data_o, .pop_i
  );

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      accepted_i <= 0;
      accepted_o <= 0;
      stall_count <= 0;
    end else begin
      if (push_i) accepted_i <= accepted_i + 1;
      if (pop_i) begin
        assert (accepted_o < 3);
        if (accepted_o == 0) assert (data_o == 8'h31);
        if (accepted_o == 1) assert (data_o == 8'ha5);
        if (accepted_o == 2) assert (data_o == 8'h7e);
        accepted_o <= accepted_o + 1;
      end
      if (!empty_o && !ready_i) stall_count <= stall_count + 1;
      else stall_count <= 0;
      assume (stall_count < 2);
      assert (accepted_o <= accepted_i);
      assert (usage_o <= 3);
    end
  end

  logic [4:0] cycle_count;
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) cycle_count <= 0;
    else begin
      cycle_count <= cycle_count + 1;
      if (cycle_count >= 15) begin
        assert (accepted_i == 3);
        assert (accepted_o == 3);
      end
    end
  end
endmodule

module pulp_rr_arb_tree_behavior (
  input logic clk_i,
  input logic rst_ni,
  input logic [3:0] req_i,
  input logic gnt_i,
  input logic [31:0] packed_data_i
);
  (* init = 0 *) logic reset_released;
  always_ff @(posedge clk_i) reset_released <= 1'b1;
  always_comb assume (rst_ni == reset_released);
  wire [3:0][7:0] data_i = {
    packed_data_i[31:24], packed_data_i[23:16],
    packed_data_i[15:8], packed_data_i[7:0]
  };
  logic [3:0] gnt_o;
  logic req_o;
  logic [7:0] data_o;
  logic [1:0] idx_o;

  rr_arb_tree #(.NumIn(4), .DataWidth(8)) dut (
    .clk_i, .rst_ni, .flush_i(1'b0), .rr_i(2'b00), .req_i,
    .gnt_o, .data_i, .req_o, .gnt_i, .data_o, .idx_o
  );

  always_comb begin
    assert (req_o == |req_i);
    assert (gnt_o == 4'b0000 || gnt_o == 4'b0001 ||
            gnt_o == 4'b0010 || gnt_o == 4'b0100 || gnt_o == 4'b1000);
    assert ((gnt_o & ~req_i) == 0);
    assert ((|gnt_o) == (req_o && gnt_i));
    if (req_o) begin
      assert (req_i[idx_o]);
      assert (data_o == data_i[idx_o]);
    end
    if (|gnt_o) assert (gnt_o[idx_o]);
  end
endmodule
