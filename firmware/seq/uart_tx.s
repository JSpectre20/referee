; One UART 8N1 byte on pin 0, LSB first, then park with the line idle (high).
; The byte to send is loaded into the shift register by the host while halted.
;
; Every bit is exactly BIT cycles: a 1-cycle SET/SHOUT plus WAIT BIT-1.
; BIT = 347 is 115200 baud at 40 MHz (0.06% fast). The tests also assemble
; this file with BIT = 16 to keep simulation quick.
.equ BIT 347
.equ TX  0

        set   TX, 0         ; start bit
        wait  BIT-1
        shout TX            ; data bit 0
        wait  BIT-1
        shout TX            ; data bit 1
        wait  BIT-1
        shout TX            ; data bit 2
        wait  BIT-1
        shout TX            ; data bit 3
        wait  BIT-1
        shout TX            ; data bit 4
        wait  BIT-1
        shout TX            ; data bit 5
        wait  BIT-1
        shout TX            ; data bit 6
        wait  BIT-1
        shout TX            ; data bit 7
        wait  BIT-1
        shout TX            ; stop bit: the shift register has refilled with 1s
        wait  BIT-1
done:   jump  done          ; park; the line stays high
