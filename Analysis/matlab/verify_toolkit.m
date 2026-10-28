function fails = verify_toolkit()
%VERIFY_TOOLKIT  Prove the toolkit against evidence that came from elsewhere.
%
%   VERIFY_TOOLKIT runs every check and prints a pass/fail line for each.
%   FAILS = VERIFY_TOOLKIT() also returns the number that failed, so it can
%   be used from a build:  matlab -batch "exit(verify_toolkit())"
%
%   The checks that matter are the ones whose expected value was NOT produced
%   by this code:
%
%     * TI's own GUI analysed the same recording and wrote its answer into
%       Device_Analysis_*.csv. Its Histogram Analysis block gives a mean and
%       a peak-to-peak per channel, which yields R with no peak detection at
%       all. If this toolkit lands on that number, the agreement is between
%       two independent implementations, not between a test and its subject.
%
%     * The same signal is on disk in four formats - our JSON, our CSV, the
%       EVM's codes and the EVM's volts. All four must give one answer, or
%       a reader is mis-scaling something.
%
%     * Swapping RED and IR is the one error that produces a plausible
%       number instead of a crash, so the guard against it is tested by
%       actually doing it.
%
%   See also AFE4490_LOAD, AFE4490_ANALYZE.

    here = fileparts(mfilename('fullpath'));
    if isempty(here), here = pwd; end
    addpath(here);
    S = fullfile(here, '..', 'samples');

    fails = 0;
    fprintf('\n  AFE4490 analysis toolkit - verification\n');
    fprintf('  %s\n', repmat('-', 1, 68));

    % ---- 1. four formats, one answer --------------------------------
    files = { fullfile(S, 'capture_demo_500Hz.json'), 'studio-json'
              fullfile(S, 'capture_demo_500Hz.csv'),  'studio-csv'
              fullfile(S, 'EVM_Codes_sample.csv'),    'evm-csv'
              fullfile(S, 'EVM_Volts_sample.csv'),    'evm-csv' };
    got = nan(size(files, 1), 3);
    for k = 1:size(files, 1)
        if exist(files{k, 1}, 'file') ~= 2
            fails = report(fails, false, sprintf('missing sample %s', files{k, 1}), '');
            continue;
        end
        w = warning('off', 'all');
        A = afe4490_analyze(afe4490_load(files{k, 1}, 'Fs', 500));
        warning(w);
        got(k, :) = [A.spo2 A.R A.pr];
    end
    spread = max(got(:, 1)) - min(got(:, 1));
    fails = report(fails, spread < 0.01, ...
        'four file formats agree on SpO2', ...
        sprintf('spread %.4f %% across json/csv/evm-codes/evm-volts', spread));

    % ---- 2. against TI's own analysis --------------------------------
    oracle = fullfile(S, 'EVM_Analysis_sample.csv');
    if exist(oracle, 'file') == 2
        [rOracle, spOracle] = tiOracle(oracle);
        mine = got(1, 1);
        fails = report(fails, abs(mine - spOracle) < 0.5, ...
            'agrees with TI''s GUI on the same recording', ...
            sprintf('TI %.2f %% (R %.4f), this toolkit %.2f %% (R %.4f), diff %.2f pp', ...
                    spOracle, rOracle, mine, got(1, 2), mine - spOracle));
    else
        fails = report(fails, false, 'TI oracle file present', oracle);
    end

    % ---- 3. the conversion the whole thing rests on ------------------
    w = warning('off', 'all');
    Sc = afe4490_load(fullfile(S, 'EVM_Codes_sample.csv'), 'Fs', 500);
    Sv = afe4490_load(fullfile(S, 'EVM_Volts_sample.csv'), 'Fs', 500);
    warning(w);
    n = min(size(Sc.volts, 1), size(Sv.volts, 1));
    err = max(max(abs(Sc.volts(1:n, :) - Sv.volts(1:n, :))));
    fails = report(fails, err < 1e-9, ...
        'codes * 1.2/2^21 reproduces the EVM''s own volts', ...
        sprintf('largest disagreement %.3g V over %d samples x 6 channels', err, n));

    % ---- 4. the register header means something ----------------------
    w = warning('off', 'all');
    Sj = afe4490_load(fullfile(S, 'capture_demo_500Hz.json'));
    warning(w);
    okPrf = isfield(Sj.cfg, 'prf') && abs(Sj.cfg.prf - 500) < 0.01;
    fails = report(fails, okPrf, 'PRPCOUNT in the capture header decodes to 500 Hz', ...
        sprintf('PRPCOUNT %d -> %.4f Hz, %d averages, RED %.2f mA, TX_REF %.2f V', ...
                Sj.cfg.prpcount, Sj.cfg.prf, Sj.cfg.numav, Sj.cfg.led2_mA, Sj.cfg.txref_v));

    % ---- 5. the silent failure is not silent -------------------------
    Sw = Sj;
    Sw.volts(:, [5 6]) = Sw.volts(:, [6 5]);
    Sw.codes(:, [5 6]) = Sw.codes(:, [6 5]);
    w = warning('off', 'all');
    B = afe4490_analyze(Sw);
    warning(w);
    caught = any(~cellfun(@isempty, strfind(lower(B.warnings), 'wrong way round'))) || ...
             any(~cellfun(@isempty, strfind(lower(B.warnings), 'channel assignment')));
    fails = report(fails, caught, 'a RED/IR swap is caught, not reported as a saturation', ...
        sprintf('swapped gives SpO2 %.1f %% (R %.3f) and %d warning(s)', ...
                B.spo2, B.R, numel(B.warnings)));

    % ---- 6. the lab's own method lands in the same place -------------
    w = warning('off', 'all');
    Lb = afe4490_analyze(Sj, 'Method', 'lab', 'Delta', 'lab');
    warning(w);
    fails = report(fails, abs(Lb.spo2 - got(1, 1)) < 1.0, ...
        'the lab''s peakdet method agrees with the beat method', ...
        sprintf('lab %.2f %%, beat %.2f %%, diff %.2f pp', ...
                Lb.spo2, got(1, 1), Lb.spo2 - got(1, 1)));

    % ---- 7. the Excel block is the shape the workbook expects --------
    w = warning('off', 'all');
    A = afe4490_analyze(Sj);
    out = afe4490_excel(A, 'Out', fullfile(tempdir, 'afe_verify'), 'Verbose', false);
    warning(w);
    okShape = size(out.manual, 2) == 5 && size(out.matlab, 2) == 14 && ...
              size(out.manual, 1) == 6;
    fails = report(fails, okShape, 'Excel blocks are 6x5 (Manual) and 6x14 (Matlab)', ...
        sprintf('got %dx%d and %dx%d', size(out.manual), size(out.matlab)));

    % Algorithm 1 inside the block must satisfy the workbook's own formulas.
    AC = out.matlab(:, 1); DC = out.matlab(:, 2);
    ACi = out.matlab(:, 3); DCi = out.matlab(:, 4);
    Rc = (AC ./ DC) ./ (ACi ./ DCi);
    errR = max(abs(Rc - out.matlab(:, 7)));
    errS = max(abs((110 - 25 * out.matlab(:, 7)) - out.matlab(:, 8)));
    fails = report(fails, errR < 1e-12 && errS < 1e-10, ...
        'R and SpO2 in the block match Excel''s own formulas', ...
        sprintf('max |dR| %.2g, max |dSpO2| %.2g', errR, errS));

    fprintf('  %s\n', repmat('-', 1, 68));
    if fails == 0
        fprintf('  all checks passed\n\n');
    else
        fprintf('  %d CHECK(S) FAILED\n\n', fails);
    end
end

% ======================================================================
function [R, spo2] = tiOracle(path)
%TIORACLE  R from TI's own Histogram Analysis block - no peak detection.
%   The GUI reports a mean and a peak-to-peak per channel over the whole
%   record, and AC/DC is exactly Vpp/Mean. That is a complete, independent
%   route to R that shares no code with this toolkit.
    txt = fileread(path);
    i0 = strfind(txt, 'Histogram Analysis');
    i1 = strfind(txt, 'ADC Register Settings');
    blk = txt(i0(1):i1(1));
    rows = strsplit(blk, sprintf('\n'));
    mean_ = containers.Map(); pp = containers.Map();
    for k = 1:numel(rows)
        p = strsplit(strtrim(rows{k}), ',');
        if numel(p) < 4, continue; end
        name = strtrim(p{1});
        if isempty(name) || strcmp(name, 'Mean (V)'), continue; end
        m = str2double(strrep(p{2}, 'E', 'e'));
        v = str2double(strrep(p{4}, 'E', 'e'));
        if isfinite(m) && isfinite(v)
            mean_(name) = m; pp(name) = v;
        end
    end
    red = 'LED 2 (RED) - LED 2 (RED) AMBIENT';
    ir  = 'LED 1 (IR) - LED 1 (IR) AMBIENT';
    R = (pp(red) / mean_(red)) / (pp(ir) / mean_(ir));
    spo2 = 110 - 25 * R;
end

function fails = report(fails, ok, name, detail)
    if ok
        fprintf('  PASS  %-52s %s\n', name, detail);
    else
        fprintf('  FAIL  %-52s %s\n', name, detail);
        fails = fails + 1;
    end
end
