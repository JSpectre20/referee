#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Local checks of the FPGA port that need no Vivado. Run from the repo root:
#   fpga/sim/run.sh            lint (Verilator) + simulation (Icarus) + the ILA check
#                              of the simulated capture (tools/ila_check.py), with a
#                              negative control that must fail
#   fpga/sim/run.sh yosys      also the Yosys synth_xilinx estimate (without the ILA IP)
set -euo pipefail
MODE="${1:-}"
cd "$(dirname "$0")/../.."
mkdir -p fpga/sim/out
SRCS=$(sed -n '/source_files:/,/^$/p' info.yaml | grep -oE '"[^"]+\.v"' | tr -d '"' | sed 's|^|src/|')

echo "== lint (verilator -Wall), ZedBoard and PYNQ-Z2 generics"
for g in "8 8 5" "6 2 4"; do
  set -- $g
  verilator --lint-only -Wall -Wno-DECLFILENAME --top-module fpga_top \
    fpga/rtl/fpga_top.v fpga/rtl/ila_none.v fpga/sim/xilinx_prims.v fpga/sim/zynq_ps_stub.v $SRCS \
    -GN_LED=$1 -GN_SW=$2 -GN_BTN=$3
  echo "lint N_LED=$1 N_SW=$2 N_BTN=$3: clean"
done

echo "== simulation (Icarus)"
python3 fpga/sim/gen_packets.py fpga/sim/out/packets.hex
mkdir -p fpga/sim/out/ila_run
rm -f fpga/sim/out/ila_run/ila.csv fpga/sim/out/ila_run/verdicts.jsonl
iverilog -g2012 -o fpga/sim/out/tb_fpga.vvp -s tb_fpga \
  fpga/sim/tb_fpga.v fpga/sim/zynq_ps_model.v fpga/sim/ila_model.v fpga/sim/xilinx_prims.v \
  fpga/rtl/fpga_top.v $SRCS
vvp -n fpga/sim/out/tb_fpga.vvp ${VCD:++vcd} | tee fpga/sim/out/sim.log
grep -q "^PASS: all checks" fpga/sim/out/sim.log

echo "== ILA check of the simulated capture (tools/ila_check.py)"
# The simulated ILA clock is 1 / 4.167 ns; the simulated chip clock 1 / 16.667 ns (20 ppm
# from 60 MHz). The pads are ideal in simulation: skew 0.5 ns is already generous.
cat > fpga/sim/out/ila_run/run.json <<'JSON'
{
  "board": "simulation (fpga/sim/tb_fpga.v)",
  "ila_clock_hz": 239980801.536,
  "chip_clock_hz": 60000000,
  "skew_ns": 0.5, "aperture_ns": 0.25, "ppm": 50,
  "firmware": "firmware/engine/i2c_setup_referee.s", "image_crc16": "4622",
  "push": "nonpass",
  "rule": {"name": "tSU;DAT", "trigger": "scl", "trigger_edge": "rise", "ref": "sda",
           "ref_edge": "last", "kind": "min", "bound_ns": 100, "slot": 0},
  "files": {"csv": "ila.csv", "verdicts": "verdicts.jsonl"}
}
JSON
python3 tools/ila_check.py fpga/sim/out/ila_run --brief | tee fpga/sim/out/ila_check.log
tail -1 fpga/sim/out/ila_check.log | grep -qx PASSED
echo "== negative control: the same capture against a 30 ns bound must FAIL"
if python3 tools/ila_check.py fpga/sim/out/ila_run --brief --bound-ns 30 > fpga/sim/out/ila_check_neg.log; then
  echo "FAIL: the negative control passed"; exit 1
fi
grep -E "^SUMMARY|^FAILED" fpga/sim/out/ila_check_neg.log

if [ "$MODE" = "yosys" ]; then
  echo "== yosys synth_xilinx (ZedBoard generics)"
  yosys -q -p "read_verilog -lib fpga/sim/zynq_ps_stub.v; read_verilog $(echo $SRCS) fpga/rtl/ila_none.v fpga/rtl/fpga_top.v; \
    chparam -set N_LED 8 -set N_SW 8 -set N_BTN 5 fpga_top; synth_xilinx -family xc7 -top fpga_top -flatten; \
    tee -o fpga/sim/out/yosys_stat.txt stat"
  grep -E '^\s+[0-9]+\s+(LUT|FD|CARRY|RAM|DSP|MUXF|SRL)' fpga/sim/out/yosys_stat.txt
fi
