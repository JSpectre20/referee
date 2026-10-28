/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * verdict: the referee's three-valued rule (PLAN.md 4.8), as hardware.
 *
 * Given an interval measured from edge timestamps (m, in clock cycles), a
 * timing bound from a protocol contract (n_fx, in fixed point: cycles with
 * FRAC_W fractional bits, so a bound such as 600 ns = 24.0 cycles at 40 MHz,
 * or 62.4 cycles at 48 MHz to 1/16 cycle, is represented exactly enough),
 * and whether it is a minimum or a maximum bound, return:
 *
 *   PASS           the true interval certainly meets the bound
 *   VIOLATION      the true interval certainly breaks the bound
 *   INDETERMINATE  the measurement can't tell (true interval within the
 *                  instrument's uncertainty of the bound)
 *
 * With U = the measurement uncertainty (U_FX, same fixed point as n_fx):
 *   minimum bound  PASS if m - U >= n      VIOLATION if m + U <= n  (< n if SHARP = 0)
 *   maximum bound  PASS if m + U <= n      VIOLATION if m - U >= n  (> n if SHARP = 0)
 *   The sharper VIOLATION test (default) is sound because the measurement
 *   error is strictly below U = 2 cycles (at most 1 + 2/K cycles in the proof).
 *
 * formal/verdict.sby proves this rule sound on top of src/edge_ts.v, with
 * external edges at any sub-cycle time and either outcome of a sample taken
 * right at an edge: PASS => the true interval meets the bound, VIOLATION =>
 * it breaks it. U = 2 cycles is proved sound; U = 1 is not, and the proof's
 * counterexample is kept in docs/results/. See docs/verdict.md.
 *
 * `m` is signed: two external edges a fraction of a cycle apart can be
 * measured in either order.
 */

`default_nettype none

module verdict #(
    parameter integer M_W    = 16,              // width of m (signed cycles)
    parameter integer FRAC_W = 4,               // fractional bits of n_fx and U_FX
    parameter integer N_W    = M_W + FRAC_W,    // width of n_fx (unsigned)
    parameter integer U_FX   = 2 << FRAC_W,     // uncertainty: 2 cycles
    parameter integer SHARP  = 1                // 1: VIOLATION also when m+U == n (min) / m-U == n (max); proved sound
) (
    input  wire signed [M_W-1:0] m,        // measured interval, cycles
    input  wire        [N_W-1:0] n_fx,     // bound, cycles in fixed point
    input  wire                  is_max,   // 0: minimum bound, 1: maximum bound
    input  wire                  invalid,  // stale timestamp or `missed` pin: force INDETERMINATE
    output wire                  pass,
    output wire                  violation  // neither: INDETERMINATE
);

  // Everything in one signed fixed-point width that can't overflow:
  // m scaled by 2**FRAC_W, plus or minus U, compared with n.
  localparam integer W = (M_W + FRAC_W > N_W ? M_W + FRAC_W : N_W) + 2;

  wire signed [W-1:0] mk = $signed({{(W - M_W - FRAC_W) {m[M_W-1]}}, m, {FRAC_W{1'b0}}});
  wire signed [W-1:0] u = U_FX[W-1:0];
  wire signed [W-1:0] n = $signed({{(W - N_W) {1'b0}}, n_fx});
  wire signed [W-1:0] lo = mk - u;  // the true interval is above this
  wire signed [W-1:0] hi = mk + u;  // ... and below this

  wire min_pass = (lo >= n);
  wire min_viol = (SHARP != 0) ? (hi <= n) : (hi < n);
  wire max_pass = (hi <= n);
  wire max_viol = (SHARP != 0) ? (lo >= n) : (lo > n);

  assign pass      = !invalid && (is_max ? max_pass : min_pass);
  assign violation = !invalid && (is_max ? max_viol : min_viol);

endmodule

`default_nettype wire
