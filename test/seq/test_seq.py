# SPDX-License-Identifier: Apache-2.0
"""pytest entry point for the sequencer's cocotb tests.

    cd test && pytest seq -v          (WAVES=1 to dump waveforms)
"""

import os
from pathlib import Path

from cocotb_tools.runner import get_runner

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent / "src"


def test_seq():
    waves = bool(os.getenv("WAVES"))
    build_dir = HERE / "sim_build"
    runner = get_runner(os.getenv("SIM", "icarus"))
    runner.build(
        sources=[SRC / "seq.v", HERE / "hdl" / "seq_tb.v"],
        hdl_toplevel="seq_tb",
        build_dir=build_dir,
        timescale=("1ns", "1ps"),
        always=True,
        waves=waves,
    )
    results = runner.test(
        hdl_toplevel="seq_tb",
        test_module="tb_seq",
        test_dir=HERE,
        build_dir=build_dir,
        waves=waves,
    )
    # runner.test() does not raise on a failing cocotb test; check the XML.
    from cocotb_tools.check_results import get_results

    _, failed = get_results(results)
    assert failed == 0, f"{failed} cocotb test(s) failed, see {results}"
