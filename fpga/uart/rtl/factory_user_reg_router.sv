`timescale 1ns/1ps
// Combined address/data write channel. No write queue and no UART backpressure.
// One outstanding AR, with a router-owned R snapshot that survives endpoint reset.
// 400C..400E keep complete original byte addresses; only 400F becomes local.
module factory_user_reg_router (
    input logic clk, rst, adder_reset_async,
    input logic [31:0] awwaddr, awwdata, araddr,
    input logic awwvalid, arvalid, rready,
    output logic awwready, arready, rvalid,
    output logic [31:0] rdata,
    output logic [31:0] adder_awwaddr, adder_awwdata, adder_araddr,
    output logic adder_awwvalid, adder_arvalid, adder_rready,
    input logic adder_awwready, adder_arready, adder_rvalid,
    input logic [31:0] adder_rdata,
    output logic [31:0] uart_awwaddr, uart_awwdata, uart_araddr,
    output logic uart_awwvalid, uart_arvalid, uart_rready,
    input logic uart_awwready, uart_arready, uart_rvalid,
    input logic [31:0] uart_rdata
);
    logic rd_wait, rd_uart, adder_reset_command;
    // Mirror the endpoint's asynchronous reset assertion immediately, and delay
    // release for two GP clocks. A level synchronizer would leave an assert gap.
    (* ASYNC_REG = "TRUE" *) logic [1:0] adder_reset_sync = 2'b11;
    logic [3:0] reset_write_guard;
    wire w_uart = awwaddr[31:16] == 16'h400f;
    wire r_uart = araddr[31:16] == 16'h400f;
    wire w_adder = awwaddr[31:18] == (32'h400c0000 >> 18) && !w_uart;
    wire r_adder = araddr[31:18] == (32'h400c0000 >> 18) && !r_uart;
    wire reset_write = w_adder && awwaddr[17:2] == 16'h0077;
    // Only assertion can destroy an uncollected adder response. Deassertion must
    // remain possible even if an old read is pending; never lock the recovery write.
    wire defer_reset_assert = reset_write && awwdata[0] && rd_wait && !rd_uart;
    wire adder_in_reset = adder_reset_command || adder_reset_sync[1] || |reset_write_guard;
    wire same_target_write = awwvalid && ((w_uart && r_uart) || (w_adder && r_adder));
    wire read_slot = !rst && !rd_wait && !rvalid && !same_target_write;

    assign adder_awwaddr = awwaddr;
    assign adder_awwdata = awwdata;
    assign adder_araddr = araddr;
    assign uart_awwaddr = {16'b0,awwaddr[15:0]};
    assign uart_awwdata = awwdata;
    assign uart_araddr = {16'b0,araddr[15:0]};
    assign adder_awwvalid = !rst && w_adder && !defer_reset_assert && awwvalid;
    assign uart_awwvalid = !rst && w_uart && awwvalid;
    assign awwready = !rst && (w_uart ? uart_awwready :
                              w_adder ? (!defer_reset_assert && adder_awwready) : 1'b1);
    assign adder_arvalid = read_slot && r_adder && !adder_in_reset && arvalid;
    assign uart_arvalid = read_slot && r_uart && arvalid;
    assign arready = read_slot && (r_uart ? uart_arready :
                                  r_adder && !adder_in_reset ? adder_arready : 1'b1);
    // Only the AR-selected endpoint can retire its response.
    assign uart_rready = !rst && rd_wait && rd_uart;
    assign adder_rready = !rst && rd_wait && !rd_uart;
    always_ff @(posedge clk or posedge adder_reset_async) begin
        if (adder_reset_async) adder_reset_sync <= 2'b11;
        else if (rst) adder_reset_sync <= 2'b11;
        else adder_reset_sync <= {adder_reset_sync[0],1'b0};
    end
    always_ff @(posedge clk) begin
        if (rst) begin
            rd_wait <= 0; rd_uart <= 0; rvalid <= 0; rdata <= 0;
            adder_reset_command <= 0; reset_write_guard <= 4'b1111;
        end else begin
            reset_write_guard <= {reset_write_guard[2:0],1'b0};
            if (awwvalid && awwready && reset_write) begin
                adder_reset_command <= awwdata[0];
                reset_write_guard <= 4'b1111;
            end
            if (rvalid && rready) rvalid <= 0;
            if (arvalid && arready) begin
                if (!r_uart && !r_adder) begin
                    rvalid <= 1; rdata <= 32'hdec0ad00;
                end else if (r_adder && adder_in_reset) begin
                    rvalid <= 1; rdata <= 32'hadde0001;
                end else begin
                    rd_wait <= 1; rd_uart <= r_uart;
                end
            end
            if (rd_wait && (rd_uart ? uart_rvalid : adder_rvalid)) begin
                rdata <= rd_uart ? uart_rdata : adder_rdata;
                rvalid <= 1; rd_wait <= 0;
            end
        end
    end
endmodule
