"""README confidence signal 4: is the extracted string actually on the page?

A value the model read off the document should appear in the document's own
text layer. When it does, that is strong independent corroboration. When the
document has no text layer — a scan, a photo, an image upload — the signal
does not apply, and treating its absence as evidence against the field would
punish every scanned invoice for being scanned.
"""

import re
from dataclasses import dataclass

# Comparison ignores everything that varies in presentation: case, spacing,
# currency symbols, thousands separators and decimal points. "1,210.00" and
# "1210.00" are the same number, and "EUR 1.210,00" contains it.
_NOISE = re.compile(r"[^0-9a-z]+")


def normalise(text: str) -> str:
    return _NOISE.sub("", text.casefold())


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
        """Is `value` present anywhere in the document's text?

        The whole document is searched rather than only the page the model
        named: a mis-numbered `source_page` is a separate problem, and letting
        it fail a value that is plainly in the document would be a false
        negative.
        """
        needle = normalise(value)
        if not needle:
            return False
        return any(needle in normalise(page) for page in self.pages)
