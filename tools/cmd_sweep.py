"""Exercise every command the firmware advertises and check the reply shape.

This validates the dispatcher, the argument parsing and the reply format.
It does NOT need an AFE4490 attached: register values will not stick with
no chip on the bus, but each command must still answer ACK (or a specific
documented reply) rather than ERR or silence.

Run:  python tools/cmd_sweep.py [COM5]
"""
import re
import sys
import time

sys.path.insert(0, __file__.rsplit('\\', 1)[0].rsplit('/', 1)[0])
from link_probe import Link, pick_port          # noqa: E402

# (command, regex the reply must match, settle seconds)
CASES = [
    ('PING',            r'^AFE:PONG:V',            0.3),
    ('CAPS?',           r'^CAPS:PROTO=A1',         0.3),
    ('STATUS?',         r'^ST:fw=',                0.3),
    ('R:23',            r'^REG:23=[0-9A-F]{6}$',   0.3),
    ('W:22,012828',     r'^ACK:W:22=',             0.4),
    ('FMT:TEXT',        r'^ACK:FMT:TEXT$',         0.3),
    ('FMT:BIN',         r'^ACK:FMT:BIN$',          0.3),
    ('DEC:4',           r'^ACK:DEC:4$',            0.3),
    ('DEC:1',           r'^ACK:DEC:1$',            0.3),
    ('PRF:250,20,60,6', r'^ACK:PRF:250,20,60,6',   1.2),
    ('PRF:500,25,50,4', r'^ACK:PRF:500,25,50,4',   1.2),
    ('NUMAV:3',         r'^ACK:NUMAV:3$',          0.4),
    ('NUMAV:AUTO',      r'^ACK:NUMAV:AUTO=',       1.2),
    ('TIMER:1',         r'^ACK:TIMER:1$',          0.3),
    ('LED:40,40',       r'^ACK:LED:40,40,',        0.4),
    ('LEDRANGE:1',      r'^ACK:LEDRANGE:1,fs=',    0.4),
    ('TXREF:1',         r'^ACK:TXREF:1,fs=',       0.5),
    ('TIA:2,0',         r'^ACK:TIA:rf=100000ohm',  0.4),
    ('TIA:0,0',         r'^ACK:TIA:rf=500000ohm',  0.4),
    ('STAGE2:1,4',      r'^ACK:STAGE2:1,4,12.0dB', 0.4),
    ('STAGE2:0,0',      r'^ACK:STAGE2:0,0,0.0dB',  0.4),
    ('AMBDAC:3',        r'^ACK:AMBDAC:3uA$',       0.4),
    ('AMBDAC:0',        r'^ACK:AMBDAC:0uA$',       0.4),
    ('FILTER:1',        r'^ACK:FILTER:1000Hz$',    0.4),
    ('FILTER:0',        r'^ACK:FILTER:500Hz$',     0.4),
    ('PDN:0,0,0',       r'^ACK:PDN:0,0,0$',        0.4),
    # The SPIM has discrete clocks and the BSP rounds a request UP to the
    # next one, so these assert the quantized value, not the request.
    ('SPI:SET:2000000', r'^ACK:SPI:2000000,req=2000000$', 0.3),
    ('SPI:SET:200000',  r'^ACK:SPI:250000,req=200000$',   0.3),
    ('SPI:SET:6000000', r'^ACK:SPI:8000000,req=6000000$', 0.3),
    ('SPI:SET:16000000', r'^ACK:SPI:8000000,req=16000000$', 0.3),
    ('SPI:SET:4000000', r'^ACK:SPI:4000000,req=4000000$', 0.3),
    ('SPI:DRIVE:1',     r'^ACK:SPI:DRIVE=H0H1$',    0.3),
    ('SPI:DRIVE:0',     r'^ACK:SPI:DRIVE=S0S1$',    0.3),
    ('SPI:TEST',        r'^SPITEST:END:BEST=',      9.0),
    ('SELFTEST:16',     r'^ACK:SELFTEST:\d+/16,',  1.5),
    ('PINTEST',         r'^PIN:MISO=',             2.0),
    ('DIAG',            r'^DIAG:[0-9A-F]{6}$',     2.0),
    ('START',           r'^ACK:START$',            0.3),
    ('STOP',            r'^ACK:STOP$',             0.3),
    ('DEFAULTS',        r'^ACK:DEFAULTS$',         2.5),
    # Malformed input must be rejected, never partially applied.
    ('W:FF,000000',     r'^ERR:BAD_ADDR$',         0.3),
    ('W:22',            r'^ERR:BAD_W_FORMAT$',     0.3),
    ('LED:300,0',       r'^ERR:BAD_LED$',          0.3),
    ('AMBDAC:99',       r'^ERR:BAD_AMBDAC$',       0.3),
    ('DEC:0',           r'^ERR:BAD_DEC$',          0.3),
    ('TIA:9,0',         r'^ERR:BAD_TIA$',          0.3),
    ('NONSENSE',        r'^ERR:UNKNOWN_COMMAND$',  0.3),
]


def main():
    port = pick_port(sys.argv[1] if len(sys.argv) > 1 else None)
    print('opening %s' % port)
    link = Link(port)
    time.sleep(0.4)
    link.ser.reset_input_buffer()
    link.pump(0.6)
    link.lines.clear()

    passed = failed = 0
    for cmd, pattern, wait in CASES:
        replies = link.cmd(cmd, wait)
        hit = next((r for r in replies if re.search(pattern, r)), None)
        if hit:
            passed += 1
            print('  ok   %-18s -> %s' % (cmd, hit[:74]))
        else:
            failed += 1
            print('  FAIL %-18s -> %s' % (cmd, replies if replies else '(no reply)'))

    link.ser.close()
    print('\n%d passed, %d failed' % (passed, failed))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
