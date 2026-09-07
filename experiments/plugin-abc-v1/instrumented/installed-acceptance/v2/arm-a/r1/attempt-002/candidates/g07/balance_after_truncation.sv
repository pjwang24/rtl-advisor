module g07_width_truncation(
  input  logic [7:0] a, b, c, d,
  output logic [8:0] y
);
  logic [7:0] partial;
  logic [8:0] cd_sum;
  assign partial = a + b;
  assign cd_sum = {1'b0, c} + {1'b0, d};
  assign y = {1'b0, partial} + cd_sum;
endmodule
