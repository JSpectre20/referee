# SPDX-License-Identifier: Apache-2.0
#
# Build the bare-metal app (fpga/sw/main.c) with the classic Vitis command line:
#
#   xsct fpga/sw/build_app.tcl zedboard        (or pynq-z2)
#
# For Vitis 2020.2 .. 2023.1 ("classic" Vitis). Vitis 2023.2 and later use the
# Unified IDE: follow the GUI steps in fpga/README.md instead.
# Input:  fpga/build/<board>/referee_<board>.xsa   (from fpga/build.tcl)
# Output: fpga/build/<board>/ws/referee_app/Debug/referee_app.elf

if {[llength $argv] < 1} { error "usage: xsct fpga/sw/build_app.tcl zedboard|pynq-z2" }
set board [lindex $argv 0]
set sw_dir  [file normalize [file dirname [info script]]]
set out_dir [file normalize [file join $sw_dir .. build $board]]
set xsa [file join $out_dir referee_$board.xsa]
if {![file exists $xsa]} { error "$xsa not found: run fpga/build.tcl first" }

setws [file join $out_dir ws]
# The template's name changed between versions; try both.
if {[catch {app create -name referee_app -hw $xsa -proc ps7_cortexa9_0 -os standalone \
                -lang C -template {Empty Application(C)}} err]} {
    puts "INFO: retrying with the older template name ($err)"
    app create -name referee_app -hw $xsa -proc ps7_cortexa9_0 -os standalone \
        -lang C -template {Empty Application}
}
importsources -name referee_app -path [file join $sw_dir main.c]
app build -name referee_app
puts "INFO: built [file join $out_dir ws referee_app Debug referee_app.elf]"
