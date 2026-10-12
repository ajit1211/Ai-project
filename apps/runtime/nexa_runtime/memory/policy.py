"""What may become a memory, and what never does on its own
(docs/architecture/memory-security.md).

Secrets are never stored, whoever asks. Personal data that matches a known shape (cards,
bank accounts, national ids, email addresses) and the categories a model is told to flag
(health, finances, identity, ...) are never stored automatically; the user may still write
such a memory down deliberately, which is their own act. An explicit claim must be grounded
in what the user wrote: a well-formed sentence is not evidence.
"""

import hashlib
import re
import unicodedata
from dataclasses import dataclass

MAX_KEY_CHARS = 80

# Credentials and keys in their usual shapes. Matching text is refused outright.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private_key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws_access_key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "github_token"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "slack_token"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "api_key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "jwt"),
    (
        re.compile(
            r"\b(password|passwd|passphrase|pass ?code|pin|otp|secret|api[ _-]?key|token)\b"
            r"\s*(?:is|was|:|=|->)\s*\S+",
            re.IGNORECASE,
        ),
        "credential",
    ),
)

# Personal identifiers NEXA never remembers on its own.
_PERSONAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,4})?\b"), "bank_account"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "national_id"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "email_address"),
)
_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

# Categories the extraction model reports that are never stored without the user's own act.
SENSITIVE_CATEGORIES: frozenset[str] = frozenset(
    {"health", "finance", "credentials", "identity", "contact", "beliefs", "sexuality"}
)

_STOPWORDS = frozenset(
    [
        "that",
        "this",
        "with",
        "from",
        "have",
        "been",
        "were",
        "they",
        "them",
        "their",
        "there",
        "what",
        "when",
        "where",
        "which",
        "while",
        "about",
        "would",
        "could",
        "should",
        "might",
        "shall",
        "will",
        "into",
        "than",
        "then",
        "also",
        "just",
        "like",
        "very",
        "more",
        "most",
        "some",
        "such",
        "only",
        "over",
        "your",
        "yours",
        "ours",
        "mine",
        "them",
        "these",
        "those",
        "does",
        "done",
        "doing",
        "being",
        "because",
        "always",
        "never",
        "often",
        "really",
        "prefer",
        "prefers",
        "preferred",
        "wants",
        "want",
        "like",
        "likes",
        "uses",
        "using",
        "used",
    ]
)


@dataclass(frozen=True, slots=True)
class Refusal:
    reason: str  # a short code, never the text that triggered it


def refuse_secret(text: str) -> Refusal | None:
    """A secret in any form: refused for every memory, including ones written by hand."""
    for pattern, reason in _SECRET_PATTERNS:
        if pattern.search(text):
            return Refusal(reason)
    return None


def refuse_personal(text: str) -> Refusal | None:
    """Personal identifiers: refused for automatic storage."""
    for pattern, reason in _PERSONAL_PATTERNS:
        if pattern.search(text):
            return Refusal(reason)
    for match in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return Refusal("card_number")
    return None


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _stems(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", text).lower())
    return {w[:5] for w in words if len(w) >= 4 and w not in _STOPWORDS}


def is_grounded(claim: str, source: str, *, threshold: float = 0.6) -> bool:
    """Whether most of the claim's content words appear in the user's own text. Short claims
    with no content words at all cannot be checked and are not grounded."""
    claim_stems = _stems(claim)
    if not claim_stems:
        return False
    found = len(claim_stems & _stems(source))
    return found / len(claim_stems) >= threshold


def normalize_key(key: str) -> str:
    """A subject key: lower-case words joined by underscores, at most 80 characters."""
    text = unicodedata.normalize("NFKC", key).lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text[:MAX_KEY_CHARS].rstrip("_")


def normalize_content(content: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", content).replace("\x00", "").split())


def content_hash(content: str) -> str:
    """The same statement in a different letter case or spacing is the same memory."""
    return hashlib.sha256(normalize_content(content).casefold().encode()).hexdigest()
