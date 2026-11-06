"""Literal, terminal-safe presentation of external text; never transform core data."""

import unicodedata

from rich.text import Text

# Direction-changing controls can spoof labels/paths. Keep legitimate joiners,
# combining marks, variation selectors, spaces and other printable Unicode.
_BIDI_CONTROLS = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)


def safe_text(value: str = "", *, style: str | None = None, multiline: bool = False) -> Text:
    """Render control characters as visible escapes, without interpreting markup.

    Escaping every C0/C1 introducer and terminator neutralizes complete and
    incomplete ANSI/OSC sequences without depending on a sequence parser.
    Only application-generated multiline layouts should opt into literal LF.
    Sanitization applies to a new Text object, leaving input and JSON untouched.
    """
    text = "".join(
        char.encode("unicode_escape").decode("ascii")
        if (unicodedata.category(char) in ("Cc", "Cs", "Zl", "Zp") or char in _BIDI_CONTROLS)
        and not (multiline and char == "\n")
        else char
        for char in value
    )
    return Text(text, style=style)
