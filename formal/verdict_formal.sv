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
//     time), judged when pin 1's rise is reported.
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
    parameter integer SHARP = 0   // verdict rule variant (see src/verdict.v)
) ();
  localparam integer K = 1 << K_W;
  localparam integer TW = TS_W + K_W + 1;  // tick counters (one bit spare)
  localparam integer N_W = TS_W + K_W;  // bound width, ticks
  localparam integer H = 4 * K + 2;  // minimum ticks between changes of one pin
  localparam integer LIMIT = (1 << (TS_W - 2)) * K;  // judge intervals shorter than this
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
      always @($global_clock) age[i] <= chg ? 8'd1 : (&age[i] ? age[i] : age[i] + 1'b1);
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
  wire [1:0] rise, fall, level, toggled;
  wire [2*TS_W-1:0] rise_ts, fall_ts;
  wire [1:0] f_warm;
  wire [1:0] f_s1, f_s2, f_s3;
  edge_ts #(
      .N(2), .TS_W(TS_W)
  ) dut (
      .clk(clk), .rst_n(rst_n), .pins(din), .now(now), .clear(2'b00),
      .level(level), .rise(rise), .fall(fall),
      .rise_ts(rise_ts), .fall_ts(fall_ts), .toggled(toggled),
      .f_warm(f_warm), .f_s1(f_s1), .f_s2(f_s2), .f_s3(f_s3)
  );

  // ---------------------------------------------------------------------------
  // Ghost bookkeeping (true intervals, in ticks)
  // ---------------------------------------------------------------------------
  wire rise0_now = tp[0] & ~tp1[0], fall0_now = ~tp[0] & tp1[0];
  wire rise1_now = tp[1] & ~tp1[1];

  reg [TW-1:0] since_r0;  // ticks since pin 0 truly rose
  reg [TW-1:0] t_pulse;  // pin 0 width, captured when it truly falls
  reg [TW-1:0] t_cross;  // pin 0 rise -> pin 1 rise, captured at pin 1's rise
  reg cross_ok;  // no pin 0 rise between pin 1's rise and its report
  reg pend_r0, pend_f0, pend_r1, seen_r0;

  always @($global_clock) begin
    since_r0 <= rise0_now ? {{(TW - 1) {1'b0}}, 1'b1} : (since_r0 == SAT ? SAT : since_r0 + 1'b1);
    if (fall0_now) t_pulse <= since_r0;
    if (rise1_now) begin
      t_cross  <= rise0_now ? {TW{1'b0}} : since_r0;
      cross_ok <= 1'b1;
    end else if (rise0_now) cross_ok <= 1'b0;

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
  // Pin 0's rise timestamp; if it is being reported this very cycle, it is `now`.
  wire [TS_W-1:0] ts_r0 = rise[0] ? now : rise_ts[0+:TS_W];
  wire signed [TS_W-1:0] m_pulse = now - rise_ts[0+:TS_W];
  wire signed [TS_W-1:0] m_cross = now - ts_r0;

  (* anyconst *) reg [N_W-1:0] n_pulse, n_cross;
  (* anyconst *) reg max_pulse, max_cross;
  wire pass_p, viol_p, pass_x, viol_x;

  verdict #(
      .M_W(TS_W), .FRAC_W(K_W), .N_W(N_W), .U_FX(U_CYC * K), .SHARP(SHARP)
  ) v_pulse (
      .m(m_pulse), .n_fx(n_pulse), .is_max(max_pulse), .pass(pass_p), .violation(viol_p)
  );
  verdict #(
      .M_W(TS_W), .FRAC_W(K_W), .N_W(N_W), .U_FX(U_CYC * K), .SHARP(SHARP)
  ) v_cross (
      .m(m_cross), .n_fx(n_cross), .is_max(max_cross), .pass(pass_x), .violation(viol_x)
  );

  wire judge_p = rst_n && fall[0] && seen_r0 && !pend_r0 && (t_pulse < LIMIT);
  // (pin 0's rise must have been reported already, or be reported this cycle)
  wire judge_x = rst_n && rise[1] && cross_ok && (seen_r0 || rise[0]) && (!pend_r0 || rise[0]) && (t_cross < LIMIT);
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
    end
    if (judge_x) begin
      cover (pass_x);
      cover (viol_x);
    end
  end

endmodule

`default_nettype wire
