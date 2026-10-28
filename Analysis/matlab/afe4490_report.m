function fig = afe4490_report(S, A, varargin)
%AFE4490_REPORT  Print the result and draw the figure that justifies it.
%
%   AFE4490_REPORT(S,A) prints a summary of the analysis A of recording S
%   and draws three panels: both plethysmograms with the detected extrema
%   marked, the per-beat SpO2 and pulse rate, and the IR spectrum.
%
%   FIG = AFE4490_REPORT(...) returns the figure handle.
%
%   Options:
%     'Figure'  true (default) - set false for the printout only
%     'Save'    a path prefix; writes <prefix>.png and <prefix>.fig
%
%   The marked extrema are the point of the figure. A SpO2 number is only
%   as good as the peaks it was measured from, and the one way to see that
%   the beat detector did something sensible is to look at where it put the
%   marks.
%
%   See also AFE4490_LOAD, AFE4490_ANALYZE, AFE4490_EXCEL.

    p = inputParser;
    p.addParameter('Figure', true, @islogical);
    p.addParameter('Save', '', @(x) ischar(x) || isstring(x));
    p.parse(varargin{:});
    o = p.Results;
    fig = [];

    ok = [A.beat.ok];
    line = repmat('-', 1, 72);

    fprintf('\n%s\n', line);
    fprintf(' AFE4490 pulse oximetry  -  %s\n', shortPath(S.file));
    fprintf('%s\n', line);
    fprintf('  source        %s, %d samples, %.2f s at %.4f Hz\n', ...
            S.format, S.n, A.dur, A.fs);
    if isfield(S.cfg, 'summary') && ~isempty(S.cfg.summary)
        fprintf('  hardware      %s\n', wrapAt(S.cfg.summary, 14, 72));
    end
    fprintf('  method        %s, delta %.4g (%s), SpO2 = %g - %g*R\n', ...
            A.method, A.delta, A.units, A.coeff(1), A.coeff(2));
    fprintf('\n');
    fprintf('  SpO2          %6.2f %%   (sd %.2f over %d of %d beats)\n', ...
            A.spo2, A.spo2_sd, A.nok, A.nbeat);
    fprintf('  R             %6.4f     (sd %.4f)\n', A.R, A.R_sd);
    if isfinite(A.spo2_log)
        fprintf('  SpO2 (log)    %6.2f %%   from R_log %.4f  [Algorithm 2]\n', ...
                A.spo2_log, A.R_log);
    else
        fprintf('  SpO2 (log)        --       Algorithm 2 undefined for this record\n');
    end
    fprintf('  pulse rate    %6.1f bpm  (sd %.1f)\n', A.pr, A.pr_sd);
    fprintf('  perfusion     %6.2f %%\n', A.pi);
    fprintf('\n');
    q = A.quality;
    fprintf('  headroom      RED %.1f %%FS, IR %.1f %%FS%s\n', ...
            q.headroom_red, q.headroom_ir, tern(q.clipping, '   *** CLIPPING ***', ''));
    fprintf('  SNR           RED %.1f dB, IR %.1f dB\n', q.snr_red, q.snr_ir);
    fprintf('  beat spread   period CV %.1f %%\n', 100 * q.period_cv);

    if ~isempty(A.warnings)
        fprintf('\n');
        for k = 1:numel(A.warnings)
            fprintf('  ! %s\n', wrapAt(A.warnings{k}, 4, 72));
        end
    end
    if ~isempty(S.markers)
        fprintf('\n  register writes during this capture:\n');
        for k = 1:numel(S.markers)
            fprintf('    %7.3f s  %s\n', S.markers(k).t, S.markers(k).label);
        end
    end
    fprintf('%s\n\n', line);

    if ~o.Figure
        return;
    end

    % ---- the figure ----------------------------------------------------
    fig = figure('Name', ['AFE4490  ' shortPath(S.file)], ...
                 'Color', 'w', 'Position', [80 80 1100 760]);

    t = (0:S.n - 1)' / A.fs;
    if numel(S.t) == S.n, t = S.t; end
    red = S.volts(:, 5);
    ir  = S.volts(:, 6);

    % RED
    ax1 = subplot(3, 2, 1);
    plot(t, red * 1e3, 'Color', [0.85 0.25 0.25]); grid on; hold on;
    markBeats(A, 'red', 1e3);
    ylabel('mV'); title('RED  (LED2 - ambient)');
    xlim([t(1) t(end)]);

    % IR
    ax2 = subplot(3, 2, 3);
    plot(t, ir * 1e3, 'Color', [0.35 0.35 0.80]); grid on; hold on;
    markBeats(A, 'ir', 1e3);
    ylabel('mV'); xlabel('time (s)'); title('IR  (LED1 - ambient)');
    xlim([t(1) t(end)]);
    linkaxes([ax1 ax2], 'x');

    % per-beat SpO2
    subplot(3, 2, 2);
    bt = [A.beat.t];
    sp = [A.beat.spo2];
    plot(bt(ok), sp(ok), 'o-', 'Color', [0.1 0.5 0.35], 'MarkerFaceColor', [0.1 0.5 0.35]);
    hold on; grid on;
    if any(~ok)
        plot(bt(~ok), sp(~ok), 'x', 'Color', [0.7 0.7 0.7]);
    end
    yline_(A.spo2, [0.1 0.5 0.35]);
    ylabel('SpO_2 (%)'); title(sprintf('per-beat SpO_2   median %.2f %%', A.spo2));

    % per-beat pulse rate
    subplot(3, 2, 4);
    pr = [A.beat.pr];
    plot(bt(ok), pr(ok), 'o-', 'Color', [0.55 0.35 0.75], 'MarkerFaceColor', [0.55 0.35 0.75]);
    hold on; grid on;
    yline_(A.pr, [0.55 0.35 0.75]);
    ylabel('bpm'); xlabel('time (s)');
    title(sprintf('per-beat pulse rate   median %.1f bpm', A.pr));

    % spectrum of the IR pleth
    subplot(3, 2, [5 6]);
    [f, mag] = spec(ir - mean(ir), A.fs);
    plot(f, mag, 'Color', [0.35 0.35 0.80]); grid on;
    xlim([0 min(10, A.fs / 2)]);
    xlabel('Hz'); ylabel('dB'); title('IR spectrum');
    hold on;
    if isfinite(A.pr)
        yl = ylim;
        plot([A.pr / 60 A.pr / 60], yl, '--', 'Color', [0.1 0.5 0.35]);
        text(A.pr / 60, yl(2), sprintf('  %.1f bpm', A.pr), ...
             'VerticalAlignment', 'top', 'Color', [0.1 0.5 0.35]);
    end

    if ~isempty(char(o.Save))
        base = char(o.Save);
        try
            print(fig, [base '.png'], '-dpng', '-r150');
            savefig(fig, [base '.fig']);
            fprintf('  saved %s.png and %s.fig\n\n', base, base);
        catch err
            warning('afe4490:report:save', 'Could not save: %s', err.message);
        end
    end
end

% ======================================================================
function markBeats(A, which, scale)
%MARKBEATS  Put the extrema the analysis actually used onto the trace.
    ok = [A.beat.ok];
    tM = [A.beat.(tern(strcmp(which, 'red'), 'tMAXred', 'tMAXir'))];
    vM = [A.beat.(tern(strcmp(which, 'red'), 'MAXred', 'MAXir'))] * scale;
    vm = [A.beat.(tern(strcmp(which, 'red'), 'MINred', 'MINir'))] * scale;
    % The minimum's position is not recorded per channel, so the marker goes
    % at the beat start - enough to show which beat it belongs to.
    tm = [A.beat.t];
    plot(tM(ok), vM(ok), 'v', 'MarkerSize', 5, 'MarkerFaceColor', [0.1 0.5 0.35], ...
         'MarkerEdgeColor', 'none');
    plot(tm(ok), vm(ok), '^', 'MarkerSize', 5, 'MarkerFaceColor', [0.9 0.6 0.1], ...
         'MarkerEdgeColor', 'none');
    if any(~ok)
        plot(tM(~ok), vM(~ok), 'x', 'Color', [0.7 0.7 0.7], 'MarkerSize', 6);
    end
end

function [f, mag] = spec(x, fs)
    x = x(isfinite(x));
    n = 2 ^ floor(log2(max(64, numel(x))));
    x = x(1:min(n, numel(x)));
    n = numel(x);
    w = 0.5 - 0.5 * cos(2 * pi * (0:n - 1)' / (n - 1));    % Hann
    X = fft(x .* w);
    half = floor(n / 2);
    mag = 20 * log10(abs(X(1:half)) / max(abs(X(2:half))) + eps);
    f = (0:half - 1)' * fs / n;
end

function yline_(y, col)
    xl = xlim;
    plot(xl, [y y], '--', 'Color', col);
end

function s = tern(c, a, b)
    if c, s = a; else, s = b; end
end

function s = shortPath(p)
    [~, n, e] = fileparts(p);
    s = [n e];
end

function out = wrapAt(s, indent, width)
%WRAPAT  Wrap a long message so a terminal does not scramble it.
    words = strsplit(s, ' ');
    out = '';
    lineLen = indent;
    pad = repmat(' ', 1, indent);
    for k = 1:numel(words)
        w = words{k};
        if lineLen + numel(w) + 1 > width && lineLen > indent
            out = [out sprintf('\n%s', pad)];                        %#ok<AGROW>
            lineLen = indent;
        end
        out = [out w ' '];                                           %#ok<AGROW>
        lineLen = lineLen + numel(w) + 1;
    end
    out = strtrim(out);
end
