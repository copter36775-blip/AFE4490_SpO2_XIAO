function S = afe4490_load(filename, varargin)
%AFE4490_LOAD  Read a pulse-oximetry recording into one struct, whatever wrote it.
%
%   S = AFE4490_LOAD(FILE) sniffs the file and returns a struct with the six
%   AFE4490 result channels in ADC codes and in volts, the sample rate, the
%   register map the capture ran under, and any register-write markers.
%
%   S = AFE4490_LOAD(FILE,'Fs',500) forces the sample rate. Needed only for
%   formats that do not record it (TI EVM logs, plain .txt, .mat).
%
%   Formats understood
%   ------------------
%     studio-csv   AFE4490 Studio V4 capture CSV ('# AFE4490 capture ...')
%     studio-json  AFE4490 Studio V4 capture JSON (always ADC codes)
%     evm-csv      TI AFE44x0SPO2EVM  Device_Codes_*.csv / Device_Volts_*.csv
%     txt2         two columns, Red and IR, whitespace or comma separated
%                  (the lab's PPG.txt, spo2prim.txt, ppg*.txt)
%     mat          a .mat holding RedPPG and IRPPG (the lab's PPG*.mat)
%
%   Fields of S
%   -----------
%     S.codes    n-by-6 double, signed ADC codes
%     S.volts    n-by-6 double, codes * 1.2 / 2^21
%     S.chan     1-by-6 cellstr, the channel names in column order:
%                {'red','amb2','ir','amb1','redAmb','irAmb'}
%     S.red      S.volts(:,5)  LED2 - ambient, the RED plethysmogram
%     S.ir       S.volts(:,6)  LED1 - ambient, the IR  plethysmogram
%     S.t        n-by-1 seconds, honouring a mid-capture rate change
%     S.fs       scalar Hz (the dominant rate if the capture changed rate)
%     S.segs     rate segments, [first_sample fs] per row
%     S.markers  struct array of register writes: .sample .t .label
%     S.regs     containers.Map, 'XX' -> uint32, the register map if recorded
%     S.cfg      decoded settings from S.regs (see AFE4490_DECODE_REGS)
%     S.format   which reader was used
%     S.file     the path it came from
%     S.notes    cellstr of header lines kept verbatim
%
%   Why both codes and volts: the lab's Excel works in volts and so does TI's
%   GUI, but the converter is a 22-bit integer and codes are what it actually
%   produced. Every value here satisfies volts = codes * 1.2 / 2^21 exactly -
%   verified against the lab's own EVM logs, where every Device_Volts entry is
%   its Device_Codes entry times that constant to the last digit.
%
%   See also AFE4490_ANALYZE, AFE4490_REPORT, AFE4490_DECODE_REGS.

%   Part of the AFE4490_SpO2_XIAO analysis toolkit.

    p = inputParser;
    p.addParameter('Fs', [], @(x) isempty(x) || (isscalar(x) && x > 0));
    p.addParameter('Format', 'auto', @(x) ischar(x) || isstring(x));
    p.parse(varargin{:});
    opt = p.Results;

    if nargin < 1 || isempty(filename)
        error('afe4490:load:noFile', 'Give a file to load.');
    end
    filename = char(filename);
    if exist(filename, 'file') ~= 2
        error('afe4490:load:notFound', 'No such file: %s', filename);
    end

    fmt = char(opt.Format);
    if strcmpi(fmt, 'auto')
        fmt = sniff(filename);
    end

    S = struct('codes', [], 'volts', [], 'chan', {CHAN()}, 't', [], 'fs', [], ...
               'segs', [], 'markers', emptyMarker(), 'regs', containers.Map(), ...
               'cfg', struct(), 'format', fmt, 'file', filename, 'notes', {{}}, ...
               't0', '', 'armed', '', 'seq0', NaN);

    switch fmt
        case 'studio-csv',  S = readStudioCsv(S, filename);
        case 'studio-json', S = readStudioJson(S, filename);
        case 'evm-csv',     S = readEvmCsv(S, filename);
        case 'txt2',        S = readTxt2(S, filename);
        case 'mat',         S = readMat(S, filename);
        otherwise
            error('afe4490:load:format', 'Unknown format "%s".', fmt);
    end

    % A forced Fs always wins; otherwise fall back to 500 Hz, which is what
    % the EVM GUI and this lab have always run at, and say so out loud.
    if ~isempty(opt.Fs)
        S.fs = opt.Fs;
        S.segs = [0 opt.Fs];
    elseif isempty(S.fs)
        S.fs = 500;
        S.segs = [0 500];
        warning('afe4490:load:assumedFs', ...
            ['%s records no sample rate; assuming 500 Hz. Pass ''Fs'' if that is ' ...
             'wrong - every time, frequency and pulse-rate result scales with it.'], ...
            fmt);
    end

    S = finish(S);
end

% ======================================================================
function c = CHAN()
    c = {'red', 'amb2', 'ir', 'amb1', 'redAmb', 'irAmb'};
end

function m = emptyMarker()
    m = struct('sample', {}, 't', {}, 'label', {});
end

function S = finish(S)
%FINISH  Fill in whatever the reader left out, consistently.
    FS_CODE = 2097152;      % 2^21
    FS_VOLT = 1.2;
    if isempty(S.codes) && ~isempty(S.volts)
        S.codes = S.volts * (FS_CODE / FS_VOLT);
    elseif isempty(S.volts) && ~isempty(S.codes)
        S.volts = S.codes * (FS_VOLT / FS_CODE);
    end
    if isempty(S.codes)
        error('afe4490:load:empty', 'No samples were read from %s.', S.file);
    end

    n = size(S.codes, 1);
    if isempty(S.segs), S.segs = [0 S.fs]; end

    % Time is summed per rate segment. A capture whose PRF changed part-way
    % has no single sample period, and pretending it does puts every later
    % timestamp - and every pulse rate derived from one - quietly wrong.
    S.t = zeros(n, 1);
    tAcc = 0;
    for k = 1:size(S.segs, 1)
        i0 = S.segs(k, 1) + 1;                        % stored 0-based
        if k < size(S.segs, 1), i1 = S.segs(k + 1, 1); else, i1 = n; end
        i1 = min(i1, n);
        if i1 < i0, continue; end
        step = 1 / S.segs(k, 2);
        S.t(i0:i1) = tAcc + (0:(i1 - i0))' * step;
        tAcc = S.t(i1) + step;
    end
    if size(S.segs, 1) > 1
        warning('afe4490:load:multiRate', ...
            ['%s changed sample rate %d time(s) mid-capture. Time is summed per ' ...
             'segment; a spectrum over the whole record is not meaningful.'], ...
            S.file, size(S.segs, 1) - 1);
    end

    S.red = S.volts(:, 5);      % LED2 - ambient
    S.ir  = S.volts(:, 6);      % LED1 - ambient
    S.n   = n;
    S.dur = S.t(end) + 1 / S.fs;
    if S.regs.Count > 0
        S.cfg = afe4490_decode_regs(S.regs);
        if isfield(S.cfg, 'prf') && ~isempty(S.cfg.prf) && isfinite(S.cfg.prf)
            % PRPCOUNT is the rate the hardware actually ran at. If the file's
            % own idea of fs disagrees by more than rounding, the file is wrong.
            expect = S.cfg.prf / max(1, S.cfg.decimation);
            if abs(expect - S.fs) > max(0.5, 0.01 * S.fs)
                warning('afe4490:load:rateMismatch', ...
                    ['Header says %.4f Hz but PRPCOUNT=%d with decimation %d gives ' ...
                     '%.4f Hz. Using the header; check the capture.'], ...
                    S.fs, S.cfg.prpcount, S.cfg.decimation, expect);
            end
        end
    end
end

% ======================================================================
function fmt = sniff(filename)
%SNIFF  Decide the format from the first few lines, not from the extension.
    [~, ~, ext] = fileparts(filename);
    if strcmpi(ext, '.mat'), fmt = 'mat'; return; end

    fid = fopen(filename, 'r');
    if fid < 0, error('afe4490:load:open', 'Cannot open %s', filename); end
    cleanup = onCleanup(@() fclose(fid));                            %#ok<NASGU>
    head = '';
    for k = 1:12
        l = fgetl(fid);
        if ~ischar(l), break; end
        head = [head l sprintf('\n')];                               %#ok<AGROW>
    end

    % Two of the lab's own files - PPG.txt and PPGsone.TXT - are MAT-files
    % that someone saved with a .txt extension. They announce themselves in
    % their first line, so believe the content over the name.
    if strncmp(head, 'MATLAB ', 7) && contains(head(1:min(80, end)), 'MAT-file')
        fmt = 'mat'; return;
    end

    if contains(head, '# AFE4490 capture')
        fmt = 'studio-csv'; return;
    end
    s = strtrim(head);
    if ~isempty(s) && s(1) == '{'
        fmt = 'studio-json'; return;
    end
    if contains(head, 'Ch :') || contains(head, 'Ch:')
        fmt = 'evm-csv'; return;
    end
    if strcmpi(ext, '.json'), fmt = 'studio-json'; return; end
    fmt = 'txt2';
end

% ======================================================================
function S = readStudioCsv(S, filename)
%READSTUDIOCSV  AFE4490 Studio V4 capture CSV.
%
%   The header is a block of '# ' lines carrying the whole provenance - the
%   rate segments, the firmware, every register, every marker - then one
%   column header, then data with a trailing text column for marker labels.
%   The units are whatever the Capture tab was showing, so they are read
%   from the header rather than assumed.
    fid = fopen(filename, 'r');
    cleanup = onCleanup(@() fclose(fid));                            %#ok<NASGU>

    units = 'code';
    segs = [];
    markers = emptyMarker();
    regs = containers.Map();
    notes = {};
    colLine = '';

    while true
        l = fgetl(fid);
        if ~ischar(l), break; end
        if isempty(l), continue; end
        if l(1) ~= '#'
            colLine = l;        % the 'sample,t_s,...' header
            break;
        end
        notes{end + 1} = l;                                          %#ok<AGROW>
        body = strtrim(l(2:end));

        tok = regexp(body, 'units=(\S+)', 'tokens', 'once');
        if ~isempty(tok), units = tok{1}; end

        if strncmpi(body, 't0 ', 3)
            % Wall clock of sample 0. Distinct from 'armed', which is when
            % the operator pressed Capture - for a snapshot every sample
            % predates that, so the two are not interchangeable.
            tk = regexp(body, '^t0\s+(\S+)', 'tokens', 'once');
            if ~isempty(tk), S.t0 = tk{1}; end
        elseif strncmpi(body, 'armed ', 6)
            tk = regexp(body, '^armed\s+(\S+)', 'tokens', 'once');
            if ~isempty(tk), S.armed = tk{1}; end
        elseif strncmpi(body, 'seq0 ', 5)
            % The firmware's own index for sample 0, which is what joins a
            % capture to the register write log (whose entries carry seq).
            tk = regexp(body, '^seq0\s+(\d+)', 'tokens', 'once');
            if ~isempty(tk), S.seq0 = str2double(tk{1}); end
        elseif strncmpi(body, 'rate segments', 13)
            pairs = regexp(body, '(\d+)@([\d.eE+-]+)', 'tokens');
            for k = 1:numel(pairs)
                segs(end + 1, :) = [str2double(pairs{k}{1}), ...
                                    str2double(pairs{k}{2})];        %#ok<AGROW>
            end
        elseif strncmpi(body, 'register', 8)
            tk = regexp(body, '^register\s+([0-9A-Fa-f]{2})=([0-9A-Fa-f]{6})', ...
                        'tokens', 'once');
            if ~isempty(tk)
                regs(upper(tk{1})) = uint32(hex2dec(tk{2}));
            end
        elseif strncmpi(body, 'marker', 6)
            tk = regexp(body, '^marker sample\s+(\d+)\s+t=([\d.eE+-]+)\s*s\s*(.*)$', ...
                        'tokens', 'once');
            if ~isempty(tk)
                markers(end + 1) = struct('sample', str2double(tk{1}), ...
                                          't', str2double(tk{2}), ...
                                          'label', strtrim(tk{3}));  %#ok<AGROW>
            end
        end
    end

    if isempty(colLine)
        error('afe4490:load:studioCsv', '%s has a header but no data.', filename);
    end
    cols = strsplit(strtrim(colLine), ',');
    idx = zeros(1, 6);
    for k = 1:6
        j = find(strcmpi(cols, S.chan{k}), 1);
        if isempty(j)
            error('afe4490:load:studioCsv', ...
                  'Column "%s" is missing from %s. Columns were: %s', ...
                  S.chan{k}, filename, strjoin(cols, ','));
        end
        idx(k) = j;
    end

    % The last column holds marker text, so every field is read as a string
    % and converted. A numeric textscan spec would stop at the first label.
    nc = numel(cols);
    spec = repmat('%s', 1, nc);
    C = textscan(fid, spec, 'Delimiter', ',', 'EndOfLine', sprintf('\n'));
    if isempty(C) || isempty(C{1})
        error('afe4490:load:studioCsv', '%s has no data rows.', filename);
    end
    n = numel(C{1});
    raw = zeros(n, 6);
    for k = 1:6
        raw(:, k) = str2double(C{idx(k)});
    end
    bad = any(~isfinite(raw), 2);
    if any(bad)
        warning('afe4490:load:nonFinite', ...
            'Dropped %d of %d rows of %s that did not parse as numbers.', ...
            sum(bad), n, filename);
        raw = raw(~bad, :);
    end

    switch lower(strtrim(units))
        case {'v', 'volt', 'volts'}
            S.volts = raw;
        case {'code', 'codes', 'adc_code'}
            S.codes = raw;
        otherwise
            % 'uA' means the console scaled by the TIA gain on the way out,
            % which is not invertible without trusting the same registers.
            % Refuse rather than silently treat microamps as volts.
            error('afe4490:load:units', ...
                ['%s was exported with units=%s. Re-export it from the Capture ' ...
                 'tab with Units set to Volts or ADC codes - microamps cannot be ' ...
                 'turned back into either without assuming the gain.'], ...
                filename, units);
    end

    S.segs = segs;
    if ~isempty(segs), S.fs = dominantRate(segs, size(raw, 1)); end
    S.markers = markers;
    S.regs = regs;
    S.notes = notes;
end

% ======================================================================
function S = readStudioJson(S, filename)
%READSTUDIOJSON  AFE4490 Studio V4 capture JSON - always ADC codes.
    J = jsondecode(fileread(filename));

    if ~isfield(J, 'channels')
        error('afe4490:load:json', '%s has no "channels" object.', filename);
    end
    n = numel(J.channels.(S.chan{1}));
    C = zeros(n, 6);
    for k = 1:6
        if ~isfield(J.channels, S.chan{k})
            error('afe4490:load:json', 'Channel "%s" missing from %s.', ...
                  S.chan{k}, filename);
        end
        v = J.channels.(S.chan{k});
        if numel(v) ~= n
            error('afe4490:load:json', ...
                  'Channel "%s" has %d samples, expected %d.', ...
                  S.chan{k}, numel(v), n);
        end
        C(:, k) = double(v(:));
    end

    if isfield(J, 'units') && ~strcmpi(J.units, 'adc_code')
        warning('afe4490:load:json', ...
            '%s claims units=%s; this reader expects adc_code.', filename, J.units);
    end
    S.codes = C;

    if isfield(J, 'rate_segments') && ~isempty(J.rate_segments)
        rs = J.rate_segments;
        if isstruct(rs)
            S.segs = [vertcat(rs.i), vertcat(rs.rate)];
        elseif iscell(rs)
            S.segs = cell2mat(cellfun(@(s) [s.i s.rate], rs(:), ...
                                      'UniformOutput', false));
        end
        S.fs = dominantRate(S.segs, n);
    end

    if isfield(J, 'registers') && isstruct(J.registers)
        f = fieldnames(J.registers);
        for k = 1:numel(f)
            % jsondecode prefixes a name starting with a digit with 'x':
            % the key "1D" arrives as the field x1D.
            name = regexprep(f{k}, '^x', '');
            S.regs(upper(name)) = uint32(hex2dec(J.registers.(f{k})));
        end
    end
    if isfield(J, 'markers') && ~isempty(J.markers)
        mk = J.markers;
        if isstruct(mk)
            for k = 1:numel(mk)
                S.markers(end + 1) = struct('sample', mk(k).sample, ...
                                            't', mk(k).t_s, ...
                                            'label', char(mk(k).label)); %#ok<AGROW>
            end
        elseif iscell(mk)
            for k = 1:numel(mk)
                S.markers(end + 1) = struct('sample', mk{k}.sample, ...
                                            't', mk{k}.t_s, ...
                                            'label', char(mk{k}.label)); %#ok<AGROW>
            end
        end
    end
    if isfield(J, 't0'),    S.t0    = char(J.t0);    end
    if isfield(J, 'armed'), S.armed = char(J.armed); end
    if isfield(J, 'seq0') && ~isempty(J.seq0) && isnumeric(J.seq0), S.seq0 = double(J.seq0); end
    if isfield(J, 'source') && isstruct(J.source)
        S.notes{end + 1} = ['source: ' jsonencode(J.source)];
    end
end

% ======================================================================
function S = readEvmCsv(S, filename)
%READEVMCSV  TI AFE44x0SPO2EVM GUI log, Device_Codes_*.csv or Device_Volts_*.csv.
%
%   Five preamble lines, a 'Codes'/'Volts' marker, a 'Ch : ...' header naming
%   the same six channels in the same order this project uses, then data with
%   a trailing comma - which must not be read as a seventh column.
    fid = fopen(filename, 'r');
    cleanup = onCleanup(@() fclose(fid));                            %#ok<NASGU>

    kind = '';
    notes = {};
    while true
        l = fgetl(fid);
        if ~ischar(l)
            error('afe4490:load:evm', ...
                  '%s ended before any "Ch :" header.', filename);
        end
        s = strtrim(l);
        if strcmpi(s, 'Codes'), kind = 'codes'; end
        if strcmpi(s, 'Volts'), kind = 'volts'; end
        if ~isempty(s), notes{end + 1} = s; end                      %#ok<AGROW>
        if strncmp(s, 'Ch :', 4) || strncmp(s, 'Ch:', 3), break; end
    end
    if isempty(kind)
        error('afe4490:load:evm', ...
              ['%s has no Codes/Volts marker - is it a Device_FFT, ' ...
               'Device_Histogram or Device_Analysis log?'], filename);
    end

    C = textscan(fid, '%f%f%f%f%f%f%*[^\n]', 'Delimiter', ',', ...
                 'EndOfLine', sprintf('\n'));
    raw = [C{1} C{2} C{3} C{4} C{5} C{6}];
    raw = raw(all(isfinite(raw), 2), :);

    if strcmp(kind, 'codes'), S.codes = raw; else, S.volts = raw; end
    S.notes = notes;
    % The EVM GUI does not log its own sample rate. finish() will warn and
    % assume 500 Hz unless the caller passed Fs.
end

% ======================================================================
function S = readTxt2(S, filename)
%READTXT2  The lab's plain two-column exports: Red then IR, one row per sample.
    raw = [];
    try
        raw = load(filename);                 % whitespace or tab separated
    catch
        raw = [];
    end
    if isempty(raw) || size(raw, 2) < 2
        try
            if exist('readmatrix', 'file') == 2
                raw = readmatrix(filename);
            else
                raw = dlmread(filename, ',');                        %#ok<DLMRD>
            end
        catch
            raw = [];
        end
    end
    if isempty(raw) || size(raw, 2) < 2
        error('afe4490:load:txt2', ...
              ['%s is not two or more numeric columns. If it is one of our ' ...
               'captures it has lost its header.'], filename);
    end
    if size(raw, 2) > 2
        warning('afe4490:load:txt2', ...
            '%s has %d columns; using the first two as Red and IR.', ...
            filename, size(raw, 2));
    end

    red = raw(:, 1);
    ir  = raw(:, 2);
    n = numel(red);
    % Only the subtracted pair exists here. The raw and ambient columns are
    % left as NaN rather than faked, so anything that needs them fails loudly.
    V = nan(n, 6);
    V(:, 5) = red;
    V(:, 6) = ir;
    S = assignByScale(S, V);
    S.notes = {'two-column file: col 1 -> Red (LED2-amb), col 2 -> IR (LED1-amb)'};
end

% ======================================================================
function S = readMat(S, filename)
%READMAT  The lab's PPG*.mat files, which hold RedPPG and IRPPG.
%
%   The extension cannot be trusted in this folder: PPG.txt and PPGsone.TXT
%   are binary MAT-files, and PPG231.mat is plain ASCII. So '-mat' is forced
%   (load() otherwise dispatches on the name and refuses a MAT-file called
%   .txt), and a file that turns out not to be one falls through to the
%   two-column reader instead of erroring.
    try
        M = load(filename, '-mat');
    catch err
        if contains(err.message, 'MAT-file') || contains(err.message, 'ASCII')
            S.format = 'txt2';
            S = readTxt2(S, filename);
            return;
        end
        rethrow(err);
    end
    f = fieldnames(M);
    % The lab named these differently in every file it ever saved: RedPPG,
    % REDPPG, PPGRed, and IRPPG, PPGIR. Match on what the name contains,
    % not on an exact spelling, but only among variables that are actually
    % numeric vectors - so a heart-sound .mat is still refused.
    vec = f(cellfun(@(n) isnumeric(M.(n)) && isvector(M.(n)) && numel(M.(n)) > 8, f));
    redName = pickContaining(vec, 'red');
    irName  = pickContaining(setdiff(vec, {redName}, 'stable'), 'ir');
    if isempty(redName) || isempty(irName)
        error('afe4490:load:mat', ...
              ['%s holds {%s} - expected a RED and an IR vector (RedPPG/IRPPG, ' ...
               'PPGRed/PPGIR or similar).'], ...
              filename, strjoin(reshape(f, 1, []), ', '));
    end
    red = double(M.(redName)(:));
    ir  = double(M.(irName)(:));
    n = min(numel(red), numel(ir));
    if numel(red) ~= numel(ir)
        warning('afe4490:load:mat', ...
            '%s: RedPPG has %d samples, IRPPG %d. Using the first %d of each.', ...
            filename, numel(red), numel(ir), n);
    end
    V = nan(n, 6);
    V(:, 5) = red(1:n);
    V(:, 6) = ir(1:n);
    S = assignByScale(S, V);
    S.notes = {[redName ' -> Red, ' irName ' -> IR']};
end

function S = assignByScale(S, V)
%ASSIGNBYSCALE  Volts or codes? The ADC spans +/-1.2 V, so anything much
%   larger than that is counts, not volts. Files that carry no units leave
%   no better evidence than their own magnitude.
    m = max(abs(V(isfinite(V))));
    if isempty(m), m = 0; end
    if m > 2
        S.codes = V;
    else
        S.volts = V;
    end
end

function name = pickContaining(have, needle)
%PICKCONTAINING  The first variable whose name contains NEEDLE, ignoring case.
%   'ir' is checked as a whole word-ish fragment so that it does not match
%   the 'ir' hiding inside some unrelated name before the real one is seen.
    name = '';
    low = lower(have);
    j = find(strcmp(low, [needle 'ppg']) | strcmp(low, ['ppg' needle]), 1);
    if isempty(j)
        j = find(contains(low, needle), 1);
    end
    if ~isempty(j), name = have{j}; end
end

% ======================================================================
function fs = dominantRate(segs, n)
%DOMINANTRATE  The rate most of the record ran at.
%   One number has to stand for the capture in spectra and in the Fs field;
%   the segment covering the most samples is the least wrong choice.
    if isempty(segs), fs = []; return; end
    if size(segs, 1) == 1, fs = segs(1, 2); return; end
    len = diff([segs(:, 1); n]);
    [~, k] = max(len);
    fs = segs(k, 2);
end
