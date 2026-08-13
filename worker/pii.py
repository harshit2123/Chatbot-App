"""PII redaction applied before log previews are persisted.

Regex-based rather than an ML recognizer (Presidio): predictable, dependency-free,
and fast enough to run inline in the worker. The tradeoff is real — regexes catch
structured identifiers, not names, addresses, or free-form disclosure. Stated
plainly in the README rather than implied to be complete.

Order matters. Card and phone patterns both match runs of digits, so cards are
redacted first to stop a card number being partially consumed as a phone match.
"""

from __future__ import annotations

import re

EMAIL_TOKEN = "[REDACTED_EMAIL]"
PHONE_TOKEN = "[REDACTED_PHONE]"
CARD_TOKEN = "[REDACTED_CARD]"
SSN_TOKEN = "[REDACTED_SSN]"
IP_TOKEN = "[REDACTED_IP]"

EMAIL_RE = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")

# 13-19 digits, optionally separated by spaces or hyphens, e.g. 4111 1111 1111 1111.
CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# International and NANP shapes: +91 98765 43210, (555) 123-4567, 555-123-4567.
# Groups run to 5 digits because several countries (India, for one) use 5-digit
# blocks — capping at 4 silently missed them.
PHONE_RE = re.compile(
    r"(?<![\w.])"
    r"(?:\+\d{1,3}[ -]?)?"
    r"(?:\(\d{2,5}\)[ -]?|\d{2,5}[ -])"
    r"\d{3,5}[ -]?\d{3,5}"
    r"(?![\w.])"
)

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _luhn_valid(digits: str) -> bool:
    """Luhn checksum, used to avoid redacting arbitrary long digit runs as cards."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _redact_cards(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        digits = re.sub(r"[ -]", "", match.group())
        # Only redact things that actually check out as card numbers; a 16-digit
        # request id should survive intact.
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            return CARD_TOKEN
        return match.group()

    return CARD_RE.sub(replace, text)


def redact(text: str | None) -> str | None:
    """Redact structured PII. Returns None unchanged so empty previews stay empty."""
    if not text:
        return text

    redacted = EMAIL_RE.sub(EMAIL_TOKEN, text)
    redacted = SSN_RE.sub(SSN_TOKEN, redacted)
    redacted = _redact_cards(redacted)
    redacted = IPV4_RE.sub(IP_TOKEN, redacted)
    redacted = PHONE_RE.sub(PHONE_TOKEN, redacted)
    return redacted
