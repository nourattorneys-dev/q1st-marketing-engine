"""An article's structure as an ordered list of text pieces (each text node of its HTML, in document order). The twin in the
other language is built by putting translated pieces back into the source's own HTML, so both have exactly the same
headings, paragraphs, list items, FAQ and tables in the same order (TranslatePress pairs translations by these pieces)."""
from __future__ import annotations
import hashlib, html as _h, json
from html.parser import HTMLParser

SKIP = {"script", "style"}


class _Walk(HTMLParser):
    """Splits HTML into tokens: ('tag', raw) for markup, ('text', text) for text nodes; keeps everything to rebuild it."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens: list[tuple[str, str]] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        self._skip += tag in SKIP
        self.tokens.append(("tag", self.get_starttag_text()))

    def handle_startendtag(self, tag, attrs):
        self.tokens.append(("tag", self.get_starttag_text()))

    def handle_endtag(self, tag):
        self._skip -= tag in SKIP and self._skip > 0
        self.tokens.append(("tag", f"</{tag}>"))

    def handle_data(self, data):
        self.tokens.append(("raw" if self._skip or not data.strip() else "text", data))


def _tokens(html: str) -> list[tuple[str, str]]:
    w = _Walk()
    w.feed(html or "")
    w.close()
    return w.tokens


def segments(html: str) -> list[str]:
    """The text pieces, trimmed, in order."""
    return [t.strip() for k, t in _tokens(html) if k == "text"]


def shape(html: str) -> list[str]:
    """The markup skeleton: tag names in order plus a marker per text piece (attributes and words do not count)."""
    out = []
    for k, t in _tokens(html):
        if k == "tag":
            out.append(t.split()[0].strip("<>/").lower() + ("/" if t.startswith("</") else ""))
        elif k == "text":
            out.append("#")
    return out


def rebuild(html: str, pieces: list[str]) -> str:
    """The same HTML with its text pieces replaced, in order, keeping the whitespace around each piece."""
    it, out = iter(pieces), []
    for k, t in _tokens(html):
        if k == "text":
            lead, trail = t[: len(t) - len(t.lstrip())], t[len(t.rstrip()):]
            out.append(lead + _h.escape(next(it), quote=False) + trail)
        elif k == "raw":
            out.append(_h.escape(t, quote=False) if t.strip() else t)
        else:
            out.append(t)
    return "".join(out)


def fingerprint(pieces: list[str]) -> str:
    return hashlib.sha256(json.dumps(pieces, ensure_ascii=False).encode()).hexdigest()[:32]
