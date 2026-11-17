# SPDX-License-Identifier: Apache-2.0
#
# Program the board over JTAG and start the app (any Vitis that ships xsct):
#
#   xsct fpga/sw/run_app.tcl zedboard <path/to/referee_app.elf>
#
# Order matters: PS init (ps7_init) first, then the bitstream, then
# ps7_post_config, which enables the PS-PL level shifters; without it the EMIO
# signals (I2C, UART, GPIO) and FCLK_RESET0_N never reach the PL.

if {[llength $argv] < 2} { error "usage: xsct fpga/sw/run_app.tcl zedboard|pynq-z2 app.elf" }
set board [lindex $argv 0]
set elf   [file normalize [lindex $argv 1]]
set out_dir [file normalize [file join [file dirname [info script]] .. build $board]]
set bit [file join $out_dir referee_$board.bit]

# ps7_init.tcl is inside the XSA (a zip). Use the copy Vitis unpacked into its
# workspace if there is one, else unzip it next to the bitstream.
proc find_file {dir name} {
    foreach f [glob -nocomplain -directory $dir *] {
        if {[file isdirectory $f]} {
            set r [find_file $f $name]
            if {$r ne ""} { return $r }
        } elseif {[file tail $f] eq $name} {
            return $f
        }
    }
    return ""
}
set ps7_init [find_file $out_dir ps7_init.tcl]
if {$ps7_init eq ""} {
    set hw_dir [file join $out_dir hw]
    file mkdir $hw_dir
    if {[catch {exec unzip -o [file join $out_dir referee_$board.xsa] ps7_init.tcl -d $hw_dir}]} {
        error "unzip ps7_init.tcl from [file join $out_dir referee_$board.xsa] into $hw_dir and run again"
    }
    set ps7_init [file join $hw_dir ps7_init.tcl]
}

connect
targets -set -nocase -filter {name =~ "APU*"}
rst -system
after 1000
targets -set -nocase -filter {name =~ "ARM*#0"}
source $ps7_init
ps7_init
fpga -file $bit
ps7_post_config
dow $elf
con
puts "INFO: running. Open the board's USB-UART at 115200 to see the banner."
