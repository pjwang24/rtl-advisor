module parity_minimal (
    input  logic a,
    input  logic b,
    output logic y
);
    assign y = a & b;
endmodule
