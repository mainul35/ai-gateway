"""Finds control characters that got into the source by accident.

    python scripts/check_control_chars.py app utils scripts

A shell heredoc will quietly eat the backslash of a `\\b`, and what lands in the file is a single
backspace byte. The regex around it still compiles, and it matches nothing it was written to match:
no error, no warning, just a filter that never fires. Two of them sat in this repository for weeks -
the check that keeps an address out of a place name, and the one that tells a screenshot offered as
evidence from one offered for editing - and both were only found because something downstream of
them was being looked at for another reason.

Nothing in this project needs a bell, a backspace, a form feed or an escape character in its source,
so their presence is always the same mistake. Run from bin/deploy.sh before anything is sent.
"""
import pathlib
import sys

# Every control character except tab, newline and carriage return, which are ordinary whitespace
NAMED = {0: r"\0 null", 7: r"\a bell", 8: r"\b backspace", 11: r"\v vertical tab",
         12: r"\f form feed", 27: "escape"}
SUFFIXES = (".py", ".html", ".css", ".js", ".yaml", ".yml", ".txt", ".sh", ".properties")


def suspect(code):
    return code < 32 and code not in (9, 10, 13)


def check(paths):
    found = []
    for root in paths:
        root = pathlib.Path(root)
        files = [root] if root.is_file() else [p for p in root.rglob("*") if p.suffix in SUFFIXES]
        for path in files:
            if "__pycache__" in path.parts:
                continue
            raw = path.read_bytes()
            for index, code in enumerate(raw):
                if suspect(code):
                    line = raw[:index].count(b"\n") + 1
                    what = NAMED.get(code, f"0x{code:02x}")
                    # The line as printed, with the offender made visible
                    start = raw.rfind(b"\n", 0, index) + 1
                    end = raw.find(b"\n", index)
                    context = raw[start:end if end != -1 else len(raw)]
                    shown = context.replace(bytes([code]), b"<" + what.encode() + b">")
                    found.append(f"{path}:{line}: {what}\n    "
                                 f"{shown.decode('utf-8', 'replace').strip()[:150]}")
    return found


paths = sys.argv[1:] or ["app", "utils", "scripts"]
problems = check(paths)
for problem in problems:
    print(problem)
if problems:
    print(f"\n{len(problems)} control character(s) in the source. If one of these should be a regular "
          f"expression escape - \\b, \\f, \\v - it was eaten by a shell; write the file with an editor "
          f"rather than a heredoc.")
    sys.exit(1)
print(f"check_control_chars: none in {', '.join(paths)}")
