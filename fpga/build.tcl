# SPDX-License-Identifier: Apache-2.0
#
# Build the Referee chip for a Zynq-7020 board: project, block design (PS7 +
# Clocking Wizard), the ILA (its own Clocking Wizard on the board oscillator +
# an ILA core), synthesis, implementation, bitstream and the XSA for Vitis.
#
#   vivado -mode batch -source fpga/build.tcl -tclargs zedboard
#   vivado -mode batch -source fpga/build.tcl -tclargs pynq-z2
#
# Run it from the repository root (it also works from elsewhere: paths are
# taken relative to this script). Output in fpga/build/<board>/:
#   referee_<board>.xsa   hardware platform, bitstream included (for Vitis)
#   referee_<board>.bit   bitstream
#   referee_<board>.ltx   the ILA's probes file (Vivado hardware manager, fpga/ila_capture.tcl)
#   ila_info.txt          the ILA's clock, depth, probes and measured pad-to-sampling-flop delays
#   timing_summary.rpt, utilization.rpt, clocks.rpt, and the Vivado project
#
# Targets Vivado 2020.2 .. 2024.2 (write_hw_platform, the .gen directory and
# processing_system7 5.5 / clk_wiz 6.0 as found there). Board files are NOT
# needed: every PS7 setting is written out below, with its source.
#
# Optional arguments, in this order:
#   jobs       parallel jobs (default 4)
#   chip MHz   the chip clock (default 60; only for the fallback in fpga/README.md,
#              'If 60 MHz does not close')
#   ILA MHz    the ILA's sampling clock (default 240 = 4 x 60; 200 is the fallback
#              in fpga/README.md, 'If the ILA does not close'; 0 builds without the ILA)
#   ILA depth  samples (default 8192; a power of two, 1024 .. 65536: 72 bits x 131072 would
#              need 288 RAMB36, more than the 7020's 140)
# e.g.  -tclargs zedboard 4 60 200      -tclargs zedboard 4 60 0

if {[llength $argv] < 1} {
    error "usage: vivado -mode batch -source fpga/build.tcl -tclargs zedboard|pynq-z2 \[jobs \[chip_MHz \[ila_MHz \[ila_depth\]\]\]\]"
}
set board [string tolower [lindex $argv 0]]
set jobs  [expr {[llength $argv] > 1 ? [lindex $argv 1] : 4}]
set mhz   [expr {[llength $argv] > 2 ? [lindex $argv 2] : 60}]
set ila_mhz   [expr {[llength $argv] > 3 ? [lindex $argv 3] : 240}]
set ila_depth [expr {[llength $argv] > 4 ? [lindex $argv 4] : 8192}]
if {$ila_mhz != 0 && ($ila_depth < 1024 || $ila_depth > 65536 || ($ila_depth & ($ila_depth - 1)) != 0)} {
    error "ILA depth $ila_depth: must be a power of two from 1024 to 65536"
}
# The chip's power-up UART bit period must be round(clock / 115200): 521 at 60 MHz.
set clks_per_bit [expr {int(round($mhz * 1e6 / 115200.0))}]
puts "INFO: board $board, chip clock $mhz MHz, CLKS_PER_BIT $clks_per_bit"
if {$ila_mhz != 0} {
    puts "INFO: ILA at $ila_mhz MHz, $ila_depth samples"
} else {
    puts "INFO: no ILA (fpga/rtl/ila_none.v)"
}

set fpga_dir [file normalize [file dirname [info script]]]
set repo_dir [file normalize [file join $fpga_dir ..]]
set out_dir  [file join $fpga_dir build $board]

# =============================================================================
# Board table
#   uart_emio: the PS UART that goes through EMIO to the chip (the other one is
#              the USB-UART on MIO, the PC's console)
#   generics:  fpga_top port widths, matching fpga/<board>.xdc
#   sys_mhz:   the board's PL oscillator on sys_clk (the ILA's clock source):
#              ZedBoard 100 MHz on Y9, PYNQ-Z2 125 MHz on H16 (sources in the XDCs)
# =============================================================================
switch -- $board {
    zedboard {
        set part      xc7z020clg484-1
        set uart_emio 0
        set generics  {N_LED=8 N_SW=8 N_BTN=5}
        set sys_mhz   100.000
    }
    pynq-z2 {
        set part      xc7z020clg400-1
        set uart_emio 1
        set generics  {N_LED=6 N_SW=2 N_BTN=4}
        set sys_mhz   125.000
    }
    default {
        error "unknown board '$board': use zedboard or pynq-z2"
    }
}

# =============================================================================
# PS7 settings per board
# =============================================================================
# --- ZedBoard ----------------------------------------------------------------
# Source: Digilent's ZedBoard board file, preset.xml (ps7_preset):
#   https://raw.githubusercontent.com/Digilent/vivado-boards/master/new/board_files/zedboard/1.3/preset.xml
# which first applies the PS7 IP's own built-in preset "ZedBoard" (the same one
# Avnet's current board file applies:
#   https://raw.githubusercontent.com/Avnet/bdf/master/zedboard/1.5/preset.xml )
# and then sets the values below. The built-in preset is part of the PS7 IP,
# not of a board file, so this works without board files installed.
# USB-UART = UART1 on MIO 48..49: ZedBoard Hardware User's Guide; also the
# ZedBoard ps7_init in U-Boot (board/xilinx/zynq/zynq-zed/ps7_init_gpl.c:
# MIO_PIN_48 L3_SEL = 7 (UART), UART1 clock on, UART0 clock off).
set ps7_preset(zedboard) ZedBoard
set ps7_board(zedboard) {
    CONFIG.PCW_PRESET_BANK1_VOLTAGE          {LVCMOS 1.8V}
    CONFIG.PCW_CRYSTAL_PERIPHERAL_FREQMHZ    {33.333333}
    CONFIG.PCW_APU_PERIPHERAL_FREQMHZ        {650}
    CONFIG.PCW_UIPARAM_DDR_PARTNO            {MT41K128M16 JT-125}
    CONFIG.PCW_UIPARAM_DDR_FREQ_MHZ          {525}
    CONFIG.PCW_UIPARAM_DDR_BOARD_DELAY0      {0.176}
    CONFIG.PCW_UIPARAM_DDR_BOARD_DELAY1      {0.159}
    CONFIG.PCW_UIPARAM_DDR_BOARD_DELAY2      {0.162}
    CONFIG.PCW_UIPARAM_DDR_BOARD_DELAY3      {0.187}
    CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_0 {-0.073}
    CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_1 {-0.034}
    CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_2 {-0.03}
    CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_3 {-0.082}
    CONFIG.PCW_UIPARAM_DDR_TRAIN_DATA_EYE    {1}
    CONFIG.PCW_UIPARAM_DDR_TRAIN_READ_GATE   {1}
    CONFIG.PCW_UIPARAM_DDR_TRAIN_WRITE_LEVEL {1}
    CONFIG.PCW_UART1_PERIPHERAL_ENABLE       {1}
    CONFIG.PCW_UART1_UART1_IO                {MIO 48 .. 49}
}

# --- PYNQ-Z2 -----------------------------------------------------------------
# Source: the Xilinx PYNQ repository, PYNQ-Z2 base overlay block design:
#   https://raw.githubusercontent.com/Xilinx/PYNQ/image_v3.0.1/boards/Pynq-Z2/base/base.tcl
# (the processing_system7 cell ps7_0). Copied: bank voltages, crystal, CPU
# clock, the complete DDR3 set (16-bit, "Custom" part) and UART0 on MIO 14..15
# (the USB-UART). Not copied: its other peripherals and AXI ports.
set ps7_preset(pynq-z2) {}
set ps7_board(pynq-z2) {
    CONFIG.PCW_PRESET_BANK0_VOLTAGE          {LVCMOS 3.3V}
    CONFIG.PCW_PRESET_BANK1_VOLTAGE          {LVCMOS 1.8V}
    CONFIG.PCW_CRYSTAL_PERIPHERAL_FREQMHZ    {50}
    CONFIG.PCW_APU_PERIPHERAL_FREQMHZ        {650}
    CONFIG.PCW_UIPARAM_DDR_ENABLE            {1}
    CONFIG.PCW_UIPARAM_DDR_ADV_ENABLE        {0}
    CONFIG.PCW_UIPARAM_DDR_MEMORY_TYPE       {DDR 3}
    CONFIG.PCW_UIPARAM_DDR_SPEED_BIN         {DDR3_1066F}
    CONFIG.PCW_UIPARAM_DDR_FREQ_MHZ          {525}
    CONFIG.PCW_UIPARAM_DDR_BUS_WIDTH         {16 Bit}
    CONFIG.PCW_UIPARAM_DDR_DRAM_WIDTH        {16 Bits}
    CONFIG.PCW_UIPARAM_DDR_DEVICE_CAPACITY   {4096 MBits}
    CONFIG.PCW_UIPARAM_DDR_BANK_ADDR_COUNT   {3}
    CONFIG.PCW_UIPARAM_DDR_ROW_ADDR_COUNT    {15}
    CONFIG.PCW_UIPARAM_DDR_COL_ADDR_COUNT    {10}
    CONFIG.PCW_UIPARAM_DDR_CL                {7}
    CONFIG.PCW_UIPARAM_DDR_CWL               {6}
    CONFIG.PCW_UIPARAM_DDR_AL                {0}
    CONFIG.PCW_UIPARAM_DDR_BL                {8}
    CONFIG.PCW_UIPARAM_DDR_T_FAW             {40.0}
    CONFIG.PCW_UIPARAM_DDR_T_RAS_MIN         {35.0}
    CONFIG.PCW_UIPARAM_DDR_T_RC              {50.625}
    CONFIG.PCW_UIPARAM_DDR_T_RCD             {13.125}
    CONFIG.PCW_UIPARAM_DDR_T_RP              {13.125}
    CONFIG.PCW_UIPARAM_DDR_ECC               {Disabled}
    CONFIG.PCW_UIPARAM_DDR_HIGH_TEMP         {Normal (0-85)}
    CONFIG.PCW_UIPARAM_DDR_CLOCK_STOP_EN     {0}
    CONFIG.PCW_UIPARAM_DDR_USE_INTERNAL_VREF {0}
    CONFIG.PCW_UIPARAM_DDR_TRAIN_DATA_EYE    {1}
    CONFIG.PCW_UIPARAM_DDR_TRAIN_READ_GATE   {1}
    CONFIG.PCW_UIPARAM_DDR_TRAIN_WRITE_LEVEL {1}
    CONFIG.PCW_UIPARAM_DDR_BOARD_DELAY0      {0.223}
    CONFIG.PCW_UIPARAM_DDR_BOARD_DELAY1      {0.212}
    CONFIG.PCW_UIPARAM_DDR_BOARD_DELAY2      {0.085}
    CONFIG.PCW_UIPARAM_DDR_BOARD_DELAY3      {0.092}
    CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_0 {0.040}
    CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_1 {0.058}
    CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_2 {0.000}
    CONFIG.PCW_UIPARAM_DDR_DQS_TO_CLK_DELAY_3 {0.000}
    CONFIG.PCW_UIPARAM_DDR_CLOCK_0_LENGTH_MM {25.8}
    CONFIG.PCW_UIPARAM_DDR_CLOCK_0_PACKAGE_LENGTH {80.4535}
    CONFIG.PCW_UIPARAM_DDR_CLOCK_0_PROPOGATION_DELAY {160}
    CONFIG.PCW_UIPARAM_DDR_CLOCK_1_LENGTH_MM {25.8}
    CONFIG.PCW_UIPARAM_DDR_CLOCK_1_PACKAGE_LENGTH {80.4535}
    CONFIG.PCW_UIPARAM_DDR_CLOCK_1_PROPOGATION_DELAY {160}
    CONFIG.PCW_UIPARAM_DDR_CLOCK_2_LENGTH_MM {0}
    CONFIG.PCW_UIPARAM_DDR_CLOCK_2_PACKAGE_LENGTH {80.4535}
    CONFIG.PCW_UIPARAM_DDR_CLOCK_2_PROPOGATION_DELAY {160}
    CONFIG.PCW_UIPARAM_DDR_CLOCK_3_LENGTH_MM {0}
    CONFIG.PCW_UIPARAM_DDR_CLOCK_3_PACKAGE_LENGTH {80.4535}
    CONFIG.PCW_UIPARAM_DDR_CLOCK_3_PROPOGATION_DELAY {160}
    CONFIG.PCW_UIPARAM_DDR_DQS_0_LENGTH_MM   {15.6}
    CONFIG.PCW_UIPARAM_DDR_DQS_0_PACKAGE_LENGTH {105.056}
    CONFIG.PCW_UIPARAM_DDR_DQS_0_PROPOGATION_DELAY {160}
    CONFIG.PCW_UIPARAM_DDR_DQS_1_LENGTH_MM   {18.8}
    CONFIG.PCW_UIPARAM_DDR_DQS_1_PACKAGE_LENGTH {66.904}
    CONFIG.PCW_UIPARAM_DDR_DQS_1_PROPOGATION_DELAY {160}
    CONFIG.PCW_UIPARAM_DDR_DQS_2_LENGTH_MM   {0}
    CONFIG.PCW_UIPARAM_DDR_DQS_2_PACKAGE_LENGTH {89.1715}
    CONFIG.PCW_UIPARAM_DDR_DQS_2_PROPOGATION_DELAY {160}
    CONFIG.PCW_UIPARAM_DDR_DQS_3_LENGTH_MM   {0}
    CONFIG.PCW_UIPARAM_DDR_DQS_3_PACKAGE_LENGTH {113.63}
    CONFIG.PCW_UIPARAM_DDR_DQS_3_PROPOGATION_DELAY {160}
    CONFIG.PCW_UIPARAM_DDR_DQ_0_LENGTH_MM    {16.5}
    CONFIG.PCW_UIPARAM_DDR_DQ_0_PACKAGE_LENGTH {98.503}
    CONFIG.PCW_UIPARAM_DDR_DQ_0_PROPOGATION_DELAY {160}
    CONFIG.PCW_UIPARAM_DDR_DQ_1_LENGTH_MM    {18}
    CONFIG.PCW_UIPARAM_DDR_DQ_1_PACKAGE_LENGTH {68.5855}
    CONFIG.PCW_UIPARAM_DDR_DQ_1_PROPOGATION_DELAY {160}
    CONFIG.PCW_UIPARAM_DDR_DQ_2_LENGTH_MM    {0}
    CONFIG.PCW_UIPARAM_DDR_DQ_2_PACKAGE_LENGTH {90.295}
    CONFIG.PCW_UIPARAM_DDR_DQ_2_PROPOGATION_DELAY {160}
    CONFIG.PCW_UIPARAM_DDR_DQ_3_LENGTH_MM    {0}
    CONFIG.PCW_UIPARAM_DDR_DQ_3_PACKAGE_LENGTH {103.977}
    CONFIG.PCW_UIPARAM_DDR_DQ_3_PROPOGATION_DELAY {160}
    CONFIG.PCW_UART0_PERIPHERAL_ENABLE       {1}
    CONFIG.PCW_UART0_UART0_IO                {MIO 14 .. 15}
}

# --- Ours, both boards ---------------------------------------------------------
# FCLK_CLK0 100 MHz (+ its reset) for the MMCM; both I2C controllers, the
# chip-side UART and 20 GPIO bits on EMIO; no AXI port (nothing in the PL is
# memory-mapped, so M_AXI_GP0 is switched off and needs no clock).
set ps7_ours [list \
    CONFIG.PCW_USE_M_AXI_GP0                 {0} \
    CONFIG.PCW_EN_CLK0_PORT                  {1} \
    CONFIG.PCW_EN_RST0_PORT                  {1} \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ      {100} \
    CONFIG.PCW_UART_PERIPHERAL_FREQMHZ       {100} \
    CONFIG.PCW_I2C0_PERIPHERAL_ENABLE        {1} \
    CONFIG.PCW_I2C0_I2C0_IO                  {EMIO} \
    CONFIG.PCW_I2C1_PERIPHERAL_ENABLE        {1} \
    CONFIG.PCW_I2C1_I2C1_IO                  {EMIO} \
    CONFIG.PCW_UART${uart_emio}_PERIPHERAL_ENABLE {1} \
    CONFIG.PCW_UART${uart_emio}_UART${uart_emio}_IO {EMIO} \
    CONFIG.PCW_GPIO_EMIO_GPIO_ENABLE         {1} \
    CONFIG.PCW_GPIO_EMIO_GPIO_IO             {20} \
]

# =============================================================================
# Project and sources
# =============================================================================
file mkdir $out_dir
create_project -force referee_$board [file join $out_dir vivado] -part $part
set_property target_language Verilog [current_project]

# The chip's RTL, unchanged: the list in info.yaml (source_files), from src/.
set fh [open [file join $repo_dir info.yaml] r]
set yaml [read $fh]
close $fh
set chip_srcs {}
set in_list 0
foreach line [split $yaml "\n"] {
    if {[regexp {^\s*source_files:} $line]} { set in_list 1; continue }
    if {$in_list} {
        if {[regexp {^\s*-\s*"?([^"\s]+)"?} $line -> f]} {
            lappend chip_srcs [file join $repo_dir src $f]
        } elseif {[regexp {^\s*#} $line] || [string trim $line] eq ""} {
            continue
        } else {
            break
        }
    }
}
if {[llength $chip_srcs] == 0} { error "no source_files found in info.yaml" }
puts "INFO: chip sources: $chip_srcs"
add_files -norecurse $chip_srcs
add_files -norecurse [file join $fpga_dir rtl fpga_top.v]
add_files -fileset constrs_1 -norecurse [file join $fpga_dir $board.xdc]

# =============================================================================
# Block design zynq_ps: PS7 + Clocking Wizard, everything else is in fpga_top
# =============================================================================
proc latest_ip {name} {
    set defs [lsort [get_ipdefs -all "xilinx.com:ip:${name}:*"]]
    if {[llength $defs] == 0} { error "IP $name not found in this Vivado" }
    return [lindex $defs end]
}

create_bd_design zynq_ps

set ps7 [create_bd_cell -type ip -vlnv [latest_ip processing_system7] ps7]
if {$ps7_preset($board) ne ""} {
    set_property CONFIG.preset $ps7_preset($board) $ps7
}
if {$board eq "pynq-z2"} {
    # "Custom" first, so the explicit DDR3 timing below is accepted.
    set_property CONFIG.PCW_UIPARAM_DDR_PARTNO {Custom} $ps7
}
set_property -dict $ps7_board($board) $ps7
set_property -dict $ps7_ours $ps7

# Check the settings that matter actually took (Vivado can silently keep a value).
proc expect_cfg {cell key want} {
    set got [get_property CONFIG.$key $cell]
    if {$got ne $want} { error "PS7 $key is '$got', expected '$want'" }
}
expect_cfg $ps7 PCW_I2C0_I2C0_IO EMIO
expect_cfg $ps7 PCW_I2C1_I2C1_IO EMIO
expect_cfg $ps7 PCW_UART${uart_emio}_UART${uart_emio}_IO EMIO
expect_cfg $ps7 PCW_GPIO_EMIO_GPIO_IO 20
expect_cfg $ps7 PCW_USE_M_AXI_GP0 0
if {$board eq "zedboard"} {
    expect_cfg $ps7 PCW_UART1_UART1_IO {MIO 48 .. 49}
    expect_cfg $ps7 PCW_UIPARAM_DDR_PARTNO {MT41K128M16 JT-125}
} else {
    expect_cfg $ps7 PCW_UART0_UART0_IO {MIO 14 .. 15}
    expect_cfg $ps7 PCW_UIPARAM_DDR_BUS_WIDTH {16 Bit}
}
puts "INFO: FCLK_CLK0 actual = [get_property CONFIG.PCW_ACT_FPGA0_PERIPHERAL_FREQMHZ $ps7] MHz"

# DDR and the fixed MIO pins straight out (Board_Preset off: no board files).
create_bd_intf_port -mode Master -vlnv xilinx.com:interface:ddrx_rtl:1.0 DDR
create_bd_intf_port -mode Master -vlnv xilinx.com:display_processing_system7:fixedio_rtl:1.0 FIXED_IO
connect_bd_intf_net [get_bd_intf_pins ps7/DDR]      [get_bd_intf_ports DDR]
connect_bd_intf_net [get_bd_intf_pins ps7/FIXED_IO] [get_bd_intf_ports FIXED_IO]

# EMIO interfaces as raw _i/_o/_t ports. fpga_top instantiates the block design
# itself (not a generated wrapper), so no IOBUF is inserted here: fpga_top
# builds the open-drain nets.
create_bd_intf_port -mode Master -vlnv xilinx.com:interface:iic_rtl:1.0  IIC_0
create_bd_intf_port -mode Master -vlnv xilinx.com:interface:iic_rtl:1.0  IIC_1
create_bd_intf_port -mode Master -vlnv xilinx.com:interface:uart_rtl:1.0 UART_EMIO
create_bd_intf_port -mode Master -vlnv xilinx.com:interface:gpio_rtl:1.0 GPIO_0
connect_bd_intf_net [get_bd_intf_pins ps7/IIC_0]  [get_bd_intf_ports IIC_0]
connect_bd_intf_net [get_bd_intf_pins ps7/IIC_1]  [get_bd_intf_ports IIC_1]
connect_bd_intf_net [get_bd_intf_pins ps7/UART_${uart_emio}] [get_bd_intf_ports UART_EMIO]
connect_bd_intf_net [get_bd_intf_pins ps7/GPIO_0] [get_bd_intf_ports GPIO_0]

# MMCM: 100 MHz FCLK_CLK0 -> exactly 60.000 MHz (or the fallback clock), reset
# by the PS's FCLK reset.
set clkw [create_bd_cell -type ip -vlnv [latest_ip clk_wiz] clk_wiz_0]
set_property -dict [list \
    CONFIG.PRIMITIVE                  {MMCM} \
    CONFIG.PRIM_IN_FREQ               {100.000} \
    CONFIG.PRIM_SOURCE                {Global_buffer} \
    CONFIG.CLKOUT1_REQUESTED_OUT_FREQ [format %.3f $mhz] \
    CONFIG.USE_RESET                  {true} \
    CONFIG.RESET_TYPE                 {ACTIVE_LOW} \
    CONFIG.USE_LOCKED                 {true} \
] $clkw
connect_bd_net [get_bd_pins ps7/FCLK_CLK0]     [get_bd_pins clk_wiz_0/clk_in1]
connect_bd_net [get_bd_pins ps7/FCLK_RESET0_N] [get_bd_pins clk_wiz_0/resetn]

# Exact? fin * M / (D * O) from the wizard's own MMCM settings.
set m [get_property CONFIG.MMCM_CLKFBOUT_MULT_F $clkw]
set d [get_property CONFIG.MMCM_DIVCLK_DIVIDE $clkw]
set o [get_property CONFIG.MMCM_CLKOUT0_DIVIDE_F $clkw]
set f [expr {100.0 * $m / ($d * $o)}]
puts "INFO: MMCM M=$m D=$d O=$o -> clk60 = $f MHz"
if {abs($f - $mhz) > 1e-6} { error "MMCM output is $f MHz, not $mhz" }

create_bd_port -dir O -type clk clk60
set_property CONFIG.FREQ_HZ [expr {int(round($mhz * 1e6))}] [get_bd_ports clk60]
create_bd_port -dir O mmcm_locked
create_bd_port -dir O -type rst ps_rst_n
connect_bd_net [get_bd_pins clk_wiz_0/clk_out1] [get_bd_ports clk60]
connect_bd_net [get_bd_pins clk_wiz_0/locked]   [get_bd_ports mmcm_locked]
connect_bd_net [get_bd_pins ps7/FCLK_RESET0_N]  [get_bd_ports ps_rst_n]

validate_bd_design
save_bd_design
set bd_file [get_files zynq_ps.bd]
generate_target all $bd_file

# The port names fpga_top uses must be exactly what Vivado generated.
set bd_hdl ""
foreach f [get_files -all -of_objects $bd_file] {
    if {[file tail $f] eq "zynq_ps.v" && [string match "*synth*" $f]} { set bd_hdl $f }
}
if {$bd_hdl eq ""} {
    foreach f [get_files -all -of_objects $bd_file] {
        if {[file tail $f] eq "zynq_ps.v"} { set bd_hdl $f }
    }
}
if {$bd_hdl ne ""} {
    set fh [open $bd_hdl r]; set txt [read $fh]; close $fh
    foreach p {clk60 mmcm_locked ps_rst_n IIC_0_scl_i IIC_0_scl_o IIC_0_scl_t
               IIC_0_sda_i IIC_0_sda_o IIC_0_sda_t IIC_1_scl_i IIC_1_scl_o
               IIC_1_scl_t IIC_1_sda_i IIC_1_sda_o IIC_1_sda_t UART_EMIO_txd
               UART_EMIO_rxd GPIO_0_tri_i GPIO_0_tri_o GPIO_0_tri_t FIXED_IO_mio DDR_dq} {
        if {![regexp "\\m${p}\\M" $txt]} {
            error "block design port $p not found in $bd_hdl: fpga/rtl/fpga_top.v must match it"
        }
    }
    puts "INFO: block design ports match fpga_top ($bd_hdl)"
} else {
    puts "WARNING: could not find the generated zynq_ps.v to check port names"
}

# =============================================================================
# The ILA (fpga/rtl/fpga_top.v, "The ILA"): two IPs instantiated in fpga_top
# =============================================================================
# clk_ila_wiz: an MMCM on the board oscillator (sys_clk), not on the PS's FCLK
# and not shared with the chip's MMCM, so the ILA's time base is independent of
# the chip's clock path and runs from configuration on. ila_bus: the ILA, 12
# probes in the order of fpga_top's u_ila, capture control (storage
# qualification) on, ila_ts data-only (no comparators: it is 40 bits wide and
# never triggers anything).
set ila_probe_widths {1 1 1 1 1 8 8 8 1 1 1 40}
set ila_probe_names  {ila_scl ila_sda ila_scl_chip ila_sda_chip ila_uart_rx ila_uo_out
                      ila_uio_in ila_uio_oe ila_rst_n ila_mark ila_chg ila_ts}
if {$ila_mhz != 0} {
    create_ip -name clk_wiz -vendor xilinx.com -library ip -module_name clk_ila_wiz
    set ciw [get_ips clk_ila_wiz]
    set_property -dict [list \
        CONFIG.PRIMITIVE                  {MMCM} \
        CONFIG.PRIM_IN_FREQ               [format %.3f $sys_mhz] \
        CONFIG.PRIM_SOURCE                {Single_ended_clock_capable_pin} \
        CONFIG.CLKOUT1_REQUESTED_OUT_FREQ [format %.3f $ila_mhz] \
        CONFIG.USE_RESET                  {false} \
        CONFIG.USE_LOCKED                 {true} \
    ] $ciw
    set m [get_property CONFIG.MMCM_CLKFBOUT_MULT_F $ciw]
    set d [get_property CONFIG.MMCM_DIVCLK_DIVIDE $ciw]
    set o [get_property CONFIG.MMCM_CLKOUT0_DIVIDE_F $ciw]
    set ila_f [expr {$sys_mhz * $m / ($d * $o)}]
    puts "INFO: ILA MMCM M=$m D=$d O=$o -> clk_ila = $ila_f MHz (VCO [expr {$sys_mhz * $m / $d}] MHz)"
    if {abs($ila_f - $ila_mhz) > 1e-6} { error "ILA MMCM output is $ila_f MHz, not $ila_mhz" }

    create_ip -name ila -vendor xilinx.com -library ip -module_name ila_bus
    set ila_ip [get_ips ila_bus]
    set cfg [list \
        CONFIG.C_NUM_OF_PROBES       [llength $ila_probe_widths] \
        CONFIG.C_DATA_DEPTH          $ila_depth \
        CONFIG.C_EN_STRG_QUAL        {1} \
        CONFIG.C_ADV_TRIGGER         {false} \
        CONFIG.C_INPUT_PIPE_STAGES   {1} \
        CONFIG.C_TRIGIN_EN           {false} \
        CONFIG.C_TRIGOUT_EN          {false} \
        CONFIG.ALL_PROBE_SAME_MU     {true} \
        CONFIG.ALL_PROBE_SAME_MU_CNT {2} \
    ]
    for {set i 0} {$i < [llength $ila_probe_widths]} {incr i} {
        lappend cfg CONFIG.C_PROBE${i}_WIDTH [lindex $ila_probe_widths $i]
    }
    # ila_ts (probe 11): data only (C_PROBE<n>_TYPE 1)
    lappend cfg CONFIG.C_PROBE11_TYPE {1}
    set_property -dict $cfg $ila_ip
    foreach {key want} [list C_NUM_OF_PROBES [llength $ila_probe_widths] C_DATA_DEPTH $ila_depth \
                            C_EN_STRG_QUAL 1 C_PROBE11_WIDTH 40 C_PROBE5_WIDTH 8 C_PROBE11_TYPE 1] {
        set got [get_property CONFIG.$key $ila_ip]
        if {$got ne $want} { error "ILA $key is '$got', expected '$want'" }
    }
    generate_target all [get_files clk_ila_wiz.xci]
    generate_target all [get_files ila_bus.xci]
    add_files -fileset constrs_1 -norecurse [file join $fpga_dir ila.xdc]
} else {
    add_files -norecurse [file join $fpga_dir rtl ila_none.v]
}

# =============================================================================
# Top, generics, synthesis, implementation, bitstream
# =============================================================================
set_property top fpga_top [current_fileset]
set_property generic "$generics CLKS_PER_BIT=$clks_per_bit" [current_fileset]
update_compile_order -fileset sources_1

launch_runs synth_1 -jobs $jobs
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {
    error "synthesis failed: see [get_property DIRECTORY [get_runs synth_1]]/runme.log"
}

launch_runs impl_1 -to_step write_bitstream -jobs $jobs
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {
    error "implementation failed: see [get_property DIRECTORY [get_runs impl_1]]/runme.log"
}

open_run impl_1
report_timing_summary -max_paths 10 -file [file join $out_dir timing_summary.rpt]
report_utilization -file [file join $out_dir utilization.rpt]
report_clocks -file [file join $out_dir clocks.rpt]
set wns [get_property SLACK [get_timing_paths -delay_type max -max_paths 1]]
set whs [get_property SLACK [get_timing_paths -delay_type min -max_paths 1]]
puts "INFO: worst setup slack $wns ns, worst hold slack $whs ns"

# Slack per clock domain: the chip's (clk60) and the ILA's (clk_ila, with the
# debug hub on it). A miss in only one of them means different things.
proc clock_slack {pin type} {
    if {[catch {
        set c [get_clocks -of_objects [get_pins $pin]]
        set s [get_property SLACK [get_timing_paths -delay_type $type -max_paths 1 -to $c]]
    }]} { return "?" }
    return $s
}
set chip_wns [clock_slack u_ps/clk60 max]
set chip_whs [clock_slack u_ps/clk60 min]
puts "INFO: clk60 (chip): setup slack $chip_wns ns, hold slack $chip_whs ns"
if {$ila_mhz != 0} {
    set ila_wns [clock_slack u_ila_clk/clk_out1 max]
    set ila_whs [clock_slack u_ila_clk/clk_out1 min]
    puts "INFO: clk_ila (ILA, $ila_mhz MHz): setup slack $ila_wns ns, hold slack $ila_whs ns"
}

set impl_dir [get_property DIRECTORY [get_runs impl_1]]
file copy -force [file join $impl_dir fpga_top.bit] [file join $out_dir referee_$board.bit]
write_hw_platform -fixed -include_bit -force [file join $out_dir referee_$board.xsa]

if {$ila_mhz != 0} {
    write_debug_probes -force [file join $out_dir referee_$board.ltx]
    # The ILA's own uncertainty includes how differently the pads reach their
    # sampling flops (ila_s1, bit 0 = SCL, bit 1 = SDA). fpga/ila.xdc bounds both
    # paths (set_max_delay -datapath_only 3 ns); the routed values are written
    # here, and skew_ns in each run's run.json must be at least this skew_ns.
    proc pad_delay {port bit type} {
        if {[catch {
            set p [get_timing_paths -delay_type $type -max_paths 1 \
                       -from [get_ports $port] -to [get_pins "ila_s1_reg\[$bit\]/D"]]
            set d [get_property DATAPATH_DELAY $p]
        }] || $d eq ""} { return "?" }
        return $d
    }
    set fh [open [file join $out_dir ila_info.txt] w]
    puts $fh "board $board"
    puts $fh "ila_clock_hz [expr {int(round($ila_mhz * 1e6))}]"
    puts $fh "ila_depth $ila_depth"
    puts $fh "chip_clock_hz [expr {int(round($mhz * 1e6))}]"
    puts $fh "probes [join $ila_probe_names { }]"
    puts $fh "probe_widths [join $ila_probe_widths { }]"
    set delays {}
    foreach {port bit} {i2c_scl 0 i2c_sda 1} {
        foreach type {min max} {
            set v [pad_delay $port $bit $type]
            puts $fh "pad_to_flop_${type}_ns $port $v"
            dict set delays $port $type $v
        }
    }
    set sk "?"
    catch {
        set a [expr {[dict get $delays i2c_scl max] - [dict get $delays i2c_sda min]}]
        set b [expr {[dict get $delays i2c_sda max] - [dict get $delays i2c_scl min]}]
        set sk [format %.3f [expr {max(abs($a), abs($b))}]]
    }
    puts $fh "skew_ns $sk"
    puts $fh "ila_setup_slack_ns $ila_wns"
    puts $fh "build_args [join $argv { }]"
    set sha "?"
    catch {set sha [lindex [exec sha256sum [file join $out_dir referee_$board.bit]] 0]}
    puts $fh "bitstream_sha256 $sha"
    close $fh
    puts "INFO: ILA: probes file referee_$board.ltx; pad-to-sampling-flop skew $sk ns (ila_info.txt)"
    if {$sk eq "?"} {
        error "the SCL / SDA pad-to-sampling-flop delays could not be read: the ILA's skew is unbounded,\
               tools/ila_check.py will refuse runs of this bitstream (see fpga/README.md, 'The ILA')"
    }
    if {$sk > 3.0} {
        error "SCL / SDA pad-to-sampling-flop skew $sk ns exceeds 3 ns: check fpga/ila.xdc took effect"
    }
}

if {$wns eq "" || $whs eq "" || $wns < 0 || $whs < 0} {
    if {$ila_mhz != 0 && $chip_wns ne "?" && $chip_whs ne "?" && $chip_wns >= 0 && $chip_whs >= 0} {
        puts "CRITICAL WARNING: TIMING NOT MET in the ILA's clock domain at $ila_mhz MHz (setup $ila_wns ns, hold $ila_whs ns);"
        puts "CRITICAL WARNING: the chip's clk60 domain is met. Do not trust the ILA capture of this bitstream;"
        puts "CRITICAL WARNING: see fpga/README.md, 'If the ILA does not close' (rebuild with 200 MHz)."
    } else {
        puts "CRITICAL WARNING: TIMING NOT MET at $mhz MHz (setup $wns ns, hold $whs ns)."
        puts "CRITICAL WARNING: do not trust verdicts from this bitstream; see fpga/README.md, 'If 60 MHz does not close'."
    }
} else {
    puts "INFO: timing met at $mhz MHz[expr {$ila_mhz != 0 ? " (chip) and $ila_mhz MHz (ILA)" : ""}]."
}
puts "INFO: done: [file join $out_dir referee_$board.xsa]"
