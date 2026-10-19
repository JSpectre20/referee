/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * UART transmitter, 8N1, LSB first, with a one-byte holding register.
 *
 * - `div` is the bit period in clock cycles.
 * - Write a byte with `wr_en` while `hold_empty` is high. The holding
 *   register lets the next byte wait while the current one is sent, so
 *   bytes go out back to back with no gap.
 * - `idle` is high when nothing is waiting and the line has finished the
 *   last stop bit.
 * - `quiet` is high when nothing is waiting and the line is idle or already
 *   in its final stop bit. The stop bit's timing is fixed when it starts, so
 *   from here the divisor can change without affecting what is on the line.
 * - `tx` comes straight from a flip-flop, so it never glitches.
 */

`default_nettype none

module uart_tx (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] div,
    input  wire [ 7:0] wr_data,
    input  wire        wr_en,
    output wire        hold_empty,
    output wire        idle,
    output wire        quiet,
    output reg         tx
);

  reg  [ 7:0] hold;
  reg         hold_full;
  reg  [ 8:0] shreg;  // {stop, data[7:0]}; shifts right, refills with 1
  reg  [ 3:0] nbits;  // bits left in the frame, counting the one on the line
  reg  [15:0] cnt;

  wire        busy = (nbits != 4'd0);
  wire        bit_done = (cnt == 16'd0);
  wire        frame_done = busy && bit_done && (nbits == 4'd1);
  // Start the next frame on the same cycle the stop bit ends, so
  // back-to-back bytes are exactly 10 bit periods apart.
  wire        load = hold_full && (!busy || frame_done);

  assign hold_empty = !hold_full;
  assign idle       = !hold_full && !busy;
  assign quiet      = !hold_full && (nbits <= 4'd1);

  always @(posedge clk) begin
    if (!rst_n) begin
      hold      <= 8'd0;
      hold_full <= 1'b0;
      shreg     <= 9'h1ff;
      nbits     <= 4'd0;
      cnt       <= 16'd0;
      tx        <= 1'b1;
    end else begin
      if (wr_en && !hold_full) begin
        hold      <= wr_data;
        hold_full <= 1'b1;
      end

      if (load) begin
        shreg     <= {1'b1, hold};
        tx        <= 1'b0;  // start bit
        nbits     <= 4'd10;
        cnt       <= div - 16'd1;
        hold_full <= 1'b0;
      end else if (!busy) begin
        // idle, line stays high
      end else if (!bit_done) begin
        cnt <= cnt - 16'd1;
      end else if (nbits == 4'd1) begin
        nbits <= 4'd0;  // stop bit done; tx is already high
      end else begin
        tx    <= shreg[0];
        shreg <= {1'b1, shreg[8:1]};
        nbits <= nbits - 4'd1;
        cnt   <= div - 16'd1;
      end
    end
  end

endmodule
