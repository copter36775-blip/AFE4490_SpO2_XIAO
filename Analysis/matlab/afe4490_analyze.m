function A = afe4490_analyze(S, varargin)
%AFE4490_ANALYZE  Beat-by-beat SpO2, pulse rate and perfusion index.
%
%   A = AFE4490_ANALYZE(S) takes the struct from AFE4490_LOAD and returns a
%   per-beat table of results plus robust summary values.
%
%   A = AFE4490_ANALYZE(S,'Name',Value,...) accepts:
%
%     'Method'   'beat' (default) or 'lab'
%                'beat' finds each heartbeat on the IR channel and takes the
%                       maximum and minimum of each raw channel inside that
%                       beat. RED and IR are therefore measured over the same
%                       beat, which is what makes R meaningful.
%                'lab'  reproduces Processing/.../SpO2_Processing_MatLab.m
%                       exactly: peakdet on each channel independently, then
%                       MAX(k) paired with MIN(k). Kept so the new numbers can
%                       be checked against the ones the lab script produced.
%     'Delta'    peak-detection threshold, in the working units.
%                'auto' (default) scales it to the signal; 'lab' uses the
%                0.0003 the lab script hard-codes; or give a number.
%     'Units'    'volts' (default) or 'codes'. Only changes the numbers'
%                scale - R, SpO2, PR and PI are all ratios and come out the
%                same either way.
%     'A','B'    SpO2 = A - B*R. Default 110 and 25, the lab's coefficients.
%     'Channels' 'subtracted' (default) uses LED-minus-ambient (columns 5,6);
%                'raw' uses the LED-on columns (1,3) without ambient removal.
%     'Reject'   true (default) flags implausible beats instead of letting
%                them drag the average.
%
%   Fields of A
%   -----------
%     A.beat      per-beat struct array / table columns:
%                 .n .t .ACred .DCred .ACir .DCir .R .spo2 .R_log .spo2_log
%                 .PI .period .pr .ok .why
%     A.spo2      median SpO2 over accepted beats  (the number to quote)
%     A.spo2_sd   standard deviation over accepted beats
%     A.R         median R
%     A.pr        median pulse rate, bpm
%     A.pi        median perfusion index, %
%     A.nbeat     beats found / A.nok accepted
%     A.fs        sample rate used
%     A.method    which method ran
%     A.delta     the threshold actually used
%     A.quality   struct of signal-quality figures (see below)
%     A.warnings  cellstr of anything the caller should know
%
%   Two algorithms are computed for every beat:
%     Algorithm 1 (ratio of ratios)  R     = (ACred/DCred) / (ACir/DCir)
%     Algorithm 2 (log ratio)        R_log = log(MAXred/MINred) / log(MAXir/MINir)
%   The lab's own script defines Algorithm 2 and then never uses it - its
%   R2 and SpO2_2 are both computed from Algorithm 1's variables. Here both
%   are real, so they can be compared.
%
%   On sign: more blood means more absorption, so systole is a trough in
%   this data, not a peak. It makes no difference to R - AC is max minus min
%   and DC is their mean, and both are unchanged if the waveform is flipped.
%
%   See also AFE4490_LOAD, AFE4490_REPORT, AFE4490_EXCEL, PEAKDET.

    p = inputParser;
    p.addParameter('Method', 'beat', @(x) any(strcmpi(x, {'beat', 'lab'})));
    p.addParameter('Delta', 'auto');
    p.addParameter('Units', 'volts', @(x) any(strcmpi(x, {'volts', 'codes'})));
    p.addParameter('A', 110, @isscalar);
    p.addParameter('B', 25, @isscalar);
    p.addParameter('Channels', 'subtracted', ...
                   @(x) any(strcmpi(x, {'subtracted', 'raw'})));
    p.addParameter('Reject', true, @islogical);
    p.parse(varargin{:});
    o = p.Results;

    warnings = {};

    % ---- pick the two working signals --------------------------------
    if strcmpi(o.Units, 'codes'), D = S.codes; else, D = S.volts; end
    if strcmpi(o.Channels, 'raw')
        red = D(:, 1);  ir = D(:, 3);
        warnings{end + 1} = ['Using the raw LED-on channels: ambient light is ' ...
                             'still in both DC levels, so R is biased.'];
    else
        red = D(:, 5);  ir = D(:, 6);
    end
    if all(~isfinite(red)) || all(~isfinite(ir))
        error('afe4490:analyze:noData', ...
              ['The chosen channels are empty in %s. A two-column .txt or .mat ' ...
               'only carries the subtracted pair, so Channels=''raw'' cannot work.'], ...
              S.file);
    end
    ok = isfinite(red) & isfinite(ir);
    if ~all(ok)
        warnings{end + 1} = sprintf('%d non-finite sample(s) dropped.', sum(~ok));
        red = red(ok);  ir = ir(ok);
    end

    fs = S.fs;
    n  = numel(ir);
    if n < round(2 * fs)
        error('afe4490:analyze:tooShort', ...
              'Only %.2f s of data at %.4f Hz - need at least 2 s to find a beat.', ...
              n / fs, fs);
    end

    % ---- how big is a beat? ------------------------------------------
    % Everything downstream needs a threshold, and a fixed one cannot serve
    % both a 9 mV finger trace and a signal in ADC codes. Measure the beat
    % amplitude first, from a band-limited copy, and scale the threshold to
    % what is actually there.
    irAC  = bandpass_ppg(ir,  fs);
    redAC = bandpass_ppg(red, fs);
    amp = robustAmp(irAC);
    if amp <= 0
        error('afe4490:analyze:flat', ...
              'The IR channel has no pulsatile component at all - is a finger on the sensor?');
    end

    if ischar(o.Delta) || isstring(o.Delta)
        switch lower(char(o.Delta))
            case 'auto'
                delta = 0.25 * amp;
            case 'lab'
                delta = 0.0003;
                if strcmpi(o.Units, 'codes')
                    delta = 0.0003 * 2097152 / 1.2;   % the same threshold, in codes
                end
            otherwise
                error('afe4490:analyze:delta', ...
                      'Delta must be ''auto'', ''lab'' or a number.');
        end
    else
        delta = double(o.Delta);
    end
    if delta <= 0
        error('afe4490:analyze:delta', 'Delta must be positive.');
    end
    if delta > 0.9 * amp
        warnings{end + 1} = sprintf(...
            ['Delta %.3g is close to the beat amplitude %.3g, so beats will be ' ...
             'missed. Try Delta=''auto''.'], delta, amp);
    end

    % ---- find the beats ----------------------------------------------
    switch lower(o.Method)
        case 'beat'
            [beats, bw] = beatMethod(red, ir, irAC, fs, delta, amp);
        case 'lab'
            [beats, bw] = labMethod(red, ir, delta, fs);
    end
    warnings = [warnings, bw];

    if isempty(beats)
        error('afe4490:analyze:noBeats', ...
              ['No beats were found with delta %.4g. The record is %.2f s at ' ...
               '%.4f Hz and the IR beat amplitude measures %.4g.'], ...
              delta, n / fs, fs, amp);
    end

    % ---- per-beat arithmetic -----------------------------------------
    nb = numel(beats);
    A = struct();
    f = @(name) arrayfun(@(b) b.(name), beats);
    ACred = f('ACred');  DCred = f('DCred');
    ACir  = f('ACir');   DCir  = f('DCir');

    % Algorithm 1: ratio of ratios.
    Rabs_red = ACred ./ DCred;
    Rabs_ir  = ACir  ./ DCir;
    R = Rabs_red ./ Rabs_ir;

    % Algorithm 2: log ratio. Only defined while both extremes are the same
    % sign and non-zero - a channel that crosses zero has no log ratio, and
    % saying so beats returning a complex number.
    MAXr = f('MAXred'); MINr = f('MINred');
    MAXi = f('MAXir');  MINi = f('MINir');
    logOk = MAXr > 0 & MINr > 0 & MAXi > 0 & MINi > 0;
    R_log = nan(size(R));
    R_log(logOk) = log(MAXr(logOk) ./ MINr(logOk)) ./ log(MAXi(logOk) ./ MINi(logOk));
    if ~any(logOk)
        warnings{end + 1} = ['Algorithm 2 (log ratio) is undefined for this record: ' ...
                             'a channel reaches zero or goes negative. Use Algorithm 1.'];
    end

    spo2     = o.A - o.B * R;
    spo2_log = o.A - o.B * R_log;
    PI = 100 * Rabs_ir;
    period = f('period');
    pr = 60 ./ period;

    % ---- which beats to believe ---------------------------------------
    good = true(1, nb);
    why = repmat({''}, 1, nb);
    if o.Reject
        bad = ~isfinite(R) | R <= 0 | R > 3;
        good(bad) = false;  [why{bad}] = deal('R out of range');

        bad = isfinite(period) & (period < 0.25 | period > 2.5);   % 24..240 bpm
        good(bad) = false;  [why{bad}] = deal('implausible beat interval');

        bad = ~isfinite(DCir) | DCir == 0 | ~isfinite(DCred) | DCred == 0;
        good(bad) = false;  [why{bad}] = deal('zero DC');

        % A beat whose amplitude is far from the record's typical beat is a
        % motion artefact or a detection error, not a pulse.
        medAC = median(ACir(isfinite(ACir)));
        if isfinite(medAC) && medAC > 0
            bad = ACir < 0.25 * medAC | ACir > 4 * medAC;
            good(bad) = false;  [why{bad}] = deal('beat amplitude outlier');
        end
    end
    nok = sum(good);
    if nok == 0
        warnings{end + 1} = ['Every beat was rejected; the summary falls back to ' ...
                             'all beats. Treat the numbers as unreliable.'];
        good = true(1, nb);
        nok = nb;
    end

    % ---- assemble ------------------------------------------------------
    A.beat = struct('n', num2cell(1:nb), ...
                    't', num2cell(f('t') / fs), ...
                    'MAXred', num2cell(MAXr), 'MINred', num2cell(MINr), ...
                    'MAXir',  num2cell(MAXi), 'MINir',  num2cell(MINi), ...
                    'tMAXir', num2cell(f('tMAXir') / fs), ...
                    'ACred', num2cell(ACred), 'DCred', num2cell(DCred), ...
                    'ACir',  num2cell(ACir),  'DCir',  num2cell(DCir), ...
                    'Ared', num2cell(Rabs_red), 'Air', num2cell(Rabs_ir), ...
                    'R', num2cell(R), 'spo2', num2cell(spo2), ...
                    'R_log', num2cell(R_log), 'spo2_log', num2cell(spo2_log), ...
                    'PI', num2cell(PI), ...
                    'period', num2cell(period), 'pr', num2cell(pr), ...
                    'ok', num2cell(good), 'why', why);

    sel = @(v) v(good & isfinite(v));
    A.spo2    = median(sel(spo2));
    A.spo2_sd = std(sel(spo2));
    A.spo2_log = median(sel(spo2_log));
    A.R       = median(sel(R));
    A.R_sd    = std(sel(R));
    A.R_log   = median(sel(R_log));
    A.pr      = median(sel(pr));
    A.pr_sd   = std(sel(pr));
    A.pi      = median(sel(PI));
    A.nbeat   = nb;
    A.nok     = nok;
    A.fs      = fs;
    A.dur     = n / fs;
    A.method  = lower(o.Method);
    A.delta   = delta;
    A.units   = lower(o.Units);
    A.coeff   = [o.A o.B];
    A.file    = S.file;

    % ---- signal quality --------------------------------------------------
    A.quality = quality(red, ir, redAC, irAC, fs, A);
    if A.quality.clipping
        warnings{end + 1} = ['The ADC is at or near full scale: the waveform is ' ...
                             'clipped and AC is wrong. Lower the LED current or RF.'];
    end
    if isfinite(A.pi) && A.pi < 0.2
        warnings{end + 1} = sprintf(...
            ['Perfusion index is %.2f %% - very weak. SpO2 from a signal this ' ...
             'small is dominated by noise.'], A.pi);
    end
    if nok < 5
        warnings{end + 1} = sprintf(...
            'Only %d usable beat(s); the median is not a stable estimate.', nok);
    end

    % ---- is RED really RED? ------------------------------------------
    % This is the one mistake that fails silently. Swap the two channels
    % and R becomes 1/R, which still produces a plausible-looking SpO2 with
    % no error and no crash - 95 % becomes 66 %. Every other thing that can
    % go wrong here is loud: a wrong sample rate moves the pulse rate but
    % cancels out of R, and a wrong volts scale cancels out of AC/DC too.
    %
    % Two independent signs that the assignment is right:
    %   * R lands in 0.4-1.0 for any saturation worth reporting.
    %   * IR carries more perfusion than RED. Measured 1.7-1.9x on both of
    %     the lab's own EVM captures, and it holds when the absolute
    %     amplitude ordering does not - RED is the larger channel in one of
    %     those captures and the smaller in the other.
    if isfinite(A.R) && (A.R > 1.2 || A.R < 0.2)
        warnings{end + 1} = sprintf(...
            ['R = %.3f is outside the 0.4-1.0 a real saturation produces. The ' ...
             'commonest cause is RED and IR being the wrong way round - in this ' ...
             'data LED2 is RED and LED1 is IR, which is the opposite of what the ' ...
             'numbering suggests. 1/R would give SpO2 %.1f %%.'], ...
            A.R, o.A - o.B / A.R);
    end
    piRed = median(sel(100 * Rabs_red));
    if isfinite(piRed) && isfinite(A.pi) && piRed > 0
        A.quality.pi_ratio = A.pi / piRed;
        if A.quality.pi_ratio < 0.8
            warnings{end + 1} = sprintf(...
                ['IR perfusion (%.2f %%) is below RED (%.2f %%). On this sensor IR ' ...
                 'normally carries 1.7-1.9x the pulsatility of RED, so check the ' ...
                 'channel assignment before quoting this SpO2.'], A.pi, piRed);
        end
    else
        A.quality.pi_ratio = NaN;
    end

    A.warnings = warnings;
end

% ======================================================================
function [beats, warns] = beatMethod(red, ir, irAC, fs, delta, amp)
%BEATMETHOD  One beat at a time, measured on both channels over the same span.
%
%   Beats are located on a band-limited copy of IR, which is the strongest
%   and cleanest channel, but every amplitude is then read off the RAW
%   signal - the DC level is the whole point of the ratio and a high-pass
%   filter would throw it away.
    warns = {};
    [MX, MN] = peakdet(irAC, 0.25 * amp);
    if size(MN, 1) < 2 && size(MX, 1) < 2
        beats = struct([]);
        warns{end + 1} = 'Fewer than two fiducials on the IR channel.';
        return;
    end

    % Trough-to-trough: a beat starts where the trace is at its most
    % absorbed and ends at the next such point.
    if size(MN, 1) >= 2
        marks = MN(:, 1);
    else
        marks = MX(:, 1);
    end
    marks = sort(marks(:));

    % Drop spans shorter than 0.25 s (240 bpm) - those are dicrotic notches
    % picked up as beats, not beats.
    minSpan = max(2, round(0.25 * fs));
    keep = [true; diff(marks) >= minSpan];
    marks = marks(keep);
    if numel(marks) < 2
        beats = struct([]);
        warns{end + 1} = 'Only one beat boundary survived the minimum-interval check.';
        return;
    end

    nb = numel(marks) - 1;
    beats = repmat(emptyBeat(), 1, nb);
    for k = 1:nb
        i0 = marks(k);
        i1 = marks(k + 1);
        seg = i0:i1;
        [mxr, jr] = max(red(seg));  mnr = min(red(seg));
        [mxi, ji] = max(ir(seg));   mni = min(ir(seg));
        beats(k).t      = i0;
        beats(k).MAXred = mxr;  beats(k).MINred = mnr;
        beats(k).MAXir  = mxi;  beats(k).MINir  = mni;
        beats(k).tMAXir = seg(ji);
        beats(k).ACred  = mxr - mnr;
        beats(k).DCred  = (mxr + mnr) / 2;
        beats(k).ACir   = mxi - mni;
        beats(k).DCir   = (mxi + mni) / 2;
        beats(k).period = (i1 - i0) / fs;
        beats(k).tMAXred = seg(jr);
    end
end

% ======================================================================
function [beats, warns] = labMethod(red, ir, delta, fs)
%LABMETHOD  Exactly what SpO2_Processing_MatLab.m does, warts included.
%
%   peakdet runs on each channel independently and then MAX(k) is paired
%   with MIN(k). That is only right while both channels produce the same
%   number of extrema in the same order; when they do not - and on a noisy
%   record they often do not - RED beat k is silently compared against IR
%   beat k+1. The original script has no check for this at all, so the
%   check lives here and says so rather than quietly truncating.
    warns = {};
    [MAXR, MINR] = peakdet(red, delta);
    [MAXIR, MINIR] = peakdet(ir, delta);

    counts = [size(MAXR, 1) size(MINR, 1) size(MAXIR, 1) size(MINIR, 1)];
    k = min(counts);
    if k == 0
        beats = struct([]);
        warns{end + 1} = 'peakdet found no extrema on at least one channel.';
        return;
    end
    if numel(unique(counts)) > 1
        warns{end + 1} = sprintf(...
            ['Method ''lab'': peakdet returned %d/%d RED and %d/%d IR extrema. ' ...
             'The lab script pairs them by index regardless; truncated to %d ' ...
             'beats, but RED and IR may be one beat out of step. Method ''beat'' ' ...
             'does not have this problem.'], counts(1), counts(2), counts(3), counts(4), k);
    end

    beats = repmat(emptyBeat(), 1, k);
    for j = 1:k
        beats(j).t      = MAXIR(j, 1);
        beats(j).MAXred = MAXR(j, 2);   beats(j).MINred = MINR(j, 2);
        beats(j).MAXir  = MAXIR(j, 2);  beats(j).MINir  = MINIR(j, 2);
        beats(j).tMAXir = MAXIR(j, 1);
        beats(j).tMAXred = MAXR(j, 1);
        beats(j).ACred  = MAXR(j, 2) - MINR(j, 2);
        beats(j).DCred  = (MAXR(j, 2) + MINR(j, 2)) / 2;
        beats(j).ACir   = MAXIR(j, 2) - MINIR(j, 2);
        beats(j).DCir   = (MAXIR(j, 2) + MINIR(j, 2)) / 2;
    end
    % The lab takes the pulse period from successive IR maxima:
    %   Tn = diff(MAXIR(:,1))*Ts;  PR = 60./Tn;
    % Its last beat has no successor, so it has no period either.
    for j = 1:k
        if j < k
            beats(j).period = (MAXIR(j + 1, 1) - MAXIR(j, 1)) / fs;
        else
            beats(j).period = NaN;
        end
    end
end

function b = emptyBeat()
    b = struct('t', NaN, 'MAXred', NaN, 'MINred', NaN, 'MAXir', NaN, ...
               'MINir', NaN, 'tMAXir', NaN, 'tMAXred', NaN, 'ACred', NaN, ...
               'DCred', NaN, 'ACir', NaN, 'DCir', NaN, 'period', NaN);
end

% ======================================================================
function y = bandpass_ppg(x, fs)
%BANDPASS_PPG  0.5 - 5 Hz, the band a plethysmogram lives in.
%
%   Written out as two one-pole sections run forwards and backwards rather
%   than calling butter/filtfilt, so the toolkit needs no Signal Processing
%   Toolbox. Zero phase, so fiducials land where they really are.
    x = x(:);
    x = x - mean(x(isfinite(x)));
    x(~isfinite(x)) = 0;

    y = onepoleHP(x,  0.5, fs);
    y = flipud(onepoleHP(flipud(y), 0.5, fs));
    y = onepoleLP(y,  5.0, fs);
    y = flipud(onepoleLP(flipud(y), 5.0, fs));
end

function y = onepoleHP(x, fc, fs)
    a = exp(-2 * pi * fc / fs);
    y = zeros(size(x));
    prevX = x(1); prevY = 0;
    for i = 1:numel(x)
        prevY = a * (prevY + x(i) - prevX);
        prevX = x(i);
        y(i) = prevY;
    end
end

function y = onepoleLP(x, fc, fs)
    a = exp(-2 * pi * fc / fs);
    y = zeros(size(x));
    prevY = x(1);
    for i = 1:numel(x)
        prevY = a * prevY + (1 - a) * x(i);
        y(i) = prevY;
    end
end

function amp = robustAmp(x)
%ROBUSTAMP  Beat amplitude that a few artefacts cannot inflate.
    x = x(isfinite(x));
    if isempty(x), amp = 0; return; end
    amp = prctileish(x, 95) - prctileish(x, 5);
end

function v = prctileish(x, p)
%PRCTILEISH  A percentile without the Statistics Toolbox.
    x = sort(x(:));
    if isempty(x), v = NaN; return; end
    idx = max(1, min(numel(x), round(p / 100 * numel(x))));
    v = x(idx);
end

% ======================================================================
function q = quality(red, ir, redAC, irAC, fs, A)
%QUALITY  The figures that say whether the SpO2 above is worth quoting.
    FS_V = 1.2;
    q = struct();
    q.dc_red = mean(red(isfinite(red)));
    q.dc_ir  = mean(ir(isfinite(ir)));
    q.pp_red = max(red) - min(red);
    q.pp_ir  = max(ir)  - min(ir);

    if strcmp(A.units, 'volts')
        q.headroom_red = 100 * abs(q.dc_red) / FS_V;
        q.headroom_ir  = 100 * abs(q.dc_ir)  / FS_V;
        q.clipping = max(abs([red; ir])) > 0.98 * FS_V;
    else
        q.headroom_red = 100 * abs(q.dc_red) / 2097152;
        q.headroom_ir  = 100 * abs(q.dc_ir)  / 2097152;
        q.clipping = max(abs([red; ir])) > 0.98 * 2097152;
    end

    % Second-difference noise: a pleth is smooth on a sample-to-sample scale,
    % so what survives x[n] - 2x[n-1] + x[n-2] is almost entirely noise.
    q.noise_red = secondDiffNoise(red);
    q.noise_ir  = secondDiffNoise(ir);
    q.snr_red = 20 * log10(rms_(redAC) / max(q.noise_red, eps));
    q.snr_ir  = 20 * log10(rms_(irAC)  / max(q.noise_ir,  eps));

    % Beat-to-beat spread: a steady pulse gives a tight period.
    per = arrayfun(@(b) b.period, A.beat);
    per = per([A.beat.ok]);
    per = per(isfinite(per));
    if numel(per) > 1
        q.period_cv = std(per) / mean(per);
    else
        q.period_cv = NaN;
    end
    q.fs = fs;
end

function v = secondDiffNoise(x)
    x = x(isfinite(x));
    if numel(x) < 3, v = NaN; return; end
    d = x(3:end) - 2 * x(2:end-1) + x(1:end-2);
    v = sqrt(mean(d .^ 2) / 6);     % /6 undoes the operator's noise gain
end

function v = rms_(x)
    x = x(isfinite(x));
    if isempty(x), v = NaN; else, v = sqrt(mean(x .^ 2)); end
end
