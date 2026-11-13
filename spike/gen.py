#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Generate spike/isa_tables_v1.py from spike/isa_v1.yaml with the project's own generator
(tools/isagen.py), so the spike's assembler and model use tables built the same way as v0.3's.

    python3 spike/gen.py            write
    python3 spike/gen.py --check    fail if the file is out of date
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
import isagen  # noqa: E402

OUT = HERE / "isa_tables_v1.py"


def build():
    isagen.ISA = HERE / "isa_v1.yaml"
    d, fields, reserved, instrs = isagen.load()
    text = isagen.gen_python(d, fields, reserved, instrs)
    return text.replace("(see isa.yaml)", "(see spike/isa_v1.yaml)").replace(
        "from isa.yaml", "from spike/isa_v1.yaml by spike/gen.py")


if __name__ == "__main__":
    text = build()
    if "--check" in sys.argv:
        sys.exit(0 if OUT.exists() and OUT.read_text() == text else f"{OUT} is out of date")
    OUT.write_text(text)
    print(f"wrote {OUT}")
