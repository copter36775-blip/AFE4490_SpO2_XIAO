"""Measure what one animation frame of the console asks the machine to do.

The obvious way to benchmark a canvas renderer - time the draw calls - does
not work. Chrome's 2D canvas records commands on the main thread and
rasterises them later, so performance.now() around stroke() measures only how
long it took to *describe* the work. That is why an earlier version of this
tool reported 1.1 ms/frame for a console that was visibly running at 30 fps.

So this counts the work instead, by instrumenting CanvasRenderingContext2D:
path segments, path length in device pixels, pixels cleared, pixels filled,
and how many draw calls carried a shadowBlur (each of which is a full
gaussian blur of everything it touches). Those numbers are hardware
independent, they are what the rasteriser actually has to chew through, and
they are directly comparable between two builds.

    python tools/render_bench.py [frames] [--page other.html] [--size WxH]
                                 [--hz 120] [--label NAME]

--page runs the same harness against an older copy of the console, which is
how the before/after numbers in the docs were produced. The function list and
the counters are probed at run time, so it works on both the full-repaint
renderer and the incremental one.
"""
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


# --headless=old on purpose: this harness ends the run with
# --virtual-time-budget, and the new headless mode does not honour that
# for --dump-dom - it simply never returns. The other harnesses wait on
# their own output instead, which is why they can use the new mode.

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

DRIVER = r"""
<div id="benchout" style="display:none"></div>
<script>
(function () {
  var N = __FRAMES__;
  var out = [];
  function say(s) { out.push(s); }

  try {
    // ================================================================
    // 1. instrument the 2D context
    // ================================================================
    var W = { seg:0, pathPx:0, clearPx:0, fillPx:0, strokes:0, fills:0,
              texts:0, shadowed:0, gradients:0, saves:0 };
    var C = CanvasRenderingContext2D.prototype;
    var meta = new WeakMap();
    function m(ctx) {
      var v = meta.get(ctx);
      if (!v) { v = { sx:1, sy:1, x:0, y:0 }; meta.set(ctx, v); }
      return v;
    }
    var oSetTransform = C.setTransform;
    C.setTransform = function (a, b, c, d, e, f) {
      if (typeof a === 'number') { var v = m(this); v.sx = a; v.sy = d; }
      return oSetTransform.apply(this, arguments);
    };
    var oMoveTo = C.moveTo, oLineTo = C.lineTo;
    C.moveTo = function (x, y) { var v = m(this); v.x = x; v.y = y; W.seg++; return oMoveTo.apply(this, arguments); };
    C.lineTo = function (x, y) {
      var v = m(this);
      W.seg++;
      W.pathPx += Math.hypot((x - v.x) * v.sx, (y - v.y) * v.sy);
      v.x = x; v.y = y;
      return oLineTo.apply(this, arguments);
    };
    var oClear = C.clearRect;
    C.clearRect = function (x, y, w, h) { var v = m(this); W.clearPx += Math.abs(w*v.sx*h*v.sy); return oClear.apply(this, arguments); };
    var oFillRect = C.fillRect;
    C.fillRect = function (x, y, w, h) {
      var v = m(this); W.fillPx += Math.abs(w*v.sx*h*v.sy); W.fills++;
      if (this.shadowBlur > 0) W.shadowed++;
      return oFillRect.apply(this, arguments);
    };
    var oStroke = C.stroke;
    C.stroke = function () { W.strokes++; if (this.shadowBlur > 0) W.shadowed++; return oStroke.apply(this, arguments); };
    var oFill = C.fill;
    C.fill = function () { W.fills++; if (this.shadowBlur > 0) W.shadowed++; return oFill.apply(this, arguments); };
    var oText = C.fillText;
    C.fillText = function () { W.texts++; return oText.apply(this, arguments); };
    var oGrad = C.createLinearGradient;
    C.createLinearGradient = function () { W.gradients++; return oGrad.apply(this, arguments); };
    var oSave = C.save;
    C.save = function () { W.saves++; return oSave.apply(this, arguments); };

    // ================================================================
    // 2. a realistic streaming state
    // ================================================================
    var timing = {0x01:6050,0x02:7998,0x03:6000,0x04:7999,0x05:50,0x06:1998,
      0x07:2050,0x08:3998,0x09:2000,0x0A:3999,0x0B:4050,0x0C:5998,0x0D:4,
      0x0E:1999,0x0F:2004,0x10:3999,0x11:4004,0x12:5999,0x13:6004,0x14:7999,
      0x15:0,0x16:3,0x17:2000,0x18:2003,0x19:4000,0x1A:4003,0x1B:6000,
      0x1C:6003,0x1D:7999,0x1E:0x000108,0x22:0x012828,0x23:0x020000};
    Object.keys(timing).forEach(function (k) { regValues[k|0] = timing[k]; });
    renderRegTable(); syncControls();
    document.getElementById('overlay').classList.add('hide');
    link.connected = true; streaming = true; stats.rate = 500;
    // Both builds must draw on every call, or the comparison is meaningless.
    if (typeof fpsCap !== 'undefined') fpsCap = 0;
    if (typeof renderHz !== 'undefined') renderHz = 1000;
    if (typeof chartOn === 'object') { chartOn.Amb = 1; applyCharts(); }

    var fs = 500, n = 0, FSC = 2097152;
    var dcIr = 0.48*FSC, dcRed = 0.40*FSC, piIr = 0.014, R0 = 0.62;
    function feed(count) {
      for (var i = 0; i < count; i++, n++) {
        var t = n/fs, ph = (t*72/60)%1;
        var p = Math.exp(-Math.pow((ph-0.16)/0.085,2))
              + 0.34*Math.exp(-Math.pow((ph-0.40)/0.10,2)) - 0.42;
        var amb = 2600 + 900*Math.sin(2*Math.PI*0.17*t) + (Math.random()-0.5)*220;
        var ir  = dcIr *(1 - piIr   *p) + (Math.random()-0.5)*600;
        var red = dcRed*(1 - piIr*R0*p) + (Math.random()-0.5)*600;
        ingest(Math.round(red+amb), Math.round(amb), Math.round(ir+amb),
               Math.round(amb), Math.round(red), Math.round(ir));
      }
      link.samples = n;
    }

    // ================================================================
    // 3. wrap whatever hot functions this build has
    // ================================================================
    var acc = {}, depth = 0;
    function wrap(name, host, key) {
      var orig = host[key];
      if (typeof orig !== 'function') return;
      acc[name] = 0;
      host[key] = function () {
        depth++;
        var t0 = performance.now();
        try { return orig.apply(this, arguments); }
        finally { depth--; acc[name] += performance.now() - t0; }
      };
    }
    ['fitCanvas','grid','trace','extent','axisLabels','drawEmpty',
     'drawFft','drawTiming','refreshMetrics','estimateHr','estimateSpo2',
     'textUpdate','playout','hud','drawSpark']
      .forEach(function (k) { wrap(k, window, k); });
    if (typeof SweepChart === 'function') {
      ['renderFrame','paintAll','paintRange','strokeCols','updateScale',
       'paintBg','push','drawCursor']
        .forEach(function (k) { wrap('SweepChart.'+k, SweepChart.prototype, k); });
    }

    // ================================================================
    // 4. run render() at the real draw cadence
    // ================================================================
    var realRaf = window.requestAnimationFrame;
    window.requestAnimationFrame = function () { return 0; };

    var stepMs = 1000/__HZ__;
    var clock = performance.now();
    var owed = 0, perFrame = fs*stepMs/1000;
    function tick() {
      owed += perFrame;
      var take = Math.floor(owed); owed -= take;
      feed(take);
      clock += stepMs;
      render(clock);
    }

    // Warm up: fill a window and let the auto-scale settle, so the first
    // unavoidable full repaint is not counted.
    for (var w2 = 0; w2 < Math.ceil(7000/stepMs); w2++) tick();
    for (var k in acc) acc[k] = 0;
    for (var k2 in W) W[k2] = 0;
    var fullBefore = (typeof renderStats === 'object') ? renderStats.fullRepaints : 0;

    var t0 = performance.now();
    for (var f = 0; f < N; f++) tick();
    var total = performance.now() - t0;
    window.requestAnimationFrame = realRaf;

    // ================================================================
    // 5. report
    // ================================================================
    function per(v) { return (v/N); }
    function big(v) {
      if (v >= 1e6) return (v/1e6).toFixed(2) + ' M';
      if (v >= 1e3) return (v/1e3).toFixed(1) + ' k';
      return v.toFixed(1);
    }
    say('page          ' + '__LABEL__');
    say('frames        ' + N + ' at ' + __HZ__ + ' fps, window ' + innerWidth + 'x' + innerHeight
        + ', dpr ' + (window.devicePixelRatio || 1));
    say('');
    say('-- rasteriser work per frame (hardware independent) ----------');
    say('  path segments      ' + big(per(W.seg)));
    say('  path length        ' + big(per(W.pathPx)) + ' device px');
    say('  pixels cleared     ' + big(per(W.clearPx)));
    say('  pixels filled      ' + big(per(W.fillPx)));
    say('  stroke() calls     ' + per(W.strokes).toFixed(1));
    say('  fill() calls       ' + per(W.fills).toFixed(1));
    say('  fillText() calls   ' + per(W.texts).toFixed(1));
    say('  draws with shadowBlur ' + per(W.shadowed).toFixed(2)
        + (W.shadowed ? '   <-- each one is a full gaussian blur' : ''));
    if (typeof renderStats === 'object') {
      say('  full repaints      ' + ((renderStats.fullRepaints-fullBefore)/N).toFixed(3) + ' per frame');
      say('  columns repainted  ' + renderStats.cols + ' on the last frame');
    }
    say('');
    say('-- javascript, command recording only ------------------------');
    say('   (headless rasterises off-thread, so this is NOT frame cost)');
    say('  per_frame_ms       ' + (total/N).toFixed(3));
    var names = Object.keys(acc).sort(function (a, b) { return acc[b]-acc[a]; });
    names.forEach(function (nm) {
      if (acc[nm] > 0.5)
        say('  ' + nm + '  ' + (acc[nm]/N).toFixed(3) + ' ms ('
            + (100*acc[nm]/total).toFixed(1) + '%)');
    });
  } catch (e) {
    say('ERROR ' + e.message + ' | ' + (e.stack||'').split('\n')[1]);
  }
  document.getElementById('benchout').textContent = out.join('\n');
})();
</script>
"""


def run(page: pathlib.Path, frames: int, hz: int, size: str, label: str) -> str:
    html = page.read_text(encoding="utf-8")
    frag = (DRIVER.replace("__FRAMES__", str(frames))
                  .replace("__HZ__", str(hz))
                  .replace("__LABEL__", label))
    html = html.replace("</body>", frag + "\n</body>")
    exe = CHROME if pathlib.Path(CHROME).exists() else EDGE
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        driven = tmp / "bench.html"
        driven.write_text(html, encoding="utf-8")
        # No --virtual-time-budget: the benchmark must see real wall clock.
        dom = subprocess.run(
            [exe, "--headless=old", "--disable-gpu", "--no-sandbox",
             "--force-device-scale-factor=1", "--window-size=" + size.replace("x", ","),
             "--user-data-dir=" + str(tmp / "p"), "--dump-dom", driven.as_uri()],
            capture_output=True, text=True, timeout=_chrome_timeout(300)).stdout or ""
    m = re.search(r'<div id="benchout"[^>]*>(.*?)</div>', dom, re.S)
    if not m:
        return "no benchmark output"
    return (m.group(1).replace("&lt;", "<").replace("&gt;", ">")
            .replace("&amp;", "&").replace("&quot;", '"').strip())


def main() -> int:
    argv = sys.argv[1:]

    def opt(name, default):
        if name in argv:
            i = argv.index(name)
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
        return default

    page = pathlib.Path(opt("--page", str(ROOT / "web" / "afe4490_studio.html")))
    size = opt("--size", "1920x1080")
    hz = int(opt("--hz", "120"))
    label = opt("--label", page.name)
    frames = int(argv[0]) if argv else 200

    if not page.exists():
        print("no such page: " + str(page))
        return 2
    print(run(page, frames, hz, size, label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
