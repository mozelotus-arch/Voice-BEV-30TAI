`timescale 1ns/1ps
module factory_uart_bridge #(
    parameter integer CLK_HZ=100000000, BAUD=9600, TX_DEPTH=128, RX_DEPTH=256
) (
    input logic clk, rst, adder_reset_async, serial_rx,
    output logic serial_tx,
    input logic [31:0] awwaddr, awwdata, araddr,
    input logic awwvalid, arvalid, rready,
    output logic awwready, arready, rvalid,
    output logic [31:0] rdata,
    output logic [31:0] adder_awwaddr, adder_awwdata, adder_araddr,
    output logic adder_awwvalid, adder_arvalid, adder_rready,
    input logic adder_awwready, adder_arready, adder_rvalid,
    input logic [31:0] adder_rdata
);
    logic [31:0] uart_awwaddr, uart_awwdata, uart_araddr, uart_rdata;
    logic uart_awwvalid, uart_awwready, uart_arvalid, uart_arready, uart_rvalid, uart_rready;
    factory_user_reg_router router(.*);
    uart_user_reg #(.CLK_HZ(CLK_HZ),.BAUD(BAUD),.TX_DEPTH(TX_DEPTH),.RX_DEPTH(RX_DEPTH)) uart (
        .clk, .rst, .serial_rx, .serial_tx, .irq(),
        .awwaddr(uart_awwaddr), .awwdata(uart_awwdata), .awwvalid(uart_awwvalid), .awwready(uart_awwready),
        .araddr(uart_araddr), .arvalid(uart_arvalid), .arready(uart_arready),
        .rdata(uart_rdata), .rvalid(uart_rvalid), .rready(uart_rready)
    );
endmodule
