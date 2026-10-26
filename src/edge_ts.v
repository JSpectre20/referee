/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * edge_ts: edge-timestamp unit for N input pins (PLAN.md 4.9).
 *
 * For each pin it keeps, every cycle and with no help from firmware:
 *   - the timestamp of the last rising edge and of the last falling edge,
 *   - a sticky "toggled since clear" bit,
 *   - one-cycle `rise` / `fall` pulses that an engine can wait on.
 *
 * Why hardware and not firmware: a firmware referee spends a few cycles between
 * waking on one edge and re-arming for the next, and it cannot look back to see
 * when a data line last changed before a clock edge. With these latches a setup
 * time is one subtraction (clock-rise timestamp minus data's last-edge
 * timestamp), and no edge is lost while the engine is busy.
 *
 * Timing contract (formal/edge_ts.sby proves it with edges at any sub-cycle
 * phase and either outcome of a metastable sample)
 * ---------------------------------------------------------------------------
 *   `now` must be a free-running counter that increments by 1 every cycle;
 *   call cycle c the cycle in which now == c. An external edge that happens
 *   at real time t, inside cycle c (c <= t < c+1, in clock periods):
 *     - is sampled by the first synchronizer flop at the clock edge ending
 *       cycle c, or, if that sample goes metastable and resolves to the old
 *       level, at the edge ending cycle c+1;
 *     - is reported in cycle c+2 (or c+3): `rise`/`fall` pulses in that
 *       cycle and the timestamp latched is `now` of that cycle.
 *   So for every edge:     1 < timestamp - t <= 3          (cycles)
 *   and for any two edges: |(ts2 - ts1) - (t2 - t1)| < 2   (cycles)
 *   which is the u = 2 of the referee's three-valued rule (PLAN.md 4.8) for
 *   intervals whose both ends are external edges.
 *
 *   Precondition: a pin holds each level for at least 4 cycles. Shorter
 *   pulses can be missed or merged by the synchronizer; nothing below claims
 *   anything about them. (The referee reports such pulses separately later.)
 *
 *   Out of reset nothing is reported until the synchronizer holds the real
 *   pin level (3 cycles), so a pin that is already high is not a "rise".
 *
 *   If `clear[i]` and an edge on pin i arrive in the same cycle, the edge
 *   wins: `toggled[i]` stays set.
 */

`default_nettype none

module edge_ts #(
    parameter integer N    = 2,   // pins watched
    parameter integer TS_W = 16   // timestamp width (wraps; compare by difference)
) (
    input  wire              clk,
    input  wire              rst_n,     // synchronous, active low
    input  wire [     N-1:0] pins,      // asynchronous: straight from the pads
    input  wire [ TS_W-1:0]  now,       // free-running cycle counter
    input  wire [     N-1:0] clear,     // clear `toggled` (edge in the same cycle wins)

    output wire [     N-1:0] level,     // synchronized pin level
    output wire [     N-1:0] rise,      // pulse: rising edge reported this cycle
    output wire [     N-1:0] fall,      // pulse: falling edge reported this cycle
    output reg  [N*TS_W-1:0] rise_ts,   // pin i at [i*TS_W +: TS_W]
    output reg  [N*TS_W-1:0] fall_ts,
    output reg  [     N-1:0] toggled
`ifdef FORMAL
    // Internal state, visible only to the formal harness (inductive invariants).
    ,
    output wire [       1:0] f_warm,
    output wire [     N-1:0] f_s1,
    output wire [     N-1:0] f_s2,
    output wire [     N-1:0] f_s3
`endif
);

  // Two-flop synchronizer (s1, s2), plus s3 = s2 one cycle ago for edge detection.
  // s1 is the only flop that ever sees an asynchronous input; nothing else
  // reads it.
  reg [N-1:0] s1, s2, s3;

  // Warm-up: the synchronizer flops come out of reset at 0, which may not be
  // the pin's level. After three clock edges s3 holds the real level; until
  // then edge reporting is off, so a pin that was high all along does not
  // produce a phantom rising edge.
  reg [1:0] warm;
  wire      armed = (warm == 2'd3);

`ifdef FORMAL
  assign f_warm = warm;
  assign {f_s1, f_s2, f_s3} = {s1, s2, s3};
`endif

  assign level = s2;
  assign rise  = {N{armed}} & s2 & ~s3;
  assign fall  = {N{armed}} & ~s2 & s3;

  always @(posedge clk) begin
    if (!rst_n) begin
      s1   <= {N{1'b0}};
      s2   <= {N{1'b0}};
      s3   <= {N{1'b0}};
      warm <= 2'd0;
    end else begin
      s1 <= pins;
      s2 <= s1;
      s3 <= s2;
      if (!armed) warm <= warm + 2'd1;
    end
  end

  // Per-pin timestamp latches and sticky bit.
  genvar i;
  generate
    for (i = 0; i < N; i = i + 1) begin : g_pin
      always @(posedge clk) begin
        if (!rst_n) begin
          rise_ts[i*TS_W+:TS_W] <= {TS_W{1'b0}};
          fall_ts[i*TS_W+:TS_W] <= {TS_W{1'b0}};
          toggled[i]            <= 1'b0;
        end else begin
          if (rise[i]) rise_ts[i*TS_W+:TS_W] <= now;
`ifdef EDGE_TS_MUTANT_FALL_LATE
          // Negative control for the formal proof: falling edges stamped one
          // cycle late. formal/edge_ts.sby must FAIL when this is defined.
          if (fall[i]) fall_ts[i*TS_W+:TS_W] <= now + {{(TS_W - 1) {1'b0}}, 1'b1};
`else
          if (fall[i]) fall_ts[i*TS_W+:TS_W] <= now;
`endif
          // A new edge wins over a clear in the same cycle.
          toggled[i] <= (toggled[i] & ~clear[i]) | rise[i] | fall[i];
        end
      end
    end
  endgenerate

endmodule

`default_nettype wire
