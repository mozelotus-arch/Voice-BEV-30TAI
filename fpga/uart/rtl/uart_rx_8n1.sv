`timescale 1ns/1ps
// 2-FF input synchronizer, fractional 16x enable, three center-sample majority.
// Invalid stops always discard the byte and increment raw_invalid_stop.
// A continuous all-zero interval >= a complete 8N1 frame is reported as BREAK;
// other invalid stops remain framing errors. Raw evidence is retained separately.
module uart_rx_8n1 #(
    parameter integer CLK_HZ = 100000000,
    parameter integer BAUD = 115200
) (
    input logic clk, rst,
    input logic rx,
    output logic [7:0] out_data,
    output logic out_valid, framing_error, busy,
    output logic break_event, raw_invalid_stop,
    output logic [7:0] last_invalid_data,
    output logic [31:0] low_at_invalid, last_invalid_low_width,
    output logic [159:0] invalid_trace,
    output logic synchronized_rx, error_pending
);
    localparam integer ACC_W = $clog2(CLK_HZ);
    localparam integer SAMPLE_HZ = BAUD*16;
    localparam integer BREAK_CLOCKS = (CLK_HZ/BAUD)*10 + (((CLK_HZ%BAUD)*10+BAUD-1)/BAUD);
    (* ASYNC_REG = "TRUE" *) logic rx_meta, rx_sync;
    logic [ACC_W-1:0] phase;
    logic [ACC_W:0] sum;
    logic sample_tick;
    typedef enum logic [2:0] {WAIT_HIGH, IDLE, START, DATA, STOP} state_t;
    state_t state;
    logic [3:0] tick_count;
    logic [2:0] bit_index;
    logic [1:0] votes;
    logic voted_bit;
    logic [7:0] shift_data;
    logic [31:0] low_clocks, last_low_width;
    logic [159:0] trace_shift;
    logic pending_is_break;
    assign sum = {1'b0, phase} + SAMPLE_HZ;
    assign sample_tick = sum >= CLK_HZ;
    assign voted_bit = (votes[0] && votes[1]) ||
                       (votes[0] && rx_sync) || (votes[1] && rx_sync);
    assign busy = state != IDLE;
    assign synchronized_rx = rx_sync;
    always_ff @(posedge clk) begin
        if (rst) begin
            rx_meta <= 1; rx_sync <= 1;
            phase <= 0;
            state <= WAIT_HIGH; tick_count <= 0; bit_index <= 0;
            votes <= 0; shift_data <= 0;
            out_data <= 0; out_valid <= 0; framing_error <= 0;
            break_event <= 0; raw_invalid_stop <= 0;
            last_invalid_data <= 0; low_at_invalid <= 0; last_invalid_low_width <= 0;
            low_clocks <= 0; last_low_width <= 0;
            trace_shift <= '1; invalid_trace <= '1;
            error_pending <= 0; pending_is_break <= 0;
        end else begin
            rx_meta <= rx;
            rx_sync <= rx_meta;
            phase <= sample_tick ? sum - CLK_HZ : sum;
            out_valid <= 0;
            framing_error <= 0;
            break_event <= 0; raw_invalid_stop <= 0;
            if (!rx_sync) begin
                if (low_clocks != 32'hffffffff) low_clocks <= low_clocks + 1'b1;
            end else begin
                if (low_clocks != 0) last_low_width <= low_clocks;
                low_clocks <= 0;
            end
            if (sample_tick) trace_shift <= {trace_shift[158:0],rx_sync};
            if (error_pending) begin
                if (!pending_is_break && last_invalid_data == 0 &&
                    low_clocks >= BREAK_CLOCKS) begin
                    break_event <= 1;
                    pending_is_break <= 1;
                end
                if (rx_sync) begin
                    last_invalid_low_width <= low_clocks != 0 ? low_clocks : last_low_width;
                    if (!pending_is_break && !(last_invalid_data == 0 && low_clocks >= BREAK_CLOCKS))
                        framing_error <= 1;
                    error_pending <= 0;
                end
            end
            if (sample_tick) case (state)
                WAIT_HIGH: begin
                    if (!rx_sync) tick_count <= 0;
                    else if (tick_count == 15) begin state <= IDLE; tick_count <= 0; end
                    else tick_count <= tick_count + 1'b1;
                end
                IDLE: if (!rx_sync) begin
                    state <= START;
                    tick_count <= 0;
                    votes <= 0;
                end
                START: begin
                    tick_count <= tick_count + 1'b1;
                    if (tick_count == 6) votes[0] <= rx_sync;
                    if (tick_count == 7) votes[1] <= rx_sync;
                    if (tick_count == 8) begin
                        tick_count <= 0;
                        bit_index <= 0;
                        state <= voted_bit ? IDLE : DATA;
                    end
                end
                DATA, STOP: begin
                    tick_count <= tick_count + 1'b1;
                    if (tick_count == 13) votes[0] <= rx_sync;
                    if (tick_count == 14) votes[1] <= rx_sync;
                    if (tick_count == 15) begin
                        tick_count <= 0;
                        if (state == DATA) begin
                            shift_data[bit_index] <= voted_bit;
                            if (bit_index == 7) state <= STOP;
                            else bit_index <= bit_index + 1'b1;
                        end else if (voted_bit) begin
                            out_data <= shift_data;
                            out_valid <= 1;
                            state <= IDLE;
                        end else begin
                            raw_invalid_stop <= 1;
                            last_invalid_data <= shift_data;
                            low_at_invalid <= low_clocks;
                            invalid_trace <= {trace_shift[158:0],rx_sync};
                            error_pending <= 1;
                            pending_is_break <= 0;
                            state <= WAIT_HIGH;
                        end
                    end
                end
                default: state <= WAIT_HIGH;
            endcase
        end
    end
    // synthesis translate_off
    initial if (BAUD <= 0 || CLK_HZ < BAUD*32 || CLK_HZ > 1000000000)
        $fatal(1, "UART requires 32+ clocks/bit, positive baud, CLK_HZ <= 1GHz");
    // synthesis translate_on
endmodule
