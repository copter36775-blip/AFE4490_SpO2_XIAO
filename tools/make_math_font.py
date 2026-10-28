"""Rebuild the embedded maths font in web/afe4490_studio_V2.html.

The Math view is set in Latin Modern Math — the OpenType descendant of
Computer Modern, the typeface every LaTeX document uses. It carries the
OpenType MATH table, which is what lets the browser position fraction rules,
radicals and sub/superscripts the way TeX does. Cambria Math is on every
Windows machine and is a perfectly good maths font, but it is not Computer
Modern, and that difference is the whole point of asking for the LaTeX look.

The font is subset to the codepoints the page can actually ask for and
embedded as base64, so the console stays a single file that works from a
file:// URL on a machine with nothing installed. Full font 733 kB, subset
about 43 kB.

Only <mi>, <mn> and <mo> are set in the maths font — <mtext> is deliberately
the UI sans — so the glyph set is small.

    python tools/make_math_font.py            # download, subset, embed
    python tools/make_math_font.py --check    # report what is embedded now

Needs: fonttools, brotli (pip install fonttools brotli), and network access
the first time. Latin Modern Math is under the GUST Font License; the licence
requires a modified copy to be renamed, which is why the embedded family is
"AFE Math" rather than "Latin Modern Math".
"""
import base64
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = ROOT / "web" / "afe4490_studio_V2.html"
URL = "https://mirrors.ctan.org/fonts/lm-math/opentype/latinmodern-math.otf"

# ASCII, Greek, and the operators and units the formulas use. Generous on
# purpose: a missing glyph falls back to a different font mid-formula, which
# looks far worse than a few kB of unused outlines.
EXTRA = [0x2009, 0x2013, 0x2014, 0x2192, 0x27F6, 0x2217, 0x2212, 0x22C5,
         0x2211, 0x221A, 0x221E, 0x2248, 0x2264, 0x2265, 0x00B5, 0x00B7,
         0x00B0, 0x00D7, 0x00B1, 0x03A9]


def codepoints():
    used = set(range(0x20, 0x7F))
    used |= set(range(0x391, 0x3CA))          # Greek upper and lower
    used |= set(EXTRA)
    html = PAGE.read_text(encoding="utf-8")
    for m in re.finditer(r"<(mi|mn|mo)\b[^>]*>(.*?)</\1>", html, re.S):
        body = m.group(2)
        for e in re.finditer(r"&#x([0-9A-Fa-f]+);", body):
            used.add(int(e.group(1), 16))
        for ch in re.sub(r"&#x[0-9A-Fa-f]+;", "", body):
            if ch.strip():
                used.add(ord(ch))
    return sorted(used)


def check():
    html = PAGE.read_text(encoding="utf-8")
    m = re.search(r'@font-face\{\s*font-family:"AFE Math";\s*'
                  r'src:url\(data:font/woff2;base64,([A-Za-z0-9+/=]+)\)', html)
    if not m:
        print("no embedded maths font found")
        return 1
    raw = base64.b64decode(m.group(1))
    print("embedded font: %d bytes woff2 (%d bytes base64)" % (len(raw), len(m.group(1))))
    try:
        from fontTools.ttLib import TTFont
        with tempfile.TemporaryDirectory() as td:
            f = pathlib.Path(td) / "f.woff2"
            f.write_bytes(raw)
            font = TTFont(str(f))
            print("tables:", " ".join(sorted(t for t in font.keys() if t != "GlyphOrder")))
            print("MATH table:", "present" if "MATH" in font else "MISSING")
            if "MATH" in font:
                print("MathVariants:", "present" if font["MATH"].table.MathVariants else "absent")
            print("glyphs:", font["maxp"].numGlyphs)
            print("name:", font["name"].getDebugName(1))
    except ImportError:
        print("(install fonttools to inspect the tables)")
    return 0


def main():
    if "--check" in sys.argv:
        return check()

    uni = codepoints()
    print("glyph set: %d codepoints" % len(uni))

    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        src = tmp / "latinmodern-math.otf"
        print("downloading %s" % URL)
        with urllib.request.urlopen(URL, timeout=120) as r:
            src.write_bytes(r.read())
        print("  %d bytes" % src.stat().st_size)

        unifile = tmp / "unicodes.txt"
        unifile.write_text(",".join("U+%04X" % c for c in uni), encoding="ascii")
        out = tmp / "subset.woff2"
        cmd = [sys.executable, "-m", "fontTools.subset", str(src),
               "--unicodes-file=" + str(unifile), "--layout-features=*",
               "--no-hinting", "--flavor=woff2", "--output-file=" + str(out)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 or not out.exists():
            print("subset failed:\n" + (res.stderr or res.stdout))
            return 1
        print("  subset to %d bytes" % out.stat().st_size)

        from fontTools.ttLib import TTFont
        font = TTFont(str(out))
        if "MATH" not in font:
            print("subset lost the MATH table - refusing to embed")
            return 1

        b64 = base64.b64encode(out.read_bytes()).decode("ascii")

    html = PAGE.read_text(encoding="utf-8")
    new, n = re.subn(r'(src:url\(data:font/woff2;base64,)[A-Za-z0-9+/=]+(\))',
                     lambda m: m.group(1) + b64 + m.group(2), html, count=1)
    if not n:
        print("no @font-face to update - add one first")
        return 1
    PAGE.write_text(new, encoding="utf-8")
    print("embedded; page is now %d KB" % (len(new) // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
