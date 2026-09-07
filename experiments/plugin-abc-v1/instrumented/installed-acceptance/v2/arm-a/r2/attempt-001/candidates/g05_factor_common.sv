module g05_mux_common_logic_candidate(
  input  logic [15:0] a, b, c,
  input  logic        sel,
  output logic [15:0] y
);
  assign y = a + (sel ? b : c);
endmodule
