`timescale 1ns/1ps
// Single-clock byte FIFO, including non-power-of-two depths.
// A simultaneous pop permits a push even when full. Output is stable if stalled.
module uart_byte_fifo #(
    parameter integer DEPTH = 16,
    parameter integer COUNT_W = $clog2(DEPTH + 1)
) (
    input logic clk, rst,
    input logic in_valid,
    output logic in_ready,
    input logic [7:0] in_data,
    output logic out_valid,
    input logic out_ready,
    output logic [7:0] out_data,
    output logic [COUNT_W-1:0] level
);
    localparam integer PTR_W = DEPTH <= 1 ? 1 : $clog2(DEPTH);
    logic [7:0] storage [0:DEPTH-1];
    logic [PTR_W-1:0] rd_ptr, wr_ptr;
    logic push, pop;
    assign out_valid = level != 0;
    assign out_data = storage[rd_ptr];
    assign pop = out_valid && out_ready;
    assign in_ready = level < DEPTH || pop;
    assign push = in_valid && in_ready;
    always_ff @(posedge clk) begin
        if (rst) begin
            rd_ptr <= 0; wr_ptr <= 0; level <= 0;
        end else begin
            if (push) begin
                storage[wr_ptr] <= in_data;
                wr_ptr <= wr_ptr == DEPTH-1 ? 0 : wr_ptr + 1'b1;
            end
            if (pop) rd_ptr <= rd_ptr == DEPTH-1 ? 0 : rd_ptr + 1'b1;
            case ({push, pop})
                2'b10: level <= level + 1'b1;
                2'b01: level <= level - 1'b1;
                default: ;
            endcase
        end
    end
    // synthesis translate_off
    initial if (DEPTH < 1 || COUNT_W < $clog2(DEPTH+1))
        $fatal(1, "Invalid FIFO depth/count width");
    // synthesis translate_on
endmodule
