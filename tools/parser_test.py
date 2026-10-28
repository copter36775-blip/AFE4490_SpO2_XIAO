"""End-to-end test of the console's link parser.

The screenshot test drives ingest() directly, which skips the part most
likely to be wrong: feed() -> handleFrame() -> ingest(). This builds byte
sequences that are identical to what src/main.cpp emits, pushes them
through feed() in awkwardly split chunks, and checks what comes out.

Covers: a clean batch, ASCII interleaved between frames, a chunk boundary
inside every field of the header, a corrupted CRC, CSV text mode, and - on
pages that speak it - the compact 24-bit frame the firmware sends over BLE.

Run:  python tools/parser_test.py [--page web/afe4490_studio_V3.html]
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
PAGE = ROOT / "web" / "afe4490_studio.html"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

DRIVER = r"""
<script>
(function () {
  var fails = [];
  function check(name, cond, extra) {
    if (!cond) fails.push(name + (extra ? ' (' + extra + ')' : ''));
  }
  window.addEventListener('error', function (e) {
    fails.push('JS ERROR: ' + e.message + ' @' + e.lineno);
  });

  // ---- build a frame exactly the way src/main.cpp does ----------------
  function crcRef(bytes) {
    var crc = 0xFFFF;
    for (var i = 0; i < bytes.length; i++) {
      crc ^= bytes[i] << 8;
      for (var k = 0; k < 8; k++)
        crc = (crc & 0x8000) ? ((crc << 1) ^ 0x1021) & 0xFFFF : (crc << 1) & 0xFFFF;
    }
    return crc;
  }

  function buildFrame(seq, samples, rate, breakCrc) {
    var n = 8 + samples.length * 24;
    var body = new Uint8Array(n);
    var dv = new DataView(body.buffer);
    dv.setUint32(0, seq, true);
    body[4] = samples.length;
    body[5] = 0;
    dv.setUint16(6, rate, true);
    var o = 8;
    for (var s = 0; s < samples.length; s++) {
      for (var j = 0; j < 6; j++) { dv.setInt32(o, samples[s][j], true); o += 4; }
    }
    var head = new Uint8Array([0xAA, 0x55, 0x01, n & 0xFF, (n >> 8) & 0xFF]);
    var crcIn = new Uint8Array(3 + n);
    crcIn.set(head.subarray(2), 0);
    crcIn.set(body, 3);
    var crc = crcRef(crcIn);
    if (breakCrc) crc ^= 0x0001;
    var out = new Uint8Array(5 + n + 2);
    out.set(head, 0);
    out.set(body, 5);
    out[5 + n] = crc & 0xFF;
    out[6 + n] = (crc >> 8) & 0xFF;
    return out;
  }

  /* Type 0x02: four channels at three bytes each, little-endian, and the
     ambient-subtracted pair left for the console to subtract. Built here
     the same way src/main.cpp builds it, byte for byte. */
  function buildCompact(seq, samples, rate, breakCrc) {
    var n = 8 + samples.length * 12;
    var body = new Uint8Array(n);
    var dv = new DataView(body.buffer);
    dv.setUint32(0, seq, true);
    body[4] = samples.length;
    body[5] = 0;
    dv.setUint16(6, rate, true);
    var o = 8;
    for (var s = 0; s < samples.length; s++) {
      for (var j = 0; j < 4; j++) {
        var v = samples[s][j] & 0xFFFFFF;
        body[o++] = v & 0xFF;
        body[o++] = (v >> 8) & 0xFF;
        body[o++] = (v >> 16) & 0xFF;
      }
    }
    var head = new Uint8Array([0xAA, 0x55, 0x02, n & 0xFF, (n >> 8) & 0xFF]);
    var crcIn = new Uint8Array(3 + n);
    crcIn.set(head.subarray(2), 0);
    crcIn.set(body, 3);
    var crc = crcRef(crcIn);
    if (breakCrc) crc ^= 0x0001;
    var out = new Uint8Array(5 + n + 2);
    out.set(head, 0);
    out.set(body, 5);
    out[5 + n] = crc & 0xFF;
    out[6 + n] = (crc >> 8) & 0xFF;
    return out;
  }

  function ascii(s) {
    var out = new Uint8Array(s.length + 1);
    for (var i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    out[s.length] = 0x0A;
    return out;
  }

  function concat(arrs) {
    var total = 0, i;
    for (i = 0; i < arrs.length; i++) total += arrs[i].length;
    var out = new Uint8Array(total), o = 0;
    for (i = 0; i < arrs.length; i++) { out.set(arrs[i], o); o += arrs[i].length; }
    return out;
  }

  function reset() {
    resetBuffers();
    resetRx();
    link.frames = 0;
    link.crcErr = 0;
    link.samples = 0;
  }

  // Feed a byte stream in fixed-size slices to force chunk boundaries in
  // every position of the header and payload.
  function feedChunked(bytes, size) {
    for (var i = 0; i < bytes.length; i += size)
      feed(bytes.subarray(i, Math.min(bytes.length, i + size)));
  }

  window.addEventListener('load', function () { setTimeout(run, 150); });

  function run() {
    try {
      var MK = function (base) {
        return [base, base + 1, base + 2, base + 3, base + 4, base + 5];
      };

      // ---- 1. one clean 6-sample frame, delivered whole ---------------
      reset();
      var s1 = [MK(1000), MK(2000), MK(-3000), MK(4000), MK(-5000), MK(6000)];
      feed(buildFrame(0, s1, 500));
      check('1: frame counted', link.frames === 1, 'frames=' + link.frames);
      check('1: crc clean', link.crcErr === 0);
      check('1: sample count', buf.count === 6, 'count=' + buf.count);
      check('1: rate adopted', stats.rate === 500, 'rate=' + stats.rate);
      // at(x,0) is the newest sample, which is the last one in the batch.
      check('1: newest RED', at(buf.red, 0) === 6000, at(buf.red, 0));
      check('1: newest IR', at(buf.ir, 0) === 6002, at(buf.ir, 0));
      check('1: newest RED-amb', at(buf.redAmb, 0) === 6004, at(buf.redAmb, 0));
      check('1: newest IR-amb', at(buf.irAmb, 0) === 6005, at(buf.irAmb, 0));
      check('1: oldest RED', at(buf.red, 5) === 1000, at(buf.red, 5));

      // ---- 2. negative values survive sign extension -------------------
      check('2: negative RED', at(buf.red, 1) === -5000, at(buf.red, 1));
      check('2: negative IR', at(buf.ir, 3) === -2998, at(buf.ir, 3));

      // ---- 3. every chunk split from 1 to 40 bytes ---------------------
      for (var size = 1; size <= 40; size++) {
        reset();
        feedChunked(buildFrame(0, s1, 500), size);
        if (buf.count !== 6 || link.frames !== 1) {
          check('3: split at ' + size, false,
                'count=' + buf.count + ' frames=' + link.frames);
          break;
        }
      }
      check('3: all splits ok', true);

      // ---- 4. ASCII lines interleaved with frames ----------------------
      reset();
      var mixed = concat([
        ascii('ST:fw=V1.0.0,prf=500'),
        buildFrame(0, s1, 500),
        ascii('EVT:READY'),
        buildFrame(6, s1, 500),
        ascii('ACK:START')
      ]);
      feedChunked(mixed, 7);
      check('4: both frames parsed', link.frames === 2, 'frames=' + link.frames);
      check('4: 12 samples', buf.count === 12, 'count=' + buf.count);
      check('4: no crc errors', link.crcErr === 0);

      // ---- 5. a corrupted frame is rejected, and the next one is not ---
      reset();
      feed(buildFrame(0, s1, 500, true));
      check('5: bad crc counted', link.crcErr === 1, 'crcErr=' + link.crcErr);
      check('5: bad frame dropped', buf.count === 0, 'count=' + buf.count);
      feed(buildFrame(6, s1, 500));
      check('5: recovers after bad crc', buf.count === 6, 'count=' + buf.count);

      // ---- 6. a partial frame must not be consumed early ---------------
      reset();
      var f = buildFrame(0, s1, 500);
      feed(f.subarray(0, f.length - 3));
      check('6: partial held', buf.count === 0 && link.frames === 0,
            'count=' + buf.count);
      feed(f.subarray(f.length - 3));
      check('6: completes', buf.count === 6 && link.frames === 1,
            'count=' + buf.count);

      // ---- 6b. stray 0xAA must not wedge the scanner --------------------
      // A tab that hangs here is worse than a dropped sample, so this
      // check has a watchdog: if feed() spins, the timer never fires and
      // the title stays at its default, which the runner treats as FAIL.
      reset();
      var junk = new Uint8Array([0xAA, 0x12, 0x34, 0xAA, 0xAA, 0x00]);
      feed(junk);
      feed(ascii('ACK:START'));
      feed(buildFrame(0, s1, 500));
      check('6b: survives stray 0xAA', buf.count === 6 && link.frames === 1,
            'count=' + buf.count + ' frames=' + link.frames);

      // ---- 7. CSV text mode --------------------------------------------
      reset();
      feed(ascii('D,0,111,222,333,444,555,666'));
      check('7: csv sample', buf.count === 1, 'count=' + buf.count);
      check('7: csv RED', at(buf.red, 0) === 111, at(buf.red, 0));
      check('7: csv IR-amb', at(buf.irAmb, 0) === 666, at(buf.irAmb, 0));

      // ---- 8. a 1-sample frame (the tail of a stream) -------------------
      reset();
      feed(buildFrame(0, [MK(77)], 250));
      check('8: single sample', buf.count === 1, 'count=' + buf.count);
      check('8: rate switched', stats.rate === 250, 'rate=' + stats.rate);

      // ---- 9. the compact BLE frame ------------------------------------
      // Only V3 carries a BLE transport, so V1 and V2 skip this block
      // rather than failing for lacking a feature they never had.
      if (typeof i24 === 'function') {
        var C = function (l2, a2, l1, a1) { return [l2, a2, l1, a1]; };

        reset();
        feed(buildCompact(0, [C(1181100, 459700, 1181100, 813700)], 125));
        check('9: compact frame counted', link.frames === 1, 'frames=' + link.frames);
        check('9: compact crc clean', link.crcErr === 0);
        check('9: compact sample', buf.count === 1, 'count=' + buf.count);
        check('9: compact rate', stats.rate === 125, 'rate=' + stats.rate);
        check('9: compact RED raw', at(buf.red, 0) === 1181100, at(buf.red, 0));
        check('9: compact IR raw', at(buf.ir, 0) === 1181100, at(buf.ir, 0));
        // The whole point of the format: these two arrive as a subtraction.
        check('9: RED-amb derived', at(buf.redAmb, 0) === 721400, at(buf.redAmb, 0));
        check('9: IR-amb derived', at(buf.irAmb, 0) === 367400, at(buf.irAmb, 0));

        // ---- 10. 22-bit negatives survive the 24-bit round trip ---------
        reset();
        feed(buildCompact(0, [C(-2097152, -1, 2097151, -1000000)], 125));
        check('10: most negative', at(buf.red, 0) === -2097152, at(buf.red, 0));
        check('10: minus one', at(buf.amb2, 0) === -1, at(buf.amb2, 0));
        check('10: most positive', at(buf.ir, 0) === 2097151, at(buf.ir, 0));
        check('10: negative derived', at(buf.redAmb, 0) === -2097151, at(buf.redAmb, 0));

        // ---- 11. a full 16-sample BLE batch, split at every boundary -----
        var big = [];
        for (var q = 0; q < 16; q++) big.push(C(q * 100, q, -q * 100, -q));
        var ok = true;
        for (var sz = 1; sz <= 40 && ok; sz++) {
          reset();
          feedChunked(buildCompact(0, big, 125), sz);
          if (buf.count !== 16 || link.frames !== 1) {
            check('11: compact split at ' + sz, false,
                  'count=' + buf.count + ' frames=' + link.frames);
            ok = false;
          }
        }
        check('11: all compact splits ok', ok);
        check('11: packet is 207 B', buildCompact(0, big, 125).length === 207,
              buildCompact(0, big, 125).length);

        // ---- 12. mixed frame types and text in one stream ---------------
        reset();
        feedChunked(concat([
          ascii('ST:fw=V2.0.0,dec=4'),
          buildCompact(0, big, 125),
          ascii('BLESTAT:mtu=244,dle=27,ci=24'),
          buildFrame(16, [MK(9000)], 125)
        ]), 5);
        check('12: both types parsed', link.frames === 2, 'frames=' + link.frames);
        check('12: 17 samples', buf.count === 17, 'count=' + buf.count);
        check('12: no crc errors', link.crcErr === 0);

        // ---- 13. a corrupt compact frame is rejected --------------------
        reset();
        feed(buildCompact(0, big, 125, true));
        check('13: compact bad crc counted', link.crcErr === 1, 'crcErr=' + link.crcErr);
        check('13: compact bad frame dropped', buf.count === 0, 'count=' + buf.count);
      }

      // ---- 14. a fractional sample rate must not wipe the sweep --------
      // 500 Hz at DEC:8 is 62.5 Hz. The binary frame header carries whole
      // hertz and rounds it to 62, while STATUS reports the exact 62.5. The
      // two used to overwrite each other several times a second, and every
      // flip resized the sweep window - which reallocates and throws away
      // every sample on screen. On a real board that looked like the trace
      // restarting two or three times a second.
      //
      // The final window size is NOT the thing to assert on: it ends up
      // back where it started either way, which is how the first version of
      // this test passed against the bug it was written for. Count the
      // resizes, because the resize is the damage.
      reset();
      handleLine('ST:fw=V2.0.0,prf=500,dec=8,drop=0,txdrop=0');
      check('14: fractional rate kept exactly',
            Math.abs(stats.rate - 62.5) < 1e-9, stats.rate);
      applySweep();

      var resizes = 0, lastVis = charts[0].visible;
      function watchSweep() {
        applySweep();                       // the render loop, mid-flap
        if (charts[0].visible !== lastVis) { resizes++; lastVis = charts[0].visible; }
      }
      for (var r = 0; r < 8; r++) {
        feed(buildFrame(r, [MK(100 + r)], 62));                  // header: 62
        watchSweep();
        handleLine('ST:fw=V2.0.0,prf=500,dec=8,drop=0,txdrop=0'); // status: 62.5
        watchSweep();
      }
      check('14: sweep never resized by rounding', resizes === 0,
            resizes + ' resizes over 8 frames');
      check('14: rate does not flap', Math.abs(stats.rate - 62.5) < 1e-9,
            stats.rate);

      // A real rate change still has to take effect, or the guard is just a
      // way of ignoring the device.
      handleLine('ST:fw=V2.0.0,prf=500,dec=1,drop=0,txdrop=0');
      applySweep();
      check('14: a real rate change still applies',
            stats.rate === 500 && charts[0].visible !== lastVis,
            stats.rate + ', ' + charts[0].visible);

      // ---- 15. the battery panel ---------------------------------------
      // Only V3 has it. The three states are not distinguishable from the
      // charger's ~CHG line alone - "full" and "no input" both release it -
      // so the firmware sends VBUS too and this checks all three land.
      if (document.getElementById('batState')) {
        handleLine('ST:fw=V2.0.0,prf=500,dec=1,drop=0,txdrop=0,vbat=4.16,bpct=99,chg=1,vbus=1');
        check('15: charging state shown',
              /charging/i.test(document.getElementById('batState').textContent),
              document.getElementById('batState').textContent);
        check('15: charging animates the bar',
              document.getElementById('batBar').className.split(' ').indexOf('chg') >= 0,
              document.getElementById('batBar').className);
        check('15: volts to three places',
              document.getElementById('batV').textContent === '4.160',
              document.getElementById('batV').textContent);

        handleLine('ST:fw=V2.0.0,prf=500,dec=1,drop=0,txdrop=0,vbat=4.19,bpct=100,chg=2,vbus=1');
        check('15: charged is not charging',
              /charged/i.test(document.getElementById('batState').textContent) &&
              !(document.getElementById('batBar').className.split(' ').indexOf('chg') >= 0),
              document.getElementById('batState').textContent);

        handleLine('ST:fw=V2.0.0,prf=500,dec=1,drop=0,txdrop=0,vbat=3.62,bpct=6,chg=0,vbus=0');
        check('15: on battery, and a low cell is flagged',
              /battery/i.test(document.getElementById('batState').textContent) &&
              /crit|low/.test(document.getElementById('batBar').className),
              document.getElementById('batState').textContent + ' / ' +
              document.getElementById('batBar').className);
        check('15: bar tracks the estimate',
              document.getElementById('batBar').firstElementChild.style.width === '6%',
              document.getElementById('batBar').firstElementChild.style.width);
        // 4.48 V is what the unfixed acquisition window reported for a cell
        // that cannot exceed 4.2. Nothing should ever render it as normal.
        check('15: impossible voltage still renders',
              (function(){
                handleLine('ST:fw=V2.0.0,prf=500,dec=1,drop=0,txdrop=0,vbat=4.48,bpct=100,chg=0,vbus=0');
                return document.getElementById('batV').textContent === '4.480';
              })(), document.getElementById('batV').textContent);
      }

      // ---- 16. the SpO2 calibration fit --------------------------------
      // The numbers are the real ones off a UNI-SIM: three saturations, the
      // R this hardware produced at each. Worked by hand and by numpy, the
      // least-squares line is 115.85 - 34.40*R with an RMS residual of
      // 0.05 %SpO2, against 0.90 for the 110 - 25R the design ships with.
      // If this drifts, every calibrated reading drifts with it.
      if (typeof calFit === 'function') {
        cal.pts = [
          { ref: 98, R: 0.520, sd: 0, n: 40 },
          { ref: 95, R: 0.604, sd: 0, n: 40 },
          { ref: 90, R: 0.752, sd: 0, n: 40 }
        ];
        var f = calFit();
        check('16: slope', Math.abs(f.B - 34.40) < 0.01, f.B);
        check('16: intercept', Math.abs(f.A - 115.85) < 0.01, f.A);
        check('16: residual', Math.abs(f.rms - 0.05) < 0.005, f.rms);
        check('16: span reported', Math.abs(f.span - 8) < 1e-9, f.span);
        check('16: n reported', f.n === 3, f.n);

        // The shipped curve on the same points, for the comparison the
        // whole feature exists to make.
        cal.A = 110; cal.B = 25;
        var ship = cal.pts.map(function (p) { return spo2FromR(p.R) - p.ref; });
        check('16: shipped curve errs about a percent',
              Math.abs(ship[0] + 1.00) < 0.01 && Math.abs(ship[2] - 1.20) < 0.01,
              ship.map(function (e) { return e.toFixed(2); }).join(' '));

        cal.A = f.A; cal.B = f.B;
        var fit = cal.pts.map(function (p) { return spo2FromR(p.R) - p.ref; });
        check('16: fitted curve errs under a tenth',
              Math.max.apply(null, fit.map(Math.abs)) < 0.1,
              fit.map(function (e) { return e.toFixed(3); }).join(' '));

        // A line through points that share an R is not a line.
        cal.pts = [{ ref: 98, R: 0.5, sd: 0, n: 9 }, { ref: 90, R: 0.5, sd: 0, n: 9 }];
        check('16: degenerate fit refused', calFit() === null, String(calFit()));
        check('16: one point is not a fit',
              (function () { cal.pts = [{ ref: 98, R: 0.5, sd: 0, n: 9 }];
                             return calFit() === null; })(), 'ok');

        // Saturation is clamped: a curve extrapolated far enough will ask
        // for 104 %, and no such reading exists.
        cal.A = 115.85; cal.B = 34.40;
        check('16: clamped at 100', spo2FromR(0.1) === 100, spo2FromR(0.1));
        check('16: clamped at 50', spo2FromR(3.0) === 50, spo2FromR(3.0));

        // Round-trip through storage, because a calibration that does not
        // survive a reload is not a calibration.
        cal.pts = [{ ref: 97, R: 0.55, sd: 0.001, n: 40 }];
        saveCal();
        cal.A = 110; cal.B = 25; cal.pts = [];
        loadCal();
        check('16: curve survives a reload',
              Math.abs(cal.A - 115.85) < 1e-9 && Math.abs(cal.B - 34.40) < 1e-9,
              cal.A + ' / ' + cal.B);
        check('16: points survive with it',
              cal.pts.length === 1 && Math.abs(cal.pts[0].R - 0.55) < 1e-9,
              JSON.stringify(cal.pts));

        cal.A = 110; cal.B = 25; cal.pts = []; saveCal();
        check('16: default curve recognised', calIsDefault(), 'ok');
      }

      // ---- 17. SpO2 and pulse rate at high sample rates -----------------
      // The analysis windows are given in seconds and the ring is sized in
      // samples, so the two used to disagree the moment the rate left
      // 500 Hz: at 2500 Hz a 4096-sample ring held 1.6 s, pulse rate wanted
      // 2.5 and SpO2 wanted 4, and both silently returned null while the
      // trace on screen looked perfect. Drive a real pulse in at each rate
      // and insist both come out.
      //
      // 72 bpm, 2 % modulation on IR and 1.2 % on RED, which is an R near
      // 0.6 and a saturation in the mid nineties - a plausible finger, not
      // a square wave that any peak finder would catch.
      function feedPulse(fs, seconds, bpm) {
        stats.rate = fs;
        retune(fs);
        resetBuffers();
        dcInit = false;
        var n = Math.round(fs * seconds), w = 2 * Math.PI * (bpm / 60) / fs;
        for (var i = 0; i < n; i++) {
          // Sharpened so it looks like a pleth rather than a sinusoid.
          var p = Math.sin(w * i);
          p = (p + 0.45 * Math.sin(2 * w * i + 0.6)) / 1.45;
          var ir  = 800000 * (1 + 0.0200 * p);
          var red = 300000 * (1 + 0.0120 * p);
          ingest(red + 30000, 30000, ir + 30000, 30000, red, ir);
        }
      }

      // Twelve seconds fed, six analysed. The band-pass starts from rest and
      // its settling transient is a step the size of the DC level; feeding
      // exactly one window long put that transient inside the window and
      // dropped the coherence to 22 % at every rate - which looked like a
      // rate problem and was not one.
      // V1 and V2 share the ring and the estimators but not the reason
      // strings, so the rate checks run everywhere and the wording checks
      // only where there is wording to check.
      var hasWhy = (typeof spo2Why !== 'undefined');
      var whyS = function () { return hasWhy ? ': ' + spo2Why : ''; };
      var whyH = function () { return hasWhy ? ': ' + hrWhy : ''; };
      var rateRows = [];
      [125, 500, 1000, 2500, 4000, 4901].forEach(function (fs) {
        feedPulse(fs, 12, 72);
        var ox = estimateSpo2(), hr = estimateHr();
        rateRows.push(fs + ':' + (ox ? ox.spo2.toFixed(1) : 'null')
                         + '/' + (hr ? hr.bpm.toFixed(0) : 'null'));
        check('17: SpO2 at ' + fs + ' Hz', ox !== null,
              ox ? 'ok' : ('declined' + whyS()));
        check('17: pulse rate at ' + fs + ' Hz', hr !== null,
              hr ? 'ok' : ('declined' + whyH()));
        if (ox) {
          // R is (AC/DC)red over (AC/DC)ir = 0.012/0.020 = 0.60, and it must
          // not drift with the sample rate - that would be a filter designed
          // against the wrong fs.
          check('17: R at ' + fs + ' Hz', Math.abs(ox.R - 0.60) < 0.06,
                ox.R.toFixed(3));
        }
        if (hr) {
          check('17: bpm at ' + fs + ' Hz', Math.abs(hr.bpm - 72) < 4,
                hr.bpm.toFixed(1));
        }
      });
      check('17: every rate produced a number', true, rateRows.join('  '));

      // A declined estimate has to say what it is waiting for; a bare dash
      // cannot tell "still filling" from "no finger on the probe".
      if (hasWhy) {
        stats.rate = 500; retune(500); resetBuffers(); dcInit = false;
        estimateSpo2();
        check('17: says why it declined', /filling the window/.test(spo2Why),
              spo2Why);
        feedPulse(500, 12, 72);
        estimateSpo2();
        check('17: and clears the reason once it works', spo2Why === '',
              JSON.stringify(spo2Why));
      }

      // ---- 18. a rate change must not wipe the picture ------------------
      // setSweep() used to reallocate the column arrays whenever the sample
      // rate moved, and allocate() sizes those by pixel width - so the
      // reallocation threw away every column already drawn for no reason at
      // all. `visible` only sets how far the cursor moves per sample. The
      // fix is that a rate change adjusts `step` and nothing else, and this
      // asserts on the columns rather than on the window size, because the
      // window size is allowed to change.
      var ch = charts[0];
      if (ch && typeof applyRateChange === 'function') {
        stats.rate = 500;
        retune(500);
        resetBuffers();
        ch.w = ch.w || 600;
        if (!ch.valid) ch.allocate();
        applySweep();

        // Lay down some ink, then remember exactly what is on screen.
        for (var q = 0; q < 400; q++) {
          var v = [1000 + 80 * Math.sin(q / 9), 2000 + 80 * Math.cos(q / 9)];
          ch.push(v);
        }
        var inkBefore = 0;
        for (var k = 0; k < ch.valid.length; k++) inkBefore += ch.valid[k] ? 1 : 0;
        var colBefore = ch.col, arrBefore = ch.valid;
        var stepBefore = ch.step, visBefore = ch.visible;
        check('18: the fixture actually drew something', inkBefore > 50, inkBefore);

        // Five times the rate - the step this complaint was made about.
        applyRateChange(2500);
        applySweep();

        var inkAfter = 0;
        for (var k2 = 0; k2 < ch.valid.length; k2++) inkAfter += ch.valid[k2] ? 1 : 0;
        check('18: the columns survive the change', inkAfter === inkBefore,
              inkBefore + ' -> ' + inkAfter);
        check('18: no reallocation', ch.valid === arrBefore, 'same array');
        check('18: the cursor keeps its place', ch.col === colBefore,
              colBefore + ' -> ' + ch.col);
        check('18: but the sweep speed did change',
              ch.visible !== visBefore && ch.step !== stepBefore,
              visBefore + '/' + stepBefore.toFixed(4) + ' -> '
                + ch.visible + '/' + ch.step.toFixed(4));

        // The window is a duration, so five times the rate is five times the
        // samples across the same five seconds.
        check('18: window still spans the same seconds',
              Math.abs(ch.visible / 2500 - visBefore / 500) < 0.05,
              (ch.visible / 2500).toFixed(2) + ' s vs ' + (visBefore / 500).toFixed(2) + ' s');

        // And the high-pass must not have to charge from nothing again: fed
        // a settled DC, a freshly primed section outputs ~0, not a step.
        primeFilters(500000, 800000);
        var out = step(coefHP, stHPr, 500000);
        check('18: filters start settled, not charging', Math.abs(out) < 1,
              out.toFixed(6));
      }

      document.title = fails.length
        ? 'FAIL: ' + fails.join(' | ')
        : 'PASS all parser checks';
    } catch (err) {
      document.title = 'FAIL: threw ' + err.message;
    }
  }
})();
</script>
"""


def main():
    page_path = PAGE
    if "--page" in sys.argv:
        page_path = (ROOT / sys.argv[sys.argv.index("--page") + 1]).resolve()
    html = page_path.read_text(encoding="utf-8").replace("</body>", DRIVER + "\n</body>")
    exe = CHROME if pathlib.Path(CHROME).exists() else EDGE
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        page = tmp / "driven.html"
        page.write_text(html, encoding="utf-8")
        dom = subprocess.run(
            [exe, "--headless=old", "--disable-gpu", "--no-sandbox",
             "--user-data-dir=" + str(tmp / "p"),
             "--virtual-time-budget=6000", "--dump-dom", page.as_uri()],
            capture_output=True, text=True, timeout=_chrome_timeout(120)).stdout or ""

    m = re.search(r"<title>(.*?)</title>", dom, re.S)
    title = m.group(1).strip() if m else "(no title)"
    print(page_path.name + ": " + title)
    return 0 if title.startswith("PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
