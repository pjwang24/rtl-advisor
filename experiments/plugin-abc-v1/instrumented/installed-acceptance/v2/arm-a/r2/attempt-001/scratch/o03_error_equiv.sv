module o03_error_equiv #(
  parameter int Width = 32
) (
  input logic [Width-1:0] primary_count,
  input logic [Width-1:0] secondary_count
);
  logic [Width:0] sum;
  logic old_err, new_err;
  assign sum = primary_count + secondary_count;
  assign old_err = (sum != {1'b0, {Width{1'b1}}});
  assign new_err = (primary_count != ~secondary_count);
  always_comb assert (old_err == new_err);
endmodule
