"""Exercise the V3 console's BLE transport without a radio.

A BLE link cannot be tested in headless Chrome - there is no adapter and no
device. What can be tested is everything on this side of it: that the UUIDs
match the firmware, that a notification is fed to the same parser the serial
path uses, that commands are routed to the right characteristic and chunked
safely, that a dropped link tears the session down, and that the console
explains itself rather than throwing when the origin forbids Bluetooth.

So this substitutes a fake GATT characteristic for the real one and drives
the console through it, byte for byte the way the firmware would.

Run:  python tools/ble_selftest.py
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
PAGE = ROOT / "web" / "afe4490_studio_V3.html"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

DRIVER = r"""
<div id="bletest" style="display:none"></div>
<script>
(function () {
  var R = { errors: [], checks: [] };
  function emit(){ document.getElementById('bletest').textContent = JSON.stringify(R); }
  function check(name, ok, detail){
    R.checks.push({ name:name, ok:!!ok, detail:String(detail === undefined ? '' : detail) });
  }
  window.addEventListener('error', function (e){
    R.errors.push((e.message||'error') + ' @' + (e.lineno||'?')); emit();
  });
  window.addEventListener('unhandledrejection', function (e){
    R.errors.push('rejection: ' + (e.reason && e.reason.message || e.reason)); emit();
  });

  // ---- build a protocol A1 frame exactly as src/main.cpp does ---------
  function crc(b){
    var c = 0xFFFF;
    for (var i=0;i<b.length;i++){
      c ^= b[i] << 8;
      for (var k=0;k<8;k++) c = (c & 0x8000) ? ((c<<1)^0x1021)&0xFFFF : (c<<1)&0xFFFF;
    }
    return c;
  }
  function frame(seq, count, rate){
    var body = [];
    body.push(seq&255, (seq>>8)&255, (seq>>16)&255, (seq>>24)&255);
    body.push(count, 0, rate&255, (rate>>8)&255);
    for (var s=0;s<count;s++){
      var vals = [1000000+s, 3000, 1200000+s, 3000, 997000+s, 1197000+s];
      for (var j=0;j<6;j++){
        var v = vals[j]|0;
        body.push(v&255, (v>>8)&255, (v>>16)&255, (v>>24)&255);
      }
    }
    var n = body.length;
    var head = [0xAA, 0x55, 0x01, n&255, (n>>8)&255];
    var c = crc(head.slice(2).concat(body));
    return new Uint8Array(head.concat(body).concat([c&255, (c>>8)&255]));
  }

  /* Type 0x02, the frame the firmware sends when the transport is BLE:
     the same conversions, four channels instead of six and three bytes
     each instead of four. Same values as frame() so the two can be
     compared sample for sample. */
  function compact(seq, count, rate){
    var body = [];
    body.push(seq&255, (seq>>8)&255, (seq>>16)&255, (seq>>24)&255);
    body.push(count, 0, rate&255, (rate>>8)&255);
    for (var s=0;s<count;s++){
      var vals = [1000000+s, 3000, 1200000+s, 3000];
      for (var j=0;j<4;j++){
        var v = vals[j] & 0xFFFFFF;
        body.push(v&255, (v>>8)&255, (v>>16)&255);
      }
    }
    var n = body.length;
    var head = [0xAA, 0x55, 0x02, n&255, (n>>8)&255];
    var c = crc(head.slice(2).concat(body));
    return new Uint8Array(head.concat(body).concat([c&255, (c>>8)&255]));
  }

  // ---- a stand-in for the GATT characteristics ------------------------
  var written = [];
  function fakeChar(uuid, canWriteNoResp){
    var listeners = [];
    var ch = {
      uuid: uuid,
      startNotifications: function(){ ch.notifying = true; return Promise.resolve(ch); },
      stopNotifications:  function(){ ch.notifying = false; return Promise.resolve(ch); },
      addEventListener: function(t, f){ if (t === 'characteristicvaluechanged') listeners.push(f); },
      removeEventListener: function(t, f){
        var i = listeners.indexOf(f); if (i >= 0) listeners.splice(i, 1);
      },
      writeValue: function(b){ written.push(b); return Promise.resolve(); },
      notify: function(bytes){
        var dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
        listeners.forEach(function(f){ f({ target: { value: dv } }); });
      },
      listenerCount: function(){ return listeners.length; }
    };
    if (canWriteNoResp)
      ch.writeValueWithoutResponse = function(b){ written.push(b); return Promise.resolve(); };
    return ch;
  }

  window.addEventListener('load', function(){ setTimeout(run, 150); });

  function run(){
    body().then(function(){
      R.done = true; document.title = 'BLETEST DONE'; emit();
    }).catch(function(e){
      R.errors.push('driver: ' + e.message + ' | ' + (e.stack||'').split('\n')[1]);
      R.done = true; emit();
    });
  }

  /* A device emulator on the far end of the fake GATT link. Enough of the
     firmware's replies to let the console's connect sequence complete, so
     the handshake can be tested rather than just the byte plumbing. */
  function handshake(){
    var tx = fakeChar(NUS.tx, false), rx = fakeChar(NUS.rx, true);
    var sent = [], line = '';
    function say(t){ tx.notify(new TextEncoder().encode(t + '\r\n')); }
    function onWrite(b){
      line += new TextDecoder().decode(b);
      var i;
      while ((i = line.indexOf('\n')) >= 0){
        var cmd = line.slice(0, i).trim(); line = line.slice(i+1);
        if (!cmd) continue;
        sent.push(cmd);
        if (cmd === 'PING')  say('AFE:PONG:V2.0.0');
        else if (cmd === 'CAPS?') say('CAPS:PROTO=A1,FW=V2.0.0,CHIP=AFE4490,BLE=1');
        else if (cmd === 'REG?'){
          say('REG:1D=001F3F'); say('REG:22=012828'); say('REG:END');
        }
        else if (cmd.indexOf('DEC:') === 0) say('ACK:DEC:' + cmd.slice(4));
        else if (cmd === 'STATUS?') say('ST:fw=V2.0.0,prf=500,link=BLE,drop=0');
        else if (cmd === 'START') say('ACK:START');
        else say('ACK:' + cmd);
      }
    }
    rx.writeValueWithoutResponse = function(b){ onWrite(b); return Promise.resolve(); };
    rx.writeValue = rx.writeValueWithoutResponse;

    link.bleTx = tx; link.bleRx = rx;
    link.bleDev = { name:'AFE4490-XIAO',
                    gatt:{ connected:true, disconnect:function(){ this.connected = false; } },
                    addEventListener:function(){}, removeEventListener:function(){} };
    link.kind = 'ble';
    tx.addEventListener('characteristicvaluechanged', onBleData);
    tx.startNotifications();
    afterConnect();                       // this schedules the handshake

    return new Promise(function(res){ setTimeout(res, 4000); }).then(function(){
      check('handshake ran over BLE', sent.indexOf('PING') >= 0 && sent.indexOf('CAPS?') >= 0,
            sent.slice(0, 6).join(' '));
      /* tools/chrome_ble_probe.py measured Chrome sustaining 8.9
         notifications a second on this link - 140 samples/s at 16 samples a
         packet - so the console opens at 125 Hz, which needs 7.8. */
      check('BLE connect sets the measured-safe rate', sent.indexOf('DEC:4') >= 0,
            sent.join(' ').slice(0, 90));
      check('decimation box follows', document.getElementById('inDec').value === '4',
            document.getElementById('inDec').value);
      check('and it started streaming', sent.indexOf('START') >= 0, sent.join(' ').slice(0, 90));
      check('the BLE banner explains why',
            /125 Hz/.test(document.getElementById('bannerTitle').textContent + ' ' +
                          document.getElementById('bannerText').textContent),
            document.getElementById('bannerTitle').textContent);

      /* The back-off is the safety net for the one link that has not been
         measured, so it gets tested rather than trusted. Two status lines
         with a rising drop count must halve the rate exactly once. */
      var decBefore = document.getElementById('inDec').value;
      ble.backoffAt = -1e6;
      streaming = true;
      handleLine('ST:fw=V2.0.0,prf=500,dec=4,drop=0,txdrop=0');
      handleLine('ST:fw=V2.0.0,prf=500,dec=4,drop=140,txdrop=0');
      return new Promise(function (r) { setTimeout(r, 450); }).then(function () {
        check('a rising drop count backs the rate off',
              document.getElementById('inDec').value === '8',
              decBefore + ' -> ' + document.getElementById('inDec').value);
        check('and says so', /dropped samples/i.test(
                document.getElementById('bannerTitle').textContent),
              document.getElementById('bannerTitle').textContent);
        check('a steady drop count does not keep stepping',
              (function () {
                ble.backoffAt = -1e6;
                handleLine('ST:fw=V2.0.0,prf=500,dec=8,drop=140,txdrop=0');
                return document.getElementById('inDec').value === '8';
              })(), document.getElementById('inDec').value);
      });
    });
  }

  function body(){
    // ---- 1. the UUIDs are the ones the firmware advertises -----------
    check('NUS service UUID', NUS.service === '6e400001-b5a3-f393-e0a9-e50e24dcca9e', NUS.service);
    check('NUS notify (device to host)', NUS.tx === '6e400003-b5a3-f393-e0a9-e50e24dcca9e', NUS.tx);
    check('NUS write (host to device)',  NUS.rx === '6e400002-b5a3-f393-e0a9-e50e24dcca9e', NUS.rx);

    // ---- 2. a blocked origin explains itself, it does not throw -------
    var savedBt = navigator.bluetooth;
    try { Object.defineProperty(navigator, 'bluetooth', {value: undefined, configurable: true}); }
    catch (e) {}
    var threw = false;
    return connectBle().catch(function(){ threw = true; }).then(function(){
      check('no Bluetooth: handled, not thrown', !threw, 'ok');
      check('no Bluetooth: banner shown',
            document.getElementById('banner').classList.contains('on'),
            document.getElementById('bannerTitle').textContent);
      try { Object.defineProperty(navigator, 'bluetooth', {value: savedBt, configurable: true}); }
      catch (e) {}
      showBanner(null);

      // ---- 3. stand up a fake link and feed it a real frame -----------
      var tx = fakeChar(NUS.tx, false), rx = fakeChar(NUS.rx, true);
      link.bleTx = tx; link.bleRx = rx;
      link.bleDev = { name:'AFE4490-XIAO',
                      gatt:{ connected:true, disconnect:function(){ this.connected = false; } },
                      addEventListener:function(){}, removeEventListener:function(){} };
      link.kind = 'ble';
      tx.addEventListener('characteristicvaluechanged', onBleData);
      tx.startNotifications();
      afterConnect();

      check('status bar names the transport',
            /BLE/.test(document.getElementById('sbLink').textContent),
            document.getElementById('sbLink').textContent);
      check('BLE button hides while connected',
            document.getElementById('btnBle').style.display === 'none',
            document.getElementById('btnBle').style.display || '(shown)');

      var before = link.samples, framesBefore = link.frames, errBefore = link.crcErr;
      tx.notify(frame(0, 8, 500));
      check('a notification reaches the parser', link.samples === before + 8,
            (link.samples - before) + ' samples, ' + (link.frames - framesBefore) + ' frame');
      check('and it passes CRC', link.crcErr === errBefore, 'crcErr ' + link.crcErr);

      // Split one frame across two notifications, the way a small MTU does.
      var f = frame(8, 8, 500), cut = 37;
      before = link.samples;
      tx.notify(f.slice(0, cut));
      check('a half frame is held, not mis-parsed', link.samples === before, 'held');
      tx.notify(f.slice(cut));
      check('the rest completes it', link.samples === before + 8,
            (link.samples - before) + ' samples');

      // An ASCII line interleaved with frames, as the firmware really sends.
      before = link.samples;
      tx.notify(new TextEncoder().encode('ST:fw=V2.0.0,link=BLE\r\n'));
      tx.notify(frame(16, 8, 500));
      check('ASCII interleaves with frames cleanly', link.samples === before + 8 &&
            link.crcErr === errBefore, link.samples - before + ' samples, crcErr ' + link.crcErr);

      // ---- 3b. the compact frame, which exists only on this transport --
      before = link.samples; errBefore = link.crcErr;
      var cf = compact(24, 16, 125);
      tx.notify(cf);
      check('compact frame parses', link.samples === before + 16,
            (link.samples - before) + ' samples');
      check('compact frame passes CRC', link.crcErr === errBefore,
            'crcErr ' + link.crcErr);
      check('16 BLE samples still fit one packet', cf.length === 207,
            cf.length + ' B');
      // The whole reason the format exists: same 16 samples, half the air.
      check('compact halves the byte rate',
            cf.length - 15 === (frame(0, 16, 125).length - 15) / 2,
            (cf.length - 15) + ' B vs ' + (frame(0, 16, 125).length - 15) + ' B');
      check('ambient subtraction restored by the console',
            Math.round(at(buf.redAmb, 0)) === 997015 &&
            Math.round(at(buf.irAmb, 0)) === 1197015,
            at(buf.redAmb, 0) + ' / ' + at(buf.irAmb, 0));
      check('raw channels survive the 24-bit trip',
            Math.round(at(buf.red, 0)) === 1000015 &&
            Math.round(at(buf.amb2, 0)) === 3000,
            at(buf.red, 0) + ' / ' + at(buf.amb2, 0));
      check('compact frame adopts its own rate', stats.rate === 125, stats.rate);

      // A compact frame split across notifications, as a small MTU does.
      before = link.samples;
      var cf2 = compact(40, 16, 125);
      tx.notify(cf2.slice(0, 91));
      check('half a compact frame is held', link.samples === before, 'held');
      tx.notify(cf2.slice(91));
      check('the rest completes it', link.samples === before + 16,
            (link.samples - before) + ' samples');

      // Both frame types in one stream, which is what a USB console
      // watching a board that just switched to BLE would actually see.
      before = link.samples; errBefore = link.crcErr;
      tx.notify(compact(56, 4, 125));
      tx.notify(frame(60, 4, 500));
      check('both frame types coexist', link.samples === before + 8 &&
            link.crcErr === errBefore,
            (link.samples - before) + ' samples, crcErr ' + link.crcErr);

      // ---- 3c. a line longer than one notification -------------------
      // STATUS grew past 242 bytes once the battery fields went in, and the
      // firmware used to truncate it - silently, leaving a line that still
      // looked well formed but had lost whichever counters sorted last.
      // It now spans packets, so the console has to put them back together.
      var longLine = 'ST:fw=V2.0.0,proto=A1,stream=1,dec=1,vbat=4.16,bpct=99,'
                   + 'chg=1,vbus=1,pad=' + new Array(230).join('x') + ',up=999';
      var enc2 = new TextEncoder().encode(longLine + String.fromCharCode(13, 10));
      var seen = null, tapWas = lineTap;
      lineTap = function (l) { if (l.indexOf('pad=') >= 0) seen = l; };
      tx.notify(enc2.slice(0, 242));
      check('a split line is not delivered early', seen === null,
            seen === null ? 'held' : 'leaked');
      tx.notify(enc2.slice(242));
      lineTap = tapWas;
      check('a split line reassembles whole', seen === longLine,
            seen ? seen.length + ' of ' + longLine.length + ' chars' : 'never arrived');
      check('and the battery fields survived it',
            document.getElementById('batV').textContent === '4.160',
            document.getElementById('batV').textContent);

      // ---- 4. commands go out on the write characteristic ------------
      written.length = 0;
      return linkSend('STATUS?\n').then(function(){
        check('command written to the RX characteristic', written.length >= 1,
              written.length + ' write(s)');
        var joined = '';
        written.forEach(function(b){ joined += new TextDecoder().decode(b); });
        check('command arrives intact', joined === 'STATUS?\n', JSON.stringify(joined));

        written.length = 0;
        var long = 'W:21,' + '0'.repeat(60) + '\n';
        return linkSend(long).then(function(){
          var max = 0;
          written.forEach(function(b){ max = Math.max(max, b.length); });
          check('long command chunked to a safe size', max <= 20, 'largest chunk ' + max + ' B');
          var j2 = '';
          written.forEach(function(b){ j2 += new TextDecoder().decode(b); });
          check('chunks reassemble to the original', j2 === long, j2.length + ' of ' + long.length);

          // ---- 5. a dropped link tears the session down ---------------
          onBleDropped();
          return new Promise(function(res){ setTimeout(res, 60); });
        });
      }).then(function(){
        check('drop disconnects', link.connected === false, 'connected=' + link.connected);
        check('drop clears the BLE handles',
              link.bleTx === null && link.bleRx === null && link.bleDev === null,
              'tx=' + link.bleTx + ' rx=' + link.bleRx + ' dev=' + link.bleDev);
        check('drop stops notification listeners', tx.listenerCount() === 0,
              tx.listenerCount() + ' left');
        check('drop restores the connect buttons',
              document.getElementById('btnConnect').textContent === 'Connect' &&
              document.getElementById('btnBle').style.display !== 'none',
              document.getElementById('btnConnect').textContent);
        return handshake();
      }).then(function(){
        check('no JS errors', R.errors.length === 0, R.errors.join(' | '));
      });
    });
  }
})();
</script>
"""


def main():
    page_path = PAGE
    if "--page" in sys.argv:
        page_path = (ROOT / sys.argv[sys.argv.index("--page") + 1]).resolve()
    exe = CHROME if pathlib.Path(CHROME).exists() else EDGE
    if not pathlib.Path(exe).exists():
        print("no Chrome or Edge found")
        return 2
    html = page_path.read_text(encoding="utf-8").replace("</body>", DRIVER + "\n</body>")
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        page = tmp / "bletest.html"
        page.write_text(html, encoding="utf-8")
        dom = subprocess.run(
            [exe, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--window-size=1600,1000", "--force-device-scale-factor=1",
             "--user-data-dir=" + str(tmp / "p"),
             "--virtual-time-budget=20000", "--dump-dom", page.as_uri()],
            capture_output=True, text=True, timeout=_chrome_timeout(240)).stdout or ""

    m = re.search(r'<div id="bletest"[^>]*>(.*?)</div>', dom, re.S)
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
        print("  %s  %-44s %s" % ("ok  " if c["ok"] else "FAIL", c["name"], c["detail"][:80]))
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
