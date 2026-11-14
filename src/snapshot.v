/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * snapshot: one engine's atomic, ordered copy of edge_ts (docs/isa-timing.md 3.6).
 *
 * Kept in its own module so that the tick-level soundness proof
 * (formal/verdict_formal.sv, snapshot lemma) runs on exactly the RTL the
 * engine uses, not on a copy.
 *
 * Timing (r = the cycle in which the trigger edge is REPORTED by edge_ts)
 * ---------------------------------------------------------------------------
 *   r     `hit` and no snapshot held or pending: the capture is scheduled
 *         (cap_stage <- 1) and the trigger edge is remembered (tm_r / tm_f).
 *   r+1   cap_stage == 1.
 *   r+2   cap_stage == 2: the snapshot registers load edge_ts's registers.
 *         In cycle r+2 those registers hold exactly the state edge_ts latched
 *         at the END OF CYCLE r+1 (edges reported up to and including r+1;
 *         an edge reported in r+2 is latched only at the end of r+2, so it is
 *         not in the snapshot). S, epoch and valid update at the end of r+2,
 *         so S is visible from r+3.
 *
 * Why r+1 and not r: two edges' report latencies differ by less than 2
 * cycles, in either direction. An edge that truly came before the trigger
 * edge is therefore reported by r+1 at the latest, so it is in the snapshot.
 * An edge that truly came after the trigger can be reported as early as r-1.
 *
 * ORD (order-ambiguous) per edge = "reported in cycle r-1, r or r+1", taken
 * from the report-pulse history h3 / h2 / h1 (pulses 3 / 2 / 1 cycles before
 * the capture cycle r+2). Such an edge may have come just before or just
 * after the trigger, so a verdict using it must be INDETERMINATE. Edges
 * reported at r-2 or earlier truly came before the trigger.
 *
 * The trigger edge ITSELF is not ORD: it is reported in cycle r by
 * definition, and its order relative to the trigger is known (it IS the
 * trigger). Without this exception every judgment that uses the trigger edge
 * (e.g. `tsget Y, 1, rise` in the I2C setup-time referee) would be
 * INDETERMINATE. Only its cycle-r report is excluded; the same edge type on
 * the same channel cannot be reported again within 2 cycles anyway (edge_ts
 * needs every level held for >= 4 cycles).
 *
 * Trigger while a snapshot is held (S) or a capture is pending: `lost_now`
 * pulses, the snapshot is unchanged (still one consistent instant).
 *
 * `clear` (a RUN starting) empties everything: no valid snapshot, epoch 0,
 * no pending capture, and the 2- and 3-cycle report history.
 */

`default_nettype none

module snapshot #(
    parameter integer N    = 4,   // channels
    parameter integer CH_W = 2,   // width of a channel number
    parameter integer TS_W = 16   // timestamp width
) (
    input  wire              clk,
    input  wire              rst_n,
    input  wire              clear,      // a RUN starts: empty everything
    input  wire              arm,        // trigger enabled (engine running and TRIG issued)
    input  wire [  CH_W-1:0] trig_ch,
    input  wire              trig_e,     // 0 rise, 1 fall
    input  wire              rel,        // REL issued: S <- 0 (a capture in the same cycle wins)

    // edge_ts: this cycle's report pulses and its registered state
    input  wire [     N-1:0] et_rise,
    input  wire [     N-1:0] et_fall,
    input  wire [N*TS_W-1:0] et_rise_ts,
    input  wire [N*TS_W-1:0] et_fall_ts,
    input  wire [     N-1:0] et_rise_stale,
    input  wire [     N-1:0] et_fall_stale,
    input  wire [     N-1:0] et_missed,
    input  wire [     N-1:0] et_last_fall,

    output reg               S,          // a snapshot is held
    output reg               valid,      // a snapshot was taken since the RUN started
    output reg  [       1:0] epoch,
    output wire              lost_now,   // a trigger arrived while S or a capture was pending
    output reg  [N*TS_W-1:0] sn_rts,
    output reg  [N*TS_W-1:0] sn_fts,
    output reg  [     N-1:0] sn_rstale,
    output reg  [     N-1:0] sn_fstale,
    output reg  [     N-1:0] sn_missed,
    output reg  [     N-1:0] sn_last,    // 1: the last edge latched was a fall
    output reg  [     N-1:0] sn_ord_r,   // rise is order-ambiguous with the trigger
    output reg  [     N-1:0] sn_ord_f
`ifdef FORMAL
    ,
    output wire [       1:0] f_cap_stage,
    output wire [     N-1:0] f_tm_r,
    output wire [     N-1:0] f_tm_f
`endif
);

  reg [1:0] cap_stage;  // 0 idle; 1 in r+1; 2 in r+2 (capture)
  reg [N-1:0] tm_r, tm_f;  // the trigger edge reported in r (one-hot)
  // report-pulse history: h1 = last cycle, h2 = 2 cycles ago, h3 = 3 cycles ago
  reg [N-1:0] h1_r, h1_f, h2_r, h2_f, h3_r, h3_f;

`ifdef FORMAL
  assign f_cap_stage = cap_stage;
  assign f_tm_r = tm_r;
  assign f_tm_f = tm_f;
`endif

  wire hit = arm && (trig_e ? et_fall[trig_ch] : et_rise[trig_ch]);
  wire busy = S || (cap_stage != 2'd0);
  assign lost_now = hit && busy;
  wire new_pending = hit && !busy;
  wire capture = (cap_stage == 2'd2);
  wire [N-1:0] sel = {{(N - 1) {1'b0}}, 1'b1} << trig_ch;

  always @(posedge clk) begin
    if (!rst_n || clear) begin
      S <= 1'b0;
      valid <= 1'b0;
      epoch <= 2'd0;
      cap_stage <= 2'd0;
      {tm_r, tm_f} <= {2 * N{1'b0}};
      sn_rts <= {N * TS_W{1'b0}};
      sn_fts <= {N * TS_W{1'b0}};
      {sn_rstale, sn_fstale, sn_missed, sn_last, sn_ord_r, sn_ord_f} <= {6 * N{1'b0}};
      {h3_r, h3_f, h2_r, h2_f} <= {4 * N{1'b0}};
      // h1 keeps recording during reset release / RUN start (as before the split)
      h1_r <= rst_n ? et_rise : {N{1'b0}};
      h1_f <= rst_n ? et_fall : {N{1'b0}};
    end else begin
`ifdef SNAPSHOT_MUTANT_EARLY
      // Negative control for formal/engine.sby: capture one cycle early (at r + 1,
      // the v0.3 timing). The snapshot properties must FAIL when this is defined.
      if (cap_stage == 2'd1) begin
`else
      if (capture) begin
`endif
        sn_rts <= et_rise_ts;
        sn_fts <= et_fall_ts;
        sn_rstale <= et_rise_stale;
        sn_fstale <= et_fall_stale;
        sn_missed <= et_missed;
        sn_last <= et_last_fall;
        sn_ord_r <= h1_r | (h2_r & ~tm_r) | h3_r;
        sn_ord_f <= h1_f | (h2_f & ~tm_f) | h3_f;
        epoch <= epoch + 2'd1;
        S <= 1'b1;
        valid <= 1'b1;
      end else if (rel) S <= 1'b0;

      cap_stage <= new_pending ? 2'd1 : (cap_stage == 2'd1) ? 2'd2 : 2'd0;
      if (new_pending) begin
        tm_r <= trig_e ? {N{1'b0}} : sel;
        tm_f <= trig_e ? sel : {N{1'b0}};
      end
      {h3_r, h3_f, h2_r, h2_f, h1_r, h1_f} <= {h2_r, h2_f, h1_r, h1_f, et_rise, et_fall};
    end
  end

`ifdef SNAPSHOT_PROPS
  // ===========================================================================
  // Snapshot properties (docs/isa-timing.md section 9), cycle level, with every
  // input free: proved by formal/engine.sby (task `snap`). The tick-level
  // lemma, that the snapshot holds every edge truly before the trigger and that
  // ORD-free edges truly came before it, is in formal/verdict_formal.sv.
  // (A separate define, so the tick-level harness doesn't pick these up.)
  // ===========================================================================
  reg [2:0] f_up;  // cycles since reset or clear ended (saturating)
  always @(posedge clk) f_up <= (!rst_n || clear) ? 3'd0 : (&f_up ? f_up : f_up + 3'd1);

  // "edge_ts state as of the end of cycle c" = what edge_ts latched at the clock
  // edge ending cycle c = the contents of its registers during cycle c + 1.
  // So during cycle c + 2, f_end_* holds the state as of the end of cycle c.
  reg [N*TS_W-1:0] f_end_rts, f_end_fts;
  reg [N-1:0] f_end_rstale, f_end_fstale, f_end_missed, f_end_last;
  always @(posedge clk) begin
    f_end_rts <= et_rise_ts;
    f_end_fts <= et_fall_ts;
    f_end_rstale <= et_rise_stale;
    f_end_fstale <= et_fall_stale;
    f_end_missed <= et_missed;
    f_end_last <= et_last_fall;
  end

  // f_r3: this is cycle r + 3, r = the cycle a trigger was accepted
  // (new_pending). f_up >= 4: no reset or clear in cycles r - 1 .. r + 2, so
  // the capture happened and the report history back to r - 1 is real.
  reg f_r3;
  always @(*) f_r3 = (f_up >= 3'd4) && f_np3;
  reg f_np1, f_np2, f_np3;  // new_pending 1, 2, 3 cycles ago
  always @(posedge clk) {f_np3, f_np2, f_np1} <= {f_np2, f_np1, new_pending};

  integer fc;
  always @(posedge clk) begin
    if (rst_n && f_up != 3'd0) begin
      // -- a trigger reported in r is captured exactly once, as the edge_ts
      //    state as of the END OF CYCLE r + 1, and becomes visible in r + 3
      if (f_r3) begin
        // (during r + 3, f_end_* = the edge_ts state as of the end of r + 1)
        assert (sn_rts == f_end_rts && sn_fts == f_end_fts);
        assert (sn_rstale == f_end_rstale && sn_fstale == f_end_fstale);
        assert (sn_missed == f_end_missed && sn_last == f_end_last);
        assert (S && valid && epoch == $past(epoch) + 2'd1);
        assert (!$past(S));  // S was not visible before r + 3
        // -- ORD is set exactly on the edges reported in r - 1, r or r + 1,
        //    except the trigger edge itself (its own report in r)
        for (fc = 0; fc < N; fc = fc + 1) begin
          assert (sn_ord_r[fc] == ($past(et_rise[fc], 2) ||
                                   ($past(et_rise[fc], 3) && !($past(!trig_e && trig_ch == fc, 3))) ||
                                   $past(et_rise[fc], 4)));
          assert (sn_ord_f[fc] == ($past(et_fall[fc], 2) ||
                                   ($past(et_fall[fc], 3) && !($past(trig_e && trig_ch == fc, 3))) ||
                                   $past(et_fall[fc], 4)));
        end
      end
      // -- S, the snapshot and the epoch change only at a capture (r + 2) or,
      //    for S, a REL: constant while S
      if (!$past(clear) && !$past(capture)) begin
        assert (sn_rts == $past(sn_rts) && sn_fts == $past(sn_fts));
        assert ({sn_rstale, sn_fstale, sn_missed, sn_last, sn_ord_r, sn_ord_f} ==
                $past({sn_rstale, sn_fstale, sn_missed, sn_last, sn_ord_r, sn_ord_f}));
        assert (epoch == $past(epoch));
        if ($past(S) && !$past(rel)) assert (S);
      end
      // -- a capture happens only 2 cycles after an accepted trigger, and none
      //    is accepted while S or a capture is pending (those are lost)
      if (capture) assert ($past(new_pending, 2) || f_up < 3'd2);
      if (new_pending) assert (!S && cap_stage == 2'd0);
      assert (lost_now == (hit && (S || cap_stage != 2'd0)));
    end
  end

  always @(posedge clk) begin
    if (rst_n && f_up != 3'd0) begin
      cover (f_r3 && sn_ord_r[0] && !$past(et_rise[0], 3));  // ORD from r - 1 or r + 1
      cover (f_r3 && $past(et_rise[0], 3) && !sn_ord_r[0]);  // the trigger edge, not ORD
      cover (f_r3 && sn_ord_f[1]);
      cover (lost_now && !S);  // lost while a capture is pending
    end
  end
`endif

endmodule

`default_nettype wire
