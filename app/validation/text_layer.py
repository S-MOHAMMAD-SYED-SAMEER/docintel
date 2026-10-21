"""README confidence signal 4: is the extracted string actually on the page?

A value the model read off the document should appear in the document's own
text layer. When it does, that is strong independent corroboration. When the
document has no text layer — a scan, a photo, an image upload — the signal
does not apply, and treating its absence as evidence against the field would
punish every scanned invoice for being scanned.

The match is exact on whole tokens. Both sides are put through Unicode NFKC and
case-folded; the page is split into whitespace-separated tokens, and inside a
token only presentation is ignored (punctuation, currency symbols, thousands
separators), so `1210.00` matches `EUR 1,210.00` and `inv-2026-0042` matches
`INV-2026-0042`. A value is found only if its tokens appear, in order and
whole, in one page's tokens. There is no substring pass: `FR4012345682` is not
found in a page that prints `FR40123456824`.

Invisible (Unicode "format") characters such as the zero-width space are
dropped from the *page*, because text extraction can insert them. A *value*
that contains one is not what is printed, so it is never found.
"""

import re
import unicodedata
from dataclasses import dataclass

# Everything inside a token that is not a letter or a digit is presentation.
_PRESENTATION = re.compile(r"[\W_]+")


def has_invisible(text: str) -> bool:
    """Does `text` contain a format character (zero-width space, soft hyphen, bidi mark, BOM...)?"""
    return any(unicodedata.category(char) == "Cf" for char in unicodedata.normalize("NFKC", text))


def _tokens(text: str, *, drop_invisible: bool) -> list[str]:
    folded = unicodedata.normalize("NFKC", text).casefold()
    if drop_invisible:
        folded = "".join(char for char in folded if unicodedata.category(char) != "Cf")
    tokens = (_PRESENTATION.sub("", token) for token in folded.split())
    return [token for token in tokens if token]


def normalise(text: str) -> str:
    """The tokens of `text` joined by single spaces (what the matcher compares)."""
    return " ".join(_tokens(text, drop_invisible=True))


@dataclass(frozen=True)
class TextLayer:
    """The document's embedded text, or the explicit absence of any."""

    pages: tuple[str, ...] = ()

    @property
    def exists(self) -> bool:
        return bool(self.pages)

    @classmethod
    def from_pages(cls, pages: list[str]) -> "TextLayer":
        return cls(tuple(pages))

    def contains(self, value: str) -> bool:
        """Is `value` printed, whole, somewhere in the document's text?

        The whole document is searched rather than only the page the model
        named: a mis-numbered `source_page` is a separate problem, and letting
        it fail a value that is plainly in the document would be a false
        negative.
        """
        if has_invisible(value):
            return False
        needle = _tokens(value, drop_invisible=False)
        if not needle:
            return False
        return any(_contains_run(_tokens(page, drop_invisible=True), needle) for page in self.pages)


def _contains_run(haystack: list[str], needle: list[str]) -> bool:
    width = len(needle)
    return any(haystack[start : start + width] == needle for start in range(len(haystack) - width + 1))
