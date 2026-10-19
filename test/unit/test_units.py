# SPDX-License-Identifier: Apache-2.0
"""pytest entry point for the module-level cocotb tests.

    cd test && pytest unit -v          (WAVES=1 to dump waveforms)
"""

import os
from pathlib import Path

import pytest
from cocotb_tools.runner import get_runner

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent / "src"


@pytest.mark.parametrize("top", ["uart_rx", "uart_tx"])
def test_module(top):
    waves = bool(os.getenv("WAVES"))
    build_dir = HERE / "sim_build" / top
    runner = get_runner(os.getenv("SIM", "icarus"))
    runner.build(
        sources=[SRC / f"{top}.v", HERE / "hdl" / f"{top}_tb.v"],
        hdl_toplevel=f"{top}_tb",
        build_dir=build_dir,
        timescale=("1ns", "1ps"),
        always=True,
        waves=waves,
    )
    runner.test(
        hdl_toplevel=f"{top}_tb",
        test_module=f"tb_{top}",
        test_dir=HERE,
        build_dir=build_dir,
        waves=waves,
    )
