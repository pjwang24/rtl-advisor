module g02_add32_five_candidate(
  input  logic [31:0] a, b, c, d, e,
  output logic [31:0] y
);
  assign y = ((a + b) + (c + d)) + e;
endmodule
