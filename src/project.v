/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * Referee: programmable protocol emulator and bus referee (work in progress).
 *
 * This build: a host loads a program into `seq` (the four-instruction pin
 * sequencer) over a UART and runs it; the program drives the uio pins.
 *
 *   host (PC or the demo board's RP2350)
 *     -> ui_in[3] -> uart_rx -> host_port -> seq -> uio_out[7:0]
 *     <- uo_out[4] <- uart_tx <-'
 *
 * Pins
 *   ui_in[3]   UART RX from the host, 115200 8N1 (Tiny Tapeout UART pinout)
 *   uo_out[4]  UART TX to the host
 *   uo_out[0]  seq is running
 *   uio[7:0]   seq pins; each is an output only once the host sets its bit
 *              with the 'O' command (all inputs after reset)
 * Host protocol: see src/host_port.v and tools/referee_host.py.
 */

`default_nettype none

module tt_um_shreyansh_referee #(
    parameter integer CLKS_PER_BIT = 347  // 115200 baud at the 40 MHz project clock
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

  // UART to the host.
  wire [7:0] rx_data;
  wire rx_valid, rx_ferr;
  uart_rx #(
      .CLKS_PER_BIT(CLKS_PER_BIT)
  ) u_rx (
      .clk(clk), .rst_n(rst_s), .rx(ui_in[3]),
      .data(rx_data), .valid(rx_valid), .frame_err(rx_ferr)
  );

  wire [7:0] tx_data;
  wire tx_start, tx_busy, tx;
  uart_tx #(
      .CLKS_PER_BIT(CLKS_PER_BIT)
  ) u_tx (
      .clk(clk), .rst_n(rst_s), .data(tx_data), .start(tx_start), .tx(tx), .busy(tx_busy)
  );

  // Command port: loads and runs seq.
  wire run, prog_we, sr_we;
  wire [4:0] prog_addr, seq_pc;
  wire [15:0] prog_data;
  wire [7:0] sr_data, oe;
  host_port u_host (
      .clk(clk), .rst_n(rst_s),
      .rx_data(rx_data), .rx_valid(rx_valid), .rx_ferr(rx_ferr),
      .tx_data(tx_data), .tx_start(tx_start), .tx_busy(tx_busy),
      .run(run), .prog_we(prog_we), .prog_addr(prog_addr), .prog_data(prog_data),
      .sr_we(sr_we), .sr_data(sr_data), .seq_pc(seq_pc),
      .uio_oe(oe)
  );

  // The sequencer. Its pins come out of reset high (the idle level of UART
  // and I2C lines), but they only reach the pads once the host enables them.
  wire [7:0] pins;
  wire issuing;
  seq #(
      .ADDR_W(5),
      .PINS_RESET(8'hFF)
  ) u_seq (
      .clk(clk), .rst_n(rst_s), .run(run),
      .prog_we(prog_we), .prog_addr(prog_addr), .prog_data(prog_data),
      .sr_we(sr_we), .sr_data(sr_data),
      .pins(pins), .pc_out(seq_pc), .issuing(issuing)
  );

  assign uo_out  = {3'b000, tx, 3'b000, run};
  assign uio_out = pins;
  assign uio_oe  = oe;

  // Inputs not used yet (the referee will read them).
  wire _unused = &{ena, ui_in[7:4], ui_in[2:0], uio_in, issuing, 1'b0};

endmodule

`default_nettype wire
