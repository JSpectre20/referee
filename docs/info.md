<!---
This file is used to generate your project datasheet.
-->

## How it works

Referee is a programmable protocol emulator and bus referee, built for the Jane Street
protocol emulator ASIC competition. Protocols are programs, not fixed logic.

This build contains the first pieces:

- **seq**, a four-instruction pin sequencer (`set`, `wait`, `shout`, `jump`) with a
  32-word program memory. Every instruction takes exactly one cycle, `wait n` takes exactly
  n cycles, and a `set` or `shout` is visible on its pin the next cycle. A UART transmitter, for
  example, is a 21-word program (`firmware/seq/uart_tx.s`).
- **A UART host port** (115200 baud, 8N1, at the 40 MHz project clock) on the standard
  Tiny Tapeout UART pins. The host loads programs, sets the sequencer's shift register,
  enables output pins and starts or halts the sequencer with 4-byte commands. Each command
  gets a 2-byte reply: ACK (0x06) or NAK (0x15), then a data byte.

| Command | Bytes | Effect |
| --- | --- | --- |
| Identify | `49 00 00 00` | reply `06 52` |
| Write program word | `57 addr hi lo` | only while halted; addr 0..31 |
| Set shift register | `53 byte 00 00` | only while halted |
| Run / halt | `52 01 00 00` / `52 00 00 00` | halting puts pc back to 0 |
| Output enable | `4F mask 00 00` | bit n = 1 makes uio[n] an output |
| Status | `51 00 00 00` | data = {running, overrun, 0, pc[4:0]} |

The sequencer's pins drive `uio[7:0]`; all uio pins are inputs until the host enables them.
`uo_out[0]` is high while the sequencer runs.

## How to test

Set the project clock to 40 MHz and connect a 115200-baud serial port (3.3 V) to
`ui_in[3]` (chip RX) and `uo_out[4]` (chip TX); the demo board's microcontroller can act as
the host. Then, from a PC:

```bash
python3 tools/referee_host.py --port /dev/ttyUSB0 id
python3 tools/referee_host.py --port /dev/ttyUSB0 load firmware/seq/uart_tx.s --sr 0x41 --oe 0x01 --run
```

The second command loads the UART-transmitter program, puts `0x41` ('A') in the shift
register, makes `uio[0]` an output and starts the sequencer: `uio[0]` sends one 'A' at
115200 baud. Halt, set a new byte and run again to send another.

## External hardware

A 3.3 V USB-serial adapter (or the demo board's RP2350 as host) for the command port;
optionally a second one on `uio[0]` to watch what the program sends.
