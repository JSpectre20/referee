/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * uart_tx: UART transmitter, 8N1, LSB first, fixed baud.
 *
 * Frame: the line idles high; then 1 start bit (0), 8 data bits LSB first,
 * 1 stop bit (1). Every bit lasts exactly CLKS_PER_BIT clock cycles.
 *
 * Handshake
 * ---------
 *   - Pulse `start` for one cycle while `busy` is low. `data` is copied in
 *     that cycle, so the caller may change it right after.
 *   - `start` while `busy` is high is ignored (the byte is NOT queued; the
 *     caller must wait for `busy` to fall).
 *   - Timing (proved in formal/uart.sby): if `start` is accepted in cycle t,
 *     the start bit is on `tx` from cycle t+1, the frame occupies cycles
 *     t+1 .. t+10*CLKS_PER_BIT, and `busy` is high exactly during those
 *     cycles. A new `start` can be accepted in cycle t+10*CLKS_PER_BIT+1 at the
 *     earliest, so back-to-back bytes have a stop bit one cycle longer than
 *     nominal, which every receiver accepts.
 *
 * Why `tx` is a flip-flop: it drives a pad directly. A combinational output
 * (e.g. a mux of the shift register) can glitch while its inputs settle, and
 * a glitch on an idle UART line looks like a start bit to the receiver.
 *
 * Bit period: the runtime input `div` (written CLKS_PER_BIT above), so the
 * host can change the baud rate between frames. `cnt` counts 0 .. div-1
 * inside one bit and is DIV_W = 16 bits wide (down to 2 cycles per bit).
 */

`default_nettype none

module uart_tx #(
    /* verilator lint_off UNUSEDPARAM */
    parameter integer CLKS_PER_BIT = 347,  // used only by the formal properties (div == it)
    /* verilator lint_on UNUSEDPARAM */
    parameter integer DIV_W = 16          // width of the runtime bit period
) (
    input  wire       clk,
    input  wire       rst_n,  // synchronous, active low
    input  wire [DIV_W-1:0] div,  // bit period in cycles (>= 2); change it only while idle
    input  wire [7:0] data,   // byte to send, sampled when `start` is accepted
    input  wire       start,  // one-cycle pulse: send `data`
    output reg        tx,     // the serial line; 1 (idle) during and after reset
    output wire       busy    // high while a frame is on the line
);

  // The bit period is a runtime input, so the host can switch baud rates
  // (host port command 'B', applied only between frames).
  localparam integer CW = DIV_W;
  wire [CW-1:0] last = div - 1'b1;  // last cycle of a bit

  // Four states, two bits. `default` in the case below brings any illegal
  // value back to IDLE (cannot happen from reset, but costs nothing).
  localparam [1:0] IDLE = 2'd0, START = 2'd1, DATA = 2'd2, STOP = 2'd3;

  reg [   1:0] state;
  reg [CW-1:0] cnt;    // cycle within the current bit
  reg [   2:0] idx;    // which data bit is on the line (0..7)
  reg [   7:0] shreg;  // copy of the byte: `data` may change mid-frame

  assign busy = (state != IDLE);
  wire bit_end = (cnt == last);

  always @(posedge clk) begin
    if (!rst_n) begin
      state <= IDLE;
      cnt   <= {CW{1'b0}};
      idx   <= 3'd0;
      shreg <= 8'd0;
      tx    <= 1'b1;  // idle level: a 0 here would look like a start bit
    end else begin
      // Bit timer: runs during a frame, restarts at every bit boundary.
      if (state == IDLE || bit_end) cnt <= {CW{1'b0}};
      else cnt <= cnt + 1'b1;

      case (state)
        IDLE: begin
          tx <= 1'b1;
          if (start) begin
            shreg <= data;
            tx    <= 1'b0;  // start bit, visible from the next cycle
            state <= START;
          end
        end

        START:
        if (bit_end) begin
          tx    <= shreg[0];  // data bit 0 (LSB first)
          shreg <= {1'b0, shreg[7:1]};
          idx   <= 3'd0;
          state <= DATA;
        end

        DATA:
        if (bit_end) begin
          if (idx == 3'd7) begin
            tx    <= 1'b1;  // stop bit
            state <= STOP;
          end else begin
            tx    <= shreg[0];
            shreg <= {1'b0, shreg[7:1]};
            idx   <= idx + 3'd1;
          end
        end

`ifdef UART_TX_MUTANT_SHORT_STOP
        // Negative control: the stop bit ends one cycle early.
        // formal/uart.sby must FAIL when this is defined.
        STOP: if (cnt == last - 1'b1) state <= IDLE;
`else
        STOP: if (bit_end) state <= IDLE;
`endif

        default: state <= IDLE;
      endcase
    end
  end

  // ---------------------------------------------------------------------------
  // Formal properties (formal/uart.sby, task tx_prove)
  // ---------------------------------------------------------------------------
`ifdef FORMAL
  reg f_past_valid = 1'b0;
  always @(posedge clk) f_past_valid <= 1'b1;
  always @(*) if (!f_past_valid) assume (!rst_n);
  always @(*) assume (div == CLKS_PER_BIT);  // the proof is for one fixed bit period

  // Harness view of a frame, written from the frame format, not from the FSM:
  // `f_t` counts cycles since the accepted start (1 = first cycle of the start
  // bit), `f_byte` is the byte being sent.
  localparam integer FRAME = 10 * CLKS_PER_BIT;
  reg [15:0] f_t;
  reg [ 7:0] f_byte;
  always @(posedge clk) begin
    if (!rst_n) f_t <= 16'd0;
    else if (!busy && start) begin
      f_t    <= 16'd1;
      f_byte <= data;
    end else if (f_t == FRAME) f_t <= 16'd0;
    else if (f_t != 16'd0) f_t <= f_t + 16'd1;
  end

  // Which bit of the frame is on the line in cycle f_t: 0 = start, 1..8 data, 9 stop.
  wire [15:0] f_bit = (f_t - 16'd1) / CLKS_PER_BIT;

  always @(posedge clk) begin
    if (f_past_valid && $past(!rst_n)) assert (tx);  // idle high out of reset
    if (rst_n) begin
      if (!busy) assert (tx);  // idle line is high
      assert (busy == (f_t != 16'd0));  // busy exactly for 10*CLKS cycles
      if (f_t != 16'd0) begin  // every bit has the right value
        if (f_bit == 16'd0) assert (!tx);
        else if (f_bit == 16'd9) assert (tx);
        else assert (tx == f_byte[f_bit-16'd1]);
      end
      // Inductive strengthening (the FSM's position matches the harness).
      assert (cnt <= last);
      if (f_t != 16'd0) assert ({16'd0, cnt} == (f_t - 16'd1) % CLKS_PER_BIT);
      if (state == DATA) assert ({13'd0, idx} == f_bit - 16'd1);
      if (state == DATA) assert (shreg == (f_byte >> (f_bit)));
      if (state == START) assert (shreg == f_byte);
      assert (state == IDLE || f_t != 16'd0);
      if (f_t != 16'd0)
        assert (state == (f_bit == 16'd0 ? START : f_bit == 16'd9 ? STOP : DATA));
    end
  end

  always @(posedge clk) begin
    if (rst_n) begin
      cover (f_past_valid && $past(f_t) == FRAME && !busy);  // a full frame completes
      cover (busy && start);  // start while busy (ignored)
      cover (f_past_valid && $past(f_t, 2) == FRAME && f_t == 16'd1);  // back to back
    end
  end
`endif

endmodule

`default_nettype wire
