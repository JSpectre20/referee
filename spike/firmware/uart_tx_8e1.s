; UART TX, 8E1 (8 data bits LSB first, even parity, 1 stop bit), drift-free, with CTS flow control.
; Same pins and host protocol as uart_tx.s: bytes come from the IN FIFO (one per frame); TX is
; output pin 0 (uio[0]); CTS is input 8 (ui_in[0], active low).
;
; v1 candidate used: C2 (the CRC register as a parity generator). POLY = 0x8000 makes L a parity
; register: with the tap on the bits `sho` sends, each data bit b does L[15] <- L[15] ^ b, so after
; the 8 data bits L[15] is their XOR, which is the even-parity bit; `sho 0, crc` sends it.
; Without C2 the parity bit would need the byte's bits tested one by one (v0.3 cannot branch on
; SR's bits at all), which is why 8E1 did not exist in v0.3.
; Clock-independent: time literals and BIT are converted to cycles at the image's clock.
.equ BIT 1s/115200           ; 115200 baud: 521 cycles at 60 MHz

        crcp 0x80, hi        ; POLY = 0x8000: L is a 1-bit parity register (in L[15])
start:  pull                 ; non-blocking
        je   idle
cts:    tmon 250us           ; CTS timeout
        waitp 8, 0           ; CTS low ...
        jt   cts             ; ... timed out: retry; the byte stays in SRO
        mark                 ; the frame's schedule starts here
        set  0, 0            ; start bit
        bsc  sho, zeros      ; L <- 0; from now on L follows every bit sho sends
        waitd BIT
        ldrc 7
bit:    sho  0               ; data bits, LSB first
        waitd BIT
        loop bit
        sho  0, crc          ; parity bit = L[15] = XOR of the 8 data bits
        waitd BIT
        set  0, 1            ; stop bit
        waitd BIT
        jmp  start
idle:   mark                 ; nothing to send: poll every 10 us
        waitd 10us
        jmp  start
