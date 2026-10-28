"""Render web/afe4490_studio.html to a PNG with a synthetic plethysmograph.

Boots the real console in headless Chrome, injects a driver that fakes the
AFE4490 register set and feeds a physiologically shaped PPG straight into
ingest(), spins the animation loop, then screenshots the result.

Doubles as the page's smoke test: any uncaught JS error is trapped and
written into <title>, and the script exits non-zero when one shows up.

Two headless limitations to know about, neither of which is a page bug:

  * each chart well stacks a foreground canvas over a background one, and
    headless composites only the lower of the two. getImageData on the
    foreground returns thousands of lit pixels either way - they just never
    reach the captured frame - so the harness copies the foreground down
    onto the background before the shot.
  * the sweep advances on wall-clock time, and under a virtual clock there
    is almost none of it, so a captured sweep is always shorter than a live
    one. The capture view has no such dependency and photographs exactly.

For real verification of the renderer, run tools/console_selftest.py: it
reads pixels back and asserts on them rather than trusting a picture.

Run:  python tools/studio_shot.py [out.png] [--width 1920] [--height 1080]
      python tools/studio_shot.py --check          (no PNG, just validate)
"""
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = ROOT / "web" / "afe4490_studio.html"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# Injected just before </body>. Everything here runs against the real page
# objects, so it exercises the same code path a live device would.
DRIVER = r"""
<style>
/* Chrome's virtual clock freezes partway through a CSS transition, which
   makes tab highlights look wrong in a capture. Snap them for the shot. */
*, *::before, *::after { transition: none !important; animation: none !important; }
</style>
<script>
(function () {
  var errors = [];
  window.addEventListener('error', function (e) {
    errors.push((e.message || 'error') + ' @' + (e.lineno || '?'));
    document.title = 'JSERROR: ' + errors.join(' | ');
  });
  window.addEventListener('unhandledrejection', function (e) {
    errors.push('unhandled rejection: ' + (e.reason && e.reason.message || e.reason));
    document.title = 'JSERROR: ' + errors.join(' | ');
  });

  window.addEventListener('load', function () { setTimeout(run, 150); });

  function run() {
    try {
      // ---- 1. fake the register map the firmware would have reported ----
      var defaults = {
        0x00: 0x000001, 0x1D: 7999, 0x1E: 0x000108,
        0x20: 0x000000, 0x21: 0x000000, 0x22: 0x012828, 0x23: 0x020000,
        0x29: 0x000000, 0x30: 0x000000
      };
      // PRF = 500 Hz, 25 % duty: datasheet Table 2 verbatim.
      var timing = {
        0x01: 6050, 0x02: 7998, 0x03: 6000, 0x04: 7999,
        0x05:   50, 0x06: 1998, 0x07: 2050, 0x08: 3998,
        0x09: 2000, 0x0A: 3999, 0x0B: 4050, 0x0C: 5998,
        0x0D:    4, 0x0E: 1999, 0x0F: 2004, 0x10: 3999,
        0x11: 4004, 0x12: 5999, 0x13: 6004, 0x14: 7999,
        0x15:    0, 0x16:    3, 0x17: 2000, 0x18: 2003,
        0x19: 4000, 0x1A: 4003, 0x1B: 6000, 0x1C: 6003
      };
      Object.keys(defaults).forEach(function (k) { regValues[k | 0] = defaults[k]; });
      Object.keys(timing).forEach(function (k) { regValues[k | 0] = timing[k]; });
      for (var a = 0x2A; a <= 0x2F; a++) regValues[a] = 0;
      renderRegTable();
      syncControls();
      chartOn.Amb = 1; applyCharts();

      // ---- 2. cosmetics so the shot looks like a live session ----------
      document.getElementById('overlay').classList.add('hide');
      link.connected = true;
      streaming = true;
      document.getElementById('liveDot').classList.add('live');
      document.getElementById('btnStream').disabled = false;
      document.getElementById('btnStream').textContent = 'Stop';
      document.getElementById('btnStream').classList.add('stop');
      document.getElementById('btnConnect').textContent = 'Disconnect';
      document.getElementById('sbLink').textContent = '\u25cf connected';
      document.getElementById('sbLink').style.color = 'var(--ok)';
      document.getElementById('pillFw').innerHTML = 'FW <b>V1.0.0</b>';
      document.getElementById('pillRate').innerHTML = 'PRF <b>500 Hz</b>';
      document.getElementById('pillLed').innerHTML = 'LED <b>7.8 / 7.8 mA</b>';
      document.getElementById('sbVbat').textContent = '4.06 V';

      // ---- 3. synthetic plethysmograph ---------------------------------
      // DC set to ~48 % of full scale, 1.4 % perfusion on IR and a red/IR
      // ratio of R = 0.62, which lands SpO2 near 95 %.
      var fs = 500, n = 0;
      var FSC = 2097152;
      var dcIr = 0.48 * FSC, dcRed = 0.40 * FSC;
      var piIr = 0.014, R = 0.62;
      var piRed = piIr * R;

      function pulse(ph) {
        // Systolic upstroke, dicrotic notch, diastolic decay.
        var v = Math.exp(-Math.pow((ph - 0.16) / 0.085, 2))
              + 0.34 * Math.exp(-Math.pow((ph - 0.40) / 0.10, 2))
              + 0.11 * Math.exp(-Math.pow((ph - 0.62) / 0.16, 2));
        return v - 0.42;
      }

      function feed(count) {
        for (var i = 0; i < count; i++, n++) {
          var t = n / fs;
          var ph = (t * 72 / 60) % 1;                 // 72 bpm
          var p = pulse(ph);
          var resp = 0.10 * Math.sin(2 * Math.PI * 0.24 * t);   // baseline wander
          // Ambient in a lit room: a slow drift with a little noise on it.
          var amb = 2600 + 900 * Math.sin(2 * Math.PI * 0.17 * t)
                  + 260 * Math.sin(2 * Math.PI * 0.9 * t)
                  + (Math.random() - 0.5) * 220;

          var ir  = dcIr  * (1 - piIr  * (p + resp)) + (Math.random() - 0.5) * 600;
          var red = dcRed * (1 - piRed * (p + resp)) + (Math.random() - 0.5) * 600;

          ingest(Math.round(red + amb), Math.round(amb),
                 Math.round(ir + amb),  Math.round(amb),
                 Math.round(red),       Math.round(ir));
        }
      }

      // Prime ~10 s of history so the metric cache and the FFT both have
      // something real to chew on.
      feed(fs * 10);
      link.samples = n;
      link.frames = Math.floor(n / 8);
      stats.rate = fs;

      // The sweep advances on wall-clock time, and under Chrome's virtual
      // clock there is essentially none of it: the playout buffer never
      // opens, so a screenshot catches a display that has drawn nothing.
      //
      // Rather than fake a clock, push the ring straight into the charts and
      // paint once. That is the same data the playout path would deliver,
      // just without waiting for time that is not going to pass.
      //
      // It runs repeatedly and late on purpose. Fonts, the ambient card and
      // the scrollbar all settle over the first seconds of virtual time, and
      // any one of those resizes a chart - which reallocates its columns and
      // clears it. Re-priming on a short interval means whatever frame the
      // capture lands on has a full sweep on it.
      // Headless Chrome composites only the lower canvas of each well. The
      // trace really is painted - getImageData on the foreground returns
      // thousands of lit pixels - but it never reaches the captured frame.
      // Copying the foreground down onto the background makes the screenshot
      // show what a real browser shows. It has to happen after every render,
      // because adopting a new auto-scale repaints the background and would
      // wipe a one-off copy. Purely a capture workaround: nothing in the page
      // does this.
      function flatten() {
        charts.forEach(function (c) {
          if (!c.w) return;
          c.bctx.save();
          c.bctx.setTransform(1, 0, 0, 1, 0, 0);
          c.bctx.drawImage(c.fg, 0, 0);
          c.bctx.restore();
        });
      }
      var innerRender = window.render;
      window.render = function (ts) {
        var r = innerRender.call(this, ts);
        if (viewMode === 'monitor') flatten();
        return r;
      };

      function primeSweep() {
        var raf = window.requestAnimationFrame;
        window.requestAnimationFrame = function () { return 0; };
        try {
          var span = Math.min(buf.count, Math.round(windowSec * stats.rate));
          charts.forEach(function (c) { c.reset(); });
          for (var i = span; i > 0; i--) {
            var k = ((buf.head - i) % 4096 + 4096) % 4096;
            charts.forEach(function (c) {
              c.push([buf[c.defs[0].src][k], buf[c.defs[1].src][k]]);
            });
          }
          dispRead = buf.written;
          // reset() clears each lane's `drawn` flag, and updateScale only
          // runs every 90 ms - so without clearing lastScale the very next
          // render() finds no drawn lane and paints nothing at all.
          charts.forEach(function (c) { c.lastScale = 0; c.full = true; });
          render(performance.now());

          flatten();
        } finally { window.requestAnimationFrame = raf; }
        link.samples = n;
        link.frames = Math.floor(n / 8);
      }

      setInterval(function () { feed(25); link.samples = n; link.frames = Math.floor(n / 8); }, 50);

      // Land on a tab worth photographing.
      var wanted = (location.hash || '#acq').slice(1);
      var tab = document.querySelector('.tab[data-p="' + wanted + '"]');
      if (tab) tab.click();
      // Late, deliberately: fonts, the ambient card and the scrollbar all
      // settle in the first couple of seconds of virtual time, and any one
      // of those resizes a chart, which reallocates its columns and clears
      // it. Fill the sweep once nothing is moving any more, just before the
      // virtual-time budget expires and the frame is captured.
      if (wanted !== 'cap') setInterval(primeSweep, 250);

      // The maths panels read the same ring the monitor does, so they need
      // it full before the shot.
      if (wanted === 'math') {
        setView('math');
        setInterval(primeSweep, 250);
      }

      // The capture page is only worth a picture with a capture in it.
      if (wanted === 'cap') {
        setTimeout(function () {
          document.getElementById('capMode').value = 'snapshot';
          document.getElementById('capN').value = '2048';
          cap.plots[0].dom = 'time';  cap.plots[1].dom = 'time';
          cap.plots[2].dom = 'fftlo'; cap.plots[3].dom = 'hist';
          capBuildGrid();          // keep the header pickers in step
          capStart();
        }, 500);
      }

      setTimeout(function () {
        if (!errors.length) document.title = 'READY ' + n + ' samples';
      }, 900);
    } catch (err) {
      document.title = 'JSERROR: ' + err.message;
    }
  }
})();
</script>
"""


def build(tmpdir: pathlib.Path, frag: str) -> pathlib.Path:
    html = PAGE.read_text(encoding="utf-8")
    if "</body>" not in html:
        raise SystemExit("no </body> in " + str(PAGE))
    html = html.replace("</body>", frag + "\n</body>")
    out = tmpdir / "driven.html"
    out.write_text(html, encoding="utf-8")
    return out


def run_chrome(target: str, args: list, tmpdir: pathlib.Path) -> str:
    exe = CHROME if pathlib.Path(CHROME).exists() else EDGE
    # --headless=new, not old: each well stacks a background canvas under a
    # foreground one, and old headless composites only the lower of the two.
    # The trace is genuinely painted either way - getImageData proves it - but
    # old headless leaves it out of the captured frame.
    cmd = [
        exe, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", "--force-device-scale-factor=1",
        "--user-data-dir=" + str(tmpdir / "profile"),
        "--virtual-time-budget=4000",
    ] + args + [target]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return res.stdout or ""


def main() -> int:
    global PAGE
    argv = sys.argv[1:]
    check_only = "--check" in argv
    argv = [a for a in argv if a != "--check"]
    if "--page" in argv:
        i = argv.index("--page")
        PAGE = pathlib.Path(argv[i + 1])
        del argv[i:i + 2]

    width = height = None
    if "--width" in argv:
        width = argv[argv.index("--width") + 1]
    if "--height" in argv:
        height = argv[argv.index("--height") + 1]
    positional = [a for a in argv if not a.startswith("--") and not a.isdigit()]
    tab = "acq"
    if positional and "#" in positional[0]:
        positional[0], tab = positional[0].split("#", 1)
    out = pathlib.Path(positional[0]) if positional else ROOT / "docs" / "studio.png"

    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        page = build(tmp, DRIVER)
        if tab != "acq":
            page = pathlib.Path(str(page))
        target = page.as_uri() + ("#" + tab if tab else "")

        # Pass 1: dump the DOM and read the title for a verdict.
        exe = CHROME if pathlib.Path(CHROME).exists() else EDGE
        dom = subprocess.run(
            [exe, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--user-data-dir=" + str(tmp / "p1"),
             "--virtual-time-budget=4000", "--dump-dom", target],
            capture_output=True, text=True, timeout=120).stdout or ""

        m = re.search(r"<title>(.*?)</title>", dom, re.S)
        title = (m.group(1).strip() if m else "(no title)")
        if title.startswith("JSERROR"):
            print("FAIL " + title)
            return 1
        print("page title: " + title)

        # A couple of structural assertions so a silent render failure
        # cannot pass as success.
        rows = dom.count('class="addr"')
        if rows < 40:
            print("FAIL register table only rendered %d rows" % rows)
            return 1
        print("register rows: %d" % rows)
        for probe in ("numSpo2", "cPleth", "cTiming", "regBody"):
            if probe not in dom:
                print("FAIL missing element " + probe)
                return 1

        if check_only:
            print("OK")
            return 0

        # Pass 2: screenshot.
        out.parent.mkdir(parents=True, exist_ok=True)
        # Chrome resolves a relative --screenshot path against its own cwd,
        # which is not this one. Always hand it an absolute path.
        args = ["--screenshot=" + str(out.resolve())]
        if width and height:
            args.append("--window-size=%s,%s" % (width, height))
        else:
            args.append("--window-size=1920,1080")
        run_chrome(target, args, tmp)
        if not out.exists():
            print("FAIL no screenshot produced")
            return 1
        print("wrote %s (%d bytes)" % (out, out.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
