module g06_priority_decode(
  input  logic [7:0] request,
  output logic [2:0] index,
  output logic       valid
);
  always_comb begin
    valid = |request;
    index = 3'd0;
    if (|request[7:4]) begin
      if (|request[7:6])
        index = request[7] ? 3'd7 : 3'd6;
      else
        index = request[5] ? 3'd5 : 3'd4;
    end else if (|request[3:0]) begin
      if (|request[3:2])
        index = request[3] ? 3'd3 : 3'd2;
      else
        index = request[1] ? 3'd1 : 3'd0;
    end
  end
endmodule
