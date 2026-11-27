# SPDX-License-Identifier: Apache-2.0
#
# Capture the ILA of the Referee FPGA build (fpga/build.tcl) with the Vivado
# hardware manager, in batch mode, and export the samples for tools/ila_check.py.
#
#   vivado -mode batch -source fpga/ila_capture.tcl -tclargs <run folder> [options]
#
# e.g. (the board already programmed and the app running, fpga/README.md steps 3-4):
#   vivado -mode batch -source fpga/ila_capture.tcl -tclargs fpga/runs/2026-12-01_zedboard_400k -trigger mark
#
# It connects to the hw_server, finds the xc7z020, loads the probes file
# (fpga/build/<board>/referee_<board>.ltx), sets up the ILA, ARMS it, prints
# "ARMED", waits for the trigger and a full buffer, then writes into the run folder:
#   ila.csv      every stored sample (write_hw_ila_data -csv_file): what ila_check.py reads
#   ila.ila      the same capture in Vivado's own format (open it in the hardware manager)
#   ila.vcd      the same as a VCD, for GTKWave (git-ignored: large)
#   capture.txt  the settings used, the ILA's status and the time
# It does NOT program the FPGA unless -program is given: run_app.tcl / Vitis already did,
# and reprogramming the PL resets the chip under a running app.
#
# Options (all optional):
#   -board zedboard|pynq-z2   which build's .ltx / .bit to use (default zedboard)
#   -ltx <file>               the probes file (default fpga/build/<board>/referee_<board>.ltx)
#   -trigger <t>              scl_fall (default): the first SCL fall (a START's) after arming
#                             sda_fall, mark (the PS's mark rises: start of a traffic cycle,
#                             fpga/sw/main.c), uart_rx (a host byte's start bit),
#                             uart_tx (a chip reply's start bit), now (immediately)
#   -mode changes|all         changes (default): store only samples where a probe changed
#                             (capture control "ila_chg == 1"; plus a 1.09 ms heartbeat),
#                             so the buffer spans seconds: use this for ila_check.py.
#                             all: every sample, a contiguous window of depth / 240 MHz
#                             (34 us at 8192, a few bytes of a ~300 us BTNL burst): for
#                             looking at edges, not for checking a run
#   -position <n>             trigger position in the window (default 16)
#   -depth <n>                window depth, a power of two <= the built depth (default: built)
#   -timeout <minutes>        give up waiting after this long (default 10)
#   -url <host:port>          hw_server (default localhost:3121)
#   -jtag_hz <hz>             JTAG clock, e.g. 1000000 if the debug hub is not detected
#   -program                  program fpga/build/<board>/referee_<board>.bit first
#
# Targets Vivado 2020.2 .. 2024.2 (open_hw_manager, write_hw_ila_data -csv_file / -vcd_file).
# NOT YET RUN on any Vivado: see fpga/README.md, "What is not verified".

proc usage {} {
    error "usage: vivado -mode batch -source fpga/ila_capture.tcl -tclargs <run folder> \[-board zedboard|pynq-z2\] \[-trigger scl_fall|sda_fall|mark|uart_rx|uart_tx|now\] \[-mode changes|all\] \[-position n\] \[-depth n\] \[-timeout min\] \[-url host:port\] \[-jtag_hz hz\] \[-ltx file\] \[-program\]"
}
if {[llength $argv] < 1} { usage }
set run_dir [file normalize [lindex $argv 0]]
array set opt {-board zedboard -ltx "" -trigger scl_fall -mode changes -position 16 -depth 0
               -timeout 10 -url localhost:3121 -jtag_hz 0 -program 0}
set rest [lrange $argv 1 end]
while {[llength $rest]} {
    set k [lindex $rest 0]
    if {$k eq "-program"} { set opt(-program) 1; set rest [lrange $rest 1 end]; continue }
    if {![info exists opt($k)] || [llength $rest] < 2} { usage }
    set opt($k) [lindex $rest 1]
    set rest [lrange $rest 2 end]
}
set board [string tolower $opt(-board)]
if {$board ni {zedboard pynq-z2}} { error "unknown board '$board': use zedboard or pynq-z2" }
if {$opt(-trigger) ni {scl_fall sda_fall mark uart_rx uart_tx now}} { usage }
if {$opt(-mode) ni {changes all}} { usage }

set fpga_dir  [file normalize [file dirname [info script]]]
set build_dir [file join $fpga_dir build $board]
set ltx [expr {$opt(-ltx) ne "" ? [file normalize $opt(-ltx)] : [file join $build_dir referee_$board.ltx]}]
set bit [file join $build_dir referee_$board.bit]
if {![file exists $ltx]} { error "$ltx not found: build with the ILA first (fpga/build.tcl)" }
file mkdir $run_dir

# ---- hardware manager, target, device ----------------------------------------------------
if {[catch {open_hw_manager}]} { open_hw }
connect_hw_server -url $opt(-url)
open_hw_target
if {$opt(-jtag_hz) > 0} {
    set_property PARAM.FREQUENCY $opt(-jtag_hz) [current_hw_target]
    close_hw_target
    open_hw_target
}
set dev [lindex [get_hw_devices xc7z020*] 0]
if {$dev eq ""} { error "no xc7z020 on the JTAG chain: [get_hw_devices]" }
current_hw_device $dev
if {$opt(-program)} {
    set_property PROGRAM.FILE $bit $dev
    set_property PROBES.FILE $ltx $dev
    set_property FULL_PROBES.FILE $ltx $dev
    program_hw_devices $dev
    puts "INFO: programmed $bit (the chip is reset; load the firmware again)"
}
set_property PROBES.FILE $ltx $dev
set_property FULL_PROBES.FILE $ltx $dev
refresh_hw_device $dev

set ila [lindex [get_hw_ilas -of_objects $dev -filter {CELL_NAME =~ "*u_ila*"}] 0]
if {$ila eq ""} { set ila [lindex [get_hw_ilas -of_objects $dev] 0] }
if {$ila eq ""} {
    error "no ILA found: is the ILA bitstream loaded (LD0 blinking), the .ltx from the same build, the\
           JTAG clock low enough (try -jtag_hz 1000000)?"
}

# Probes by their base name (ila_ts[39:0] -> ila_ts), whatever hierarchy prefix Vivado gives them.
proc find_probe {ila base} {
    foreach p [get_hw_probes -of_objects $ila] {
        set n [get_property NAME $p]
        regsub {\[[0-9]+:[0-9]+\]$} $n {} n
        if {[lindex [split $n /] end] eq $base} { return $p }
    }
    error "probe $base not found; the ILA has: [get_hw_probes -of_objects $ila]"
}

# ---- window, trigger, capture control -----------------------------------------------------
set built [get_property CONTROL.DATA_DEPTH $ila]
set depth [expr {$opt(-depth) > 0 ? $opt(-depth) : $built}]
set_property CONTROL.DATA_DEPTH $depth $ila
set_property CONTROL.WINDOW_COUNT 1 $ila
set_property CONTROL.TRIGGER_POSITION $opt(-position) $ila
set_property CONTROL.TRIGGER_MODE BASIC_ONLY $ila
set_property CONTROL.TRIGGER_CONDITION AND $ila
switch -- $opt(-trigger) {
    scl_fall { set_property TRIGGER_COMPARE_VALUE eq1'bF [find_probe $ila ila_scl] }
    sda_fall { set_property TRIGGER_COMPARE_VALUE eq1'bF [find_probe $ila ila_sda] }
    mark     { set_property TRIGGER_COMPARE_VALUE eq1'bR [find_probe $ila ila_mark] }
    uart_rx  { set_property TRIGGER_COMPARE_VALUE eq1'bF [find_probe $ila ila_uart_rx] }
    uart_tx  { set_property TRIGGER_COMPARE_VALUE eq8'bXXXFXXXX [find_probe $ila ila_uo_out] }
    now      { }
}
if {$opt(-mode) eq "changes"} {
    set_property CONTROL.CAPTURE_MODE BASIC $ila
    set_property CONTROL.CAPTURE_CONDITION AND $ila
    set_property CAPTURE_COMPARE_VALUE eq1'b1 [find_probe $ila ila_chg]
} else {
    set_property CONTROL.CAPTURE_MODE ALWAYS $ila
}
# Plain numbers in the CSV for the sample counter (ila_check.py also reads HEX).
catch {set_property DISPLAY_RADIX UNSIGNED [find_probe $ila ila_ts]}

# ---- arm, wait, export ------------------------------------------------------------------
set t0 [clock seconds]
if {$opt(-trigger) eq "now"} {
    run_hw_ila -trigger_now $ila
} else {
    run_hw_ila $ila
}
puts "ARMED: $ila, trigger $opt(-trigger), $opt(-mode) mode, depth $depth (built $built), position $opt(-position)."
puts "ARMED: start the traffic now (e.g. one press of BTNL / BTN2); waiting up to $opt(-timeout) min."
flush stdout
set wait_err ""
if {[catch {wait_on_hw_ila -timeout $opt(-timeout) $ila} e]} { set wait_err $e }
set status [get_property STATUS.CORE_STATUS $ila]
proc write_capture_txt {fn lines} {
    set fh [open $fn w]
    foreach l $lines { puts $fh $l }
    close $fh
}
set base [list "date [clock format $t0 -format {%Y-%m-%d %H:%M:%S} -gmt 1] UTC" "board $board" "ltx $ltx" \
              "ila $ila" "trigger $opt(-trigger)" "mode $opt(-mode)" "depth $depth" "built_depth $built" \
              "position $opt(-position)" "core_status $status" "waited_s [expr {[clock seconds] - $t0}]"]
# A complete capture ends with the core FULL. Anything else (the timeout ran out before the
# trigger or before the buffer filled) is not a capture to check: say so and stop.
if {$wait_err ne "" || $status ne "FULL"} {
    write_capture_txt [file join $run_dir capture.txt] [concat $base [list "error not captured: status $status $wait_err"]]
    error "ILA not captured (status $status$wait_err): nothing exported; see [file join $run_dir capture.txt]"
}
set data [upload_hw_ila_data $ila]
write_hw_ila_data -force -csv_file [file join $run_dir ila.csv] $data
if {[catch {write_hw_ila_data -force [file join $run_dir ila.ila] $data} e]} {
    puts "WARNING: could not write ila.ila: $e"
}
if {[catch {write_hw_ila_data -force -vcd_file [file join $run_dir ila.vcd] $data} e]} {
    puts "WARNING: could not write ila.vcd: $e"
}

write_capture_txt [file join $run_dir capture.txt] $base
puts "INFO: core status $status; wrote [file join $run_dir ila.csv] (and ila.ila, ila.vcd, capture.txt)"
puts "INFO: next: python3 tools/ila_check.py $run_dir"
