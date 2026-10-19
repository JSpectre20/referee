/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * Register map reached from the console (8-bit address, 8-bit data).
 *
 *   00  ID        RO   0x52 ('R')
 *   01  VERSION   RO   0x01
 *   02  SCRATCH   RW   free for testing
 *   03  STATUS    W1C  bit0 overrun (byte dropped), bit1 UART framing error
 *   04  DIV_LO    RW   UART bit period in clock cycles, low byte
 *   05  DIV_HI    RW   high byte; writing it applies the new divisor once
 *                      the "ok" reply has finished sending
 *   10  GPIO_OUT  RW   drives uio_out
 *   11  GPIO_OE   RW   uio_oe (1 = output)
 *   12  GPIO_IN   RO   uio_in
 *   13  UI_IN     RO   ui_in (bit 3 is the UART RX line)
 *   14  UO_OUT    RW   drives uo_out, except bit 4 which is UART TX
 *
 * Unmapped addresses read as 0x00; writes to them are ignored.
 */

`default_nettype none

module regs #(
    parameter [15:0] DEFAULT_DIV = 16'd347
) (
    input  wire        clk,
    input  wire        rst_n,
    // Bus from the console
    input  wire [ 7:0] addr,
    input  wire [ 7:0] wdata,
    input  wire        we,
    output reg  [ 7:0] rdata,
    // Pins (already synchronized)
    input  wire [ 7:0] uio_in_s,
    input  wire [ 7:0] ui_in_s,
    output reg  [ 7:0] gpio_out,
    output reg  [ 7:0] gpio_oe,
    output reg  [ 7:0] uo_reg,
    // Status events
    input  wire        set_overrun,
    input  wire        set_frame_err,
    // UART divisor
    input  wire        apply_ok,  // console idle and TX in its last stop bit (or idle)
    output reg  [15:0] div
);

  localparam [7:0] A_ID = 8'h00, A_VERSION = 8'h01, A_SCRATCH = 8'h02, A_STATUS = 8'h03;
  localparam [7:0] A_DIV_LO = 8'h04, A_DIV_HI = 8'h05;
  localparam [7:0] A_GPIO_OUT = 8'h10, A_GPIO_OE = 8'h11, A_GPIO_IN = 8'h12;
  localparam [7:0] A_UI_IN = 8'h13, A_UO_OUT = 8'h14;

  localparam [15:0] MIN_DIV = 16'd4;

  reg [ 7:0] scratch;
  reg [ 1:0] status;
  reg [15:0] div_shadow;
  reg        div_pending;

  always @(*) begin
    case (addr)
      A_ID:       rdata = 8'h52;
      A_VERSION:  rdata = 8'h01;
      A_SCRATCH:  rdata = scratch;
      A_STATUS:   rdata = {6'd0, status};
      A_DIV_LO:   rdata = div_shadow[7:0];
      A_DIV_HI:   rdata = div_shadow[15:8];
      A_GPIO_OUT: rdata = gpio_out;
      A_GPIO_OE:  rdata = gpio_oe;
      A_GPIO_IN:  rdata = uio_in_s;
      A_UI_IN:    rdata = ui_in_s;
      A_UO_OUT:   rdata = uo_reg;
      default:    rdata = 8'h00;
    endcase
  end

  wire [1:0] status_clr = (we && addr == A_STATUS) ? wdata[1:0] : 2'b00;

  always @(posedge clk) begin
    if (!rst_n) begin
      scratch     <= 8'd0;
      status      <= 2'd0;
      div_shadow  <= DEFAULT_DIV;
      div_pending <= 1'b0;
      div         <= DEFAULT_DIV;
      gpio_out    <= 8'd0;
      gpio_oe     <= 8'd0;
      uo_reg      <= 8'd0;
    end else begin
      if (we) begin
        case (addr)
          A_SCRATCH:  scratch <= wdata;
          A_DIV_LO:   div_shadow[7:0] <= wdata;
          A_DIV_HI: begin
            div_shadow[15:8] <= wdata;
            div_pending      <= 1'b1;
          end
          A_GPIO_OUT: gpio_out <= wdata;
          A_GPIO_OE:  gpio_oe <= wdata;
          A_UO_OUT:   uo_reg <= wdata;
          default:    ;
        endcase
      end

      // A new event wins over a clear in the same cycle.
      status <= (status & ~status_clr) | {set_frame_err, set_overrun};

      if (div_pending && apply_ok && !we) begin
        div         <= (div_shadow < MIN_DIV) ? MIN_DIV : div_shadow;
        div_pending <= 1'b0;
      end
    end
  end

endmodule
