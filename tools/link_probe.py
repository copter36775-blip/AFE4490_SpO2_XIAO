"""Talk to the AFE4490_XIAO firmware over USB CDC without the browser.

Runs the same protocol the web console uses, so it answers the first
question that matters on a fresh board: is the MCU alive, and is the SPI
link to the AFE actually working?

Run:  python tools/link_probe.py [COM5] [--stream 3]
"""
import sys
import time

import serial
import serial.tools.list_ports as list_ports


def pick_port(explicit=None):
    if explicit:
        return explicit
    for p in list_ports.comports():
        if 'Bluetooth' not in p.description:
            return p.device
    raise SystemExit('no USB serial port found')


def crc16(data, crc=0xFFFF):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class Link:
    """Same two-stream parser as the console: ASCII lines plus AA 55 frames."""

    def __init__(self, port):
        self.ser = serial.Serial(port, 115200, timeout=0.05)
        self.buf = bytearray()
        self.lines = []
        self.samples = []
        self.crc_err = 0
        self.frames = 0
        self.batch_sizes = []

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            chunk = self.ser.read(4096)
            if chunk:
                self.buf += chunk
                self._parse()
            else:
                time.sleep(0.005)

    def _parse(self):
        i = 0
        b = self.buf
        while i < len(b):
            if b[i] == 0xAA:
                if i + 1 >= len(b):
                    break
                if b[i + 1] == 0x55:
                    if i + 5 > len(b):
                        break
                    ln = b[i + 3] | (b[i + 4] << 8)
                    total = 5 + ln + 2
                    if i + total > len(b):
                        break
                    self._frame(bytes(b[i:i + total]), ln)
                    i += total
                    continue
            nl = -1
            hit = False
            for k in range(i, len(b)):
                if b[k] == 0x0A:
                    nl = k
                    break
                if b[k] == 0xAA:
                    nl, hit = k, True
                    break
            if hit:
                i = nl
                continue
            if nl < 0:
                break
            line = bytes(b[i:nl]).decode('ascii', 'replace').strip()
            if line:
                self.lines.append(line)
            i = nl + 1
        del self.buf[:i]

    def _frame(self, f, ln):
        want = f[5 + ln] | (f[6 + ln] << 8)
        if crc16(f[2:5 + ln]) != want:
            self.crc_err += 1
            return
        if f[2] != 0x01:
            return
        self.frames += 1
        p = f[5:5 + ln]
        count = p[4]
        self.batch_sizes.append(count)
        o = 8
        for _ in range(count):
            self.samples.append(tuple(
                int.from_bytes(p[o + 4 * j:o + 4 * j + 4], 'little', signed=True)
                for j in range(6)))
            o += 24

    def cmd(self, text, wait=0.35):
        self.ser.write((text + '\n').encode())
        self.ser.flush()
        before = len(self.lines)
        self.pump(wait)
        return self.lines[before:]


def main():
    args = [a for a in sys.argv[1:]]
    stream_s = 0.0
    if '--stream' in args:
        idx = args.index('--stream')
        stream_s = float(args[idx + 1])
        del args[idx:idx + 2]
    port = pick_port(args[0] if args else None)

    print('opening %s' % port)
    link = Link(port)
    time.sleep(0.4)
    link.ser.reset_input_buffer()
    link.pump(0.6)
    for l in link.lines:
        print('  boot | %s' % l)
    link.lines.clear()

    ok = True
    for c in ('PING', 'CAPS?', 'STATUS?'):
        for l in link.cmd(c):
            print('  %-8s | %s' % (c, l))

    print('\n-- SPI integrity ------------------------------------------')
    for l in link.cmd('SELFTEST:256', 1.5):
        print('  %s' % l)
        if 'FAIL' in l:
            ok = False

    print('\n-- register map -------------------------------------------')
    regs = {}
    for l in link.cmd('REG?', 2.0):
        if l.startswith('REG:') and '=' in l:
            a, _, v = l[4:].partition('=')
            if a != 'END':
                regs[int(a, 16)] = int(v, 16)
    print('  read %d registers' % len(regs))
    for a in (0x1D, 0x1E, 0x20, 0x21, 0x22, 0x23):
        if a in regs:
            print('    0x%02X = %06X' % (a, regs[a]))
    if 0x1D in regs and regs[0x1D]:
        print('  PRPCOUNT %d  ->  PRF %.1f Hz' % (regs[0x1D], 4e6 / (regs[0x1D] + 1)))

    if stream_s:
        print('\n-- streaming %.1f s ----------------------------------------' % stream_s)
        link.cmd('START', 0.2)
        link.samples.clear()
        t0 = time.time()
        link.pump(stream_s)
        dt = time.time() - t0
        link.cmd('STOP', 0.2)
        n = len(link.samples)
        print('  %d samples in %.2f s = %.1f Hz, crc errors %d' % (n, dt, n / dt, link.crc_err))
        if link.frames:
            avg = sum(link.batch_sizes) / len(link.batch_sizes)
            print('  %d frames = %.1f frames/s, %.2f samples per frame'
                  % (link.frames, link.frames / dt, avg))
        if n:
            cols = list(zip(*link.samples))
            names = ('LED2', 'ALED2', 'LED1', 'ALED1', 'LED2-A', 'LED1-A')
            for nm, c in zip(names, cols):
                mn, mx = min(c), max(c)
                mean = sum(c) / len(c)
                print('    %-7s mean %+10.0f  (%+.4f V)  span %d' %
                      (nm, mean, mean * 1.2 / 2097152, mx - mn))
        if link.crc_err:
            ok = False

    link.ser.close()
    print('\n%s' % ('OK' if ok else 'ISSUES FOUND'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
