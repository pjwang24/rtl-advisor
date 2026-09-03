module g06_priority_decode(
  input  logic [7:0] request,
  output logic [2:0] index,
  output logic       valid
);
  always_comb begin
    index = 3'd0;
    valid = |request;
    for (int i = 0; i < 8; i++) begin
      if (request[i]) index = 3'(i);
    end
  end
endmodule
