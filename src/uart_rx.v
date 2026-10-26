/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * uart_rx: UART receiver, 8N1, LSB first, fixed baud.
 *
 * How it samples
 * --------------
 *   - `rx` is asynchronous, so it first passes a 2-flop synchronizer
 *     (s1, s2). Nothing but s1 ever looks at the raw pin.
 *   - The receiver waits in IDLE for the synchronized line to go low. That
 *     cycle is "cycle 0" of the frame; every bit then lasts CLKS_PER_BIT
 *     cycles counted from it.
 *   - Each bit is decided by a majority vote of three consecutive samples
 *     placed around the middle of the bit, so a single-cycle spike on the
 *     line cannot flip a bit. The vote is taken at cycle MID of each bit;
 *     MID = (CLKS_PER_BIT+1)/2 compensates for the ~2 cycles the
 *     synchronizer adds and for the vote window, which puts the centre of the
 *     three samples at the centre of the bit.
 *   - Start-bit check: if the vote at the middle of the start bit says the
 *     line is high again, the low pulse was a glitch shorter than about half
 *     a bit, and the receiver returns to IDLE without reporting anything.
 *   - Stop bit: high -> `valid` pulses for one cycle with `data`; low ->
 *     `frame_err` pulses for one cycle, the byte is dropped, and the receiver
 *     waits for the line to go high before looking for a new start bit (so a
 *     long break is reported once, not as a string of zero bytes).
 *   - The receiver returns to IDLE in the middle of the stop bit, so a sender
 *     whose clock is a little fast (its next start bit comes early) is fine.
 *     Tolerated baud error is about +-4% at 347 cycles per bit (tested at
 *     +-3%).
 */

`default_nettype none

module uart_rx #(
    parameter integer CLKS_PER_BIT = 347  // 40 MHz / 115200 baud; minimum 6
) (
    input  wire       clk,
    input  wire       rst_n,      // synchronous, active low
    input  wire       rx,         // asynchronous serial input
    output reg  [7:0] data,       // last good byte (held until the next one)
    output reg        valid,      // one-cycle pulse: `data` is new
    output reg        frame_err   // one-cycle pulse: stop bit was low
);

  localparam integer CW = $clog2(CLKS_PER_BIT);
  localparam [CW-1:0] LAST = CLKS_PER_BIT[CW-1:0] - 1'b1;
  localparam integer MIDI = (CLKS_PER_BIT + 1) / 2;
  localparam [CW-1:0] MID = MIDI[CW-1:0];

  // Synchronizer, then two more history flops for the 3-sample vote.
  // Reset to 1: the idle level, so leaving reset is not a start bit.
  reg s1, s2, h1, h2;
  always @(posedge clk) begin
    if (!rst_n) {s1, s2, h1, h2} <= 4'b1111;
    else {s1, s2, h1, h2} <= {rx, s1, s2, h1};
  end
  wire vote = (s2 & h1) | (s2 & h2) | (h1 & h2);  // majority of the last 3 samples

  localparam [2:0] IDLE = 3'd0, START = 3'd1, DATA = 3'd2, STOP = 3'd3, BREAK = 3'd4;

  reg [   2:0] state;
  reg [CW-1:0] cnt;    // cycle within the current bit
  reg [   2:0] idx;    // data bit being received
  reg [   7:0] shreg;

  wire bit_end = (cnt == LAST);
  wire at_mid = (cnt == MID);

  always @(posedge clk) begin
    valid     <= 1'b0;  // pulses: low unless set below
    frame_err <= 1'b0;
    if (!rst_n) begin
      state <= IDLE;
      cnt   <= {CW{1'b0}};
      idx   <= 3'd0;
      shreg <= 8'd0;
      data  <= 8'd0;
    end else begin
      if (state == IDLE || bit_end) cnt <= {CW{1'b0}};
      else cnt <= cnt + 1'b1;

      case (state)
        IDLE: if (!s2) begin
          // First synchronized low: this cycle is cycle 0 of the start bit.
          cnt   <= {{(CW - 1) {1'b0}}, 1'b1};
          state <= START;
        end

        START: begin
          if (at_mid && vote) state <= IDLE;  // high again at mid-bit: a glitch
          else if (bit_end) begin
            idx   <= 3'd0;
            state <= DATA;
          end
        end

        DATA: begin
          if (at_mid) shreg <= {vote, shreg[7:1]};  // LSB arrives first
          if (bit_end) begin
            if (idx == 3'd7) state <= STOP;
            idx <= idx + 3'd1;
          end
        end

        STOP:
        if (at_mid) begin
          if (vote) begin
            data  <= shreg;
            valid <= 1'b1;
            state <= IDLE;
          end else begin
            frame_err <= 1'b1;
            state     <= BREAK;
          end
        end

        BREAK: if (s2 && h1) state <= IDLE;  // line high again (2 samples)

        default: state <= IDLE;
      endcase
    end
  end

endmodule

`default_nettype wire
