// Test wrapper for src/edge_ts.v: 40 MHz clock and the free-running `now`
// counter live in Verilog; cocotb drives the two pins at arbitrary times.
`default_nettype none
`timescale 1ns / 1ps

module edge_ts_tb;
  reg clk = 1'b0;
  always #12.5 clk = ~clk;

  reg         rst_n = 1'b0;
  reg  [ 1:0] pins = 2'b00;
  reg  [ 1:0] clear = 2'b00;
  reg  [15:0] now = 16'd0;
  always @(posedge clk) now <= now + 16'd1;

  wire [ 1:0] level, rise, fall, toggled;
  wire [31:0] rise_ts, fall_ts;

  edge_ts #(
      .N(2), .TS_W(16)
  ) dut (
      .clk(clk), .rst_n(rst_n), .pins(pins), .now(now), .clear(clear),
      .level(level), .rise(rise), .fall(fall),
      .rise_ts(rise_ts), .fall_ts(fall_ts), .toggled(toggled)
  );
endmodule
