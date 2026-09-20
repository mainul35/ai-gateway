"""Renders an answer's Markdown to HTML, for the playground to show it the way MDViewer does.

A model can write anything, so raw HTML in an answer is escaped rather than passed through, links
open in a new tab, and pictures are shown as links instead of being fetched: an answer must not be
able to make the browser call out to somewhere of its own choosing.
"""
import logging
import re

log = logging.getLogger("tools.markdown")

try:
    from markdown_it import MarkdownIt
    from markdown_it.common.utils import escapeHtml
except ImportError:  # the gateway still runs, answers just stay plain text
    MarkdownIt = None

SAFE_LINK = re.compile(r"^(https?:|mailto:|/chat/files/|#)", re.I)
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
        if source.startswith("/chat/files/"):  # the user's own generated images are safe to show
            return f'<img src="{escapeHtml(source)}" alt="{escapeHtml(label)}">'
        if not SAFE_LINK.match(source):
            return escapeHtml(label)
        return (f'<a href="{escapeHtml(source)}" target="_blank" rel="noopener noreferrer nofollow">'
                f'{escapeHtml(label)} (image)</a>')

    parser.add_render_rule("link_open", link_open)
    parser.add_render_rule("image", image)
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
