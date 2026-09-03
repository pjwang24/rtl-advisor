module g04_mixed_procedural_add(
  input  logic [15:0] a, b, c, d,
  input  logic        bypass,
  output logic [15:0] y
);
  always_comb begin
    if (bypass)
      y = a;
    else
      y = ((a + b) + c) + d;
  end
endmodule
