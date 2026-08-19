"""Redaction tests.

Two failure modes matter equally: leaking real PII, and over-redacting ordinary
text into uselessness. Both are covered.
"""

import pytest

from app.telemetry.pii import redact


@pytest.mark.parametrize(
    "text",
    [
        "reach me at harshit@example.com",
        "HARSHIT.KUMAR+tag@sub.domain.co.uk please",
    ],
)
def test_emails_are_redacted(text):
    result = redact(text)
    assert "@" not in result
    assert "[REDACTED_EMAIL]" in result


def test_ssn_is_redacted():
    assert redact("ssn 123-45-6789") == "ssn [REDACTED_SSN]"


@pytest.mark.parametrize(
    "card",
    [
        "4111111111111111",  # Visa test number
        "4111 1111 1111 1111",
        "5500-0000-0000-0004",
    ],
)
def test_valid_cards_are_redacted(card):
    result = redact(f"card {card} here")
    assert "[REDACTED_CARD]" in result
    assert "1111" not in result.replace("[REDACTED_CARD]", "")


def test_digit_run_that_fails_luhn_is_not_treated_as_a_card():
    """A 16-digit request id should survive; only real card numbers get redacted."""
    result = redact("trace 1234567890123456 done")
    assert "[REDACTED_CARD]" not in result


@pytest.mark.parametrize(
    "phone",
    [
        "+91 98765 43210",
        "(555) 123-4567",
        "555-123-4567",
    ],
)
def test_phones_are_redacted(phone):
    assert "[REDACTED_PHONE]" in redact(f"call {phone} now")


def test_redaction_leaves_no_stray_plus_sign():
    """Regression: "+91 98765 43210" redacted to "+[REDACTED_PHONE]".

    The optional country-code group failed to match, stranding the leading `+`
    outside the token. Harmless for privacy, but it advertises a sloppy redactor.
    """
    assert redact("phone +91 98765 43210 here") == "phone [REDACTED_PHONE] here"


def test_card_redaction_preserves_the_following_separator():
    """Regression: the card pattern consumed the space after the last digit,
    producing "[REDACTED_CARD]ssn" and welding the next word onto the token."""
    result = redact("card 4111 1111 1111 1111 ssn 123-45-6789")

    assert result == "card [REDACTED_CARD] ssn [REDACTED_SSN]"
    assert "[REDACTED_CARD]ssn" not in result


def test_ipv4_is_redacted():
    assert redact("from 192.168.1.44") == "from [REDACTED_IP]"


def test_ordinary_prose_is_untouched():
    """Over-redaction destroys the debugging value of previews."""
    text = "Summarize the Q3 report in 3 bullet points, focusing on revenue growth."
    assert redact(text) == text


def test_code_like_content_survives():
    text = "user: what does status 404 mean versus 500?"
    assert redact(text) == text


def test_multiple_pii_types_in_one_string():
    result = redact("mail a@b.com or call 555-123-4567, ssn 123-45-6789")
    assert "[REDACTED_EMAIL]" in result
    assert "[REDACTED_PHONE]" in result
    assert "[REDACTED_SSN]" in result


@pytest.mark.parametrize("value", [None, ""])
def test_empty_values_pass_through(value):
    assert redact(value) == value
