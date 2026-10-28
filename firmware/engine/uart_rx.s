; UART receiver, 8N1, LSB first, on engine input 1 (uio_in[1]); every good byte
; goes to the OUT FIFO for the host. 115200 baud at 60 MHz: BIT = 520.8 -> 521.
;
; Timing: the start bit's falling edge is seen by `waitp` 1 - 2 cycles after it
; happens (2-flop synchronizer); `mark` puts the schedule there, so each data
; bit is sampled about half a bit (HALF) + k * BIT later: near its middle, the
; error staying well under a quarter bit for the whole frame.
.clock_hz 60e6
.equ BIT 521
.equ HALF 260

idle:   tmon 1023, x16           ; 273 us without a start bit: just look again
        waitp 1, 0               ; start bit (line goes low)
        jt   idle
        mark                     ; the frame's schedule starts here
        waitd HALF               ; middle of the start bit
        tstp 1
        jc   idle                ; high again: a glitch, not a start bit
        ldrc 7                   ; 8 data bits
bit:    waitd BIT                ; middle of the next data bit
        shi  1                   ; SR <- {in, SR[15:1]}: after 8, the byte is SR[15:8]
        loop bit
        waitd BIT                ; middle of the stop bit
        tstp 1
        jnc  idle                ; stop bit low: framing error, drop the byte
        push hi                  ; the byte -> OUT
        jmp  idle
