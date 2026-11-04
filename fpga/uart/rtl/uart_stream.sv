`timescale 1ns/1ps
// Byte-level integration boundary. No AXI/APB/CPU driver or command parser.
module uart_stream #(
    parameter integer CLK_HZ = 100000000,
    parameter integer BAUD = 115200,
    parameter integer TX_DEPTH = 16,
    parameter integer RX_DEPTH = 16
) (
    input logic clk, rst,
    input logic serial_rx,
    output logic serial_tx,
    input logic [7:0] tx_data,
    input logic tx_valid,
    output logic tx_ready,
    output logic [7:0] rx_data,
    output logic rx_valid,
    input logic rx_ready,
    output logic tx_busy, rx_busy,
    output logic [$clog2(TX_DEPTH+1)-1:0] tx_level,
    output logic [$clog2(RX_DEPTH+1)-1:0] rx_level,
    output logic rx_framing_error, rx_overrun,
    output logic [31:0] framing_error_count, overrun_count,
    output logic [31:0] break_count, invalid_stop_count,
    output logic [7:0] last_invalid_data,
    output logic [31:0] low_at_invalid, last_invalid_low_width,
    output logic [159:0] invalid_trace,
    output logic synchronized_rx, error_pending,
    input logic clear_stats
);
    logic [7:0] queued_tx, received_byte;
    logic queued_valid, queued_ready, core_tx_busy, received_valid, fifo_rx_ready;
    logic break_event, raw_invalid_stop;
    uart_byte_fifo #(.DEPTH(TX_DEPTH)) tx_fifo(
        .clk, .rst, .in_valid(tx_valid), .in_ready(tx_ready), .in_data(tx_data),
        .out_valid(queued_valid), .out_ready(queued_ready), .out_data(queued_tx), .level(tx_level)
    );
    uart_tx_8n1 #(.CLK_HZ(CLK_HZ), .BAUD(BAUD)) transmitter(
        .clk, .rst, .in_valid(queued_valid), .in_ready(queued_ready),
        .in_data(queued_tx), .tx(serial_tx), .busy(core_tx_busy)
    );
    uart_rx_8n1 #(.CLK_HZ(CLK_HZ), .BAUD(BAUD)) receiver(
        .clk, .rst, .rx(serial_rx), .out_data(received_byte), .out_valid(received_valid),
        .framing_error(rx_framing_error), .busy(rx_busy), .break_event, .raw_invalid_stop,
        .last_invalid_data, .low_at_invalid, .last_invalid_low_width, .invalid_trace,
        .synchronized_rx, .error_pending
    );
    uart_byte_fifo #(.DEPTH(RX_DEPTH)) rx_fifo(
        .clk, .rst, .in_valid(received_valid), .in_ready(fifo_rx_ready), .in_data(received_byte),
        .out_valid(rx_valid), .out_ready(rx_ready), .out_data(rx_data), .level(rx_level)
    );
    assign tx_busy = core_tx_busy || queued_valid;
    always_ff @(posedge clk) begin
        if (rst) begin
            rx_overrun <= 0;
            framing_error_count <= 0; overrun_count <= 0;
            break_count <= 0; invalid_stop_count <= 0;
        end else begin
            rx_overrun <= received_valid && !fifo_rx_ready;
            if (clear_stats) begin
                framing_error_count <= 0; overrun_count <= 0;
                break_count <= 0; invalid_stop_count <= 0;
            end else begin
                if (rx_framing_error && framing_error_count != 32'hffffffff)
                    framing_error_count <= framing_error_count + 1'b1;
                if (break_event && break_count != 32'hffffffff) break_count <= break_count + 1'b1;
                if (raw_invalid_stop && invalid_stop_count != 32'hffffffff)
                    invalid_stop_count <= invalid_stop_count + 1'b1;
                if (received_valid && !fifo_rx_ready && overrun_count != 32'hffffffff)
                    overrun_count <= overrun_count + 1'b1;
            end
        end
    end
endmodule
