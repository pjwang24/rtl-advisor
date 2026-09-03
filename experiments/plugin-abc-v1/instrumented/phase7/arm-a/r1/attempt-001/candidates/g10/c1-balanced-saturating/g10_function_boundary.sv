module g10_function_boundary(
  input  logic [7:0] a, b, c, d,
  output logic [7:0] y
);
  function automatic logic [7:0] sat_add(
    input logic [7:0] lhs,
    input logic [7:0] rhs
  );
    logic [8:0] wide;
    begin
      wide = lhs + rhs;
      sat_add = wide[8] ? 8'hff : wide[7:0];
    end
  endfunction
  assign y = sat_add(sat_add(a, b), sat_add(c, d));
endmodule
