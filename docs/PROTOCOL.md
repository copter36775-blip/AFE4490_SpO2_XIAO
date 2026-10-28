# Link protocol "A1"

`src/main.cpp` V1.0.0 ↔ `web/afe4490_studio.html` V1.0.0, over USB CDC at
115200 baud (the rate is ignored by CDC but the API wants one).

One pipe carries two interleaved things.

## Framing

**ASCII lines**, `\n` terminated, for commands and replies. Strictly 7-bit, so a
byte ≥ 0x80 never appears inside one.

**Binary sample frames**, which always begin `AA 55`:

```
AA 55 | TYPE(1) | LEN(2, LE) | PAYLOAD(LEN) | CRC16(2, LE)
```

`CRC16-CCITT`, polynomial `0x1021`, init `0xFFFF`, computed over
`TYPE .. PAYLOAD` inclusive.

Because `0xAA` cannot occur in the ASCII stream, a receiver switches between the
two with a two-byte look-ahead and no escaping or byte stuffing. The rule is:

* at `AA 55` → consume exactly `5 + LEN + 2` bytes as a frame;
* otherwise → consume up to the next `\n` as a line, but stop early if an `AA`
  appears (that is a frame starting, and the partial line is discarded).

### TYPE 0x01 — sample batch

| Offset | Size | Field |
|--------|------|-------|
| 0 | u32 LE | `seq` — sample index of the first sample in the batch |
| 4 | u8 | `count` — samples in this frame (1…8) |
| 5 | u8 | reserved, 0 |
| 6 | u16 LE | `rate` — effective sample rate in Hz (PRF ÷ decimation) |
| 8 | `count` × 6 × i32 LE | the samples |

Each sample is six signed values, in register order:

```
LED2 (RED), ALED2, LED1 (IR), ALED1, LED2-ALED2, LED1-ALED1
```

Sign-extended from the ADC's 22-bit two's complement, so the range is
−2 097 152 … +2 097 151 for ±1.2 V. Volts = `code × 1.2 / 2²¹`.

Eight samples per frame keeps a frame at 207 bytes, inside the 256-byte
`CFG_TUD_CDC_TX_BUFSIZE`, so the acquisition path never blocks on a stalled
host. When the FIFO cannot take a whole frame the firmware drops it and counts
it in `txdrop` rather than waiting.

A frame is sent when it is **full or 25 ms old**, whichever comes first. This
matters more than it looks: `loop()` iterates in microseconds while samples
arrive every 2 ms, so without the batching rule the ring never held more than
one sample and every frame carried exactly one — 500 USB transfers a second,
which is enough event-loop churn to visibly starve a browser's render loop.
Measured on hardware, batching takes it from 500 frames/s to 66.3 frames/s at
exactly 8.00 samples per frame.

### Text stream mode

`FMT:TEXT` replaces binary frames with CSV lines, so a plain serial monitor is
usable during bring-up:

```
D,<seq>,<led2>,<aled2>,<led1>,<aled1>,<led2-amb>,<led1-amb>
```

The console parses both.

---

## Commands

Case-insensitive up to the first comma. Unrecognised input is rejected outright —
the dispatcher is a whitelist, so a typo can never partially apply.

### Identity and status

| Command | Reply |
|---------|-------|
| `PING` | `AFE:PONG:<fw>` |
| `CAPS?` | `CAPS:PROTO=A1,FW=…,CHIP=AFE4490,…` |
| `STATUS?` | `ST:fw=…,prf=…,drop=…,vbat=…` (see below) |
| `REG?` | `REG:<aa>=<vvvvvv>` × 49, then `REG:END` |

`STATUS?` fields: `fw proto stream fmt prf duty numav dec seq rdy drop txdrop
miss spihz spidrive led1ma led2ma fs bpm vbat up`. It is also emitted once per
second unprompted while streaming.

`bpm` is the **board's own** pulse estimate, from a detector running on the IR
ambient-subtracted channel. It drives the green heartbeat LED and works with no
host attached; it is independent of the console's autocorrelation estimate.

### Streaming

| Command | Reply |
|---------|-------|
| `START` / `STOP` | `ACK:START` / `ACK:STOP` |
| `FMT:BIN` / `FMT:TEXT` | `ACK:FMT:<mode>` |
| `DEC:<1..64>` | `ACK:DEC:<n>` |

### Registers

| Command | Reply |
|---------|-------|
| `R:<aa>` | `REG:<aa>=<vvvvvv>` |
| `W:<aa>,<vvvvvv>` | `ACK:W:<aa>=<vvvvvv>` — the value **read back**, not the value sent |
| `WM:<aa>=<vvvvvv>[,<aa>=<vvvvvv>…]` | `ACK:WM:n=<n>,seq=<s>,flush=<0\|1>,prf=<hz>,<aa>=<vvvvvv>…` (V2.1.0) |
| `REGS?` | `REGS:<v00>,<v01>,…,<v30>` — all 49 from the driver's shadow, no SPI (V2.1.0) |
| `REGS?HW` | `REGSHW:<v00>,…,<v30>` — all 49 from silicon; the shadow is not touched (V2.1.0) |

Addresses and values are hex, no `0x`. `W:` reads the register back after
writing so the console can never drift from silicon; `CONTROL0` (0x00) is
write-only and echoes the written value instead.

`WM:` is a transaction of up to 48 pairs, and the only way a timing change
reaches the chip in one piece:

- Every pair is validated before anything is written. Addresses above 0x29 and
  the factory registers 0x27/0x28 are refused; one bad pair changes nothing.
- If any of 0x01–0x1D is in the batch, TIMEREN is cleared, the pairs are written
  in order, `TIM_CNT_RST` is pulsed, CONTROL1 (0x1E) is written **last**, and the
  timer is restored. The firmware then waits 60 ms for the switched-capacitor
  filters and discards everything converted meanwhile (`flush=1`).
- A `TX_REF` change waits 20 ms and also flushes. `SW_RST` in CONTROL0 performs a
  real software reset and clears the shadow.
- Gain and LED writes are not flushed (`flush=0`): the analog step response is
  left in the stream for whoever is looking for it.
- `seq` is the sequence number of the first sample shipped after the change took
  effect — frame header seq plus position — so a host can mark that exact sample.
- Each pair is echoed as read back from silicon (CONTROL0 echoes the value sent).

After `W:` or `WM:` the driver rebuilds its timing from the register shadow, so
`STATUS?` `prpct`, `RATES?` and the frame-header rate describe a hand-written
PRPCOUNT rather than the last `PRF:` request. Command lines may be up to 639
bytes.

### Timing

| Command | Reply |
|---------|-------|
| `PRF:<hz>[,<duty%>[,<settle>[,<adcrst>]]]` | `ACK:PRF:<hz>,<duty>,<settle>,<adcrst>,numav=<n>,swin=<cnt>,cwin=<cnt>,max=<hz>,seq=<s>` |
| `NUMAV:<0..255>` | `ACK:NUMAV:<n>` |
| `NUMAV:AUTO` | `ACK:NUMAV:AUTO=<n>` |
| `TIMER:<0\|1>` | `ACK:TIMER:<n>` |

`PRF` rewrites all 29 edge registers. `settle` and `adcrst` are in counts of the
4 MHz clock. Ranges: PRF 62…1000 Hz, duty 1…25 %, adcrst 2…16.

### Transmitter

| Command | Reply |
|---------|-------|
| `LED:<led1>,<led2>` | `ACK:LED:<a>,<b>,<x>mA,<y>mA` |
| `LEDRANGE:<0..3>` | `ACK:LEDRANGE:<n>,fs=<n>mA` |
| `TXREF:<0..3>` | `ACK:TXREF:<n>,fs=<n>mA` |

Codes are 0…255; current is `code/256 × full scale`, and full scale depends on
both `TXREF` and `LEDRANGE` (datasheet Table 6).

### Receiver

| Command | Reply |
|---------|-------|
| `TIA:<rf>,<cf>[,<sep>,<rf1>,<cf1>]` | `ACK:TIA:rf=<ohm>,cf=<pF>,sep=<n>` |
| `STAGE2:<en>,<gain>` | `ACK:STAGE2:<en>,<gain>,<x.y>dB` |
| `AMBDAC:<0..10>` | `ACK:AMBDAC:<n>uA` |
| `FILTER:<0\|1>` | `ACK:FILTER:<500\|1000>Hz` |

`rf` 0…7 = 500 k, 250 k, 100 k, 50 k, 25 k, 10 k, 1 M, open.
`cf` is the 5-bit field; a fixed 5 pF plus 5/15/25/50/150 pF per set bit.

### Power, reset, diagnostics

| Command | Reply |
|---------|-------|
| `PDN:<afe>,<rx>,<tx>` | `ACK:PDN:<a>,<r>,<t>` |
| `DEFAULTS` | `ACK:DEFAULTS` — the SpO₂ preset |
| `RESET` / `SWRESET` | `ACK:RESET` / `ACK:SWRESET`, then re-applies defaults |
| `DIAG` | `DIAG:<vvvvvv>`, then `DIAG:OK` or one `DIAG:FAULT:<name>` per flag |
| `AGC[:<targetPct>]` | `ACK:AGC:<OK\|LIMIT>,rf=…,led1=…,led2=…` |
| `SELFTEST[:<trials>]` | `ACK:SELFTEST:<errors>/<trials>,<PASS\|FAIL>` |
| `PINTEST` | `PIN:MISO=…`, `PIN:ADC_RDY=…`, `PIN:LOOPBACK=…`, `PIN:VERDICT=…` |
| `SPI:SET:<hz>` | `ACK:SPI:<actual>,req=<requested>` |
| `SPI:DRIVE:<0\|1>` | `ACK:SPI:DRIVE=<S0S1\|H0H1>` |
| `SPI:TEST` | `SPITEST:<hz>=<errors>/<trials>,<PASS\|FAIL>` per step, then `SPITEST:END:…` |

Commands that disturb the signal chain (`DEFAULTS`, `RESET`, `DIAG`, `AGC`,
`SELFTEST`, `PINTEST`, `SPI:TEST`) stop the stream, do their work, and restart it
if it was running.

### A note on `SPI:SET`

The reply carries **both** numbers because they routinely differ. The nRF52 SPIM
has only seven clocks on SPIM0/1/2 — 125 k, 250 k, 500 k, 1 M, 2 M, 4 M, 8 M —
and the BSP rounds a request *up* to the next one. `SPI:SET:200000` really means
250 kHz on the wire; `SPI:SET:16000000` really means 8 MHz, because 16 and 32 MHz
exist only on SPIM3. `STATUS?` reports `spihz` as the quantized truth, and the
console follows it rather than its own dropdown.

`SPITEST:END` fields: `BEST` (fastest error-free clock, 0 if none passed),
`SAFE` (one step below `BEST`), `ACTIVE` (what SCLK was left at — `SAFE`, for
margin), `DRIVE`, and `MAX` (the SPIM ceiling).

### Errors

`ERR:BAD_ADDR`, `ERR:BAD_VALUE`, `ERR:BAD_W_FORMAT`, `ERR:BAD_PRF`,
`ERR:BAD_NUMAV`, `ERR:BAD_LED`, `ERR:BAD_RANGE`, `ERR:BAD_TXREF`,
`ERR:BAD_TIA`, `ERR:BAD_STAGE2`, `ERR:BAD_AMBDAC`, `ERR:BAD_DEC`,
`ERR:BAD_FMT`, `ERR:BAD_SPI`, `ERR:UNKNOWN_COMMAND`.

From `WM:` (V2.1.0): `ERR:BAD_WM_FORMAT`, `ERR:WM_READONLY:<aa>` (an address
above 0x29, or 0x27/0x28), `ERR:BAD_VALUE:<aa>` (more than 24 bits),
`ERR:WM_TOO_MANY` (over 48 pairs). None of them writes anything. A host that
gets `ERR:UNKNOWN_COMMAND` for `WM:` is talking to older firmware and should
fall back to one `W:` per register.

### Unsolicited events

| Line | When |
|------|------|
| `EVT:BOOT:<fw>,proto=A1` | at startup, and again when a host asserts DTR |
| `EVT:SPI:<OK\|FAIL>,sclk=<hz>,drive=<S0S1\|H0H1>` | result of the boot-time self-test, with the real SCLK |
| `EVT:HINT:…` | advice when the boot self-test failed |
| `EVT:READY` | end of the boot banner — a good trigger for `REG?` |

---

## Notes for a future BLE transport

The frame was sized so it can move to a BLE characteristic without touching the
console's parser: 159 bytes fits inside a 185-byte ATT payload at a 247-byte MTU,
`seq` already lets the receiver detect loss, and the CRC survives a lossy link.
The console's `feed()` is transport-agnostic — point it at notification payloads
instead of the serial reader and everything downstream is unchanged.

`CAPS` advertises `BLE=0` today; flip it when the transport lands so a console can
offer the choice.
