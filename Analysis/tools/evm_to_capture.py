"""Rewrite a TI AFE44x0SPO2EVM log as an AFE4490 Studio capture.

The EVM GUI and this project's console record the same six AFE4490 result
registers, in the same order, on the same scale - verified against the lab's
own logs: every Device_Volts value is exactly its Device_Codes value times
1.2 / 2^21. So an EVM log can be rewritten as a Studio capture without
touching a single number, and the MATLAB toolkit can then be tested against
data whose answers are already known.

That is what this script is for: building fixtures, and bringing old EVM
recordings into the new pipeline. It is not needed for day-to-day work -
Analysis/matlab/afe4490_load.m reads EVM logs natively.

Usage:
  python evm_to_capture.py <Device_Codes_or_Volts.csv> <out_basename> [--fs 500]

Writes <out_basename>.csv (volts, the layout Excel wants) and
<out_basename>.json (ADC codes, the lossless one).
"""
import argparse
import json
import pathlib
import re
import sys

FS_CODE = 2097152          # 2^21, the AFE4490's 22-bit full scale
FS_VOLT = 1.2
CHANNELS = ["red", "amb2", "ir", "amb1", "redAmb", "irAmb"]

# The register map a 500 Hz, 25 % duty capture runs at, from the firmware's
# applyDefaults() plus applyTiming(T=8000). Written into the header so the
# fixture carries the same provenance block a real capture does.
def default_registers(prpcount=7999, duty=25, settle=50, adcrst=4):
    T = prpcount + 1
    Q = T // 4
    ton = min(T * duty // 100, Q)
    if ton < settle + 202:
        ton = settle + 202
    se, le = ton - 2, ton - 1
    r = {0x00: 0x000000}
    r[0x05], r[0x06] = settle, se
    r[0x09], r[0x0A] = Q, Q + le
    r[0x07], r[0x08] = Q + settle, Q + se
    r[0x0B], r[0x0C] = 2 * Q + settle, 2 * Q + se
    r[0x03], r[0x04] = 3 * Q, 3 * Q + le
    r[0x01], r[0x02] = 3 * Q + settle, 3 * Q + se
    for k, (rs, re, cs, ce) in enumerate(
            [(0x15, 0x16, 0x0D, 0x0E), (0x17, 0x18, 0x0F, 0x10),
             (0x19, 0x1A, 0x11, 0x12), (0x1B, 0x1C, 0x13, 0x14)]):
        P = k * Q
        r[rs], r[re], r[cs], r[ce] = P, P + adcrst - 1, P + adcrst, P + Q - 1
    r[0x1D] = prpcount
    numav = max(0, min(15, (Q // 200) - 2))
    r[0x1E] = 0x100 | numav          # TIMEREN + NUMAV
    r[0x1F] = 0
    r[0x20] = 0x000000               # TIAGAIN: shared gain
    r[0x21] = 0x000000               # RF 500k, CF 5 pF, no stage 2, 500 Hz corner
    r[0x22] = 0x012828               # LED range 1 (50 mA FS), both LEDs at 0x28
    r[0x23] = 0x020000               # TX_REF 0.5 V, H-bridge
    r[0x24] = r[0x25] = r[0x26] = r[0x29] = 0
    return r

REG_NAMES = {
    0x00: "CONTROL0", 0x01: "LED2STC", 0x02: "LED2ENDC", 0x03: "LED2LEDSTC",
    0x04: "LED2LEDENDC", 0x05: "ALED2STC", 0x06: "ALED2ENDC", 0x07: "LED1STC",
    0x08: "LED1ENDC", 0x09: "LED1LEDSTC", 0x0A: "LED1LEDENDC", 0x0B: "ALED1STC",
    0x0C: "ALED1ENDC", 0x0D: "LED2CONVST", 0x0E: "LED2CONVEND",
    0x0F: "ALED2CONVST", 0x10: "ALED2CONVEND", 0x11: "LED1CONVST",
    0x12: "LED1CONVEND", 0x13: "ALED1CONVST", 0x14: "ALED1CONVEND",
    0x15: "ADCRSTSTCT0", 0x16: "ADCRSTENDCT0", 0x17: "ADCRSTSTCT1",
    0x18: "ADCRSTENDCT1", 0x19: "ADCRSTSTCT2", 0x1A: "ADCRSTENDCT2",
    0x1B: "ADCRSTSTCT3", 0x1C: "ADCRSTENDCT3", 0x1D: "PRPCOUNT",
    0x1E: "CONTROL1", 0x1F: "SPARE1", 0x20: "TIAGAIN", 0x21: "TIA_AMB_GAIN",
    0x22: "LEDCNTRL", 0x23: "CONTROL2", 0x24: "SPARE2", 0x25: "SPARE3",
    0x26: "SPARE4", 0x29: "ALARM",
}


def read_evm(path):
    """Return (rows_of_six_floats, 'codes'|'volts'). The EVM writes five
    preamble lines, then a header naming the six channels, then data with a
    trailing comma that must not become a seventh column."""
    kind, rows, in_data = None, [], False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not in_data:
                low = s.lower()
                if low == "codes":
                    kind = "codes"
                elif low == "volts":
                    kind = "volts"
                elif s.startswith("Ch :") or s.startswith("Ch:"):
                    in_data = True
                continue
            s = s.rstrip(",")
            if not s:
                continue
            try:
                vals = [float(x) for x in s.split(",")]
            except ValueError:
                continue
            if len(vals) >= 6:
                rows.append(vals[:6])
    if kind is None:
        sys.exit(f"{path}: no 'Codes' or 'Volts' marker line - is this an EVM log?")
    return rows, kind


def read_analysis_registers(path):
    """Pull the real register settings out of a Device_Analysis_*.csv.

    The EVM GUI writes an 'ADC Register Settings' block into its Analysis
    export - block name, register name, data as unpadded hex, address as
    unpadded hex. That is the capture's true provenance, so it beats any
    table of defaults compiled in here."""
    regs, in_block = {}, False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if s.startswith("ADC Register Settings"):
                in_block = True
                continue
            if not in_block or not s:
                continue
            if s.startswith("Block Name"):
                continue
            parts = [x.strip() for x in s.split(",")]
            if len(parts) < 4:
                continue
            try:
                addr = int(parts[3], 16)
                val = int(parts[2], 16)
            except ValueError:
                continue
            # 0x2A..0x30 are result and diagnostic registers, not settings;
            # the GUI dumps them as zero and they would be a lie in a header.
            if addr <= 0x29:
                regs[addr] = val
    return regs


def find_analysis_sibling(src):
    """Device_Codes_<stamp>.csv and Device_Analysis_<stamp>.csv come from the
    same capture, so the register dump is usually right next to the data."""
    p = pathlib.Path(src)
    m = re.match(r"(.*?)(Codes|Volts)(.*)\.csv$", p.name, re.I)
    if m:
        cand = p.with_name(f"{m.group(1)}Analysis{m.group(3)}.csv")
        if cand.exists():
            return cand
    for name in ("EVM_Analysis_sample.csv",):
        cand = p.with_name(name)
        if cand.exists():
            return cand
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--fs", type=float, default=None,
                    help="sample rate; default is derived from PRPCOUNT, else 500")
    ap.add_argument("--analysis", default=None,
                    help="Device_Analysis_*.csv holding the real register dump")
    a = ap.parse_args()

    rows, kind = read_evm(a.src)
    if not rows:
        sys.exit(f"{a.src}: no data rows")
    # Codes are the ground truth; volts are codes * 1.2 / 2^21 exactly.
    if kind == "volts":
        codes = [[round(v * FS_CODE / FS_VOLT) for v in r] for r in rows]
    else:
        codes = [[int(round(v)) for v in r] for r in rows]

    n = len(codes)

    # Real registers if the capture's Analysis export is available; computed
    # defaults only as a last resort, and the header says which it was.
    src_analysis = a.analysis or find_analysis_sibling(a.src)
    if src_analysis and pathlib.Path(src_analysis).exists():
        regs = read_analysis_registers(src_analysis)
        prov = f"registers from {pathlib.Path(src_analysis).name}"
    else:
        regs = {}
        prov = "registers ASSUMED (no Device_Analysis log found)"

    # PRPCOUNT is the rate the hardware really ran at - better evidence than
    # any command-line default.
    if a.fs is not None:
        fs = a.fs
    elif 0x1D in regs and regs[0x1D] > 0:
        fs = 4e6 / (regs[0x1D] + 1)
    else:
        fs = 500.0
    if not regs:
        regs = default_registers(prpcount=int(round(4e6 / fs)) - 1)

    stamp = "1970-01-01T00:00:00.000Z"      # fixtures must be reproducible
    out = pathlib.Path(a.out)

    hd = [f"# AFE4490 capture  n={n}  duration={n / fs:.6f} s  {stamp}  units=V",
          f"# t0 {stamp}  (wall clock of sample 0, t_s=0)",
          f"# armed {stamp}  (when Capture was pressed)",
          "# seq0 0  (firmware sample index of t_s=0)",
          "# channels  red=LED2(RED) amb2=LED2 ambient  ir=LED1(IR) amb1=LED1 ambient"
          "  redAmb=red-amb2  irAmb=ir-amb1",
          f"# rate segments (first sample @ Hz): 0@{fs:g}",
          f"# firmware V2.1.0  link usb  decimation 1",
          f"# converted from {pathlib.Path(a.src).name} by evm_to_capture.py, {prov}"]
    for addr in sorted(regs):
        hd.append(f"# register {addr:02X}={regs[addr]:06X}  {REG_NAMES.get(addr, '?')}")
    hd.append("sample,t_s,red,amb2,ir,amb1,redAmb,irAmb,marker")

    k = FS_VOLT / FS_CODE
    with out.with_suffix(".csv").open("w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(hd) + "\n")
        for i, c in enumerate(codes):
            cols = ",".join(f"{v * k:.8g}" for v in c)
            f.write(f"{i},{i / fs:.6f},{cols},\n")

    doc = {
        "device": "AFE4490", "kind": "capture", "exported": stamp,
        "captured": stamp, "samples": n,
        "duration_s": round(n / fs, 6),
        "t0": stamp, "armed": stamp, "seq0": 0,
        "rate_segments": [{"i": 0, "rate": fs}],
        "channels_meaning": {"red": "LED2 (RED)", "amb2": "LED2 ambient",
                             "ir": "LED1 (IR)", "amb1": "LED1 ambient",
                             "redAmb": "red - amb2", "irAmb": "ir - amb1"},
        "units": "adc_code", "full_scale_code": FS_CODE, "full_scale_volts": FS_VOLT,
        "source": {"fw": "V2.1.0", "link": "usb", "dec": 1,
                   "note": f"converted from {pathlib.Path(a.src).name}"},
        "registers": {f"{k2:02X}": f"{v:06X}" for k2, v in sorted(regs.items())},
        "markers": [],
        "channels": {name: [c[j] for c in codes] for j, name in enumerate(CHANNELS)},
    }
    out.with_suffix(".json").write_text(json.dumps(doc), encoding="utf-8")
    print(f"{n} samples @ {fs:g} Hz from {kind} -> "
          f"{out.with_suffix('.csv').name}, {out.with_suffix('.json').name}")


if __name__ == "__main__":
    main()
