module g01_add8_left_candidate(
  input  logic [7:0] a, b, c, d,
  output logic [7:0] y
);
  assign y = (a + b) + (c + d);
endmodule
