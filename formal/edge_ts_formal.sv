// Formal harness for src/edge_ts.v: timestamps are honest about real time.
//
// The outside world is modelled at sub-cycle resolution. Each clock cycle is
// P ticks long. In any cycle a pin may change (if it has been stable for at
// least 4 cycles) at any phase 0..P-1 of that cycle. A change in the last
// tick before the sampling clock edge is inside the metastability window: the
// first synchronizer flop may resolve to the new level or to the old one (a
// free choice each time). The harness records the true time of every change in
// ticks, independently of the design.
//
// Proved for every sequence of changes, phases and metastable outcomes:
//   A1  no phantom edges: every reported edge matches a real change
//   A2  latency: P <= timestamp*P - true_time <= 3P   (1 to 3 cycles)
//   A3  no missed edges: every real change is reported within 3 cycles
//   A4  interval error <= u = 2 cycles, both for a pulse width on one pin
//       (rise to fall) and between pins (pin 0 rise to pin 1 rise, e.g. data
//       edge to clock edge for a setup time)
// plus the sticky bit and latch behaviour. A2 is kept as an invariant on the
// latched timestamps, which is what makes A4 provable by induction.
`default_nettype none

module edge_ts_formal #(
    parameter integer TS_W = 6,  // small, so wrap-around is exercised
    parameter integer PH_W = 2   // P = 2**PH_W ticks per cycle
) (
    input wire clk
);
  localparam integer N = 2;
  localparam integer P = 1 << PH_W;
  localparam integer TW = TS_W + PH_W;  // true time in ticks
  localparam integer Q = 4;  // minimum cycles a pin holds each level

  // ---------------------------------------------------------------------------
  // Reset: low in the first cycle, high afterwards.
  // ---------------------------------------------------------------------------
  reg f_past_valid = 1'b0;
  always @(posedge clk) f_past_valid <= 1'b1;
  reg rst_n;
  always @(*) assume (rst_n == f_past_valid);

  // Free-running cycle counter, shared by the design (`now`) and the harness.
  reg [TS_W-1:0] cyc;
  always @(posedge clk) cyc <= cyc + 1'b1;

  // ---------------------------------------------------------------------------
  // The outside world
  // ---------------------------------------------------------------------------
  (* anyseq *) reg [N-1:0] chg;  // a pin changes during this cycle
  (* anyseq *) reg [N*PH_W-1:0] phase;  // ... at this tick of the cycle
  (* anyseq *) reg [N-1:0] meta_old;  // metastable sample resolves to the old level
  (* anyseq *) reg [N-1:0] clear;

  reg [N-1:0] tl;  // true level (arbitrary at start)
  reg [3:0] age[0:N-1];  // cycles since this pin last changed (saturates)
  reg [N-1:0] pins;  // what the first synchronizer flop sees this cycle

  reg [TW-1:0] tr_rise[0:N-1], tr_fall[0:N-1];  // true times of the last changes
  reg [N-1:0] pend_rise, pend_fall;  // changed, not reported yet
  reg [N-1:0] seen_rise, seen_fall;  // reported at least once since reset

  wire [N-1:0] rise, fall, level, toggled;
  wire [N*TS_W-1:0] rise_ts, fall_ts;
  wire [1:0] f_warm;
  wire [N-1:0] f_s1, f_s2, f_s3;

  edge_ts #(
      .N(N), .TS_W(TS_W)
  ) dut (
      .clk(clk), .rst_n(rst_n), .pins(pins), .now(cyc), .clear(clear),
      .level(level), .rise(rise), .fall(fall),
      .rise_ts(rise_ts), .fall_ts(fall_ts), .toggled(toggled),
      .f_warm(f_warm), .f_s1(f_s1), .f_s2(f_s2), .f_s3(f_s3)
  );

  genvar i;
  generate
    for (i = 0; i < N; i = i + 1) begin : g_world
      wire [PH_W-1:0] ph = phase[i*PH_W+:PH_W];
      wire            in_window = (ph == P - 1);  // last tick before the sampling edge

      // Changes only out of reset and after Q stable cycles.
      always @(*) if (chg[i]) assume (rst_n && age[i] >= Q);

      always @(*) pins[i] = chg[i] ? ((in_window && meta_old[i]) ? tl[i] : ~tl[i]) : tl[i];

      always @(posedge clk) begin
        if (chg[i]) tl[i] <= ~tl[i];

        if (!rst_n) age[i] <= 4'd0;
        else if (chg[i]) age[i] <= 4'd1;
        else if (age[i] != 4'hf) age[i] <= age[i] + 4'd1;

        if (!rst_n) begin
          pend_rise[i] <= 1'b0;
          pend_fall[i] <= 1'b0;
          seen_rise[i] <= 1'b0;
          seen_fall[i] <= 1'b0;
        end else begin
          if (rise[i]) begin
            pend_rise[i] <= 1'b0;
            seen_rise[i] <= 1'b1;
          end
          if (fall[i]) begin
            pend_fall[i] <= 1'b0;
            seen_fall[i] <= 1'b1;
          end
          if (chg[i] && !tl[i]) begin
            pend_rise[i] <= 1'b1;
            tr_rise[i]   <= {cyc, ph};
          end
          if (chg[i] && tl[i]) begin
            pend_fall[i] <= 1'b1;
            tr_fall[i]   <= {cyc, ph};
          end
        end
      end

      // Timestamps scaled to ticks (cycle c starts at tick c*P).
      wire [TW-1:0] now_t = {cyc, {PH_W{1'b0}}};
      wire [TW-1:0] rts_t = {rise_ts[i*TS_W+:TS_W], {PH_W{1'b0}}};
      wire [TW-1:0] fts_t = {fall_ts[i*TS_W+:TS_W], {PH_W{1'b0}}};
      wire [TW-1:0] lat_now_r = now_t - tr_rise[i];
      wire [TW-1:0] lat_now_f = now_t - tr_fall[i];
      wire [TW-1:0] lat_rts = rts_t - tr_rise[i];
      wire [TW-1:0] lat_fts = fts_t - tr_fall[i];

      always @(posedge clk) begin
        if (rst_n) begin
          // A1: no phantom edges.
          if (rise[i]) assert (pend_rise[i]);
          if (fall[i]) assert (pend_fall[i]);
          // A2: latency of the edge being reported now, in ticks.
          if (rise[i]) assert (lat_now_r >= P && lat_now_r <= 3 * P);
          if (fall[i]) assert (lat_now_f >= P && lat_now_f <= 3 * P);
          // A2 as an invariant on the latched timestamps.
          if (seen_rise[i] && !pend_rise[i]) assert (lat_rts >= P && lat_rts <= 3 * P);
          if (seen_fall[i] && !pend_fall[i]) assert (lat_fts >= P && lat_fts <= 3 * P);
          // A3: no missed edges.
          if (pend_rise[i] || pend_fall[i]) assert (age[i] <= 4'd3);
        end
      end

      // Latches and sticky bit.
      always @(posedge clk) begin
        if (f_past_valid && $past(rst_n) && rst_n) begin
          if ($past(rise[i])) assert (rise_ts[i*TS_W+:TS_W] == $past(cyc));
          else assert (rise_ts[i*TS_W+:TS_W] == $past(rise_ts[i*TS_W+:TS_W]));
          if ($past(fall[i])) assert (fall_ts[i*TS_W+:TS_W] == $past(cyc));
          else assert (fall_ts[i*TS_W+:TS_W] == $past(fall_ts[i*TS_W+:TS_W]));
          if ($past(rise[i] | fall[i])) assert (toggled[i]);
          else if ($past(clear[i])) assert (!toggled[i]);
          else assert (toggled[i] == $past(toggled[i]));
        end
      end

      // Inductive strengthening: while warming up the harness and the design
      // count the same cycles and nothing is pending; once a pin has been
      // stable for 3 cycles the synchronizer holds its true level.
      always @(posedge clk) begin
        if (rst_n) begin
          if (f_warm != 2'd3) assert (age[i] == {2'b00, f_warm} && !pend_rise[i] && !pend_fall[i]);
          // (a metastable sample delays the pipeline by one cycle, so s3 is
          // only guaranteed to hold the new level from age 4)
          if (age[i] >= 4'd4 && f_warm == 2'd3) assert (f_s1[i] == tl[i] && f_s2[i] == tl[i] && f_s3[i] == tl[i]);
          if (age[i] == 4'd3 && f_warm == 2'd3) assert (f_s1[i] == tl[i] && f_s2[i] == tl[i]);
          if (age[i] == 4'd2) assert (f_s1[i] == tl[i]);
          if (age[i] >= 4'd4) assert (!pend_rise[i] && !pend_fall[i]);
          if (pend_rise[i]) assert (tl[i] && !f_s3[i]);
          if (pend_fall[i]) assert (!tl[i] && f_s3[i]);
          if (!pend_rise[i] && !pend_fall[i] && f_warm == 2'd3) assert (f_s3[i] == tl[i]);
          if (pend_rise[i] || pend_fall[i]) assert (age[i] != 4'd0);
          if (pend_rise[i]) assert (lat_now_r <= (age[i] * P));
          if (pend_fall[i]) assert (lat_now_f <= (age[i] * P));
          if (pend_rise[i]) assert (lat_now_r >= ((age[i] - 1) * P) + 1);
          if (pend_fall[i]) assert (lat_now_f >= ((age[i] - 1) * P) + 1);
          if (age[i] == 4'd1 && (pend_rise[i] || pend_fall[i])) assert (f_s2[i] == f_s3[i]);
        end
      end
    end
  endgenerate

  // ---------------------------------------------------------------------------
  // A4: interval error <= 2 cycles (u = 2 for two external edges).
  // ---------------------------------------------------------------------------
  // Pulse width on pin 0, measured when its fall is reported.
  wire [TW-1:0] w_meas = {cyc - rise_ts[0+:TS_W], {PH_W{1'b0}}};
  wire [TW-1:0] w_true = tr_fall[0] - tr_rise[0];
  // Pin 0 rise to pin 1 rise, measured when pin 1's rise is reported.
  wire [TW-1:0] x_meas = {cyc - rise_ts[0+:TS_W], {PH_W{1'b0}}};
  wire [TW-1:0] x_true = tr_rise[1] - tr_rise[0];

  // Errors as signed tick counts; the claim is |error| <= 2P.
  wire signed [TW-1:0] w_err = w_meas - w_true;
  wire signed [TW-1:0] x_err = x_meas - x_true;
  localparam signed [TW-1:0] U = 2 * P;

  always @(posedge clk) begin
    if (rst_n) begin
      if (fall[0] && seen_rise[0] && !pend_rise[0]) assert (w_err >= -U && w_err <= U);
      if (rise[1] && seen_rise[0] && !pend_rise[0]) assert (x_err >= -U && x_err <= U);
    end
  end

  // ---------------------------------------------------------------------------
  // Covers: every interesting case is reachable.
  // ---------------------------------------------------------------------------
  always @(posedge clk) begin
    if (rst_n) begin
      cover (rise[0] && g_world[0].lat_now_r == 3 * P - (P - 1));  // late metastable resolution
      cover (rise[0] && g_world[0].lat_now_r == P + 1);  // fastest report
      cover (fall[0] && seen_rise[0]);  // a pulse width measured
      cover (rise[1] && seen_rise[0] && !pend_rise[0]);  // a cross-pin interval measured
      cover (f_past_valid && $past(clear[0]) && $past(rise[0]) && toggled[0]);  // edge beats clear
      cover (fall[0] && cyc < rise_ts[0+:TS_W]);  // measured across a counter wrap
    end
  end

endmodule

`default_nettype wire
