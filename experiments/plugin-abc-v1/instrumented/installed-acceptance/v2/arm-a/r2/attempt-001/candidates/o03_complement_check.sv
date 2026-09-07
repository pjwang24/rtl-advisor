module o03_complement_check_candidate #(
  parameter int Width = 2
) (
  input  logic [Width-1:0] primary_count,
  input  logic [Width-1:0] secondary_count,
  output logic             err_d
);
  assign err_d = (primary_count != ~secondary_count);
endmodule
