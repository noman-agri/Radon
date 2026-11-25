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


# ---------------------------------------------------------------------------
# Code masking: send the STRUCTURE of code, not its meaning.
# Identifiers -> v1, v2 ...; string literals -> "S1" ...; comments removed.
# Line numbers are preserved so findings map back exactly; the mapping never leaves this PC.
# ---------------------------------------------------------------------------
import builtins as _builtins
import io as _io
import keyword as _keyword
import tokenize as _tokenize

_KEEP = set(_keyword.kwlist) | set(getattr(_keyword, "softkwlist", [])) | set(dir(_builtins)) | {"self", "cls"}


def mask_python(code: str) -> tuple[str, dict[str, str]]:
    """Mask a Python source. Returns (masked_code, {mask: original}). Raises ValueError if not tokenizable."""
    lines = code.splitlines(keepends=True)
    try:
        toks = list(_tokenize.generate_tokens(_io.StringIO(code).readline))
    except (_tokenize.TokenError, IndentationError, SyntaxError) as exc:
        raise ValueError(f"not valid Python: {exc}") from exc
    imported: set[str] = set()
    starts = (_tokenize.NEWLINE, _tokenize.NL, _tokenize.INDENT, _tokenize.DEDENT)
    for i, t in enumerate(toks):  # keep library names, aliases and imported symbols (needed to judge API use)
        if t.type == _tokenize.NAME and t.string in ("import", "from") and (i == 0 or toks[i - 1].type in starts):
            j = i + 1
            while j < len(toks) and toks[j].type not in (_tokenize.NEWLINE, _tokenize.ENDMARKER):
                if toks[j].type == _tokenize.NAME and toks[j].string not in _KEEP:
                    imported.add(toks[j].string)
                j += 1
    names: dict[str, str] = {}
    strings: dict[str, str] = {}
    reverse: dict[str, str] = {}
    edits = []
    for k, t in enumerate(toks):
        lib_attr = k >= 2 and toks[k - 1].string == "." and toks[k - 2].string in imported  # e.g. np.mean
        if t.type == _tokenize.NAME and t.string not in _KEEP and t.string not in imported and not lib_attr:
            new = names.setdefault(t.string, f"v{len(names) + 1}")
        elif t.type == _tokenize.STRING:
            key = strings.setdefault(t.string, f'"S{len(strings) + 1}"')
            new = key + "\n" * (t.end[0] - t.start[0])  # keep line count for multi-line strings
        elif t.type == _tokenize.COMMENT:
            new = ""
        else:
            continue
        reverse[new.strip()] = t.string if t.type != _tokenize.STRING else "<string>"
        edits.append((t.start, t.end, new))
    for (sr, sc), (er, ec), new in reversed(edits):  # apply from the end so positions stay valid
        head = lines[sr - 1][:sc]
        tail = lines[er - 1][ec:]
        if "\n" in new:  # multi-line string: first line gets the mask, blank lines keep numbering
            first, rest = new.split("\n", 1)
            lines[sr - 1:er] = [head + first + "\n"] + ["\n"] * (er - sr - 1) + [tail]
        else:
            lines[sr - 1:er] = [head + new + tail]
    return "".join(lines), {k: v for k, v in reverse.items() if k}


def mask_generic(code: str) -> tuple[str, dict[str, str]]:
    """Non-Python fallback: hide string literals and comments (identifiers stay). Line count preserved."""
    mapping: dict[str, str] = {}
    def s_repl(m: re.Match[str]) -> str:
        key = f'"S{len(mapping) + 1}"'
        mapping[key] = "<string>"
        return key
    out = re.sub(r"/\*[\s\S]*?\*/", lambda m: "\n" * m.group(0).count("\n"), code)
    out = re.sub(r"(?m)(^|\s)(//|#).*$", r"\1", out)
    out = re.sub(r"\"(?:\.|[^\"\\n])*\"|'(?:\.|[^'\\n])*'|`(?:\.|[^`\])*`", s_repl, out)
    return out, mapping


def mask_code(code: str, filename: str = "") -> tuple[str, dict[str, str], str]:
    """Best available masking. Returns (masked, mapping, level) with level 'full' or 'partial'."""
    if filename.endswith(".py") or not filename:
        try:
            masked, mapping = mask_python(code)
            return masked, mapping, "full"
        except ValueError:
            pass
    masked, mapping = mask_generic(code)
    return masked, mapping, "partial"


def unmask(text: str, mapping: dict[str, str]) -> str:
    """Restore real names in model output (only identifiers; strings stay hidden as <string>)."""
    return re.sub(r"\bv\d+\b|\"S\d+\"", lambda m: mapping.get(m.group(0), m.group(0)), text)
