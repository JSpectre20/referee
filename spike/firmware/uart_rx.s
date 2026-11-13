; UART receiver, 8N1, LSB first, on engine input 1 (uio_in[1]); every good byte goes to the
; OUT FIFO for the host; a byte whose stop bit is low (framing error) is dropped.
; Port of firmware/engine/uart_rx.s to the ISA v1 spike: the same instructions, word for word.
; v1 candidates used: none. (With A, `shi` shifts SRI and `push hi` reads SRI.)
;
; Timing: the start bit's falling edge is seen by `waitp` 2 cycles after it reaches the pad
; (2-flop synchronizer); `mark` puts the schedule there, so each bit is sampled HALF + k * BIT
; later: near its middle, the error staying well under a quarter bit for the whole frame.
; Clock-independent: time literals and BIT are converted to cycles at the image's clock.
.equ BIT 1s/115200
.equ HALF 1s/115200/2

idle:   tmon 1023, x16           ; 273 us without a start bit: just look again
        waitp 1, 0               ; start bit (line goes low)
        jt   idle
        mark                     ; the frame's schedule starts here
        waitd HALF               ; middle of the start bit
        tstp 1
        jc   idle                ; high again: a glitch, not a start bit
        ldrc 7                   ; 8 data bits
bit:    waitd BIT                ; middle of the next data bit
        shi  1                   ; SRI <- {in, SRI[15:1]}: after 8, the byte is SRI[15:8]
        loop bit
        waitd BIT                ; middle of the stop bit
        tstp 1
        jnc  idle                ; stop bit low: framing error, drop the byte
        push hi                  ; the byte -> OUT
        jmp  idle
