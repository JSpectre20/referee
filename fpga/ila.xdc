## SPDX-License-Identifier: Apache-2.0
## The ILA (fpga/rtl/fpga_top.v, "The ILA"), both boards; fpga/build.tcl adds this file only
## when it builds the ILA.
##
## clk_ila comes from the board oscillator through its own MMCM (u_ila_clk); the Clocking
## Wizard IP's own XDC creates the input clock on sys_clk and the MMCM derives clk_ila. It is
## asynchronous to every other clock (clk60 and the PS's FCLK come from the PS crystal): every
## signal it samples enters through the two ASYNC_REG flops ila_s1 / ila_s2. With one -group,
## set_clock_groups makes the group asynchronous to all other clocks.
set_clock_groups -name ila_async -asynchronous -group [get_clocks -of_objects [get_pins u_ila_clk/clk_out1]]

## The judged pads into their sampling flops (ila_s1 bit 0 = SCL, bit 1 = SDA): both paths at
## most 3 ns, so the placer keeps the two flops near the pads and the SCL / SDA difference
## stays well below one 4.17 ns sample. The routed values (min and max corner) are written to
## ila_info.txt by fpga/build.tcl as skew_ns, which run.json's skew_ns must not undercut. The
## two flops share one BUFG; their clock-arrival difference (tens of ps) is not in skew_ns but
## in tools/ila_check.py's aperture_ns (0.25 ns per edge, with the MMCM jitter).
set_max_delay -datapath_only -from [get_ports i2c_scl] -to [get_pins {ila_s1_reg[0]/D}] 3.000
set_max_delay -datapath_only -from [get_ports i2c_sda] -to [get_pins {ila_s1_reg[1]/D}] 3.000
