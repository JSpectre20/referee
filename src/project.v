/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * Referee: programmable protocol emulator (work in progress).
 *
 * Stage 1: a UART text console that reads and writes a small register map,
 * including the uio pins. Later stages hang the CPU's debug controls off the
 * same register bus.
 *
 * Pins:
 *   ui_in[3]   UART RX (demo board USB bridge)
 *   uo_out[4]  UART TX (demo board USB bridge)
 *   uio[7:0]   general-purpose pins, set from the console
 */

`default_nettype none

module tt_um_shreyansh_referee #(
    parameter CLK_HZ = 40_000_000,
    parameter BAUD   = 115_200
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

  localparam integer DIV_ROUNDED = (CLK_HZ + BAUD / 2) / BAUD;
  localparam [15:0] DEFAULT_DIV = DIV_ROUNDED[15:0];

  // Reset: assert immediately, release in step with the clock.
  reg [1:0] rst_pipe;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) rst_pipe <= 2'b00;
    else rst_pipe <= {rst_pipe[0], 1'b1};
  end
  wire rst_ni = rst_pipe[1];

  // Synchronize the pins that the console can read.
  reg [7:0] ui_m, ui_s, uio_m, uio_s;
  always @(posedge clk) begin
    ui_m  <= ui_in;
    ui_s  <= ui_m;
    uio_m <= uio_in;
    uio_s <= uio_m;
  end

  wire [15:0] div;

  wire [ 7:0] rx_data;
  wire        rx_valid;
  wire        rx_frame_err;

  uart_rx u_rx (
      .clk      (clk),
      .rst_n    (rst_ni),
      .div      (div),
      .rx       (ui_in[3]),
      .data     (rx_data),
      .valid    (rx_valid),
      .frame_err(rx_frame_err)
  );

  wire [7:0] tx_data;
  wire       tx_wr;
  wire       tx_hold_empty;
  wire       tx_idle;  // unused for now
  wire       tx_quiet;
  wire       tx;

  uart_tx u_tx (
      .clk       (clk),
      .rst_n     (rst_ni),
      .div       (div),
      .wr_data   (tx_data),
      .wr_en     (tx_wr),
      .hold_empty(tx_hold_empty),
      .idle      (tx_idle),
      .quiet     (tx_quiet),
      .tx        (tx)
  );

  wire [7:0] bus_addr;
  wire [7:0] bus_wdata;
  wire       bus_we;
  wire [7:0] bus_rdata;
  wire       overrun;
  wire       console_idle;

  console u_console (
      .clk          (clk),
      .rst_n        (rst_ni),
      .rx_data      (rx_data),
      .rx_valid     (rx_valid),
      .tx_data      (tx_data),
      .tx_wr        (tx_wr),
      .tx_hold_empty(tx_hold_empty),
      .bus_addr     (bus_addr),
      .bus_wdata    (bus_wdata),
      .bus_we       (bus_we),
      .bus_rdata    (bus_rdata),
      .overrun      (overrun),
      .idle         (console_idle)
  );

  wire [7:0] gpio_out;
  wire [7:0] gpio_oe;
  wire [7:0] uo_reg;

  regs #(
      .DEFAULT_DIV(DEFAULT_DIV)
  ) u_regs (
      .clk          (clk),
      .rst_n        (rst_ni),
      .addr         (bus_addr),
      .wdata        (bus_wdata),
      .we           (bus_we),
      .rdata        (bus_rdata),
      .uio_in_s     (uio_s),
      .ui_in_s      (ui_s),
      .gpio_out     (gpio_out),
      .gpio_oe      (gpio_oe),
      .uo_reg       (uo_reg),
      .set_overrun  (overrun),
      .set_frame_err(rx_frame_err),
      // Switch baud rate once the reply's last stop bit has started: a fast
      // host may answer at the new rate as soon as it samples that stop bit.
      .apply_ok     (console_idle && tx_quiet),
      .div          (div)
  );

  assign uo_out  = {uo_reg[7:5], tx, uo_reg[3:0]};
  assign uio_out = gpio_out;
  assign uio_oe  = gpio_oe;

  wire _unused = &{ena, uo_reg[4], tx_idle, 1'b0};

endmodule
