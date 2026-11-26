## SPDX-License-Identifier: Apache-2.0
## Referee on ZedBoard (xc7z020clg484-1): board pins of fpga/rtl/fpga_top.v.
##
## Every PACKAGE_PIN below is copied from the official ZedBoard master XDC
## (Avnet "Zedboard-Master.xdc", as published by Digilent):
##   https://raw.githubusercontent.com/Digilent/digilent-xdc/master/Zedboard-Master.xdc
## and cross-checked against Avnet's ZedBoard board file (same locations):
##   https://raw.githubusercontent.com/Avnet/bdf/master/zedboard/1.5/part0_pins.xml
## No pin here is guessed. The DDR and MIO pins need no constraints (the PS7 IP
## places them).
##
## I/O standards
##   Bank 13 (Pmods JA..JD) and bank 33 (LEDs) are fixed at 3.3 V on ZedBoard
##   (master XDC notes): LVCMOS33.
##   Buttons (bank 34) and switches (bank 35) sit on VADJ, set by jumper J18.
##   They are inputs only. LVCMOS25 matches Avnet's board file (VADJ = 2.5 V);
##   the master XDC's default is LVCMOS18. An input standard below VADJ is
##   harmless for slow switches, so either jumper setting works; if you set J18
##   to 3.3 V you may change these to LVCMOS33.

## ---------------------------------------------------------------------------
## I2C bus: Pmod JA, Digilent Pmod type-6 I2C positions (pin 3 SCL, pin 4 SDA)
## Master XDC: JA3 = Y10, JA4 = AA9 (bank 13)
## ---------------------------------------------------------------------------
set_property -dict {PACKAGE_PIN Y10 IOSTANDARD LVCMOS33} [get_ports i2c_scl]
set_property -dict {PACKAGE_PIN AA9 IOSTANDARD LVCMOS33} [get_ports i2c_sda]
## Weak internal pull-ups so the bus idles high with nothing attached.
## Add external 2.2-4.7 kohm pull-ups to 3.3 V for realistic I2C edges.
set_property PULLUP true [get_ports i2c_scl]
set_property PULLUP true [get_ports i2c_sda]

## ---------------------------------------------------------------------------
## Chip uio[0..7]: Pmod JB (bank 13). JB1..JB4 = uio[0..3], JB7..JB10 = uio[4..7]
## Master XDC: JB1 W12, JB2 W11, JB3 V10, JB4 W8, JB7 V12, JB8 W10, JB9 V9, JB10 V8
## ---------------------------------------------------------------------------
set_property -dict {PACKAGE_PIN W12 IOSTANDARD LVCMOS33} [get_ports {uio[0]}]
set_property -dict {PACKAGE_PIN W11 IOSTANDARD LVCMOS33} [get_ports {uio[1]}]
set_property -dict {PACKAGE_PIN V10 IOSTANDARD LVCMOS33} [get_ports {uio[2]}]
set_property -dict {PACKAGE_PIN W8  IOSTANDARD LVCMOS33} [get_ports {uio[3]}]
set_property -dict {PACKAGE_PIN V12 IOSTANDARD LVCMOS33} [get_ports {uio[4]}]
set_property -dict {PACKAGE_PIN W10 IOSTANDARD LVCMOS33} [get_ports {uio[5]}]
set_property -dict {PACKAGE_PIN V9  IOSTANDARD LVCMOS33} [get_ports {uio[6]}]
set_property -dict {PACKAGE_PIN V8  IOSTANDARD LVCMOS33} [get_ports {uio[7]}]
## Weak pull-ups: undriven engine pins read 1 (I2C on uio[2..3] still needs
## the bus pull-ups; see fpga/README.md).
set_property PULLUP true [get_ports {uio[*]}]

## ---------------------------------------------------------------------------
## User LEDs LD0..LD7 (bank 33, 3.3 V, active high)
## Master XDC: T22 T21 U22 U21 V22 W22 U19 U14
## ---------------------------------------------------------------------------
set_property -dict {PACKAGE_PIN T22 IOSTANDARD LVCMOS33} [get_ports {led[0]}]
set_property -dict {PACKAGE_PIN T21 IOSTANDARD LVCMOS33} [get_ports {led[1]}]
set_property -dict {PACKAGE_PIN U22 IOSTANDARD LVCMOS33} [get_ports {led[2]}]
set_property -dict {PACKAGE_PIN U21 IOSTANDARD LVCMOS33} [get_ports {led[3]}]
set_property -dict {PACKAGE_PIN V22 IOSTANDARD LVCMOS33} [get_ports {led[4]}]
set_property -dict {PACKAGE_PIN W22 IOSTANDARD LVCMOS33} [get_ports {led[5]}]
set_property -dict {PACKAGE_PIN U19 IOSTANDARD LVCMOS33} [get_ports {led[6]}]
set_property -dict {PACKAGE_PIN U14 IOSTANDARD LVCMOS33} [get_ports {led[7]}]

## ---------------------------------------------------------------------------
## Push buttons (bank 34, VADJ, active high; Avnet board file: "U R L D C")
## btn[0] BTNC P16, btn[1] BTND R16, btn[2] BTNL N15, btn[3] BTNR R18, btn[4] BTNU T18
## ---------------------------------------------------------------------------
set_property -dict {PACKAGE_PIN P16 IOSTANDARD LVCMOS25} [get_ports {btn[0]}]
set_property -dict {PACKAGE_PIN R16 IOSTANDARD LVCMOS25} [get_ports {btn[1]}]
set_property -dict {PACKAGE_PIN N15 IOSTANDARD LVCMOS25} [get_ports {btn[2]}]
set_property -dict {PACKAGE_PIN R18 IOSTANDARD LVCMOS25} [get_ports {btn[3]}]
set_property -dict {PACKAGE_PIN T18 IOSTANDARD LVCMOS25} [get_ports {btn[4]}]

## ---------------------------------------------------------------------------
## DIP switches SW0..SW7 (bank 35, VADJ)
## Master XDC: F22 G22 H22 F21 H19 H18 H17 M15
## ---------------------------------------------------------------------------
set_property -dict {PACKAGE_PIN F22 IOSTANDARD LVCMOS25} [get_ports {sw[0]}]
set_property -dict {PACKAGE_PIN G22 IOSTANDARD LVCMOS25} [get_ports {sw[1]}]
set_property -dict {PACKAGE_PIN H22 IOSTANDARD LVCMOS25} [get_ports {sw[2]}]
set_property -dict {PACKAGE_PIN F21 IOSTANDARD LVCMOS25} [get_ports {sw[3]}]
set_property -dict {PACKAGE_PIN H19 IOSTANDARD LVCMOS25} [get_ports {sw[4]}]
set_property -dict {PACKAGE_PIN H18 IOSTANDARD LVCMOS25} [get_ports {sw[5]}]
set_property -dict {PACKAGE_PIN H17 IOSTANDARD LVCMOS25} [get_ports {sw[6]}]
set_property -dict {PACKAGE_PIN M15 IOSTANDARD LVCMOS25} [get_ports {sw[7]}]

## ---------------------------------------------------------------------------
## Timing. clk60 is constrained by the PS7 and Clocking Wizard IPs. The board
## pins are asynchronous (I2C at <= 400 kHz, UART, switches, LEDs): no I/O
## timing applies, so they are excluded from timing analysis.
## ---------------------------------------------------------------------------
set_false_path -from [get_ports {i2c_scl i2c_sda uio[*] sw[*] btn[*]}]
set_false_path -to   [get_ports {i2c_scl i2c_sda uio[*] led[*]}]
