// Formal harness: src/snapshot.v alone, every input free (the edge_ts outputs
// included, so nothing is assumed about them). The properties themselves are
// in src/snapshot.v under `ifdef SNAPSHOT_PROPS`. See formal/engine.sby.
`default_nettype none

module snapshot_formal ();
  (* gclk *) reg clk;

  reg f_init = 1'b1;
  always @(posedge clk) f_init <= 1'b0;

  (* anyseq *) reg rst_n, clear, arm, trig_e, rel;
  (* anyseq *) reg [1:0] trig_ch;
  (* anyseq *) reg [3:0] et_rise, et_fall, et_rise_stale, et_fall_stale, et_missed, et_last_fall;
  (* anyseq *) reg [63:0] et_rise_ts, et_fall_ts;
  always @(*) if (f_init) assume (!rst_n);

  wire S, valid, lost_now;
  wire [1:0] epoch, f_cap_stage;
  wire [63:0] sn_rts, sn_fts;
  wire [3:0] sn_rstale, sn_fstale, sn_missed, sn_last, sn_ord_r, sn_ord_f, f_tm_r, f_tm_f;
  snapshot #(
      .N(4), .CH_W(2), .TS_W(16)
  ) dut (
      .clk(clk), .rst_n(rst_n), .clear(clear), .arm(arm), .trig_ch(trig_ch), .trig_e(trig_e), .rel(rel),
      .et_rise(et_rise), .et_fall(et_fall), .et_rise_ts(et_rise_ts), .et_fall_ts(et_fall_ts),
      .et_rise_stale(et_rise_stale), .et_fall_stale(et_fall_stale), .et_missed(et_missed),
      .et_last_fall(et_last_fall),
      .S(S), .valid(valid), .epoch(epoch), .lost_now(lost_now),
      .sn_rts(sn_rts), .sn_fts(sn_fts), .sn_rstale(sn_rstale), .sn_fstale(sn_fstale),
      .sn_missed(sn_missed), .sn_last(sn_last), .sn_ord_r(sn_ord_r), .sn_ord_f(sn_ord_f),
      .f_cap_stage(f_cap_stage), .f_tm_r(f_tm_r), .f_tm_f(f_tm_f)
  );
endmodule

`default_nettype wire
