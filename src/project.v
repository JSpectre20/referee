/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * Referee: programmable protocol emulator and bus referee.
 *
 * This build: one Referee engine (ISA v0.3, docs/isa-timing.md) with its
 * edge-timestamp unit, snapshot, verdict block and stats, loaded and run by a
 * host over a UART.
 *
 *   host (PC or the demo board's RP2350)
 *     -> ui_in[3] -> uart_rx -> host_port (v2) -> engine_sys -> pins
 *     <- uo_out[4] <- uart_tx <-'
 *
 * Pins
 *   ui_in[3]       UART RX from the host, 115200 8N1 at power-up (Tiny Tapeout
 *                  UART pinout); the host can switch to clock/20 = 3 Mbaud at 60 MHz ('B')
 *   uo_out[4]      UART TX to the host (never an engine output)
 *   ui_in[4..7]    judge channels 0..3 (default mux): input-only pads, so judging
 *                  them can't disturb the bus (non-intrusive, NI)
 *   ui_in[0..2]    engine inputs 8..10 (ui_in[3], the host RX, is input 11)
 *   uio[0..7]      engine pins 0..7: input, push-pull or open-drain (global config)
 *   uo_out[0..3,5..7]  engine pins 8..14 (push-pull)
 * Host protocol: src/host_port.v, tools/referee_host.py.
 */

`default_nettype none

module tt_um_shreyansh_referee #(
    // Power-up baud 115200 at the project clock in info.yaml (60 MHz: 520.8 -> 521).
    // Must equal round(clock_hz / 115200): test/test.py checks it against info.yaml.
    parameter integer CLKS_PER_BIT = 521
) (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  // Reset synchronizer. rst_n comes from the demo board's microcontroller and
  // is not aligned to clk, so it passes two flops before the rest of the
  // design uses it as a synchronous reset. (These two flops are the only ones
  // without a reset of their own: they ARE the reset.) Reset takes effect 2
  // cycles after rst_n falls and ends 2 cycles after it rises.
  reg rst_m, rst_s;
  always @(posedge clk) begin
    rst_m <= rst_n;
    rst_s <= rst_m;
  end

  // ---------------------------------------------------------------------------
  // UART to the host, with a bit period the host can change ('B' command)
  // ---------------------------------------------------------------------------
  wire [15:0] div;
  wire [7:0] rx_data;
  wire rx_valid, rx_ferr;
  uart_rx u_rx (
      .clk(clk), .rst_n(rst_s), .div(div), .rx(ui_in[3]),
      .data(rx_data), .valid(rx_valid), .frame_err(rx_ferr)
  );

  wire [7:0] tx_data;
  wire tx_start, tx_busy, tx;
  uart_tx #(
      .CLKS_PER_BIT(CLKS_PER_BIT)
  ) u_tx (
      .clk(clk), .rst_n(rst_s), .div(div), .data(tx_data), .start(tx_start), .tx(tx), .busy(tx_busy)
  );

  // ---------------------------------------------------------------------------
  // Host port v2 <-> engine subsystem
  // ---------------------------------------------------------------------------
  wire h_gwe, h_gcrc_we, h_we, h_crc_we, h_run, h_halt, h_in_push, h_out_pop, h_vf_pop;
  wire h_stats_latch, h_stats_clear, h_lost_clear, h_status_clear;
  wire [2:0] h_gaddr, h_stats_idx;
  wire [6:0] h_addr, h_raddr;
  wire [15:0] h_gwdata, h_gcrc, h_wdata, h_crc, h_grdata, h_rdata, vf_data, stats_rdata, crc_got_g, crc_got_e;
  wire [7:0] h_in_data, out_data, lost, status;
  wire [1:0] h_stats_slot;
  wire [3:0] vf_count;
  wire in_full, out_empty, halted, running, checking, ni;

  host_port #(
      .DIV_RESET(CLKS_PER_BIT)
  ) u_host (
      .clk(clk), .rst_n(rst_s),
      .rx_data(rx_data), .rx_valid(rx_valid), .rx_ferr(rx_ferr),
      .tx_data(tx_data), .tx_start(tx_start), .tx_busy(tx_busy), .div(div),
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

  wire [7:0] uo_eng;
  engine_sys u_sys (
      .clk(clk), .rst_n(rst_s), .ui_in(ui_in), .uio_in(uio_in), .uio_out(uio_out), .uio_oe(uio_oe),
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

  // uo_out[4] is the host UART TX; engine_sys never drives it (uo_eng[4] = 0).
  assign uo_out = {uo_eng[7:5], tx, uo_eng[3:0]};

  wire _unused = &{ena, uo_eng[4], 1'b0};

endmodule

`default_nettype wire
