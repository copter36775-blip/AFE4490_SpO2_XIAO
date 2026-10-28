"""Find the sample rate this board and this link actually sustain.

The pulse rate the AFE can be told to run at, the rate its timing windows
still hold at, and the rate the link can carry are three different numbers,
and only the first one is in a datasheet. This walks a list of rates over
USB, streams at each, and reports what arrived against what was produced.

A rate is only honest if `drop` stays at zero: the firmware counts a sample
it converted and could not ship, so a clean run means the whole chain kept
up, not just that the console saw a plausible number.

Run:  python tools/rate_sweep.py
      python tools/rate_sweep.py --port COM7 --dwell 4
      python tools/rate_sweep.py --rates 500,1000,2000,4000,4900
"""
import argparse
import struct
import sys
import time

import serial
import serial.tools.list_ports as list_ports

DEFAULT_RATES = [125, 250, 500, 1000, 2000, 3000, 4000, 4500, 4900, 5000]


def pick_port(explicit=None):
    if explicit:
        return explicit
    for p in list_ports.comports():
        if "Bluetooth" not in (p.description or ""):
            return p.device
    raise SystemExit("no USB serial port found")


def crc16(data, crc=0xFFFF):
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class Stream(object):
    """Protocol A1: binary frames and ASCII lines sharing one byte stream."""

    def __init__(self):
        self.buf = bytearray()
        self.lines = []
        self.samples = 0
        self.frames = 0
        self.crc_err = 0
        self.gaps = 0
        self.last_seq = None
        self.rate_hdr = 0
        self.wire = 0            # payload bytes actually on the link
        self.types = {}

    def feed(self, chunk):
        self.buf += chunk
        b = self.buf
        i = 0
        while i < len(b):
            if b[i] == 0xAA:
                if i + 1 >= len(b):
                    break
                if b[i + 1] == 0x55:
                    if i + 5 > len(b):
                        break
                    ln = b[i + 3] | (b[i + 4] << 8)
                    if ln > 4096:
                        i += 1
                        continue
                    total = 5 + ln + 2
                    if i + total > len(b):
                        break
                    self._frame(bytes(b[i:i + total]), ln)
                    i += total
                    continue
                i += 1
                continue
            nl, hit = -1, False
            for k in range(i, len(b)):
                if b[k] == 0x0A:
                    nl = k
                    break
                if b[k] == 0xAA:
                    hit, nl = True, k
                    break
            if hit:
                i = nl
                continue
            if nl < 0:
                break
            text = bytes(b[i:nl]).decode("ascii", "replace").strip()
            if text:
                self.lines.append(text)
            i = nl + 1
        if i:
            del b[:i]

    def _frame(self, pkt, ln):
        if crc16(pkt[2:5 + ln]) != (pkt[5 + ln] | (pkt[6 + ln] << 8)):
            self.crc_err += 1
            return
        self.frames += 1
        # Count what the frame really was rather than assuming 24 bytes a
        # sample: type 0x02 carries 12, and reporting the wrong one turns a
        # halved byte rate into a doubled one on the way out.
        self.wire += len(pkt)
        self.types[pkt[2]] = self.types.get(pkt[2], 0) + 1
        body = pkt[5:5 + ln]
        seq = struct.unpack_from("<I", body, 0)[0]
        count = body[4]
        self.rate_hdr = struct.unpack_from("<H", body, 6)[0]
        if self.last_seq is not None and seq != self.last_seq:
            self.gaps += seq - self.last_seq
        self.last_seq = seq + count
        self.samples += count


def cmd(port, text, wait=0.6):
    port.reset_input_buffer()
    port.write((text + "\n").encode())
    time.sleep(wait)
    return [l for l in port.read(6000).decode("ascii", "replace").splitlines() if l.strip()]


def field(line, key, default=0):
    for kv in str(line or "").split(","):
        if kv.startswith(key + "="):
            try:
                return int(kv.split("=", 1)[1])
            except ValueError:
                return default
    return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--dwell", type=float, default=3.0)
    ap.add_argument("--rates", help="comma separated, default %s"
                                    % ",".join(str(r) for r in DEFAULT_RATES))
    args = ap.parse_args()
    rates = ([int(x) for x in args.rates.split(",")] if args.rates else DEFAULT_RATES)

    port = pick_port(args.port)
    print("port %s, %.0f s per rate\n" % (port, args.dwell))
    print("  asked   set   AFE converted   received    kB/s  frame   drop  late  gaps  crc")
    print("  " + "-" * 82)

    worst = None
    with serial.Serial(port, 115200, timeout=0.05) as s:
        try:
            s.set_buffer_size(rx_size=1 << 20)      # Windows driver buffer
        except Exception:
            pass
        time.sleep(2.2)
        cmd(s, "STOP", 0.8)
        cmd(s, "FMT:BIN", 0.4)
        cmd(s, "DEC:1", 0.4)

        for want in rates:
            ack = [l for l in cmd(s, "PRF:%d" % want, 0.9) if l.startswith("ACK:PRF:")]
            if not ack:
                print("  %5d   (no reply)" % want)
                continue
            body = ack[0][len("ACK:PRF:"):]
            got = int(body.split(",")[0])
            swin, cwin = field(body, "swin"), field(body, "cwin")

            base = [l for l in cmd(s, "STATUS?", 0.7) if l.startswith("ST:")]
            prpct = field(base[0], "prpct") if base else 0

            # Everything from here on goes through the frame parser. A
            # plain read() while the stream runs returns binary frames mixed
            # with the ASCII reply, and decoding that as text produces an
            # ST: line with corrupted fields - which is exactly how this
            # harness came to report a 73 kHz conversion rate.
            st = Stream()

            def drain(seconds):
                t = time.time()
                while time.time() - t < seconds:
                    st.feed(s.read(max(1, s.in_waiting)) or b"")

            def ask(text, seconds=0.6):
                mark = len(st.lines)
                s.write((text + chr(10)).encode())
                drain(seconds)
                return st.lines[mark:]

            def latest(lines, pre):
                hit = [l for l in lines if l.startswith(pre)]
                return hit[-1] if hit else ""

            s.reset_input_buffer()
            ask("START", 0.35)
            # START zeroes drop/txdrop/miss but NOT rdy, which counts ADC_RDY
            # since boot, so its baseline has to be taken after the reset.
            rdy0 = field(latest(ask("STATUS?", 0.5), "ST:"), "rdy")
            # rdy spans from here to the closing STATUS, which is longer than
            # the sample window by both round trips. Time it separately rather
            # than dividing it by dt and reporting a rate 12 % too high.
            t_rdy0 = time.time()

            base_samples, base_frames, base_wire = st.samples, st.frames, st.wire
            t0 = time.time()
            drain(args.dwell)
            dt = time.time() - t0
            got_s = st.samples - base_samples
            got_f = st.frames - base_frames
            nbytes = st.wire - base_wire

            end = latest(ask("STATUS?", 0.6), "ST:")
            rdy_dt = time.time() - t_rdy0
            ask("STOP", 0.4)
            drop = field(end, "drop") + field(end, "txdrop")
            miss = field(end, "miss")
            rdy = field(end, "rdy") - rdy0

            flag = ""
            if swin < 200 or cwin < 200:
                flag = "  <- windows short of the 200-count minimum"
            elif drop > 0:
                flag = "  <- the board could not ship it"
            elif st.gaps > 0:
                # Sequence numbers are contiguous on the wire, so a gap here
                # with drop == 0 means this script lost bytes, not the board.
                flag = "  <- this reader fell behind, board was fine"
            if drop == 0 and st.gaps == 0 and swin >= 200 and cwin >= 200:
                worst = max(worst or 0, got)

            kind = ("0x%02X" % max(st.types, key=st.types.get)) if st.types else "-"
            st.types.clear()
            print("  %5d %5d  %8.1f Hz  %7.1f Hz  %5.1f  %-5s %6d %5d %5d  %3d%s"
                  % (want, got, rdy / max(rdy_dt, 1e-6), got_s / dt,
                     nbytes / dt / 1000.0, kind, drop, miss, st.gaps,
                     st.crc_err, flag))

        cmd(s, "PRF:500", 0.9)
        cmd(s, "DEC:1", 0.4)

    print()
    if worst:
        print("Highest rate that streamed clean over USB: %d Hz" % worst)
    print("A clean row means the AFE converted, the MCU read all six registers,")
    print("and the link shipped every sample - drop counts what fell out anywhere.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
