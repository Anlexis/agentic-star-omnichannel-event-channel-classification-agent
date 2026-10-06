"""AgentCore Platform v1.0"""

# One definition of what caller data this agent accepts, shared by the two
# places that see it: the outer input stage, which sees the request as text,
# and the domain parser, which sees it as a decoded object.
#
# They are separate layers on purpose and each catches something the other
# cannot. A control token written literally is visible in the text and gone from
# the object (it is just a string value by then). A control token written as
# `<|im_start|>` is invisible in the text — JSON escapes are still
# escapes until something decodes them — and plain in the object. Screening only
# one of the two leaves the other form unscreened.

from __future__ import annotations

import math
import re
from typing import Any, Optional, Tuple

# ── Chat-template control markers ────────────────────────────────────────────
# Screened as a CLASS rather than as a list of directive phrases. A phrase list
# recognises an instruction it has seen written before; these markers are how a
# model is told where a turn begins and who is speaking, so one of them inside
# caller data is an attempt to forge that structure whatever words follow it.
#
# `<<SYS>>` is in the set deliberately: the framework's own injection scoring
# returns nothing for it while scoring `<|im_start|>` and `[INST]` as high
# confidence, so it is the member of the class most likely to arrive unscreened.
_CONTROL_TOKEN_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"<\|[^|>\n]{0,64}\|>"),
    re.compile(r"\[/?INST\]", re.IGNORECASE),
    re.compile(r"<<\s*/?\s*SYS\s*>>", re.IGNORECASE),
    re.compile(r"</?s>", re.IGNORECASE),
    re.compile(r"</?system>", re.IGNORECASE),
)

# Tags are removed and the remainder re-joined before the second pass, so a
# marker split across markup (`<|im<b>_start</b>|>`) is caught once the splice
# closes. The strip is a SCREENING step only — nothing sanitised this way is
# ever forwarded, because a sanitiser that removes a marker and passes the rest
# on converts a detectable attack into an undetectable one.
_MARKUP_TAG_RE = re.compile(r"<[^<>]{0,128}>")


def _strip_markup(text: str) -> str:
    return _MARKUP_TAG_RE.sub("", text)


def find_control_token(text: Any) -> Optional[str]:
    """Return the name of the control-marker family found in *text*, else None.

    The family name is returned rather than the matched text: the caller needs
    to know which screen fired, and echoing the match back would reproduce the
    payload in the error path it was refused on.
    """
    if not isinstance(text, str):
        return None
    for candidate in (text, _strip_markup(text)):
        for pattern in _CONTROL_TOKEN_PATTERNS:
            if pattern.search(candidate):
                return "chat_template_control_marker"
    return None


def screen_decoded(obj: Any, _depth: int = 0) -> Optional[str]:
    """Walk a decoded payload depth-first and screen every string in it.

    KEYS are screened as well as values. A mapping key is caller-controlled in
    exactly the same way a value is, and a screen that reads only values leaves
    a whole half of the payload unexamined.

    Returns the family name of the first finding, else None.
    """
    if _depth > _MAX_NESTING_DEPTH:
        return "payload_nesting_depth"
    if isinstance(obj, str):
        return find_control_token(obj)
    if isinstance(obj, dict):
        for key, value in obj.items():
            found = find_control_token(key) if isinstance(key, str) else None
            if found:
                return found
            found = screen_decoded(value, _depth + 1)
            if found:
                return found
        return None
    if isinstance(obj, list):
        for item in obj:
            found = screen_decoded(item, _depth + 1)
            if found:
                return found
        return None
    return None


# ── Structural bounds ────────────────────────────────────────────────────────
# A payload that is merely large is not hostile, but nothing downstream is
# bounded by anything else: the quality scorer iterates every key, the masker
# recurses every value, and the result carries a flag per finding. These are the
# limits that keep one request's cost a function of the contract rather than of
# what the caller chose to send.
MAX_RAW_INPUT_CHARS = 65_536
MAX_PAYLOAD_KEYS = 256
_MAX_NESTING_DEPTH = 32


# ── Numbers ──────────────────────────────────────────────────────────────────
# Every caller-controlled number goes through this. `float("NaN")` and
# `float("Infinity")` both parse without complaint, and Python's own `json`
# decoder accepts the bare literals `NaN` and `Infinity` in a request body. A
# NaN then compares False against every threshold it meets, so a rule written as
# "flag it when the ratio exceeds the limit" silently stops flagging anything —
# fail-open on the exact decision the check exists to make.
def finite_in_range(value: Any, lo: float, hi: float) -> Optional[float]:
    """Return *value* as a float when it is finite and within [lo, hi], else None.

    Booleans are rejected rather than coerced: `float(True)` is `1.0`, so a
    caller sending `true` for an amount would otherwise be read as one yen.
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    if parsed < lo or parsed > hi:
        return None
    return parsed


# ── Identifiers that render into the response ────────────────────────────────
# Caller strings that come back out in a flag or a violation message are locked
# to this shape. The alphabet is the one this template already declares for
# identifiers in its data-quality rules, so a SKU that the scorer calls
# well-formed is a SKU this renders — the two cannot disagree about what an
# identifier is.
#
# What it excludes is what matters: whitespace, newlines, quotes and brackets.
# A consumer that renders a list of violations line by line cannot be handed an
# extra line, and one that renders them into markup cannot be handed a tag.
_INERT_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# Redaction markers. A value carrying one of these is not the caller's value —
# something upstream replaced it, in whole or in part, before this code ran.
#
# It matters that these are recognised rather than treated as ordinary text.
# The platform's own personal-data filter rewrites what it matches in place, so
# a field arrives looking like a perfectly well-formed short string, and code
# that does not know the marker will happily report it as a valid identifier —
# certifying a redaction sentinel as data. `[MASKED]` even satisfies the
# identifier shape below on its own.
REDACTION_SENTINELS = ("[MASKED]", "***MASKED***")


def is_redacted(value: Any) -> bool:
    """True when *value* carries a redaction marker from any layer."""
    return isinstance(value, str) and any(mark in value for mark in REDACTION_SENTINELS)


# Substituted for a value that does not meet the shape. The rejected value is
# never echoed — naming the field is what the caller needs in order to fix it,
# and repeating the value would put the refused content back into the response
# the refusal exists to keep it out of.
INERT_PLACEHOLDER = "<unrenderable>"


def inert_identifier(value: Any) -> Optional[str]:
    """Return *value* if it is a renderable identifier, else None.

    A redacted value is not an identifier, whatever shape it has left.
    """
    if not isinstance(value, str) or is_redacted(value):
        return None
    candidate = value.strip()
    if not _INERT_IDENTIFIER_RE.match(candidate):
        return None
    return candidate


def render_identifier(value: Any) -> str:
    """Render a caller identifier into output, or the placeholder if it cannot be."""
    return inert_identifier(value) or INERT_PLACEHOLDER
