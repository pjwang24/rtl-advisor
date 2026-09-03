module g03_mixed_safe_add(
  input  logic [15:0] a, b, c, d,
  input  logic [1:0]  sel,
  output logic [15:0] sum,
  output logic [15:0] picked
);
  assign sum = ((a + b) + c) + d;
  always_comb begin
    case (sel)
      2'd0: picked = a;
      2'd1: picked = b;
      2'd2: picked = c;
      default: picked = d;
    endcase
  end
endmodule
