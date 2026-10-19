<!---
This file is used to generate your project datasheet.
-->

## How it works

Referee is a programmable protocol emulator (work in progress). The end goal is
a tiny CPU whose instructions read pins, write pins, count cycles and hit
precise timing, so protocols like UART, SPI and I2C run as firmware instead of
fixed logic.

This first stage is the **debug console**: a UART text interface that reads
and writes a small set of registers, including the general-purpose `uio` pins.
Later stages will load programs and control the CPU through the same registers.

- UART: 8N1, 115200 baud at the 40 MHz project clock (divisor 347), adjustable
  at runtime.
- RX is on `ui_in[3]` and TX on `uo_out[4]`, the pins the demo board's RP2040
  bridges to USB serial.

### Console commands

Type a command and press Enter. Hex digits are case-insensitive, 1-2 digits per number.

| Command   | Reply | Meaning                     |
|-----------|-------|-----------------------------|
| `r AA`    | `dd`  | read register `AA`          |
| `w AA DD` | `ok`  | write `DD` to register `AA` |
| other     | `?`   | not understood, nothing changed |

- Characters are echoed as you type. Enter (CR) runs the line; LF is ignored.
- Backspace, DEL, Ctrl-C or ESC discard the line.
- Each reply ends with the prompt `> `. Wait for it before sending the next line;
  bytes that arrive while a reply is being sent are dropped (STATUS bit 0).

### Registers

| Addr | Name     | Access | Description |
|------|----------|--------|-------------|
| `00` | ID       | R      | `52` ("R") |
| `01` | VERSION  | R      | `01` |
| `02` | SCRATCH  | RW     | free for testing |
| `03` | STATUS   | W1C    | bit 0: byte dropped (overrun); bit 1: UART framing error. Write 1 to clear. |
| `04` | DIV_LO   | RW     | UART bit period in clock cycles, low byte |
| `05` | DIV_HI   | RW     | high byte. Writing it applies the new divisor right after the `ok` reply. Minimum 4. |
| `10` | GPIO_OUT | RW     | value driven on `uio` |
| `11` | GPIO_OE  | RW     | `uio` output enables (1 = output). All inputs after reset. |
| `12` | GPIO_IN  | R      | `uio` pin levels |
| `13` | UI_IN    | R      | `ui_in` pin levels (bit 3 is UART RX) |
| `14` | UO_OUT   | RW     | value driven on `uo_out`, except bit 4 (UART TX) |

Unmapped addresses read `00` and ignore writes.

To change the baud rate, write `DIV_LO` then `DIV_HI`; the `ok` for `DIV_HI`
still comes at the old rate, then switch your terminal. Divisor = clock / baud,
for example `w 04 28` and `w 05 00` give 1 Mbaud at 40 MHz. Reset restores 115200.

## How to test

1. Select the project, set the clock to 40 MHz and reset it.
2. Open the RP2040's USB serial port at 115200 8N1 (for example
   `picocom -b 115200 /dev/ttyACM0`). Press Enter and you should see `> `.
3. `r 00` should reply `52`.
4. `w 11 ff` then `w 10 a5` drives `a5` onto the `uio` pins.

Or run the scripted checks: `python host/referee.py --port /dev/ttyACM0 run test/transcripts/*.txt`.

If the chip runs at a different clock, the baud rate scales with it (clock / 347).

## External hardware

None needed. A logic analyzer or LEDs on the `uio` Pmod header show the GPIO writes.
