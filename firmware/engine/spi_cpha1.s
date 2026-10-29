; SPI controller, modes 1 and 3 (CPHA = 1; CPOL = 0 for mode 1, 1 for mode 3), MSB first,
; full duplex, SCK 1 MHz. Driven by the host through the IN FIFO.
; SCK = output pin 4 (uio[4]), MOSI = pin 5 (uio[5]), CS = pin 6 (uio[6]), all push-pull;
; MISO = input 7 (uio_in[7]).
;
; Each byte to send comes spread over two IN bytes (referee_host.spread: t7 0 t6 0 ... t0 0;
; why: see i2c_controller.s), and the byte received comes back as two OUT bytes
; (referee_host.gather). CS goes low with the first byte and stays low until the host
; sends the end-of-frame marker 0xFF (a spread byte never has bit 6 set).
;
; CPHA = 1: both ends shift on the leading edge (MOSI is set just after it) and sample
; on the trailing edge, half a period later.
; Clock-independent: time literals are converted at the chip's clock (info.yaml).
.equ CPOL 0                      ; idle SCK level (referee_host.py --equ CPOL=1: mode 3)
.equ HALF 500ns                  ; half an SCK period

        set  6, 1                ; CS high (inactive)
        set  4, CPOL             ; SCK idle
frame:  pull hi                  ; next byte (high half), or the end-of-frame marker
        je   wait
        cmp  0xFF, hi
        jc   endf
lo:     pull                     ; low half (the host sends it right after)
        je   lo
        set  6, 0                ; CS low (already low after the first byte)
        mark
        waitd HALF               ; CS setup (and a gap between bytes)
        ldrc 7
bit:    set  4, 1-CPOL           ; leading edge: both ends shift
        sho  5, msb              ; MOSI <- next bit
        waitd HALF
        set  4, CPOL             ; trailing edge: both ends sample
        shi  7, msb              ; MISO (synchronized: its value just before the edge)
        waitd HALF
        loop bit
        push hi                  ; the byte received, spread: 1 r7 1 r6 ... 1 r0
        push
        jmp  frame
endf:   mark
        waitd HALF               ; CS hold after the last edge
        set  6, 1
        jmp  frame
wait:   mark                     ; nothing to send: look again
        waitd 2us
        jmp  frame
