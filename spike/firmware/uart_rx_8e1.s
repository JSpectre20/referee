; UART receiver, 8E1 (8 data bits LSB first, even parity, 1 stop bit), on engine input 1 (uio_in[1]).
;
; Host protocol (OUT FIFO), for every frame: the data byte D as received, then its status:
;   D FF         good byte
;   D 00 FF      parity error (the stop bit was good)
;   D 00 00      framing error: the stop bit was low (whatever the parity)
; So after D: FF means good; 00 means bad, and the next byte says why (FF parity, 00 framing).
; A bad byte is never followed by FF alone, so the host cannot take it for a good one. A frame
; puts at most 3 bytes in OUT (depth 4): the host must pop them before the next frame ends.
;
; Why the status takes this shape: with candidate A, `push` reads SRI, and only `shi` writes SRI.
; There is no way to push a constant (in v0.3: `ldsr 0xFF, hi; push hi`). So the constants are
; sampled: 00 is 8 samples of the start bit (all low, or it is no start bit) that the data bits
; then shift into SRI[7:0]; FF is 8 consecutive high samples of the line after the frame.
;
; v1 candidates used: A (the 16 bits of SRI hold D and the 00 together), C2 (parity): POLY = 0x8000
; makes L a parity register; with the tap on the bits `shi` samples, after the 8 data bits
; L == 0 (flag Z) exactly when they have even parity. The parity bit itself is read with `tstp`,
; not shifted in: shifting it would push the 00 out of SRI. So the check is "parity bit == !Z".
;
; Timing: as uart_rx.s; the schedule starts where `waitp` sees the start bit, every bit is read
; at its middle (HALF + k * BIT). The 8 start-bit samples and the 8 high samples take 24 cycles
; each (5% of a bit at 115200 baud). Frames back to back need BIT >= 70 cycles (tested on the
; model at 70: 857 kbaud at 60 MHz; at 60 the status work overruns the stop bit). A frame with a
; framing error needs idle time after it: the receiver waits for the line to go high.
; Clock-independent: time literals and BIT are converted to cycles at the image's clock.
.equ BIT 1s/115200
.equ HALF 1s/115200/2

        crcp 0x80, hi            ; POLY = 0x8000: L[15] is the running parity of the tapped bits
idle:   tmon 1023, x16           ; 273 us without a start bit: just look again
        waitp 1, 0               ; start bit (line goes low)
        jt   idle
        mark                     ; the frame's schedule starts here
        waitd HALF               ; middle of the start bit
        ldrc 7
st:     shi  1                   ; 8 samples of the start bit: SRI[15:8] = 0x00 ...
        jc   idle                ; ... or it was a glitch, not a start bit
        loop st
        bsc  shi, zeros          ; L <- 0; L follows the bits shi samples from now on
        ldrc 7                   ; 8 data bits
bit:    waitd BIT                ; middle of the next data bit
        shi  1                   ; SRI <- {in, SRI[15:1]}: after 8, SRI = {D, 0x00}
        loop bit
        push hi                  ; D -> OUT; its status follows
        waitd BIT                ; middle of the parity bit
        tstp 1                   ; C <- the parity bit; Z: the data bits have even parity
        jc   p1
        jz   good                ; parity bit 0, data even: good
        jmp  bad
p1:     jnz  good                ; parity bit 1, data odd: good
bad:    push                     ; 00 (SRI[7:0]): bad
        waitd BIT                ; middle of the stop bit
        tstp 1
        jc   ff                  ; parity error: 00 FF
        jmp  ferr                ; framing error: 00 00 (one 00 is out already)
good:   waitd BIT                ; middle of the stop bit
        tstp 1
        jc   ff                  ; good: FF
        push                     ; framing error: 00 00
ferr:   push
high:   tmon 1023, x16           ; wait for the line to go high again, so the low stop bit is
        waitp 1, 1               ; not taken for the next start bit
        jt   high
        jmp  idle
ff:     ldrc 7                   ; FF: 8 consecutive high samples (the line is in its stop bit)
f1:     shi  1
        jnc  ff                  ; a low sample: start the 8 again
        loop f1
        push hi                  ; FF
        jmp  idle
