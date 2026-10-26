#!/usr/bin/env python3
"""Every page's JavaScript must parse: one bad character stops a whole page from doing anything.

The playground went dead (2026-10-19) because a fix replaced a string's quotes with typographic ones:
'Uncaught SyntaxError: Invalid or unexpected token', and nothing on the page worked. Python is
compiled before a deploy; the pages were not checked at all.

Takes the inline <script> blocks of each template in app/templates, puts a placeholder where Jinja
fills something in, and asks node to parse the result. Without node it says so and passes: the check
runs where node is (this PC, the server), and the deploy runs it before anything is sent.

    python scripts/check_page_scripts.py [template ...]
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "app", "templates")
# Scripts the pages load as files: checked as they are. A syntax error in one (a line break written
# into a string, 2026-10-26) stopped the draft editor and the check, reading templates only, missed it
STATIC = os.path.join(ROOT, "app", "static")


def scripts_of(path):
    with open(path, encoding="utf-8") as f:
        page = f.read()
    code = "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page, re.S))
    code = re.sub(r"\{\{.*?\}\}", "0", code, flags=re.S)       # what Jinja fills in
    return re.sub(r"\{%.*?%\}", "", code, flags=re.S)


def main(paths):
    node = shutil.which("node")
    if not node:
        print("check_page_scripts: node is not installed here; page scripts not checked")
        return 0
    paths = paths or (sorted(os.path.join(TEMPLATES, f) for f in os.listdir(TEMPLATES) if f.endswith(".html"))
                      + sorted(os.path.join(STATIC, f) for f in os.listdir(STATIC) if f.endswith(".js")))
    broken = 0
    for path in paths:
        if path.endswith(".js"):
            with open(path, encoding="utf-8") as f:
                code = f.read()
        else:
            code = scripts_of(path)
        if not code.strip():
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(code)
        try:
            result = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
        finally:
            os.unlink(f.name)
        if result.returncode:
            broken += 1
            lines = [l for l in result.stderr.splitlines() if l.strip()][:4]
            print(f"check_page_scripts: {os.path.relpath(path, ROOT)} does not parse:\n  " + "\n  ".join(lines))
    if not broken:
        print("check_page_scripts: every page's script and script file parses")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
