// Test wrapper for src/uart_tx.v and src/uart_rx.v.
//   - u_tx: transmitter driven by cocotb, output `tx`
//   - u_rx: receiver on `rx_in`, driven by cocotb (cocotbext-uart or by hand)
//   - u_lb: a second receiver looped back from `tx`
// The 40 MHz clock is made here (much faster than a cocotb clock in Icarus).
`default_nettype none
`timescale 1ns / 1ps

module uart_tb #(
    parameter integer CLKS = 16
);
  reg clk = 1'b0;
  always #12.5 clk = ~clk;

  reg        rst_n = 1'b0;
  reg  [7:0] tx_data = 8'd0;
  reg        tx_start = 1'b0;
  wire       tx, tx_busy;
  reg        rx_in = 1'b1;
  wire [7:0] rx_data, lb_data;
  wire       rx_valid, rx_ferr, lb_valid, lb_ferr;

  localparam [15:0] DIV = CLKS;  // the runtime bit period, fixed here
  uart_tx #(.CLKS_PER_BIT(CLKS)) u_tx (
      .clk(clk), .rst_n(rst_n), .div(DIV), .data(tx_data), .start(tx_start), .tx(tx), .busy(tx_busy)
  );
  uart_rx u_rx (
      .clk(clk), .rst_n(rst_n), .div(DIV), .rx(rx_in), .data(rx_data), .valid(rx_valid), .frame_err(rx_ferr)
  );
  uart_rx u_lb (
      .clk(clk), .rst_n(rst_n), .div(DIV), .rx(tx), .data(lb_data), .valid(lb_valid), .frame_err(lb_ferr)
  );
endmodule
