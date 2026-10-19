// Unit-test wrapper: makes the 40 MHz clock in Verilog (much faster than a
// cocotb-driven clock) and exposes the ports as top-level signals.
`default_nettype none
`timescale 1ns / 1ps

module uart_tx_tb;
  reg clk = 1'b0;
  always #12.5 clk = ~clk;

  reg        rst_n = 1'b0;
  reg [15:0] div = 16'd16;
  reg  [7:0] wr_data = 8'd0;
  reg        wr_en = 1'b0;
  wire       hold_empty;
  wire       idle;
  wire       quiet;
  wire       tx;

  uart_tx dut (
      .clk(clk), .rst_n(rst_n), .div(div), .wr_data(wr_data), .wr_en(wr_en),
      .hold_empty(hold_empty), .idle(idle), .quiet(quiet), .tx(tx)
  );
endmodule
