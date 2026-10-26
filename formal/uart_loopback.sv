// Formal harness: uart_tx looped straight into uart_rx (bounded check).
//
// For any stream of bytes, written whenever the transmitter is idle (any data,
// any gaps), within the BMC depth:
//   - every byte the receiver reports is the oldest byte not yet received,
//   - the receiver never reports a framing error,
//   - every byte sent is received within one frame time of the sender
//     finishing its stop bit (none are lost).
`default_nettype none

module uart_loopback #(
    parameter integer CLKS = 6
) (
    input wire clk
);
  reg f_past_valid = 1'b0;
  always @(posedge clk) f_past_valid <= 1'b1;
  reg rst_n;
  always @(*) assume (rst_n == f_past_valid);

  (* anyseq *) reg want;
  (* anyseq *) reg [7:0] wdata;

  wire line, busy;
  wire start = rst_n && want && !busy;
  uart_tx #(.CLKS_PER_BIT(CLKS)) u_tx (
      .clk(clk), .rst_n(rst_n), .data(wdata), .start(start), .tx(line), .busy(busy)
  );

  wire [7:0] rdata;
  wire rvalid, ferr;
  uart_rx #(.CLKS_PER_BIT(CLKS)) u_rx (
      .clk(clk), .rst_n(rst_n), .rx(line), .data(rdata), .valid(rvalid), .frame_err(ferr)
  );

  // Scoreboard: at most two bytes can be in flight (one on the line, one
  // still being decided by the receiver).
  reg [7:0] q0, q1;
  reg [1:0] n;
  reg [7:0] since_done;  // cycles the transmitter has been idle (0 while sending)
  reg [3:0] received;
  always @(posedge clk) begin
    if (!rst_n) begin
      n <= 0;
      received <= 0;
      since_done <= 0;
    end else begin
      case ({start, rvalid})
        2'b10: begin
          if (n == 0) q0 <= wdata;
          else q1 <= wdata;
          n <= n + 1'b1;
        end
        2'b01: begin
          q0 <= q1;
          n  <= n - 1'b1;
        end
        2'b11: begin
          if (n == 1) q0 <= wdata;
          else begin
            q0 <= q1;
            q1 <= wdata;
          end
        end
        default: ;
      endcase
      if (rvalid && received != 4'hf) received <= received + 1'b1;
      since_done <= (busy || start) ? 8'd0 : (since_done == 8'hff ? 8'hff : since_done + 1'b1);
    end
  end

  always @(posedge clk) begin
    if (rst_n) begin
      if (rvalid) assert (n != 0 && rdata == q0);
      assert (!ferr);
      assert (n <= 2);
      // nothing lost: once the line has been idle for a frame, all was received
      if (since_done > 10 * CLKS) assert (n == 0);
    end
  end

  always @(posedge clk) begin
    if (rst_n) cover (received == 4'd2);  // two bytes back to back made it through
  end
endmodule

`default_nettype wire
