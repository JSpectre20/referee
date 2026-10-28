// Formal: the verdict block alone (src/verdict.v), combinational theorem
// (docs/isa-timing.md section 9), with the engine's exact parameters.
//
//   For every m, n_lo, inexact, is_max and every true interval T with
//   |m - T| < u (the measurement lemma's bound, u = 2 cycles):
//     minimum bound:  PASS => T >= N      VIOLATION => T < N
//     maximum bound:  PASS => T <= N      VIOLATION => T > N
//   where N is the TRUE bound: N = n_lo when the ns -> cycles conversion was
//   exact, and n_lo < N < n_lo + 1/16 cycle when it was not (`inexact`).
//
// Real numbers, finitely
// ----------------------
// T and N are real. The verdict only sees values on the 1/16-cycle grid (m in
// cycles, n_lo in 1/16, u = 2 cycles), so the outcome of every comparison
// depends only on where T and N fall relative to that grid, and on the order
// of T and N. A finer grid with E extra fractional bits has 2**E - 1 points
// strictly inside each 1/16 gap; two are enough to place T and N in the same
// gap in either order. So E = 2 covers every real configuration, and this
// theorem holds for any K (ticks per cycle): bounds are in 1/16 cycle and the
// lemma (formal/verdict_formal.sv) is in cycles.
//
// Negative controls (must FAIL, see formal/verdict.sby):
//   comb_onesided  the verdict block is given the rounded-down bound only
//                  (inexact forced to 0): PASS of a minimum bound is then
//                  unsound, so the two-sided rounding of section 6 is needed.
//   comb_u1        u = 1 cycle against a lemma error of up to 2 cycles.
`default_nettype none

module verdict_comb #(
    parameter integer U_CYC    = 2,  // u in the verdict block, cycles
    parameter integer ONESIDED = 0   // 1: hide `inexact` from the verdict block
) ();
  localparam integer M_W = 16, FRAC_W = 4, N_W = 20;  // as instantiated in src/engine.v
  localparam integer E = 2;  // extra fractional bits for the true values
  localparam integer F = FRAC_W + E;  // fractional bits of T and N
  localparam integer W = M_W + F + 3;  // signed width with headroom

  (* anyconst *) reg signed [M_W-1:0] m;
  (* anyconst *) reg [N_W-1:0] n_lo;
  (* anyconst *) reg inexact, is_max;
  (* anyconst *) reg signed [W-1:0] t;  // true interval, 2**-F cycles
  (* anyconst *) reg [E-1:0] n_frac;  // where N sits inside the 1/16 gap above n_lo

  wire pass, violation;
  verdict #(
      .M_W(M_W), .FRAC_W(FRAC_W), .N_W(N_W), .U_FX(U_CYC << FRAC_W), .SHARP(1)
  ) dut (
      .m(m), .n_lo(n_lo), .inexact(ONESIDED ? 1'b0 : inexact), .is_max(is_max),
      .invalid(1'b0), .pass(pass), .violation(violation)
  );

  // Everything in units of 2**-F cycles.
  wire signed [W-1:0] mt = $signed({{(W - M_W - F) {m[M_W-1]}}, m, {F{1'b0}}});
  wire signed [W-1:0] nl = $signed({{(W - N_W - E) {1'b0}}, n_lo, {E{1'b0}}});
  wire signed [W-1:0] n_true = nl + $signed({{(W - E) {1'b0}}, n_frac});
  localparam signed [W:0] LEMMA = 2 << F;  // |m - T| < 2 cycles
  // one bit wider than the operands, so the differences can't wrap
  wire signed [W:0] d_mt = mt - t;
  wire signed [W:0] d_tm = t - mt;

  always @(*) begin
    // the true bound: exact, or strictly inside the gap above n_lo
    assume (inexact ? (n_frac != 0) : (n_frac == 0));
    // the measurement lemma
    assume (d_mt < LEMMA && d_tm < LEMMA);

    if (!is_max && pass) assert (t >= n_true);
    if (!is_max && violation) assert (t < n_true);
    if (is_max && pass) assert (t <= n_true);
    if (is_max && violation) assert (t > n_true);

    // not vacuous: every verdict and both kinds of rounding are reachable
    cover (!is_max && pass && inexact);
    cover (!is_max && violation && inexact);
    cover (is_max && pass && inexact);
    cover (is_max && violation && inexact);
    cover (!pass && !violation);
  end

endmodule

`default_nettype wire
