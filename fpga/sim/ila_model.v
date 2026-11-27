// SPDX-License-Identifier: Apache-2.0
//
// Simulation stand-ins for the two ILA IPs of fpga/build.tcl, for the Icarus
// run of fpga/sim/tb_fpga.v (Icarus -g2012 only):
//
//   clk_ila_wiz  its own 240 MHz-ish clock (4.167 ns = 2.083 + 2.084 ns, i.e.
//                239.98 MHz), unrelated in phase and frequency to clk60, as the
//                board oscillator's MMCM is on the board.
//   ila_bus      once armed (task arm), writes every sample with probe10
//                (ila_chg) = 1 to a CSV file in the layout of Vivado's
//                write_hw_ila_data -csv_file (a header row of probe names, a
//                "Radix - ..." row, one row per stored sample), i.e. what the
//                ILA stores with fpga/ila_capture.tcl's capture control
//                "ila_chg == 1". TRIGGER is 1 in the first stored sample where
//                the SCL probe fell. DEPTH samples at most, as the real buffer.
//                File: +ila_csv=<path>, default fpga/sim/out/ila_run/ila.csv.
//
// tools/ila_check.py then reads this file exactly as it reads a real capture.
`timescale 1ns / 1ps
`default_nettype none
/* verilator lint_off DECLFILENAME */
module clk_ila_wiz (
    input  wire clk_in1,
    output reg  clk_out1,
    output reg  locked
);
  initial begin
    clk_out1 = 1'b0;
    locked = 1'b0;
    #1.3;  // an arbitrary phase against clk60
    forever begin
      #2.083 clk_out1 = 1'b1;
      #2.084 clk_out1 = 1'b0;
    end
  end
  initial #500 locked = 1'b1;
  wire _unused = clk_in1;
endmodule

module ila_bus #(
    parameter integer DEPTH = 8192
) (
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
  reg armed = 1'b0, triggered = 1'b0, last_scl = 1'b1;
  integer fd = 0, n = 0;
  reg [8*256-1:0] path;

  task arm;
    begin
      if (!$value$plusargs("ila_csv=%s", path)) path = "fpga/sim/out/ila_run/ila.csv";
      fd = $fopen(path, "w");
      if (fd == 0) $display("FAIL: ila_model cannot write %0s", path);
      $fwrite(fd, "Sample in Buffer,Sample in Window,TRIGGER,ila_scl,ila_sda,ila_scl_chip,");
      $fwrite(fd, "ila_sda_chip,ila_uart_rx,ila_uo_out[7:0],ila_uio_in[7:0],ila_uio_oe[7:0],");
      $fwrite(fd, "ila_rst_n,ila_mark,ila_chg,ila_ts[39:0]\n");
      $fwrite(fd, "Radix - UNSIGNED,UNSIGNED,UNSIGNED,BINARY,BINARY,BINARY,BINARY,BINARY,");
      $fwrite(fd, "HEX,HEX,HEX,BINARY,BINARY,BINARY,HEX\n");
      n = 0;
      triggered = 1'b0;
      last_scl = probe0;
      armed = 1'b1;
    end
  endtask

  task stop;
    begin
      if (armed && fd != 0) $fclose(fd);
      armed = 1'b0;
      $display("ila_model: %0d samples stored", n);
    end
  endtask

  reg trig;
  always @(posedge clk) begin
    if (armed && probe10[0] && n < DEPTH) begin
      trig = !triggered && last_scl && !probe0[0];
      if (trig) triggered = 1'b1;
      $fwrite(fd, "%0d,%0d,%0d,%b,%b,%b,%b,%b,%h,%h,%h,%b,%b,%b,%h\n", n, n, trig, probe0, probe1,
              probe2, probe3, probe4, probe5, probe6, probe7, probe8, probe9, probe10, probe11);
      last_scl = probe0[0];
      n = n + 1;
    end
  end
endmodule
`default_nettype wire
