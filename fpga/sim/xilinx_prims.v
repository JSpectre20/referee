// SPDX-License-Identifier: Apache-2.0
// Behavioural IOBUF for the lint and the Icarus simulation only (Vivado and
// Yosys use their own Xilinx primitive). Same ports and meaning as UNISIM:
// T = 1 -> high impedance, T = 0 -> drive I; O always reads the pad.
`default_nettype none
/* verilator lint_off DECLFILENAME */
module IOBUF (
    output wire O,
    inout  wire IO,
    input  wire I,
    input  wire T
);
  assign IO = T ? 1'bz : I;
  assign O  = IO;
endmodule
`default_nettype wire
