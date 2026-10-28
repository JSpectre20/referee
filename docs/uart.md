# UART and host port

`src/uart_tx.v`, `src/uart_rx.v` and `src/host_port.v` are the chip's link to its host. In
the top module (`src/project.v`) they load and run `seq` (see `docs/seq.md`). The host side
is `tools/referee_host.py`, and the datasheet (`docs/info.md`) lists the commands.

## Transmitter (`uart_tx`)

- 8N1, LSB first, `div` cycles per bit: a runtime input (the host port's 'B' command sets it
  between frames). Power-up value 521 = 115200 baud at 60 MHz; 20 = 3 Mbaud. The formal
  proofs fix `div` = `CLKS_PER_BIT` (they hold for any one value).
- Pulse `start` while `busy` is low; `data` is copied in that cycle. A `start` while busy
  is ignored, not queued.
- Contract: a start accepted in cycle t puts the frame on `tx` in cycles
  t+1 .. t+10*CLKS_PER_BIT, and `busy` is high exactly in those cycles.
- `tx` is a flip-flop (a glitch on an idle line looks like a start bit) and is 1 during and
  after reset.

## Receiver (`uart_rx`)

- 2-flop synchronizer, then a majority vote of three consecutive samples centred on the
  middle of each bit, so a one-cycle spike can't flip a bit.
- Start-bit check at mid-bit: a low pulse shorter than about half a bit is a glitch and is
  ignored.
- A low stop bit pulses `frame_err` and drops the byte; the receiver then waits for the line
  to go high, so a long break is reported once.
- It returns to idle in the middle of the stop bit, so a slightly fast sender is fine.

## Host port (`host_port`)

4-byte commands `CMD A B C`, 2-byte replies `STATUS DATA` (see the table in `docs/info.md`).
Program and shift-register writes are refused (NAK) while the sequencer runs. A partial
packet is dropped after 2^17 cycles (3.3 ms) of silence, or on a framing error, so a host
that lost count recovers by pausing. A packet that arrives while a reply is still going out is
dropped and flagged as `overrun` in the status byte.

## Verification

```bash
cd test && pytest uart -v          # unit tests, at 16 and at 347 cycles per bit
cd test && make                    # chip-level tests through the pins (also run at gate level)
cd formal && sby -f uart.sby       # proofs, covers, loopback, negative control
```

**Unit tests** (`test/uart`, cocotb, each run at 16 and at 347 cycles per bit):
- TX decoded by `cocotbext-uart`: all 256 byte values back to back at 16 cycles per bit, and
  edge values plus random bytes at 347.
- TX frame checked cycle by cycle (exactly 10 x CLKS cycles); `busy` timing; a start while
  busy is ignored.
- RX fed by `cocotbext-uart`: same byte sets, then a sender 3% fast and 3% slow.
- RX rejects glitches, outvotes a one-cycle spike at the middle of every bit, and reports
  one framing error for a bad stop bit and one for a long break.
- TX looped back into RX: 200 random bytes.

**Formal** (`formal/uart.sby`):
- `tx_prove`: unbounded proof, by k-induction, that every frame has the right bit in every
  cycle, `busy` is high for exactly 10 x CLKS cycles, and the idle line is high.
- `tx_cover`: a full frame, back-to-back frames, and a start while busy are all reachable.
- `tx_mutant`: a stop bit one cycle short. It must fail, and it does.
- `loopback` (bounded, 140 cycles, about two frames at 6 cycles per bit): for any bytes
  written at any time, the receiver reports exactly the bytes sent, in order, never a framing
  error, and none are lost.
- `lb_cover`: two back-to-back bytes make it through.

**Chip level** (`test/test.py`, through the real pins, at a real 115200 baud, RTL and gate
level):
- reset state;
- identify;
- the full UART firmware loaded over the UART, run, and its byte decoded on `uio[0]`;
- writes refused while running;
- reload after a halt;
- bad commands NAKed;
- resync after a partial packet (RTL only: it is a 3.5 ms wait).
