#!/usr/bin/env python3
"""
free_scout_gate.py - Radon 1 Zero-Trust Anti-Hallucination Free-Tier Scout.

Zero external dependencies (Python stdlib only). Every failure path returns
(None, reason) so the caller falls back to the raw document; nothing here raises
on network, provider, or model misbehaviour.

Typical use:
    context, status = build_main_context(raw_doc, task_hint="extract error causes")
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCOUT_SYSTEM_PROMPT = (
    "You are a strict extractive filter. You MUST NOT summarize in your own words, "
    "invent facts, or give advice. Return ONLY valid JSON with this exact schema:\n"
    '{"relevant_quotes": ["exact verbatim substring from SOURCE"], "keys": ["exact keyword from SOURCE"]}\n'
    "Every string in relevant_quotes MUST appear character-for-character inside SOURCE. "
    "If nothing is relevant, return {\"relevant_quotes\": [], \"keys\": []}."
)

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),          # GitHub classic tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),        # GitHub fine-grained PAT
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),                 # OpenAI / Anthropic (sk-ant-...)
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),                # Google API key
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),           # AWS access key id
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),          # Slack token
)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_ENV_KEYS = ("RADON_SCOUT_BASE_URL", "RADON_SCOUT_MODEL", "RADON_SCOUT_API_KEY")


def _read_dotenv(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader (no interpolation). Missing/unreadable file -> {}."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        values[key] = val
    return values


@dataclass(frozen=True)
class ScoutConfig:
    base_url: str
    model: str
    api_key: str
    timeout_sec: int = 40  # thinking models (Gemini 3.x) need longer than 15s
    max_source_chars: int = 24000
    max_compression_ratio: float = 0.85

    def __repr__(self) -> str:  # never leak the key into logs/tracebacks
        return f"ScoutConfig(base_url={self.base_url!r}, model={self.model!r}, api_key='***')"

    @classmethod
    def from_env(cls, dotenv_path: str | Path | None = ".env") -> ScoutConfig | None:
        """OS environment wins; a local .env only fills variables that are unset."""
        file_vals = _read_dotenv(Path(dotenv_path)) if dotenv_path else {}
        get = lambda k: (os.environ.get(k) or file_vals.get(k) or "").strip()  # noqa: E731
        base_url, model, api_key = (get(k) for k in _ENV_KEYS)
        base_url = base_url.rstrip("/")
        if not (base_url and model and api_key):
            return None
        return cls(base_url=base_url, model=model, api_key=api_key)


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def contains_secret(text: str) -> bool:
    """Detect private keys or live API token prefixes before any outbound free-tier call."""
    return any(pat.search(text) for pat in _SECRET_PATTERNS)


def _endpoint_is_safe(base_url: str) -> bool:
    """Bearer keys may only travel over HTTPS (plain HTTP allowed for localhost only)."""
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme == "https" and parsed.hostname:
        return True
    return parsed.scheme == "http" and (parsed.hostname or "") in _LOCAL_HOSTS


def _extract_json_payload(raw: str) -> str:
    """Extract JSON from fenced blocks or outermost braces even if a 7B model adds preamble."""
    text = raw.strip()
    m = re.search(r"```(?:json)?[ \t]*\n?(.*?)\n?```", text, flags=re.DOTALL | re.IGNORECASE)
    if m and m.group(1).strip().startswith("{"):
        return m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1].strip()
    return text


def verify_extractive_json(
    raw_output: str,
    source_text: str,
    max_compression_ratio: float = 0.85,
) -> tuple[dict[str, list[str]] | None, str]:
    """
    Deterministic anti-hallucination gate:
    1. Must parse as a JSON object with list[str] 'relevant_quotes' and optional list[str] 'keys'.
    2. Every quote MUST be an exact verbatim substring of the newline-normalized source.
    3. Deduplicates quotes and enforces compression ROI (< max_compression_ratio of source).
    """
    if not isinstance(raw_output, str):
        return None, "SCOUT_REJECTED_SCHEMA: model content is not text"
    try:
        parsed: Any = json.loads(_extract_json_payload(raw_output))
    except json.JSONDecodeError as exc:
        return None, f"SCOUT_REJECTED_JSON: {exc.msg}"

    if not isinstance(parsed, dict):
        return None, "SCOUT_REJECTED_SCHEMA: root is not a JSON object"

    quotes = parsed.get("relevant_quotes")
    keys = parsed.get("keys")
    if keys is None:
        keys = []
    if not isinstance(quotes, list) or not all(isinstance(q, str) for q in quotes):
        return None, "SCOUT_REJECTED_SCHEMA: 'relevant_quotes' must be list[str]"
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        return None, "SCOUT_REJECTED_SCHEMA: 'keys' must be list[str]"

    norm_source = _normalize_newlines(source_text)
    verified_quotes: list[str] = []
    seen_quotes: set[str] = set()
    for q in quotes:
        q_clean = _normalize_newlines(q).strip()
        if not q_clean or q_clean in seen_quotes:
            continue
        if q_clean not in norm_source:
            return None, f"HALLUCINATION_REJECTED: quote not found verbatim in source ({q_clean[:40]!r})"
        seen_quotes.add(q_clean)
        verified_quotes.append(q_clean)

    if verified_quotes and len(norm_source) >= 200:
        extracted_len = sum(len(q) for q in verified_quotes)
        if extracted_len >= len(norm_source) * max_compression_ratio:
            return None, "SCOUT_NO_COMPRESSION_GAIN"

    norm_source_lower = norm_source.lower()
    verified_keys: list[str] = []
    seen_keys: set[str] = set()
    for k in keys:
        k_clean = k.strip()
        k_low = k_clean.lower()
        if k_clean and k_low not in seen_keys and k_low in norm_source_lower:
            seen_keys.add(k_low)
            verified_keys.append(k_clean)

    return {"relevant_quotes": verified_quotes, "keys": verified_keys}, "VERIFIED_EXTRACTIVE"


def run_free_scout(
    source_text: str,
    task_hint: str,
    config: ScoutConfig | None = None,
) -> tuple[dict[str, list[str]] | None, str]:
    """
    Call a free OpenAI-compatible endpoint at temperature=0 and verify output deterministically.
    Returns (verified_dict, status) or (None, fallback_reason). Never raises.
    """
    cfg = config or ScoutConfig.from_env()
    if cfg is None:
        return None, "SCOUT_SKIPPED_UNCONFIGURED"
    if not source_text.strip():
        return None, "SCOUT_SKIPPED_EMPTY"
    if len(source_text) > cfg.max_source_chars:
        return None, "SCOUT_SKIPPED_TOO_LARGE"
    if contains_secret(source_text) or contains_secret(task_hint):
        return None, "SCOUT_BLOCKED_SECRET_DETECTED"
    content, status = call_chat(cfg, SCOUT_SYSTEM_PROMPT, f"TASK_HINT: {task_hint}\n\nSOURCE:\n{source_text}")
    if content is None:
        return None, status
    return verify_extractive_json(content, source_text, cfg.max_compression_ratio)


def call_chat(cfg: ScoutConfig, system_prompt: str, user_content: "str | list") -> tuple[str | None, str]:
    """One OpenAI-compatible chat call at temperature=0. Returns (content, "OK") or (None, reason). Never raises."""
    texts = [user_content] if isinstance(user_content, str) else [p.get("text", "") for p in user_content if isinstance(p, dict)]
    if any(contains_secret(t) for t in texts):
        return None, "SCOUT_BLOCKED_SECRET_DETECTED"
    if not _endpoint_is_safe(cfg.base_url):
        return None, "SCOUT_BLOCKED_INSECURE_ENDPOINT"

    endpoint = f"{cfg.base_url}/chat/completions"
    payload = {
        "model": cfg.model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout_sec) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as exc:
        return None, f"SCOUT_FALLBACK_HTTP_{exc.code}"
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        # OSError covers URLError, timeouts, ConnectionReset/RemoteDisconnected;
        # ValueError covers JSONDecodeError and UnicodeDecodeError.
        return None, f"SCOUT_FALLBACK_ERROR: {type(exc).__name__}"
    if not isinstance(content, str):
        return None, "SCOUT_FALLBACK_ERROR: empty_content"
    return content, "OK"


def build_main_context(
    raw_doc: str,
    task_hint: str,
    config: ScoutConfig | None = None,
) -> tuple[str, str]:
    """
    Safe glue for Tier 1 -> Tier 2: returns verified quotes, or the FULL raw document
    whenever the scout failed OR extracted nothing (never hands the main model an empty context).
    """
    verified, status = run_free_scout(raw_doc, task_hint, config)
    if verified and verified["relevant_quotes"]:
        return "\n".join(verified["relevant_quotes"]), status
    if verified is not None:
        status = "SCOUT_EMPTY_EXTRACTION_FALLBACK"
    return raw_doc, status
