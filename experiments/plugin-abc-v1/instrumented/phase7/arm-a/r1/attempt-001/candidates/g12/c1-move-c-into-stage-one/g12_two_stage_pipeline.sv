module g12_two_stage_pipeline #(
  parameter int WIDTH = 16
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic             valid_i,
  input  logic [WIDTH-1:0] a_i, b_i, c_i,
  output logic             valid_o,
  output logic [WIDTH-1:0] y_o
);
  logic [WIDTH-1:0] sum_q;
  logic             valid_q;
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      sum_q <= '0;
      valid_q <= 1'b0;
      y_o <= '0;
      valid_o <= 1'b0;
    end else begin
      sum_q <= a_i + b_i + c_i;
      valid_q <= valid_i;
      y_o <= sum_q;
      valid_o <= valid_q;
    end
  end
endmodule
