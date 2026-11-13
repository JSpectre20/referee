; UART TX, 8N1, drift-free, with CTS flow control (docs/isa-timing.md section 5.1).
; Port of firmware/engine/uart_tx.s to the ISA v1 spike: the same instructions, word for word.
; Bytes come from the IN FIFO; TX is output pin 0 (uio[0]); CTS is input 8 (ui_in[0], active low).
; v1 candidates used: none. (With A, `pull` loads SRO and `sho` shifts SRO: the v0.3 SR's
; output half; this program never reads anything back.)
; Clock-independent: time literals and BIT are converted to cycles at the image's clock.
.equ BIT 1s/115200           ; 115200 baud: 521 cycles at 60 MHz

start:  pull                 ; non-blocking
        je   idle
cts:    tmon 250us           ; CTS timeout
        waitp 8, 0           ; CTS low ...
        jt   cts             ; ... timed out: retry; the byte stays in SRO
        mark                 ; the frame's schedule starts here
        set  0, 0            ; start bit
        waitd BIT
        ldrc 7
bit:    sho  0               ; data bits, LSB first
        waitd BIT
        loop bit
        set  0, 1            ; stop bit
        waitd BIT
        jmp  start
idle:   mark                 ; nothing to send: poll every 10 us
        waitd 10us
        jmp  start
