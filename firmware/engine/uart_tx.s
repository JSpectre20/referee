; UART TX, 8N1, drift-free, with CTS flow control (docs/isa-timing.md section 5.1).
; Bytes come from the IN FIFO; TX is output pin 0 (uio[0]); CTS is input 8 (ui_in[0], active low).
.clock_hz 60e6
.equ BIT 521                 ; 115200 baud at 60 MHz (8.68 us; 0.03% slow)

start:  pull                 ; non-blocking
        je   idle
cts:    tmon 250us           ; CTS timeout
        waitp 8, 0           ; CTS low ...
        jt   cts             ; ... timed out: retry; the byte stays in SR
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
