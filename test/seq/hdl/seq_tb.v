// Test wrapper for src/seq.v: makes the 40 MHz clock in Verilog (much faster
// than a cocotb-driven clock in Icarus) and exposes pin 0 as `tx`.
`default_nettype none
`timescale 1ns / 1ps

module seq_tb;
  reg clk = 1'b0;
  always #12.5 clk = ~clk;

  reg         rst_n = 1'b0;
  reg         run = 1'b0;
  reg         prog_we = 1'b0;
  reg  [ 4:0] prog_addr = 5'd0;
  reg  [15:0] prog_data = 16'd0;
  reg         sr_we = 1'b0;
  reg  [ 7:0] sr_data = 8'd0;
  wire [ 7:0] pins;
  wire [ 4:0] pc_out;
  wire        issuing;
  wire        tx = pins[0];

  // Pin 0 comes out of reset high, like an idle UART line.
  seq #(
      .ADDR_W    (5),
      .PINS_RESET(8'h01)
  ) dut (
      .clk(clk), .rst_n(rst_n), .run(run),
      .prog_we(prog_we), .prog_addr(prog_addr), .prog_data(prog_data),
      .sr_we(sr_we), .sr_data(sr_data),
      .pins(pins), .pc_out(pc_out), .issuing(issuing)
  );
endmodule
