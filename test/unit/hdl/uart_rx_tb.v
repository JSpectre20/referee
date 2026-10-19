// Unit-test wrapper: makes the 40 MHz clock in Verilog (much faster than a
// cocotb-driven clock) and exposes the ports as top-level signals.
`default_nettype none
`timescale 1ns / 1ps

module uart_rx_tb;
  reg clk = 1'b0;
  always #12.5 clk = ~clk;

  reg        rst_n = 1'b0;
  reg [15:0] div = 16'd16;
  reg        rx = 1'b1;
  wire [7:0] data;
  wire       valid;
  wire       frame_err;

  uart_rx dut (
      .clk(clk), .rst_n(rst_n), .div(div), .rx(rx),
      .data(data), .valid(valid), .frame_err(frame_err)
  );
endmodule
