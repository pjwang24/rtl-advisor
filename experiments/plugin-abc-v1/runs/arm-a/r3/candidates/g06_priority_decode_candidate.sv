module g06_priority_decode_candidate(
  input  logic [7:0] request,
  output logic [2:0] index,
  output logic       valid
);
  logic upper_half;

  assign valid      = |request;
  assign upper_half = |request[7:4];
  assign index[2]   = upper_half;
  assign index[1]   = request[7] | request[6] |
                      (~upper_half & (request[3] | request[2]));
  assign index[0]   = request[7] |
                      (~request[6] & request[5]) |
                      (~|request[7:4] & request[3]) |
                      (~|request[7:2] & request[1]);
endmodule
