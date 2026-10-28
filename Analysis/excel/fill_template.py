"""Fill the Lab 7 workbook from the blocks afe4490_excel.m produced.

MATLAB can write straight into the workbook and usually does. This exists for
the two cases where that is not enough: a machine without Excel registered,
where MATLAB's own writer may not preserve the sheet's formulas, and anyone
who wants to go from a capture to a filled workbook without opening MATLAB at
all. openpyxl rewrites only the cells it is given and leaves every formula
untouched, which is the property that matters here - the workbook IS the
calculation, and all thirty input cells drive it.

Usage
-----
  python fill_template.py <prefix>_Manual_D4.csv [--template T.xlsx] [--out O.xlsx]

  The matching <prefix>_Matlab_D5.csv is picked up automatically if it is
  beside the first one.

What it writes
--------------
  Sheet "Manual", D4:H9   Max Red, Min Red, Max IR, Min IR, time of IR max
  Sheet "Matlab", D5:Q10  the computed per-beat block

Everything else on both sheets is left exactly as the lab wrote it.
"""
import argparse
import csv
import pathlib
import shutil
import sys

try:
    import openpyxl
except ImportError:
    sys.exit("openpyxl is required:  pip install openpyxl")

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_TEMPLATE = HERE / "SpO2_Lab7_Template.xlsx"

MANUAL_AT = ("Manual", 4, 4)      # sheet, first row, first column (D = 4)
MATLAB_AT = ("Matlab", 5, 4)
MANUAL_COLS = 5
MATLAB_COLS = 14
MAX_ROWS = 6                      # the sheet has room for six beats


def read_block(path, want_cols):
    """Read one of afe4490_excel.m's CSV blocks: a '#' note, a header, rows."""
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for rec in csv.reader(f):
            if not rec or (rec[0].startswith("#")):
                continue
            # The header line is the one whose first field is not a number.
            try:
                float(rec[0])
            except ValueError:
                continue
            vals = []
            for x in rec[:want_cols]:
                x = x.strip()
                vals.append(None if x == "" else float(x))
            while len(vals) < want_cols:
                vals.append(None)
            rows.append(vals)
    if not rows:
        sys.exit(f"{path}: no numeric rows found - is this one of the "
                 f"*_Manual_D4.csv / *_Matlab_D5.csv blocks?")
    return rows


def write_block(ws, at, rows, ncols, label):
    _, r0, c0 = at
    if len(rows) > MAX_ROWS:
        print(f"  note: {label} has {len(rows)} beats; the sheet holds "
              f"{MAX_ROWS}, writing the first {MAX_ROWS}")
        rows = rows[:MAX_ROWS]
    for i, row in enumerate(rows):
        for j in range(ncols):
            ws.cell(row=r0 + i, column=c0 + j).value = row[j] if j < len(row) else None
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manual_csv", help="the *_Manual_D4.csv block")
    ap.add_argument("--matlab-csv", default=None,
                    help="the *_Matlab_D5.csv block (found automatically if omitted)")
    ap.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    man_path = pathlib.Path(a.manual_csv)
    if not man_path.exists():
        sys.exit(f"no such file: {man_path}")

    mat_path = pathlib.Path(a.matlab_csv) if a.matlab_csv else None
    if mat_path is None:
        guess = pathlib.Path(str(man_path).replace("_Manual_D4.csv", "_Matlab_D5.csv"))
        mat_path = guess if guess.exists() else None

    template = pathlib.Path(a.template)
    if not template.exists():
        sys.exit(f"no template at {template}")

    out = pathlib.Path(a.out) if a.out else \
        man_path.with_name(man_path.name.replace("_Manual_D4.csv", "_Results.xlsx"))

    shutil.copy(template, out)
    wb = openpyxl.load_workbook(out)

    n = write_block(wb[MANUAL_AT[0]], MANUAL_AT,
                    read_block(man_path, MANUAL_COLS), MANUAL_COLS, "Manual")
    print(f"  Manual  D4:H{3 + n}   {n} beats from {man_path.name}")

    if mat_path and mat_path.exists():
        m = write_block(wb[MATLAB_AT[0]], MATLAB_AT,
                        read_block(mat_path, MATLAB_COLS), MATLAB_COLS, "Matlab")
        print(f"  Matlab  D5:Q{4 + m}   {m} beats from {mat_path.name}")
    else:
        print("  Matlab  sheet left empty (no *_Matlab_D5.csv alongside)")

    wb.save(out)
    print(f"\n  wrote {out}")
    print("  Open it in Excel - every formula recalculates from the thirty "
          "cells on Manual.")


if __name__ == "__main__":
    main()
