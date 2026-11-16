"""Security helpers: secret redaction, path safety, untrusted-content fencing."""

from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Any

# Patterns are deliberately conservative: they redact the *value* and keep the
# key name so the report still explains which header/parameter was involved.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], Any]] = [
    # Authorization: Bearer xxx / Basic xxx / Token xxx
    (re.compile(r"(?i)\b(authorization\s*[:=]\s*[\"']?(?:bearer|basic|token|jwt)\s+)[A-Za-z0-9._~+/=-]{8,}"),
     r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"), r"\1[REDACTED]"),
    # key=value / "key": "value" / ENV_VAR_KEY=value for typical secret names. The key may carry a
    # prefix (DB_PASSWORD, OPENAI_API_KEY, AWS_SECRET_ACCESS_KEY); quoted values may contain spaces.
    (re.compile(
        r"(?i)([\"']?(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_-])*"
        r"(?:api[_-]?key|apikey|access[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|token|secret|"
        r"client[_-]?secret|password|passwd|pwd|private[_-]?key|session[_-]?id|x-api-key|cookie|set-cookie)"
        r"(?![A-Za-z0-9])[\"']?\s*[:=]\s*)(\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s\"',;&}]{4,})"),
     lambda m: m.group(1) + (m.group(2)[0] + "[REDACTED]" + m.group(2)[0] if m.group(2)[0] in "\"'" else "[REDACTED]")),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED_JWT]"),
    # AWS access key ids
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED_AWS_KEY]"),
    # Common provider token prefixes (GitHub, Slack, OpenAI/Anthropic-style)
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{20,})\b"),
     "[REDACTED_TOKEN]"),
    # PEM private key blocks
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
     "[REDACTED_PRIVATE_KEY]"),
    # Credentials embedded in URLs: scheme://user:pass@host
    (re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s@/]+(@)"), r"\1[REDACTED]\2"),
]


def redact_secrets(text: str) -> tuple[str, int]:
    """Return (redacted_text, number_of_redactions)."""
    total = 0
    for pattern, repl in _SECRET_PATTERNS:
        text, n = pattern.subn(repl, text)
        total += n
    return text, total


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def validate_slug(value: str, what: str = "name") -> str:
    """Validate a project/template/provider identifier (no path separators)."""
    value = value.strip().lower()
    if not _SLUG_RE.match(value):
        raise ValueError(
            f"Invalid {what} {value!r}: use 1-64 chars of lowercase letters, digits, '-' or '_'."
        )
    return value


def safe_join(base: Path, *parts: str) -> Path:
    """Join path components and guarantee the result stays inside *base*."""
    base_resolved = base.resolve()
    candidate = base_resolved.joinpath(*parts).resolve()
    if candidate != base_resolved and base_resolved not in candidate.parents:
        raise ValueError(f"Path {candidate} escapes {base_resolved}")
    return candidate


def fence_untrusted(content: str, label: str = "untrusted_input") -> tuple[str, str]:
    """Wrap untrusted content in nonce-tagged delimiters the content cannot forge.

    Returns (fenced_text, tag_name). Any occurrence of the tag inside the content is
    neutralised so the content can't close the fence early.
    """
    nonce = secrets.token_hex(6)
    tag = f"{label}_{nonce}"
    cleaned = content.replace(tag, tag.replace("_", "-"))
    return f"<{tag}>\n{cleaned}\n</{tag}>", tag


# Whole escape sequences (CSI / OSC) must be matched before lone control bytes.
_CONTROL_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)


def strip_control_chars(text: str) -> str:
    """Remove terminal escape sequences / control chars from untrusted text before display."""
    return _CONTROL_RE.sub("", text)


def escape_markup(text: str) -> str:
    """Make untrusted text literal inside Textual markup.

    textual.markup.escape leaves "[Customers API]" as a (silently dropped) style tag, and a
    trailing backslash would escape our own closing tag, so escape every "[" ourselves.
    """
    return text.replace("[", "\\[") + (" " if text.endswith("\\") else "")
