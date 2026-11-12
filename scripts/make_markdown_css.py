"""Turns MDViewer's preview stylesheet into one scoped to the playground's rendered answers.

MDViewer styles a whole page; here the same design has to sit inside a chat bubble next to the
gateway's own chrome, so every rule is prefixed with .md and the page-frame rules are dropped.
"""
import pathlib
import re
import sys

SOURCE = pathlib.Path(sys.argv[1]) / "mdviewer-preview.css"
TARGET = pathlib.Path(sys.argv[2])

# Rules that belong to the desktop app, not to a message in a chat
DROP_SELECTORS = re.compile(
    r"mdv-block-editing|mdv-block-editor|mdv-cell-editing|mdv-img-selected|mdv-chart|mdv-diagram"
    r"|mermaid|^html$|^html\[|^body|^:root$|mdv-code-lang|^\.mdv-code pre$", re.I)
# The document frame: a message has no page, margins or paper shadow of its own
DROP_DECLARATIONS = re.compile(r"^\s*(max-width|margin-inline|box-shadow|min-height|padding-block)\s*:", re.M)


def blocks(css):
    """Splits the stylesheet into (selector, body) pairs, skipping at-rules such as @media print."""
    depth, start, found = 0, 0, []
    i = 0
    while i < len(css):
        if css[i] == "{":
            if depth == 0:
                selector = css[start:i].strip()
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                found.append((selector, css[css.index("{", start) + 1:i]))
                start = i + 1
        i += 1
    return [(s, b) for s, b in found if not s.startswith("@")]


DARK = 'html[data-theme="dark"]'
COLOUR = re.compile(r"^(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\))$")


def palettes(css):
    """MDViewer's light palette (:root) and its dark one, {--name: value} each."""
    light, dark = {}, {}
    for selector, body in blocks(css):
        if selector in (":root", DARK):
            for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body):
                (dark if selector == DARK else light)[name] = value.strip()
    return light, dark


def dark_colours(css):
    """MDViewer's dark-only colours for code - {frozenset of plain selectors: colour}."""
    found = {}
    for selector, body in blocks(css):
        parts = [p.strip() for p in selector.split(",")]
        colour = re.fullmatch(r"\s*color\s*:\s*([^;]+);\s*", body)
        if colour and all(p.startswith(DARK + " ") for p in parts):
            found[frozenset(p[len(DARK) + 1:] for p in parts)] = colour.group(1).strip()
    return found


def scope(selector):
    """`.md` in front of every part of a selector list, so nothing leaks into the page."""
    parts = []
    for part in selector.split(","):
        part = part.strip()
        if not part:
            continue
        # MDViewer wraps a code block in .mdv-code; markdown-it writes a plain <pre>
        part = ".md pre" if part == ".mdv-code" else part
        parts.append(part if part.startswith(".md ") or part == ".md" else f".md {part}")
    return ", ".join(parts)


# Comments sit between rules and would otherwise be read as part of the next selector
source = re.sub(r"/\*.*?\*/", "", SOURCE.read_text(encoding="utf-8"), flags=re.S)
light, dark = palettes(source)
# Both themes at once: a colour is light-dark(light, dark), and the page's color-scheme (base.html,
# set from the theme switch or the system) picks one. Anything that is not a single colour - the
# plate's shadow - gets a dark rule of its own.
both, dark_only = {}, {}
for name, value in light.items():
    other = dark.get(name, value)
    if other == value:
        both[name] = value
    elif COLOUR.match(value) and COLOUR.match(other):
        both[name] = f"light-dark({value}, {other})"
    else:
        both[name] = value
        dark_only[name] = other
out = ["/* The look of MDViewer's preview, for answers rendered in the playground, in light and dark.",
       " * Generated from MDViewer's MainController.previewCss() by scripts/make_markdown_css.py;",
       " * every rule is scoped to .md and the page-frame rules are left out.",
       " */",
       ".md{", *(f"  {name}:{value};" for name, value in both.items()),
       "  color:var(--ink);font-family:var(--body);",
       "}"]
if dark_only:
    values = " ".join(f"{name}:{value};" for name, value in dark_only.items())
    out += [f':root[data-theme="dark"] .md{{ {values} }}',
            f'@media (prefers-color-scheme: dark){{ :root:not([data-theme="light"]) .md{{ {values} }} }}']
code_dark = dark_colours(source)
for selector, body in blocks(source):
    if DROP_SELECTORS.search(selector):
        continue
    # Code colours MDViewer changes for dark: the light rule carries both
    night = code_dark.get(frozenset(p.strip() for p in selector.split(",")))
    if night:
        body = re.sub(r"color\s*:\s*(#[0-9a-fA-F]{3,8})", lambda m: f"color:light-dark({m.group(1)}, {night})", body, count=1)
    body = DROP_DECLARATIONS.sub(lambda m: f"  /* dropped: {m.group(1)} */ x-{m.group(1)}:", body)
    body = re.sub(r"\s*/\* dropped: [\w-]+ \*/ x-[\w-]+:[^;]*;", "", body)
    if not body.strip():
        continue
    out.append(f"{scope(selector)} {{{body}}}")

# What MDViewer's own assistant panel adds on top of the preview stylesheet: an answer is a column
# in a side panel, not a printed page. The code padding comes from its .mdv-code pre, which is
# dropped above because markdown-it writes no such wrapper.
out += ["",
        "/* MDViewer's assistant panel overrides: a message, not a page */",
        ".md { font-size:14px; line-height:1.6; }",
        ".md > :first-child { margin-top:0; }",
        ".md > :last-child { margin-bottom:0; }",
        ".md pre { padding:16px 18px; overflow-x:auto; }",
        ".md h1 { font-size:1.75rem; }",
        ".md h2 { font-size:1.35rem; }",
        ".md h3 { font-size:1.15rem; }",
        ".md table { display:block; overflow-x:auto; }",
        "",
        "/* Tighter rhythm than MDViewer's, which spaces a printed page: there a horizontal rule",
        " * gets two and a half lines of air above it and the same below, and a section heading",
        " * nearly as much again. Read in a chat column that looks like the answer has finished and",
        " * something else has started. A line and a quarter, and at most two lines of air between",
        " * one block and the next. */",
        ".md { line-height:1.25; }",
        ".md p, .md ul, .md ol { margin-bottom:1.25em; }",
        ".md li { margin:.15em 0; }",
        ".md h1, .md h2, .md h3, .md h4, .md h5, .md h6 { margin-top:1.5em; margin-bottom:.5em; }",
        ".md hr { margin-top:1.25em; margin-bottom:1.25em; }",
        ".md pre, .md blockquote, .md table, .md figure { margin-top:1.25em; margin-bottom:1.25em; }"]

TARGET.write_text("\n".join(out).replace("\r\n", "\n") + "\n", encoding="utf-8", newline="\n")
print(f"{TARGET}: {len(out)} rules from {len(blocks(source))} in the source")
