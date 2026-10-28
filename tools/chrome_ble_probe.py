"""Run the real console in real Chrome against the real board, and measure it.

tools/ble_probe.py answers "can the radio carry this?" from Python. It cannot
answer "can Chrome?", and that turned out to be the question that mattered:
the same link that lost nothing to a bleak client lost most of its samples to
the browser. The difference is not the radio.

This drives Chrome over the DevTools protocol: it serves the console from
localhost (Web Bluetooth refuses a file:// URL), clicks the BLE button, answers
the device chooser through DeviceAccess.selectPrompt, streams, and then reports
three things side by side - what the page received, when the radio went quiet,
and what the firmware says it lost.

`--norender` neuters the animation loop after connecting. If the losses go away
with it, the bottleneck is the renderer competing with notification delivery on
the same main thread, not the link.

Chrome must be headed - headless has no Bluetooth adapter - so a window opens
for the length of the run.

Run:  python tools/chrome_ble_probe.py --arm COM5 --seconds 30 --dec 4
      python tools/chrome_ble_probe.py --seconds 30 --dec 4 --norender
"""
import argparse
import asyncio
import functools
import http.server
import json
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import websockets

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# Counts notifications as the page receives them, and records every silence
# long enough to mean something. A consumer that cannot keep up shows here as
# a gap long before it shows on the device as a dropped sample.
HOOK = """
(function(){
  if (window.__probe) return 'already';
  window.__probe = { n:0, bytes:0, maxGap:0, last:0, stalls:[],
                     t0:performance.now() };
  var inner = window.feed;
  window.feed = function(chunk){
    var p = window.__probe, t = performance.now();
    if (p.last) {
      var g = t - p.last;
      if (g > p.maxGap) p.maxGap = g;
      if (g > 250) p.stalls.push([Math.round(t - p.t0), Math.round(g)]);
    }
    p.last = t; p.n++; p.bytes += chunk.length;
    return inner.apply(this, arguments);
  };
  /* The question the stall histogram cannot answer: did the sweep keep
     moving? A playout buffer that runs dry is a frozen trace, so sample
     `playing` rather than inferring it. */
  window.__play = { dry:0, n:0, minLag:1e9, maxLag:0 };
  setInterval(function(){
    var q = window.__play; q.n++;
    if (typeof playing !== 'undefined' && !playing) q.dry++;
    if (typeof playLagMs !== 'undefined'){
      if (playLagMs < q.minLag) q.minLag = playLagMs;
      if (playLagMs > q.maxLag) q.maxLag = playLagMs;
    }
  }, 50);
  return 'hooked';
})()
"""

RESET_PROBE = ("__probe.maxGap=0; __probe.last=0; __probe.stalls.length=0; "
               "__probe.t0=performance.now(); "
               "__play.dry=0; __play.n=0; __play.minLag=1e9; __play.maxLag=0; 1")

READ_PROBE = """({
  s:link.samples, f:link.frames, e:link.crcErr,
  n:__probe.n, b:__probe.bytes, g:__probe.maxGap, stalls:__probe.stalls,
  t:performance.now(), rate:stats.rate,
  dry:__play.dry, ticks:__play.n, minLag:__play.minLag, maxLag:__play.maxLag,
  drop:document.getElementById('sbDrop').textContent
})"""


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def handle_one_request(self):
        # Chrome opens and abandons speculative sockets; a stack trace per
        # reset buries the only output anyone came here to read.
        try:
            http.server.SimpleHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True


def serve(directory, port):
    handler = functools.partial(Quiet, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def field(line, key, default=0):
    for kv in str(line or "").split(","):
        if kv.startswith(key + "="):
            try:
                return int(kv.split("=", 1)[1])
            except ValueError:
                return default
    return default


class CDP(object):
    """Just enough DevTools protocol to click a button and read a variable."""

    def __init__(self, ws):
        self.ws = ws
        self.next_id = 1
        self.replies = {}
        self.events = []

    async def pump(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg:
                    self.replies[msg["id"]] = msg
                else:
                    self.events.append(msg)
        except Exception:
            pass

    async def call(self, method, params=None, timeout=30.0):
        mid = self.next_id
        self.next_id += 1
        await self.ws.send(json.dumps({"id": mid, "method": method,
                                       "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if mid in self.replies:
                msg = self.replies.pop(mid)
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})
            await asyncio.sleep(0.01)
        raise RuntimeError("%s timed out" % method)

    async def js(self, expr, await_promise=False):
        res = await self.call("Runtime.evaluate", {
            "expression": expr, "returnByValue": True,
            "awaitPromise": await_promise, "userGesture": True})
        if res.get("exceptionDetails"):
            raise RuntimeError(json.dumps(res["exceptionDetails"])[:300])
        return res.get("result", {}).get("value")


async def attach(dbg):
    for _ in range(120):
        try:
            data = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/json" % dbg, timeout=1).read().decode())
            for t in data:
                if t.get("type") == "page" and "localhost" in (t.get("url") or ""):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return None


async def pick_device(cdp):
    """Answer the Web Bluetooth chooser.

    Chrome opens the prompt empty and re-emits it as devices are discovered,
    so the first event is always the useless one. It also throttles the scan
    behind a background window, which is why the page is brought to the front
    first and the button clicked again partway through.
    """
    await cdp.js("document.getElementById('btnBle').click(); 1")
    deadline = time.time() + 45
    reclicked = False
    while time.time() < deadline:
        for msg in list(cdp.events):
            if msg.get("method") != "DeviceAccess.deviceRequestPrompted":
                continue
            for d in msg["params"].get("devices", []):
                if (d.get("name") or "").startswith("AFE4490"):
                    return msg["params"]["id"], d
        if not reclicked and time.time() > deadline - 25:
            reclicked = True
            try:
                await cdp.call("Page.bringToFront")
            except Exception:
                pass
            await cdp.js("document.getElementById('btnBle').click(); 1")
        await asyncio.sleep(0.25)
    return None, None


async def measure(cdp, args):
    if args.dec:
        await cdp.js("send('DEC:%d', /^ACK:DEC/, 3000)" % args.dec, True)
        await cdp.js("document.getElementById('inDec').value=%d; 1" % args.dec)
        await asyncio.sleep(1.5)

    if args.norender:
        # Leave the data path alone and stop only the drawing, so the only
        # difference between two runs is renderer load.
        await cdp.js("window.requestAnimationFrame = function(){ return 0; }; 1")
        print("rendering disabled for this run")

    # The console auto-starts only when it likes the look of the bus; a
    # measurement must not depend on that judgement.
    await cdp.js("if (!streaming) setStreaming(true); 1")
    await asyncio.sleep(2.0)

    fw0 = await cdp.js("send('STATUS?', /^ST:/, 3000)", True)
    ble0 = await cdp.js("send('BLESTAT?', /^BLESTAT:/, 3000)", True)
    base = await cdp.js(READ_PROBE)
    await cdp.js(RESET_PROBE)
    print("streaming %.0f s in Chrome ..." % args.seconds)
    await asyncio.sleep(args.seconds)
    end = await cdp.js(READ_PROBE)
    stat = st = None
    for _ in range(4):
        stat = stat or await cdp.js("send('BLESTAT?', /^BLESTAT:/, 4000)", True)
        st = st or await cdp.js("send('STATUS?', /^ST:/, 4000)", True)
        if stat and st:
            break
        await asyncio.sleep(1.0)

    dt = (end["t"] - base["t"]) / 1000.0
    ds, df = end["s"] - base["s"], end["f"] - base["f"]
    dn, db = end["n"] - base["n"], end["b"] - base["b"]
    want = end.get("rate") or 0

    print("")
    print("-- what Chrome received ----------------------------------")
    print("  %d samples in %.2f s = %.1f Hz   (firmware is sending %s Hz)"
          % (ds, dt, ds / dt, want))
    if want:
        print("  delivered %.0f %% of the stream" % (100.0 * (ds / dt) / want))
    print("  %d frames = %.1f/s, %.2f samples per frame"
          % (df, df / dt, (ds / df) if df else 0))
    print("  %d notifications = %.1f/s, %.2f kB/s"
          % (dn, dn / dt, db / dt / 1000.0))
    print("  crc errors %d" % (end["e"] - base["e"]))

    stalls = end.get("stalls") or []
    print("")
    print("-- when the radio went quiet -----------------------------")
    if not stalls:
        print("  no silence longer than 250 ms")
    else:
        print("  %d silences over 250 ms, %.1f s of the %.0f s run"
              % (len(stalls), sum(g for _, g in stalls) / 1000.0, dt))
        for at, g in stalls[:12]:
            print("    t+%6.1f s   %5d ms" % (at / 1000.0, g))
        if len(stalls) > 12:
            print("    ... %d more" % (len(stalls) - 12))

    ticks = end.get("ticks") or 0
    print("")
    print("-- did the sweep keep moving? ----------------------------")
    if ticks:
        print("  playout buffer dry in %.1f %% of %d samples"
              % (100.0 * (end.get("dry") or 0) / ticks, ticks))
        print("  display lag %d - %d ms"
              % (end.get("minLag") or 0, end.get("maxLag") or 0))
    else:
        print("  (no samples - page has no playout instrumentation)")

    drop = field(st, "drop") - field(fw0, "drop")
    pkts = field(stat, "packets") - field(ble0, "packets")
    print("")
    print("-- the radio, over this run only -------------------------")
    print("  samples converted and never sent:  %d" % drop)
    print("  ADC_RDY seen while busy (miss):    %d"
          % (field(st, "miss") - field(fw0, "miss")))
    print("  notifications the SoftDevice took: %d (%.1f/s)" % (pkts, pkts / dt))
    print("  backpressure events:  %d"
          % (field(stat, "backpressure") - field(ble0, "backpressure")))
    print("  transmit queue drops: %d"
          % (field(stat, "qdrops") - field(ble0, "qdrops")))
    print("  mtu %d, dle %d, interval %d units = %.1f ms"
          % (field(stat, "mtu"), field(stat, "dle"),
             field(stat, "ci"), field(stat, "ci") * 1.25))
    print("  streaming: %s"
          % ("yes" if field(st, "stream") else "NO - nothing measured"))

    print("")
    if not field(st, "stream"):
        print("FAIL: the stream never started, so nothing was measured")
    elif end["e"] - base["e"]:
        print("FAIL: CRC errors - the link is corrupting data")
    elif drop:
        print("FAIL: the firmware lost %d samples it had already converted" % drop)
    else:
        print("OK: nothing lost on the Chrome path either")
        return 0
    return 1


async def run(args):
    if args.arm:
        import serial
        with serial.Serial(args.arm, 115200, timeout=1.5) as ser:
            time.sleep(0.4)
            ser.reset_input_buffer()
            ser.write(b"BLE:ON\n")
            time.sleep(0.6)
            ser.read(400)
        print("armed advertising over %s" % args.arm)

    port = free_port()
    serve(WEB, port)
    url = "http://localhost:%d/%s" % (port, args.page)
    dbg = free_port()
    exe = CHROME if pathlib.Path(CHROME).exists() else EDGE
    profile = tempfile.mkdtemp(prefix="cdpble")

    proc = subprocess.Popen(
        [exe, "--remote-debugging-port=%d" % dbg, "--user-data-dir=" + profile,
         "--no-first-run", "--no-default-browser-check",
         "--disable-features=OptimizationGuideModelDownloading",
         "--window-size=1500,950", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        ws_url = await attach(dbg)
        if not ws_url:
            print("FAIL: could not attach to Chrome")
            return 1

        async with websockets.connect(ws_url, max_size=None) as ws:
            cdp = CDP(ws)
            asyncio.ensure_future(cdp.pump())
            await cdp.call("Runtime.enable")
            await cdp.call("Page.enable")
            await cdp.call("DeviceAccess.enable")

            ready = False
            for _ in range(80):
                ready = await cdp.js(
                    "document.readyState === 'complete' && "
                    "!!document.getElementById('btnBle') && "
                    "typeof feed === 'function'")
                if ready:
                    break
                await asyncio.sleep(0.25)
            if not ready:
                print("FAIL: the console page never finished loading")
                return 1

            try:
                await cdp.call("Page.bringToFront")
            except Exception:
                pass
            print("hook: " + str(await cdp.js(HOOK)))

            prompt_id, pick = await pick_device(cdp)
            if pick is None:
                print("FAIL: the chooser never offered an AFE4490 - is another "
                      "tab still connected to it, or is it not advertising?")
                return 1
            print("chooser offered: %s" % (pick.get("name") or pick["id"]))
            await cdp.call("DeviceAccess.selectPrompt",
                           {"id": prompt_id, "deviceId": pick["id"]})

            connected = False
            for _ in range(160):
                connected = await cdp.js(
                    "(typeof link !== 'undefined') && !!link.connected")
                if connected:
                    break
                await asyncio.sleep(0.25)
            if not connected:
                print("FAIL: the page never reported a connection")
                tail = await cdp.js("(document.getElementById('log')||{})"
                                    ".textContent||''")
                print("  page log tail: " + str(tail)[-500:])
                return 1
            print("connected, letting the handshake settle ...")
            await asyncio.sleep(10.0)

            return await measure(cdp, args)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", metavar="COM", help="send BLE:ON over this USB port first")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--dec", type=int, default=0, help="set decimation once connected")
    ap.add_argument("--norender", action="store_true",
                    help="stop the animation loop, to separate renderer load "
                         "from link throughput")
    ap.add_argument("--page", default="afe4490_studio_V3.html")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
