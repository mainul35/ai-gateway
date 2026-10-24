"""Renders an answer's Markdown to HTML, for the playground to show it the way MDViewer does.

A model can write anything, so raw HTML in an answer is escaped rather than passed through, links
open in a new tab, and pictures are shown as links instead of being fetched: an answer must not be
able to make the browser call out to somewhere of its own choosing.

Formulas - $...$, $$...$$, \\(...\\), \\[...\\] - are taken out before any Markdown is read, so that
an _ or a * in them is not made italic, and handed to the page as written, marked class="math";
the page draws them with KaTeX (app/static/katex). A plain $5 and $10 is not a formula: a $...$ span
counts only when it has a backslash command, a ^ or a _ in it.
"""
import logging
import re

from app.tools import tex

log = logging.getLogger("tools.markdown")

try:
    from markdown_it import MarkdownIt
    from markdown_it.common.utils import escapeHtml
except ImportError:  # the gateway still runs, answers just stay plain text
    MarkdownIt = None

SAFE_LINK = re.compile(r"^(https?:|mailto:|/chat/files/|#)", re.I)
CHAT_FILE = re.compile(r"^/chat/files/\d+$")
MATHS = re.compile(r"\\[A-Za-z]|[\^_]")
# Where each kind of formula opens: (opening, closing, shown on its own line)
DELIMITERS = (("$$", "$$", True), ("\\[", "\\]", True), ("\\(", "\\)", False))
_renderer = None


def _build():
    # html=False escapes any HTML the model wrote; tables and ~~strikethrough~~ are worth having
    parser = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": False})
    parser.enable(["table", "strikethrough", "linkify"])

    def link_open(self, tokens, index, options, env):
        token = tokens[index]
        href = token.attrGet("href") or ""
        if not SAFE_LINK.match(href):
            token.attrSet("href", "#")
        token.attrSet("target", "_blank")
        token.attrSet("rel", "noopener noreferrer nofollow")
        return self.renderToken(tokens, index, options, env)

    def image(self, tokens, index, options, env):
        """A picture in an answer becomes a link: loading it would call out to its host."""
        token = tokens[index]
        source = token.attrGet("src") or ""
        label = token.content or source
        # The user's own files are safe to show - an exact /chat/files/<id> and nothing more: a path
        # like /chat/files/../../logout would have the browser fetch any page here, signed in
        if CHAT_FILE.match(source):
            return f'<img src="{escapeHtml(source)}" alt="{escapeHtml(label)}">'
        if not SAFE_LINK.match(source):
            return escapeHtml(label)
        return (f'<a href="{escapeHtml(source)}" target="_blank" rel="noopener noreferrer nofollow">'
                f'{escapeHtml(label)} (image)</a>')

    def symbols(state):
        """Turns $\rightarrow$ into an arrow, in prose only.

        Done on the token stream rather than the text, so that it reaches every piece of writing and
        no piece of code: inline code and fenced blocks are tokens of their own and are never walked.
        """
        for token in state.tokens:
            if token.type != "inline":
                continue
            for child in token.children or ():
                if child.type == "text":
                    child.content = tex.unwrap(child.content)

    def maths(state, silent):
        """A formula starting here becomes one token, its text untouched by the rest of Markdown."""
        src, start = state.src, state.pos
        found = None
        for opening, closing, display in DELIMITERS:
            if src.startswith(opening, start):
                end = src.find(closing, start + len(opening))
                if end > start + len(opening):
                    found = (src[start + len(opening):end], end + len(closing), display)
                break
        if found is None and src.startswith("$", start) and not src.startswith("$$", start):
            # $x$: no space just inside either dollar, no digit just after the closing one ($5 and $10)
            end = src.find("$", start + 1)
            while end > 0 and src[end - 1] == "\\":            # \$ is a dollar inside the formula
                end = src.find("$", end + 1)
            inner = src[start + 1:end] if 0 < end - start <= 300 else ""
            if (inner and not inner[0].isspace() and not inner[-1].isspace() and "\n\n" not in inner
                    and not src[end + 1:end + 2].isdigit() and MATHS.search(inner)):
                found = (inner, end + 1, False)
        if found is None or not found[0].strip():
            return False
        if not silent:
            token = state.push("maths", "", 0)
            token.content = found[0].strip()
            token.info = "1" if found[2] else "0"
        state.pos = found[1]
        return True

    def maths_html(self, tokens, index, options, env):
        token = tokens[index]
        return f'<span class="math" data-display="{token.info}">{escapeHtml(token.content)}</span>'

    parser.inline.ruler.before("escape", "maths", maths)
    parser.add_render_rule("maths", maths_html)
    parser.add_render_rule("link_open", link_open)
    parser.add_render_rule("image", image)
    parser.core.ruler.push("symbols", symbols)
    return parser


def is_available():
    return MarkdownIt is not None


def render(text):
    """HTML for one answer, or None when it should simply be shown as text."""
    global _renderer
    if not text or MarkdownIt is None:
        return None
    if _renderer is None:
        _renderer = _build()
    try:
        return _renderer.render(text)
    except Exception as e:  # never lose an answer over its formatting
        log.warning("Could not render Markdown: %s", e)
        return None
