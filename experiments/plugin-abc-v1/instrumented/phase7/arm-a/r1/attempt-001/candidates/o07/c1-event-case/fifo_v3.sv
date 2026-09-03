// Candidate: make mutually exclusive occupancy events explicit.
`include "common_cells/assertions.svh"

module fifo_v3 #(
    parameter bit          FALL_THROUGH = 1'b0,
    parameter int unsigned DATA_WIDTH   = 32,
    parameter int unsigned DEPTH        = 8,
    parameter type dtype                = logic [DATA_WIDTH-1:0],
    parameter int unsigned ADDR_DEPTH   = (DEPTH > 1) ? $clog2(DEPTH) : 1
)(
    input  logic  clk_i,
    input  logic  rst_ni,
    input  logic  flush_i,
    input  logic  testmode_i,
    output logic  full_o,
    output logic  empty_o,
    output logic  [ADDR_DEPTH-1:0] usage_o,
    input  dtype  data_i,
    input  logic  push_i,
    output dtype  data_o,
    input  logic  pop_i
);
    localparam int unsigned FifoDepth = (DEPTH > 0) ? DEPTH : 1;
    logic gate_clock;
    logic push_event, pop_event;
    logic [ADDR_DEPTH - 1:0] read_pointer_n, read_pointer_q, write_pointer_n, write_pointer_q;
    logic [ADDR_DEPTH:0] status_cnt_n, status_cnt_q;
    dtype [FifoDepth - 1:0] mem_n, mem_q;

    assign usage_o = status_cnt_q[ADDR_DEPTH-1:0];

    if (DEPTH == 0) begin : gen_pass_through
        assign empty_o = ~push_i;
        assign full_o  = ~pop_i;
    end else begin : gen_fifo
        assign full_o  = (status_cnt_q == FifoDepth[ADDR_DEPTH:0]);
        assign empty_o = (status_cnt_q == 0) & ~(FALL_THROUGH & push_i);
    end

    assign push_event = push_i && ~full_o;
    assign pop_event  = pop_i && ~empty_o;

    always_comb begin : read_write_comb
        read_pointer_n  = read_pointer_q;
        write_pointer_n = write_pointer_q;
        status_cnt_n    = status_cnt_q;
        data_o          = (DEPTH == 0) ? data_i : mem_q[read_pointer_q];
        mem_n           = mem_q;
        gate_clock      = 1'b1;

        if (push_event) begin
            mem_n[write_pointer_q] = data_i;
            gate_clock = 1'b0;
            if (write_pointer_q == FifoDepth[ADDR_DEPTH-1:0] - 1)
                write_pointer_n = '0;
            else
                write_pointer_n = write_pointer_q + 1;
        end

        if (pop_event) begin
            if (read_pointer_n == FifoDepth[ADDR_DEPTH-1:0] - 1)
                read_pointer_n = '0;
            else
                read_pointer_n = read_pointer_q + 1;
        end

        unique case ({push_event, pop_event})
            2'b10: status_cnt_n = status_cnt_q + 1;
            2'b01: status_cnt_n = status_cnt_q - 1;
            default: status_cnt_n = status_cnt_q;
        endcase

        if (FALL_THROUGH && (status_cnt_q == 0) && push_i) begin
            data_o = data_i;
            if (pop_i) begin
                status_cnt_n = status_cnt_q;
                read_pointer_n = read_pointer_q;
                write_pointer_n = write_pointer_q;
            end
        end
    end

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if(~rst_ni) begin
            read_pointer_q  <= '0;
            write_pointer_q <= '0;
            status_cnt_q    <= '0;
        end else begin
            if (flush_i) begin
                read_pointer_q  <= '0;
                write_pointer_q <= '0;
                status_cnt_q    <= '0;
             end else begin
                read_pointer_q  <= read_pointer_n;
                write_pointer_q <= write_pointer_n;
                status_cnt_q    <= status_cnt_n;
            end
        end
    end

    always_ff @(posedge clk_i or negedge rst_ni) begin
        if(~rst_ni) begin
            mem_q <= {FifoDepth{dtype'('0)}};
        end else if (!gate_clock) begin
            mem_q <= mem_n;
        end
    end

`ifndef COMMON_CELLS_ASSERTS_OFF
    `ASSERT_INIT(depth_0, DEPTH > 0, "DEPTH must be greater than 0.")
    `ASSERT(full_write, full_o |-> ~push_i, clk_i, !rst_ni,
            "Trying to push new data although the FIFO is full.")
    `ASSERT(empty_read, empty_o |-> ~pop_i, clk_i, !rst_ni,
            "Trying to pop data although the FIFO is empty.")
`endif
endmodule
