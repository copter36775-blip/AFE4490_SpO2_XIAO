"""Structural check on a console page's inline JavaScript, without a browser.

The selftests are the real verification, but they need Chrome, and Chrome needs
CPU that a loaded machine may not have. This catches the class of damage a bad
patch actually does - an unbalanced bracket, a helper defined twice, a name used
but never defined - in about a second, from Python alone.

It is a bracket scanner that understands strings, regex literals, comments and
template literals - including `${...}` expressions that hold their own strings,
regexes and nested templates - not a parser: it will not find a type error and
does not try.

Run:  python tools/js_lint.py
      python tools/js_lint.py web/afe4490_studio_V2.html
"""
import io
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OPEN = {"(": ")", "[": "]", "{": "}"}
CLOSE = {v: k for k, v in OPEN.items()}
BS = chr(92)
TPL = "${"                      # stack sentinel: inside a template expression


def regex_can_start(js, i):
    """A '/' begins a regex where an operand is expected, never after one.

    Division follows a value - a name, a number, ')' or ']' - so looking
    back past whitespace is enough to tell `a / b` from `.replace(/"/g, x)`.
    """
    j = i - 1
    while j >= 0 and js[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return True
    if js[j] in "(,=:[!&|?{};+-*%<>~^":
        return True
    k = j
    while k >= 0 and (js[k].isalnum() or js[k] in "_$"):
        k -= 1
    return js[k + 1:j + 1] in ("return", "typeof", "case", "void", "in", "of")


def scan(js, label):
    """Walk the source counting brackets, skipping anything quoted."""
    stack = []
    i, n, line = 0, len(js), 1
    in_tpl = False

    def fail(msg):
        print("  %s: %s" % (label, msg))
        return False

    while i < n:
        c = js[i]
        if c == "\n":
            line += 1

        if in_tpl:
            # Template text: only an escape, the closing backtick or the
            # start of an expression mean anything here.
            if c == BS:
                i += 2
                continue
            if c == "`":
                in_tpl = False
                i += 1
                continue
            if c == "$" and i + 1 < n and js[i + 1] == "{":
                stack.append((TPL, line))
                in_tpl = False
                i += 2
                continue
            i += 1
            continue

        if c == "\n":
            i += 1
            continue
        if c == "/" and i + 1 < n and js[i + 1] == "/":
            while i < n and js[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and js[i + 1] == "*":
            j = js.find("*/", i + 2)
            line += js.count("\n", i, j if j > 0 else n)
            i = (j + 2) if j > 0 else n
            continue
        if c == "/" and regex_can_start(js, i):
            # A regex literal can hold quotes and brackets - /"/g, /[&<>"]/ -
            # that are neither strings nor code, so skip it whole.
            i += 1
            in_class = False
            while i < n and js[i] != "\n":
                ch = js[i]
                if ch == BS:
                    i += 2
                    continue
                if in_class:
                    in_class = ch != "]"
                elif ch == "[":
                    in_class = True
                elif ch == "/":
                    i += 1
                    break
                i += 1
            continue
        if c == "`":
            in_tpl = True
            i += 1
            continue
        if c in "\"'":
            quote = c
            i += 1
            while i < n:
                if js[i] == BS:
                    i += 2
                    continue
                if js[i] == "\n":
                    line += 1
                if js[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c in OPEN:
            stack.append((c, line))
            i += 1
            continue
        if c in CLOSE:
            if not stack:
                return fail("stray '%s' at line %d" % (c, line))
            o, ol = stack.pop()
            if o == TPL:
                if c != "}":
                    return fail("'%s' at line %d closes a ${ from line %d" % (c, line, ol))
                in_tpl = True               # back into the template text
                i += 1
                continue
            if o != CLOSE[c]:
                return fail("'%s' at line %d closes '%s' from line %d" % (c, line, o, ol))
            i += 1
            continue
        i += 1

    if in_tpl:
        return fail("template literal never closed")
    if stack:
        o, ol = stack[0]
        return fail("'%s' opened at line %d is never closed (%d open)" % (o, ol, len(stack)))
    return True


NAMES = [
    "PRESETS", "renderPresets", "renderAdvice", "applyPreset", "afeCfLabel",
    "renderRateBar", "syncSpsMenu", "buildSpsMenu", "exactPrf", "SPS_EXACT",
    "SPS_FAST", "applyRail", "setRail", "LINK_MAX_SPS", "calFit", "spo2FromR",
    "renderCal", "renderBattery", "refreshProfile", "showRate", "darkTest",
    "acStats", "checkRateBudget", "bleAutoBackoff", "i24",
    # V4 register lab
    "lab", "labV", "labC", "labCommit", "labDrc", "labGenTiming", "labInit",
    "labTick", "labMark", "labIngestMarks", "markPlay", "markIn", "markDone",
    "labRecordExternal", "labSeqOf", "labOnReset", "labOnShow", "labCapToggle",
    "labCapButton", "capBeginAround", "capExportJson", "capHint", "capT",
    "labBarState", "labDock", "labSub", "labRenderInsp", "labDrawTiming",
    "labMakeScope", "labMeters", "labVerify", "labExport",
]


def main():
    page = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "web/afe4490_studio_V3.html")
    src = io.open(page, encoding="utf-8").read()
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", src, re.S)
    print("%s: %d inline block(s), %d chars"
          % (page.name, len(blocks), sum(len(b) for b in blocks)))

    ok = True
    for k, b in enumerate(blocks):
        if not scan(b, "block %d" % k):
            ok = False
    print("  brackets:", "balanced" if ok else "BROKEN")

    js = "\n".join(blocks)
    problems = []
    for name in NAMES:
        defs = len(re.findall(r"\b(?:function|const|let|var|class)\s+" + re.escape(name) + r"\b", js))
        uses = len(re.findall(r"(?<![\w$.])" + re.escape(name) + r"\b", js))
        note = ""
        if uses and defs == 0:
            note = "  <- used but never declared"
            problems.append(name)
        elif defs > 1:
            note = "  <- declared %d times" % defs
            problems.append(name)
        if uses:
            print("  %-18s declared %d, used %3d%s" % (name, defs, uses, note))

    # Every id the script reaches for should exist in the markup, or the
    # handler silently does nothing - the failure mode that shipped a
    # collapsed capture grid once already.
    ids = set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', src))
    # Elements the script builds itself carry ids that never appear in the
    # markup, and looking one of those up afterwards is correct, not a typo.
    ids |= set(re.findall(r"""\.id\s*=\s*['"]([A-Za-z0-9_-]+)['"]""", js))
    wanted = set(re.findall(r"""\$\(['"]#([A-Za-z0-9_-]+)['"]\)""", js))
    wanted |= set(re.findall(r"""getElementById\(['"]([A-Za-z0-9_-]+)['"]\)""", js))
    missing = sorted(w for w in wanted if w not in ids)
    if missing:
        print("  ids referenced but not in the markup: " + ", ".join(missing))
        problems.extend(missing)
    else:
        print("  ids: all %d referenced ids exist" % len(wanted))

    if not ok or problems:
        print("\nFAIL")
        return 1
    print("\nPASS structural checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
