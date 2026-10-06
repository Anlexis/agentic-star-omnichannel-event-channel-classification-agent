"""AgentCore Platform v1.0"""

# Personal-data shapes, defined once.
#
# Two boundaries need them and they must not be able to disagree: the input
# parser masks what it finds before writing anything to state, and the output
# boundary refuses a response in which any of it survived. When those two carry
# separate copies of the same pattern, the second stops being a check on the
# first — a shape one of them recognises and the other does not is either data
# destroyed for no reason or data released that should not have been.

from __future__ import annotations

import re
from typing import Any, List, Optional

MASK_TOKEN = "***MASKED***"

# Payload keys whose value is personal data whatever it looks like. This is the
# precise mechanism and the one that does the real work: a field NAMED phone is
# a phone, and no amount of pattern-matching is a better judge of that than the
# caller's own schema.
PII_KEYS = frozenset(
    {
        "name",
        "full_name",
        "customer_name",
        "first_name",
        "last_name",
        "email",
        "email_address",
        "phone",
        "phone_number",
        "tel",
        "mobile",
        "address",
        "postal_address",
        "street_address",
        "zip",
        "postal_code",
        "dob",
        "date_of_birth",
        "ssn",
        "my_number",
    }
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# ── Telephone numbers in free text ───────────────────────────────────────────
# The pattern net is the SECONDARY mechanism: it exists for a number written
# inside a comment or a complaint body, where no key names it. Its job is
# therefore to recognise the shape of a phone number, not to treat every long
# run of digits as one.
#
# That distinction is the whole design here, because in retail data the long
# runs of digits are the domain. A JAN/EAN-13 barcode is thirteen digits, an
# ITF-14 carton code is fourteen, an ISO-8601 timestamp is a hyphen-separated
# run, and an order reference is a year followed by more digits. A net cast
# wide enough to catch a bare digit run catches all of those, and what it does
# to them is not a false alarm that someone reviews — it silently replaces the
# value, so the timestamp stops parsing, the barcode stops being a barcode, and
# the data-quality score that is computed from them reports the damage as the
# caller's fault.
#
# So a candidate must LOOK like a phone number:
#   - ten to fifteen digits (E.164 caps at fifteen; domestic numbers start at ten),
#   - written with separators, or in international `+` form,
#   - not the leading date of a timestamp,
#   - not part of a longer identifier.
#
# `.` is deliberately NOT a separator. A dot between digit groups is a decimal
# point far more often than a phone separator in this data, and admitting it
# makes a price like `123456789.0` a ten-digit "phone number".
_PHONE_CANDIDATE_RE = re.compile(r"(?<![A-Za-z0-9_\-])\+?\d[\d\-\s()]{6,}\d(?![A-Za-z0-9_\-])")
_ISO_DATE_PREFIX_RE = re.compile(r"^\+?\d{4}-\d{2}-\d{2}")
_PHONE_SEPARATORS = frozenset("- \t()")
_PHONE_MIN_DIGITS = 10
_PHONE_MAX_DIGITS = 15


def _is_phone_shaped(candidate: str) -> bool:
    """True when *candidate* has the structure of a telephone number."""
    if _ISO_DATE_PREFIX_RE.match(candidate):
        return False
    digits = sum(1 for ch in candidate if ch.isdigit())
    if not _PHONE_MIN_DIGITS <= digits <= _PHONE_MAX_DIGITS:
        return False
    # Structure: grouped with separators, or explicit international form. A
    # bare run of digits carries no evidence that it is a phone number, and in
    # this domain the evidence usually points the other way.
    return candidate.startswith("+") or any(ch in _PHONE_SEPARATORS for ch in candidate)


def find_phone(text: str) -> Optional[str]:
    """Return the first phone-shaped substring of *text*, else None."""
    for match in _PHONE_CANDIDATE_RE.finditer(text):
        if _is_phone_shaped(match.group()):
            return match.group()
    return None


def _mask_phones(text: str) -> str:
    return _PHONE_CANDIDATE_RE.sub(lambda m: MASK_TOKEN if _is_phone_shaped(m.group()) else m.group(), text)


def mask_text(value: Any) -> Any:
    """Mask personal-data shapes inside a single scalar."""
    if not isinstance(value, str):
        return value
    return _mask_phones(EMAIL_RE.sub(MASK_TOKEN, value))


def mask_payload(obj: Any) -> Any:
    """Return a copy of *obj* with personal data masked.

    Keys named as personal data have their values replaced outright; every
    remaining string is scanned for an embedded address or number.
    """
    if isinstance(obj, dict):
        return {
            key: MASK_TOKEN if isinstance(key, str) and key.strip().lower() in PII_KEYS else mask_payload(value)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [mask_payload(item) for item in obj]
    return mask_text(obj)


def collect_strings(obj: Any) -> List[str]:
    """Every string leaf in a nested structure, keys included.

    Keys are collected because a mapping built from caller data can carry the
    caller's text in either position, and a scan that reads only values would
    pass a response whose keys are the payload.
    """
    found: List[str] = []
    if isinstance(obj, str):
        found.append(obj)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str):
                found.append(key)
            found.extend(collect_strings(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(collect_strings(item))
    return found


def find_pii(obj: Any) -> Optional[str]:
    """Return the kind of personal data found anywhere in *obj*, else None.

    The KIND is returned, never the value: this is used to explain a refusal,
    and a refusal that quotes what it refused has published it.
    """
    for text in collect_strings(obj):
        if EMAIL_RE.search(text):
            return "email_address"
        if find_phone(text) is not None:
            return "telephone_number"
    return None
