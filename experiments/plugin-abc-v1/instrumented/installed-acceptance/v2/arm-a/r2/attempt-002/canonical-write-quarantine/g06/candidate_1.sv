module g06_priority_decode(
  input  logic [7:0] request,
  output logic [2:0] index,
  output logic       valid
);
  always_comb begin
    index = 3'd0;
    valid = 1'b1;
    case (1'b1)
      request[7]: index = 3'd7;
      request[6]: index = 3'd6;
      request[5]: index = 3'd5;
      request[4]: index = 3'd4;
      request[3]: index = 3'd3;
      request[2]: index = 3'd2;
      request[1]: index = 3'd1;
      request[0]: index = 3'd0;
      default:    valid = 1'b0;
    endcase
  end
endmodule
