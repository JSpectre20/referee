// Formal harness: src/engine_sys.v (engine + edge_ts + snapshot + global config
// + pads) with every input free: pins, host strobes and data. The program,
// bounds and claimed-pin mask are whatever the host writes, or arbitrary from
// the start (the image flip-flops have no reset). The properties are in
// src/engine.v and src/engine_sys.v under `ifdef FORMAL`. See formal/engine.sby.
`default_nettype none

module engine_formal ();
  (* gclk *) reg clk;

  reg f_init = 1'b1;
  always @(posedge clk) f_init <= 1'b0;

  (* anyseq *) reg rst_n;
  (* anyseq *) reg [7:0] ui_in, uio_in;
  (* anyseq *) reg h_gwe, h_gcrc_we, h_we, h_crc_we, h_run, h_halt, h_in_push, h_out_pop, h_vf_pop;
  (* anyseq *) reg h_stats_latch, h_stats_clear, h_lost_clear, h_status_clear;
  (* anyseq *) reg [2:0] h_gaddr, h_stats_idx;
  (* anyseq *) reg [6:0] h_addr, h_raddr;
  (* anyseq *) reg [15:0] h_gwdata, h_gcrc, h_wdata, h_crc;
  (* anyseq *) reg [7:0] h_in_data;
  (* anyseq *) reg [1:0] h_stats_slot;
  always @(*) if (f_init) assume (!rst_n);

  wire [7:0] uio_out, uio_oe, uo_eng, out_data, lost, status;
  wire [15:0] h_grdata, h_rdata, vf_data, stats_rdata, crc_got_g, crc_got_e;
  wire [3:0] vf_count;
  wire in_full, out_empty, halted, running, checking, ni;

  engine_sys dut (
      .clk(clk), .rst_n(rst_n), .ui_in(ui_in), .uio_in(uio_in), .uio_out(uio_out), .uio_oe(uio_oe),
      .uo_eng(uo_eng),
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

`default_nettype wire
