// SPDX-License-Identifier: Apache-2.0
//
// Empty stand-ins for the two ILA IPs that fpga/build.tcl creates
// (clk_ila_wiz: Clocking Wizard on the board oscillator; ila_bus: the ILA),
// with the same ports fpga/rtl/fpga_top.v connects. Used by:
//   - fpga/build.tcl when the ILA is switched off (ILA clock 0): no clock, no
//     ILA, so Vivado removes the whole capture logic;
//   - the lint and the Yosys estimate in fpga/sim/run.sh (the estimate then
//     excludes the ILA; its own cost is in fpga/README.md).
// The Icarus simulation uses fpga/sim/ila_model.v instead (a clock and a CSV
// writer in Vivado's format).
`default_nettype none
/* verilator lint_off DECLFILENAME */
/* verilator lint_off UNUSEDSIGNAL */
module clk_ila_wiz (
    input  wire clk_in1,
    output wire clk_out1,
    output wire locked
);
  assign clk_out1 = 1'b0;
  assign locked   = 1'b0;
endmodule

module ila_bus (
    input wire        clk,
    input wire [ 0:0] probe0,
    input wire [ 0:0] probe1,
    input wire [ 0:0] probe2,
    input wire [ 0:0] probe3,
    input wire [ 0:0] probe4,
    input wire [ 7:0] probe5,
    input wire [ 7:0] probe6,
    input wire [ 7:0] probe7,
    input wire [ 0:0] probe8,
    input wire [ 0:0] probe9,
    input wire [ 0:0] probe10,
    input wire [39:0] probe11
);
endmodule
/* verilator lint_on UNUSEDSIGNAL */
`default_nettype wire
