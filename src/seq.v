/*
 * Copyright (c) 2026 Shreyansh
 * SPDX-License-Identifier: Apache-2.0
 *
 * seq: four-instruction pin sequencer, the seed of the Referee engine.
 *
 * This is the first block where a protocol lives in *firmware* instead of in
 * fixed logic: the same hardware sends a UART frame, an SPI byte or any other
 * pin pattern depending only on the program loaded into it.
 *
 * Instruction word (16 bits)
 * --------------------------
 *   [15:14] opcode
 *     00  SET   pin, v   [13:11] = pin, [0] = v.  pins[pin] <= v
 *     01  WAIT  n        [13:0]  = n.             occupy n cycles (0 counts as 1)
 *     10  SHOUT pin      [13:11] = pin.           pins[pin] <= sr[0]; sr <= {1, sr[7:1]}
 *     11  JUMP  addr     [13:0]  = addr.          pc <= addr (upper bits must be 0)
 *   Unused bits must be 0 (the assembler guarantees it; the hardware ignores them).
 *
 * Timing model (the contract; formal/seq.sby proves the RTL keeps it)
 * -------------------------------------------------------------------
 *   - An instruction "issues" in the cycle it is executed. While running,
 *     exactly one instruction issues per cycle, except that WAIT n keeps the
 *     next instruction from issuing until n cycles after the WAIT issued.
 *     So every instruction costs exactly 1 cycle and WAIT n costs exactly
 *     max(n, 1) cycles. JUMP is 1 cycle too: no branch penalty, because the
 *     program memory is read combinationally (flip-flops, not SRAM, for now).
 *   - Pin latency: a SET or SHOUT issued in cycle t changes `pins` at the
 *     clock edge that ends cycle t, i.e. the new value is visible in cycle t+1.
 *     `pins` changes at no other time. The pins are registers, so they never
 *     glitch.
 *   - Start: the first cycle in which `run` is 1 issues the instruction at
 *     address 0. While `run` is 0 the sequencer is halted: pc is forced to 0
 *     and any pending WAIT is cancelled. `pins` keeps its value across a halt,
 *     so a line can be parked at its idle level (e.g. UART TX high).
 *
 * Loading rule: the program memory and the shift register can only be
 * written while halted (`run` = 0). Writes while running are ignored. This is
 * what makes the timing of a running program depend on nothing but the
 * program itself (docs/seq.md).
 *
 * Example: one UART 8N1 byte at CLKS cycles per bit (see firmware/seq/):
 *   SET tx,0 ; WAIT CLKS-1          start bit, exactly CLKS cycles
 *   SHOUT tx ; WAIT CLKS-1   (x8)   data bits, LSB first
 *   SHOUT tx ; WAIT CLKS-1          stop bit: sr refills with 1s, so a 9th
 *                                   shift outputs a 1
 *   done: JUMP done                 park (a program never runs off its end)
 */

`default_nettype none

module seq #(
    parameter integer   ADDR_W     = 5,      // program memory holds 2**ADDR_W words
    parameter     [7:0] PINS_RESET = 8'h00   // pin levels coming out of reset
) (
    input  wire              clk,
    input  wire              rst_n,      // synchronous, active low

    input  wire              run,        // 1 = execute, 0 = halt (pc back to 0)

    // Program loading (ignored while running)
    input  wire              prog_we,
    input  wire [ADDR_W-1:0] prog_addr,
    input  wire [      15:0] prog_data,

    // Shift register loading (ignored while running)
    input  wire              sr_we,
    input  wire [       7:0] sr_data,

    output reg  [       7:0] pins,
    output wire [ADDR_W-1:0] pc_out,     // for debug and for the testbench
    output wire              issuing     // an instruction issues this cycle
);

  localparam [1:0] OP_SET = 2'b00, OP_WAIT = 2'b01, OP_SHOUT = 2'b10, OP_JUMP = 2'b11;

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------

  // Program memory. Deliberately NOT reset: it stands in for the SRAM macro
  // that replaces it later, whose contents are undefined after power-up. The
  // rule instead is "nothing runs until the host has loaded a program", and
  // the formal proof leaves the contents fully arbitrary, so its properties
  // hold for every possible program. Resetting 32x16 flops would also cost
  // area (reset flops are bigger) for no benefit.
  reg  [      15:0] mem       [0:(1<<ADDR_W)-1];

  reg  [ADDR_W-1:0] pc;
  reg  [      13:0] wait_left;  // cycles still to wait after the WAIT's own cycle
  reg  [       7:0] sr;         // shift register for SHOUT

  // ---------------------------------------------------------------------------
  // Decode (combinational: the memory read has no latency)
  // ---------------------------------------------------------------------------
  wire [      15:0] instr = mem[pc];
  wire [       1:0] op = instr[15:14];
  wire [       2:0] pin = instr[13:11];
  wire [      13:0] imm = instr[13:0];

  // An instruction issues when running and no WAIT is still counting.
  assign issuing = run && (wait_left == 14'd0);
  assign pc_out  = pc;

  // WAIT n has already used one cycle (its own) when it issues, so n-1 are
  // left; WAIT 0 and WAIT 1 leave none.
  wire [      13:0] wait_load = (imm == 14'd0) ? 14'd0 : imm - 14'd1;

  // ---------------------------------------------------------------------------
  // Sequential logic
  // ---------------------------------------------------------------------------

  // Program memory writes: only while halted.
  always @(posedge clk) begin
    if (prog_we && !run) mem[prog_addr] <= prog_data;
  end

  always @(posedge clk) begin
    if (!rst_n) begin
      pc        <= {ADDR_W{1'b0}};
      wait_left <= 14'd0;
      sr        <= 8'hFF;  // all ones = an idle UART line if shifted out
      pins      <= PINS_RESET;
    end else if (!run) begin
      // Halted: get ready to start at address 0. Pins hold their level.
      pc        <= {ADDR_W{1'b0}};
      wait_left <= 14'd0;
      if (sr_we) sr <= sr_data;
    end else if (wait_left != 14'd0) begin
      // Inside a WAIT: count down, nothing else happens.
      wait_left <= wait_left - 14'd1;
    end else begin
      // Issue the instruction at pc.
      pc <= pc + {{(ADDR_W - 1) {1'b0}}, 1'b1};  // default: next word (wraps)
      case (op)
        OP_SET: pins[pin] <= instr[0];
        OP_WAIT: begin
`ifdef SEQ_MUTANT_WAIT
          // Negative control for the formal proof: a WAIT that is one cycle
          // too long. formal/seq.sby must FAIL when this is defined.
          wait_left <= wait_load + 14'd1;
`else
          wait_left <= wait_load;
`endif
        end
        OP_SHOUT: begin
          pins[pin] <= sr[0];
          sr        <= {1'b1, sr[7:1]};  // LSB first; refill with 1 (stop bits)
        end
        OP_JUMP: pc <= imm[ADDR_W-1:0];
        default: ;  // all four codes are used; kept for lint and clarity
      endcase
    end
  end

  // The JUMP target's unused upper bits are ignored by design.
  wire _unused = &{1'b0, imm[13:ADDR_W]};

  // ---------------------------------------------------------------------------
  // Formal properties (SymbiYosys: formal/seq.sby)
  // ---------------------------------------------------------------------------
`ifdef FORMAL
  reg f_past_valid = 1'b0;
  always @(posedge clk) f_past_valid <= 1'b1;

  // Start every trace in reset, then leave reset alone.
  always @(*) if (!f_past_valid) assume (!rst_n);
  always @(posedge clk) if (f_past_valid) assume (rst_n);

  // Independent model of "when may the next instruction issue": an
  // instruction issues exactly when `f_gap` reaches 0. A WAIT n loads
  // max(n,1)-1; every other cycle counts down. This restates the timing
  // contract from the header, not the RTL's own counter arithmetic.
  reg [13:0] f_gap;
  always @(posedge clk) begin
    if (!rst_n || !run) f_gap <= 14'd0;
    else if (f_gap != 14'd0) f_gap <= f_gap - 14'd1;
    else if (op == OP_WAIT) f_gap <= (imm > 14'd1) ? imm - 14'd1 : 14'd0;
    else f_gap <= 14'd0;
  end

  always @(posedge clk) begin
    if (f_past_valid && rst_n) begin
      // 1. Exact WAIT timing: issue happens iff the contract says it may.
      assert (issuing == (run && f_gap == 14'd0));
      // (inductive strengthening: the RTL counter equals the model)
      assert (wait_left == f_gap);
    end
  end

  // 2. Pins change only one cycle after a SET or SHOUT issued, and then
  //    only the addressed pin, to the documented value.
  always @(posedge clk) begin
    if (f_past_valid && $past(rst_n) && rst_n) begin
      if (!($past(issuing) && ($past(op) == OP_SET || $past(op) == OP_SHOUT)))
        assert (pins == $past(pins));
      if ($past(issuing) && $past(op) == OP_SET)
        assert (pins[$past(pin)] == $past(instr[0]));
      if ($past(issuing) && $past(op) == OP_SHOUT)
        assert (pins[$past(pin)] == $past(sr[0]));
    end
  end

  // 3. Halting: one cycle of run=0 puts pc back to 0 with no WAIT pending.
  always @(posedge clk) begin
    if (f_past_valid && $past(rst_n) && rst_n && !$past(run)) begin
      assert (pc == {ADDR_W{1'b0}});
      assert (wait_left == 14'd0);
    end
  end

  // 4. The program cannot change while it runs.
  (* anyconst *) reg [ADDR_W-1:0] f_addr;
  always @(posedge clk) begin
    if (f_past_valid && $past(run)) assert (mem[f_addr] == $past(mem[f_addr]));
  end

  // Covers: the interesting paths are reachable (a proof that only holds
  // because nothing can happen would be vacuous).
  always @(posedge clk) begin
    if (f_past_valid && rst_n) begin
      cover ($past(issuing) && $past(op) == OP_SHOUT && pins != $past(pins));
      cover ($past(issuing) && $past(op) == OP_JUMP && pc != $past(pc) + 1'b1);
      cover ($past(issuing) && $past(op) == OP_WAIT && $past(imm) == 14'd3 && wait_left == 14'd2);
      cover ($past(wait_left) == 14'd1 && issuing);
    end
  end
`endif

endmodule

`default_nettype wire
