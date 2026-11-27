## SPDX-License-Identifier: Apache-2.0
## Referee on PYNQ-Z2 (xc7z020clg400-1): board pins of fpga/rtl/fpga_top.v.
##
## Every PACKAGE_PIN below is copied from the official PYNQ-Z2 constraints in
## the Xilinx PYNQ repository (base overlay):
##   https://raw.githubusercontent.com/Xilinx/PYNQ/image_v3.0.1/boards/Pynq-Z2/base/vivado/constraints/base.xdc
## and cross-checked against TUL's PYNQ-Z2 board file in the Xilinx board store
## (same locations; it also names the Pmod pins JA1..JA10, JB1..JB10 and gives
## the RGB LED order "RGBRGB" for rgb_led[5:0]):
##   https://raw.githubusercontent.com/Xilinx/XilinxBoardStore/2022.2/boards/TUL/pynq-z2/A.0/part0_pins.xml
##   https://raw.githubusercontent.com/Xilinx/XilinxBoardStore/2022.2/boards/TUL/pynq-z2/A.0/board.xml
## No pin here is guessed. All PL banks on PYNQ-Z2 are 3.3 V: LVCMOS33.
## Note: Pmod JA shares its pins with the Raspberry Pi header (RPi GPIO 0..7);
## leave that header empty.

## ---------------------------------------------------------------------------
## I2C bus: Pmod JA pin 3 SCL, pin 4 SDA (JA3 = Y16, JA4 = Y17; the PYNQ base
## design puts pull-ups on these same two pins)
## ---------------------------------------------------------------------------
set_property -dict {PACKAGE_PIN Y16 IOSTANDARD LVCMOS33} [get_ports i2c_scl]
set_property -dict {PACKAGE_PIN Y17 IOSTANDARD LVCMOS33} [get_ports i2c_sda]
## Weak internal pull-ups; add external 2.2-4.7 kohm pull-ups to 3.3 V.
set_property PULLUP true [get_ports i2c_scl]
set_property PULLUP true [get_ports i2c_sda]

## ---------------------------------------------------------------------------
## Chip uio[0..7]: Pmod JB. JB1..JB4 = uio[0..3], JB7..JB10 = uio[4..7]
## JB1 W14, JB2 Y14, JB3 T11, JB4 T10, JB7 V16, JB8 W16, JB9 V12, JB10 W13
## ---------------------------------------------------------------------------
set_property -dict {PACKAGE_PIN W14 IOSTANDARD LVCMOS33} [get_ports {uio[0]}]
set_property -dict {PACKAGE_PIN Y14 IOSTANDARD LVCMOS33} [get_ports {uio[1]}]
set_property -dict {PACKAGE_PIN T11 IOSTANDARD LVCMOS33} [get_ports {uio[2]}]
set_property -dict {PACKAGE_PIN T10 IOSTANDARD LVCMOS33} [get_ports {uio[3]}]
set_property -dict {PACKAGE_PIN V16 IOSTANDARD LVCMOS33} [get_ports {uio[4]}]
set_property -dict {PACKAGE_PIN W16 IOSTANDARD LVCMOS33} [get_ports {uio[5]}]
set_property -dict {PACKAGE_PIN V12 IOSTANDARD LVCMOS33} [get_ports {uio[6]}]
set_property -dict {PACKAGE_PIN W13 IOSTANDARD LVCMOS33} [get_ports {uio[7]}]
set_property PULLUP true [get_ports {uio[*]}]

## ---------------------------------------------------------------------------
## LEDs: led[0..3] = LD0..LD3 (R14 P14 N16 M14);
## led[4] = LD4 red (N15, rgb_led[2]), led[5] = LD4 green (G17, rgb_led[1])
## ---------------------------------------------------------------------------
set_property -dict {PACKAGE_PIN R14 IOSTANDARD LVCMOS33} [get_ports {led[0]}]
set_property -dict {PACKAGE_PIN P14 IOSTANDARD LVCMOS33} [get_ports {led[1]}]
set_property -dict {PACKAGE_PIN N16 IOSTANDARD LVCMOS33} [get_ports {led[2]}]
set_property -dict {PACKAGE_PIN M14 IOSTANDARD LVCMOS33} [get_ports {led[3]}]
set_property -dict {PACKAGE_PIN N15 IOSTANDARD LVCMOS33} [get_ports {led[4]}]
set_property -dict {PACKAGE_PIN G17 IOSTANDARD LVCMOS33} [get_ports {led[5]}]

## ---------------------------------------------------------------------------
## Buttons BTN0..BTN3 (D19 D20 L20 L19; pressed = 1). btn[0] resets the chip.
## ---------------------------------------------------------------------------
set_property -dict {PACKAGE_PIN D19 IOSTANDARD LVCMOS33} [get_ports {btn[0]}]
set_property -dict {PACKAGE_PIN D20 IOSTANDARD LVCMOS33} [get_ports {btn[1]}]
set_property -dict {PACKAGE_PIN L20 IOSTANDARD LVCMOS33} [get_ports {btn[2]}]
set_property -dict {PACKAGE_PIN L19 IOSTANDARD LVCMOS33} [get_ports {btn[3]}]

## ---------------------------------------------------------------------------
## Switches SW0, SW1 (M20, M19)
## ---------------------------------------------------------------------------
set_property -dict {PACKAGE_PIN M20 IOSTANDARD LVCMOS33} [get_ports {sw[0]}]
set_property -dict {PACKAGE_PIN M19 IOSTANDARD LVCMOS33} [get_ports {sw[1]}]

## ---------------------------------------------------------------------------
## PL oscillator, 125 MHz: the ILA's clock source only (fpga_top sys_clk).
## TUL board file part0_pins.xml:  name ="sys_clk" iostandard="LVCMOS33" loc="H16";
## board.xml: interface sys_clock, parameter frequency 125000000. (The PYNQ base.xdc has no
## PL clock pin: the base overlay clocks the PL from the PS.)
##   https://raw.githubusercontent.com/Xilinx/XilinxBoardStore/2022.2/boards/TUL/pynq-z2/A.0/part0_pins.xml
## No create_clock here: the Clocking Wizard IP (clk_ila_wiz) creates it on its input.
## ---------------------------------------------------------------------------
set_property -dict {PACKAGE_PIN H16 IOSTANDARD LVCMOS33} [get_ports sys_clk]

## ---------------------------------------------------------------------------
## Timing: clk60 comes from the PS7 / Clocking Wizard IP constraints; the board
## pins are asynchronous and excluded from timing analysis.
## ---------------------------------------------------------------------------
set_false_path -from [get_ports {uio[*] sw[*] btn[*]}]
## The I2C pads: false paths only into the chip's judge registers (and nothing is timed into
## the PS's EMIO inputs). Their paths into the ILA's sampling flops are NOT false: fpga/ila.xdc
## bounds them, because the ILA's uncertainty includes how differently SCL and SDA reach them.
set_false_path -from [get_ports {i2c_scl i2c_sda}] -to [get_cells {scl_q_reg sda_q_reg}]
set_false_path -to   [get_ports {i2c_scl i2c_sda uio[*] led[*]}]
