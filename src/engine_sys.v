/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * engine_sys: the engine subsystem (v0: one engine).
 *
 *   - 2-flop synchronizers for engine inputs 0..11 (uio_in[0..7], ui_in[0..3])
 *   - judge-channel mux: channel ch = ui_in[4+ch] (default, input-only pads:
 *     non-intrusive) or uio_in[0..7] (global config word 6)
 *   - edge_ts, 4 channels, 16-bit timestamps; engine inputs 12..15 are its
 *     synchronized levels, so WAITP and the timestamps see the same signal
 *   - the global config (8 words; written only while every engine is halted)
 *     and its decode: per output pin owner + mode
 *   - engine 0, and the pad drivers: push-pull drives the value; open-drain
 *     drives 0 or releases (oe = 0); unowned pins are not driven
 *
 * Output pins: 0..7 = uio[0..7]; 8..14 = uo_out[0,1,2,3,5,6,7]. uo_out[4] is
 * the host UART TX and is never driven from here.
 */

`default_nettype none

module engine_sys (
    input  wire        clk,
    input  wire        rst_n,

    input  wire [ 7:0] ui_in,
    input  wire [ 7:0] uio_in,
    output wire [ 7:0] uio_out,
    output wire [ 7:0] uio_oe,
    output wire [ 7:0] uo_eng,   // engine-driven uo_out bits (bit 4 always 0)

    // host: global image
    input  wire        h_gwe,
    input  wire [ 2:0] h_gaddr,
    input  wire [15:0] h_gwdata,
    output wire [15:0] h_grdata,
    input  wire        h_gcrc_we,
    input  wire [15:0] h_gcrc,

    // host: engine 0 (see engine.v)
    input  wire        h_we,
    input  wire [ 6:0] h_addr,
    input  wire [15:0] h_wdata,
    input  wire [ 6:0] h_raddr,
    output wire [15:0] h_rdata,
    input  wire        h_crc_we,
    input  wire [15:0] h_crc,
    input  wire        h_run,
    input  wire        h_halt,
    input  wire        h_in_push,
    input  wire [ 7:0] h_in_data,
    output wire        in_full,
    input  wire        h_out_pop,
    output wire [ 7:0] out_data,
    output wire        out_empty,
    input  wire        h_vf_pop,
    output wire [15:0] vf_data,
    output wire [ 3:0] vf_count,
    input  wire        h_stats_latch,
    input  wire [ 1:0] h_stats_slot,
    input  wire [ 2:0] h_stats_idx,
    output wire [15:0] stats_rdata,
    input  wire        h_stats_clear,
    input  wire        h_lost_clear,
    output wire [ 7:0] lost,
    input  wire        h_status_clear,
    output wire        halted,
    output wire        running,
    output wire        checking,
    output wire [15:0] crc_got_g,
    output wire [15:0] crc_got_e,
    output wire [ 7:0] status,
    output wire        ni
);

  // ---------------------------------------------------------------------------
  // Time base: free-running cycle counter shared by edge_ts and the engine
  // ---------------------------------------------------------------------------
  reg [15:0] now;
  always @(posedge clk) begin
    if (!rst_n) now <= 16'd0;
    else now <= now + 16'd1;
  end

  // ---------------------------------------------------------------------------
  // Global config
  // ---------------------------------------------------------------------------
  reg [15:0] glob[0:7];
  integer gi;
  always @(posedge clk) begin
    if (!rst_n) begin
      for (gi = 0; gi < 8; gi = gi + 1) glob[gi] <= 16'd0;  // all pins unowned, mux = ui_in
`ifdef ENGINE_FORMAL_START_RUNNING
      // formal reachability tasks only (see engine.v): uio[0..7] owned by
      // engine 0, open-drain, so the pad covers are reachable without a RUN check
      glob[2] <= 16'h6666;
      glob[3] <= 16'h6666;
`endif
    end else if (h_gwe && halted) begin
      glob[h_gaddr] <= h_gwdata;
    end
  end
  reg [15:0] g_crc_exp;
  always @(posedge clk) begin
    if (!rst_n) g_crc_exp <= 16'd0;
    else if (h_gcrc_we) g_crc_exp <= h_gcrc;
  end
  assign h_grdata = glob[h_gaddr];
  wire [2:0] gw_addr;
  wire [15:0] gw_data = glob[gw_addr];

  // per output pin: owner (2 bits) and mode (2 bits), 4 pins per word (words 2..5)
  wire [63:0] pin_nib = {glob[5], glob[4], glob[3], glob[2]};
  wire [14:0] owned;
  wire [ 7:0] od_mode;
  wire [14:0] pp_mode;
  genvar p;
  generate
    for (p = 0; p < 15; p = p + 1) begin : g_pin
      wire [1:0] owner = pin_nib[4*p+2+:2];
      wire [1:0] mode = pin_nib[4*p+:2];
      // engine 0 (owner code 1) may drive pin p if it owns it in a driving mode;
      // uo_out pins (8..14) are push-pull only
      assign owned[p] = (owner == 2'd1) && (mode != 2'd0) && ((p < 8) || (mode == 2'd1));
      assign pp_mode[p] = (mode == 2'd1);
      if (p < 8) begin : g_od
        assign od_mode[p] = (mode == 2'd2);
      end
    end
  endgenerate

  // judge-channel mux (word 6): 0 = ui_in[4+ch], 1..8 = uio_in[0..7]
  wire [3:0] ch_raw;
  wire [3:0] ch_src_is_ui;
  generate
    for (p = 0; p < 4; p = p + 1) begin : g_mux
      wire [3:0] src = glob[6][4*p+:4];
      assign ch_raw[p] = (src == 4'd0) ? ui_in[4+p] : uio_in[src[2:0] - 3'd1];
      assign ch_src_is_ui[p] = (src == 4'd0);
    end
  endgenerate
  assign ni = &ch_src_is_ui;

  // ---------------------------------------------------------------------------
  // Synchronizers for engine inputs 0..11
  // ---------------------------------------------------------------------------
  reg [11:0] sync1, sync2;
  always @(posedge clk) begin
    if (!rst_n) begin
      sync1 <= 12'd0;
      sync2 <= 12'd0;
    end else begin
      sync1 <= {ui_in[3:0], uio_in};
      sync2 <= sync1;
    end
  end

  // ---------------------------------------------------------------------------
  // edge_ts
  // ---------------------------------------------------------------------------
  wire [3:0] et_rise, et_fall, et_level, et_toggled, et_rise_stale, et_fall_stale, et_missed, et_last_fall;
  wire [63:0] et_rise_ts, et_fall_ts;
  wire et_clear_missed;
  edge_ts #(
      .N(4), .TS_W(16)
  ) u_edge (
      .clk(clk), .rst_n(rst_n), .pins(ch_raw), .now(now), .clear({4{et_clear_missed}}),
      .level(et_level), .rise(et_rise), .fall(et_fall),
      .rise_ts(et_rise_ts), .fall_ts(et_fall_ts), .toggled(et_toggled),
      .rise_stale(et_rise_stale), .fall_stale(et_fall_stale), .missed(et_missed),
      .last_fall(et_last_fall)
  );

  // ---------------------------------------------------------------------------
  // Engine 0
  // ---------------------------------------------------------------------------
  wire [14:0] out_val;
`ifdef FORMAL
  wire [14:0] f_drive;
  wire f_halting;
`endif
  engine u_eng (
      .clk(clk), .rst_n(rst_n), .now(now),
      .in_sync({et_level, sync2}),
      .et_rise(et_rise), .et_fall(et_fall), .et_rise_ts(et_rise_ts), .et_fall_ts(et_fall_ts),
      .et_rise_stale(et_rise_stale), .et_fall_stale(et_fall_stale), .et_missed(et_missed),
      .et_last_fall(et_last_fall), .et_clear_missed(et_clear_missed),
      .owned(owned), .od_mode(od_mode), .ni(ni),
      .gw_addr(gw_addr), .gw_data(gw_data), .g_crc_exp(g_crc_exp),
      .out_val(out_val),
      .h_we(h_we), .h_addr(h_addr), .h_wdata(h_wdata), .h_raddr(h_raddr), .h_rdata(h_rdata),
      .h_crc_we(h_crc_we), .h_crc(h_crc), .h_run(h_run), .h_halt(h_halt),
      .h_in_push(h_in_push), .h_in_data(h_in_data), .in_full(in_full),
      .h_out_pop(h_out_pop), .out_data(out_data), .out_empty(out_empty),
      .h_vf_pop(h_vf_pop), .vf_data(vf_data), .vf_count(vf_count),
      .h_stats_latch(h_stats_latch), .h_stats_slot(h_stats_slot), .h_stats_idx(h_stats_idx),
      .stats_rdata(stats_rdata), .h_stats_clear(h_stats_clear),
      .h_lost_clear(h_lost_clear), .lost(lost), .h_status_clear(h_status_clear),
      .halted(halted), .running(running), .checking(checking),
      .crc_got_g(crc_got_g), .crc_got_e(crc_got_e), .status(status)
`ifdef FORMAL
      , .f_drive(f_drive), .f_halting(f_halting)
`endif
  );

  // ---------------------------------------------------------------------------
  // Pads
  // ---------------------------------------------------------------------------
  generate
    for (p = 0; p < 8; p = p + 1) begin : g_uio
      // push-pull: drive the value; open-drain: drive 0 when the value is 0, else release
      assign uio_oe[p] = owned[p] && (pp_mode[p] || !out_val[p]);
      assign uio_out[p] = owned[p] && pp_mode[p] && out_val[p];
    end
  endgenerate
  // pins 8..14 -> uo_out[0,1,2,3,5,6,7]
  assign uo_eng = {
    owned[14] & out_val[14], owned[13] & out_val[13], owned[12] & out_val[12], 1'b0,
    owned[11] & out_val[11], owned[10] & out_val[10], owned[9] & out_val[9], owned[8] & out_val[8]
  };

  wire _unused = &{1'b0, et_toggled, pp_mode[14:8], pin_nib[63:60]};  // pin 15 is reserved

`ifdef FORMAL
  // ===========================================================================
  // Pad and global-config properties (docs/isa-timing.md section 9), proved by
  // formal/engine.sby together with the engine's own (src/engine.v).
  // ===========================================================================
  reg f_past;  // the last cycle was out of reset
  always @(posedge clk) f_past <= rst_n;

  // what each output pin presents: uio = {oe, out}; uo_out bits = value
  wire [29:0] f_pad = {
    2'b00, uo_eng[7], uo_eng[6], uo_eng[5], uo_eng[3], uo_eng[2], uo_eng[1], uo_eng[0],
    uio_oe[7], uio_out[7], uio_oe[6], uio_out[6], uio_oe[5], uio_out[5], uio_oe[4], uio_out[4],
    uio_oe[3], uio_out[3], uio_oe[2], uio_out[2], uio_oe[1], uio_out[1], uio_oe[0], uio_out[0]
  };
  (* anyconst *) reg [2:0] f_gidx;  // any global config word
  integer fp;

  always @(posedge clk) begin
    if (rst_n && f_past) begin
      // the global config (pin owners and modes, channel mux) changes only by a
      // host write while the engine is halted
      if (glob[f_gidx] != $past(glob[f_gidx])) assert ($past(h_gwe && halted && h_gaddr == f_gidx));

      // -- pads change only one cycle after a SET / SHO, and only for owned pins
      //    (checked over a stretch where the engine runs throughout; while it is
      //    halted, the host may change the config and with it the pads)
      if ($past(running) && running) begin
        for (fp = 0; fp < 8; fp = fp + 1)
          if ({uio_oe[fp], uio_out[fp]} != $past({uio_oe[fp], uio_out[fp]})) assert ($past(f_drive[fp]) && owned[fp]);
        for (fp = 8; fp < 15; fp = fp + 1)
          if (f_pad[16+fp-8] != $past(f_pad[16+fp-8])) assert ($past(f_drive[fp]) && owned[fp]);
      end
      // an unowned pin is never driven, and uo_out[4] (host TX) never by the engine
      for (fp = 0; fp < 8; fp = fp + 1) if (!owned[fp]) assert (!uio_oe[fp] && !uio_out[fp]);
      assert (!uo_eng[4]);

      // -- HALT releases open-drain pins: after a halt, no open-drain pin pulls low
      if ($past(f_halting))
        for (fp = 0; fp < 8; fp = fp + 1) if (od_mode[fp]) assert (!uio_oe[fp]);
    end
  end

  always @(posedge clk) begin
    if (rst_n && f_past) begin
      cover ($past(running) && running && uio_oe[0] != $past(uio_oe[0]));  // an open-drain pin pulled low / released
      cover ($past(f_halting) && od_mode[1] && $past(uio_oe[1]));  // a halt releasing a held-low pin
    end
  end
`endif

endmodule

`default_nettype wire
