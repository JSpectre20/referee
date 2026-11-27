// SPDX-License-Identifier: Apache-2.0
//
// End-to-end simulation of fpga/rtl/fpga_top.v with the chip at 60 MHz.
// fpga/sim/zynq_ps_model.v plays the block design: PS UART on EMIO, PS I2C0 as
// controller, PS I2C1 as target at 0x50, 60 MHz clock. Run: fpga/sim/run.sh
//
// Checks
//   1. Through the PS UART (EMIO) at 115200: identify 49 00 00 00 -> 06 52.
//   2. Every load packet of firmware/engine/i2c_setup_referee.s is ACKed; RUN is ACKed.
//   3. I2C traffic between the two PS models over fpga_top's open-drain nets:
//      write [10 a5 3c 5a c3] at 400 kHz, random read back (repeated START),
//      the same at 100 kHz, then one data byte (0x55) whose SDA changes only
//      40 ns before each SCL rise (Fast-mode needs tSU;DAT >= 100 ns).
//      The target stretches SCL once; the controller must wait for it.
//   4. The judge channels see the bus: the chip's ui_in[5]/ui_in[4] follow
//      the SCL/SDA pads edge for edge.
//   5. stats 0: pass + violation + indeterminate == SCL rises on the pad since
//      RUN; violations >= 7 (the bad byte), and none on the clean transfers.
//   6. drain: the VIOLATIONs come out of the verdict FIFO.
//   7. The ILA path: fpga/sim/ila_model.v stores every change of the probes
//      (as the real ILA does with fpga/ila_capture.tcl's capture control) from
//      RUN on into fpga/sim/out/ila_run/ila.csv, and this testbench writes the
//      chip's side (stats 0, the drained slots, status) to
//      fpga/sim/out/ila_run/verdicts.jsonl; fpga/sim/run.sh then runs
//      tools/ila_check.py on the two, as on a board run.
`timescale 1ns / 1ps
`default_nettype none

module tb_fpga;
  wire i2c_scl, i2c_sda;
  wire [7:0] uio;
  wire [7:0] led;
  pullup (i2c_scl);
  pullup (i2c_sda);
  pullup (uio[0]); pullup (uio[1]); pullup (uio[2]); pullup (uio[3]);
  pullup (uio[4]); pullup (uio[5]); pullup (uio[6]); pullup (uio[7]);
  reg [7:0] sw = 8'h00;
  reg [4:0] btn = 5'h00;
  reg sys_clk = 1'b0;  // the board oscillator (100 MHz); ila_model.v makes its own ILA clock
  always #5 sys_clk = ~sys_clk;

  // PS-only pins: left floating (inout), as the model ignores them.
  wire [14:0] DDR_addr; wire [2:0] DDR_ba; wire [3:0] DDR_dm, DDR_dqs_n, DDR_dqs_p;
  wire [31:0] DDR_dq; wire [53:0] FIXED_IO_mio;
  wire DDR_cas_n, DDR_ck_n, DDR_ck_p, DDR_cke, DDR_cs_n, DDR_odt, DDR_ras_n, DDR_reset_n,
       DDR_we_n, FIXED_IO_ddr_vrn, FIXED_IO_ddr_vrp, FIXED_IO_ps_clk, FIXED_IO_ps_porb,
       FIXED_IO_ps_srstb;

  fpga_top #(.N_LED(8), .N_SW(8), .N_BTN(5)) dut (
      .DDR_addr(DDR_addr), .DDR_ba(DDR_ba), .DDR_cas_n(DDR_cas_n), .DDR_ck_n(DDR_ck_n),
      .DDR_ck_p(DDR_ck_p), .DDR_cke(DDR_cke), .DDR_cs_n(DDR_cs_n), .DDR_dm(DDR_dm),
      .DDR_dq(DDR_dq), .DDR_dqs_n(DDR_dqs_n), .DDR_dqs_p(DDR_dqs_p), .DDR_odt(DDR_odt),
      .DDR_ras_n(DDR_ras_n), .DDR_reset_n(DDR_reset_n), .DDR_we_n(DDR_we_n),
      .FIXED_IO_ddr_vrn(FIXED_IO_ddr_vrn), .FIXED_IO_ddr_vrp(FIXED_IO_ddr_vrp),
      .FIXED_IO_mio(FIXED_IO_mio), .FIXED_IO_ps_clk(FIXED_IO_ps_clk),
      .FIXED_IO_ps_porb(FIXED_IO_ps_porb), .FIXED_IO_ps_srstb(FIXED_IO_ps_srstb),
      .sys_clk(sys_clk), .i2c_scl(i2c_scl), .i2c_sda(i2c_sda), .uio(uio), .sw(sw), .btn(btn),
      .led(led)
  );

  // Optional waveform: +vcd
  initial if ($test$plusargs("vcd")) begin
    $dumpfile("fpga/sim/out/tb_fpga.vcd");
    $dumpvars(0, tb_fpga);
  end

  integer errors = 0;
  task check(input cond, input [8*80-1:0] what);
    begin
      if (!cond) begin
        errors = errors + 1;
        $display("FAIL: %0s", what);
      end
    end
  endtask

  // ---------------------------------------------------------------- bus monitors
  // Edges on the pads, and the same edges as the chip's judge channels see them.
  integer scl_rises = 0, scl_edges = 0, sda_edges = 0, j_scl_edges = 0, j_sda_edges = 0;
  always @(posedge i2c_scl) scl_rises = scl_rises + 1;
  always @(i2c_scl) scl_edges = scl_edges + 1;
  always @(i2c_sda) sda_edges = sda_edges + 1;
  always @(dut.u_chip.ui_in[5]) j_scl_edges = j_scl_edges + 1;
  always @(dut.u_chip.ui_in[4]) j_sda_edges = j_sda_edges + 1;

  // ---------------------------------------------------------------- host side
  reg [31:0] pkts[0:255];
  integer npkt;
  reg [7:0] r0, r1;

  task transact(input [31:0] p, output [7:0] st, output [7:0] data);
    integer n0;
    begin
      n0 = dut.u_ps.rx_n;
      dut.u_ps.uart_send(p[31:24]);
      dut.u_ps.uart_send(p[23:16]);
      dut.u_ps.uart_send(p[15:8]);
      dut.u_ps.uart_send(p[7:0]);
      fork : wait_reply
        wait (dut.u_ps.rx_n >= n0 + 2);
        begin
          #2_000_000;
          $display("FAIL: no reply to %08h", p);
          errors = errors + 1;
        end
      join_any
      disable wait_reply;
      st = dut.u_ps.rx_buf[n0 % 1024];
      data = dut.u_ps.rx_buf[(n0 + 1) % 1024];
    end
  endtask

  task stat16(input [7:0] idx, output [15:0] v);
    reg [7:0] s, hi, lo;
    begin
      transact({8'h73, 8'h00, idx, 8'h01}, s, hi);
      transact({8'h73, 8'h00, idx, 8'h00}, s, lo);
      v = {hi, lo};
    end
  endtask

  // ---------------------------------------------------------------- I2C helpers
  reg nack;
  reg [7:0] rb;
  task i2c_write(input [7:0] ptr, input [31:0] d);
    integer i;
    begin
      dut.u_ps.m_start;
      dut.u_ps.m_write_byte(8'hA0, dut.u_ps.t_su, nack); check(!nack, "address ACK (write)");
      dut.u_ps.m_write_byte(ptr, dut.u_ps.t_su, nack);  check(!nack, "pointer ACK");
      for (i = 3; i >= 0; i = i - 1) begin
        dut.u_ps.m_write_byte(d[8*i +: 8], dut.u_ps.t_su, nack); check(!nack, "data ACK");
      end
      dut.u_ps.m_stop;
    end
  endtask
  task i2c_read_check(input [7:0] ptr, input [31:0] d);
    integer i;
    begin
      dut.u_ps.m_start;
      dut.u_ps.m_write_byte(8'hA0, dut.u_ps.t_su, nack); check(!nack, "address ACK (ptr)");
      dut.u_ps.m_write_byte(ptr, dut.u_ps.t_su, nack);  check(!nack, "pointer ACK (read)");
      dut.u_ps.m_rstart;
      dut.u_ps.m_write_byte(8'hA1, dut.u_ps.t_su, nack); check(!nack, "address ACK (read)");
      for (i = 3; i >= 0; i = i - 1) begin
        dut.u_ps.m_read_byte(rb, i == 0);
        check(rb == d[8*i +: 8], "read-back data");
      end
      dut.u_ps.m_stop;
    end
  endtask

  // ---------------------------------------------------------------- the test
  integer i, rises0, n, fd;
  integer e0_scl, e0_sda, e0_jscl, e0_jsda;
  reg [15:0] npass, nviol, nind, smin, smax;
  reg [15:0] slot;
  integer viol_drained, vf;
  initial begin
    #500_000_000;
    $display("FAIL: global timeout");
    $finish;
  end

  initial begin
    fd = $fopen("fpga/sim/out/packets.hex", "r");
    if (fd == 0) begin
      $display("FAIL: run fpga/sim/gen_packets.py first");
      $finish;
    end
    npkt = 0;
    while (npkt < 256 && $fscanf(fd, "%h\n", pkts[npkt]) == 1) npkt = npkt + 1;
    $fclose(fd);
    $display("%0d host packets", npkt);

    wait (dut.chip_rst_n === 1'b1);
    #2000;

    // 1 + 2: identify, load, run (all at 115200, through the EMIO UART)
    for (i = 0; i < npkt; i = i + 1) begin
      transact(pkts[i], r0, r1);
      if (i == 0) begin
        $display("identify: 49 00 00 00 -> %02h %02h (expect 06 52)", r0, r1);
        check(r0 == 8'h06 && r1 == 8'h52, "identify reply 06 52");
      end else begin
        check(r0 == 8'h06, "load/run packet ACK");
        if (r0 != 8'h06) $display("  packet %0d %08h -> %02h %02h", i, pkts[i], r0, r1);
      end
    end
    $display("loaded and running at t = %0t ns", $time);
    rises0 = scl_rises;
    e0_scl = scl_edges; e0_sda = sda_edges; e0_jscl = j_scl_edges; e0_jsda = j_sda_edges;
    dut.u_ila.arm;                   // 7: the ILA from RUN on, like the board run's steps
    dut.u_ps.gpio_out[15] = 1'b1;    // the PS mark, as fpga/sw/main.c during traffic

    // 3: traffic. 400 kHz
    dut.u_ps.t_low = 1300.0; dut.u_ps.t_high = 1200.0; dut.u_ps.t_su = 600.0;
    i2c_write(8'h10, 32'ha53c5ac3);
    #5000;
    i2c_read_check(8'h10, 32'ha53c5ac3);
    #5000;
    // 100 kHz
    dut.u_ps.t_low = 4700.0; dut.u_ps.t_high = 4000.0; dut.u_ps.t_su = 2000.0;
    i2c_write(8'h20, 32'h01020304);
    #20000;
    i2c_read_check(8'h20, 32'h01020304);
    #20000;
    check(dut.u_ps.s_stretches == 1, "the target stretched SCL once");
    // bad setup: 0x55 with SDA changing 40 ns before each SCL rise (400 kHz)
    dut.u_ps.t_low = 1300.0; dut.u_ps.t_high = 1200.0; dut.u_ps.t_su = 600.0;
    dut.u_ps.m_start;
    dut.u_ps.m_write_byte(8'hA0, 600.0, nack);
    dut.u_ps.m_write_byte(8'h30, 600.0, nack);
    dut.u_ps.m_write_byte(8'h55, 40.0, nack);
    dut.u_ps.m_stop;
    dut.u_ps.gpio_out[15] = 1'b0;
    #20000;

    // 4: the judge channels followed the pads
    $display("since RUN: pad edges SCL %0d SDA %0d; judge channel edges SCL %0d SDA %0d",
             scl_edges - e0_scl, sda_edges - e0_sda, j_scl_edges - e0_jscl, j_sda_edges - e0_jsda);
    check(j_scl_edges - e0_jscl == scl_edges - e0_scl && j_sda_edges - e0_jsda == sda_edges - e0_sda,
          "judge channels see every pad edge");
    check(scl_edges - e0_scl > 400, "the bus toggled");

    // 5: stats 0
    transact(32'h53000000, r0, r1); check(r0 == 8'h06, "stats latch ACK");
    stat16(8'd0, npass);
    stat16(8'd1, nviol);
    stat16(8'd2, nind);
    stat16(8'd3, smin);
    stat16(8'd4, smax);
    $display("stats 0: pass %0d viol %0d indet %0d; SCL rises since RUN %0d",
             npass, nviol, nind, scl_rises - rises0);
    check(npass + nviol + nind == scl_rises - rises0, "one verdict per SCL rise");
    check(nviol >= 7, "the 40 ns setup byte gives violations");
    check(nviol + nind <= 9, "no non-PASS verdict outside the bad byte");

    // 6: drain
    transact(32'h44000800, r0, r1);
    n = r1;
    #(2 * n * 87000 + 100000);
    viol_drained = 0;
    for (i = 0; i < n; i = i + 1)
      if (dut.u_ps.rx_buf[(dut.u_ps.rx_n - 2 * n + 2 * i) % 1024] >> 6 == 2)
        viol_drained = viol_drained + 1;
    $display("drain: %0d slots, %0d VIOLATION headers", n, viol_drained);
    check(r0 == 8'h06 && viol_drained >= 7, "VIOLATIONs drained");

    // 7: the chip's side of the ILA check (tools/ila_check.py record format)
    vf = $fopen("fpga/sim/out/ila_run/verdicts.jsonl", "w");
    $fwrite(vf, "{\"type\": \"stats\", \"slot\": 0, \"pass\": %0d, \"viol\": %0d, \"indet\": %0d, \"min\": %0d, \"max\": %0d}\n",
            npass, nviol, nind, smin, smax);
    $fwrite(vf, "{\"type\": \"drain\", \"slots\": [");
    for (i = 0; i < n; i = i + 1) begin
      slot = {dut.u_ps.rx_buf[(dut.u_ps.rx_n - 2 * n + 2 * i) % 1024],
              dut.u_ps.rx_buf[(dut.u_ps.rx_n - 2 * n + 2 * i + 1) % 1024]};
      $fwrite(vf, "%0s%0d", i ? ", " : "", slot);
    end
    $fwrite(vf, "]}\n");
    transact(32'h51000000, r0, r1); check(r0 == 8'h06, "status ACK");
    $fwrite(vf, "{\"type\": \"status\", \"data\": %0d}\n", r1);
    $fclose(vf);
    dut.u_ila.stop;

    $display("LEDs %b (7..0)", led);
    if (errors == 0) $display("PASS: all checks");
    else $display("FAILED: %0d checks", errors);
    $finish;
  end
endmodule
`default_nettype wire
