#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Write the host packets the FPGA simulation sends, one 4-byte packet per line (hex).

They come from tools/referee_host.py, the same code that talks to the board:
  1. identify                        49 00 00 00   -> expect 06 52
  2. load firmware/engine/i2c_setup_referee.s (halt, global config, image, CRCs)
  3. run                             52 00 01 00   -> expect ACK

    python3 fpga/sim/gen_packets.py fpga/sim/out/packets.hex
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import referee_host as host  # noqa: E402

glob, img, notes = host.build((ROOT / "firmware/engine/i2c_setup_referee.s").read_text())
pkts = [host.pkt_id()] + host.load_packets(glob, img) + [host.pkt_run(True)]
out = Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text("".join(p.hex() + "\n" for p in pkts))
print(f"{len(pkts)} packets -> {out} ({'; '.join(notes)})")
