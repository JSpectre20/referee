// Formal harness: the referee's verdicts are sound (src/verdict.v on top of
// src/edge_ts.v), checked against the true interval at sub-cycle resolution.
//
// Time model
// ----------
//   - A global formal clock ticks K = 2**K_W times per chip clock cycle. The
//     chip clock `clk` rises once every K ticks; all chip logic (edge_ts and
//     the cycle counter `now`) runs on it.
//   - Each pin's TRUE level is a free input every tick, so an external edge
//     can happen at any tick (subject to the edge_ts precondition: a level
//     is held for at least 4 cycles; here 4K+2 ticks).
//   - Near-edge capture: if a pin changes within one tick of a chip clock
//     edge (the tick boundary just before, at, or just after it), the first
//     synchronizer flop captures the old or the new level, a free choice
//     every time. Otherwise it captures the level just before the edge.
//     (To decide this, the harness looks one tick past the edge, so the
//     physical pin level is the free input delayed by 3 ticks. That delay is
//     the same for every pin, so true intervals are unchanged.)
//   - Both pins see the same clock edges: zero skew between synchronizers.
//     The glitch filter is not modelled (edge_ts has none).
//
// Ghost interval
// --------------
// T is the true interval in ticks, counted by the harness from the free
// inputs, independently of the design. Two intervals are judged:
//   - pulse width on pin 0 (rise to fall), judged when the fall is reported;
//   - pin 0 rise to pin 1 rise (e.g. a data edge to a clock edge: a setup
//     time), judged THROUGH THE ENGINE'S SNAPSHOT (src/snapshot.v), exactly
//     as `trig 1, rise; waits; tsget X, 0, rise; tsget Y, 1, rise; judge`
//     does: X and Y are read from the snapshot taken on pin 1's rise, in the
//     cycle the snapshot becomes visible, and ORD on either forces
//     INDETERMINATE. REL is a free input, so triggers can also be lost.
//
// Snapshot lemma (docs/isa-timing.md section 9; removes ledger A16)
// -----------------------------------------------------------------
// The merged proof judged the cross interval only once pin 0's rise "had
// been reported" (the harness condition `!pend_r0 || rise[0]`), which no
// hardware enforced. Now there is no such condition. Instead it is proved
// that, in the snapshot of a pin-1 rise (the trigger):
//   - the trigger edge Y is that rise, and it is never ORD;
//   - completeness: X is never older than pin 0's last true rise at or
//     before the trigger (every edge truly before the trigger is in it);
//   - order: if X is not ORD, X IS that rise (an edge latched at r - 2 or
//     earlier truly came before the trigger);
// and with these the verdict on Y - X is sound against the true interval.
// Each is judged against its own free bound N (in ticks, so non-integer cycle
// bounds are exact) and its own free choice of minimum or maximum bound.
//
// Proved
// ------
//   minimum bound: VIOLATION => T < N,  PASS => T >= N
//   maximum bound: VIOLATION => T > N,  PASS => T <= N
`default_nettype none

module verdict_formal #(
    parameter integer K_W   = 2,  // K = 4 ticks per chip cycle
    parameter integer TS_W  = 6,  // timestamp width (small: exercises wrap-around)
    parameter integer U_CYC = 2,  // the uncertainty under test, in cycles
    parameter integer SHARP = 0,  // verdict rule variant (see src/verdict.v); sharp4/8 set 1
    parameter integer HOLD  = 0   // minimum ticks per level; 0 = the edge_ts precondition (4K+2)
) ();
  localparam integer K = 1 << K_W;
  localparam integer TW = TS_W + K_W + 1;  // tick counters (one bit spare)
  localparam integer N_W = TS_W + K_W;  // bound width, ticks
  localparam integer H = (HOLD != 0) ? HOLD : 4 * K + 2;  // minimum ticks between changes of one pin
  localparam [TS_W-1:0] QUARTER = {2'b01, {(TS_W - 2) {1'b0}}};
  // No assumption bounds the length of an interval: a timestamp older than
  // QUARTER cycles is flagged stale by edge_ts, which forces INDETERMINATE.
  localparam [TW-1:0] SAT = {TW{1'b1}};

  // ---------------------------------------------------------------------------
  // Clocks and reset
  // ---------------------------------------------------------------------------
  reg [K_W-1:0] ph = 0;  // tick within the chip cycle
  always @($global_clock) ph <= ph + 1'b1;

  reg clk = 1'b0;  // high during the tick with ph == 0: rises once per K ticks
  always @($global_clock) clk <= (ph == K - 1);

  reg rst_n = 1'b0;  // low through the first chip clock edge, then high
  always @($global_clock) if (clk) rst_n <= 1'b1;

  reg [TS_W-1:0] now;  // chip cycle counter (`now` of edge_ts)
  always @(posedge clk) now <= now + 1'b1;

  reg [7:0] since_rst;  // ticks since reset ended (saturating)
  always @($global_clock) since_rst <= !rst_n ? 8'd0 : (&since_rst ? since_rst : since_rst + 1'b1);

  // ---------------------------------------------------------------------------
  // The outside world
  // ---------------------------------------------------------------------------
  (* anyseq *) reg [1:0] tp;  // true pin levels (3 ticks ahead of the pins)
  (* anyseq *) reg [1:0] coin;  // outcome of a near-edge capture
  reg [1:0] tp1, tp2, tp3;  // tp delayed by 1, 2, 3 ticks
  always @($global_clock) {tp3, tp2, tp1} <= {tp2, tp1, tp};

  reg [7:0] age[0:1];  // ticks since each pin last changed (saturating)
  genvar i;
  generate
    for (i = 0; i < 2; i = i + 1) begin : g_pin
      wire chg = (tp[i] != tp1[i]);
      always @($global_clock) age[i] <= !rst_n ? 8'hff : chg ? 8'd1 : (&age[i] ? age[i] : age[i] + 1'b1);
      // Changes only out of reset (after edge_ts's 3-cycle warm-up) and at
      // least H ticks apart.
      always @(*) if (chg) assume (rst_n && since_rst >= 6 * K && age[i] >= H);
      always @(*) if (!rst_n) assume (!chg);
    end
  endgenerate

  // What the first synchronizer flop captures at the next clock edge.
  // Decided in the tick with ph == K-2 (two ticks before the edge) and held
  // for K ticks, so it is stable whenever the flop samples. In that tick:
  //   tp3 = level two ticks before the edge, tp2 = level just before it,
  //   tp1 = level just after it, tp = level two ticks after it.
  reg [1:0] din;
  wire [1:0] near = (tp3 ^ tp2) | (tp2 ^ tp1) | (tp1 ^ tp);
  always @($global_clock) if (ph == K - 2) din <= tp2 ^ (near & coin);

  // ---------------------------------------------------------------------------
  // Design under test
  // ---------------------------------------------------------------------------
  wire [1:0] rise, fall, level, toggled, rise_stale, fall_stale, missed;
  wire [2*TS_W-1:0] rise_ts, fall_ts;
  wire [1:0] f_warm;
  wire [1:0] f_s1, f_s2, f_s3;
  edge_ts #(
      .N(2), .TS_W(TS_W)
  ) dut (
      .clk(clk), .rst_n(rst_n), .pins(din), .now(now), .clear(2'b00),
      .level(level), .rise(rise), .fall(fall),
      .rise_ts(rise_ts), .fall_ts(fall_ts), .toggled(toggled),
      .rise_stale(rise_stale), .fall_stale(fall_stale), .missed(missed),
      .f_warm(f_warm), .f_s1(f_s1), .f_s2(f_s2), .f_s3(f_s3)
  );

  // ---------------------------------------------------------------------------
  // Ghost bookkeeping (true intervals, in ticks)
  // ---------------------------------------------------------------------------
  wire rise0_now = tp[0] & ~tp1[0], fall0_now = ~tp[0] & tp1[0];
  wire rise1_now = tp[1] & ~tp1[1];

  reg [TW-1:0] since_r0;  // ticks since pin 0 truly rose
  reg [TW-1:0] since_r0p;  // ticks since pin 0's PREVIOUS true rise
  reg [TW-1:0] since_r1;  // ticks since pin 1 truly rose (the trigger)
  reg [TW-1:0] t_pulse;  // pin 0 width, captured when it truly falls
  reg [TW-1:0] t_cross;  // pin 0's last rise at or before pin 1's rise -> pin 1's rise
  reg pre0;  // pin 0 had risen at or before pin 1's last rise
  reg pend_r0, pend_f0, pend_r1, seen_r0;

  wire [TW-1:0] inc_r0 = since_r0 == SAT ? SAT : since_r0 + 1'b1;
  always @($global_clock) begin
    since_r0 <= rise0_now ? {{(TW - 1) {1'b0}}, 1'b1} : inc_r0;
    // (only once there was an earlier rise: since_r0 means nothing before the first one)
    since_r0p <= !rst_n ? SAT : rise0_now ? ((seen_r0 || pend_r0) ? inc_r0 : SAT) : (since_r0p == SAT ? SAT : since_r0p + 1'b1);
    since_r1 <= !rst_n ? SAT : rise1_now ? {{(TW - 1) {1'b0}}, 1'b1} : (since_r1 == SAT ? SAT : since_r1 + 1'b1);
    if (fall0_now) t_pulse <= since_r0;
    if (!rst_n) pre0 <= 1'b0;
    else if (rise1_now) begin
      t_cross <= rise0_now ? {TW{1'b0}} : since_r0;
      pre0 <= rise0_now || seen_r0 || pend_r0;
    end

    if (!rst_n) {pend_r0, pend_f0, pend_r1, seen_r0} <= 4'b0000;
    else begin
      if (rise0_now) pend_r0 <= 1'b1;
      else if (rise[0]) pend_r0 <= 1'b0;
      if (fall0_now) pend_f0 <= 1'b1;
      else if (fall[0]) pend_f0 <= 1'b0;
      if (rise1_now) pend_r1 <= 1'b1;
      else if (rise[1]) pend_r1 <= 1'b0;
      if (rise[0]) seen_r0 <= 1'b1;
    end
  end

  // ---------------------------------------------------------------------------
  // Measurements and verdicts
  // ---------------------------------------------------------------------------
  wire signed [TS_W-1:0] m_pulse = now - rise_ts[0+:TS_W];

  // The engine's snapshot, triggered on pin 1's rise (src/snapshot.v, the RTL
  // the engine uses). REL is free: a snapshot may be held for any time, and
  // triggers that arrive meanwhile are lost.
  (* anyseq *) reg rel;
  wire S, s_valid, lost_now;
  wire [1:0] epoch, f_cap_stage, f_tm_r, f_tm_f;
  wire [2*TS_W-1:0] sn_rts, sn_fts;
  wire [1:0] sn_rstale, sn_fstale, sn_missed, sn_last, sn_ord_r, sn_ord_f;
  snapshot #(
      .N(2), .CH_W(1), .TS_W(TS_W)
  ) snap (
      .clk(clk), .rst_n(rst_n), .clear(1'b0), .arm(1'b1), .trig_ch(1'b1), .trig_e(1'b0), .rel(rel),
      .et_rise(rise), .et_fall(fall), .et_rise_ts(rise_ts), .et_fall_ts(fall_ts),
      .et_rise_stale(rise_stale), .et_fall_stale(fall_stale), .et_missed(missed), .et_last_fall(2'b00),
      .S(S), .valid(s_valid), .epoch(epoch), .lost_now(lost_now),
      .sn_rts(sn_rts), .sn_fts(sn_fts), .sn_rstale(sn_rstale), .sn_fstale(sn_fstale),
      .sn_missed(sn_missed), .sn_last(sn_last), .sn_ord_r(sn_ord_r), .sn_ord_f(sn_ord_f),
      .f_cap_stage(f_cap_stage), .f_tm_r(f_tm_r), .f_tm_f(f_tm_f)
  );
  reg S_q;  // S in the previous chip cycle
  always @(posedge clk) S_q <= rst_n && S;
  wire [TS_W-1:0] sx = sn_rts[0+:TS_W], sy = sn_rts[TS_W+:TS_W];
  wire signed [TS_W-1:0] m_cross = sy - sx;  // judge: m = Y - X

  (* anyconst *) reg [N_W-1:0] n_pulse, n_cross;
  (* anyconst *) reg max_pulse, max_cross;
  wire pass_p, viol_p, pass_x, viol_x;
  // The flags the hardware offers: a stale reference timestamp or a pin
  // flagged `missed` makes the verdict INDETERMINATE. (Pin 0's rise reported
  // this very cycle is fresh: its timestamp is `now`.)
  wire inv_p = rise_stale[0] | missed[0];
  // tsget flags: F = stale | missed, O = ORD (both X and Y), as the engine does
  wire inv_x = sn_rstale[0] | sn_rstale[1] | sn_missed[0] | sn_missed[1] | sn_ord_r[0] | sn_ord_r[1];

  verdict #(
      .M_W(TS_W), .FRAC_W(K_W), .N_W(N_W), .U_FX(U_CYC * K), .SHARP(SHARP)
  ) v_pulse (
      .m(m_pulse), .n_lo(n_pulse), .inexact(1'b0), .is_max(max_pulse), .invalid(inv_p), .pass(pass_p), .violation(viol_p)
  );
  verdict #(
      .M_W(TS_W), .FRAC_W(K_W), .N_W(N_W), .U_FX(U_CYC * K), .SHARP(SHARP)
  ) v_cross (
      .m(m_cross), .n_lo(n_cross), .inexact(1'b0), .is_max(max_cross), .invalid(inv_x), .pass(pass_x), .violation(viol_x)
  );

  wire judge_p = rst_n && fall[0] && !pend_r0;
  // judged in the cycle the snapshot becomes visible (r + 3): no harness condition
  wire judge_x = rst_n && S && !S_q;
  wire [TW-1:0] np = {{(TW - N_W) {1'b0}}, n_pulse};
  wire [TW-1:0] nx = {{(TW - N_W) {1'b0}}, n_cross};

  // Measurement error in ticks (for covers): m*K - T.
  wire signed [TW+1:0] err_p = $signed({{2{m_pulse[TS_W-1]}}, m_pulse, {K_W{1'b0}}}) - $signed({2'b00, t_pulse});

  always @($global_clock) begin
    if (judge_p) begin
      if (!max_pulse && viol_p) assert (t_pulse < np);
      if (!max_pulse && pass_p) assert (t_pulse >= np);
      if (max_pulse && viol_p) assert (t_pulse > np);
      if (max_pulse && pass_p) assert (t_pulse <= np);
    end
    if (judge_x) begin
      if (!max_cross && viol_x) assert (t_cross < nx);
      if (!max_cross && pass_x) assert (t_cross >= nx);
      if (max_cross && viol_x) assert (t_cross > nx);
      if (max_cross && pass_x) assert (t_cross <= nx);
    end
  end

  // ---------------------------------------------------------------------------
  // Snapshot lemma, checked in the cycle the snapshot becomes visible.
  //   D = (start of the report cycle, in ticks) - (tick of the true change),
  //   DMIN .. DMAX: "timestamp ts belongs to the true edge at tick t" means
  //   ts*K - t is in DMIN .. DMAX.
  // ---------------------------------------------------------------------------
  localparam integer LDMIN = K + 2, LDMAX = 2 * K + 4;
  localparam integer LGW = TS_W + K_W;
  wire [LGW-1:0] l_gt = {now, ph};
  wire [TW:0] tb = {1'b0, since_r1} + {1'b0, t_cross};  // ticks since pin 0's last rise at/before the trigger
  wire tb_ok = since_r1 != SAT && t_cross != SAT && tb < (1 << (LGW - 1));
  wire [LGW-1:0] dsx = {sx, {K_W{1'b0}}} - (l_gt - tb[LGW-1:0]);
  wire [LGW-1:0] dsy = {sy, {K_W{1'b0}}} - (l_gt - since_r1[LGW-1:0]);

  always @($global_clock) begin
    if (judge_x) begin
      // Y is the trigger edge (pin 1's last true rise), fresh, and never ORD
      assert (since_r1 != SAT && dsy >= LDMIN && dsy <= LDMAX);
      assert (!sn_ord_r[1] && !sn_rstale[1]);
      // no rise of pin 0 at or before the trigger: X can't be used
      if (!pre0) assert (sn_rstale[0] || sn_ord_r[0]);
      if (pre0 && !sn_rstale[0]) begin
        // completeness: X is not older than pin 0's last rise at/before the
        // trigger (every edge truly before the trigger is in the snapshot)
        if (tb_ok) assert (dsx >= LDMIN && dsx <= tb);
        // order: if X is not ORD, it IS that rise (it truly came before the trigger)
        if (!sn_ord_r[0]) assert (tb_ok && dsx <= LDMAX);
      end
    end
  end


`ifndef NO_INVARIANTS
  // ---------------------------------------------------------------------------
  // Inductive strengthening. None of these is an assumption: each is proved
  // like the main assertions, so they cannot make the proof unsound; they only
  // tell the solver how the pipeline relates to the ghost history.
  //   D = (start of the report cycle, in ticks) - (tick of the true change):
  //   the per-edge report latency, K+2 .. 2K+4 ticks (the +3 is the model's
  //   look-ahead delay). Interval error = difference of two Ds <= K+2 < 2K.
  // ---------------------------------------------------------------------------
  localparam integer DMIN = K + 2, DMAX = 2 * K + 4;
  localparam integer GW = TS_W + K_W;  // chip time in ticks: {now, ph}
  wire [GW-1:0] gt = {now, ph};
  wire [GW-1:0] cyc_t = {now, {K_W{1'b0}}};  // start of the current cycle
  wire [GW-1:0] chg0_t = gt - age[0];  // tick of pin 0's last true change
  wire [GW-1:0] chg1_t = gt - age[1];
  wire [GW-1:0] d0 = cyc_t - chg0_t, d1 = cyc_t - chg1_t;
  wire [GW-1:0] r0_t = gt - since_r0[GW-1:0];  // tick of pin 0's last true rise
  wire [GW-1:0] dr0 = {rise_ts[0+:TS_W], {K_W{1'b0}}} - r0_t;
  wire [GW-1:0] dr0p = {rise_ts[0+:TS_W], {K_W{1'b0}}} - (gt - since_r0p[GW-1:0]);
  wire [GW-1:0] dr1 = {rise_ts[TS_W+:TS_W], {K_W{1'b0}}} - (gt - since_r1[GW-1:0]);
  wire armed = (f_warm == 2'd3);

  reg g_valid = 1'b0;  // false only in the very first tick
  always @($global_clock) g_valid <= 1'b1;

  always @($global_clock) begin
    if (g_valid) assert (clk == (ph == 0));
    if (rst_n) begin
      if (since_rst >= 4 * K) assert (armed);
      if (!armed) assert (age[0] == 8'hff && age[1] == 8'hff);
      // delay line
      for (int j = 0; j < 2; j++) begin
        if (age[j] == 1) assert (tp2[j] != tp1[j]);
        if (age[j] == 2) assert (tp2[j] == tp1[j] && tp3[j] != tp2[j]);
        if (age[j] >= 3) assert (tp3[j] == tp2[j] && tp2[j] == tp1[j]);
        if (age[j] >= K + 3) assert (din[j] == tp1[j]);
        if (age[j] >= 4 * K + 2 && armed) assert (f_s1[j] == tp1[j] && f_s2[j] == tp1[j] && f_s3[j] == tp1[j]);
      end
      // a report comes D ticks after its change
      if (rise[0] || fall[0]) assert (age[0] != 8'hff && d0 >= DMIN && d0 <= DMAX);
      if (rise[1] || fall[1]) assert (age[1] != 8'hff && d1 >= DMIN && d1 <= DMAX);
      if (age[0] != 8'hff && age[0] < DMIN) assert (f_s3[0] != tp1[0] && !rise[0] && !fall[0]);
      if (age[1] != 8'hff && age[1] < DMIN) assert (f_s3[1] != tp1[1] && !rise[1] && !fall[1]);
      if (age[0] != 8'hff && f_s3[0] != tp1[0]) assert (age[0] <= DMAX + K);
      if (age[1] != 8'hff && f_s3[1] != tp1[1]) assert (age[1] <= DMAX + K);
      // pending flags
      if (pend_r0) assert (tp1[0] && f_s3[0] == 1'b0 && age[0] != 8'hff && age[0] <= DMAX + 1);
      if (pend_f0) assert (!tp1[0] && f_s3[0] == 1'b1 && age[0] != 8'hff && age[0] <= DMAX + 1);
      if (pend_r1) assert (tp1[1] && f_s3[1] == 1'b0 && age[1] != 8'hff && age[1] <= DMAX + 1);
      if (tp1[0] && age[0] <= DMIN) assert (pend_r0);
      if (!tp1[0] && age[0] <= DMIN) assert (pend_f0);
      if (tp1[1] && age[1] <= DMIN) assert (pend_r1);
      // ghost intervals in terms of the ages
      if (tp1[0] && age[0] != 8'hff) assert (since_r0 == age[0]);
      if (!tp1[0] && age[0] != 8'hff && since_r0 != SAT) assert (t_pulse == since_r0 - age[0]);
      // the cross interval in terms of the ghosts: pin 0's last rise at or
      // before pin 1's last rise is the latest one, or the one before it
      if (pre0 && since_r1 != SAT && since_r0 != SAT && since_r0 >= since_r1) assert (t_cross == since_r0 - since_r1);
      if (pre0 && since_r1 != SAT && since_r0 < since_r1 && since_r0p != SAT && since_r0p >= since_r1) assert (t_cross == since_r0p - since_r1);
      // two rises of one pin are a rise, a fall and a rise apart: >= 2H ticks
      if (since_r0 != SAT && since_r0p != SAT) assert (since_r0p >= since_r0 + 2 * H);
      // while a capture is in flight or just landed, the trigger rise is recent
      // (so at most one pin-0 rise can have followed it)
      if (f_cap_stage == 2'd1) assert (since_r1 <= DMAX + 2 * K);
      if (f_cap_stage == 2'd2) assert (since_r1 <= DMAX + 3 * K);
      if (S && !S_q) assert (since_r1 <= DMAX + 4 * K);
      if (since_r0 == SAT) assert (since_r0p == SAT);
      if (tp1[1] && age[1] != 8'hff) assert (since_r1 == age[1]);
      // pin 1's latched rise vs its true rise (as for pin 0 below)
      if (!rise_stale[1] && !pend_r1 && !rise[1]) assert (since_r1 <= (QUARTER + 1) * K + DMAX && dr1 >= DMIN && dr1 <= DMAX);
      // while pin 0's newest rise is unreported, rise_ts[0] still holds the previous one
      if ((pend_r0 || rise[0]) && !rise_stale[0]) assert (since_r0p <= (QUARTER + 1) * K + DMAX + 2 * H && dr0p >= DMIN && dr0p <= DMAX);
      // the latched rise timestamp of pin 0 vs its true rise
      // (since_r0 is bounded first, so that dr0, computed modulo 2**GW ticks, can't alias)
      if (!rise_stale[0] && !pend_r0 && !rise[0]) assert (since_r0 <= (QUARTER + 1) * K + DMAX && dr0 >= DMIN && dr0 <= DMAX);
      if (!rise_stale[0]) assert (seen_r0 && (now - rise_ts[0+:TS_W]) <= QUARTER);
      if (seen_r0) assert (since_r0 != 0);
    end
  end

`endif

  // ---------------------------------------------------------------------------
  // Covers: every verdict is reachable, INDETERMINATE on both sides of the
  // bound, and a measurement error above one cycle (so u = 1 cannot be sound).
  // ---------------------------------------------------------------------------
  wire indet_p = !pass_p && !viol_p;
  always @($global_clock) begin
    if (judge_p) begin
      cover (!max_pulse && pass_p);
      cover (!max_pulse && viol_p);
      cover (!max_pulse && indet_p && t_pulse >= np);
      cover (!max_pulse && indet_p && t_pulse < np);
      cover (max_pulse && pass_p);
      cover (max_pulse && viol_p);
      cover (max_pulse && indet_p && t_pulse <= np);
      cover (max_pulse && indet_p && t_pulse > np);
      cover (err_p > K);  // measured more than one cycle long
      cover (err_p < -K);  // measured more than one cycle short
      cover (rise_stale[0] && !pass_p && !viol_p);  // a long pulse: flagged stale, INDETERMINATE
    end
    if (judge_x) begin
      cover (pass_x);
      cover (viol_x);
      cover (sn_ord_r[0] && !pend_r0 && since_r0 < since_r1);  // X after the trigger, flagged ORD
      cover (sn_ord_r[0] && since_r0 >= since_r1);  // X just before the trigger, flagged ORD
      cover (!sn_ord_r[0] && pre0 && !sn_rstale[0] && since_r0 < since_r1);  // pin 0 rose after the trigger, too late for the snapshot
    end
    cover (lost_now);  // a trigger lost while a snapshot is held
  end

endmodule

`default_nettype wire
