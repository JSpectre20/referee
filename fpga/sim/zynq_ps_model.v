// SPDX-License-Identifier: Apache-2.0
//
// Simulation stand-in for the block design zynq_ps (PS7 + Clocking Wizard),
// with the same ports fpga_top uses. Behavioural, Icarus (-g2012) only.
//
//   clk60        60 MHz (16.667 ns as 8.333 + 8.334 ns, like test/tb.v)
//   mmcm_locked  high after 2 us;  ps_rst_n high after 1 us
//   IIC_0_*      an I2C controller on EMIO pins: drives only _t (with _o = 0),
//                reads _i; honours clock stretching. Tasks m_* below.
//   IIC_1_*      an I2C target at 0x50 on EMIO pins: 256-byte register file
//                (24C02-like), ACKs, stretches SCL once after its first ACK.
//   UART_EMIO_*  115200 8N1: task uart_send, received bytes in rx_buf
//   GPIO_0_*     tri_o drives the LED requests from `gpio_out`
//
// These are models of what the Cadence controllers do on their EMIO pins, not
// of the Cadence IP itself; their job is to exercise fpga_top's open-drain nets
// and the chip's judge channels the way real controllers would.
`timescale 1ns / 1ps
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
    output reg         clk60,
    output reg         mmcm_locked,
    output reg         ps_rst_n,
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
    output reg         UART_EMIO_txd,
    input  wire        UART_EMIO_rxd,
    input  wire [19:0] GPIO_0_tri_i,
    output wire [19:0] GPIO_0_tri_o,
    output wire [19:0] GPIO_0_tri_t
);
  // ---------------------------------------------------------------- clock, resets
  initial begin
    clk60 = 1'b0;
    mmcm_locked = 1'b0;
    ps_rst_n = 1'b0;
    UART_EMIO_txd = 1'b1;
    #1000 ps_rst_n = 1'b1;
    #1000 mmcm_locked = 1'b1;
  end
  always begin
    #8.333 clk60 = 1'b1;
    #8.334 clk60 = 1'b0;
  end

  reg [19:0] gpio_out = 20'd0;
  assign GPIO_0_tri_o = gpio_out;
  assign GPIO_0_tri_t = ~20'd0 ^ (20'hF << 16);  // bits 16..19 are outputs

  // ---------------------------------------------------------------- UART 115200
  localparam real BIT_NS = 1.0e9 / 115200.0;
  reg [7:0] rx_buf[0:1023];
  integer rx_n = 0;
  task uart_send(input [7:0] b);
    integer i;
    begin
      UART_EMIO_txd = 1'b0;
      #(BIT_NS);
      for (i = 0; i < 8; i = i + 1) begin
        UART_EMIO_txd = b[i];
        #(BIT_NS);
      end
      UART_EMIO_txd = 1'b1;
      #(BIT_NS);
    end
  endtask
  // Receiver: sample in the middle of each bit.
  reg [7:0] rx_byte;
  integer k;
  always @(negedge UART_EMIO_rxd) begin
    #(BIT_NS / 2);
    if (UART_EMIO_rxd == 1'b0) begin
      for (k = 0; k < 8; k = k + 1) begin
        #(BIT_NS);
        rx_byte[k] = UART_EMIO_rxd;
      end
      #(BIT_NS);  // stop bit
      rx_buf[rx_n % 1024] = rx_byte;
      rx_n = rx_n + 1;
    end
  end

  // ---------------------------------------------------------------- I2C controller (I2C0)
  reg m_scl_low = 1'b0, m_sda_low = 1'b0;
  assign IIC_0_scl_o = 1'b0;
  assign IIC_0_sda_o = 1'b0;
  assign IIC_0_scl_t = ~m_scl_low;   // T = 0: pull low
  assign IIC_0_sda_t = ~m_sda_low;
  real t_low = 1300.0, t_high = 1200.0, t_su = 600.0;  // 400 kHz-ish, set by the testbench

  task m_start;  // bus idle (both high) -> START, SCL low
    begin
      m_sda_low = 1'b1;
      #(t_high);
      m_scl_low = 1'b1;
    end
  endtask
  task m_clock;  // release SCL, wait (stretching), high time, pull low
    begin
      m_scl_low = 1'b0;
      wait (IIC_0_scl_i === 1'b1);
      #(t_high);
      m_scl_low = 1'b1;
    end
  endtask
  task m_bit(input b, input real su);  // SCL low on entry and exit
    begin
      #(t_low - su);
      m_sda_low = ~b;
      #(su);
      m_clock;
    end
  endtask
  task m_rbit(output b);  // release SDA, clock, sample in the high phase
    begin
      m_sda_low = 1'b0;
      #(t_low);
      m_scl_low = 1'b0;
      wait (IIC_0_scl_i === 1'b1);
      #(t_high / 2);
      b = IIC_0_sda_i;
      #(t_high / 2);
      m_scl_low = 1'b1;
    end
  endtask
  task m_write_byte(input [7:0] v, input real su, output nack);
    integer i;
    begin
      for (i = 7; i >= 0; i = i - 1) m_bit(v[i], su);
      m_rbit(nack);
    end
  endtask
  task m_read_byte(output [7:0] v, input last);
    integer i;
    reg b;
    begin
      for (i = 7; i >= 0; i = i - 1) begin
        m_rbit(b);
        v[i] = b;
      end
      m_bit(last, t_su);  // NACK (1) on the last byte, else ACK (0)
    end
  endtask
  task m_rstart;  // SCL low -> repeated START, SCL low
    begin
      #(t_low / 2);
      m_sda_low = 1'b0;
      #(t_low / 2);
      m_scl_low = 1'b0;
      wait (IIC_0_scl_i === 1'b1);
      #(t_high);
      m_sda_low = 1'b1;
      #(t_high);
      m_scl_low = 1'b1;
    end
  endtask
  task m_stop;  // SCL low -> STOP, bus idle
    begin
      #(t_low / 2);
      m_sda_low = 1'b1;
      #(t_low / 2);
      m_scl_low = 1'b0;
      wait (IIC_0_scl_i === 1'b1);
      #(t_high);
      m_sda_low = 1'b0;
      #(t_high);
    end
  endtask

  // ---------------------------------------------------------------- I2C target (I2C1, 0x50)
  reg s_scl_low = 1'b0, s_sda_low = 1'b0;
  assign IIC_1_scl_o = 1'b0;
  assign IIC_1_sda_o = 1'b0;
  assign IIC_1_scl_t = ~s_scl_low;
  assign IIC_1_sda_t = ~s_sda_low;
  reg [7:0] s_mem[0:255];
  reg [7:0] s_ptr = 8'd0;
  reg       s_stretch_once = 1'b1;
  integer   s_stretches = 0, s_acks = 0;
  initial for (k = 0; k < 256; k = k + 1) s_mem[k] = k[7:0];

  event ev_start, ev_stop;
  always @(negedge IIC_1_sda_i) if (IIC_1_scl_i === 1'b1) ->ev_start;
  always @(posedge IIC_1_sda_i) if (IIC_1_scl_i === 1'b1) ->ev_stop;

  localparam real T_HD = 300.0;  // the target's data hold after SCL falls

  task s_ack;  // drive ACK for the next clock; stretch SCL the first time
    begin
      @(negedge IIC_1_scl_i);
      #(T_HD);
      s_sda_low = 1'b1;
      s_acks = s_acks + 1;
      if (s_stretch_once) begin
        s_scl_low = 1'b1;
        #5000;
        s_scl_low = 1'b0;
        s_stretch_once = 1'b0;
        s_stretches = s_stretches + 1;
      end
      @(negedge IIC_1_scl_i);
      #(T_HD);
    end
  endtask

  task automatic s_body;
    reg [7:0] b;
    reg nack;
    reg first;
    integer i;
    begin
      for (i = 7; i >= 0; i = i - 1) begin
        @(posedge IIC_1_scl_i);
        b[i] = IIC_1_sda_i;
      end
      if (b[7:1] == 7'h50) begin
        s_ack;
        if (b[0] == 1'b0) begin  // controller writes: pointer, then data
          s_sda_low = 1'b0;
          first = 1'b1;
          forever begin
            for (i = 7; i >= 0; i = i - 1) begin
              @(posedge IIC_1_scl_i);
              b[i] = IIC_1_sda_i;
            end
            if (first) s_ptr = b;
            else begin
              s_mem[s_ptr] = b;
              s_ptr = s_ptr + 8'd1;
            end
            first = 1'b0;
            s_ack;
            s_sda_low = 1'b0;
          end
        end else begin  // controller reads: send from the pointer until NACK
          forever begin
            b = s_mem[s_ptr];
            s_ptr = s_ptr + 8'd1;
            for (i = 7; i >= 0; i = i - 1) begin
              s_sda_low = ~b[i];
              @(negedge IIC_1_scl_i);
              #(T_HD);
            end
            s_sda_low = 1'b0;
            @(posedge IIC_1_scl_i);
            nack = IIC_1_sda_i;
            @(negedge IIC_1_scl_i);
            #(T_HD);
            if (nack) begin
              @(ev_stop);  // wait for the end; the fork below cuts this short
            end
          end
        end
      end else begin
        @(ev_stop);  // not us
      end
    end
  endtask

  reg s_pending = 1'b0;
  always begin
    if (!s_pending) @(ev_start);
    s_pending = 1'b0;
    fork : s_run
      s_body;
      @(ev_stop);
      begin
        @(ev_start);
        s_pending = 1'b1;
      end
    join_any
    disable s_run;
    s_sda_low = 1'b0;
    s_scl_low = 1'b0;
  end

  wire _unused = &{1'b0, GPIO_0_tri_i};
endmodule
`default_nettype wire
