module o03_error_p2 #(
  parameter int Width = 32
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic [Width-1:0] primary_count,
  input  logic [Width-1:0] secondary_count,
  output logic             old_err_q,
  output logic             new_err_q
);
  logic [Width:0] sum;
  logic old_err_d, new_err_d;
  assign sum = primary_count + secondary_count;
  assign old_err_d = (sum != {1'b0, {Width{1'b1}}});
  assign new_err_d = (primary_count != ~secondary_count);

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      old_err_q <= 1'b0;
      new_err_q <= 1'b0;
    end else begin
      old_err_q <= old_err_d;
      new_err_q <= new_err_d;
    end
  end
endmodule
