# SPDX-License-Identifier: Apache-2.0
"""pytest entry point for the engine lockstep tests.

    cd test && pytest engine -v          (WAVES=1 to dump waveforms; SEEDS=n for more programs)
"""

import os
from pathlib import Path

from cocotb_tools.check_results import get_results
from cocotb_tools.runner import get_runner

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent / "src"


def test_engine():
    waves = bool(os.getenv("WAVES"))
    build_dir = HERE / "sim_build"
    runner = get_runner(os.getenv("SIM", "icarus"))
    runner.build(
        sources=[SRC / f for f in ("edge_ts.v", "verdict.v", "engine.v", "engine_sys.v")] + [HERE / "hdl" / "engine_tb.v"],
        hdl_toplevel="engine_tb",
        build_dir=build_dir,
        timescale=("1ns", "1ps"),
        always=True,
        waves=waves,
    )
    results = runner.test(
        hdl_toplevel="engine_tb", test_module="tb_engine", test_dir=HERE, build_dir=build_dir,
        results_xml=str(build_dir / "results.xml"), waves=waves,
    )
    _, failed = get_results(results)
    assert failed == 0, f"{failed} cocotb test(s) failed, see {results}"
