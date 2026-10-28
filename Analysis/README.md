# Analysis — from a capture to the Lab 7 answers

This folder turns a recording made with **AFE4490 Studio V4** into the numbers
Lab 7 asks for: SpO₂ by both algorithms, pulse rate, perfusion index, and the
blocks that fill the lab's Excel workbook.

It replaces the data **source**, not the method. The arithmetic is the lab's,
register for register and formula for formula. What changed is where the
samples come from: a XIAO nRF52840 with an AFE4490 breakout, instead of TI's
AFE44x0SPO2EVM and its 332 MB LabVIEW GUI.

```
Analysis/
  matlab/     the toolkit — load, analyse, report, fill Excel
  excel/      the lab workbook as a clean template, and a filler
  samples/    a 12 s recording with known answers, used by the self-test
  tools/      convert a TI EVM log into a Studio capture
```

---

## Quick start

1. In the console's **Capture** tab, record a capture and press **JSON**
   (preferred — always ADC codes, and it carries the register map) or **CSV**.
2. Open `matlab/run_lab7.m`, point `CAPTURE` at the file you saved, press Run.

You get the printed result, a figure showing exactly which peaks were used,
two CSV blocks for the workbook, and a filled copy of the workbook itself.

```matlab
S = afe4490_load('afe4490_capture_2026-10-28.json');   % read it
A = afe4490_analyze(S);                                % beat by beat
afe4490_report(S, A);                                  % print + plot
afe4490_excel(A, 'Workbook', '../excel/SpO2_Lab7_Template.xlsx');
```

Run `verify_toolkit` once after cloning. It checks the toolkit against TI's
own GUI output, not against itself.

---

## What it reads

| Format | Looks like | Sample rate |
|---|---|---|
| `studio-json` | Studio V4 Capture → JSON | in the file |
| `studio-csv` | Studio V4 Capture → CSV | in the file |
| `evm-csv` | TI EVM `Device_Codes_*.csv` / `Device_Volts_*.csv` | **not recorded — pass `'Fs'`** |
| `txt2` | two columns, Red then IR (`spo2prim.txt`, `7.txt`) | not recorded |
| `mat` | the lab's `PPG*.mat` (`RedPPG`/`IRPPG`, `PPGRed`/`PPGIR`, …) | sometimes stored as `Fs` |

The format is decided by reading the file, not its name — which matters here,
because in the lab's own folder `PPG.txt` and `PPGsone.TXT` are MAT-files and
`PPG231.mat` is plain ASCII. 15 of the 18 lab data files load; the three that
do not are a heart-sound recording, a corrupt MAT-file and a one-column file.

Everything arrives as both ADC codes and volts, related by
`volts = codes × 1.2 / 2²¹`. That constant is not assumed: every value in the
lab's `Device_Volts` logs equals its `Device_Codes` value times exactly that,
across all six channels and both signs, so our captures and the EVM's are on
one scale.

---

## The six channels, and the one mistake that matters

Column order, in every format:

| # | Name | Meaning |
|---|---|---|
| 1 | `red` | LED2 — **RED** |
| 2 | `amb2` | ambient during the RED phase |
| 3 | `ir` | LED1 — **IR** |
| 4 | `amb1` | ambient during the IR phase |
| 5 | `redAmb` | `red − amb2` |
| 6 | `irAmb` | `ir − amb1` |

**LED2 is RED and LED1 is IR** — the opposite of what the numbering suggests.
Get that pair the wrong way round and R becomes 1/R, which is still a
plausible-looking saturation: on the sample recording, 95.7 % becomes 66.4 %.
No error, no crash.

Every other thing that can go wrong here is loud. A wrong sample rate moves
the pulse rate but cancels out of R, because AC/DC is a ratio. A wrong volts
scale cancels too. Even flipping both channels upside down cancels — AC is
`max − min` and DC is their mean, so both survive. Only a *swap* is silent.

So `afe4490_analyze` checks two things and warns:

- R outside 0.2–1.2, and
- IR carrying less perfusion than RED. On both of the lab's EVM captures IR
  carries **1.7–1.9×** RED, and that ratio holds even where the absolute
  amplitudes reverse (RED is the larger channel in one capture, the smaller
  in the other). It is the one stable discriminator in the package.

Columns 5 and 6 are exactly `1−2` and `3−4` — verified to zero deviation over
all 92 183 rows of the lab's log — so they carry no extra information.

---

## Two methods, two algorithms

`afe4490_analyze(S, 'Method', ...)`:

- **`'beat'`** (default) finds each heartbeat on a band-limited copy of IR,
  then reads max and min of each **raw** channel inside that beat. RED and IR
  are measured over the same beat, which is what makes their ratio mean
  anything. Amplitudes come off the raw signal because the DC level is half
  the ratio and a high-pass filter would throw it away.
- **`'lab'`** reproduces `SpO2_Processing_MatLab.m` exactly: `peakdet` on each
  channel independently, then `MAX(k)` paired with `MIN(k)`. Kept so new
  numbers can be checked against the lab's. On the sample the two agree to
  0.06 pp.

Both algorithms are computed per beat:

```
Algorithm 1   R     = (ACred/DCred) / (ACir/DCir)
Algorithm 2   R_log = log(MAXred/MINred) / log(MAXir/MINir)
              SpO2  = 110 − 25·R
```

> The lab's own script defines Algorithm 2 and then never uses it — its `R2`
> and `SpO2_2` are both computed from Algorithm 1's variables
> (`R2 = RedAbsorption1./IRAbsorption1`). Here both are real, so they can be
> compared. Algorithm 2 is undefined where a channel crosses zero, and
> returns blank there rather than a complex number.

`peakdet`'s threshold is scaled to the signal by default. The lab hard-codes
`0.0003`, which is tuned for volts at one particular amplitude; on ADC codes
it finds every noise wiggle. `'Delta','lab'` restores the original value,
converted if you are working in codes.

---

## Excel

`excel/SpO2_Lab7_Template.xlsx` is the lab's own workbook with the example
data cleared. Structure, formulas and layout are untouched.

The whole sheet is driven by **thirty cells**, `Manual!D4:H9` — six beats of
Max Red, Min Red, Max IR, Min IR and the time of the IR maximum. Everything
else, both SpO₂ algorithms, the pulse rate and the perfusion index, is an
Excel formula over those five columns. So that is all anything here writes.

```matlab
afe4490_excel(A, 'Workbook', '../excel/SpO2_Lab7_Template.xlsx')
```

writes `Manual!D4:H9` and `Matlab!D5:Q10` into a copy. If MATLAB cannot write
the workbook on your machine, the same blocks are also saved as CSV:

```bash
python excel/fill_template.py <prefix>_Manual_D4.csv
```

Both routes preserve every formula — checked.

---

## Verification

`verify_toolkit` is the file to trust, because most of what it checks was not
produced by this code:

| Check | Result on the sample |
|---|---|
| Four formats (our JSON/CSV, EVM codes/volts) give one answer | spread 0.0000 % |
| Agrees with **TI's own GUI** on the same recording | TI 95.50 %, this 95.65 % — **0.16 pp** |
| `codes × 1.2/2²¹` reproduces the EVM's own volts | worst 5 × 10⁻¹⁰ V |
| Capture header's PRPCOUNT decodes to the real rate | 7999 → 500.0000 Hz |
| A RED/IR swap is caught, not reported as a saturation | caught, 2 warnings |
| The lab's method agrees with the beat method | 0.06 pp |
| Excel block matches the workbook's own formulas | exact |

The TI comparison is the one that counts. TI's GUI wrote its own analysis into
`Device_Analysis_*.csv`; its Histogram block gives a mean and a peak-to-peak
per channel, which yields R with no peak detection at all. Landing on that
number is agreement between two independent implementations.

---

## Sample data

`samples/capture_demo_500Hz.{csv,json}` is 12 s of a real finger recording,
converted from the lab's EVM log by `tools/evm_to_capture.py` — the same
numbers, in our format, with the EVM's own register settings carried across
from its Analysis log. Known answers: **SpO₂ 95.65 %, R 0.574, PR 89.3 bpm,
PI 11.5 %**.

`tools/evm_to_capture.py` is also how you bring an old EVM recording into this
pipeline, though `afe4490_load` reads EVM logs directly and you rarely need it.

---

## Known limits

- **The EVM logs record no sample rate.** Nothing in `Device_Codes` or
  `Device_Volts` names one; it survives only as `PRPCOUNT` in the Analysis
  file, which the analysis path never reads. Pass `'Fs'` or the loader assumes
  500 Hz and says so. A wrong rate gives a wrong pulse rate, not a wrong SpO₂.
- **`110 − 25·R` is a population average** for somebody else's emitters and
  probe geometry. For a number that means anything on *this* sensor, calibrate
  against a simulator and pass your own `'A'` and `'B'`.
- **There is no golden test case from the lab.** The graded workbook's inputs
  (Max Red 0.0484, Max IR 0.0547) do not appear in any shipped recording, so
  the lab's own answers cannot be regression-tested. That is why the TI GUI
  comparison above exists instead.
- **`units=µA` captures are refused.** Microamps cannot be turned back into
  codes or volts without re-assuming the gain. Export as Volts or ADC codes.
