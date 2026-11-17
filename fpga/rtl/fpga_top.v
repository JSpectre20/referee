/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * fpga_top: the Referee chip (tt_um_shreyansh_referee, src/, unchanged) in the PL
 * of a Zynq-7020, judging a real I2C bus driven by the Zynq's own PS I2C
 * controllers (Cadence IP), on physical Pmod pins.
 *
 *   ZedBoard (xc7z020clg484-1)  : fpga/zedboard.xdc   N_LED 8, N_SW 8, N_BTN 5
 *   PYNQ-Z2  (xc7z020clg400-1)  : fpga/pynq-z2.xdc    N_LED 6, N_SW 2, N_BTN 4
 *   (fpga/build.tcl sets the generics per board.)
 *
 * This module is the Vivado top. It instantiates the block design `zynq_ps`
 * (made by fpga/build.tcl: PS7 + Clocking Wizard), whose ports are:
 *   clk60        60.000 MHz from the MMCM (FCLK_CLK0 100 MHz in)
 *   mmcm_locked  MMCM locked
 *   ps_rst_n     FCLK_RESET0_N (low until the PS has been initialised)
 *   IIC_0_*      PS I2C0 on EMIO: the controller (master) that makes traffic
 *   IIC_1_*      PS I2C1 on EMIO: the target (slave, address 0x50)
 *   UART_EMIO_*  the PS UART that is NOT on the USB-UART (ZedBoard: UART0,
 *                PYNQ-Z2: UART1), routed through EMIO to the chip's host port
 *   GPIO_0_*     20 EMIO GPIO bits (switches, buttons in; status LEDs out)
 *   DDR_*, FIXED_IO_*  the PS's dedicated pins, passed straight through
 * Why the block design is a submodule and not the other way round (the wrapper
 * as a module reference inside the block design): the same file is the top in
 * Vivado, in the lint, in the Icarus simulation (fpga/sim/zynq_ps_model.v plays
 * the block design) and in the Yosys estimate, and it avoids module-reference
 * quirks (interface / clock / reset inference on port names, FREQ_HZ checks on
 * the 60 MHz pin, inout ports crossing the block-design boundary).
 *
 * The I2C bus
 *   SCL and SDA are each ONE open-drain net on one Pmod pin. Either PS
 *   controller pulling a line low pulls the pad low (IOBUF with I = 0 and
 *   T = "nobody pulls"). The pad has the FPGA's weak PULLUP (XDC); external
 *   2.2-4.7 kohm pull-ups to 3.3 V give realistic edges. Both PS controllers
 *   AND the chip's judge channels read the PAD input (IOBUF O), so the referee
 *   sees the real pin, edges included.
 *   The judge channels are registered once in the IOB (IOB = "TRUE") before
 *   the chip: SDA and SCL are then sampled with matched input delays (a fabric
 *   path could add a few ns of unmatched skew, and the chip's error budget
 *   assumes no skew between judged pins). The extra cycle delays both channels
 *   equally, so every interval the chip measures is unchanged.
 *
 * Chip pins here
 *   ui_in[3]  <- PS UART (EMIO) TX     uo_out[4] -> PS UART (EMIO) RX
 *   ui_in[4]  <- SDA pad (judge channel 0)
 *   ui_in[5]  <- SCL pad (judge channel 1)
 *   ui_in[0..2,6,7] <- 0 (engine inputs 8..10, judge channels 2..3: unused)
 *   uio[0..7] <-> second Pmod, IOBUF per pin, driven when uio_oe[k] = 1
 *   uo_out[0] -> LED 7 (ZedBoard); other uo_out bits are not brought out
 *
 * Reset: the chip's rst_n = !(PS reset) && MMCM locked && !(button 0), held
 * low for 8 more cycles of clk60 after all three are fine (asynchronous
 * assert, synchronous release). The chip then synchronises it again itself.
 *
 * LEDs (led[k] for k < N_LED)
 *   0 heartbeat, about 1 Hz, from clk60 (off while the chip is in reset)
 *   1 traffic on               (PS GPIO, set by fpga/sw/main.c)
 *   2 I2C activity: SCL pad was low in the last ~17 ms
 *   3 chip UART TX activity in the last ~17 ms (the chip is answering)
 *   4 error: an I2C transfer failed or read back wrong (PS GPIO, sticky)
 *   5 ok: the last write / read-back cycle matched     (PS GPIO)
 *   6 400 kHz selected                                  (PS GPIO)
 *   7 uo_out[0] (engine pin 8)
 *   PYNQ-Z2 has 6: LD0..LD3, then LD4 red (error), LD4 green (ok).
 *
 * EMIO GPIO map (PS pin number = 54 + bit)
 *   bits 0..7   in : switches (sw[0..N_SW-1], the rest 0)
 *   bits 8..12  in : buttons  (btn[0..N_BTN-1], the rest 0)
 *   bits 16..19 out: LED requests: 16 traffic on, 17 400 kHz, 18 ok, 19 error
 */

`default_nettype none

module fpga_top #(
    parameter integer N_LED         = 8,    // ZedBoard 8, PYNQ-Z2 6
    parameter integer N_SW          = 8,    // ZedBoard 8, PYNQ-Z2 2
    parameter integer N_BTN         = 5,    // ZedBoard 5, PYNQ-Z2 4
    // The chip's power-up bit period: round(60 MHz / 115200) = 521. If the
    // design is ever built for another clock, this must be round(clock / 115200).
    parameter integer CLKS_PER_BIT  = 521,
    parameter integer HEARTBEAT_BIT = 25,   // 2^25 / 60 MHz = 0.56 s per half period
    parameter integer ACT_BITS      = 20    // 2^20 / 60 MHz = 17 ms LED stretch
) (
    // ---- PS7 dedicated pins (no XDC needed: the PS7 IP places them) ----
    inout  wire [14:0] DDR_addr,
    inout  wire [ 2:0] DDR_ba,
    inout  wire        DDR_cas_n,
    inout  wire        DDR_ck_n,
    inout  wire        DDR_ck_p,
    inout  wire        DDR_cke,
    inout  wire        DDR_cs_n,
    inout  wire [ 3:0] DDR_dm,
    inout  wire [31:0] DDR_dq,
    inout  wire [ 3:0] DDR_dqs_n,
    inout  wire [ 3:0] DDR_dqs_p,
    inout  wire        DDR_odt,
    inout  wire        DDR_ras_n,
    inout  wire        DDR_reset_n,
    inout  wire        DDR_we_n,
    inout  wire        FIXED_IO_ddr_vrn,
    inout  wire        FIXED_IO_ddr_vrp,
    inout  wire [53:0] FIXED_IO_mio,
    inout  wire        FIXED_IO_ps_clk,
    inout  wire        FIXED_IO_ps_porb,
    inout  wire        FIXED_IO_ps_srstb,

    // ---- board pins (fpga/<board>.xdc) ----
    inout  wire             i2c_scl,  // Pmod JA pin 3, open drain, PULLUP
    inout  wire             i2c_sda,  // Pmod JA pin 4, open drain, PULLUP
    inout  wire [      7:0] uio,      // Pmod JB pins 1-4, 7-10 = uio[0..7]
    input  wire [ N_SW-1:0] sw,
    input  wire [N_BTN-1:0] btn,      // btn[0] resets the chip (active high)
    output wire [N_LED-1:0] led
);

  // ===========================================================================
  // Block design: PS7 + Clocking Wizard
  // ===========================================================================
  wire        clk60, mmcm_locked, ps_rst_n;
  wire        i2c0_scl_i, i2c0_scl_o, i2c0_scl_t, i2c0_sda_i, i2c0_sda_o, i2c0_sda_t;
  wire        i2c1_scl_i, i2c1_scl_o, i2c1_scl_t, i2c1_sda_i, i2c1_sda_o, i2c1_sda_t;
  wire        ps_uart_txd;  // PS UART (EMIO) -> chip RX
  wire        ps_uart_rxd;  // chip TX -> PS UART (EMIO)
  wire [19:0] gpio_i, gpio_o, gpio_t;

  zynq_ps u_ps (
      .DDR_addr(DDR_addr), .DDR_ba(DDR_ba), .DDR_cas_n(DDR_cas_n), .DDR_ck_n(DDR_ck_n),
      .DDR_ck_p(DDR_ck_p), .DDR_cke(DDR_cke), .DDR_cs_n(DDR_cs_n), .DDR_dm(DDR_dm),
      .DDR_dq(DDR_dq), .DDR_dqs_n(DDR_dqs_n), .DDR_dqs_p(DDR_dqs_p), .DDR_odt(DDR_odt),
      .DDR_ras_n(DDR_ras_n), .DDR_reset_n(DDR_reset_n), .DDR_we_n(DDR_we_n),
      .FIXED_IO_ddr_vrn(FIXED_IO_ddr_vrn), .FIXED_IO_ddr_vrp(FIXED_IO_ddr_vrp),
      .FIXED_IO_mio(FIXED_IO_mio), .FIXED_IO_ps_clk(FIXED_IO_ps_clk),
      .FIXED_IO_ps_porb(FIXED_IO_ps_porb), .FIXED_IO_ps_srstb(FIXED_IO_ps_srstb),
      .clk60(clk60), .mmcm_locked(mmcm_locked), .ps_rst_n(ps_rst_n),
      .IIC_0_scl_i(i2c0_scl_i), .IIC_0_scl_o(i2c0_scl_o), .IIC_0_scl_t(i2c0_scl_t),
      .IIC_0_sda_i(i2c0_sda_i), .IIC_0_sda_o(i2c0_sda_o), .IIC_0_sda_t(i2c0_sda_t),
      .IIC_1_scl_i(i2c1_scl_i), .IIC_1_scl_o(i2c1_scl_o), .IIC_1_scl_t(i2c1_scl_t),
      .IIC_1_sda_i(i2c1_sda_i), .IIC_1_sda_o(i2c1_sda_o), .IIC_1_sda_t(i2c1_sda_t),
      .UART_EMIO_txd(ps_uart_txd), .UART_EMIO_rxd(ps_uart_rxd),
      .GPIO_0_tri_i(gpio_i), .GPIO_0_tri_o(gpio_o), .GPIO_0_tri_t(gpio_t)
  );

  // ===========================================================================
  // The I2C bus: one open-drain net per line, on a Pmod pin
  // ===========================================================================
  // A PS I2C controller on EMIO pulls a line low when its tristate control _t
  // is 0 (output enabled) and its output _o is 0. Anything else (released, or
  // the unusual "drive high") leaves the line to the pull-up: open drain.
  wire scl_pull = (~i2c0_scl_t & ~i2c0_scl_o) | (~i2c1_scl_t & ~i2c1_scl_o);
  wire sda_pull = (~i2c0_sda_t & ~i2c0_sda_o) | (~i2c1_sda_t & ~i2c1_sda_o);
  wire scl_pad, sda_pad;  // what the pads read: the real bus level

  // T = 1: high impedance (released). T = 0: drive I = 0 (pull low).
  IOBUF u_scl_iobuf (.I(1'b0), .T(~scl_pull), .O(scl_pad), .IO(i2c_scl));
  IOBUF u_sda_iobuf (.I(1'b0), .T(~sda_pull), .O(sda_pad), .IO(i2c_sda));

  // Both controllers read the pad, so each sees the other (ACKs, clock
  // stretching, arbitration) exactly as on a shared external bus.
  assign i2c0_scl_i = scl_pad;
  assign i2c1_scl_i = scl_pad;
  assign i2c0_sda_i = sda_pad;
  assign i2c1_sda_i = sda_pad;

  // Judge channels: one IOB register per line (matched sampling, see header).
  (* IOB = "TRUE" *) reg sda_q;
  (* IOB = "TRUE" *) reg scl_q;
  always @(posedge clk60) begin
    sda_q <= sda_pad;
    scl_q <= scl_pad;
  end

  // ===========================================================================
  // Chip reset: async assert, release 8 cycles after everything is fine
  // ===========================================================================
  // Registers in this wrapper carry FPGA power-up values (= 0, the default
  // INIT of a 7-series flop). That is an FPGA feature, allowed here and not
  // in the chip's RTL; it also keeps the simulation free of X.
  /* verilator lint_off PROCASSINIT */
  wire       rst_req_n = ps_rst_n & mmcm_locked & ~btn[0];
  reg  [3:0] rst_cnt = 4'd0;
  always @(posedge clk60 or negedge rst_req_n) begin
    if (!rst_req_n) rst_cnt <= 4'd0;
    else if (!rst_cnt[3]) rst_cnt <= rst_cnt + 4'd1;
  end
  wire chip_rst_n = rst_cnt[3];

  // ===========================================================================
  // The chip, unchanged
  // ===========================================================================
  wire [7:0] uio_in, uio_out, uio_oe, uo_out;
  wire [7:0] ui_in = {2'b00,      // ui_in[7:6] judge channels 2, 3: unused
                      scl_q,      // ui_in[5]   judge channel 1 = SCL
                      sda_q,      // ui_in[4]   judge channel 0 = SDA
                      ps_uart_txd,// ui_in[3]   host UART RX
                      3'b000};    // ui_in[2:0] engine inputs 8..10: unused

  tt_um_shreyansh_referee #(
      .CLKS_PER_BIT(CLKS_PER_BIT)
  ) u_chip (
      .ui_in  (ui_in),
      .uo_out (uo_out),
      .uio_in (uio_in),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (1'b1),
      .clk    (clk60),
      .rst_n  (chip_rst_n)
  );

  assign ps_uart_rxd = uo_out[4];  // chip UART TX -> PS UART RX

  // uio[0..7] on the second Pmod: driven only where the chip enables them.
  // (Open-drain engine pins come out as uio_oe = 1 only while driving low.)
  genvar k;
  generate
    for (k = 0; k < 8; k = k + 1) begin : g_uio
      IOBUF u_uio_iobuf (.I(uio_out[k]), .T(~uio_oe[k]), .O(uio_in[k]), .IO(uio[k]));
    end
  endgenerate

  // ===========================================================================
  // EMIO GPIO: switches and buttons in, LED requests out
  // ===========================================================================
  wire [7:0] sw8;
  wire [4:0] btn5;
  generate
    for (k = 0; k < 8; k = k + 1) begin : g_sw
      if (k < N_SW) begin : g_on
        assign sw8[k] = sw[k];
      end else begin : g_off
        assign sw8[k] = 1'b0;
      end
    end
    for (k = 0; k < 5; k = k + 1) begin : g_btn
      if (k < N_BTN) begin : g_on
        assign btn5[k] = btn[k];
      end else begin : g_off
        assign btn5[k] = 1'b0;
      end
    end
  endgenerate
  assign gpio_i = {4'b0000, 3'b000, btn5, sw8};

  // ===========================================================================
  // Status LEDs
  // ===========================================================================
  reg [HEARTBEAT_BIT:0] hb = {(HEARTBEAT_BIT + 1) {1'b0}};
  reg [ ACT_BITS-1:0] scl_act = {ACT_BITS{1'b0}};
  reg [ ACT_BITS-1:0] tx_act = {ACT_BITS{1'b0}};
  always @(posedge clk60) begin
    hb      <= hb + 1'b1;
    scl_act <= !scl_q ? {ACT_BITS{1'b1}} : scl_act - (|scl_act);
    tx_act  <= !uo_out[4] ? {ACT_BITS{1'b1}} : tx_act - (|tx_act);
  end
  /* verilator lint_on PROCASSINIT */

  wire [7:0] status = {uo_out[0],               // 7 engine pin 8
                       gpio_o[17],              // 6 400 kHz
                       gpio_o[18],              // 5 ok
                       gpio_o[19],              // 4 error
                       |tx_act,                 // 3 chip answering
                       |scl_act,                // 2 I2C activity
                       gpio_o[16],              // 1 traffic on
                       hb[HEARTBEAT_BIT] & chip_rst_n};  // 0 heartbeat
  assign led = status[N_LED-1:0];

  // Unused on purpose: the PS GPIO output enables (LEDs follow gpio_o), the
  // engine outputs not brought out, and the top bits of status on PYNQ-Z2.
  wire _unused = &{1'b0, gpio_t, gpio_o[15:0], uo_out[7:5], uo_out[3:1], status};

endmodule

`default_nettype wire
