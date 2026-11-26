// SPDX-License-Identifier: Apache-2.0
//
// Gate-level simulation only (make GATES=yes GL_POWERUP=0|1|rand; test/README.md).
// NOT part of the design: it is never in info.yaml's source_files.
//
// A behavioural model of sg13cmos5l_dfrbpq_1, the only flip-flop cell the
// hardened netlist uses, that powers up at a DEFINED value instead of X. The
// Makefile compiles it in place of the PDK's model (it removes that one module
// from a copy of the PDK's cell file; every other cell stays the PDK's).
//
// Why: the required gate-level run starts every flop at X (the PDK model), and
// X-propagating simulation is pessimistic. A flop whose synthesized reset path
// reads its own output, e.g. D = NOT(q AND NOT q), is 1 in silicon but stays X
// in simulation (u_sys.crc_got_e0[14] in hardening run 44309280727). Silicon
// powers up at SOME 0/1 value per flop; these runs check the chip from such
// states: all 0, all 1, and random per flop from a seed. They come IN ADDITION
// to the X run, which stays required (an X that survives reset there needs an
// explicit, reviewed waiver, never a relaxed test; test/README.md).
//
// Function, as the PDK model (ihp_dff_r UDP with r = !RESET_B): RESET_B low
// clears Q to 0 at once (asynchronous; the netlist ties it high, the design's
// reset is synchronous); otherwise Q takes D at the rising CLK edge.
// ASSUMES RESET_B is tied high, as in every netlist so far: the model clears on
// a falling RESET_B or on a clock edge while it is low (edge-triggered), so a
// RESET_B held low from time 0 would keep the power-up value until the first
// clock, unlike the PDK's level-sensitive primitive (review 6, 18 Nov). No timing
// checks (the PDK model's are zero-width in this functional simulation too).
//
// Defines (from the Makefile): GL_POWERUP_0, GL_POWERUP_1 or GL_POWERUP_RAND.
// GL_POWERUP_RAND: every flop draws its own bit from one shared $random
// sequence seeded with +GL_SEED=<n> (make GL_SEED=n, default 1); the seed is
// printed in the log, so a failing run can be repeated exactly.

`timescale 1ns / 10ps
`default_nettype none

`ifdef GL_POWERUP_RAND
// The seed, shared by every flop instance. A separate top-level module (the
// Makefile names it with -s), so the netlist and tb.v stay untouched.
module gl_powerup_seed;
  integer seed;
  initial begin
    if (!$value$plusargs("GL_SEED=%d", seed)) seed = 1;
    $display("GL_POWERUP rand: every sg13cmos5l_dfrbpq_1 powers up at a random bit, GL_SEED=%0d", seed);
  end
endmodule
`endif

module sg13cmos5l_dfrbpq_1 (Q, CLK, D, RESET_B);
  output reg Q;
  input CLK, D, RESET_B;

  initial begin : powerup
`ifdef GL_POWERUP_RAND
    reg [31:0] r;
    // #0: after gl_powerup_seed has read the plusarg (still before the first
    // clock edge). Bit 16 of $random: the low bits of its generator are weak.
    #0 r = $random(gl_powerup_seed.seed);
    Q = r[16];
`elsif GL_POWERUP_1
    Q = 1'b1;
`elsif GL_POWERUP_0
    Q = 1'b0;
`else
    // compiled without a GL_POWERUP_* define: refuse to run rather than
    // silently start at some value
    $display("ERROR: test/gl_powerup_dfrbpq.v needs GL_POWERUP_0, GL_POWERUP_1 or GL_POWERUP_RAND");
    $finish;
`endif
  end

  always @(posedge CLK or negedge RESET_B)
    if (!RESET_B) Q <= 1'b0;
    else Q <= D;
endmodule

`default_nettype wire
