; SPI controller, modes 0-3 (CPOL and CPHA by .equ), MSB first, full duplex, SCK = 1 / (2 HALF).
; Pins as v0.3 (firmware/engine/spi_cpha0.s): SCK = output pin 4 (uio[4]), MOSI = pin 5 (uio[5]),
; CS = pin 6 (uio[6]), all push-pull; MISO = input 7 (uio_in[7]).
;
; Host protocol: one IN byte per byte to send, one OUT byte per byte received (no spreading).
; Frames: CS falls when the first byte arrives and stays low while bytes keep coming. When no
; next byte has arrived GAP after a byte ends, CS rises: the host ends a frame by pausing
; (>= GAP + POLL), and keeps a frame going by pushing each next byte within GAP of the end of
; the one before (IN holds 4, so a host can stay ahead). CS then stays high >= TCSH.
; Why not an end-of-frame marker byte as in v0.3: every value 0..255 is data now, and with
; candidate A the firmware cannot look at an IN byte anyway (`pull` loads SRO, `cmp` reads SRI).
;
; One program for all four modes. Each bit is "set SCK to LV1, put the bit on MOSI, half a
; period, set SCK to LV2, sample MISO, half a period":
;   CPHA 0: LV1 = CPOL (the trailing edge, or nothing on the first bit), LV2 = the leading edge:
;           MOSI is set half a period before the leading edge, both ends sample on it; after
;           the 8th bit one more `set SCK, CPOL` gives the last trailing edge.
;   CPHA 1: LV1 = the leading edge (both ends shift), LV2 = the trailing edge (both sample).
; Every `set 4` issues right after a `waitd`, so SCK's high and low times are both exactly HALF
; (v0.3's loop ends `waitd HALF; loop`, which makes one half period a cycle longer than the other).
; `shi` samples MISO as it was 2 cycles before the SCK pad changes (the set's output latency 1 +
; the synchronizer 2 - the 1 cycle shi issues after the set): a target must have MISO valid by
; then.
;
; v1 candidate used: A. Without it every data bit needs a shift out and a shift in of the one
; SR, so the host had to spread each byte over two IN bytes and gather two OUT bytes
; (v0.3: 29 words per CPHA, 2 IN + 2 OUT bytes per data byte).
; Clock-independent: time literals are converted to cycles at the image's clock.
.equ CPOL 0
.equ CPHA 0
.equ HALF 500ns                  ; half an SCK period (1 MHz)
.equ GAP 20us                    ; a frame ends when no byte comes for this long
.equ POLL 1us                    ; how often the IN FIFO is looked at during GAP
.equ TCSH 1us                    ; CS high time between frames
.equ LV1 (CPOL + CPHA) & 1
.equ LV2 (CPOL + CPHA + 1) & 1

        set  6, 1                ; CS high (inactive)
        set  4, CPOL             ; SCK idle
idle:   pull hi                  ; the first byte of a frame, MSB in SRO[15]
        je   idle
        set  6, 0                ; CS low
go:     mark
        ldrc 7
bit:    waitd HALF               ; (first bit: CS setup, or the gap between bytes)
        set  4, LV1
        sho  5, msb              ; MOSI <- SRO[15]
        waitd HALF
        set  4, LV2
        shi  7, msb              ; SRI <- {SRI[14:0], MISO}
        loop bit
        waitd HALF
        set  4, CPOL             ; CPHA 0: the last trailing edge (CPHA 1: SCK is idle already)
        push                     ; the byte received: SRI[7:0]
        ldrc GAP/POLL - 1        ; look for the next byte for up to GAP
next:   pull hi
        jne  go                  ; it came: same frame
        waitd POLL
        loop next
        set  6, 1                ; none: CS high, the frame is over
        mark
        waitd TCSH
        jmp  idle
