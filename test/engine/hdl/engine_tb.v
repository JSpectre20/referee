// Lockstep wrapper for src/engine_sys.v: clock in Verilog, every port as a tb signal.
`default_nettype none
`timescale 1ns / 1ps

module engine_tb;
  reg clk = 1'b0;
  always #12.5 clk = ~clk;

  reg rst_n = 1'b0;
  reg [7:0] ui_in = 8'd0, uio_in = 8'd0;
  wire [7:0] uio_out, uio_oe, uo_eng;
  reg h_gwe = 0, h_gcrc_we = 0, h_we = 0, h_crc_we = 0, h_run = 0, h_halt = 0;
  reg [2:0] h_gaddr = 0;
  reg [15:0] h_gwdata = 0, h_gcrc = 0, h_wdata = 0, h_crc = 0;
  reg [6:0] h_addr = 0, h_raddr = 0;
  wire [15:0] h_grdata, h_rdata;
  reg h_in_push = 0, h_out_pop = 0, h_vf_pop = 0;
  reg [7:0] h_in_data = 0;
  wire in_full, out_empty;
  wire [7:0] out_data, lost, status;
  wire [15:0] vf_data, stats_rdata, crc_got_g, crc_got_e;
  wire [3:0] vf_count;
  reg h_stats_latch = 0, h_stats_clear = 0, h_lost_clear = 0, h_status_clear = 0;
  reg [1:0] h_stats_slot = 0;
  reg [2:0] h_stats_idx = 0;
  wire halted, running, checking, ni;

  engine_sys u_sys (
      .clk(clk), .rst_n(rst_n), .ui_in(ui_in), .uio_in(uio_in),
      .uio_out(uio_out), .uio_oe(uio_oe), .uo_eng(uo_eng),
      .h_gwe(h_gwe), .h_gaddr(h_gaddr), .h_gwdata(h_gwdata), .h_grdata(h_grdata),
      .h_gcrc_we(h_gcrc_we), .h_gcrc(h_gcrc),
      .h_we(h_we), .h_addr(h_addr), .h_wdata(h_wdata), .h_raddr(h_raddr), .h_rdata(h_rdata),
      .h_crc_we(h_crc_we), .h_crc(h_crc), .h_run(h_run), .h_halt(h_halt),
      .h_in_push(h_in_push), .h_in_data(h_in_data), .in_full(in_full),
      .h_out_pop(h_out_pop), .out_data(out_data), .out_empty(out_empty),
      .h_vf_pop(h_vf_pop), .vf_data(vf_data), .vf_count(vf_count),
      .h_stats_latch(h_stats_latch), .h_stats_slot(h_stats_slot), .h_stats_idx(h_stats_idx),
      .stats_rdata(stats_rdata), .h_stats_clear(h_stats_clear),
      .h_lost_clear(h_lost_clear), .lost(lost), .h_status_clear(h_status_clear),
      .halted(halted), .running(running), .checking(checking),
      .crc_got_g(crc_got_g), .crc_got_e(crc_got_e), .status(status), .ni(ni)
  );
endmodule
