module g06_priority_decode(
  input  logic [7:0] request,
  output logic [2:0] index,
  output logic       valid
);
  always_comb begin
    index = 3'd0;
    valid = 1'b1;
    if      (request[7]) index = 3'd7;
    else if (request[6]) index = 3'd6;
    else if (request[5]) index = 3'd5;
    else if (request[4]) index = 3'd4;
    else if (request[3]) index = 3'd3;
    else if (request[2]) index = 3'd2;
    else if (request[1]) index = 3'd1;
    else if (request[0]) index = 3'd0;
    else                 valid = 1'b0;
  end
endmodule
