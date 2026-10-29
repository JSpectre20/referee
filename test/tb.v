`default_nettype none
`timescale 1ns / 1ps

/* Chip-level testbench, used for both RTL and gate-level simulation.

   - The clock is generated here rather than from cocotb: with Icarus a
     cocotb-driven clock is many times slower, and these tests run real UART
     traffic. It must be the clock in info.yaml (60 MHz): images are
     assembled for that clock (ledger A9), and test/test.py checks the period.
   - `host_rx` drives ui_in[3] (the chip's UART RX); `host_tx` is uo_out[4]
     (the chip's UART TX). The other ui_in bits come from `ui_in_pins`
     (ui_in[4..7] are the judge channels).
   - uio_in[1] comes from `uio_rx1` (a UART the engine firmware receives);
     the other uio_in bits from `uio_in_pins`, except:
   - an I2C bus on uio[2] (SCL) and uio[3] (SDA): open-drain, pulled up here.
     Each line is the wired AND of the chip (low when it drives 0) and the
     cocotb target model (low while `i2c_scl_low` / `i2c_sda_low`), and the
     chip reads the bus back on uio_in[2] / uio_in[3];
   - SPI on uio[4] (SCK), uio[5] (MOSI), uio[6] (CS) from the chip, and
     uio_in[7] (MISO) from the cocotb target model (`spi_miso`).
*/
module tb ();

  // Dump the signals to a FST file. You can view it with gtkwave or surfer.
  initial begin
    $dumpfile("tb.fst");
    $dumpvars(0, tb);
    #1;
  end

  reg clk = 1'b0;
  // 60 MHz: 16.667 ns, as 8.333 + 8.334 ns (1 ps resolution)
  always begin
    #8.333 clk = 1'b1;
    #8.334 clk = 1'b0;
  end

  reg rst_n;
  reg ena;
  reg [7:0] ui_in_pins;
  reg host_rx;
  reg [7:0] uio_in_pins;
  reg uio_rx1;
  reg i2c_scl_low, i2c_sda_low;  // the I2C target model pulls a line low
  reg spi_miso;
  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;
  // an output pin's level as the chip drives it (1 when not driving: pulled up)
  wire scl_bus = (uio_oe[2] ? uio_out[2] : 1'b1) & !i2c_scl_low;
  wire sda_bus = (uio_oe[3] ? uio_out[3] : 1'b1) & !i2c_sda_low;
  wire spi_sck = uio_out[4], spi_mosi = uio_out[5];
  wire spi_cs = uio_oe[6] ? uio_out[6] : 1'b1;  // pulled up: inactive until the chip drives it
  wire [7:0] ui_in = {ui_in_pins[7:4], host_rx, ui_in_pins[2:0]};
  wire [7:0] uio_in = {spi_miso, uio_in_pins[6:4], sda_bus, scl_bus, uio_rx1, uio_in_pins[0]};
  wire host_tx = uo_out[4];
  wire eng_pin0 = uio_out[0];  // engine output pin 0 (uio[0])

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
