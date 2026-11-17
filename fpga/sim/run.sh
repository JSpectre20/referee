#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Local checks of the FPGA port that need no Vivado. Run from the repo root:
#   fpga/sim/run.sh            lint (Verilator) + simulation (Icarus)
#   fpga/sim/run.sh yosys      also the Yosys synth_xilinx estimate
set -euo pipefail
MODE="${1:-}"
cd "$(dirname "$0")/../.."
mkdir -p fpga/sim/out
SRCS=$(sed -n '/source_files:/,/^$/p' info.yaml | grep -oE '"[^"]+\.v"' | tr -d '"' | sed 's|^|src/|')

echo "== lint (verilator -Wall), ZedBoard and PYNQ-Z2 generics"
for g in "8 8 5" "6 2 4"; do
  set -- $g
  verilator --lint-only -Wall -Wno-DECLFILENAME --top-module fpga_top \
    fpga/rtl/fpga_top.v fpga/sim/xilinx_prims.v fpga/sim/zynq_ps_stub.v $SRCS \
    -GN_LED=$1 -GN_SW=$2 -GN_BTN=$3
  echo "lint N_LED=$1 N_SW=$2 N_BTN=$3: clean"
done

echo "== simulation (Icarus)"
python3 fpga/sim/gen_packets.py fpga/sim/out/packets.hex
iverilog -g2012 -o fpga/sim/out/tb_fpga.vvp -s tb_fpga \
  fpga/sim/tb_fpga.v fpga/sim/zynq_ps_model.v fpga/sim/xilinx_prims.v fpga/rtl/fpga_top.v $SRCS
vvp -n fpga/sim/out/tb_fpga.vvp ${VCD:++vcd} | tee fpga/sim/out/sim.log
grep -q "^PASS: all checks" fpga/sim/out/sim.log

if [ "$MODE" = "yosys" ]; then
  echo "== yosys synth_xilinx (ZedBoard generics)"
  yosys -q -p "read_verilog -lib fpga/sim/zynq_ps_stub.v; read_verilog $(echo $SRCS) fpga/rtl/fpga_top.v; \
    chparam -set N_LED 8 -set N_SW 8 -set N_BTN 5 fpga_top; synth_xilinx -family xc7 -top fpga_top -flatten; \
    tee -o fpga/sim/out/yosys_stat.txt stat"
  grep -E '^\s+[0-9]+\s+(LUT|FD|CARRY|RAM|DSP|MUXF|SRL)' fpga/sim/out/yosys_stat.txt
fi
