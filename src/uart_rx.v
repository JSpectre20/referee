/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * UART receiver, 8N1, LSB first.
 *
 * - `div` is the bit period in clock cycles (minimum 4).
 * - The input is synchronized and passed through a 3-sample majority
 *   filter, so single-cycle glitches are ignored.
 * - The start bit is re-checked at mid-bit; if the line is high again it
 *   was a glitch and the receiver goes back to idle.
 * - A low stop bit raises `frame_err` for one cycle, drops the byte, and
 *   waits for the line to go high again (so a long break is reported once).
 */

`default_nettype none

module uart_rx (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] div,
    input  wire        rx,
    output reg  [ 7:0] data,
    output reg         valid,
    output reg         frame_err
);

  // Synchronizer (s1, s2) followed by two history stages for the majority vote.
  reg s1, s2, s3, s4;
  always @(posedge clk) begin
    if (!rst_n) begin
      {s1, s2, s3, s4} <= 4'b1111;
    end else begin
      s1 <= rx;
      s2 <= s1;
      s3 <= s2;
      s4 <= s3;
    end
  end

  wire line = (s2 & s3) | (s2 & s4) | (s3 & s4);

  localparam [2:0] IDLE = 3'd0, START = 3'd1, DATA = 3'd2, STOP = 3'd3, WAIT_HIGH = 3'd4;

  reg [ 2:0] state;
  reg [15:0] cnt;
  reg [ 2:0] nbit;
  reg [ 7:0] shreg;

  // The filter delays the line by about 3 cycles, and the countdown adds one
  // more, so start at div/2 - 2 to land the sample in the middle of each bit.
  wire [15:0] half = (div >> 1) - 16'd2;

  always @(posedge clk) begin
    valid     <= 1'b0;
    frame_err <= 1'b0;
    if (!rst_n) begin
      state <= IDLE;
      cnt   <= 16'd0;
      nbit  <= 3'd0;
      shreg <= 8'd0;
      data  <= 8'd0;
    end else begin
      case (state)
        IDLE: begin
          if (!line) begin
            state <= START;
            cnt   <= half;
          end
        end

        START: begin
          if (cnt != 16'd0) begin
            cnt <= cnt - 16'd1;
          end else if (!line) begin
            state <= DATA;
            cnt   <= div - 16'd1;
            nbit  <= 3'd0;
          end else begin
            state <= IDLE;  // glitch, not a start bit
          end
        end

        DATA: begin
          if (cnt != 16'd0) begin
            cnt <= cnt - 16'd1;
          end else begin
            shreg <= {line, shreg[7:1]};
            cnt   <= div - 16'd1;
            nbit  <= nbit + 3'd1;
            if (nbit == 3'd7) state <= STOP;
          end
        end

        STOP: begin
          if (cnt != 16'd0) begin
            cnt <= cnt - 16'd1;
          end else if (line) begin
            data  <= shreg;
            valid <= 1'b1;
            state <= IDLE;  // back to idle at mid-stop-bit, ready for the next start
          end else begin
            frame_err <= 1'b1;
            state     <= WAIT_HIGH;
          end
        end

        WAIT_HIGH: begin
          if (line) state <= IDLE;
        end

        default: state <= IDLE;
      endcase
    end
  end

endmodule
