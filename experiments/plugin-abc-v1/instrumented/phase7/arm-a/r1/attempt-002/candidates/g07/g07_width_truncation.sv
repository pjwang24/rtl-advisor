module g07_width_truncation(
  input  logic [7:0] a, b, c, d,
  output logic [8:0] y
);
  logic [7:0] partial;
  assign partial = a + b;
  assign y = partial + (c + d);
endmodule
