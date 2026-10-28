"""Boot the console in headless Chrome and prove the renderer actually works.

studio_shot.py only checks that the page loads and the DOM is present. That
cannot tell a working sweep from four blank canvases, which is exactly the
failure that matters here. This one drives the real page objects and then
reads pixels back:

  * every visible trace canvas has ink on it, in both trace colours, rather
    than just a grid line or the "no data" note
  * the incremental path is genuinely incremental - full repaints per frame
    stay far below 1, which is the whole point of the rewrite
  * split lanes really do get independent scales (the bug that made the
    plethysmograph look like two flat lines)
  * the sweep head advances and wraps twice without leaving the ring
  * capture mode records, analyses and draws in all three domains
  * preferences survive a round trip through localStorage

Frames are pumped by calling render() with synthetic timestamps rather than
waiting on requestAnimationFrame: under Chrome's virtual clock rAF fires only
a handful of times, which is not enough to test anything, and pumping makes
the whole run deterministic.

Run:  python tools/console_selftest.py
"""
import json
import pathlib
import re
import subprocess
import sys
import tempfile

# How long to wait for headless Chrome. The default suits an idle machine;
# a loaded one can need several times that, and a browser that never got
# scheduled is not a failing test. Override with AFE_TEST_TIMEOUT=1200.
def _chrome_timeout(default):
    import os
    try:
        return max(default, int(os.environ.get("AFE_TEST_TIMEOUT", "0")))
    except ValueError:
        return default


ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = ROOT / "web" / "afe4490_studio.html"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

DRIVER = r"""
<div id="selftest" style="display:none"></div>
<script>
(function () {
  var R = { errors: [], checks: [] };
  function emit() {
    document.getElementById('selftest').textContent = JSON.stringify(R);
  }
  function check(name, ok, detail) {
    R.checks.push({ name: name, ok: !!ok, detail: String(detail === undefined ? '' : detail) });
  }
  window.addEventListener('error', function (e) {
    R.errors.push((e.message || 'error') + ' @' + (e.lineno || '?')); emit();
  });
  window.addEventListener('unhandledrejection', function (e) {
    R.errors.push('rejection: ' + (e.reason && e.reason.message || e.reason)); emit();
  });

  /* Count painted pixels and how many carry each trace hue. A canvas with
     only a grid line or an error note on it must not pass as a canvas with
     two traces on it. */
  function ink(cv) {
    var g = cv.getContext('2d'), d;
    try { d = g.getImageData(0, 0, cv.width, cv.height).data; }
    catch (e) { return { total: -1, err: e.message }; }
    var lit = 0, red = 0, violet = 0, mint = 0;
    for (var i = 0; i < d.length; i += 4) {
      if (d[i + 3] < 8) continue;
      lit++;
      var r = d[i], gg = d[i + 1], b = d[i + 2];
      if (r > 120 && r > gg + 30 && r > b + 20) red++;
      else if (b > 110 && b > gg + 25 && r > 90) violet++;
      else if (gg > 110 && gg > r + 30) mint++;
    }
    return { total: lit, red: red, violet: violet, mint: mint, px: cv.width * cv.height };
  }

  var frames = 0, clock = 0;
  function pump(count, dtMs) {
    var raf = window.requestAnimationFrame;
    window.requestAnimationFrame = function () { return 0; };
    try {
      for (var i = 0; i < count; i++) { clock += (dtMs || 8.333); frames++; render(clock); }
    } finally { window.requestAnimationFrame = raf; }
  }

  var fs = 500, n = 0, FSC = 2097152;
  var dcIr = 0.48 * FSC, dcRed = 0.40 * FSC, piIr = 0.014, R0 = 0.62;

  function feed(count) {
    for (var i = 0; i < count; i++, n++) {
      var t = n / fs, ph = (t * 72 / 60) % 1;
      var p = Math.exp(-Math.pow((ph - 0.16) / 0.085, 2))
            + 0.34 * Math.exp(-Math.pow((ph - 0.40) / 0.10, 2))
            + 0.11 * Math.exp(-Math.pow((ph - 0.62) / 0.16, 2)) - 0.42;
      var resp = 0.10 * Math.sin(2 * Math.PI * 0.24 * t);
      var amb = 2600 + 900 * Math.sin(2 * Math.PI * 0.17 * t) + (Math.random() - 0.5) * 220;
      var ir = dcIr * (1 - piIr * (p + resp)) + (Math.random() - 0.5) * 600;
      var red = dcRed * (1 - piIr * R0 * (p + resp)) + (Math.random() - 0.5) * 600;
      ingest(Math.round(red + amb), Math.round(amb),
             Math.round(ir + amb), Math.round(amb),
             Math.round(red), Math.round(ir));
    }
    link.samples = n;
  }

  /* Feed the stream and draw it at the same time, the way a live session
     does: 500 Hz of samples spread across frames of dtMs each. */
  function stream(seconds, dtMs) {
    dtMs = dtMs || 8.333;
    var nf = Math.round(seconds * 1000 / dtMs);
    var per = fs * dtMs / 1000;
    var owed = 0;
    for (var i = 0; i < nf; i++) {
      owed += per;
      var take = Math.floor(owed); owed -= take;
      feed(take);
      pump(1, dtMs);
    }
  }

  window.addEventListener('load', function () { setTimeout(run, 120); });

  function run() {
    try { body(); } catch (e) { R.errors.push('driver: ' + e.message + ' | ' + (e.stack || '').split('\n')[1]); }
    R.done = true;
    document.title = 'SELFTEST DONE';
    emit();
  }

  function body() {
    // ---- 0. put the page into a live-looking state ------------------
    var timing = { 0x01:6050,0x02:7998,0x03:6000,0x04:7999,0x05:50,0x06:1998,
      0x07:2050,0x08:3998,0x09:2000,0x0A:3999,0x0B:4050,0x0C:5998,0x0D:4,
      0x0E:1999,0x0F:2004,0x10:3999,0x11:4004,0x12:5999,0x13:6004,0x14:7999,
      0x15:0,0x16:3,0x17:2000,0x18:2003,0x19:4000,0x1A:4003,0x1B:6000,
      0x1C:6003,0x1D:7999,0x1E:0x000108,0x20:0,0x21:0x000000,
      0x22:0x012828,0x23:0x020000 };
    Object.keys(timing).forEach(function (k) { regValues[k | 0] = timing[k]; });
    renderRegTable(); syncControls();
    document.getElementById('overlay').classList.add('hide');
    link.connected = true; streaming = true; stats.rate = fs;
    chartOn.Amb = 1; applyCharts();
    clock = performance.now();
    pump(2);                      // let the charts measure themselves

    // ---- 1. the sweep paints ----------------------------------------
    stream(6.0);
    check('render loop ran', frames > 400, frames + ' frames');
    var names = ['Pleth', 'Ac', 'Raw', 'Amb'];
    charts.forEach(function (c) {
      if (names.indexOf(c.key) < 0) return;
      check(c.key + ': canvas sized', c.w > 100 && c.h > 40, c.w + 'x' + c.h);
      check(c.key + ': sweep advanced', c.col > 20, 'col=' + c.col);
      var k = ink(c.fg);
      check(c.key + ': trace has ink', k.total > 600, JSON.stringify(k));
      check(c.key + ': both traces drawn', k.red > 60 && (k.violet > 60 || k.mint > 60),
            JSON.stringify(k));
      var b = ink(c.bg);
      check(c.key + ': background painted', b.total > 0.5 * b.px, b.total + '/' + b.px);
    });

    // ---- 2. it is incremental, not a full repaint per frame ---------
    var before = renderStats.fullRepaints, f0 = frames;
    stream(4.0);
    var dr = renderStats.fullRepaints - before, df = frames - f0;
    check('incremental (full repaints per frame < 0.5)', df > 100 && dr / df < 0.5,
          dr + ' repaints / ' + df + ' frames');
    var widest = 0;
    charts.forEach(function (c) { widest = Math.max(widest, c.w); });
    check('band is narrow (cols per frame well under one screen width)',
          renderStats.cols > 0 && renderStats.cols < 0.25 * widest * 4,
          renderStats.cols + ' cols across 4 charts, widest ' + widest);

    // ---- 3. split lanes get independent scales ----------------------
    var pleth = charts.filter(function (c) { return c.key === 'Pleth'; })[0];
    check('split mode gives two lanes', pleth.groups.length === 2, pleth.groups.length + ' groups');
    var a = pleth.groups[0], b2 = pleth.groups[1];
    check('lanes do not overlap in y',
          Math.abs((a.top + a.h) - b2.top) < 12 && a.h > 10,
          'A ' + a.top.toFixed(0) + '+' + a.h.toFixed(0) + '  B ' + b2.top.toFixed(0));
    // RED sits near 0.40 FS and IR near 0.48 FS, so an honest per-lane
    // auto-scale has to land on visibly different windows. Sharing one
    // axis is what made both look like flat lines.
    check('each lane auto-scaled to its own signal',
          a.drawn && b2.drawn && Math.abs(a.dMin - b2.dMin) > 10000,
          'A[' + a.dMin.toFixed(0) + ',' + a.dMax.toFixed(0) + ']  ' +
          'B[' + b2.dMin.toFixed(0) + ',' + b2.dMax.toFixed(0) + ']');
    check('lane spans are tight, not full-scale',
          (a.dMax - a.dMin) < 0.20 * FSC && (b2.dMax - b2.dMin) < 0.20 * FSC,
          (a.dMax - a.dMin).toFixed(0) + ' / ' + (b2.dMax - b2.dMin).toFixed(0));

    // ---- 4. overlay mode collapses to one lane ----------------------
    laneMode = 'overlay';
    charts.forEach(function (c) { c.buildGroups(); c.layout(); });
    stream(1.5);
    check('overlay mode gives one lane', pleth.groups.length === 1, pleth.groups.length + ' groups');
    check('overlay still paints', ink(pleth.fg).total > 400, JSON.stringify(ink(pleth.fg)));
    laneMode = 'split';
    charts.forEach(function (c) { c.buildGroups(); c.layout(); });
    stream(2.0);

    // ---- 5. the head wraps without falling off the ring -------------
    var ac = charts.filter(function (c) { return c.key === 'Ac'; })[0];
    var width = ac.valid.length, seen = {}, laps = Math.ceil(2 * width / ac.step) + 40;
    for (var i = 0; i < laps; i++) {
      ac.push([Math.sin(i / 90) * 5000, Math.cos(i / 110) * 5000]);
      seen[ac.col] = 1;
    }
    check('head stays in range over two laps', ac.col >= 0 && ac.col < width,
          'col=' + ac.col + ' of ' + width);
    check('head visited every column', Object.keys(seen).length >= width - 1,
          Object.keys(seen).length + '/' + width);
    ac.full = true; stream(1.0);

    // ---- 6. hidden charts cost nothing ------------------------------
    var amb = charts.filter(function (c) { return c.key === 'Amb'; })[0];
    chartOn.Amb = 0; applyCharts(); pump(3);
    check('hidden chart paints nothing', amb.renderFrame(clock) === 0, 'painted 0');
    chartOn.Amb = 1; applyCharts(); stream(1.5);

    // ---- 7. capture: snapshot ---------------------------------------
    document.getElementById('capMode').value = 'snapshot';
    document.getElementById('capN').value = '2048';
    capStart();
    check('snapshot captured samples', cap.n >= 2000, 'n=' + cap.n);
    check('capture switched the view', viewMode === 'capture', viewMode);
    check('capture holds all six channels', cap.ch && cap.ch.length === 6,
          cap.ch ? cap.ch.length : 'null');
    // The snapshot has to be in time order: the newest sample belongs last.
    var last = cap.ch[2][cap.n - 1], live = at(buf.ir, 0);
    check('snapshot is chronological', Math.abs(last - live) < Math.abs(live) * 0.02,
          'last=' + last.toFixed(0) + ' live=' + live.toFixed(0));

    // ---- 8. capture plots draw in every domain ----------------------
    cap.plots[0].dom = 'time';
    cap.plots[1].dom = 'fft';
    cap.plots[2].dom = 'hist';
    cap.plots[1].ch = 2;           // IR, which carries the pulse
    cap.dirty = true;
    pump(4);
    var cvs = document.querySelectorAll('#capGrid canvas');
    check('capture grid built', cvs.length === capLayout,
          cvs.length + ' canvases, layout ' + capLayout);
    /* A canvas can be full of ink and still be invisible: fitCanvas falls
       back to 600x150 for the backing store when clientHeight is 0, so the
       drawing succeeds while the element occupies no space at all. That is
       exactly how the capture grid shipped collapsed once. Measure the
       laid-out box, not the bitmap. */
    var grid = document.getElementById('capGrid');
    var col = document.querySelector('.plotcol');
    check('capture grid fills the column',
          grid.clientHeight > 0.5 * col.clientHeight,
          grid.clientHeight + ' of ' + col.clientHeight + ' px');
    var small = [];
    for (var q = 0; q < cvs.length; q++)
      if (cvs[q].clientHeight < 60 || cvs[q].clientWidth < 300)
        small.push(cvs[q].clientWidth + 'x' + cvs[q].clientHeight);
    check('every capture canvas has real on-screen size', small.length === 0,
          small.length ? small.join(' ') : cvs[0].clientWidth + 'x' + cvs[0].clientHeight);
    check('capture backing store matches its box',
          Math.abs(cvs[0].width - cvs[0].clientWidth * Math.min(devicePixelRatio || 1, 2)) < 3,
          cvs[0].width + ' vs ' + cvs[0].clientWidth);
    ['time', 'fft', 'hist'].forEach(function (dom, i) {
      check('capture ' + dom + ' plot has ink', ink(cvs[i]).total > 400,
            JSON.stringify(ink(cvs[i])));
    });
    var st = document.getElementById('capSt1').textContent;
    check('spectrum reports SNR and ENOB',
          /SNR/.test(st) && /ENOB/.test(st) && !/NaN/.test(st), st.slice(0, 100));
    var f0 = /([\d.]+)\s*Hz/.exec(st);
    check('spectrum found the 1.2 Hz pulse',
          f0 && Math.abs(parseFloat(f0[1]) - 1.2) < 0.25, f0 ? f0[1] + ' Hz' : 'no f0');
    check('rail statistics filled',
          document.getElementById('capTable').textContent.length > 60,
          document.getElementById('capTable').textContent.replace(/\s+/g, ' ').slice(0, 70));

    // ---- 9. layout switching ----------------------------------------
    [1, 2, 6, 4].forEach(function (np) {
      capLayout = np; capBuildGrid(); sizeDirty = true; pump(3);
      var c2 = document.querySelectorAll('#capGrid canvas');
      var shortest = 1e9;
      for (var q = 0; q < c2.length; q++) shortest = Math.min(shortest, c2[q].clientHeight);
      check('capture layout ' + np,
            c2.length === np && shortest > 40,
            c2.length + ' canvases, shortest ' + shortest + ' px');
    });

    // ---- 10. capture units and detrend -------------------------------
    capUnits = 'code'; capDetrend = 'band'; cap.dirty = true;
    var S = capSeries(0), mean = 0;
    for (var j = 0; j < S.data.length; j++) mean += S.data[j];
    mean /= S.data.length;
    check('band detrend removes DC', Math.abs(mean) < 800, 'mean=' + mean.toFixed(1));
    capUnits = 'v'; capDetrend = 'none'; cap.dirty = true;
    var V = capSeries(0);
    check('volts scaling is sane', V.data[0] > 0.05 && V.data[0] < 1.25,
          V.data[0].toFixed(4) + ' V');
    capUnits = 'ua'; regValues[0x21] = 0x000002;      // RF = 100 k
    var A = capSeries(0);
    check('current scaling follows RF', A.unit.indexOf('A') >= 0 && A.data[0] > 0 && A.data[0] < 5,
          A.data[0].toFixed(4) + A.unit);
    capUnits = 'v'; regValues[0x21] = 0; cap.dirty = true;

    // ---- 11. preferences round trip ----------------------------------
    windowSec = 8; quality = 'balanced'; fpsCap = 60; invertPleth = false;
    savePrefs();
    windowSec = 5; quality = 'high'; fpsCap = 0; invertPleth = true;
    loadPrefs();
    check('prefs round trip',
          windowSec === 8 && quality === 'balanced' && fpsCap === 60 && invertPleth === false,
          windowSec + '/' + quality + '/' + fpsCap + '/' + invertPleth);
    try { localStorage.removeItem('afe4490.prefs'); } catch (e) {}
    fpsCap = 0; invertPleth = true; windowSec = 5;
    charts.forEach(function (c) { c.setInvert(true); });

    // ---- 12. back to the monitor, still painting ----------------------
    setView('monitor');
    charts.forEach(function (c) { c.full = true; });
    stream(2.0);
    var k2 = ink(ac.fg);
    check('monitor repaints after leaving capture', k2.total > 600, JSON.stringify(k2));

    // ---- 13. sweep window change reallocates cleanly ------------------
    windowSec = 12; applySweep(); stream(2.0);
    check('sweep window applied', ac.visible === 12 * fs, ac.visible + ' samples');
    check('still painting after resweep', ink(ac.fg).total > 400, JSON.stringify(ink(ac.fg)));

    // ---- 14. the vitals actually resolved -----------------------------
    check('pulse rate locked near 72 bpm',
          metrics.hr && Math.abs(metrics.hr.bpm - 72) < 6,
          metrics.hr ? metrics.hr.bpm.toFixed(1) + ' bpm' : 'null');
    check('SpO2 resolved near 95 %',
          metrics.ox && Math.abs(metrics.ox.spo2 - 95) < 4,
          metrics.ox ? metrics.ox.spo2.toFixed(1) + ' %' : 'null');

    // ---- 15. reset view to defaults ---------------------------------
    windowSec = 20; quality = 'raw'; laneMode = 'overlay'; invertPleth = false;
    capLayout = 1; capUnits = 'code'; fpsCap = 30;
    document.getElementById('btnResetView').click();
    pump(3);
    check('reset restores display defaults',
          windowSec === 5 && quality === 'high' && laneMode === 'split'
          && invertPleth === true && fpsCap === 0 && capLayout === 4 && capUnits === 'v',
          [windowSec, quality, laneMode, invertPleth, fpsCap, capLayout, capUnits].join('/'));
    check('reset restores the chart toggles',
          chartOn.Pleth === 1 && chartOn.Ac === 1 && chartOn.Amb === 0,
          JSON.stringify(chartOn));
    check('reset left the register map alone',
          regValues[0x1D] === 7999 && regValues[0x22] === 0x012828,
          'PRPCOUNT=' + regValues[0x1D]);
    check('reset rebuilt the capture grid',
          document.querySelectorAll('#capGrid canvas').length === 4,
          document.querySelectorAll('#capGrid canvas').length + '');
    setView('monitor'); stream(2.0);
    check('monitor still paints after reset', ink(ac.fg).total > 400,
          JSON.stringify(ink(ac.fg)));

    check('no JS errors', R.errors.length === 0, R.errors.join(' | '));
  }
})();
</script>
"""


def main():
    global PAGE
    if "--page" in sys.argv:
        PAGE = pathlib.Path(sys.argv[sys.argv.index("--page") + 1])
    exe = CHROME if pathlib.Path(CHROME).exists() else EDGE
    if not pathlib.Path(exe).exists():
        print("no Chrome or Edge found")
        return 2
    html = PAGE.read_text(encoding="utf-8").replace("</body>", DRIVER + "\n</body>")
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        page = tmp / "selftest.html"
        page.write_text(html, encoding="utf-8")
        dom = subprocess.run(
            [exe, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--window-size=1600,1000", "--force-device-scale-factor=1",
             "--user-data-dir=" + str(tmp / "p"),
             "--virtual-time-budget=30000", "--dump-dom", page.as_uri()],
            capture_output=True, text=True, timeout=_chrome_timeout(300)).stdout or ""

    m = re.search(r'<div id="selftest"[^>]*>(.*?)</div>', dom, re.S)
    if not m:
        print("FAIL: the harness produced no output at all")
        return 1
    raw = (m.group(1).replace("&quot;", '"').replace("&lt;", "<")
           .replace("&gt;", ">").replace("&amp;", "&"))
    try:
        res = json.loads(raw)
    except ValueError as exc:
        print("FAIL: unreadable harness output (%s)\n%s" % (exc, raw[:400]))
        return 1

    checks = res.get("checks", [])
    failed = [c for c in checks if not c["ok"]]
    for c in checks:
        print("  %s  %-56s %s" % ("ok  " if c["ok"] else "FAIL", c["name"], c["detail"][:96]))
    for e in res.get("errors", []):
        print("  JS ERROR: " + e)

    if not res.get("done"):
        print("\nFAIL: the harness did not finish (%d checks ran)" % len(checks))
        return 1
    if res.get("errors"):
        print("\nFAIL: %d uncaught JS error(s)" % len(res["errors"]))
        return 1
    if failed:
        print("\nFAIL: %d of %d checks" % (len(failed), len(checks)))
        return 1
    print("\nPASS all %d checks" % len(checks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
