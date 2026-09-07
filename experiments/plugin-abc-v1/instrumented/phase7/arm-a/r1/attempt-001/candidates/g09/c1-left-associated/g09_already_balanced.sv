module g09_already_balanced(
  input  logic [31:0] a, b, c, d,
  output logic [31:0] y
);
  assign y = ((a + b) + c) + d;
endmodule
