"""Hardware check of the register-lab protocol added in firmware V2.1.0.

  WM:aa=vvvvvv,...  atomic multi-register write with read-back and seq=
  REGS? / REGS?HW   one-line register dump, shadow and silicon

What it proves on the real board:
  1. a full 30-register timing change goes over in one line and lands intact
     (read-back equals request, STATUS prpct and the frame rate follow it);
  2. the stream across that change has no sequence gaps and no CRC errors;
  3. a gain write is not flushed and reports the seq the change applies from;
  4. a batch with one illegal address changes nothing;
  5. shadow and silicon agree afterwards.

Run:  python tools/wm_test.py [--port COM5]
"""
import argparse
import sys
import time

import serial

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
from rate_sweep import Stream, pick_port, field  # noqa: E402


def gen_timing(T, duty=25, settle=50, rst=4):
    """The firmware's applyTiming() arithmetic, for an integer period T."""
    Q = T // 4
    ton = min(T * duty // 100, Q)
    settle = min(settle, Q // 2)
    if ton < settle + 202:
        ton = settle + 202
        if ton > Q:
            ton, settle = Q, (Q - 202 if Q > 202 else 1)
    se, le = ton - 2, ton - 1
    r = {0x05: settle, 0x06: se,
         0x09: Q, 0x0A: Q + le, 0x07: Q + settle, 0x08: Q + se,
         0x0B: 2 * Q + settle, 0x0C: 2 * Q + se,
         0x03: 3 * Q, 0x04: 3 * Q + le, 0x01: 3 * Q + settle, 0x02: 3 * Q + se}
    for k, (rs, re_, cs, ce) in enumerate([(0x15, 0x16, 0x0D, 0x0E), (0x17, 0x18, 0x0F, 0x10),
                                            (0x19, 0x1A, 0x11, 0x12), (0x1B, 0x1C, 0x13, 0x14)]):
        P = k * Q
        r[rs], r[re_], r[cs], r[ce] = P, P + rst - 1, P + rst, P + Q - 1
    r[0x1D] = T - 1
    pq = (T // 4) // 200
    numav = 0 if pq < 2 else min(15, pq - 2)
    return r, numav


class Link(object):
    def __init__(self, s):
        self.s, self.st = s, Stream()

    def drain(self, secs):
        t = time.time()
        while time.time() - t < secs:
            self.st.feed(self.s.read(max(1, self.s.in_waiting)) or b"")

    def ask(self, text, prefix, secs=2.0):
        mark = len(self.st.lines)
        self.s.write((text + chr(10)).encode())
        t = time.time()
        while time.time() - t < secs:
            self.st.feed(self.s.read(max(1, self.s.in_waiting)) or b"")
            hit = [l for l in self.st.lines[mark:] if l.startswith(prefix) or l.startswith("ERR:")]
            if hit:
                return hit[0]
        return ""


def parse_wm(line):
    items = line[len("ACK:WM:"):].split(",")
    kv, back = {}, {}
    for it in items:
        k, v = it.split("=", 1)
        if len(k) == 2:
            back[int(k, 16)] = int(v, 16)
        else:
            kv[k] = v
    return kv, back


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    args = ap.parse_args()
    fails = []

    def check(ok, what):
        print("  [%s] %s" % ("PASS" if ok else "FAIL", what))
        if not ok:
            fails.append(what)

    with serial.Serial(pick_port(args.port), 115200, timeout=0.05) as s:
        try:
            s.set_buffer_size(rx_size=1 << 20)
        except Exception:
            pass
        time.sleep(2.2)
        L = Link(s)
        L.ask("STOP", "ACK:STOP")
        L.ask("FMT:BIN", "ACK:FMT")
        L.ask("DEC:1", "ACK:DEC")
        pong = L.ask("PING", "AFE:PONG")
        caps = L.ask("CAPS?", "CAPS:")
        check(pong.endswith("V2.1.0"), "firmware reports V2.1.0 (%s)" % pong)
        check("WM=48" in caps, "CAPS advertises WM=48")

        sh = L.ask("REGS?", "REGS:")
        vals = [int(x, 16) for x in sh[5:].split(",")] if sh.startswith("REGS:") else []
        check(len(vals) == 49, "REGS? returns 49 registers in one line")

        L.ask("DEFAULTS", "ACK:DEFAULTS", 4)
        L.ask("START", "ACK:START")
        L.drain(1.0)
        base_frames, base_gaps, base_crc = L.st.frames, L.st.gaps, L.st.crc_err

        # 1. gain write, not flushed
        wm = L.ask("WM:22=015050", "ACK:WM:")
        kv, back = parse_wm(wm) if wm.startswith("ACK:WM:") else ({}, {})
        check(back.get(0x22) == 0x015050, "LEDCNTRL written and read back (%s)" % wm[:60])
        check(kv.get("flush") == "0", "a gain write is not flushed")
        seq_led = int(kv.get("seq", "-1"))
        check(seq_led >= L.st.last_seq if L.st.last_seq is not None else seq_led >= 0,
              "seq %d is not behind what already arrived (%s)" % (seq_led, L.st.last_seq))
        L.drain(0.8)

        # 2. whole timing change in one line, 500 -> 1000 Hz
        reg, numav = gen_timing(4000)
        pairs = ["%02X=%06X" % (a, v) for a, v in sorted(reg.items())]
        pairs.append("1E=%06X" % (0x100 | numav))
        line = "WM:" + ",".join(pairs)
        print("  WM line is %d bytes for %d registers" % (len(line), len(pairs)))
        wm = L.ask(line, "ACK:WM:", 3)
        kv, back = parse_wm(wm) if wm.startswith("ACK:WM:") else ({}, {})
        wrong = [a for a, v in reg.items() if back.get(a) != v]
        check(wm.startswith("ACK:WM:n=30"), "30 registers accepted in one transaction")
        check(not wrong, "every timing register read back as written %s" % wrong)
        check(kv.get("flush") == "1" and kv.get("prf") == "1000", "timing write flushes, prf=1000")
        L.drain(2.0)
        stt = L.ask("STATUS?", "ST:")
        check(field(stt, "prpct") == 3999, "STATUS prpct follows the raw PRPCOUNT (%s)" % field(stt, "prpct"))
        check(L.st.rate_hdr == 1000, "frame header rate is 1000 Hz (%d)" % L.st.rate_hdr)
        check(L.st.gaps == base_gaps, "no sequence gaps across the change (%d)" % (L.st.gaps - base_gaps))
        check(L.st.crc_err == base_crc, "no CRC errors (%d)" % (L.st.crc_err - base_crc))
        check(L.st.frames > base_frames, "frames kept flowing")

        # 3. an illegal address anywhere rejects the whole batch
        before = L.ask("REGS?", "REGS:")
        bad = L.ask("WM:22=010101,2A=000000", "ACK:WM:")
        after = L.ask("REGS?", "REGS:")
        check(bad.startswith("ERR:WM_READONLY:2A"), "read-only address rejected (%s)" % bad)
        check(before == after and before, "the rejected batch changed nothing")

        # 4. shadow agrees with silicon
        L.ask("STOP", "ACK:STOP")
        L.drain(0.3)
        hw = L.ask("REGS?HW", "REGSHW:")
        sh = L.ask("REGS?", "REGS:")
        hv = [int(x, 16) for x in hw[7:].split(",")]
        sv = [int(x, 16) for x in sh[5:].split(",")]
        rw = [a for a in range(0x01, 0x27)] + [0x29]
        diff = ["%02X" % a for a in rw if hv[a] != sv[a]]
        check(len(hv) == 49 and not diff, "shadow == silicon for every writable register %s" % diff)

        L.ask("DEFAULTS", "ACK:DEFAULTS", 4)
        L.ask("STOP", "ACK:STOP")

    print()
    print("FAIL: %d" % len(fails) if fails else "PASS: register-lab protocol verified on hardware")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
