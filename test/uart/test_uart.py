# SPDX-License-Identifier: Apache-2.0
"""pytest entry point for the UART's cocotb tests, at two baud settings.

    cd test && pytest uart -v          (WAVES=1 to dump waveforms)
"""

import os
from pathlib import Path

import pytest
from cocotb_tools.check_results import get_results
from cocotb_tools.runner import get_runner

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent / "src"


@pytest.mark.parametrize("clks", [16, 347])
def test_uart(clks):
    waves = bool(os.getenv("WAVES"))
    build_dir = HERE / "sim_build" / f"clks{clks}"
    runner = get_runner(os.getenv("SIM", "icarus"))
    runner.build(
        sources=[SRC / "uart_tx.v", SRC / "uart_rx.v", HERE / "hdl" / "uart_tb.v"],
        hdl_toplevel="uart_tb",
        build_dir=build_dir,
        parameters={"CLKS": clks},
        timescale=("1ns", "1ps"),
        always=True,
        waves=waves,
    )
    results = runner.test(
        hdl_toplevel="uart_tb",
        test_module="tb_uart",
        test_dir=HERE,
        build_dir=build_dir,
        extra_env={"CLKS": str(clks)},
        results_xml=str(build_dir / "results.xml"),
        waves=waves,
    )
    _, failed = get_results(results)
    assert failed == 0, f"{failed} cocotb test(s) failed, see {results}"
