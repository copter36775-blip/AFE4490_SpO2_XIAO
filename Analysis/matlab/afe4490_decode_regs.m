function cfg = afe4490_decode_regs(regs)
%AFE4490_DECODE_REGS  Turn a captured AFE4490 register map into settings.
%
%   CFG = AFE4490_DECODE_REGS(REGS) takes the containers.Map that
%   AFE4490_LOAD builds from a capture header ('1D' -> uint32, ...) and
%   returns the physical settings those registers mean.
%
%   This is what makes a capture reproducible. A waveform on its own cannot
%   say how much LED current produced it or what transimpedance turned the
%   photocurrent into volts, so two recordings that look alike may be two
%   quite different measurements. The register block in the capture header
%   carries that, and this function reads it.
%
%   Fields of CFG
%   -------------
%     prpcount     PRPCOUNT (0x1D), the pulse-repetition period - 1, in
%                  counts of the AFE's internal 4 MHz clock
%     prf          4e6 / (prpcount + 1), the true pulse rate in Hz
%     numav        number of ADC averages per phase (NUMAV + 1, max 16)
%     timer_on     CONTROL1 TIMEREN - false means the sequencer was stopped
%     rf_led2      transimpedance on the LED2/shared path, ohms
%     rf_led1      transimpedance on the LED1 path, ohms (0x20 only when
%                  ENSEPGAIN is set, otherwise the same as rf_led2)
%     cf_led2      feedback capacitance on the LED2/shared path, pF
%     cf_led1      the same for LED1
%     stage2_led2  linear stage-2 gain on LED2 (1 when bypassed)
%     stage2_led1  the same for LED1
%     sep_gain     ENSEPGAIN: were the two channels given separate gain?
%     ambdac_uA    ambient-cancellation DAC current, microamps
%     filter_hz    anti-alias corner, 500 or 1000 Hz
%     txref_v      transmitter reference voltage
%     led_fs_mA    LED full-scale current for this TX_REF / LED_RANGE pair
%     led1_mA      IR  drive current, mA   (code/256 * full scale)
%     led2_mA      RED drive current, mA
%     ipd_fs_led2  full-scale photodiode current on the LED2 path, amps:
%                  the TIA is differential, so 1 V full scale is 1/(2*RF)
%     ipd_fs_led1  the same for LED1
%     decimation   always 1 here; AFE4490_LOAD overwrites it from the header
%     summary      one printable line
%
%   See also AFE4490_LOAD.

%   Field positions are from TI SBAS602H section 8.6 (register map) and
%   Table 6 (full-scale LED current across TX_REF settings).

    cfg = struct();
    cfg.decimation = 1;

    g = @(key) getReg(regs, key);

    % ---- 0x1D PRPCOUNT: the pulse repetition period ------------------
    prp = g('1D');
    if ~isnan(prp)
        cfg.prpcount = double(bitand(prp, 65535));
        if cfg.prpcount > 0
            cfg.prf = 4e6 / (cfg.prpcount + 1);
        else
            cfg.prf = NaN;
        end
    else
        cfg.prpcount = NaN;
        cfg.prf = NaN;
    end

    % ---- 0x1E CONTROL1: timer enable and averaging -------------------
    c1 = g('1E');
    if ~isnan(c1)
        % NUMAV is the number of averages minus one and saturates at 16.
        cfg.numav = min(double(bitand(c1, 255)), 15) + 1;
        cfg.timer_on = bitand(c1, 256) ~= 0;
    else
        cfg.numav = NaN;
        cfg.timer_on = NaN;
    end

    % ---- 0x20 TIAGAIN / 0x21 TIA_AMB_GAIN: the receive chain ---------
    RF_OHMS   = [500e3 250e3 100e3 50e3 25e3 10e3 1e6 0];   % code 7 = open
    STG2_LIN  = [1 1.5 2 3 4 1 1 1];
    CF_ADD_PF = [5 15 25 50 150];                           % plus a fixed 5 pF

    tg = g('20');
    ag = g('21');
    cfg.sep_gain = ~isnan(tg) && bitand(tg, 32768) ~= 0;

    [cfg.rf_led2, cfg.cf_led2, cfg.stage2_led2] = gainOf(ag, RF_OHMS, STG2_LIN, CF_ADD_PF);
    if cfg.sep_gain
        [cfg.rf_led1, cfg.cf_led1, cfg.stage2_led1] = gainOf(tg, RF_OHMS, STG2_LIN, CF_ADD_PF);
    else
        % ENSEPGAIN clear: both channels run on 0x21 and 0x20 is inert.
        cfg.rf_led1 = cfg.rf_led2;
        cfg.cf_led1 = cfg.cf_led2;
        cfg.stage2_led1 = cfg.stage2_led2;
    end

    if ~isnan(ag)
        cfg.ambdac_uA = double(bitand(bitshift(ag, -16), 15));
        if cfg.ambdac_uA > 10, cfg.ambdac_uA = NaN; end     % 1011..1111 undefined
        if bitand(ag, 32768) ~= 0, cfg.filter_hz = 1000; else, cfg.filter_hz = 500; end
    else
        cfg.ambdac_uA = NaN;
        cfg.filter_hz = NaN;
    end

    % ---- 0x22 LEDCNTRL / 0x23 CONTROL2: the transmitter --------------
    % Table 6: full-scale mA indexed [TX_REF][LED_RANGE].
    LED_FS = [150 75 150 0;      % TX_REF 00 = 0.75 V
              100 50 100 0;      % TX_REF 01 = 0.50 V
              200 100 200 0;     % TX_REF 10 = 1.00 V
              150 75 150 0];     % TX_REF 11 = 0.75 V
    TXREF_V = [0.75 0.50 1.00 0.75];

    lc = g('22');
    c2 = g('23');
    if ~isnan(c2)
        txref = double(bitand(bitshift(c2, -17), 3));
        cfg.txref_v = TXREF_V(txref + 1);
        cfg.pdn_afe = bitand(c2, 1) ~= 0;
        cfg.pdn_rx  = bitand(c2, 2) ~= 0;
        cfg.pdn_tx  = bitand(c2, 4) ~= 0;
    else
        txref = 0;
        cfg.txref_v = NaN;
        cfg.pdn_afe = NaN; cfg.pdn_rx = NaN; cfg.pdn_tx = NaN;
    end
    if ~isnan(lc)
        range = double(bitand(bitshift(lc, -16), 3));
        cfg.led_range = range;
        cfg.led_fs_mA = LED_FS(txref + 1, range + 1);
        cfg.led1_code = double(bitand(bitshift(lc, -8), 255));
        cfg.led2_code = double(bitand(lc, 255));
        cfg.led1_mA = cfg.led1_code / 256 * cfg.led_fs_mA;
        cfg.led2_mA = cfg.led2_code / 256 * cfg.led_fs_mA;
    else
        cfg.led_range = NaN; cfg.led_fs_mA = NaN;
        cfg.led1_code = NaN; cfg.led2_code = NaN;
        cfg.led1_mA = NaN;   cfg.led2_mA = NaN;
    end

    % ---- what the gain means at the photodiode -----------------------
    % The TIA is differential: its 1 V full-scale output corresponds to an
    % input current of 1/(2*RF), which reproduces the datasheet's I_IN_FS
    % table exactly (500 k -> 1 uA, 100 k -> 5 uA, 10 k -> 50 uA). Stage 2
    % shrinks the usable input range by its own gain.
    cfg.ipd_fs_led2 = fsCurrent(cfg.rf_led2, cfg.stage2_led2);
    cfg.ipd_fs_led1 = fsCurrent(cfg.rf_led1, cfg.stage2_led1);

    cfg.summary = sprintf(['PRF %.3f Hz (PRPCOUNT %d), %d average(s) | ' ...
                           'RED %.2f mA, IR %.2f mA of %g mA FS, TX_REF %.2f V | ' ...
                           'RF %s, stage2 x%g, CF %g pF, AMBDAC %g uA, filter %g Hz'], ...
        cfg.prf, cfg.prpcount, cfg.numav, cfg.led2_mA, cfg.led1_mA, ...
        cfg.led_fs_mA, cfg.txref_v, ohmStr(cfg.rf_led2), cfg.stage2_led2, ...
        cfg.cf_led2, cfg.ambdac_uA, cfg.filter_hz);
end

% ======================================================================
function v = getReg(regs, key)
    if isa(regs, 'containers.Map') && regs.isKey(key)
        v = double(regs(key));
    else
        v = NaN;
    end
end

function [rf, cf, g2] = gainOf(reg, RF_OHMS, STG2_LIN, CF_ADD_PF)
    if isnan(reg)
        rf = NaN; cf = NaN; g2 = NaN; return;
    end
    rf = RF_OHMS(double(bitand(reg, 7)) + 1);
    cfField = double(bitand(bitshift(reg, -3), 31));
    cf = 5;                                   % a fixed 5 pF is always present
    for b = 1:5
        if bitand(cfField, 2^(b - 1)) ~= 0
            cf = cf + CF_ADD_PF(b);
        end
    end
    if bitand(reg, 16384) ~= 0                % STAGE2EN
        g2 = STG2_LIN(double(bitand(bitshift(reg, -8), 7)) + 1);
    else
        g2 = 1;                               % stage 2 bypassed
    end
end

function i = fsCurrent(rf, g2)
    if isnan(rf) || rf == 0 || isnan(g2)
        i = NaN;
    else
        i = 0.5 / rf / g2;
    end
end

function s = ohmStr(r)
    if isnan(r),      s = '?';
    elseif r == 0,    s = 'open';
    elseif r >= 1e6,  s = sprintf('%g M', r / 1e6);
    else,             s = sprintf('%g k', r / 1e3);
    end
end
