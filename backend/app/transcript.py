"""Deterministic, local scam-language analysis for consented transcripts.

This module deliberately has no model or network dependency.  It accepts only
bounded text, normalizes it in memory, and returns fixed metadata.  Neither
the transcript nor matching excerpts are included in any result.
"""

from __future__ import annotations

import re
from typing import Any


MAX_TRANSCRIPT_CHARS = 12_000

SCAM_CATEGORIES = (
    "financial_request",
    "credential_request",
    "urgency_or_threat",
    "impersonation",
    "remote_access_request",
)


class TranscriptValidationError(ValueError):
    """Raised when a transcript cannot be accepted for local analysis."""


_WHITESPACE = re.compile(r"\s+")

# Every rule is compiled once at import time.  The expressions intentionally
# describe broad language signals rather than trying to identify a caller or
# prove that a conversation is fraudulent.
_CATEGORY_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "financial_request": (
        re.compile(
            r"\b(?:send|wire|transfer|pay|give|deposit|buy|purchase|load|top[ -]?up)"
            r"\b.{0,80}\b(?:money|cash|funds?|payment|gift cards?|bitcoin|crypto|"
            r"dollars?|usd)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:money|cash|funds?|payment|gift cards?|bitcoin|crypto|dollars?|usd)\b"
            r".{0,80}\b(?:send|wire|transfer|pay|give|deposit|buy|purchase|load|top[ -]?up)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:send|wire|transfer|pay|give|deposit|buy|purchase)\s+(?:me|us|the)\b",
            re.IGNORECASE,
        ),
        re.compile(r"(?:[$€£]\s?\d|\b\d[\d,]*(?:\.\d{2})?\s*(?:dollars?|usd)\b)", re.IGNORECASE),
    ),
    "credential_request": (
        re.compile(
            r"\b(?:tell|give|share|read|provide|confirm|verify|send|enter|type)\b"
            r".{0,60}\b(?:password|passcode|pin|one[ -]?time password|otp|"
            r"verification code|security code|login|username|user name|social security|"
            r"ssn|account number|card number|cvv|cvc)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:what is|can you provide|need|asking for)\b.{0,60}\b(?:your\s+)?"
            r"(?:password|passcode|pin|one[ -]?time password|otp|verification code|"
            r"security code|login|username|account number|card number|cvv|cvc)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:password|passcode|pin|one[ -]?time password|otp|verification code|"
            r"security code|login|username|account number|card number|cvv|cvc)\b"
            r".{0,60}\b(?:tell|give|share|read|provide|confirm|verify|send|enter|type)\b",
            re.IGNORECASE,
        ),
    ),
    "urgency_or_threat": (
        re.compile(
            r"\b(?:act|respond|reply|pay|send|verify|confirm|call|do)\s+"
            r"(?:now|immediately|at once|right away)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:urgent(?:ly)?|immediate(?:ly)?|asap|final warning|last chance|"
            r"time[ -]?sensitive)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:unless|or else|if you do not|if you don't|failure to)\b.{0,100}\b"
            r"(?:close|closed|suspend|suspended|freeze|frozen|arrest|jail|prosecute|"
            r"lose|terminate|block|delete|penalt(?:y|ies)|legal action)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:your|the)\s+(?:account|service|benefits?|license)\b.{0,80}\b"
            r"(?:suspend|suspended|close|closed|freeze|frozen|terminate|block|expire)\b",
            re.IGNORECASE,
        ),
    ),
    "impersonation": (
        re.compile(
            r"\b(?:i(?:'m| am)|this is|calling from|speaking for|representing|from|with)\b"
            r".{0,50}\b(?:your\s+)?(?:bank|police|irs|government|tax (?:office|agency)|"
            r"court|microsoft|apple|amazon|paypal|venmo|visa|mastercard|delivery|"
            r"support|security|fraud department)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:your|the)\s+(?:bank|police|irs|government|tax (?:office|agency)|"
            r"court|microsoft|apple|amazon|paypal|venmo|visa|mastercard|delivery|"
            r"support|security|fraud department)\b.{0,50}\b(?:calling|speaking|"
            r"department|agent|officer|representative)\b",
            re.IGNORECASE,
        ),
        re.compile(r"\b(?:impersonat(?:e|ing)|pretend(?:ing)? to be)\b", re.IGNORECASE),
    ),
    "remote_access_request": (
        re.compile(
            r"\b(?:install|download|open|run|launch|give|grant|allow|share|provide)\b"
            r".{0,80}\b(?:anydesk|teamviewer|screenconnect|remote desktop|quick assist|"
            r"remote access|screen sharing|screen share|splashtop|logmein)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:remote access|screen share|screen sharing|control your\s+"
            r"(?:computer|device|phone)|let me control)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:give|grant|allow|provide)\b.{0,40}\b(?:access|control)\b.{0,40}\b"
            r"(?:computer|device|phone|screen)\b",
            re.IGNORECASE,
        ),
    ),
}

_CATEGORY_EXPLANATIONS = {
    "financial_request": "money, payment, transfer, gift-card, or cryptocurrency requests",
    "credential_request": "requests for passwords, codes, PINs, or account credentials",
    "urgency_or_threat": "urgent instructions, deadlines, or consequences",
    "impersonation": "claims to represent a bank, agency, company, or authority",
    "remote_access_request": "requests to install remote software or share device control",
}


def _unavailable_result() -> dict[str, Any]:
    return {
        "scam_status": "unavailable",
        "scam_score": None,
        "scam_categories": [],
        "scam_explanation": (
            "No transcript was provided, so local scam-language analysis is unavailable."
        ),
    }


def unavailable_scam_result() -> dict[str, Any]:
    """Return a fresh no-transcript result for inference and route callers."""

    return _unavailable_result()


def normalize_transcript(transcript: str) -> str:
    """Normalize case and whitespace without retaining the source text."""

    return _WHITESPACE.sub(" ", transcript).strip().casefold()


def _validate_transcript(transcript: str | None, consented: bool | None) -> None:
    if transcript is None:
        return
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        raise TranscriptValidationError(
            f"The transcript exceeds the {MAX_TRANSCRIPT_CHARS} character limit."
        )
    if not transcript.strip():
        return
    if consented is False:
        raise TranscriptValidationError(
            "Transcript consent must be true when a transcript is provided."
        )


def analyze_transcript(
    transcript: str | None, *, consented: bool | None = None
) -> dict[str, Any]:
    """Analyze a bounded transcript and return only fixed scam metadata.

    ``consented`` is optional for direct pure-function use.  HTTP callers pass
    the submitted transcript consent explicitly, which rejects non-empty text
    without affirmative consent before any matching occurs.
    """

    if transcript is None:
        return _unavailable_result()
    if not isinstance(transcript, str):
        raise TranscriptValidationError("The transcript must be text.")
    _validate_transcript(transcript, consented)
    if not transcript.strip():
        return _unavailable_result()

    normalized = normalize_transcript(transcript)
    categories = [
        category
        for category in SCAM_CATEGORIES
        if any(pattern.search(normalized) for pattern in _CATEGORY_PATTERNS[category])
    ]
    score = round(len(categories) / len(SCAM_CATEGORIES), 4)

    if categories:
        matched = "; ".join(
            f"{category}: {_CATEGORY_EXPLANATIONS[category]}" for category in categories
        )
        explanation = (
            f"Local rule-based transcript analysis matched {matched}. "
            f"Scam score {score:.4f} is the fraction of the five fixed categories "
            "matched; it is a heuristic signal, not a probability, and is "
            "independent of the deepfake score."
        )
        status = "review"
    else:
        explanation = (
            "A consented transcript was analyzed locally and no configured "
            "scam-language categories matched. Scam score 0.0000 is the fraction "
            "of the five fixed categories matched; it is a heuristic signal, not "
            "a probability, and is independent of the deepfake score."
        )
        status = "lower_signal"

    return {
        "scam_status": status,
        "scam_score": score,
        "scam_categories": categories,
        "scam_explanation": explanation,
    }


__all__ = [
    "MAX_TRANSCRIPT_CHARS",
    "SCAM_CATEGORIES",
    "TranscriptValidationError",
    "analyze_transcript",
    "normalize_transcript",
    "unavailable_scam_result",
]
