`timescale 1ns/1ps
// ABI v3: nonblocking TX submission; full FIFO drops are explicit and counted.
// Offsets are LOCAL BYTES. RX snapshots/pops at AR, even with stalled RREADY.
module uart_user_reg #(
    parameter integer CLK_HZ=100000000, BAUD=9600, TX_DEPTH=128, RX_DEPTH=256
) (
    input logic clk, rst, serial_rx,
    output logic serial_tx, irq,
    input logic [31:0] awwaddr, awwdata,
    input logic awwvalid,
    output logic awwready,
    input logic [31:0] araddr,
    input logic arvalid,
    output logic arready,
    output logic [31:0] rdata,
    output logic rvalid,
    input logic rready
);
    logic tx_ready, rx_valid, rx_ready, tx_busy, rx_busy;
    logic [7:0] rx_data;
    logic [$clog2(TX_DEPTH+1)-1:0] tx_level;
    logic [$clog2(RX_DEPTH+1)-1:0] rx_level;
    logic [31:0] framing_count, overrun_count, bad_writes, bad_reads;
    logic [31:0] break_count, invalid_stop_count, low_at_invalid, last_invalid_low_width;
    logic [7:0] last_invalid_data;
    logic [159:0] invalid_trace;
    logic synchronized_rx, error_pending;
    logic [2:0] irq_mask, irq_status;
    logic write_fire, read_fire, clear_stats, soft_reset;
    logic [31:0] tx_accept_count, tx_drop_count, reset_epoch;
    logic tx_overflow;
    assign awwready = !rst;
    assign arready = !rst && !rvalid;
    assign write_fire = awwvalid && awwready;
    assign read_fire = arvalid && arready;
    assign clear_stats = write_fire && awwaddr == 32'h10 && awwdata[0];
    assign soft_reset = write_fire && awwaddr == 32'h10 && awwdata[1];
    assign rx_ready = read_fire && araddr == 32'h04;
    assign irq_status = {(invalid_stop_count != 0 || overrun_count != 0 || tx_overflow), tx_ready, rx_valid};
    assign irq = !rst && |(irq_status & irq_mask);
    uart_stream #(.CLK_HZ(CLK_HZ),.BAUD(BAUD),.TX_DEPTH(TX_DEPTH),.RX_DEPTH(RX_DEPTH)) core (
        .clk, .rst(rst || soft_reset), .serial_rx, .serial_tx,
        .tx_data(awwdata[7:0]), .tx_valid(write_fire && awwaddr == 0 && tx_ready), .tx_ready,
        .rx_data, .rx_valid, .rx_ready, .tx_busy, .rx_busy, .tx_level, .rx_level,
        .rx_framing_error(), .rx_overrun(), .framing_error_count(framing_count),
        .overrun_count, .clear_stats, .break_count, .invalid_stop_count,
        .last_invalid_data, .low_at_invalid, .last_invalid_low_width, .invalid_trace,
        .synchronized_rx, .error_pending
    );
    always_ff @(posedge clk) begin
        if (rst) begin
            rvalid <= 0; rdata <= 0; irq_mask <= 0; bad_writes <= 0; bad_reads <= 0;
            tx_accept_count <= 0; tx_drop_count <= 0; tx_overflow <= 0; reset_epoch <= 0;
        end else begin
            if (rvalid && rready) rvalid <= 0;
            if (write_fire) case (awwaddr)
                32'h00: begin
                    if (tx_ready) tx_accept_count <= tx_accept_count + 1'b1;
                    else begin
                        tx_overflow <= 1;
                        if (tx_drop_count != 32'hffffffff) tx_drop_count <= tx_drop_count + 1'b1;
                    end
                end
                32'h10: begin end
                32'h20: irq_mask <= awwdata[2:0];
                default: if (bad_writes != 32'hffffffff) bad_writes <= bad_writes + 1'b1;
            endcase
            if (read_fire) begin
                rvalid <= 1;
                case (araddr)
                    32'h04: rdata <= {23'b0,rx_valid,(rx_valid ? rx_data : 8'b0)};
                    32'h08: rdata <= {28'b0,rx_busy,tx_busy,rx_valid,tx_ready};
                    32'h0c: rdata <= 32'h55415203;
                    32'h10: rdata <= 0;
                    32'h14: rdata <= framing_count;
                    32'h18: rdata <= overrun_count;
                    32'h1c: rdata <= (32'(rx_level) << 16) | 32'(tx_level);
                    32'h20: rdata <= {29'b0,irq_mask};
                    32'h24: rdata <= {29'b0,irq_status};
                    32'h28: rdata <= CLK_HZ;
                    32'h2c: rdata <= BAUD;
                    32'h30: rdata <= bad_writes;
                    32'h34: rdata <= bad_reads;
                    32'h38: rdata <= break_count;
                    32'h3c: rdata <= invalid_stop_count;
                    32'h40: rdata <= {22'b0,error_pending,synchronized_rx,last_invalid_data};
                    32'h44: rdata <= low_at_invalid;
                    32'h48: rdata <= last_invalid_low_width;
                    32'h50: rdata <= invalid_trace[31:0];
                    32'h54: rdata <= invalid_trace[63:32];
                    32'h58: rdata <= invalid_trace[95:64];
                    32'h5c: rdata <= invalid_trace[127:96];
                    32'h60: rdata <= invalid_trace[159:128];
                    32'h64: rdata <= TX_DEPTH - 32'(tx_level);
                    32'h68: rdata <= tx_accept_count;
                    32'h6c: rdata <= tx_drop_count;
                    32'h70: rdata <= {31'b0,tx_overflow};
                    32'h74: rdata <= reset_epoch;
                    32'h78: rdata <= (RX_DEPTH << 16) | TX_DEPTH;
                    default: begin
                        rdata <= 32'hdec0ad03;
                        if (bad_reads != 32'hffffffff) bad_reads <= bad_reads + 1'b1;
                    end
                endcase
            end
            if (clear_stats || soft_reset) begin bad_writes <= 0; bad_reads <= 0; end
            if (clear_stats) begin tx_accept_count <= 0; tx_drop_count <= 0; tx_overflow <= 0; end
            // Reset flushes queues/serial activity, but cannot revoke a captured R.
            // Accepted means queued, not delivered; epoch makes an abort observable.
            if (soft_reset) begin irq_mask <= 0; reset_epoch <= reset_epoch + 1'b1; end
        end
    end
endmodule
