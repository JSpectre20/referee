// SPDX-License-Identifier: Apache-2.0
// Lint-only stand-in for the block design zynq_ps (fpga/build.tcl): the same
// ports fpga_top connects, nothing behind them. Vivado generates the real one.
`default_nettype none
/* verilator lint_off DECLFILENAME */
module zynq_ps (
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
    output wire        clk60,
    output wire        mmcm_locked,
    output wire        ps_rst_n,
    input  wire        IIC_0_scl_i,
    output wire        IIC_0_scl_o,
    output wire        IIC_0_scl_t,
    input  wire        IIC_0_sda_i,
    output wire        IIC_0_sda_o,
    output wire        IIC_0_sda_t,
    input  wire        IIC_1_scl_i,
    output wire        IIC_1_scl_o,
    output wire        IIC_1_scl_t,
    input  wire        IIC_1_sda_i,
    output wire        IIC_1_sda_o,
    output wire        IIC_1_sda_t,
    output wire        UART_EMIO_txd,
    input  wire        UART_EMIO_rxd,
    input  wire [19:0] GPIO_0_tri_i,
    output wire [19:0] GPIO_0_tri_o,
    output wire [19:0] GPIO_0_tri_t
);
  // Outputs follow inputs, so the lint sees every net used and nothing constant.
  wire toggle = FIXED_IO_ps_clk;
  assign clk60 = toggle;
  assign mmcm_locked = IIC_0_sda_i;
  assign ps_rst_n = IIC_1_scl_i;
  assign {IIC_0_scl_o, IIC_0_scl_t, IIC_0_sda_o, IIC_0_sda_t} = {4{IIC_1_sda_i}};
  assign {IIC_1_scl_o, IIC_1_scl_t, IIC_1_sda_o, IIC_1_sda_t} = {4{UART_EMIO_rxd ^ IIC_0_scl_i}};
  assign UART_EMIO_txd = FIXED_IO_ps_porb;  // not the clock net: fpga_top samples it as data (ILA)
  assign GPIO_0_tri_o = GPIO_0_tri_i;
  assign GPIO_0_tri_t = ~GPIO_0_tri_i;
endmodule
`default_nettype wire
