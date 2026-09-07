module g06_priority_decode(
  input  logic [7:0] request,
  output logic [2:0] index,
  output logic       valid
);
  always_comb begin
    valid = 1'b1;
    casez (request)
      8'b1???????: index = 3'd7;
      8'b01??????: index = 3'd6;
      8'b001?????: index = 3'd5;
      8'b0001????: index = 3'd4;
      8'b00001???: index = 3'd3;
      8'b000001??: index = 3'd2;
      8'b0000001?: index = 3'd1;
      8'b00000001: index = 3'd0;
      default: begin
        index = 3'd0;
        valid = 1'b0;
      end
    endcase
  end
endmodule
