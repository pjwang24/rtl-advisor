module g08_signed_chain_candidate(
  input  logic signed [15:0] a, b, c, d,
  output logic signed [15:0] y
);
  assign y = (a + b) + (c + d);
endmodule
