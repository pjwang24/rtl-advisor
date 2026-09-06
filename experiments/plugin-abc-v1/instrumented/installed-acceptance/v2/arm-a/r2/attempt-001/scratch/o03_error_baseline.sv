module o03_error_baseline #(
  parameter int Width = 2
) (
  input  logic [Width-1:0] primary_count,
  input  logic [Width-1:0] secondary_count,
  output logic             err_d
);
  logic [Width:0] sum;
  assign sum = primary_count + secondary_count;
  assign err_d = (sum != {1'b0, {Width{1'b1}}});
endmodule
