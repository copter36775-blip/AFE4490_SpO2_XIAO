"""Check that the V2 Math view is showing live arithmetic, not decoration.

The view is six panels, each pairing a formula with the graph it is talking
about, all fed from the buffers the monitor draws from. That gives it three
ways to fail quietly:

  * a panel that draws nothing, which looks fine next to five that work
  * a value slot that never resolves and sits at an em dash forever
  * values that render but are frozen — the formula is decoration, not a
    live readout

So this feeds a synthetic pleth, reads pixels and text back, then changes the
signal and checks the numbers followed it. It also cross-checks the two
figures that exist in both views: the SpO₂ on the board against the vitals
card, and the autocorrelation it draws against the rate the monitor reports.

Run:  python tools/math_selftest.py
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
PAGE = ROOT / "web" / "afe4490_studio_V2.html"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

DRIVER = r"""
<div id="mathtest" style="display:none"></div>
<script>
(function () {
  var R = { errors: [], checks: [] };
  function emit(){ document.getElementById('mathtest').textContent = JSON.stringify(R); }
  function check(name, ok, detail){
    R.checks.push({ name:name, ok:!!ok, detail:String(detail === undefined ? '' : detail) });
  }
  window.addEventListener('error', function (e){
    R.errors.push((e.message||'error') + ' @' + (e.lineno||'?')); emit();
  });
  window.addEventListener('unhandledrejection', function (e){
    R.errors.push('rejection: ' + (e.reason && e.reason.message || e.reason)); emit();
  });

  /* How much is painted, and roughly where and in what hue. Two panels that
     drew the same picture would produce the same signature. */
  function sig(cv){
    var g = cv.getContext('2d'), d;
    try { d = g.getImageData(0, 0, cv.width, cv.height).data; }
    catch (e) { return { lit:-1, err:e.message }; }
    var lit = 0, sx = 0, sy = 0, hue = 0, W = cv.width;
    for (var i = 0; i < d.length; i += 4) {
      if (d[i+3] < 8) continue;
      var r = d[i], gg = d[i+1], b = d[i+2];
      if (r < 26 && gg < 26 && b < 32) continue;
      lit++;
      sx += (i/4) % W; sy += Math.floor((i/4)/W); hue += r*3 + gg*5 + b*7;
    }
    return { lit:lit, cx: lit ? Math.round(sx/lit) : 0, cy: lit ? Math.round(sy/lit) : 0,
             hue: lit ? Math.round(hue/lit) : 0 };
  }

  var frames = 0, clock = 0;
  function pump(n, dt){
    var raf = window.requestAnimationFrame;
    window.requestAnimationFrame = function(){ return 0; };
    try { for (var i=0;i<n;i++){ clock += (dt||16.7); frames++; render(clock); } }
    finally { window.requestAnimationFrame = raf; }
  }

  var fs = 500, n = 0, FSC = 2097152;
  var dcIrS = 0.48*FSC, dcRedS = 0.40*FSC, piIr = 0.014, R0 = 0.62, bpm = 72;
  function feed(count){
    for (var i=0;i<count;i++, n++){
      var t = n/fs, ph = (t*bpm/60)%1;
      var p = Math.exp(-Math.pow((ph-0.16)/0.085,2))
            + 0.34*Math.exp(-Math.pow((ph-0.40)/0.10,2))
            + 0.11*Math.exp(-Math.pow((ph-0.62)/0.16,2)) - 0.42;
      var resp = 0.10*Math.sin(2*Math.PI*0.24*t);
      var amb = 2600 + 900*Math.sin(2*Math.PI*0.17*t) + (Math.random()-0.5)*220;
      var ir  = dcIrS *(1 - piIr   *(p+resp)) + (Math.random()-0.5)*600;
      var red = dcRedS*(1 - piIr*R0*(p+resp)) + (Math.random()-0.5)*600;
      ingest(Math.round(red+amb), Math.round(amb), Math.round(ir+amb),
             Math.round(amb), Math.round(red), Math.round(ir));
    }
    link.samples = n;
  }
  function stream(secs, dt){
    dt = dt || 16.7;
    var nf = Math.round(secs*1000/dt), per = fs*dt/1000, owed = 0;
    for (var i=0;i<nf;i++){
      owed += per; var take = Math.floor(owed); owed -= take;
      feed(take); pump(1, dt);
    }
  }
  function slotText(){
    var out = [];
    document.querySelectorAll('#mGrid .val').forEach(function(el){ out.push(el.textContent.trim()); });
    return out;
  }

  window.addEventListener('load', function(){ setTimeout(run, 120); });

  function run(){
    try { body(); }
    catch (e){ R.errors.push('driver: ' + e.message + ' | ' + (e.stack||'').split('\n')[1]); }
    R.done = true; document.title = 'MATHTEST DONE'; emit();
  }

  function body(){
    var timing = { 0x01:6050,0x02:7998,0x03:6000,0x04:7999,0x05:50,0x06:1998,
      0x07:2050,0x08:3998,0x09:2000,0x0A:3999,0x0B:4050,0x0C:5998,0x0D:4,
      0x0E:1999,0x0F:2004,0x10:3999,0x11:4004,0x12:5999,0x13:6004,0x14:7999,
      0x15:0,0x16:3,0x17:2000,0x18:2003,0x19:4000,0x1A:4003,0x1B:6000,
      0x1C:6003,0x1D:7999,0x1E:0x000108,0x20:0,0x21:0x000002,
      0x22:0x012828,0x23:0x020000 };
    Object.keys(timing).forEach(function(k){ regValues[k|0] = timing[k]; });
    renderRegTable(); syncControls();
    document.getElementById('overlay').classList.add('hide');
    link.connected = true; streaming = true; stats.rate = fs;
    clock = performance.now();
    pump(2);

    stream(9.0);
    check('metrics resolved before switching', !!(metrics.ox && metrics.hr),
          metrics.ox ? ('R=' + metrics.ox.R.toFixed(3) + ' bpm=' +
                        (metrics.hr ? metrics.hr.bpm.toFixed(1) : '-')) : 'null');

    setView('math');
    stream(2.0);
    check('math view is showing', viewMode === 'math' &&
          document.body.classList.contains('mathv'), document.body.className);
    check('all panels built',
          document.querySelectorAll('#mGrid .mcard').length === MCARDS.length,
          document.querySelectorAll('#mGrid .mcard').length + ' of ' + MCARDS.length);

    // ---- every panel draws, and draws something of its own ---------
    var sigs = [];
    for (var i = 0; i < MCARDS.length; i++){
      var c = MCARDS[i], cv = document.getElementById('mcv-' + c.id);
      check(c.id + ': canvas has real size',
            cv && cv.clientWidth > 200 && cv.clientHeight > 60,
            cv ? cv.clientWidth + 'x' + cv.clientHeight : 'missing');
      var s2 = sig(cv);
      check(c.id + ': draws something', s2.lit > 500, s2.lit + ' lit px');
      var ro = document.getElementById('mro-' + c.id);
      check(c.id + ': header readout filled',
            ro && ro.textContent.trim().length > 0 && ro.textContent.trim() !== '—',
            ro ? ro.textContent.trim() : 'missing');
      sigs.push({ id:c.id, s:s2 });
    }
    var dupes = [];
    for (var a = 0; a < sigs.length; a++)
      for (var b = a+1; b < sigs.length; b++){
        var A = sigs[a].s, B = sigs[b].s;
        if (Math.abs(A.lit-B.lit) < 40 && Math.abs(A.cx-B.cx) < 6 &&
            Math.abs(A.cy-B.cy) < 6 && Math.abs(A.hue-B.hue) < 8)
          dupes.push(sigs[a].id + '≈' + sigs[b].id);
      }
    check('panels draw distinct pictures', dupes.length === 0,
          dupes.length ? dupes.join(' ') : sigs.map(function(x){ return x.id+':'+x.s.lit; }).join(' '));

    // ---- every live value resolved ---------------------------------
    var slots = slotText();
    var dashes = slots.filter(function(t){ return t === '\u2014'; });
    check('every value slot resolved', dashes.length === 0,
          (slots.length - dashes.length) + '/' + slots.length +
          ' e.g. ' + slots.slice(0, 4).join(' · '));

    // ---- the numbers are the monitor's numbers ---------------------
    var board = document.getElementById('mGrid').textContent;
    var shown = /(\d+\.\d)\s*%/.exec(board);
    check('SpO2 on the board matches the vitals card',
          shown && metrics.ox && Math.abs(parseFloat(shown[1]) - metrics.ox.spo2) < 1.5,
          shown ? shown[1] + ' vs ' + metrics.ox.spo2.toFixed(1) : 'not found');
    var D = hrDetail(performance.now() + 99999);
    check('autocorrelation produced a curve', !!D && D.ac.length > 10,
          D ? (D.lagMin + '…' + D.lagMax + ' peak ' + D.bestLag) : 'null');
    check('autocorrelation agrees with the monitor',
          !!D && metrics.hr && Math.abs(D.bpm - metrics.hr.bpm) < 1.0,
          D && metrics.hr ? D.bpm.toFixed(2) + ' vs ' + metrics.hr.bpm.toFixed(2) : 'n/a');
    check('parabolic term is a sub-sample correction', !!D && Math.abs(D.delta) <= 1.0,
          D ? D.delta.toFixed(4) : 'n/a');

    // ---- and they are LIVE ------------------------------------------
    // Change the signal underneath and the formulas have to follow. This is
    // the check that separates a live readout from a rendered screenshot.
    var before = slotText();
    var beforeSpo2 = metrics.ox ? metrics.ox.spo2 : NaN;
    var beforeBpm  = metrics.hr ? metrics.hr.bpm  : NaN;
    bpm = 104;                       // faster pulse
    R0 = 1.05;                       // lower saturation
    dcIrS = 0.30*FSC;                // dimmer, so DC and headroom move too
    stream(14.0);
    var after = slotText();
    var moved = 0;
    for (var k = 0; k < Math.min(before.length, after.length); k++)
      if (before[k] !== after[k]) moved++;
    check('values follow the signal', moved > before.length*0.5,
          moved + ' of ' + before.length + ' slots changed');
    check('pulse rate followed the new signal',
          metrics.hr && Math.abs(metrics.hr.bpm - 104) < 8,
          metrics.hr ? beforeBpm.toFixed(1) + ' → ' + metrics.hr.bpm.toFixed(1) : 'null');
    check('SpO2 followed the new ratio',
          metrics.ox && metrics.ox.spo2 < beforeSpo2 - 3,
          metrics.ox ? beforeSpo2.toFixed(1) + ' → ' + metrics.ox.spo2.toFixed(1) : 'null');
    var D2 = hrDetail(performance.now() + 199999);
    check('autocorrelation peak moved with the rate',
          !!D2 && D2.bestLag < D.bestLag,
          D2 ? D.bestLag + ' → ' + D2.bestLag + ' samples' : 'null');
    check('graphs redrew after the change',
          sig(document.getElementById('mcv-corr')).lit > 500,
          sig(document.getElementById('mcv-corr')).lit + ' lit px');

    // ---- leaving and coming back ------------------------------------
    setView('monitor'); stream(1.5);
    setView('math'); stream(1.5);
    check('panels repaint after leaving the view',
          sig(document.getElementById('mcv-acdc')).lit > 500,
          sig(document.getElementById('mcv-acdc')).lit + ' lit px');

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
        page = tmp / "mathtest.html"
        page.write_text(html, encoding="utf-8")
        dom = subprocess.run(
            [exe, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--window-size=1700,1000", "--force-device-scale-factor=1",
             "--user-data-dir=" + str(tmp / "p"),
             "--virtual-time-budget=40000", "--dump-dom", page.as_uri()],
            capture_output=True, text=True, timeout=_chrome_timeout(420)).stdout or ""

    m = re.search(r'<div id="mathtest"[^>]*>(.*?)</div>', dom, re.S)
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
        print("  %s  %-46s %s" % ("ok  " if c["ok"] else "FAIL", c["name"], c["detail"][:88]))
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
