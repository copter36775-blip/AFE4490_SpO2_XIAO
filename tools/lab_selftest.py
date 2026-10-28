"""Drive the V4 Register Lab in headless Chrome against a simulated board.

The page's transport is replaced by a small firmware model that answers the
real protocol - WM:, W:, REG?, REGS?, REGS?HW, R:, STATUS? - and streams
genuine type-0x01 frames with CRCs and sequence numbers, so everything above
linkSend() runs exactly as it does on the bench. The model's LED drive
changes the simulated DC level, which gives the before/after table a real
effect to measure.

What it proves:
  * the generator reproduces datasheet Table 2 to the count at T = 8000
  * the firmware's own SpO2 defaults pass the rule check, and injected
    faults (PRPCOUNT, XTALDIS, reserved bits, a sample outside its LED) do not
  * a live edit is one WM: line, read back, logged, undoable, and its marker
    lands on exactly the sample whose sequence number the ACK named
  * staged edits go over as one transaction; a timing change as one line of
    30 registers, and the display rate follows it
  * capture by count, by duration and around a register write, with the
    marker at the pre-trigger boundary and the register map in the CSV
  * verify catches a register silicon disagrees with; old firmware without
    WM: falls back to W:; rail panels write through the lab
  * dragging an edge on the timing canvas changes the right register

Run:  python tools/lab_selftest.py [--page web/afe4490_studio_V4.html]
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = ROOT / "web" / "afe4490_studio_V4.html"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def _chrome_timeout(default):
    try:
        return max(default, int(os.environ.get("AFE_TEST_TIMEOUT", "0")))
    except ValueError:
        return default


DRIVER = r"""
<div id="selftest" style="display:none"></div>
<script>
(function () {
  var R = { errors: [], checks: [], done: false };
  function emit() { document.getElementById('selftest').textContent = JSON.stringify(R); }
  function check(name, ok, detail) {
    R.checks.push({ name: name, ok: !!ok, detail: String(detail === undefined ? '' : detail) });
    emit();
  }
  window.addEventListener('error', function (e) { R.errors.push((e.message || 'error') + ' @' + (e.lineno || '?')); emit(); });
  window.addEventListener('unhandledrejection', function (e) { R.errors.push('rejection: ' + (e.reason && e.reason.stack || e.reason)); emit(); });

  function ink(cv) {
    var d;
    try { d = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height).data; } catch (e) { return -1; }
    var lit = 0;
    for (var i = 3; i < d.length; i += 4) if (d[i] > 8) lit++;
    return lit;
  }
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  /* ---- downloads are captured, not saved ------------------------------ */
  var blobs = [];
  URL.createObjectURL = function (b) { blobs.push(b); return 'blob:selftest'; };
  URL.revokeObjectURL = function () {};
  HTMLAnchorElement.prototype.click = function () {};
  window.confirm = function () { return true; };
  window.prompt = function (q, d) { return d; };

  /* ---- the simulated firmware ----------------------------------------- */
  var FSC = 2097152;
  var F = { regs: new Array(49).fill(0), seq: 0, wm: true, corrupt: null, sent: [], rate: 500 };
  function defaults() {
    var snap = labBuiltinSnaps()[0];
    labSnapPairs(snap).forEach(function (p) { F.regs[p[0]] = p[1]; });
  }
  function prf() { return 4e6 / ((F.regs[0x1D] & 0xFFFF) + 1); }

  linkSend = function (text) {
    text.split('\n').filter(Boolean).forEach(function (line) {
      F.sent.push(line);
      var out = [];
      if (line.indexOf('WM:') === 0) {
        if (!F.wm) out.push('ERR:UNKNOWN_COMMAND');
        else {
          var timing = false;
          var pairs = line.slice(3).split(',').map(function (x) { return x.split('=').map(function (h) { return parseInt(h, 16); }); });
          pairs.forEach(function (p) {
            if (p[0] === 0 && (p[1] & 8)) F.regs.fill(0); else F.regs[p[0]] = p[1];
            if (p[0] >= 1 && p[0] <= 0x1D) timing = true;
          });
          out.push('ACK:WM:n=' + pairs.length + ',seq=' + F.seq + ',flush=' + (timing ? 1 : 0)
                   + ',prf=' + Math.round(prf()) + ',' + pairs.map(function (p) { return hx(p[0], 2) + '=' + hx(F.regs[p[0]], 6); }).join(','));
        }
      } else if (line.indexOf('W:') === 0) {
        var aw = line.slice(2).split(',').map(function (h) { return parseInt(h, 16); });
        F.regs[aw[0]] = aw[1];
        out.push('ACK:W:' + hx(aw[0], 2) + '=' + hx(aw[1], 6));
      } else if (line === 'REG?') {
        for (var a = 0; a <= 0x30; a++) out.push('REG:' + hx(a, 2) + '=' + hx(F.regs[a], 6));
        out.push('REG:END');
      } else if (line === 'REGS?') {
        out.push('REGS:' + F.regs.map(function (v) { return hx(v, 6); }).join(','));
      } else if (line === 'REGS?HW') {
        out.push('REGSHW:' + F.regs.map(function (v, a) { return hx(a === F.corrupt ? (v ^ 1) : v, 6); }).join(','));
      } else if (line.indexOf('R:') === 0) {
        var ar = parseInt(line.slice(2), 16);
        out.push('REG:' + hx(ar, 2) + '=' + hx(F.regs[ar], 6));
      } else if (line === 'STATUS?') {
        out.push('ST:prf=' + Math.round(prf()) + ',dec=1,prpct=' + (F.regs[0x1D] & 0xFFFF) + ',drop=0,txdrop=0');
      } else {
        out.push('ACK:' + line.split(':')[0]);
      }
      setTimeout(function () { out.forEach(handleLine); }, 3);
    });
    return Promise.resolve();
  };

  /* One type-0x01 frame, built the way sendSampleFrame() builds it. */
  function frame(seq, samples, rate) {
    var len = 8 + samples.length * 24, b = new Uint8Array(5 + len + 2), dv = new DataView(b.buffer);
    b[0] = 0xAA; b[1] = 0x55; b[2] = 0x01; b[3] = len & 255; b[4] = len >> 8;
    dv.setUint32(5, seq, true); b[9] = samples.length; b[10] = 0; dv.setUint16(11, rate, true);
    var o = 13;
    samples.forEach(function (s) { for (var k = 0; k < 6; k++) { dv.setInt32(o, s[k], true); o += 4; } });
    var c = crc16(b, 2, 3 + len);
    b[5 + len] = c & 255; b[6 + len] = c >> 8;
    return b;
  }

  var tSim = 0, owed = 0, clock = 0, pending = [];
  function sample() {
    var t = tSim; tSim += 1 / F.rate;
    var ph = (t * 72 / 60) % 1;
    var p = Math.exp(-Math.pow((ph - 0.16) / 0.085, 2)) + 0.3 * Math.exp(-Math.pow((ph - 0.4) / 0.1, 2)) - 0.3;
    var red2 = F.regs[0x22] & 255, ir1 = (F.regs[0x22] >> 8) & 255;
    var dcRed = Math.min(0.8, 0.30 * red2 / 40) * FSC, dcIr = Math.min(0.8, 0.40 * ir1 / 40) * FSC;
    var amb = 3000 + (Math.random() - 0.5) * 200;
    var red = dcRed * (1 - 0.009 * p) + (Math.random() - 0.5) * 400;
    var ir = dcIr * (1 - 0.014 * p) + (Math.random() - 0.5) * 400;
    return [Math.round(red + amb), Math.round(amb), Math.round(ir + amb), Math.round(amb), Math.round(red), Math.round(ir)];
  }
  function streamSim(seconds, dtMs) {
    dtMs = dtMs || 8.333;
    var raf = window.requestAnimationFrame;
    window.requestAnimationFrame = function () { return 0; };
    try {
      clock = Math.max(clock, performance.now());
      var nf = Math.round(seconds * 1000 / dtMs);
      for (var i = 0; i < nf; i++) {
        owed += F.rate * dtMs / 1000;
        while (owed >= 1) { pending.push(sample()); owed -= 1; }
        while (pending.length >= 8) {
          var batch = pending.splice(0, 8);
          feed(frame(F.seq, batch, Math.round(F.rate)));
          F.seq += batch.length;
        }
        clock += dtMs;
        render(clock);
      }
    } finally { window.requestAnimationFrame = raf; }
  }

  window.addEventListener('load', function () { setTimeout(function () { run().catch(function (e) { R.errors.push('run: ' + (e && e.stack || e)); R.done = true; emit(); }); }, 150); });

  async function run() {
    /* ---- generator against Table 2 ------------------------------------ */
    var g = labGenTiming(8000, 25, 50, 4).reg;
    var t2 = { 0x01:6050, 0x02:7998, 0x03:6000, 0x04:7999, 0x05:50, 0x06:1998, 0x07:2050, 0x08:3998,
               0x09:2000, 0x0A:3999, 0x0B:4050, 0x0C:5998, 0x0D:4, 0x0E:1999, 0x0F:2004, 0x10:3999,
               0x11:4004, 0x12:5999, 0x13:6004, 0x14:7999, 0x15:0, 0x16:3, 0x17:2000, 0x18:2003,
               0x19:4000, 0x1A:4003, 0x1B:6000, 0x1C:6003, 0x1D:7999 };
    var wrong = Object.keys(t2).filter(function (a) { return g[a] !== t2[a]; }).map(function (a) { return hx(+a, 2) + '=' + g[a]; });
    check('generator reproduces datasheet Table 2 exactly', wrong.length === 0, wrong.join(' ') || '29 registers');

    /* ---- connect to the model ------------------------------------------ */
    defaults();
    link.connected = true; link.kind = 'usb';
    document.getElementById('overlay').classList.add('hide');
    handleLine('CAPS:PROTO=A1,FW=V2.1.0,PRFMAX=4901,WM=48,REGSQ=1');
    check('CAPS WM= switches on transactions', lab.fwWm === true, lab.fwWm);
    await readAllRegisters();
    await sleep(20);
    streaming = true;
    resetBuffers();
    setView('lab');
    streamSim(1.5);

    var rail = getComputedStyle(document.querySelector('.rail')).display;
    var lv = document.getElementById('labView').getBoundingClientRect();
    check('lab view takes the main area, rail hidden', rail === 'none' && lv.width > 1000 && lv.height > 500, rail + ' ' + Math.round(lv.width) + 'x' + Math.round(lv.height));
    check('map lists all 49 registers', document.querySelectorAll('#labMapBody tr.r').length === 49, document.querySelectorAll('#labMapBody tr.r').length);
    var panes = ['labMap', 'labEdit', 'labScope', 'labDock'].map(function (id) { var r = document.getElementById(id).getBoundingClientRect(); return Math.round(r.width) + 'x' + Math.round(r.height); });
    check('four lab panes have real size', panes.every(function (s) { var p = s.split('x'); return +p[0] > 150 && +p[1] > 100; }), panes.join(' '));
    check('live scope has ink', ink(lab.scope.fg) > 500, ink(lab.scope.fg));
    check('meters show a DC level', /%/.test(document.querySelector('#mRed [data-k="dc"]').textContent), document.querySelector('#mRed [data-k="dc"]').textContent);

    var errs = lab.drc.filter(function (x) { return x.lv === 'err'; });
    check('firmware SpO2 defaults pass the rule check', errs.length === 0, errs.map(function (x) { return x.title; }).join(' | ') || lab.drc.length + ' notes');

    /* ---- inspector ------------------------------------------------------ */
    labSelect(0x21); streamSim(0.1);
    check('inspector draws 24 bits and a field table', document.querySelectorAll('#labInsp .bit').length === 24 && document.querySelectorAll('#labInsp .ftab tbody tr').length === 6,
          document.querySelectorAll('#labInsp .bit').length + ' bits');
    check('reserved bits of 0x21 are hatched', document.querySelectorAll('#labInsp .bit.rsv').length === 7, document.querySelectorAll('#labInsp .bit.rsv').length);

    /* ---- rule check catches injected faults ----------------------------- */
    lab.mode = 'staged';
    labStageMany([[0x1D, 500], [0x23, F.regs[0x23] | (1 << 9)], [0x1E, F.regs[0x1E] | (1 << 20)], [0x07, 1000]]);
    streamSim(0.05);
    var titles = lab.drc.map(function (x) { return x.title; }).join(' | ');
    check('rule check: PRPCOUNT out of range', /PRPCOUNT 500 outside/.test(titles), '');
    check('rule check: XTALDIS', /Crystal oscillator disabled/.test(titles), '');
    check('rule check: reserved bits', /1E CONTROL1: reserved bits set/.test(titles), '');
    check('rule check: sample outside its LED', /Sample LED1 outside the LED pulse/.test(titles), '');
    check('staging sent nothing to the chip', !F.sent.some(function (l) { return /^WM:/.test(l); }), F.sent.filter(function (l) { return /^WM/.test(l); }).length);
    labDiscard(); streamSim(0.05);

    /* ---- live edit: one transaction, read back, exact marker ------------ */
    lab.mode = 'live';
    var before = F.regs[0x22];
    var nSent = F.sent.length;
    labSet(0x22, (before & ~0xFF) | 60, 'selftest LED2');
    var seqAtWrite = F.seq;
    await sleep(30);
    var wm = F.sent.slice(nSent).filter(function (l) { return /^WM:/.test(l); });
    check('live edit is one WM: line', wm.length === 1 && wm[0] === 'WM:22=' + hx((before & ~0xFF) | 60, 6), wm.join(' / '));
    var tx = lab.log[0];
    check('transaction read back and logged', tx && tx.ok && tx.pairs[0].back === ((before & ~0xFF) | 60) && !tx.pairs[0].mismatch, tx && JSON.stringify(tx.pairs));
    check('write carries the firmware seq', tx && tx.seq === seqAtWrite, tx && tx.seq + ' vs ' + seqAtWrite);
    var offBefore = link.seqOff;
    streamSim(0.4);
    var m = markDone[markDone.length - 1];
    check('marker landed on the named sample', m && m.w === seqAtWrite + offBefore, m ? m.w + ' vs ' + (seqAtWrite + offBefore) : 'none');
    check('scope shows the marker', lab.scope.marks.length >= 1, JSON.stringify({ marks: lab.scope.marks.length, queued: markPlay.length,
          idx: m && m.idx, disp: dispRead, written: buf.written, playing: playing, col: lab.scope.col, w: lab.scope.w }));
    check('undo step recorded', lab.undo.length >= 1, lab.undo.length);
    streamSim(1.6);
    var fx = lab.fx[0];
    check('before/after measured', fx && fx.state === 'done', fx && fx.state);
    check('before/after sees the brighter RED', fx && fx.post && fx.pre && fx.post.red.mean > fx.pre.red.mean * 1.3,
          fx && fx.pre && fx.post ? Math.round(fx.pre.red.mean) + ' -> ' + Math.round(fx.post.red.mean) : '');

    /* ---- undo ------------------------------------------------------------ */
    await labUndo(); await sleep(20);
    check('undo writes the old value back', F.regs[0x22] === before && regValues[0x22] === before, hx(F.regs[0x22], 6));
    await labRedo(); await sleep(20);
    check('redo writes it again', (F.regs[0x22] & 0xFF) === 60, hx(F.regs[0x22], 6));

    /* ---- staged batch ------------------------------------------------------ */
    lab.mode = 'staged';
    nSent = F.sent.length;
    labSet(0x21, 1, 'rf 250k');
    labSet(0x20, 1, 'tiagain');
    labSet(0x22, (F.regs[0x22] & ~0xFF00) | (50 << 8), 'LED1');
    check('three edits staged', lab.stage.size === 3, lab.stage.size);
    labDock('pend'); streamSim(0.05);
    check('pending tab offers one transaction', /Apply 3 as one transaction/.test(document.getElementById('dkPend').textContent), '');
    await labApply(); await sleep(20);
    wm = F.sent.slice(nSent).filter(function (l) { return /^WM:/.test(l); });
    check('staged apply is a single WM: line', wm.length === 1 && wm[0].split(',').length === 3 && lab.stage.size === 0, wm.join(' / '));

    /* ---- generator: 1000 Hz in one line ----------------------------------- */
    lab.mode = 'live';
    labSub('gen');
    document.getElementById('gPrp').value = 3999;
    document.getElementById('gAvg').value = 'auto';
    labGenRender();
    nSent = F.sent.length;
    document.getElementById('gApply').click();
    await sleep(40);
    wm = F.sent.slice(nSent).filter(function (l) { return /^WM:/.test(l); });
    F.rate = 1000;
    check('generator applies 30 registers in one line', wm.length === 1 && wm[0].split(',').length === 30, wm[0] ? wm[0].split(',').length + ' pairs, ' + wm[0].length + ' bytes' : 'none');
    check('display rate follows PRPCOUNT', Math.abs(stats.rate - 1000) < 0.01, stats.rate);
    streamSim(0.5);
    errs = lab.drc.filter(function (x) { return x.lv === 'err'; });
    check('generated 1000 Hz timing passes the rule check', errs.length === 0, errs.map(function (x) { return x.title; }).join(' | '));

    /* ---- timing editor ---------------------------------------------------- */
    labSub('timing'); sizeDirty = true; streamSim(0.1);
    var tcv = document.getElementById('cTEdit');
    check('timing diagram draws', ink(tcv) > 2000, ink(tcv));
    check('timing table lists 14 signals', document.querySelectorAll('#tTable tr').length === 14, document.querySelectorAll('#tTable tr').length);
    lab.mode = 'staged';
    var rc = tcv.getBoundingClientRect(), G = labTimingGeom(tcv.clientWidth, tcv.clientHeight), V = labTView();
    var endOld = F.regs[0x0A] & 0xFFFF;
    var xEdge = rc.left + G.L + (endOld + 1 - V.x0) / (V.x1 - V.x0) * G.pw;
    var yRow = rc.top + G.TOP + 1.5 * G.rh;
    var opt = function (x) { return { clientX: x, clientY: yRow, pointerId: 1, bubbles: true }; };
    tcv.dispatchEvent(new PointerEvent('pointermove', opt(xEdge)));
    tcv.dispatchEvent(new PointerEvent('pointerdown', opt(xEdge)));
    tcv.dispatchEvent(new PointerEvent('pointermove', opt(xEdge - 30)));
    tcv.dispatchEvent(new PointerEvent('pointerup', opt(xEdge - 30)));
    streamSim(0.05);
    check('dragging LED1 end edge stages LED1LEDENDC', lab.stage.has(0x0A) && (lab.stage.get(0x0A) & 0xFFFF) < endOld,
          lab.stage.has(0x0A) ? endOld + ' -> ' + (lab.stage.get(0x0A) & 0xFFFF) : 'nothing staged: ' + [...lab.stage.keys()]);
    labDiscard();
    lab.mode = 'live';

    /* ---- captures ---------------------------------------------------------- */
    capStart({ mode: 'count', n: 1000, stay: true });
    streamSim(1.3);
    check('capture by count keeps exactly N', cap.n === 1000 && viewMode === 'lab', cap.n + ' in ' + viewMode);

    capStart({ mode: 'dur', dur: 0.5, stay: true });
    streamSim(0.8);
    check('capture by duration: 0.5 s at 1000 Hz', cap.n === 500, cap.n);

    capStart({ mode: 'write', pre: 0.2, post: 0.3, stay: true });
    streamSim(0.4);
    check('around-write capture is armed and waiting', cap.armed && cap.got === 0, cap.armed + ' got ' + cap.got);
    labSet(0x22, (F.regs[0x22] & ~0xFF) | 20, 'step for capture');
    await sleep(30);
    streamSim(0.6);
    check('around-write capture: 0.2 s before + 0.3 s after', cap.n >= 499 && cap.n <= 501, cap.n);
    check('marker sits at the trigger sample', cap.marks.length === 1 && cap.marks[0].i === 200, JSON.stringify(cap.marks));

    blobs.length = 0;
    capExportCsv();
    var csv = blobs.length ? await blobs[0].text() : '';
    check('capture CSV carries registers and the marker', /# register 22=/.test(csv) && /# marker sample 200/.test(csv) && /,marker\n/.test(csv),
          csv.slice(0, 120).replace(/\n/g, ' / '));
    blobs.length = 0;
    capExportJson();
    var js = blobs.length ? JSON.parse(await blobs[0].text()) : {};
    check('capture JSON has samples, markers, registers', js.samples === cap.n && js.markers && js.markers.length === 1 && js.registers && js.registers['1D'],
          js.samples + ' samples');

    /* ---- exports ------------------------------------------------------------ */
    blobs.length = 0; labExport('c');
    var ch = blobs.length ? await blobs[0].text() : '';
    check('C header export', /AFE4490_CONFIG\[\] = \{/.test(ch) && /\{ 0x1D, 0x000F9F \}/.test(ch) && /\{ 0x1E, 0x/.test(ch.split('0x1D')[1] || ''),
          (ch.match(/\{ 0x/g) || []).length + ' entries');
    blobs.length = 0; labExport('json');
    var jj = blobs.length ? JSON.parse(await blobs[0].text()) : {};
    check('JSON export is loadable by V3 (registers map)', jj.registers && jj.registers['1D'] === '000F9F', jj.registers && jj.registers['1D']);

    /* ---- verify ---------------------------------------------------------------- */
    F.corrupt = 0x21;
    await labVerify(); streamSim(0.05);
    check('verify flags the register silicon disagrees with', lab.verify && lab.verify.mism.length === 1 && lab.verify.mism[0].a === 0x21 &&
          lab.drc.some(function (x) { return /silicon differs/.test(x.title); }), JSON.stringify(lab.verify && lab.verify.mism));
    F.corrupt = null;
    await labVerify();
    check('verify clean once they agree', lab.verify.mism.length === 0, lab.verify.mism.length);

    /* ---- rail panels write through the lab ------------------------------------ */
    setView('monitor'); streamSim(0.1);
    // Count deliveries rather than inspect columns: a monitor chart that
    // reallocates on a width change legitimately drops what it had drawn.
    var markCalls = 0, plethMark = charts[0].mark;
    charts[0].mark = function (l, c) { markCalls++; return plethMark.call(this, l, c); };
    var logN = lab.log.length;
    nSent = F.sent.length;
    document.getElementById('inLed1').value = 90;
    document.getElementById('inLed1').dispatchEvent(new Event('change'));
    await sleep(30);
    check('rail LED slider writes via WM: and is logged', F.sent.slice(nSent).some(function (l) { return /^WM:22=/.test(l); }) && lab.log.length === logN + 1,
          F.sent.slice(nSent).join(' / '));
    streamSim(0.5);
    var txr = lab.log[0];
    check('monitor charts get the marker too', markCalls >= 1, JSON.stringify({
      calls: markCalls, marks: charts[0].marks.length, queued: markPlay.map(function (q) { return q.idx; }), txIdx: txr && txr.idx,
      seq: txr && txr.seq, disp: dispRead, written: buf.written, playing: playing, col: charts[0].col,
      w: charts[0].w, adv: charts[0].adv, on: chartOn.Pleth, view: viewMode }));

    /* ---- old firmware ----------------------------------------------------------- */
    F.wm = false; lab.fwWm = null;
    setView('lab');
    labSet(0x21, 2, 'old firmware');
    await sleep(60);
    tx = lab.log[0];
    check('firmware without WM: falls back to W:', lab.fwWm === false && tx.ok && tx.approx && F.regs[0x21] === 2 &&
          F.sent.some(function (l) { return l === 'W:21,000002'; }), JSON.stringify({ fw: lab.fwWm, ok: tx.ok, approx: tx.approx }));
    streamSim(0.2);

    /* ---- every dock tab renders -------------------------------------------------- */
    ['drc', 'pend', 'log', 'fx', 'snap', 'con'].forEach(function (d) { labDock(d); streamSim(0.02); });
    check('dock tabs render', document.getElementById('dkLog').querySelectorAll('tr').length > 5 &&
          document.getElementById('dkFx').querySelectorAll('tr').length > 1 &&
          document.getElementById('dkSnap').querySelectorAll('tr').length >= 3, '');

    check('no JS errors', R.errors.length === 0, R.errors.join(' | '));
    R.done = true;
    emit();
  }
})();
</script>
"""


def main():
    page = PAGE
    if "--page" in sys.argv:
        page = (ROOT / sys.argv[sys.argv.index("--page") + 1]).resolve()
    exe = CHROME if pathlib.Path(CHROME).exists() else EDGE
    if not pathlib.Path(exe).exists():
        print("no Chrome or Edge found")
        return 2
    html = page.read_text(encoding="utf-8").replace("</body>", DRIVER + "\n</body>")
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        target = tmp / "labtest.html"
        target.write_text(html, encoding="utf-8")
        dom = subprocess.run(
            [exe, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--window-size=1680,1050", "--force-device-scale-factor=1",
             "--user-data-dir=" + str(tmp / "p"),
             "--virtual-time-budget=60000", "--dump-dom", target.as_uri()],
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
        print("  %s  %-58s %s" % ("ok  " if c["ok"] else "FAIL", c["name"], c["detail"][:110]))
    for e in res.get("errors", []):
        print("  JS ERROR: " + e[:400])
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
