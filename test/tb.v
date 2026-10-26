`default_nettype none
`timescale 1ns / 1ps

/* Chip-level testbench, used for both RTL and gate-level simulation.

   - The 40 MHz clock is generated here rather than from cocotb: with Icarus a
     cocotb-driven clock is many times slower, and these tests run real
     115200-baud UART traffic.
   - `host_rx` drives ui_in[3] (the chip's UART RX); `host_tx` is uo_out[4]
     (the chip's UART TX). The other ui_in bits come from `ui_in_pins`.
*/
module tb ();

  // Dump the signals to a FST file. You can view it with gtkwave or surfer.
  initial begin
    $dumpfile("tb.fst");
    $dumpvars(0, tb);
    #1;
  end

  reg clk = 1'b0;
  always #12.5 clk = ~clk;

  reg rst_n;
  reg ena;
  reg [7:0] ui_in_pins;
  reg host_rx;
  reg [7:0] uio_in;
  wire [7:0] ui_in = {ui_in_pins[7:4], host_rx, ui_in_pins[2:0]};
  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;
  wire host_tx = uo_out[4];
  wire seq_pin0 = uio_out[0];

  tt_um_shreyansh_referee user_project (
      .ui_in  (ui_in),    // Dedicated inputs
      .uo_out (uo_out),   // Dedicated outputs
      .uio_in (uio_in),   // IOs: Input path
      .uio_out(uio_out),  // IOs: Output path
      .uio_oe (uio_oe),   // IOs: Enable path (active high: 0=input, 1=output)
      .ena    (ena),      // enable - goes high when design is selected
      .clk    (clk),      // clock
      .rst_n  (rst_n)     // not reset
  );

endmodule
