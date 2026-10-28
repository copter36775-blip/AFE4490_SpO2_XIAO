function out = afe4490_excel(A, varargin)
%AFE4490_EXCEL  Produce the numbers the Lab 7 workbook expects.
%
%   OUT = AFE4490_EXCEL(A) takes an analysis from AFE4490_ANALYZE and
%   returns the two blocks the lab's "Results Laboratory 7.xlsx" is built
%   around, writes them as CSV, and - if a workbook is given - fills them
%   straight into a copy of it.
%
%   OUT = AFE4490_EXCEL(A,'Name',Value,...):
%     'Beats'     how many beats to report (default 6, what the sheet holds)
%     'First'     start at this accepted beat (default 1)
%     'Workbook'  path to the lab .xlsx template; a filled copy is written
%     'Out'       output prefix (default the capture name beside it)
%     'Verbose'   true (default) prints the block it wrote
%
%   What goes where
%   ---------------
%   Sheet "Manual" is driven entirely by thirty cells, D4:H9 - six beats of
%   Max Red, Min Red, Max IR, Min IR and the time of the IR maximum. Every
%   other number on that sheet, both SpO2 algorithms, the pulse rate and the
%   perfusion index, is an Excel formula over those five columns. So that is
%   all this writes, and the workbook stays exactly the lab's.
%
%   Sheet "Matlab" is a paste of computed values, D5:Q10:
%     ACred DCred ACir DCir | R_abs IR_abs R SpO2 | R_abs IR_abs R SpO2 | PR PI
%     \____ Algorithm 1 ____________________/   \__ Algorithm 2 __/
%
%   OUT has fields .manual (6x5), .matlab (6x14), .csv_manual, .csv_matlab
%   and .workbook (the copy written, if any).
%
%   See also AFE4490_ANALYZE, AFE4490_REPORT.

    p = inputParser;
    p.addParameter('Beats', 6, @(x) isscalar(x) && x >= 1);
    p.addParameter('First', 1, @(x) isscalar(x) && x >= 1);
    p.addParameter('Workbook', '', @(x) ischar(x) || isstring(x));
    p.addParameter('Out', '', @(x) ischar(x) || isstring(x));
    p.addParameter('Verbose', true, @islogical);
    p.parse(varargin{:});
    o = p.Results;

    % ---- choose the beats ------------------------------------------------
    good = find([A.beat.ok]);
    if isempty(good)
        error('afe4490:excel:noBeats', 'No accepted beats to report.');
    end
    if o.First > numel(good)
        error('afe4490:excel:first', ...
              'First=%d but only %d accepted beats exist.', o.First, numel(good));
    end
    pick = good(o.First:min(numel(good), o.First + o.Beats - 1));
    nb = numel(pick);
    if nb < o.Beats
        warning('afe4490:excel:short', ...
            ['Only %d accepted beat(s) available from beat %d; the sheet has ' ...
             'room for %d. The remaining rows are left blank.'], nb, o.First, o.Beats);
    end
    B = A.beat(pick);

    % ---- the Manual sheet inputs, D4:H9 ----------------------------------
    % Volts, because that is what the lab's workbook and TI's GUI both show.
    manual = [ [B.MAXred]' [B.MINred]' [B.MAXir]' [B.MINir]' [B.tMAXir]' ];

    % ---- the Matlab sheet values, D5:Q10 ---------------------------------
    matlab = [ [B.ACred]'  [B.DCred]' [B.ACir]'  [B.DCir]' ...
               [B.Ared]'   [B.Air]'   [B.R]'     [B.spo2]' ...
               logAbs(B, 'red') logAbs(B, 'ir') [B.R_log]' [B.spo2_log]' ...
               [B.pr]'     [B.PI]' ];

    out = struct('manual', manual, 'matlab', matlab, ...
                 'beats', pick, 'csv_manual', '', 'csv_matlab', '', 'workbook', '');

    % ---- write the CSV blocks --------------------------------------------
    prefix = char(o.Out);
    if isempty(prefix)
        [d, n] = fileparts(A.file);
        if isempty(d), d = pwd; end
        prefix = fullfile(d, [n '_lab7']);
    end

    out.csv_manual = [prefix '_Manual_D4.csv'];
    writeCsv(out.csv_manual, ...
             {'Max_Red_V', 'Min_Red_V', 'Max_IR_V', 'Min_IR_V', 'Max_IR_Time_s'}, ...
             manual, ...
             sprintf(['# paste into sheet "Manual", cell D4  (%s, %s, SpO2 = %g - %g*R)'], ...
                     shortName(A.file), A.method, A.coeff(1), A.coeff(2)));

    out.csv_matlab = [prefix '_Matlab_D5.csv'];
    writeCsv(out.csv_matlab, ...
             {'ACred', 'DCred', 'ACir', 'DCir', ...
              'R_Absorption', 'IR_Absorption', 'R', 'SpO2', ...
              'R_Absorption_log', 'IR_Absorption_log', 'R_log', 'SpO2_log', ...
              'PR_bpm', 'PI_pct'}, ...
             matlab, ...
             sprintf('# paste into sheet "Matlab", cell D5  (%s)', shortName(A.file)));

    % ---- fill a copy of the workbook -------------------------------------
    wb = char(o.Workbook);
    if ~isempty(wb)
        if exist(wb, 'file') ~= 2
            error('afe4490:excel:noWorkbook', 'No such workbook: %s', wb);
        end
        [~, wn, we] = fileparts(wb);
        dest = [prefix '_' wn we];
        copyfile(wb, dest, 'f');
        try
            % 'inplace' leaves every other cell - and every formula - alone.
            writematrix(manual, dest, 'Sheet', 'Manual', 'Range', 'D4', ...
                        'WriteMode', 'inplace', 'AutoFitWidth', false);
            writematrix(matlab, dest, 'Sheet', 'Matlab', 'Range', 'D5', ...
                        'WriteMode', 'inplace', 'AutoFitWidth', false);
            out.workbook = dest;
        catch err
            warning('afe4490:excel:writeFailed', ...
                ['Could not write into %s (%s). The CSV blocks are still there - ' ...
                 'open them and paste, or run Analysis/excel/fill_template.py.'], ...
                dest, err.message);
        end
    end

    % ---- say what happened ------------------------------------------------
    if o.Verbose
        fprintf('\n  Lab 7 workbook blocks from %s\n', shortName(A.file));
        fprintf('  beats %s of %d accepted\n', mat2str(pick), numel(good));
        fprintf('\n  Manual sheet, paste at D4:\n');
        fprintf('    %-12s %-12s %-12s %-12s %-12s\n', ...
                'Max Red', 'Min Red', 'Max IR', 'Min IR', 'Max IR t');
        for k = 1:nb
            fprintf('    %-12.6f %-12.6f %-12.6f %-12.6f %-12.4f\n', manual(k, :));
        end
        fprintf('\n  Matlab sheet, paste at D5  (first 8 of 14 columns):\n');
        fprintf('    %-11s %-11s %-11s %-11s %-11s %-11s %-9s %-7s\n', ...
                'ACred', 'DCred', 'ACir', 'DCir', 'R_abs', 'IR_abs', 'R', 'SpO2');
        for k = 1:nb
            fprintf('    %-11.6f %-11.6f %-11.6f %-11.6f %-11.6f %-11.6f %-9.5f %-7.2f\n', ...
                    matlab(k, 1:8));
        end
        fprintf('\n  wrote %s\n        %s\n', out.csv_manual, out.csv_matlab);
        if ~isempty(out.workbook)
            fprintf('        %s\n', out.workbook);
        end
        fprintf('\n');
    end
end

% ======================================================================
function v = logAbs(B, which)
%LOGABS  Algorithm 2's absorbance, log(max/min), per beat.
%   Undefined where a channel touches or crosses zero; NaN rather than a
%   complex number, so a bad beat shows up as blank in Excel instead of
%   poisoning an average.
    if strcmp(which, 'red')
        hi = [B.MAXred]'; lo = [B.MINred]';
    else
        hi = [B.MAXir]';  lo = [B.MINir]';
    end
    v = nan(size(hi));
    ok = hi > 0 & lo > 0;
    v(ok) = log(hi(ok) ./ lo(ok));
end

function writeCsv(path, header, M, note)
    fid = fopen(path, 'w');
    if fid < 0
        error('afe4490:excel:open', 'Cannot write %s', path);
    end
    c = onCleanup(@() fclose(fid));                                  %#ok<NASGU>
    fprintf(fid, '%s\n', note);
    fprintf(fid, '%s\n', strjoin(header, ','));
    for r = 1:size(M, 1)
        row = M(r, :);
        s = cell(1, numel(row));
        for k = 1:numel(row)
            if isfinite(row(k))
                s{k} = sprintf('%.10g', row(k));
            else
                s{k} = '';          % Excel reads an empty field as blank
            end
        end
        fprintf(fid, '%s\n', strjoin(s, ','));
    end
end

function s = shortName(p)
    [~, n, e] = fileparts(p);
    s = [n e];
end
