/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Referee on Zynq-7020: bare-metal app for the ARM (standalone BSP).
 *
 * 1. UART bridge. The board's USB-UART (a PS UART on MIO) <-> the other PS
 *    UART, which goes through EMIO to the chip's host port (ui_in[3] RX,
 *    uo_out[4] TX). Both at 115200 8N1, bytes copied both ways, nothing added
 *    or removed, so tools/referee_host.py --port <COM port> works unchanged.
 *      ZedBoard: PC = UART1 (MIO 48/49), chip = UART0 (EMIO)
 *      PYNQ-Z2:  PC = UART0 (MIO 14/15), chip = UART1 (EMIO)
 *    The board is detected at run time from the MIO mux (see pick_uarts).
 *    After one banner line at start-up, this app never writes to the PC on its
 *    own: status goes to the LEDs, because the console IS the bridge.
 *
 * 2. I2C traffic (third-party implementation: the Zynq's Cadence I2C).
 *    PS I2C0 = controller, PS I2C1 = target at 0x50, both on EMIO, joined in
 *    the PL into one open-drain SCL and one SDA net on Pmod pins, which the
 *    chip's judge channels also read. The target behaves like a 24C02
 *    EEPROM: a 256-byte register file; a write sends [pointer, data...]; a
 *    read returns bytes from the pointer, auto-incrementing.
 *    The controller repeats, every GAP_MS:
 *      write   S 0x50+W ptr d0 d1 d2 d3 P
 *      read    S 0x50+W ptr Sr 0x50+R d0 d1 d2 d3 P   (random read)
 *    and checks the read-back.
 *
 * Board controls (PL EMIO GPIO, pin 54 + bit, see fpga/rtl/fpga_top.v)
 *    SW0  traffic on / off           SW1  0 = 100 kHz, 1 = 400 kHz
 *    BTN0 (ZedBoard BTNC) resets the chip in the PL (not seen by this app)
 *    BTN1 (ZedBoard BTND) clears the error LED
 *    BTN2 (ZedBoard BTNL) one write / read-back cycle (with SW0 off): a known
 *         number of SCL rises, so `stats 0` can be checked by hand
 *    LEDs driven from here: traffic on, 400 kHz, ok (last check passed), error
 *    EMIO GPIO 15: the ILA's mark, high from the start of a write / read-back
 *         cycle to its end (an ILA trigger: fpga/ila_capture.tcl -trigger mark)
 *
 * Everything is polled in one loop; nothing blocks for longer than one
 * register access, so the bridge keeps up (a 64-byte UART FIFO lasts 5.5 ms at
 * 115200) while both I2C controllers are serviced. If the loop is late, the
 * Cadence target stretches SCL, which every I2C controller must honour.
 *
 * The I2C controller and target are driven at register level (xiicps_hw.h)
 * because the XIicPs polled calls block, and here one CPU is both ends of the
 * bus. The sequences follow the Linux i2c-cadence driver, which handles the
 * Zynq-7000's hold-bit erratum (a repeated-start read must clear HOLD right
 * after writing the address) and implements target mode the same way.
 */

#include <stddef.h>

#include "xil_io.h"
#include "xil_types.h"
#include "xiicps.h"
#include "xgpiops.h"
#include "xparameters.h"
#include "xstatus.h"
#include "xtime_l.h"
#include "xuartps.h"

/* ---- fixed Zynq-7000 addresses (TRM UG585, appendix B) -------------------- */
#define UART0_BASE   0xE0000000u
#define UART1_BASE   0xE0001000u
#define I2C0_BASE    0xE0004000u
#define I2C1_BASE    0xE0005000u
#define GPIO_BASE    0xE000A000u
#define SLCR_MIO_PIN(n) (0xF8000700u + 4u * (n))

/* ---- settings ---------------------------------------------------------------- */
#define BAUD         115200u
#define TARGET_ADDR  0x50u   /* the PS I2C1 target, like a 24C02 */
/* Where the traffic goes. To exercise a real 24C02 on the Pmod instead: set
 * TRAFFIC_ADDR to its address (0x50) and TARGET_ADDR to a free one (0x51). */
#define TRAFFIC_ADDR TARGET_ADDR
#define WRITE_WAIT_MS 10u    /* after a write: a real EEPROM's write cycle (5 ms) */
#define XFER_BYTES   4       /* data bytes per write / read */
#define GAP_MS       20u     /* idle time between write / read-back cycles */
#define XFER_TIMEOUT_MS 20u  /* a transfer that takes longer is an error */

/* EMIO GPIO: PS pin = 54 + bit */
#define EMIO(bit)    (54u + (bit))
#define IN_TRAFFIC   EMIO(0)   /* SW0 */
#define IN_FAST      EMIO(1)   /* SW1 */
#define IN_CLEAR     EMIO(9)   /* BTN1 */
#define IN_SHOT      EMIO(10)  /* BTN2 */
#define OUT_MARK     EMIO(15)  /* ILA mark: a traffic cycle is running */
#define OUT_TRAFFIC  EMIO(16)
#define OUT_FAST     EMIO(17)
#define OUT_OK       EMIO(18)
#define OUT_ERR      EMIO(19)

/* ---- short register helpers ------------------------------------------------- */
#define RD(base, off)      Xil_In32((base) + (off))
#define WR(base, off, val) Xil_Out32((base) + (off), (val))
#define CR_DIVS  (XIICPS_CR_DIV_A_MASK | XIICPS_CR_DIV_B_MASK)
#define CR_MASTER (XIICPS_CR_MS_MASK | XIICPS_CR_ACKEN_MASK | XIICPS_CR_NEA_MASK)
#define ISR_ERRS (XIICPS_IXR_NACK_MASK | XIICPS_IXR_ARB_LOST_MASK | XIICPS_IXR_TO_MASK | \
                  XIICPS_IXR_RX_OVR_MASK | XIICPS_IXR_RX_UNF_MASK | XIICPS_IXR_TX_OVR_MASK)

static XUartPs uart_pc, uart_chip;
static XIicPs  iic0, iic1;
static XGpioPs gpio;
static u32 pc_base, chip_base;

static u64 now_ms(void)
{
    XTime t;
    XTime_GetTime(&t);
    return (u64)t / (COUNTS_PER_SECOND / 1000u);
}

/* =============================================================================
 * Driver lookup that works with both BSP flavours: the classic one (Vitis up
 * to 2023.1, LookupConfig takes a device ID) and the system-device-tree one
 * (SDT, Vitis 2023.2 and later, LookupConfig takes the base address).
 * ========================================================================== */
static XUartPs_Config *uart_cfg(u32 base)
{
#ifdef SDT
    return XUartPs_LookupConfig(base);
#else
    for (u16 id = 0; id < XPAR_XUARTPS_NUM_INSTANCES; id++) {
        XUartPs_Config *c = XUartPs_LookupConfig(id);
        if (c && c->BaseAddress == base) return c;
    }
    return NULL;
#endif
}

static XIicPs_Config *iic_cfg(u32 base)
{
#ifdef SDT
    return XIicPs_LookupConfig(base);
#else
    for (u16 id = 0; id < XPAR_XIICPS_NUM_INSTANCES; id++) {
        XIicPs_Config *c = XIicPs_LookupConfig(id);
        if (c && c->BaseAddress == base) return c;
    }
    return NULL;
#endif
}

static XGpioPs_Config *gpio_cfg(void)
{
#ifdef SDT
    return XGpioPs_LookupConfig(GPIO_BASE);
#else
    return XGpioPs_LookupConfig(XPAR_XGPIOPS_0_DEVICE_ID);
#endif
}

/* Which PS UART is the USB-UART? The one whose MIO pins are muxed to a UART
 * (L3_SEL = 7 in MIO_PIN_xx, TRM UG585 B.28). PYNQ-Z2: UART0 on MIO 14/15.
 * ZedBoard: UART1 on MIO 48/49. The other UART is on EMIO, to the chip. */
static void pick_uarts(void)
{
    u32 mio14 = (Xil_In32(SLCR_MIO_PIN(14)) >> 5) & 7u;
    if (mio14 == 7u) {          /* PYNQ-Z2 */
        pc_base = UART0_BASE;
        chip_base = UART1_BASE;
    } else {                    /* ZedBoard */
        pc_base = UART1_BASE;
        chip_base = UART0_BASE;
    }
}

static int uart_init(XUartPs *u, u32 base)
{
    XUartPs_Config *c = uart_cfg(base);
    if (!c || XUartPs_CfgInitialize(u, c, c->BaseAddress) != XST_SUCCESS) return XST_FAILURE;
    XUartPs_SetOperMode(u, XUARTPS_OPER_MODE_NORMAL);
    return XUartPs_SetBaudRate(u, BAUD);   /* 8N1 is the driver default */
}

static void pc_puts(const char *s)
{
    while (*s) XUartPs_SendByte(pc_base, (u8)*s++);
}

/* =============================================================================
 * 1. The UART bridge: up to 16 bytes each way per call, never blocking long
 *    (both sides run at the same baud, so the TX FIFO drains as fast as RX fills)
 * ========================================================================== */
static void bridge_poll(void)
{
    for (int i = 0; i < 16 && XUartPs_IsReceiveData(pc_base); i++)
        XUartPs_SendByte(chip_base, XUartPs_RecvByte(pc_base));
    for (int i = 0; i < 16 && XUartPs_IsReceiveData(chip_base); i++)
        XUartPs_SendByte(pc_base, XUartPs_RecvByte(chip_base));
}

/* =============================================================================
 * 2a. The I2C target (PS I2C1, address 0x50): a 24C02-like register file
 * ========================================================================== */
static u8  t_mem[256];
static u8  t_ptr;
static int t_expect_ptr = 1;   /* the next byte written is the pointer */

static void target_setup(u32 sclk_hz)
{
    (void)XIicPs_SetSClk(&iic1, sclk_hz);   /* the target's sampling / hold timing */
    XIicPs_SetupSlave(&iic1, TARGET_ADDR);   /* ACKEN, 7-bit, slave mode, FIFO cleared */
    WR(I2C1_BASE, XIICPS_TIME_OUT_OFFSET, 0xFFu);
    t_expect_ptr = 1;
}

static void target_poll(void)
{
    u32 isr = RD(I2C1_BASE, XIICPS_ISR_OFFSET);
    if (isr) WR(I2C1_BASE, XIICPS_ISR_OFFSET, isr);     /* write 1 to clear */
    u32 sr = RD(I2C1_BASE, XIICPS_SR_OFFSET);

    /* Bytes from the controller first (this also takes in the pointer byte of a
     * random read before the target starts sending after the repeated START). */
    for (int i = 0; i < XIICPS_FIFO_DEPTH && (sr & XIICPS_SR_RXDV_MASK); i++) {
        u8 b = (u8)RD(I2C1_BASE, XIICPS_DATA_OFFSET);
        if (t_expect_ptr) {
            t_ptr = b;
            t_expect_ptr = 0;
        } else {
            t_mem[t_ptr++] = b;
        }
        sr = RD(I2C1_BASE, XIICPS_SR_OFFSET);
    }

    /* Controller reading (RXRW): DATA asks for the next byte; one per request,
     * as the Linux driver does. The target holds SCL low until it is written.
     * After the controller's final NACK one byte too many may be queued (the
     * pointer then ends one past the last byte read); the FIFO is cleared below. */
    if ((sr & XIICPS_SR_RXRW_MASK) && (isr & XIICPS_IXR_DATA_MASK))
        WR(I2C1_BASE, XIICPS_DATA_OFFSET, t_mem[t_ptr++]);

    if (isr & (XIICPS_IXR_NACK_MASK | XIICPS_IXR_RX_OVR_MASK | XIICPS_IXR_RX_UNF_MASK |
               XIICPS_IXR_TX_OVR_MASK))
        WR(I2C1_BASE, XIICPS_CR_OFFSET, RD(I2C1_BASE, XIICPS_CR_OFFSET) | XIICPS_CR_CLR_FIFO_MASK);

    /* End of a transfer (STOP, or the bus went idle): next write starts with a
     * pointer. A repeated START keeps the bus active, so a random read keeps it. */
    if ((isr & XIICPS_IXR_COMP_MASK) || !(sr & XIICPS_SR_BA_MASK))
        t_expect_ptr = 1;
}

/* =============================================================================
 * 2b. The I2C controller (PS I2C0): non-blocking transfers of <= 16 bytes
 * ========================================================================== */
static void master_setup(u32 sclk_hz)
{
    (void)XIicPs_SetSClk(&iic0, sclk_hz);
    WR(I2C0_BASE, XIICPS_CR_OFFSET,
       (RD(I2C0_BASE, XIICPS_CR_OFFSET) & CR_DIVS) | CR_MASTER | XIICPS_CR_CLR_FIFO_MASK);
    WR(I2C0_BASE, XIICPS_TIME_OUT_OFFSET, 0xFFu);
}

/* Start: S addr+W bytes..., then P, or (hold) keep SCL low for a repeated START. */
static void master_start_write(const u8 *b, int n, int hold)
{
    u32 cr = (RD(I2C0_BASE, XIICPS_CR_OFFSET) & CR_DIVS) | CR_MASTER | XIICPS_CR_CLR_FIFO_MASK;
    if (hold) cr |= XIICPS_CR_HOLD_MASK;
    WR(I2C0_BASE, XIICPS_CR_OFFSET, cr);                 /* RW = 0: transmit */
    WR(I2C0_BASE, XIICPS_ISR_OFFSET, RD(I2C0_BASE, XIICPS_ISR_OFFSET));
    for (int i = 0; i < n; i++) WR(I2C0_BASE, XIICPS_DATA_OFFSET, b[i]);
    WR(I2C0_BASE, XIICPS_ADDR_OFFSET, TRAFFIC_ADDR);     /* starts the transfer */
}

/* After a held write: Sr addr+R, n bytes, NACK, P. */
static void master_start_read_after_hold(int n)
{
    u32 cr = RD(I2C0_BASE, XIICPS_CR_OFFSET) | XIICPS_CR_RD_WR_MASK | XIICPS_CR_CLR_FIFO_MASK;
    WR(I2C0_BASE, XIICPS_CR_OFFSET, cr);                 /* HOLD still set */
    WR(I2C0_BASE, XIICPS_ISR_OFFSET, RD(I2C0_BASE, XIICPS_ISR_OFFSET));
    WR(I2C0_BASE, XIICPS_TRANS_SIZE_OFFSET, (u32)n);
    WR(I2C0_BASE, XIICPS_ADDR_OFFSET, TRAFFIC_ADDR);     /* repeated START */
    /* Last message: release HOLD now so the controller ends with STOP. On the
     * Zynq-7000 this must happen before the transfer size reaches 0 (erratum
     * handled the same way in Linux i2c-cadence.c, CDNS_I2C_BROKEN_HOLD_BIT). */
    WR(I2C0_BASE, XIICPS_CR_OFFSET, RD(I2C0_BASE, XIICPS_CR_OFFSET) & ~XIICPS_CR_HOLD_MASK);
    (void)RD(I2C0_BASE, XIICPS_CR_OFFSET);
}

/* 0 running, 1 complete, -1 error (NACK, arbitration lost, timeout). */
static int master_done(void)
{
    u32 isr = RD(I2C0_BASE, XIICPS_ISR_OFFSET);
    if (isr & ISR_ERRS) return -1;
    return (isr & XIICPS_IXR_COMP_MASK) ? 1 : 0;
}

/* After an error: drop HOLD (the controller sends STOP), clear the FIFO. */
static void master_recover(void)
{
    WR(I2C0_BASE, XIICPS_CR_OFFSET,
       (RD(I2C0_BASE, XIICPS_CR_OFFSET) & CR_DIVS) | CR_MASTER | XIICPS_CR_CLR_FIFO_MASK);
    WR(I2C0_BASE, XIICPS_ISR_OFFSET, RD(I2C0_BASE, XIICPS_ISR_OFFSET));
}

/* =============================================================================
 * 2c. The traffic generator
 * ========================================================================== */
enum { T_IDLE, T_WRITE, T_WAIT, T_PTR, T_READ };
static int  t_state = T_IDLE;
static u64  t_since;
static u8   wbuf[1 + XFER_BYTES];
static u32  cycle;
static int  led_ok, led_err;
static int  shot;              /* one cycle requested by BTN2 */

static void traffic_poll(int enabled)
{
    u64 t = now_ms();
    int r;

    switch (t_state) {
    case T_IDLE:
        if (!(enabled || shot) || t - t_since < GAP_MS) return;
        shot = 0;
        XGpioPs_WritePin(&gpio, OUT_MARK, 1);               /* ILA mark: cycle starts */
        wbuf[0] = (u8)(cycle * XFER_BYTES);                 /* pointer */
        for (int i = 0; i < XFER_BYTES; i++) wbuf[1 + i] = (u8)(cycle * 7u + i * 0x35u);
        master_start_write(wbuf, 1 + XFER_BYTES, 0);
        t_state = T_WRITE;
        t_since = t;
        return;
    case T_WAIT:                             /* write cycle time, then pointer + hold */
        if (t - t_since < WRITE_WAIT_MS) return;
        master_start_write(wbuf, 1, 1);
        t_state = T_PTR;
        t_since = t;
        return;
    case T_WRITE:
    case T_PTR:
    case T_READ:
        r = master_done();
        if (r == 0) {
            if (t - t_since < XFER_TIMEOUT_MS) return;
            r = -1;
        }
        if (r < 0) {
            master_recover();
            led_err = 1;
            led_ok = 0;
            XGpioPs_WritePin(&gpio, OUT_MARK, 0);
            t_state = T_IDLE;
            t_since = t;
            return;
        }
        if (t_state == T_WRITE) {            /* write done: wait, then the read */
            t_state = T_WAIT;
        } else if (t_state == T_PTR) {       /* pointer done: repeated START, read */
            master_start_read_after_hold(XFER_BYTES);
            t_state = T_READ;
        } else {                             /* read done: check */
            int good = 1;
            for (int i = 0; i < XFER_BYTES; i++) {
                if (!(RD(I2C0_BASE, XIICPS_SR_OFFSET) & XIICPS_SR_RXDV_MASK)) { good = 0; break; }
                if ((u8)RD(I2C0_BASE, XIICPS_DATA_OFFSET) != wbuf[1 + i]) good = 0;
            }
            led_ok = good;
            if (!good) led_err = 1;
            cycle++;
            XGpioPs_WritePin(&gpio, OUT_MARK, 0);           /* ILA mark: cycle done */
            t_state = T_IDLE;
        }
        t_since = t;
        return;
    }
}

/* =============================================================================
 * main
 * ========================================================================== */
int main(void)
{
    pick_uarts();
    if (uart_init(&uart_pc, pc_base) != XST_SUCCESS) return XST_FAILURE;
    if (uart_init(&uart_chip, chip_base) != XST_SUCCESS) return XST_FAILURE;

    XIicPs_Config *c0 = iic_cfg(I2C0_BASE), *c1 = iic_cfg(I2C1_BASE);
    if (!c0 || !c1) return XST_FAILURE;
    if (XIicPs_CfgInitialize(&iic0, c0, c0->BaseAddress) != XST_SUCCESS) return XST_FAILURE;
    if (XIicPs_CfgInitialize(&iic1, c1, c1->BaseAddress) != XST_SUCCESS) return XST_FAILURE;
    XIicPs_Reset(&iic0);
    XIicPs_Reset(&iic1);

    XGpioPs_Config *gc = gpio_cfg();
    if (!gc || XGpioPs_CfgInitialize(&gpio, gc, gc->BaseAddr) != XST_SUCCESS) return XST_FAILURE;
    const u32 outs[] = {OUT_MARK, OUT_TRAFFIC, OUT_FAST, OUT_OK, OUT_ERR};
    for (unsigned i = 0; i < sizeof outs / sizeof outs[0]; i++) {
        XGpioPs_SetDirectionPin(&gpio, outs[i], 1);
        XGpioPs_SetOutputEnablePin(&gpio, outs[i], 1);
        XGpioPs_WritePin(&gpio, outs[i], 0);
    }
    XGpioPs_SetDirectionPin(&gpio, IN_TRAFFIC, 0);
    XGpioPs_SetDirectionPin(&gpio, IN_FAST, 0);
    XGpioPs_SetDirectionPin(&gpio, IN_CLEAR, 0);
    XGpioPs_SetDirectionPin(&gpio, IN_SHOT, 0);

    for (int i = 0; i < 256; i++) t_mem[i] = (u8)i;

    int fast = -1;          /* force the first speed setup */
    int traffic = 0;
    u64 last_ui = 0;
    int shot_btn = 0;

    pc_puts("\r\nReferee bridge ready (115200 8N1). SW0 traffic, SW1 400 kHz.\r\n");

    for (;;) {
        bridge_poll();
        target_poll();
        traffic_poll(traffic);

        /* Switches, buttons and LEDs every 20 ms (also debounces them). */
        u64 t = now_ms();
        if (t - last_ui < 20u) continue;
        last_ui = t;

        int want_fast = XGpioPs_ReadPin(&gpio, IN_FAST) != 0;
        int want_traffic = XGpioPs_ReadPin(&gpio, IN_TRAFFIC) != 0;
        if (XGpioPs_ReadPin(&gpio, IN_CLEAR)) led_err = 0;
        int b2 = XGpioPs_ReadPin(&gpio, IN_SHOT) != 0;
        if (b2 && !shot_btn) shot = 1;          /* on the press, once */
        shot_btn = b2;
        if (want_traffic && !traffic) led_err = 0;   /* a fresh start clears the error */

        /* Change speed only between transfers. */
        if (want_fast != fast && t_state == T_IDLE) {
            u32 hz = want_fast ? 400000u : 100000u;
            master_setup(hz);
            target_setup(hz);
            fast = want_fast;
        }
        traffic = want_traffic;

        XGpioPs_WritePin(&gpio, OUT_TRAFFIC, (u32)traffic);
        XGpioPs_WritePin(&gpio, OUT_FAST, (u32)(fast == 1));
        XGpioPs_WritePin(&gpio, OUT_OK, (u32)led_ok);
        XGpioPs_WritePin(&gpio, OUT_ERR, (u32)led_err);
    }
}
