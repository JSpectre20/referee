# SPDX-License-Identifier: Apache-2.0
"""Host side of the Referee UART console.

Shared by the cocotb tests (test/test.py) and the serial tool (host/referee.py),
so the same transcripts check the simulation, an FPGA and the real chip.

Pure Python: no dependencies here. pyserial is only needed by SerialConsole.

Transcript format (test/transcripts/*.txt):

    # comment
    > r 00          a line to type (sent followed by CR)
    < 52            the reply body expected for it ("<" alone = no body)

Every "> " line must be followed by exactly one "<" line.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

PROMPT = b"\r\n> "


class ConsoleError(Exception):
    pass


@dataclass
class Step:
    line: str  # what to type
    expect: str  # expected reply body
    where: str  # file:line, for error messages


def parse_transcript(path: str | Path) -> list[Step]:
    path = Path(path)
    steps: list[Step] = []
    pending: tuple[str, str] | None = None
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        where = f"{path.name}:{n}"
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith(">"):
            if pending:
                raise ValueError(f"{pending[1]}: command has no '<' line after it")
            pending = (raw[2:] if raw.startswith("> ") else raw[1:], where)
        elif raw.startswith("<"):
            if not pending:
                raise ValueError(f"{where}: '<' line without a command before it")
            expect = raw[2:] if raw.startswith("< ") else raw[1:]
            steps.append(Step(pending[0], expect.strip(), pending[1]))
            pending = None
        else:
            raise ValueError(f"{where}: lines must start with '>', '<' or '#'")
    if pending:
        raise ValueError(f"{pending[1]}: command has no '<' line after it")
    return steps


def expected_echo(line: str) -> bytes:
    """The chip echoes printable characters only."""
    return bytes(b for b in line.encode() if 0x20 <= b <= 0x7E)


def split_reply(echo: bytes, raw: bytes) -> str:
    """Check the echo and framing of one reply, and return its body."""
    if not raw.startswith(echo):
        raise ConsoleError(f"echo mismatch: expected {echo!r}, got {raw!r}")
    rest = raw[len(echo) :]
    if rest == PROMPT:
        return ""
    if not (rest.startswith(b"\r\n") and rest.endswith(PROMPT) and len(rest) > 2 + len(PROMPT)):
        raise ConsoleError(f"bad reply framing: {raw!r}")
    return rest[2 : -len(PROMPT)].decode(errors="replace")


class SerialConsole:
    """Talks to the chip over a serial port (USB-UART, FPGA, or a simulator pty)."""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 2.0):
        import serial  # pyserial

        self.ser = serial.Serial(port, baud, timeout=timeout)
        self.timeout = timeout

    def close(self):
        self.ser.close()

    def _read_until(self, suffix: bytes) -> bytes:
        buf = bytearray()
        deadline = time.monotonic() + self.timeout
        while not buf.endswith(suffix):
            if time.monotonic() > deadline:
                raise ConsoleError(f"timeout waiting for {suffix!r}, got {bytes(buf)!r}")
            buf += self.ser.read(1)
        return bytes(buf)

    def sync(self):
        """Get to a clean prompt, whatever state the line was in."""
        self.ser.write(b"\x03")  # Ctrl-C discards any half-typed line
        self._read_until(PROMPT)
        time.sleep(0.05)
        self.ser.reset_input_buffer()

    def cmd(self, line: str) -> str:
        echo = expected_echo(line)
        self.ser.write(line.encode() + b"\r")
        raw = bytes(self.ser.read(len(echo))) if echo else b""
        raw += self._read_until(PROMPT)
        return split_reply(echo, raw)

    def read(self, addr: int) -> int:
        body = self.cmd(f"r {addr:02x}")
        try:
            return int(body, 16)
        except ValueError:
            raise ConsoleError(f"read {addr:02x}: unexpected reply {body!r}") from None

    def write(self, addr: int, data: int):
        body = self.cmd(f"w {addr:02x} {data:02x}")
        if body != "ok":
            raise ConsoleError(f"write {addr:02x}: unexpected reply {body!r}")
