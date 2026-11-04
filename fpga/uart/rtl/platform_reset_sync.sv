`timescale 1ns/1ps
module platform_reset_sync(input logic clk, async_reset, output logic reset_out);
    (* ASYNC_REG = "TRUE" *) logic [2:0] reset_pipe = 3'b111;
    always_ff @(posedge clk or posedge async_reset)
        if (async_reset) reset_pipe <= 3'b111;
        else reset_pipe <= {reset_pipe[1:0],1'b0};
    assign reset_out = reset_pipe[2];
endmodule
