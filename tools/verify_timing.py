"""Check AFE4490::applyTiming() against the datasheet's worked example.

SBAS602H Table 2 lists every edge register for PRF = 500 Hz at 25 % duty.
This is a straight transcription of the arithmetic in src/AFE4490.cpp, so
if the two disagree the firmware is wrong, not the table.

Also sweeps the whole supported PRF range and asserts the invariants that
actually matter on silicon: phases stay inside their own quarter, the Rx
window never runs past the LED window, conversions start exactly one clock
after the ADC reset ends, and nothing exceeds PRPCOUNT.

Run:  python tools/verify_timing.py
"""
import sys

# ---- datasheet Table 2, PRF = 500 Hz, duty = 25 % ----------------------
TABLE2 = {
    0x01: 6050, 0x02: 7998, 0x03: 6000, 0x04: 7999,
    0x05:   50, 0x06: 1998, 0x07: 2050, 0x08: 3998,
    0x09: 2000, 0x0A: 3999, 0x0B: 4050, 0x0C: 5998,
    0x0D:    4, 0x0E: 1999, 0x0F: 2004, 0x10: 3999,
    0x11: 4004, 0x12: 5999, 0x13: 6004, 0x14: 7999,
    0x15:    0, 0x16:    3, 0x17: 2000, 0x18: 2003,
    0x19: 4000, 0x1A: 4003, 0x1B: 6000, 0x1C: 6003,
    0x1D: 7999,
}
NAMES = {
    0x01: 'LED2STC', 0x02: 'LED2ENDC', 0x03: 'LED2LEDSTC', 0x04: 'LED2LEDENDC',
    0x05: 'ALED2STC', 0x06: 'ALED2ENDC', 0x07: 'LED1STC', 0x08: 'LED1ENDC',
    0x09: 'LED1LEDSTC', 0x0A: 'LED1LEDENDC', 0x0B: 'ALED1STC', 0x0C: 'ALED1ENDC',
    0x0D: 'LED2CONVST', 0x0E: 'LED2CONVEND', 0x0F: 'ALED2CONVST', 0x10: 'ALED2CONVEND',
    0x11: 'LED1CONVST', 0x12: 'LED1CONVEND', 0x13: 'ALED1CONVST', 0x14: 'ALED1CONVEND',
    0x15: 'ADCRSTSTCT0', 0x16: 'ADCRSTENDCT0', 0x17: 'ADCRSTSTCT1', 0x18: 'ADCRSTENDCT1',
    0x19: 'ADCRSTSTCT2', 0x1A: 'ADCRSTENDCT2', 0x1B: 'ADCRSTSTCT3', 0x1C: 'ADCRSTENDCT3',
    0x1D: 'PRPCOUNT',
}


def apply_timing(prf=500, duty=25, settle=50, adc_rst=4):
    """Transcription of AFE4490::applyTiming()."""
    prf = max(62, min(1000, prf))
    duty = max(1, min(25, duty))
    adc_rst = max(2, min(16, adc_rst))

    T = 4_000_000 // prf
    T = max(800, min(64000, T))
    Q = T // 4

    ton = (T * duty) // 100
    ton = min(ton, Q)
    settle = min(settle, Q // 2)
    if ton < settle + 202:
        ton = settle + 202
        if ton > Q:
            ton = Q
            settle = Q - 202 if Q > 202 else 1

    sample_end = ton - 2
    led_end = ton - 1
    r = {}

    r[0x05] = 0 * Q + settle
    r[0x06] = 0 * Q + sample_end
    r[0x09] = 1 * Q
    r[0x0A] = 1 * Q + led_end
    r[0x07] = 1 * Q + settle
    r[0x08] = 1 * Q + sample_end
    r[0x0B] = 2 * Q + settle
    r[0x0C] = 2 * Q + sample_end
    r[0x03] = 3 * Q
    r[0x04] = 3 * Q + led_end
    r[0x01] = 3 * Q + settle
    r[0x02] = 3 * Q + sample_end

    rst_start = [0x15, 0x17, 0x19, 0x1B]
    rst_end = [0x16, 0x18, 0x1A, 0x1C]
    cv_start = [0x0D, 0x0F, 0x11, 0x13]
    cv_end = [0x0E, 0x10, 0x12, 0x14]
    for k in range(4):
        P = k * Q
        r[rst_start[k]] = P
        r[rst_end[k]] = P + adc_rst - 1
        r[cv_start[k]] = P + adc_rst
        r[cv_end[k]] = P + Q - 1

    r[0x1D] = T - 1
    return r, T, Q, ton, settle


def compute_numav(prp):
    per_quarter = (prp // 4) // 200
    if per_quarter < 2:
        return 0
    return min(15, per_quarter - 2)


def main():
    fails = []

    # ---- 1. reproduce the datasheet example exactly --------------------
    got, T, Q, ton, settle = apply_timing(500, 25, 50, 4)
    print('PRF 500 Hz / 25 %% duty  ->  T=%d Q=%d Ton=%d settle=%d' % (T, Q, ton, settle))
    for addr in sorted(TABLE2):
        want, have = TABLE2[addr], got.get(addr)
        mark = 'ok ' if want == have else 'BAD'
        if want != have:
            fails.append('%s (0x%02X): datasheet %d, firmware %s' % (NAMES[addr], addr, want, have))
        print('  %s 0x%02X %-14s datasheet %5d   firmware %5s' % (mark, addr, NAMES[addr], want, have))

    numav = compute_numav(T)
    print('  NUMAV = %d  (%d averages)' % (numav, numav + 1))

    # ---- 2. invariants across the whole supported range ----------------
    print('\nsweeping PRF x duty ...')
    checked = 0
    for prf in (62, 100, 125, 250, 400, 500, 625, 800, 1000):
        for duty in (1, 5, 10, 15, 20, 25):
            r, T, Q, ton, settle = apply_timing(prf, duty, 50, 4)
            prp = r[0x1D]
            ctx = 'PRF=%d duty=%d%%' % (prf, duty)
            checked += 1

            if not (800 <= T <= 64000):
                fails.append('%s: PRPCOUNT %d outside 800..64000' % (ctx, T))

            for addr, v in r.items():
                if v > prp:
                    fails.append('%s: %s=%d exceeds PRPCOUNT %d' % (ctx, NAMES[addr], v, prp))
                if v < 0 or v > 0xFFFF:
                    fails.append('%s: %s=%d not a 16-bit count' % (ctx, NAMES[addr], v))

            # Every phase must stay inside its own quarter.
            quarters = [
                (0, [0x05, 0x06, 0x15, 0x16, 0x0D, 0x0E]),
                (1, [0x09, 0x0A, 0x07, 0x08, 0x17, 0x18, 0x0F, 0x10]),
                (2, [0x0B, 0x0C, 0x19, 0x1A, 0x11, 0x12]),
                (3, [0x03, 0x04, 0x01, 0x02, 0x1B, 0x1C, 0x13, 0x14]),
            ]
            for k, addrs in quarters:
                lo, hi = k * Q, (k + 1) * Q - 1
                for a in addrs:
                    if not (lo <= r[a] <= hi):
                        fails.append('%s: %s=%d escapes quarter %d [%d..%d]'
                                     % (ctx, NAMES[a], r[a], k, lo, hi))

            # Rx sampling must sit inside the LED-on window, after settling.
            for s_st, s_en, l_st, l_en in ((0x07, 0x08, 0x09, 0x0A), (0x01, 0x02, 0x03, 0x04)):
                if r[s_st] <= r[l_st]:
                    fails.append('%s: %s starts before the LED turns on' % (ctx, NAMES[s_st]))
                if r[s_en] >= r[l_en]:
                    fails.append('%s: %s ends after the LED turns off' % (ctx, NAMES[s_en]))
                if r[s_en] - r[s_st] < 199:
                    fails.append('%s: %s window %d counts < 50 us minimum'
                                 % (ctx, NAMES[s_st], r[s_en] - r[s_st]))

            # A conversion starts one clock after its reset pulse ends.
            for rst_e, cv_s in ((0x16, 0x0D), (0x18, 0x0F), (0x1A, 0x11), (0x1C, 0x13)):
                if r[cv_s] != r[rst_e] + 1:
                    fails.append('%s: %s must start 1 clock after %s'
                                 % (ctx, NAMES[cv_s], NAMES[rst_e]))

    print('checked %d PRF/duty combinations' % checked)

    if fails:
        print('\nFAIL (%d)' % len(fails))
        for f in fails[:30]:
            print('  ' + f)
        return 1
    print('\nPASS: matches SBAS602H Table 2 and all invariants hold')
    return 0


if __name__ == '__main__':
    sys.exit(main())
