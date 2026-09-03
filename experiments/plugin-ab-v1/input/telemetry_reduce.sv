// Generated held-out input for the RTL Advisor Codex A/B evaluation.
// This file is not derived from proprietary or third-party RTL.
module telemetry_reduce (
  input  logic [17:0] sample0,
  input  logic [17:0] sample1,
  input  logic [17:0] sample2,
  input  logic [17:0] sample3,
  input  logic [17:0] sample4,
  input  logic [17:0] sample5,
  input  logic [17:0] threshold,
  input  logic [5:0]  valid,
  output logic [20:0] sum,
  output logic        any_over,
  output logic [2:0]  first_over
);
  logic [20:0] term0;
  logic [20:0] term1;
  logic [20:0] term2;
  logic [20:0] term3;
  logic [20:0] term4;
  logic [20:0] term5;
  logic [5:0]  over;

  assign term0 = {{3{1'b0}}, sample0};
  assign term1 = {{3{1'b0}}, sample1};
  assign term2 = {{3{1'b0}}, sample2};
  assign term3 = {{3{1'b0}}, sample3};
  assign term4 = {{3{1'b0}}, sample4};
  assign term5 = {{3{1'b0}}, sample5};

  assign sum = term0 + term1 + term2 + term3 + term4 + term5;

  assign over[0] = valid[0] && (sample0 > threshold);
  assign over[1] = valid[1] && (sample1 > threshold);
  assign over[2] = valid[2] && (sample2 > threshold);
  assign over[3] = valid[3] && (sample3 > threshold);
  assign over[4] = valid[4] && (sample4 > threshold);
  assign over[5] = valid[5] && (sample5 > threshold);
  assign any_over = |over;

  always_comb begin
    first_over = 3'd0;
    if (over[5])
      first_over = 3'd5;
    else if (over[4])
      first_over = 3'd4;
    else if (over[3])
      first_over = 3'd3;
    else if (over[2])
      first_over = 3'd2;
    else if (over[1])
      first_over = 3'd1;
  end
endmodule
