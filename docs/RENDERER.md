# The console renderer, and why it was rewritten

The first version of `web/afe4490_studio.html` drew every trace in full on
every animation frame. On a maximised 1920-wide window that held the display
to 30–40 fps with visible stutter, no matter how fast the machine was. It now
runs at the panel rate — 120 fps on a 120 Hz display, with a measured p99
frame time of 8.3 ms and zero missed vsyncs.

This is the same rewrite that was done for the HECG01 console, applied to a
different signal. The technique is not novel; the point of writing it down is
that the *measurement* is what made it obvious, and the obvious measurement
was the wrong one for a long time.

---

## 1. Why the first measurement lied

The natural way to benchmark a canvas renderer is to time the draw calls:

```js
const t0 = performance.now();
drawEverything();
const cost = performance.now() - t0;      // 1.1 ms — looks fine!
```

That number said 1.1 ms per frame while the console was visibly running at
30 fps. It was not wrong, it was measuring the wrong thing. Chrome's 2D canvas
**records** commands on the main thread and **rasterises** them later, off
that thread. Timing `stroke()` measures how long it took to describe the work,
not to do it.

`tools/render_bench.py` now instruments `CanvasRenderingContext2D` instead and
counts what the rasteriser is actually handed. Those numbers are hardware
independent and they told the truth immediately:

| per frame, 1920×1080 window | before | after | change |
|---|---:|---:|---|
| path segments | 15,300 | 1,600 | **9.6× less** |
| path length | 49,900 px | 3,600 px | **13.9× less** |
| pixels cleared | 823,300 | 95,500 | **8.6× less** |
| JS command recording | 5.69 ms | 1.27 ms | 4.5× less |
| `stroke()` calls | 13.1 | 32.4 | 2.5× *more* — see below |

Fifteen thousand path segments per frame is what a 30 fps console looks like.

> `stroke()` calls went up because the glow changed from one blurred stroke
> into three plain ones, and because a band that straddles the right edge is
> painted as two clipped ranges. Each call now covers ~30 columns instead of
> ~1,400, which is the trade that matters. The row to read is *path segments*.

---

## 2. What changed

### Incremental sweep instead of a scrolling redraw

A sweep display has a property a scrolling one does not: **between two
adjacent frames the picture changes only in a narrow band at the sweep head.**
At 120 fps with a 5 s window the head advances about two pixels.

So the trace stays on the canvas between frames, and each frame repaints only

```
[ cursor trail ][ columns the head just crossed ][ wipe gap ahead ]
```

which is around 30 columns instead of 1,400. `ctx.clip()` around the band
guarantees nothing outside it is touched, and the repaint leads in three
columns so the polyline entering the band reconnects cleanly.

Samples are bucketed into one column per pixel as they arrive — min, max and
last — so the cost is set by the canvas width, not the sample rate. Raising
PRF from 500 Hz to 1 kHz does not make the display more expensive.

### Two canvases per chart

Grid, axis labels, lane rules and the time ruler live on a background canvas
that is painted only when the scale or the geometry changes. The foreground
carries the trace and nothing else, so the incremental band never has to
redraw a grid line.

### No `shadowBlur`, anywhere

`ctx.shadowBlur` makes the browser run a full gaussian blur over everything
the call touches, once per stroke per frame. The glow is now three plain
strokes (5 px faint / 2.5 px faint / 1.35 px solid). It reads the same at
these line widths and costs orders of magnitude less.

### Full repaints only when the picture is genuinely wrong

A resize, a sweep-speed change, a chart returning from being hidden, or the
auto-scale adopting a new window. The last one has two rules:

- **clipping** — the signal has grown past the painted range and is being
  drawn flat against the edge of its lane. Adopt immediately, whatever it
  costs; showing a clipped trace is a lie about the data.
- **drift** — the axis is merely wandering. Sit still until the move would
  exceed 3 % of the painted span, and never repaint more than ~4 times a
  second.

Measured over 480 frames of live signal: **0.09 full repaints per frame.**

### A playout clock, so it is smooth and not just fast

USB delivers samples in bursts — eight at a time, about 67 times a second —
but the sweep has to advance on wall-clock time or it stutters at the burst
rate. The display therefore reads out of the ring on its own clock, through a
small elastic buffer with a gentle rate correction, the way an audio playout
buffer absorbs jitter. Metrics still run off the ring directly and are not
delayed by any of it.

The loop also refuses a timestamp it cannot use: a backgrounded tab can wake
with minutes on the clock, and a caller driving `render()` by hand can hand it
one that runs backwards. Either way it falls back to a single frame and lets
the buffer re-converge.

### Per-lane auto-scale

This was a bug, not a performance problem, and it is the reason the raw
plethysmograph looked like two flat lines: RED sits near 0.48 V and IR near
0.58 V, so a shared y axis spends its whole range on the gap between them and
compresses both pulsatile waveforms into nothing. The AFE44x0EVM avoids this
by giving every channel its own plot; this console gives every trace its own
auto-scaled lane, and *Channels → Overlaid* is there if you want the old
behaviour deliberately.

---

## 3. About "120 FPS"

`requestAnimationFrame` is clamped to the refresh rate of the monitor the
window is on. No JavaScript can raise that ceiling:

- 60 Hz panel → 60 fps, however good the code is
- 120 / 144 / 165 Hz → reachable only if every frame's work finishes inside
  8.3 / 6.9 / 6.0 ms

And **smooth is not the same as fast**. A steady 60 with every vsync met looks
better than a ragged 110. So the status bar reports what is actually happening
rather than a single number:

| field | meaning |
|---|---|
| `render` | frames per second achieved |
| `frame` | p99 frame interval — the slow tail, not the average |
| `miss` | intervals long enough to have missed a vsync, per window |
| `panel` | refresh rate detected from the median frame interval |
| `cols` | columns repainted on the last frame, across all charts |

`miss` is the one to watch. Zero means it is as smooth as the display can be.

Measured on the target machine, 120 Hz panel, live 500 Hz acquisition:
**render 120 fps · frame 8.3 ms · miss 0 · panel 120 Hz · cols 96–110.**

---

## 4. Capture and analyse

The same job the EVM's *ADC Capture & Analysis* page does: freeze a finite
block of samples and look at it properly instead of watching it scroll past.
A capture is a static object, so its plots are drawn once and cost nothing per
frame.

**Two ways in.** *Record* arms the acquisition and keeps the next N samples,
which is what the EVM's finite capture does and the only option when N is
larger than the live ring. *Snapshot* copies the last N samples already in the
ring — instant, but capped at the 4096 samples of history the monitor keeps.
Live monitoring keeps running throughout either one.

**1, 2, 4 or 6 plots.** The four-plot default mirrors the EVM's page exactly:
LED1 (IR), LED1 ambient, LED2 (RED), LED2 ambient. Each plot picks its own
channel and its own domain, and carries its own y axis.

| domain | shows |
|---|---|
| Time domain | min/max envelope with the mean over it; mean, p-p, AC rms, min, max |
| Spectrum · 0–10 Hz | the physiological band, drawn as bars because a short capture only resolves a few dozen bins there |
| Spectrum · full band | to Nyquist, for looking at the converter rather than the patient |
| Histogram | 64 bins with a fitted normal over them, plus noise-free bits |

SNR, THD and ENOB are always computed over the whole band; the zoom only
decides how much of it is drawn. The fundamental is picked above the DC
leakage bins, so a respiratory component at 0.24 Hz does not get mistaken for
the pulse.

Units are volts, ADC codes, or input current — the last derived from the TIA
feedback resistor and stage-2 gain actually programmed into `TIAGAIN`, so it
follows the register map rather than assuming a default. Detrend is none,
mean-removed, or the 0.5–5 Hz band, applied forwards and backwards for zero
phase. The mean is removed before that filter runs: otherwise the high-pass
sees a step of half the ADC range at sample zero and rings for seconds.

Export is CSV (all six channels in the selected units, with a header naming
the rate and timestamp) or PNG of the plot stack.

---

## 5. Testing it

```bash
python tools/console_selftest.py     # 65 checks, reads pixels back
python tools/render_bench.py 240     # rasteriser work per frame
python tools/studio_shot.py --check  # structural smoke test
```

`console_selftest.py` is the one that matters. It drives the real page objects
and then asserts on the pixels: that every visible canvas carries ink **in
both trace colours** rather than a grid line or a "no data" note, that full
repaints stay below 0.5 per frame, that split lanes land on genuinely
different windows, that the sweep head wraps twice without leaving the ring,
that a hidden chart paints nothing, and that capture records, analyses and
draws in all three domains.

It also checks something a pixel test alone would miss. `fitCanvas` falls back
to a 600×150 backing store when `clientHeight` is 0, so a canvas can be full
of ink and occupy no space at all — which is exactly how the capture grid
first shipped, collapsed to zero height behind a missing `flex: 1`. The
selftest now measures the laid-out box as well as the bitmap.

Frames are pumped by calling `render()` with synthetic timestamps rather than
waiting on `requestAnimationFrame`: under Chrome's virtual clock rAF fires
only a handful of times, which is not enough to test anything, and pumping
makes the run deterministic.
