"""Measure the BLE link the way the console sees it, without the browser.

The console can tell you a stream is stuttering. It cannot tell you whether
the samples died in the ring, in the transmit queue, or on the air - and
that is the only question worth answering. This connects over the same
Nordic UART Service Chrome uses, streams for a while, and puts the three
numbers side by side: what the firmware produced, what it sent, and what
arrived.

The board only advertises after a double press on D5, so --arm sends BLE:ON
over USB first and saves you the walk to the bench.

Run:  python tools/ble_probe.py --arm COM5 --seconds 20
      python tools/ble_probe.py --seconds 20 --dec 1
"""
import argparse
import asyncio
import struct
import sys
import time

from bleak import BleakClient, BleakScanner

NUS_SVC = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"   # device -> host, notify
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"   # host -> device, write
DEVICE_NAME = "AFE4490-XIAO"


def crc16(data, crc=0xFFFF):
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class Stream(object):
    """Protocol A1: ASCII lines and binary frames sharing one byte stream."""

    def __init__(self):
        self.buf = bytearray()
        self.lines = []
        self.frames = 0
        self.samples = 0
        self.crc_err = 0
        self.bytes = 0
        self.by_type = {}
        self.first_seq = None
        self.last_seq = None
        self.seq_gaps = 0

    def feed(self, chunk):
        self.bytes += len(chunk)
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
            nl = -1
            hit = False
            for k in range(i, len(b)):
                if b[k] == 0x0A:
                    nl = k
                    break
                if b[k] == 0xAA:
                    hit = True
                    nl = k
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
        want = pkt[5 + ln] | (pkt[6 + ln] << 8)
        if crc16(pkt[2:5 + ln]) != want:
            self.crc_err += 1
            return
        self.frames += 1
        ftype = pkt[2]
        self.by_type[ftype] = self.by_type.get(ftype, 0) + 1
        body = pkt[5:5 + ln]
        seq = struct.unpack_from("<I", body, 0)[0]
        count = body[4]
        # A gap here is a sample the firmware produced and never delivered.
        if self.last_seq is not None and seq != self.last_seq:
            self.seq_gaps += seq - self.last_seq
        if self.first_seq is None:
            self.first_seq = seq
        self.last_seq = seq + count
        self.samples += count


async def run(args):
    if args.arm:
        import serial
        with serial.Serial(args.arm, 115200, timeout=1.5) as ser:
            time.sleep(0.4)
            ser.reset_input_buffer()
            ser.write(b"BLE:ON\n")
            time.sleep(0.6)
            reply = ser.read(400).decode("ascii", "replace")
        print("armed over %s: %s" % (args.arm, reply.strip().splitlines()[-1:] or "(no reply)"))

    print("scanning for %s ..." % DEVICE_NAME)
    dev = await BleakScanner.find_device_by_filter(
        lambda d, ad: (d.name or "") == DEVICE_NAME
        or NUS_SVC in [str(u).lower() for u in (ad.service_uuids or [])],
        timeout=args.scan)
    if dev is None:
        print("FAIL: no advertising board found - double-press D5, or use --arm COM5")
        return 1
    print("found %s  %s" % (dev.name, dev.address))

    stream = Stream()
    async with BleakClient(dev) as client:
        print("connected, mtu %d" % client.mtu_size)

        def on_notify(_h, data):
            stream.feed(data)

        await client.start_notify(NUS_TX, on_notify)

        async def send(cmd):
            payload = (cmd + "\n").encode()
            for o in range(0, len(payload), 20):
                await client.write_gatt_char(NUS_RX, payload[o:o + 20], response=False)
            await asyncio.sleep(0.35)

        await send("PING")
        await send("DEC:%d" % args.dec)
        await send("STATUS?")
        await asyncio.sleep(0.4)

        stream.lines = []
        await send("START")
        t0 = time.time()
        base = (stream.samples, stream.frames, stream.bytes)
        print("streaming %.0f s at DEC:%d ..." % (args.seconds, args.dec))
        await asyncio.sleep(args.seconds)
        dt = time.time() - t0
        got = (stream.samples - base[0], stream.frames - base[1], stream.bytes - base[2])

        await send("STOP")
        await asyncio.sleep(0.3)
        await send("BLESTAT?")
        await send("STATUS?")
        await asyncio.sleep(0.8)
        try:
            await client.stop_notify(NUS_TX)
        except Exception:
            pass

    samples, frames, nbytes = got
    print("")
    print("-- what arrived ------------------------------------------")
    print("  %d samples in %.2f s = %.1f Hz" % (samples, dt, samples / dt))
    print("  %d frames = %.1f/s, %.2f samples per frame"
          % (frames, frames / dt, (samples / frames) if frames else 0))
    print("  %d bytes = %.2f kB/s on the air" % (nbytes, nbytes / dt / 1000.0))
    print("  crc errors %d" % stream.crc_err)
    types = ", ".join("0x%02X x%d" % (t, n) for t, n in sorted(stream.by_type.items()))
    print("  frame types: %s" % (types or "(none)"))
    print("  sequence gaps: %d samples" % stream.seq_gaps)

    print("")
    print("-- what the firmware says --------------------------------")
    for line in stream.lines:
        if line.startswith("BLESTAT:") or line.startswith("ST:"):
            print("  " + line)

    verdict = 0
    if stream.crc_err:
        print("")
        print("FAIL: %d frames failed CRC - the link is corrupting data" % stream.crc_err)
        verdict = 1
    if stream.seq_gaps:
        print("")
        print("FAIL: %d samples were produced and never delivered" % stream.seq_gaps)
        verdict = 1
    if not verdict:
        print("")
        print("OK: every sample the firmware numbered arrived, in order")
    return verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", metavar="COM", help="send BLE:ON over this USB port first")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--dec", type=int, default=4)
    ap.add_argument("--scan", type=float, default=12.0)
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
