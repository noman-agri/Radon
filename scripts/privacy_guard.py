#!/usr/bin/env python3
"""
privacy_guard.py - everything that leaves the machine passes through here first.

1. block_reason(path): refuse sensitive files outright (.env, keys, credentials, raw data dumps).
2. redact(text): replace secrets / personal data with placeholders, line structure preserved
   (so line numbers and verbatim checks still work on the redacted text).
3. audit(...): append one metadata line per outbound call to ~/.radon/audit.log
   (time, provider, model, sha256 + size of what was sent) - never the content.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

AUDIT_LOG = Path.home() / ".radon" / "audit.log"

# Always blocked, whatever the extension.
BLOCKED_NAMES = re.compile(
    r"(^\.env(\..*)?$|^id_rsa|^id_ed25519|\.pem$|\.key$|\.p12$|\.pfx$|\.kdbx$|^auth\.json$|\.env$)",
    re.IGNORECASE,
)
# Blocked only for config/data-like files: tokenizer.py or password_policy.md are code/docs, not secrets.
SENSITIVE_WORDS = re.compile(r"secret|credential|password|passwd|token", re.IGNORECASE)
CODE_OR_DOC_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".rb", ".php", ".cs",
                        ".cpp", ".c", ".h", ".kt", ".swift", ".md", ".rst", ".ipynb", ".sql", ".sh", ".ps1"}
BLOCKED_DATA_SUFFIXES = {".csv", ".parquet", ".xlsx", ".xls", ".db", ".sqlite", ".feather", ".pkl", ".npy"}

_REDACTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("API_KEY", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                           r"|AIza[0-9A-Za-z_-]{30,}|(?:AKIA|ASIA)[0-9A-Z]{16}|xox[abprs]-[A-Za-z0-9-]{10,})")),
    # Only literal values: a quoted string, or an unquoted config value that ends the line.
    # Code expressions like `api_key=key))` must stay intact, or reviewers see broken code.
    ("SECRET", re.compile(r"(?im)\b(password|passwd|pwd|secret|token|api[_-]?key)\b(\s*[:=]\s*)"
                          r"(?:(['\"])[^'\"\s]{4,}\3|()[^\s'\"(),;\[\]{}]{4,}(?=[ \t]*$))")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("IP", re.compile(r"\b(?!127\.0\.0\.1\b|0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}\b")),  # keep loopback
    ("NUMBER", re.compile(r"(?<![\w.])\+?\d[\d -]{8,}\d(?![\w.])")),  # phone / card / national-id-like
    ("USER_PATH", re.compile(r"(?i)\b([A-Z]:\\Users\\|/home/|/Users/)[^\\/\s'\"]+")),
)


def block_reason(path: Path | None) -> str | None:
    """Return why a file must never be sent, or None if it may be sent (after redaction)."""
    if path is None:
        return None
    name = path.name
    if BLOCKED_NAMES.search(name):
        return f"sensitive file name ({name})"
    if SENSITIVE_WORDS.search(name) and path.suffix.lower() not in CODE_OR_DOC_SUFFIXES:
        return f"sensitive file name ({name})"
    if path.suffix.lower() in BLOCKED_DATA_SUFFIXES:
        return f"raw data file ({path.suffix}) - summarise locally (duckdb/pandas) instead"
    return None


def redact(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}

    def sub(label: str, pat: re.Pattern[str], s: str) -> str:
        def repl(m: re.Match[str]) -> str:
            counts[label] = counts.get(label, 0) + 1
            if label == "SECRET":
                return f"{m.group(1)}{m.group(2)}<SECRET>"
            if label == "USER_PATH":
                return f"{m.group(1)}<USER>"
            if label == "PRIVATE_KEY":
                return "\n".join("<PRIVATE_KEY>" for _ in m.group(0).split("\n"))  # keep line count
            return f"<{label}>"
        return pat.sub(repl, s)

    for label, pat in _REDACTIONS:
        text = sub(label, pat, text)
    return text, counts


def audit(provider_url: str, model: str, payload: str, purpose: str) -> None:
    """Metadata-only log line; failures to log never break the call."""
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        host = re.sub(r"^https?://([^/]+).*$", r"\1", provider_url)
        line = (f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}\t{purpose}\t{host}\t{model}"
                f"\t{len(payload)} chars\tsha256:{digest}\n")
        with AUDIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
