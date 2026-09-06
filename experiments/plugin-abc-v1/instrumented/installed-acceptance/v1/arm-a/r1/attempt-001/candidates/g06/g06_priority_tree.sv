module g06_priority_decode(
  input  logic [7:0] request,
  output logic [2:0] index,
  output logic       valid
);
  logic upper_half;
  logic upper_pair;

  always_comb begin
    valid = |request;
    upper_half = |request[7:4];
    upper_pair = upper_half ? |request[7:6] : |request[3:2];

    if (upper_half) begin
      if (upper_pair)
        index = request[7] ? 3'd7 : 3'd6;
      else
        index = request[5] ? 3'd5 : 3'd4;
    end else begin
      if (upper_pair)
        index = request[3] ? 3'd3 : 3'd2;
      else
        index = request[1] ? 3'd1 : 3'd0;
    end
  end
endmodule
