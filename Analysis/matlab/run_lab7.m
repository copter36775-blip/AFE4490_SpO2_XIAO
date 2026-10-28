%RUN_LAB7  Load a capture, analyse it, and produce everything Lab 7 needs.
%
%   This is the script to run. Point CAPTURE at the file the console saved,
%   press Run, and you get: the printed result, the figure, the two CSV
%   blocks for the workbook, and - if TEMPLATE points at the lab's .xlsx -
%   a filled copy of it.
%
%   The capture can be any of:
%     ..._capture.csv    AFE4490 Studio V4, Capture tab -> CSV
%     ..._capture.json   AFE4490 Studio V4, Capture tab -> JSON  (preferred:
%                        always ADC codes, carries the register map)
%     Device_Codes_*.csv TI AFE44x0SPO2EVM GUI log
%     PPG5.mat           the lab's own recordings
%
%   See also AFE4490_LOAD, AFE4490_ANALYZE, AFE4490_REPORT, AFE4490_EXCEL.

clc; clear; close all;

% Make sure this folder is on the path however the script was started.
here = fileparts(mfilename('fullpath'));
if isempty(here), here = pwd; end
addpath(here);

% ======================================================================
%  SETTINGS - change these
% ======================================================================

CAPTURE  = fullfile(here, '..', 'samples', 'capture_demo_500Hz.json');

% The lab's workbook. Leave empty to skip filling it and just get the CSVs.
TEMPLATE = fullfile(here, '..', 'excel', 'SpO2_Lab7_Template.xlsx');

% Sample rate. Our captures record their own, so this is only used for
% formats that do not: TI EVM logs, plain .txt, and .mat files.
FS_IF_UNKNOWN = 500;

% Which beats go into the workbook (it has room for six).
FIRST_BEAT = 1;
N_BEATS    = 6;

% SpO2 = A - B*R.  110 and 25 are the lab's coefficients, and they are a
% population average for somebody else's emitters and probe geometry. If you
% have calibrated this sensor against a simulator, put your own here.
CAL_A = 110;
CAL_B = 25;

% ======================================================================
%  RUN
% ======================================================================

S = afe4490_load(CAPTURE, 'Fs', FS_IF_UNKNOWN);

% Beat-windowed detection: RED and IR are measured over the same heartbeat,
% which is what makes their ratio mean anything.
A = afe4490_analyze(S, 'Method', 'beat', 'A', CAL_A, 'B', CAL_B);

afe4490_report(S, A);

% The lab's own method, for comparison. If the two disagree by much, look at
% the figure before believing either - it usually means beats were missed.
L = afe4490_analyze(S, 'Method', 'lab', 'Delta', 'auto', 'A', CAL_A, 'B', CAL_B);
fprintf('  cross-check   beat method %.2f %%   lab method %.2f %%   (%+.2f)\n\n', ...
        A.spo2, L.spo2, L.spo2 - A.spo2);

if exist(TEMPLATE, 'file') == 2
    out = afe4490_excel(A, 'Workbook', TEMPLATE, 'First', FIRST_BEAT, 'Beats', N_BEATS);
else
    out = afe4490_excel(A, 'First', FIRST_BEAT, 'Beats', N_BEATS);
    fprintf(['  (no workbook at %s - wrote the CSV blocks only)\n\n'], TEMPLATE);
end
