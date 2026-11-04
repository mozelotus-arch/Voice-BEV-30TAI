`timescale 1ns/1ps
// Fractional baud accumulator, no generated clock. 8 data bits, LSB first.
// When data is available at the end of STOP, the next START follows immediately.
module uart_tx_8n1 #(
    parameter integer CLK_HZ = 100000000,
    parameter integer BAUD = 115200
) (
    input logic clk, rst,
    input logic in_valid,
    output logic in_ready,
    input logic [7:0] in_data,
    output logic tx,
    output logic busy
);
    localparam integer ACC_W = $clog2(CLK_HZ);
    logic [ACC_W-1:0] phase;
    logic [ACC_W:0] sum;
    logic bit_end;
    logic [9:0] frame;
    logic [3:0] bit_index;
    assign sum = {1'b0, phase} + BAUD;
    assign bit_end = sum >= CLK_HZ;
    assign in_ready = !busy || (bit_index == 9 && bit_end);
    assign tx = busy ? frame[bit_index] : 1'b1;
    always_ff @(posedge clk) begin
        if (rst) begin
            busy <= 0; phase <= 0; frame <= 10'h3ff; bit_index <= 0;
        end else if (!busy) begin
            phase <= 0;
            if (in_valid) begin
                frame <= {1'b1, in_data, 1'b0};
                bit_index <= 0;
                busy <= 1;
            end
        end else begin
            phase <= bit_end ? sum - CLK_HZ : sum;
            if (bit_end) begin
                if (bit_index == 9) begin
                    if (in_valid) begin
                        frame <= {1'b1, in_data, 1'b0};
                        bit_index <= 0;
                    end else busy <= 0;
                end else bit_index <= bit_index + 1'b1;
            end
        end
    end
    // synthesis translate_off
    initial if (BAUD <= 0 || CLK_HZ < BAUD*32 || CLK_HZ > 1000000000)
        $fatal(1, "UART requires 32+ clocks/bit, positive baud, CLK_HZ <= 1GHz");
    // synthesis translate_on
endmodule
