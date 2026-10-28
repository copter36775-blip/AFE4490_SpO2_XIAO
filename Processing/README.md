# Processing — the original lab package

`040313447_Lab7/` is the university Lab 7 package **exactly as received**.
Nothing in it has been edited, and nothing in this project writes to it.

`_backup_original/040313447_Lab7_ORIGINAL.zip` is a snapshot of everything the
lab authored — LabVIEW VIs, the MATLAB corpus, both Excel workbooks, the
example EVM logs — taken before any of this work began. TI's 332 MB
`AFE44x0SPO2EVM GUI v2_4` installer is deliberately not in the zip and not in
git: it is TI's redistributable, it is downloadable from ti.com, and it is
not ours to archive.

Our own work lives in `../Analysis/`. See `../Analysis/README.md`.

## What this package turned out to contain

- `AFE44x0SPO2EVM GUI v2_4/` — an NI installer, not the GUI. The TI payload is
  `Volume/bin/dp/data.cab`, which holds `Register Map 4490.xml`: all 49
  registers with addresses, bit fields, TI's prose descriptions and the EVM
  defaults. That file is the authoritative register model.
- `Example/Log Data/` — 15 CSVs in five kinds (Analysis, Codes, Volts, FFT,
  Histogram) over six fixed channels. Two of the three captures are
  byte-identical; there are only two distinct recordings.
- `MatLAB/` — the lab's scripts and 18 data files. `SpO2_Processing_MatLab.m`
  is the method `../Analysis/matlab` reimplements.
- `LabVIEW/`, `builds/` — the manual-processing VI and its built application.
- `Results Laboratory 7.xlsx` — the workbook. Thirty input cells drive it all.
