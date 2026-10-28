# AFE4490_XIAO

Register-level control and live plethysmography for the **TI AFE4490** pulse-oximetry
front end, driven by a **Seeed XIAO nRF52840 Sense**.

Sibling project to `HECG01` (ADS1292R): same board, same toolchain, same
"clinical instrument dark" console language — different sensor.

```
src/AFE4490_Regs.h    complete register map, bit fields and setting tables
src/AFE4490.h/.cpp    the driver: SPI, timing generator, AGC, diagnostics
src/main.cpp          link protocol A1, streaming, command dispatcher
web/afe4490_studio.html   the console — single file, Web Serial, no build step
tools/                bring-up and verification scripts
docs/PROTOCOL.md      the wire protocol, in full
```

---

## 1. Wiring

Two board revisions, two firmwares. `platformio.ini` picks which `main.cpp`
compiles, so both stay buildable from one source tree.

### Rev 2 — current, connector **H1** (`pio run -e v2_ble`, the default)

| H1 | Signal | XIAO | Note |
|----|--------|------|------|
| 1  | VCC      | **3V3** | via R1 10 k; see the supply note below |
| 12 | GND      | **GND** | |
| 7  | CS0      | **D0**  | |
| 8  | MOSI     | **D1**  | |
| 9  | SCK      | **D2**  | |
| 10 | MISO     | **D3**  | |
| 11 | DDRY     | **D4**  | `ADC_RDY`, one 4 MHz clock wide, rising edge |
| 6  | START    | —       | **not wired on rev 2** |
| 5  | PDWN     | —       | **not wired on rev 2** |
| 4/3/2 | DIAG_END, LED_ALM, PD_ALM | — | not connected; the same faults are readable over SPI in `DIAG` (0x30) |
| —  | SW1      | **D5**  | momentary to GND, internal pull-up |

With no RESET wire, every reset goes through `CONTROL0`'s `SW_RST` bit
instead. The driver takes `0xFF` for a pin that does not exist and
`hardwareReset()` falls through to `softwareReset()` — it reaches the same
state, just over SPI rather than over a wire. Nothing in the console changes.

> D5 used to be RESET. Driving it as an output now would fight the button's
> pull-up, which is why the driver has to be told the pin is absent rather
> than simply left unused.

### Rev 1 — the first wiring (`pio run -e v1_usb`)

| H3 | Signal | XIAO | | H3 | Signal | XIAO |
|----|--------|------|-|----|--------|------|
| 11 | DDRY   | **D0** | | 7 | CS0   | **D4** |
| 10 | MISO   | **D1** | | 6 | START | **D5** (AFE `RESET`) |
| 9  | SCK    | **D2** | | 5 | PDWN  | **D6** |
| 8  | MOSI   | **D3** | | | | |

Those SPI pins are **not** the XIAO's default SPI bus (D8/D9/D10), so the driver
runs on its own **SPIM1** instance. SPIM3 already backs the global `SPI` object and
SPIM2 backs `SPI1` (the on-board IMU). SPIM1 shares a peripheral ID with TWIM1, so
**do not use `Wire1`** in this project.

### Supply note — read this before wondering why RED is weak

The breakout is fed from the XIAO's 3V3 rail, which becomes `LED_DRV_SUP`.
The datasheet requires `3.0 V − (V_LED + V_cable) > 1.4 V` of headroom for the
current source, so the firmware defaults `TX_REF` to **0.50 V** rather than the
0.75 V reset value (datasheet Table 6 footnote: *"For a 3-V to 3.6-V supply, use
TX_REF = 0.5 V"*).

Even so, a red emitter at V_f ≈ 2.0 V leaves only ~1.3 V — marginally under spec.
Expect the red channel to run out of drive before the IR channel does. If you need
full LED range, give `LED_DRV_SUP` / `TX_CTRL_SUP` their own 5 V rail and switch
`TX_REF` to 0.75 V or 1.0 V in the console's **Tx / LED** tab.

---

## 2. The link, and the button on D5 (rev 2)

Rev 2 carries protocol A1 over **either** USB CDC or a BLE Nordic UART
Service. Both frames are 207 bytes, sized so one travels in a single
notification once the ATT MTU is negotiated up to 247 — but they are not the
same frame, because the two links are not short of the same thing:

| | Type | Channels | Per sample | Samples | Shipped when |
|---|---|---|---|---|---|
| **USB** | `0x01` | 6 × int32 | 24 B | 8 | full, or 25 ms old |
| **BLE** | `0x02` | 4 × int24 | 12 B | 16 | full, or 200 ms old |

The compact frame leaves out `LED2-ALED2` and `LED1-ALED1`, because they are
*exactly* the difference of the raw pair — on the bench,
1.1811 V − 0.4597 V = 0.7214 V, to the last digit the console displays. The
console subtracts them back in `handleFrame()`. What remains is four
conversions, and the converter is 22-bit, so three bytes each is lossless
rather than a compromise. Same 207-byte packet, twice the samples in it.

The button cycles which link the device is presenting:

| State | LED | How you get there |
|-------|-----|-------------------|
| **AUTO**, nothing plugged | **white** | boot, or a long press |
| **AUTO**, USB enumerated | **red** solid | plug the cable |
| **BLE**, advertising | **blue** blinking, 2 Hz | double press from white |
| **BLE**, connected | **blue** solid | a central connects |
| **IDLE** | **white** | double press from red or from blue |

```
AUTO (white) --double--> BLE (blue blink) --connect--> BLE (blue solid)
AUTO (red)   --double--> IDLE (white)
BLE (either) --double--> IDLE (white)
IDLE         --double--> BLE (blue blink)
```

On both live links a detected beat replaces the base colour with a **short
green flash**, so the LED blinks in time with the pulse whether the host is
USB or BLE, and whether or not anything is streaming.

**IDLE is a latch** — it holds white even with a cable in, which is the point
of it, but that means it needs a way out that does not depend on remembering
which state you are in. Three of them: another double press (→ BLE), a
**long press** of 1 s (→ AUTO), or plugging/unplugging the cable.

Commands `BLE:ON`, `BLE:OFF`, `IDLE` and `LINK?` do the same from a console.
`STATUS?` reports `link`, `mode`, `blemtu` and `bledrop`.

> The radio is initialised at boot but **advertises nothing** until you ask.
> A board on the bench is not discoverable by accident.

### What BLE costs, and the two ways it goes wrong

Both of these were learned on HECG01 first and written down at the top of its
`main.cpp`. This firmware repeated both mistakes before they were fixed, so
they are recorded here too.

**1. Never let the radio block the acquisition loop.** Bluefruit's
`BLEUart::write()` goes through `notify()`, which waits on a semaphore for up
to `BLE_GENERIC_TIMEOUT`. In a loop that also has to drain a 500 Hz ADC that
wait is fatal — the ring overflows and `drop` climbs into the thousands while
the trace stutters. The fix is to own the characteristic rather than borrow
`BLEUart`, and call `sd_ble_gatts_hvx()` directly: it returns
`NRF_ERROR_RESOURCES` immediately, so the packet can be re-queued and retried
after the next connection event. Frames go into a 16-slot queue that drops
rather than waits, and console text is capped at half the queue so it can
never crowd the waveform out.

**2. Let the central own the negotiation.** Chrome on Windows runs the MTU,
data-length, PHY and connection-parameter procedures itself. A competing
procedure from the peripheral is the most likely cause of a link that drops
with HCI 0x28. The connect callback sets TX power and nothing else.

**3. Do not throw a sample away because the radio blinked.** The first fix
above stopped the loop stalling, but the batch was still popped off the ring
*before* the frame was handed to the transport — so a queue that was briefly
full cost real samples, and `DROP` climbed anyway. `loop()` now copies the
batch without moving `ringTail` and commits only once `sendSampleFrame()`
reports the bytes went somewhere. A stalled central costs buffer depth
instead, and the ring is 256 samples deep — two seconds at 250 Hz.

### Throughput, measured rather than assumed

Guessing here produced two wrong numbers in a row, so `tools/ble_probe.py`
connects over the same NUS from Python and reports what the link actually
carried. On this board and this Windows machine:

| Rate | On the air | Packets | CRC errors | Samples lost |
|------|-----------|---------|-----------|--------------|
| 125 Hz (`DEC:4`) | 1.84 kB/s | 7.8/s | 0 | 0 |
| 250 Hz (`DEC:2`) | 3.46 kB/s | 15.6/s | 0 | 0 |
| **500 Hz** (`DEC:1`) | **6.69 kB/s** | 31.2/s | **0** | **0** |

`BLESTAT?` during those runs: `mtu=244,dle=251,ci=48,queue=0/16,backpressure=0`.
Data-length extension **is** negotiated, so a 207-byte notification is one
link-layer packet and any arithmetic about 20-byte fragments does not apply.

**And none of it predicted what the browser does.** That table is a Python
client. `tools/chrome_ble_probe.py` drives the actual console in actual Chrome
over the DevTools protocol, against the same board minutes later:

| | bleak (Python) | Chrome |
|---|---|---|
| notifications sustained | 31 /s | **8.9 /s** |
| link silent | never | **30–60 % of the run** |
| longest single silence | — | **~2.2 s**, many times a minute |
| CRC errors | 0 | 0 |

The link goes **completely quiet for about 2.2 seconds at a time** and then
catches up in a burst. Over two minutes it still delivers 100 % of a 125 Hz
stream with zero CRC errors, so nothing is wrong with the radio or the data —
it simply arrives in lumps. `--norender`, which kills the animation loop and
nothing else, produces an identical stall pattern, so it is not the page
competing for the main thread.

**It was not Chrome either, and this document said it was.** The operator found
the actual cause by elimination: a Bluetooth *mouse* was paired to the same
Windows adapter, and with it disconnected the stalls disappear entirely. That
fits — an HID peripheral holds its own connection with its own interval, and
the adapter interleaves them. The table above is a confound, not a comparison:
the Chrome runs opened a browser window and involved moving the mouse, the
bleak runs did not touch it.

Which leaves the fix unchanged and the conclusion better founded. You cannot
assume a clean adapter — most machines have a mouse, a headset or a keyboard on
theirs — so the device buffers through contention rather than depending on its
absence. What is now known is that the stalls are *external* contention, so a
quiet adapter gets the full rate with no buffering penalty at all.

Two buffers, sized from that measurement:

* **The firmware ring is 2048 samples** — 4 s even at 500 Hz. At 256 it was
  2.05 s at 125 Hz, which is why losses were small but never zero, and 1.02 s
  at 250 Hz, which is why they were bad.
* **The console's playout buffer is 2.6 s on BLE** and 90 ms on USB. It also
  no longer re-arms its priming wait when it runs dry: that used to add 2.8 s
  of frozen sweep on top of every 2.2 s stall, so the recovery was worse than
  the fault. Measured after: **0.0 % dry over 75 s** with 37.7 s of radio
  silence in it.

The console opens a BLE link at **125 Hz**, which needs 7.8 notifications/s
against the 8.9 sustained through that contention. 250 Hz needs 15.6 and does not fit, whatever
the Python table says. `bleAutoBackoff()` halves the rate again if the firmware
ever reports a loss. A plethysmogram carries nothing above ~10 Hz, so even the
`DEC:8` floor is six times what the signal needs.

#### The rate has to be exact, or the sweep is thrown away

500 Hz at `DEC:8` is 62.5 Hz. The binary frame header carries whole hertz and
rounds it to 62; `STATUS?` reports `prf` and `dec` separately, so the console
computes the exact 62.5. Both were being adopted unconditionally, so the two
overwrote each other several times a second — and every flip recomputed the
sweep window from the new rate, which reallocates the column buffers and
throws away everything on screen. Measured: **16 resizes across 8 frames**,
about sixteen wipes a second at that rate. On the board it looked like a trace
that restarted constantly and never flowed, and it only appeared once the
automatic back-off reached `DEC:8`, because every other decimation divides 500
exactly.

`STATUS?` is now authoritative; the frame header only speaks up when it
disagrees by more than rounding, and `setSweep()` ignores a window change
smaller than 2 %. `parser_test.py` check 14 covers it — and asserts on the
*number of resizes*, not the final window size, because the window ends up
back where it started either way. The first version of that test passed
against the bug it was written for.

The display also refuses to fall more than 1.5 s behind its target: past that
it discards the stale samples and jumps. The rate corrector runs at most 12 %
fast, so a backlog it did not catch early takes minutes to drain, and a
monitor fifteen seconds out of date is not a monitor.

#### Latency against smoothness

The price of buffering is latency, and which way to trade it depends on what
you are doing, so **Acquire → Link → Display buffer** offers 0.4 s (live, the
sweep pauses through the worst of each stall), 1.5 s (default) and 2.6 s
(never pauses, sits 2.6 s behind). **`lag` in the status bar says where the
display actually is.** The SpO₂ and pulse numbers are
computed straight from the ring, so they are only ever as late as the data.
Over USB `lag` sits at 90 ms and none of this applies.

`BLESTAT?` reports MTU, data length, connection interval, queue depth,
backpressure events, notification count, measured bytes/s and drops, and its
counters reset on `START` so they describe one run rather than one power-on.
`drop` in `STATUS?` is the number to watch: it should stay at zero.

Sampling itself is never disturbed. The AFE generates its own pulse timing and
the MCU only reads result registers when `ADC_RDY` fires, so radio work costs
buffer depth, not sample timing.

### The battery reading was fiction

`vbat` reported **4.48 V** for a 3.7 V cell — above what lithium polymer
chemistry can reach, which is what gave it away rather than any measurement.

VBAT reaches P0.31 through a 1 M / 510 k divider that P0.14 enables by going
low. The source impedance the SAADC sees is 1 M ∥ 510 k = **338 kΩ**, and the
Arduino core defaults to a **3 µs acquisition window**, which Nordic rates for
a **10 kΩ** source — thirty-four times over. The sampling capacitor never
finished charging, so every result was dragged toward the charge already on it.

| | |
|---|---|
| `analogSampleTime(40)` | 40 µs is the longest the SAADC offers, rated to 800 kΩ |
| discard the first conversion | with the divider off, P0.31 floats up toward VBAT through the 1 M leg |
| `analogOversampling(8)` + 4 reads | burst mode, so one `analogRead` is already an average |
| `analogCalibrateOffset()` | once, on the first read |

Measured after: **4.155 V on the charger, 4 mV spread over eight reads.**

Absolute accuracy still rests on a band-gap reference and two resistors with
their own tolerance, so **`BATCAL:<millivolts>`** trims it against a meter —
Diag → Battery → *Meter reads*. `BATCAL:RESET` returns to the nominal divider.
`BAT?` reports raw counts, millivolts, the percentage estimate, the charge
state and the trim in force.

**Charging.** `~CHG` (P0.17) is the charger's open-drain status line, pulled low
while charging. It is released both when the cell is full *and* when there is no
input at all, so VBUS is read alongside it to tell those apart:

| VBUS | ~CHG | State |
|------|------|-------|
| 0 | — | **on battery** |
| 1 | low | **charging** — the status bar shows ⚡ |
| 1 | high | **charged**, charger idle |

The percentage is interpolated from a resting-voltage curve, which is coarse and
optimistic under load; everything that displays it says *estimated*. Charge
current is left exactly as the board ships — `BATCHG:50` and `BATCHG:100` drive
HICHG (P0.13) if you want to choose, and for a 200 mAh cell those are 0.25 C and
0.5 C.

### One console line can be longer than one notification

`STATUS?` reached 230 characters once the battery fields went in, against the
242 bytes a notification carries, and it had already been silently truncated
once before — losing `bledle` and `bleci`, the two counters that explain a slow
radio, while still looking like a well-formed line. `sendLine()` now splits a
long line across packets and the console reassembles on the newline. Capacity
for the whole line is checked before any of it is queued: half a line on the
wire would join itself to the next one and corrupt that too.

---

## 3. Build and flash

```bash
pio run                       # build rev 2 (default)
pio run -t upload             # flash rev 2
pio run -e v1_usb -t upload   # go back to rev 1
pio device monitor            # plain serial, works with FMT:TEXT
```

**No COM port is pinned.** PlatformIO finds the board by its USB VID:PID, so
any XIAO nRF52840 enumerates and uploads without editing `platformio.ini` —
which matters the moment there is more than one board on the bench. When
several are attached at once, name the one you mean for that command:

```bash
pio run -t upload --upload-port COM7
pio device monitor --port COM7
```

| | Flash | RAM |
|---|---|---|
| rev 1, USB only | 10.3 % | 3.9 % |
| rev 2, USB + BLE | 19.8 % | 28.8 % |

Rev 2's RAM figure is mostly the 256-sample acquisition ring (6 kB) and the
SoftDevice; rev 1 links no radio at all.

### Settings that survive a power cycle

**The AFE4490 has no non-volatile memory.** All 49 registers are volatile —
remove power and every one returns to its datasheet reset value, and there is
nothing on the chip to write a setting into. Persistence lives on the MCU:
`SAVE` writes a 188-byte profile into the nRF52840's internal flash through
LittleFS, and boot replays it into the AFE once the chip is alive.

What is stored is chosen, not dumped wholesale:

| Range | Held | Why |
|-------|------|-----|
| `0x01`–`0x26` | yes | timing, gains, LED drive, CONTROL1/2, spares |
| `0x29` | yes | ALARM |
| — | yes | PRF, duty, settle, ADC reset, NUMAV, decimation, stream format, SPI clock, drive strength |
| `0x00` | **no** | CONTROL0 is write-only and self-clearing. A captured value would replay `SW_RST` at every boot. |
| `0x27`, `0x28` | **no** | factory trim: read-only, and different on every part |
| `0x2A`–`0x30` | **no** | results and diagnostics, not settings |

Registers are replayed verbatim rather than recomputed from PRF and duty, which
would quantise a second time and could land a count away from what was actually
measured with. Boot applies `applyDefaults()` first and the profile over it, so
a register added to the map by a later firmware still starts from something sane
instead of from whatever survived. A CRC guards the record and a failed check is
treated as no profile at all.

```
SAVE       store the live configuration        ACK:SAVE:bytes=188,regs=39
LOAD       apply the stored one now            ACK:LOAD:prf=250,dec=5
PROFILE?   what is stored, and whether boot applied it
REBOOT     restart as a power cycle would
FACTORY    erase it and return to the preset
```

`DEFAULTS` and `FACTORY` are deliberately different: **`DEFAULTS` resets the
chip and leaves the stored profile alone**, so the next power-up brings the
saved settings back; `FACTORY` is the one that forgets. A reset that silently
un-did itself on the next boot would be worse than no reset at all.

Verified on hardware: PRF 250, LED codes 137/201, decimation 5 and CSV output
were saved, `REBOOT` restarted the board, and it came back with `prf=250, dec=5,
fmt=TEXT, LEDCNTRL=0x0189C9`. `DEFAULTS` then left the profile intact, `LOAD`
brought it back, and after `FACTORY` a `LOAD` answered `ERR:NO_PROFILE`.

### Sample rate, and the rate you actually get

`PRF = 4 MHz / (PRPCOUNT + 1)` with PRPCOUNT an integer, so the rate requested
and the rate delivered are not always the same number — ask for 300 Hz and the
hardware runs at 300.008. Sixteen values in the 62–1000 Hz range divide 4 MHz
exactly, and metrology work should prefer them:

```
64  80  100  125  128  160  200  250  256  320  400  500  625  640  800  1000 Hz
```

`STATUS?` now reports `prpct` alongside `prf` so the host can compute the exact
rate instead of trusting the request, and `RATES?` reports the clock, the
PRPCOUNT limits, the averaging ceiling at the current rate and the streamed rate
in millihertz. The streamed rate is `PRF / decimation`, decimation 1–64; the AFE
still converts at the full PRF either way, so the on-board pulse detector and
the LED heartbeat keep working at any stream rate.

Averaging is bounded by the conversion time: each conversion takes 50 µs and
every average must fit inside its own quarter of the period, so
`NUMAV_max = ⌊(PRPCOUNT+1)/4/200⌋ − 2`, capped at 15. Left on automatic the
firmware keeps it at that ceiling.

### PRF and SPS

The datasheet says **PRF** because this is a pulsed system: it is the rate at
which the four-phase *sequence* repeats. **SPS** is what comes out. They are
numerically equal here — one repetition yields one sample of each channel — but
they count different things, and the ADC does **four conversions per period**.
At PRF 2500 that is 10 000 conversions a second at 50 µs each, which is half of
every second, and it is why the averaging ceiling collapses faster than the rate
rises. The console shows *Converting* and *Streaming* separately for the same
reason: decimation changes only the second.

### A window in seconds, a buffer in samples

The console's analysis ring held 4096 samples — 8 s at 500 Hz and **1.6 s at
2500**. Pulse rate needs 2.5 s of history and SpO₂ asks for 4, so above about
1500 Hz both quietly returned nothing and the cards went blank while the trace
looked perfect, because the sweep is fed live and never touches that ring.

The ring is 32768 samples now: 6.7 s at the 4901 Hz ceiling, 262 s at 125, about
1 MB across the eight channels. Fixed rather than rate-following, because
reallocating those arrays whenever the rate moved is what wiped the sweep
sixteen times a second the last time something here tracked `stats.rate`.

Every estimator now names what it is waiting for instead of showing a dash:
*filling the window, 1.6 of 2.5 s* reads differently from *no pulsatile
component — is the probe on?*, and only one of them is worth walking to the
bench for. `parser_test.py` check 17 drives a synthetic 72 bpm pleth at 125,
500, 1000, 2500, 4000 and 4901 Hz and insists both numbers come out with R
within 0.06 of 0.60 — it fails on the old 4096-sample ring with exactly the
message the board showed.

### How high the rate actually goes

The ceiling used to be 1000 Hz, which was a number in the driver rather than a
property of anything. PRPCOUNT bottoms out at 800, so the chip nominally reaches
**5000 Hz** — and it does not work there. A quarter of the period must hold a
200-count conversion after the ADC reset pulse *and* a 200-count sample window,
and at 5000 Hz the quarter is exactly 200 counts. Both fall short and the ADC
returns a conversion it never finished, silently, with a plausible number in the
result register.

`AFE4490::maxPrf(adcRst)` computes the limit instead of tabulating it, because it
moves with the reset width: **4901 Hz** at the default 4 counts. `CAPS?` reports
it as `PRFMAX` and the console reads its input's ceiling from there. `ACK:PRF`
now also reports `swin` and `cwin`, the windows actually achieved — the clamps
were silent before.

> A PRPCOUNT table circulating for this part gives 5000 SPS as PRPCOUNT 79 and
> 1000 SPS as 399. Those imply a **400 kHz** clock; the AFE4490's is **4 MHz**,
> so they are out by ten. The same table's lower rows (500 → 7999, 250 → 15999)
> are right. Correct values: 5000 → 799, 4000 → 999, 2000 → 1999, 1000 → 3999.

**What each link carries**, measured with `tools/rate_sweep.py` at decimation 1,
five seconds per rate. A row is clean only if `drop` stayed at zero:

| PRF | Converted | Received | On the wire | Dropped |
|-----|-----------|----------|-------------|---------|
| 1000 | 947 Hz | 1001 Hz | 25.9 kB/s | 0 |
| 2000 | 1996 Hz | 1999 Hz | 51.7 kB/s | 0 |
| 3000 | 3209 Hz | 3002 Hz | 77.7 kB/s | 0 |
| **4000** | 3651 Hz | **3998 Hz** | **103.5 kB/s** | **0** |
| 4500 | 4472 Hz | 2581 Hz | 66.8 kB/s | 8630 |
| 4900 | 4896 Hz | 2348 Hz | 60.8 kB/s | 12510 |

*Converted* is `rdy`, the ADC_RDY count, so it tracks the AFE regardless of what
shipped — the last two rows show the chip working while the link fails. Over
Bluetooth, with the 12-byte compact frame: **750 Hz clean at 9.0 kB/s**;
1000 Hz loses nothing but raises backpressure tenfold and starts shedding
console text from the queue.

#### The compact frame is not just for Bluetooth

At 4901 Hz, 24 bytes a sample is 118 kB/s and the CDC pipe on this board was
measured to carry about 105 — which is why the first sweep stopped at 4000 Hz.
`CFG_TUD_CDC_TX_BUFSIZE` is a bare `#define` in the BSP with no `#ifndef`
guard, so a build flag cannot raise it. The byte rate is the lever that is
actually available, and the format for it already existed.

The compact frame is **lossless**: the converter is 22-bit, so three bytes
hold it exactly, and the two ambient-subtracted channels are the difference of
the raw pair, which the console recomputes. `useCompactFrame()` therefore picks
it on Bluetooth always, and on USB above **2000 Hz**. Below that the full frame
costs nothing and the rev 1 consoles, which only know type `0x01`, keep working.
`FRAME:AUTO|FULL|COMPACT` overrides it, deliberately session-only — a stored
`COMPACT` would make an older console show nothing with no clue why.

Re-measured after the change, with `rate_sweep.py` now counting the bytes that
were really on the wire rather than assuming 24 a sample:

| PRF | Received | On the wire | Frame | Dropped |
|-----|----------|-------------|-------|---------|
| 500 | 502.0 Hz | 13.0 kB/s | `0x01` | 0 |
| 2000 | 1999.8 Hz | 51.7 kB/s | `0x01` | 0 |
| 3000 | 3000.8 Hz | 38.8 kB/s | `0x02` | 0 |
| **4901** | **4901.9 Hz** | **63.4 kB/s** | `0x02` | **0** |

3000 Hz costs *less* bandwidth than 2000 because it crosses the threshold.

So: **USB carries the whole range, BLE 750 Hz.** Decimation is applied after
conversion, so the AFE can run at 4901 Hz while the link carries 250 — the
on-board pulse detector and the LED heartbeat keep working at the full rate
either way. The console warns when the streamed rate exceeds what that
transport was measured to carry.

#### Changing the rate without breaking the picture

Three separate things made a rate change look like a fault, and only the first
was obvious:

**The sequencer was rewritten while it was running.** `applyTiming()` wrote
about thirty timing registers with `TIMEREN` still set. The engine reads the
phase boundaries continuously, so for the length of the burst it sampled and
converted at instants belonging to neither the old setting nor the new one, and
shipped those results as ordinary samples. It also never waited afterwards,
though moving the sampling instants is a change to the signal chain and each of
the four switched-cap filters needs more than 3 ms to settle after one.
`applyTiming()` now stops the sequencer, rewrites, restarts the period counter
through `TIM_CNT_RST`, waits 60 ms, and `flushAcquisition()` throws away
everything the transition produced.

**The console reset paths had drifted.** *Apply timing* called `resetBuffers()`
and the new rate menu did not, so picking a rate from the list left the ring
holding samples at the old rate while the playout target was scaled for the new
one. All three controls now go through `applyRateChange()`.

**And then the fix was worse than the bug.** `applyRateChange()` called
`resetBuffers()`, which calls `chart.reset()` — so every rate change wiped the
trace. It did not need to: `allocate()` sizes the column arrays by **pixel
width**, and `visible` is used for exactly one thing, how far the cursor
advances per sample. A rate change is a change of *speed*, not of anything
already drawn. `setSweep()` now updates `step` alone, and `applyRateChange()`
re-seeds the biquads instead of zeroing them — fed a settled DC, a high-pass
sits at `x1 = x2 = x0, y1 = y2 = 0`, so seeding that skips the two-second
charge that otherwise swings the AC traces through a transient shaped like a
signal.

Measured on hardware, changing 500 → 2500 → 4901 → 1000 → 500 mid-stream:
**0 sequence gaps, 0 CRC errors, 0 drops** at every step. `parser_test.py`
check 18 asserts the columns survive, and fails with `127 -> 0` against the
old behaviour.

---

## 4. The console

Open **`web/afe4490_studio.html`** in **Chrome or Edge** — double-clicking the
file is enough, there is no server and no build step. Firefox and Safari do not
implement Web Serial.

Press **Connect over USB** and pick the XIAO's port. If the SPI link is healthy
the console starts acquisition by itself; if it is not, a banner across the top of
the plots says so and offers to run the pin probe:

![fault banner](studio_faultbanner.png)

A dead bus is never silent here — the charts print the reason instead of drawing an
empty grid, and the status bar's rate stays honest rather than deriving a nonsense
value from a register that reads zero.

| Tab | What it holds |
|-----|---------------|
| **Acquire** | PRF, duty cycle, settle delay, ADC reset width, averaging, stream format, decimation, SPI clock and drive strength, presets, auto-gain, CSV export |
| **Tx / LED** | LED1/LED2 drive codes with live mA readout, current range, `TX_REF`, H-bridge vs push-pull, per-domain power-down |
| **Rx / Gain** | R_F, C_F, separate-gain mode, ambient cancellation DAC, stage-2 gain, anti-alias corner, full-scale I_PD and the R_F·C_F settling check |
| **Timing** | Live phase map drawn from registers 0x01–0x1D, plus every edge register in counts and µs |
| **Registers** | All 49 registers, hex edit, and a bit-field decoder with named enums for every field |
| **Capture** | Finite capture and analysis: record or snapshot, 1–6 plots, time / spectrum / histogram, statistics, CSV and PNG export |
| **Diag** | Sensor/cable diagnostics, SPI self-test, pin probe, SPI clock sweep, and a raw command console |

Two views, switched from the top bar. **Monitor** is the live sweep: five charts,
each toggleable from **Acquire → Display** — the ambient-subtracted
plethysmograph, the 0.5–5 Hz pulsatile band, the raw LED-on phases, the LED-off
**ambient** level, and an FFT of the IR AC. Vitals: SpO₂, pulse rate, perfusion
index and ADC headroom. **Capture** freezes a finite block and analyses it, the
way the EVM's *ADC Capture & Analysis* page does.

Every trace gets its own auto-scaled lane by default. This matters more than it
sounds: RED sits near 0.48 V and IR near 0.58 V, so a shared y axis spends its
whole range on the gap between them and squashes both pulsatile waveforms into
flat lines. *Channels → Overlaid* restores a shared axis if you want one.

Keyboard: `space` start/stop, `[` / `]` shrink/grow the sweep window, `c` capture.

### Two consoles

| File | Firmware | What it is |
|------|----------|------------|
| `web/afe4490_studio.html` | rev 1 | **Stable.** Monitor and Capture. The one to reach for when the question is about the hardware. |
| `web/afe4490_studio_V2.html` | rev 1 or 2 over USB | Everything the stable console has, plus a **Math** view. |
| `web/afe4490_studio_V3.html` | rev 2, USB **or BLE** | V2 plus a Bluetooth transport. Same protocol, same parser, same everything else. |
| `web/afe4490_studio_V4.html` | rev 2, firmware **V2.1.0**, USB or BLE | V3 plus the **Register Lab** and capture by count, duration or around a register write. Still works with older firmware, without transactions. |

Each is a superset of the one above it; none of them forks the device side.
The firmware sends identical protocol A1 frames over USB CDC and over BLE, so
V3 does not know or care which transport it is on.

> **BLE needs the console served over `http://localhost`.** Chrome treats a
> `file://` origin as trustworthy enough for Web Serial but not for Web
> Bluetooth. From the `web` folder: `python -m http.server 8000`, then open
> `http://localhost:8000/afe4490_studio_V3.html`. USB works straight from the
> file either way, and the BLE button explains this rather than just failing.

### The Register Lab (V4)

`web/afe4490_studio_V4.html` adds a fourth view, **Register Lab**, for working at
register level while watching what the signal does about it. It takes the whole
window; the rail comes back with the other views.

| Pane | What it does |
|------|--------------|
| **Map** | All 49 registers in the datasheet's groups. Committed values, staged (amber), in flight (violet), and the six result registers live from the stream (mint — no SPI reads). Dots flag rule-check findings and anything not at its reset value. Filter by address, name or field name; ↑/↓ walks it. |
| **Bit fields** | 24 clickable bits coloured by field, reserved bits hatched and locked, hex/dec entry, and every field with its own control and its meaning in engineering units: counts → µs and % of T, PRPCOUNT → exact PRF, R_F → full-scale µA with stage 2, R_F·C_F → τ, LED codes → mA at the current range and TX_REF, NUMAV against equation 5. |
| **Timing** | One pulse-repetition period to scale with all 14 edge pairs. Drag an edge, Shift-drag a pulse, wheel to zoom, drag empty space to pan, double-click to fit. The magnet snaps to quarter boundaries and other edges (Alt drags free). Staged edges leave a dashed ghost of the committed position; violations are outlined. Exact counts in the table below. |
| **Generator** | The firmware's `applyTiming()` arithmetic for any integer PRPCOUNT, so every 4 MHz/(N+1) rate is reachable, not only whole hertz. Reproduces Table 2 to the count at T = 8000. Apply sends all 30 registers as one transaction. |
| **Live response** | A sweep of the channels you pick with a marker on the sample each write took effect, and meters over the last 0.5 s per channel: DC and %FS on a ±FS bar, AC p-p, second-difference noise σ in µV and referred to the photodiode through that channel's R_F and stage-2 gain, SNR, ambient, and a clipping flag. |
| **Dock** | **Rule check**, **Pending** (field-level diff of staged changes), **Write log** (every write with read-back, sequence number, revert, CSV), **Before / after** (1 s before each marker vs 1 s after it, once 300 ms of settling has passed: DC, AC p-p, σ, SNR), **Snapshots** (named, kept in the browser, SpO₂ default and chip reset built in; stage, apply, diff, JSON), **Console**. |

**Write modes.** *Live* writes each edit at once, coalescing edits that arrive
while a write is still on the wire. *Staged* collects them in Pending, rule-checks
the staged map, and **Apply** (Ctrl+Enter) sends them as one transaction.
**Undo/Redo** (Ctrl+Z / Ctrl+Y) replay the previous values the same way. Rail
panels write through the same path, so their changes are logged, undoable and
marked too. Export as loadable JSON (V3 reads it), a C header in safe write
order, or a CSV of decoded fields.

**Transactions — firmware V2.1.0.**

| Command | Reply | |
|---------|-------|---|
| `WM:aa=vvvvvv,…` (≤ 48 pairs) | `ACK:WM:n=,seq=,flush=,prf=,aa=vvvvvv…` | Everything is validated before anything is written. If any of 0x01–0x1D is in the batch the firmware stops TIMEREN, writes, pulses TIM_CNT_RST, writes CONTROL1 last, settles 60 ms and discards what was converted meanwhile. Gain and LED writes are *not* flushed, so the analog step response stays on the trace. `seq` is the first sample shipped after the change; each register is read back. |
| `REGS?` | `REGS:` + 49 values | The driver's shadow, one line, no SPI traffic. |
| `REGS?HW` | `REGSHW:` + 49 values | Silicon, shadow untouched. **Verify** diffs the two. |
| `PRF:…` | `ACK:PRF:…,seq=` | As before, plus the sequence number. |

Raw writes now resynchronise the driver's timing from the registers, so STATUS
`prpct`, `RATES?` and the frame-header rate follow a hand-written PRPCOUNT. The
command buffer is 640 bytes and the BLE receive ring 1024, enough for the whole
map in one line. With older firmware the lab falls back to one `W:` per register
and marks the change where the reply arrived, labelled `≈`.

Measured on the board (`tools/wm_test.py`): a 500 → 1000 Hz timing change goes
over as one 302-byte line of 30 registers, every register reads back as written,
STATUS reports `prpct=3999`, the frame header says 1000 Hz, and the stream across
the change has **zero sequence gaps and zero CRC errors**. A batch containing a
read-only address is rejected and changes nothing; shadow equals silicon for all
39 writable registers afterwards.

**Rule check.** Reserved bits set; PRPCOUNT outside 800–64 000; reversed edges
or edges past the period; conversion order and sample staggering (8.3.3.1); a
conversion starting inside its ADC reset or overrun by the next one; sample and
conversion windows under 50 µs; NUMAV × 200 counts not fitting the window, and
equation 5; ADC reset under 2 counts (and the 6-count crosstalk note); a sample
still open when converted; an LED sample outside its LED pulse or an ambient
sample overlapping either LED; LED1 and LED2 overlapping; LED duty over 25 %;
R_F·C_F above a tenth of the sample time (equation 1); R_F = none, do-not-use
stage-2 and AMBDAC codes; TX_REF other than 0.5 V on this 3.3 V rail; TIMEREN,
power-down bits, XTALDIS, DIGOUT_TRISTATE and ADC bypass; low sample duty against
figure 54; and any register Verify found silicon disagreeing with.

**Capture.** From the rail's Capture tab or the lab toolbar: a number of samples
(up to 2 000 000), a duration in ms/s/min counted in sample time, **around the
next register write** (history before, recording after — a step response on
demand), or a snapshot of history. Recording is chunked, a rate change part-way
becomes a new time segment rather than a wrong time axis, and markers travel with
the data. The CSV header carries the register map, the rate segments and every
marker, with a marker column; JSON export holds samples, markers and registers.
Time plots zoom with the wheel, pan by dragging and fit on double-click, and the
spectrum and histogram follow the zoomed range.

### The Math view (V2)

A third view alongside Monitor and Capture, reachable from the top bar or the
`M` key. Six panels, all on screen at once. Each pairs a formula — with the
current values substituted into it — with the graph that formula is talking
about. Everything is read from the buffers the monitor is drawing from, so the
R on the board is the R in the vitals card and the autocorrelation curve is the
one that produced the pulse rate. Nothing is animated except the data.

| Panel | Formula | Graph |
|-------|---------|-------|
| Photocurrent to counts | i<sub>PD</sub> = code·1.2 / (2²¹·2R<sub>F</sub>G₂) | ADC headroom, and where the programmed R<sub>F</sub> sits on the full-scale-current ladder |
| Four phases, ambient removed | S<sub>LED2</sub> − S<sub>ALED2</sub> = s<sub>RED</sub>, with all four live phases | The ambient trace and the subtracted result, each on its own scale |
| AC and DC | DC, AC and PI | The live pleth with its DC baseline and an AC caliper |
| Ratio of ratios → SpO₂ | R = (AC/DC)<sub>RED</sub> / (AC/DC)<sub>IR</sub>, SpO₂ = 110 − 25R | The calibration line with the operating point on it |
| Autocorrelation → pulse rate | r(τ), τ*, the parabolic δ, PR = 60f<sub>s</sub>/(τ*+δ) | r(τ) with the winning peak marked |
| The 0.5–5 Hz band | The biquad difference equation with live coefficients | \|H(f)\| with the passband shaded and the live pulse frequency marked |

Each quantity keeps one colour in the formula and in the graph beside it, so a
symbol and a curve can be matched without a legend.

Formulas are **real MathML**, not spans pretending to be fractions. Chrome has
shipped MathML Core since 109, so the browser does the spacing, the italic
correction, the fraction rules and the sub/superscript positioning — the
difference between something that looks like maths and something that is
typeset as maths. The font is **Cambria Math**, which is on every Windows
machine, carries the OpenType MATH table, and is the closest thing to Computer
Modern that does not have to be downloaded. Nothing here loads from the
network, which is the constraint the whole console is built under.

Numbers ease toward their new values rather than snapping, which is what makes
them read as live.

### Rendering

The console draws at the panel's refresh rate — measured at **120 fps with a p99
frame time of 8.3 ms and zero missed vsyncs** on a 120 Hz display, with live
500 Hz acquisition running. It gets there by repainting only the narrow band at
the sweep head each frame, about 30 columns instead of 1,400, which is 9.6× fewer
path segments and 8.6× fewer cleared pixels than a full redraw.

The status bar reports what is actually happening rather than one number:
`render` (fps achieved), `frame` (p99 interval, the slow tail), `miss` (intervals
long enough to have missed a vsync), `panel` (refresh rate detected from the
median interval) and `cols` (columns repainted last frame). **`miss` is the one
to watch** — a steady 60 with zero misses looks smoother than a ragged 110.

`docs/RENDERER.md` has the full account, including why the first attempt at
measuring this was wrong.

### Why the waveform is flipped

The AFE's `LED2-ALED2VAL` and `LED1-ALED1VAL` registers come out **inverted**
compared with the plethysmograms in most publications — the datasheet says so
outright in the notes to figures 128 and 129. Physically it agrees: a systolic
pressure wave means more blood, more absorption, and therefore *less*
photocurrent. So on the raw data systole points **down**.

*Show the pleth the conventional way up* (Acquire → Display, on by default) flips
the y axis of the two ambient-subtracted charts so systole points up, the way a
bedside monitor draws it. The **Raw phases** chart is deliberately left alone, so
you can see the true polarity side by side. Nothing about the stored samples or
the CSV export changes, and SpO₂ is a ratio of RMS amplitudes so it is unaffected
either way.

### Status LED

| State | Colour |
|-------|--------|
| Nothing attached | **white** |
| USB host present | **red** |
| BLE connected | **blue** (reserved — no BLE stack linked yet) |
| Each detected beat | a short **green** flash over the base colour |

The beat flash comes from a pulse detector running **on the board**, not in the
browser, so the LED keeps blinking with the heart rate when the device is
running on battery with no PC attached. Its own estimate is reported as `bpm` in
`STATUS?` alongside the console's independent autocorrelation figure — if the
two disagree, the signal is marginal.

> The XIAO's RGB LEDs are common-anode: driving a pin **low** lights it. The
> Seeed variant defines `LED_STATE_ON` as `1`, which does not match the board,
> so `src/main.cpp` states the polarity explicitly instead of inheriting it.

### Optimising the emitters — precision, not accuracy

Both LEDs were driven at 12.1 mA and the channels came back at **RED 26 %FS,
IR 81 %FS**. That split is the optics, not the drive: the red emitter is
weaker and the photodiode is less responsive at 660 nm than at 905 nm.

The AFE's noise floor is set by the TIA and the ADC, so it does not care how
much light arrives — each channel's *relative* noise goes as 1/level. R is a
product of two ratios, so their relative uncertainties add in quadrature:

```
σ_R  ∝  √( 1/L_red² + 1/L_ir² )
```

which is dominated by whichever channel you neglected. At 26 / 81 that is
σ_R = 4.05 in units of the noise floor. Equalising both is optimal, and the
only limit is the rail:

| Target | LED2 (RED) | LED1 (IR) | σ_R | Better by | Peak at PI 10 % |
|--------|-----------|-----------|-----|-----------|------------------|
| 50 %FS | 23.3 mA | 7.5 mA | 2.83 | 1.43× | 55 %FS |
| 65 %FS | 30.3 mA | 9.7 mA | 2.18 | 1.86× | 72 %FS |
| **70 %FS** | **32.7 mA** | **10.5 mA** | **2.02** | **2.00×** | 77 %FS |
| 75 %FS | 35.0 mA | 11.2 mA | 1.89 | 2.15× | 82 %FS |

`AGC:70` gets there. It could not before: the loop returned as soon as the
**stronger** channel reached target, which left the weaker emitter wherever
the shared setting happened to put it — the 26 / 81 split above is that bug,
not a limitation of the hardware. Both channels now have to arrive.

**None of this moves SpO₂.** R is (AC/DC)_red ÷ (AC/DC)_ir, and both AC and DC
scale with drive current, so R is invariant to everything on this page. LED
optimisation buys *precision*. Accuracy is the curve, below.

The **ADC headroom** card used to show `max(red, ir)`, which read a comfortable
78.8 %FS while one channel was nearly railed and the other was throwing away
three quarters of its range. It shows both now, and turns amber when they are
more than 25 points apart.

### Calibrating the R → SpO₂ curve

`110 − 25·R` is what every reference design ships. It is a population average
for somebody else's emitters, somebody else's photodiode and somebody else's
probe geometry, and on this hardware against a simulator it is wrong by more
than a percent at the ends:

| Reference | R measured | 110 − 25R | error | fitted curve | error |
|-----------|-----------|-----------|-------|--------------|-------|
| 98 % | 0.520 | 97.00 | −1.00 | 97.96 | −0.04 |
| 95 % | 0.604 | 94.90 | −0.10 | 95.07 | +0.07 |
| 90 % | 0.752 | 91.20 | **+1.20** | 89.98 | −0.02 |

Least squares on those three points gives **SpO₂ = 115.85 − 34.40·R**, with an
RMS residual of **0.05 %SpO₂** against **0.90** for the shipped curve — a 19×
improvement from three measurements and no hardware change at all.

**Diag → SpO₂ calibration** does this: set the reference, type the saturation,
*Capture* averages R over a 10–30 s dwell and records the spread, then *Fit
curve* solves the line and shows the residual at every point. The curve is
stored in `localStorage` and used everywhere — the vitals card, the Math view's
plotted curve, the capture analysis. *Export CSV* writes the points, the
residuals and the coefficients.

Two cautions the panel repeats on screen:

* The **fit residual is not the uncertainty**. With two points it is zero by
  construction, which is why n and the saturation span are printed beside it.
  Take at least three, and repeat each one — the spread that *Capture* reports
  is this instrument's precision at that point, which is the other half of the
  answer.
* Fitting against a simulator characterises **this instrument against that
  simulator**. It is a traceability statement about the pair, not a clinical
  calibration against blood.

### Is that pulsatile signal even ours?

A pulsatile component in the LED-**off** phases is normally just ambient light,
and ambient subtraction removes it. A pulsatile component that survives with
our own emitters at **zero current** is a different animal — something else is
illuminating the photodiode in time with the waveform, and subtraction cannot
touch it because it is in both phases, so it lands in both terms of R.
Simulators that synthesise a photocurrent rather than attenuating the probe's
own light do exactly this.

**Diag → Emitter-off test** drives both currents to zero for six seconds,
measures mean, peak-to-peak and RMS on all four phases, restores the currents,
and compares the worst dark swing against the pulsatile amplitude the probe was
resolving while lit. Under 2 % it says so; over 10 % it names the problem.

### About the SpO₂ number

SpO₂ comes from the usual ratio-of-ratios with the standard empirical fit
`SpO₂ ≈ 110 − 25·R`. That is fine for bring-up and for watching the trend, and it
is what most reference designs print. **It is not a calibrated measurement** — a
real product needs a clinical calibration against a CO-oximeter, per probe.

---

## 5. Bring-up when nothing works

The firmware can tell a missing board apart from a wiring fault on its own:

```bash
python tools/link_probe.py COM5 --stream 3   # identity, self-test, registers, live rate
python tools/cmd_sweep.py  COM5              # exercise all 47 commands
```

or from the console's Diag tab / any serial monitor:

```
PINTEST
```

`PINTEST` pulls `MISO` and `ADC_RDY` both ways to see whether anything is driving
them, counts `ADC_RDY` edges for 250 ms, and walks a pattern through a scratch
register. It then prints one of:

| Verdict | Meaning |
|---------|---------|
| `OK` | readback and `ADC_RDY` both healthy |
| `NO_AFE` | MISO floats and `ADC_RDY` is silent — no power, no ground, or the breakout is unplugged |
| | *This is what an unwired board reports, and it is measured from the pins, not inferred from a failed read.* |
| `MISO_OPEN` | the AFE is clocking but D1 is not connected to H3.10 |
| `NO_CLOCK` | the bus answers but `ADC_RDY` never pulses — suspect the 8 MHz crystal or `TIMEREN` |
| `SPI_CORRUPT` | readback mismatches — check D2/D3/D4, or drop the SPI clock |

`SELFTEST:256` walks 256 patterns through a register and reports the error count;
it is the quickest go/no-go once the wiring is in place. If it fails, `SPI:TEST`
(section 5) tells you whether the clock is the problem.

### Signal is flat or railed

Run **Auto gain** (or send `AGC:50`). It sweeps LED current and transimpedance
until the DC level sits near half of full scale — the loop from datasheet
figure 135. Watch the **ADC headroom** vital: 40–60 % is the target, above ~85 %
you are about to clip.


---

## 6. SPI speed — what carries over from the ADS1292R, and what doesn't

HECG01 famously ran at "200 kHz". Three of the four reasons for that apply
here too, and the biggest one does not.

### Does not carry over: `t(SDECODE)`

The ADS1292R needs 4 t_CLK ≈ **7.81 µs** of dead time after an opcode byte
before the next byte. HECG01 V1 waited 2 µs and only worked at 250 kHz because
one byte already took 32 µs — the slow clock was accidentally supplying the gap.
That is why raising the clock looked like a wiring problem when it was a
firmware bug.

**The AFE4490 has no equivalent.** Its SPI is a plain 32-clock frame — 8 address
bits then 24 data bits, CS low throughout — with no opcode decode and no
inter-byte requirement. Its only timing rules are edge-relative
(`t_SCLK ≥ 62.5 ns`, setup/hold ≥ 10 ns, `t_SOMIPD ≥ 17 ns`), all of which are
satisfied by construction at any clock the SPIM can produce. So the specific
failure that pinned HECG01 to 250 kHz cannot happen on this chip.

### Carries over 1: the number you ask for is not the number on the wire

The nRF52 SPIM only has discrete clocks, and `SPIClass::setClockDivider()`
computes `div = 64 MHz / requested` then rounds the **frequency up** to the next
step. Measured on this board:

```
SPI:SET:200000    -> ACK:SPI:250000,req=200000      200 kHz does not exist
SPI:SET:3000000   -> ACK:SPI:4000000,req=3000000    rounds up, not down
SPI:SET:6000000   -> ACK:SPI:8000000,req=6000000    rounds up, not down
SPI:SET:16000000  -> ACK:SPI:8000000,req=16000000   SPIM1 tops out at 8 MHz
```

Only `125 k · 250 k · 500 k · 1 M · 2 M · 4 M · 8 M` exist on SPIM0/1/2 — 16 and
32 MHz belong to SPIM3 alone, and SPIM3 is already the global `SPI` object. The
driver therefore quantizes every request through `AFE4490::quantizeSpiHz()` and
**always reports the real SCLK**, so a reported speed is never a fiction.

### Carries over 2: the BSP forces high drive on SCK and MOSI

`SPIClass::begin()` stamps `NRF_GPIO_PIN_H0H1` onto SCK and MOSI ("highspeed
SPIM should set SCK and MOSI to high drive"). On jumper wiring that overshoots
and cross-couples into MISO. HECG01 found standard drive to be the single
biggest improvement, so this project **defaults to `S0S1`** and re-applies it
after `begin()`, which is the only place it can be undone:

```
SPI:DRIVE:0    standard (S0S1) — default, jumper wiring
SPI:DRIVE:1    high     (H0H1) — short PCB traces
```

### Carries over 3: measure, don't assume

```
SPI:TEST
```

sweeps every SPIM step, running 32 walking-pattern register read-backs at each,
and reports pass/fail per clock:

```
SPITEST:125000=0/32,PASS
...
SPITEST:8000000=7/32,FAIL
SPITEST:END:BEST=4000000,SAFE=2000000,ACTIVE=2000000,DRIVE=S0S1,MAX=8000000
```

It then leaves SCLK **one step below** the fastest error-free clock. Running at
the exact edge of what works is how a rig starts failing when someone nudges a
wire. Override with `SPI:SET:` if you disagree. The console has this on the
**Diag** tab as *Sweep SPI clock*.

If nothing passes at any clock the message says so explicitly — that is a wiring
or power fault, not a speed problem, and `PINTEST` is the next step.

### So what should the default be?

**4 MHz**, which is what `begin()` uses. It is an exact SPIM step, a 4× margin
under the AFE's own 16 MHz limit, and it costs ~48 µs to read all six result
registers — 2.4 % of a 500 Hz pulse-repetition period. Dropping to 250 kHz would
cost 768 µs per sample, which still fits at 500 Hz but **will not fit at 1 kHz**.
There is no reason to start slow here the way there was on HECG01; if the wiring
cannot take 4 MHz, `SPI:TEST` will say so in about three seconds.

---

## 7. Verification

```bash
python tools/verify_timing.py         # timing generator vs datasheet Table 2
python tools/parser_test.py           # the link parser, end to end
python tools/console_selftest.py      # 65 checks against the live renderer
python tools/console_selftest.py --page web/afe4490_studio_V2.html
python tools/math_selftest.py         # checks against the Math view
python tools/math_selftest.py --page web/afe4490_studio_V3.html
python tools/ble_selftest.py          # the BLE transport, with a fake GATT
python tools/ble_probe.py --arm COM5  # the real radio, from Python (needs bleak)
python tools/rate_sweep.py            # how fast this board and link really go
python tools/js_lint.py               # brackets, duplicate declarations, dead ids
python tools/js_lint.py web/afe4490_studio_V4.html
python tools/lab_selftest.py          # 50 checks on the V4 Register Lab, simulated board
python tools/wm_test.py --port COM5   # WM:/REGS? transactions on the real board
AFE_TEST_TIMEOUT=1500 python tools/console_selftest.py   # on a loaded machine
python tools/chrome_ble_probe.py --arm COM5 --dec 4   # the real radio, from Chrome
python tools/render_bench.py 240      # rasteriser work per frame
python tools/studio_shot.py --check   # console smoke test in headless Chrome
python tools/studio_shot.py docs/studio_capture.png#cap   # and a screenshot
```

`console_selftest.py` is the one that matters for the console. It drives the real
page objects and then reads the pixels back: every visible canvas must carry ink
in **both** trace colours rather than a grid line or a "no data" note, full
repaints must stay under 0.5 per frame, split lanes must land on genuinely
different windows, the sweep head must wrap twice without leaving the ring, a
hidden chart must paint nothing, and capture must record, analyse and draw in all
three domains. It measures laid-out boxes as well as bitmaps, because `fitCanvas`
falls back to a 600×150 backing store when `clientHeight` is 0 — so a canvas can
be full of ink and occupy no space at all, which is how the capture grid first
shipped, collapsed behind a missing `flex: 1`.

`js_lint.py` is the cheap one: it scans the inline script for unbalanced
brackets, helpers declared twice, and `$('#id')` lookups whose element does not
exist in the markup — the failure that once shipped a capture grid collapsed to
zero height. It needs no browser, so it still answers on a machine too loaded to
run headless Chrome, and it is the first thing to run after a large edit. It
follows regex literals and `${…}` inside template literals, including nested
templates, which V4 uses heavily; before that it reported brackets V4 does not
have.

`lab_selftest.py` drives the V4 Register Lab against a simulated board that
speaks the real protocol — `WM:`, `W:`, `REG?`, `REGS?`, `REGS?HW` — and streams
genuine CRC'd frames with sequence numbers, its DC level following the LED
register. It checks that the generator reproduces Table 2 exactly; that the
firmware's own defaults pass the rule check and injected faults do not; that a
live edit is one `WM:` line, read back, logged and undoable, **and that its
marker lands on the very sample the acknowledgement named**; that staged edits
and a 30-register timing change each go over as one line and the display rate
follows; that before/after sees the brighter LED; that capture by count, by
duration and around a write each keep the right samples with the marker at the
trigger and the register map in the CSV; that Verify catches a register silicon
disagrees with; that firmware without `WM:` falls back to `W:`; that rail panels
write through the lab; and that dragging an edge on the timing canvas changes
the right register.

`wm_test.py` is the hardware half: on the real board it sends a whole timing
change as one `WM:` line mid-stream and confirms the read-back, `STATUS?`
`prpct`, the frame-header rate, zero sequence gaps and zero CRC errors across the
change, that a batch with one illegal address changes nothing, and that shadow
and silicon agree afterwards.

`ble_selftest.py` runs the console against a fake GATT stack and a device
emulator, so the handshake, the compact frame, the split-notification cases and
the automatic back-off are all covered without hardware. `ble_probe.py` is the
other half: it connects to the real board over the real radio and reports what
arrived — sample rate, packet rate, bytes on the air, CRC errors, and any gap
in the sequence numbers. A gap there is a sample the firmware numbered and
never delivered, which is the only definition of "dropped" worth arguing about.
Use `--arm COM5` to send `BLE:ON` over USB instead of walking to the board.

`chrome_ble_probe.py` is the one that found the real fault. It serves the
console from localhost, drives real Chrome over the DevTools protocol, answers
the Web Bluetooth chooser through `DeviceAccess.selectPrompt`, and reports what
the page received, when the radio went quiet, whether the sweep ever froze, and
what the firmware says it lost — side by side. `--norender` kills the animation
loop and nothing else, which is how the renderer was ruled out as the cause of
the stalls. It needs `websockets`, and Chrome has to run headed because
headless has no Bluetooth adapter.

`math_selftest.py` walks all seven chapters and reads pixels back. The Math
view has two ways to fail quietly — a chapter that draws nothing, and a formula
whose live values never resolve and sit at an em dash forever — and both look
fine in a screenshot of the other six. So it checks that every chapter carries
ink, that the chapters draw *distinct* pictures rather than one painting over
the last, that every value slot resolves to a number, that the SpO₂ on the
board matches the vitals card, and that the autocorrelation it draws agrees
with the rate the monitor reports to within 1 bpm.

`render_bench.py` counts what the rasteriser is handed — path segments, path
length, pixels cleared, draws carrying a `shadowBlur` — rather than timing the
draw calls. Chrome records canvas commands on the main thread and rasterises them
later, so `performance.now()` around `stroke()` measures only how long it took to
*describe* the work: it reported 1.1 ms/frame for a console that was visibly
running at 30 fps. The work counters are hardware independent and directly
comparable between two builds; `--page` runs the same harness against an older
copy of the console.

`parser_test.py` builds byte streams byte-identical to what `src/main.cpp` emits and
pushes them through the console's real `feed()`: a clean batch, every chunk split
from 1 to 40 bytes, ASCII interleaved between frames, a corrupted CRC followed by a
good frame, a frame delivered in two halves, CSV text mode, and a single-sample
frame. It is the check that says the wire format and the parser agree.

`verify_timing.py` reproduces all 29 edge registers for the datasheet's worked
example (PRF 500 Hz, 25 % duty) and then sweeps 54 PRF/duty combinations
asserting that phases stay inside their quarter, the Rx window stays inside the
LED window with ≥50 µs of sampling, conversions start exactly one clock after
their ADC reset, and nothing exceeds `PRPCOUNT`.

---

## 8. Status

- **Firmware** — builds clean, flashed and verified on hardware. All 47 commands
  answer correctly; identity, timing, register, diagnostic and SPI-tuning paths
  all exercised.
- **Console** — verified in headless Chrome: no JS errors, all 49 registers
  rendered, and the DSP chain recovers 72 bpm and R = 0.62 from a synthetic PPG.
- **End-to-end with the AFE4490 attached — not yet run.** At the time of writing
  `PINTEST` reported `NO_AFE`, so the breakout was not connected to the XIAO.
  Wire up H3 as in section 1 and re-run `tools/link_probe.py`.
- **BLE** — not implemented. `CAPS` advertises `BLE=0`; the transport is
  deliberately USB-only for now so the 6-channel stream carries no SoftDevice
  radio jitter. The framing in `docs/PROTOCOL.md` was sized to drop into a BLE
  characteristic later without changing the console's parser.
