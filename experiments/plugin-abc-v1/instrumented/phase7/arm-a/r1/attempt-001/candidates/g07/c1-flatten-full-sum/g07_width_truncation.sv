module g07_width_truncation(
  input  logic [7:0] a, b, c, d,
  output logic [8:0] y
);
  assign y = a + b + c + d;
endmodule
