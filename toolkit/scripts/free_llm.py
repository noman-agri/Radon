#!/usr/bin/env python3
"""
free_llm.py - Radon 1 worker CLI: offload cheap work to a free / local OpenAI-compatible model.

Claude (the orchestrator) reads only the short, locally VERIFIED output - never raw model prose.

  python free_llm.py ping    --provider groq
  python free_llm.py extract --provider ollama --file build.log --hint "root cause of the failure"
  python free_llm.py review  --provider openrouter --file src/train.py

Env (OS env or .env in the current folder): RADON_SCOUT_API_KEY, RADON_SCOUT_MODEL,
optional RADON_SCOUT_BASE_URL (overrides --provider). Ollama needs no key.
Exit codes: 0 = verified output, 2 = fallback (Claude should read the source itself), 1 = usage error.
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import privacy_guard  # noqa: E402
from free_scout_gate import (  # noqa: E402
    ScoutConfig,
    _extract_json_payload,
    _normalize_newlines,
    _read_dotenv,
    call_chat,
    run_free_scout,
)

PRESETS = {
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "github": "https://models.github.ai/inference",
    "cerebras": "https://api.cerebras.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "hf": "https://router.huggingface.co/v1",
    "ollama": "http://localhost:11434/v1",
}

REVIEW_PROMPT = (
    "You are a strict code reviewer. Report only concrete bugs (wrong logic, crashes, data leakage, "
    "security holes, off-by-one, wrong API use). No style comments, no praise. Return ONLY JSON:\n"
    '{"findings": [{"quote": "one exact line copied from CODE", "issue": "<= 20 words"}]}\n'
    "Each quote MUST be copied character-for-character from CODE. At most 8 findings. "
    'If there are no real bugs return {"findings": []}.'
)

CHUNK_CHARS = 20000
GRACE_SEC = 8.0  # after 2 models agree to answer, wait at most this long for a 3rd


SKILL_DOTENV = Path(__file__).resolve().parent.parent / ".env"
SKILL_MODELS = Path(__file__).resolve().parent.parent / "models.env"
# The API key lives OUTSIDE the skill folder, so sharing/zipping/publishing the skill can never leak it.
SECRETS = Path.home() / ".radon" / "secrets.env"


def build_configs(provider: str | None, model: str | None, dotenv: str | None = None,
                  model_var: str = "RADON_SCOUT_MODEL") -> list[ScoutConfig]:
    """
    One config per model. RADON_SCOUT_MODEL may be a comma-separated list: when a model is
    rate-limited or removed, the next one is tried. Lookup order: OS env, ./.env, <skill>/.env.
    """
    file_vals: dict[str, str] = {}
    # later files win; placeholders never override a real value; ~/.radon/secrets.env wins last
    for src in (SKILL_DOTENV, Path(dotenv or ".env"), SECRETS):
        file_vals.update({k: v for k, v in _read_dotenv(src).items() if v and not v.startswith("PASTE_")})
    model_vals = _read_dotenv(SKILL_MODELS)  # non-secret config; wins over model lines in .env
    get = lambda k: (os.environ.get(k) or file_vals.get(k) or model_vals.get(k) or "").strip()  # noqa: E731
    get_m = lambda k: (os.environ.get(k) or model_vals.get(k) or file_vals.get(k) or "").strip()  # noqa: E731
    default_provider = provider or get("RADON_SCOUT_PROVIDER") or None
    entries = [m.strip() for m in (model or get_m(model_var) or get_m("RADON_SCOUT_MODEL")).split(",") if m.strip()]
    cfgs: list[ScoutConfig] = []
    for entry in entries:
        # "gemini:gemini-2.5-flash" -> provider gemini; unprefixed -> default provider
        prov, mid = default_provider, entry
        head, sep, rest = entry.partition(":")
        if sep and head in PRESETS:
            prov, mid = head, rest
        if provider and prov != provider:
            continue  # --provider given: use only that provider's models
        custom = get("RADON_SCOUT_BASE_URL") if prov == default_provider else ""
        base_url = (custom or PRESETS.get(prov or "", "")).rstrip("/")
        key = get(f"RADON_KEY_{prov.upper()}") if prov else ""
        if not key and prov == default_provider:
            key = get("RADON_SCOUT_API_KEY")  # legacy single-key setting
        if not key and prov == "ollama":
            key = "ollama"  # local server ignores it
        if base_url and mid and key and not key.startswith("PASTE_"):
            cfgs.append(ScoutConfig(base_url=base_url, model=mid, api_key=key))
    return cfgs


def build_config(provider: str | None, model: str | None, dotenv: str | None = None) -> ScoutConfig | None:
    cfgs = build_configs(provider, model, dotenv)
    return cfgs[0] if cfgs else None


_ROTATE = ("SCOUT_FALLBACK_HTTP", "SCOUT_FALLBACK_ERROR")


def chat_rotating(cfgs: list[ScoutConfig], system: str, user: str) -> tuple[str | None, str]:
    """Try each model in order; move on only for provider-side failures (rate limit, removed model, timeout)."""
    status = "SCOUT_SKIPPED_UNCONFIGURED"
    for cfg in cfgs:
        privacy_guard.audit(cfg.base_url, cfg.model, user, "chat")
        content, status = call_chat(cfg, system, user)
        if content is not None or not status.startswith(_ROTATE):
            return content, f"{status} via {cfg.model}" if content is not None else status
    return None, status


def scout_rotating(cfgs: list[ScoutConfig], text: str, hint: str) -> tuple[dict | None, str]:
    status = "SCOUT_SKIPPED_UNCONFIGURED"
    for cfg in cfgs:
        privacy_guard.audit(cfg.base_url, cfg.model, text, "extract")
        verified, status = run_free_scout(text, hint, cfg)
        if verified is not None or not status.startswith(_ROTATE):
            return verified, status
    return None, status


def chunk_lines(text: str, limit: int = CHUNK_CHARS) -> list[tuple[int, str]]:
    """Split on line boundaries into (first_line_number, chunk) pieces no longer than `limit` chars."""
    chunks: list[tuple[int, str]] = []
    buf: list[str] = []
    size, start = 0, 1
    for i, line in enumerate(_normalize_newlines(text).split("\n"), start=1):
        if buf and size + len(line) + 1 > limit:
            chunks.append((start, "\n".join(buf)))
            buf, size, start = [], 0, i
        buf.append(line[:limit])
        size += len(line) + 1
    if buf:
        chunks.append((start, "\n".join(buf)))
    return chunks


def verify_review(raw: str, chunk: str, first_line: int) -> tuple[list[tuple[int, str, str]], int, str]:
    """Keep only findings whose quote is an exact line fragment of the chunk. Returns (kept, dropped, status)."""
    try:
        data = json.loads(_extract_json_payload(raw))
    except (json.JSONDecodeError, TypeError):
        return [], 0, "REVIEW_REJECTED_JSON"
    items = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return [], 0, "REVIEW_REJECTED_SCHEMA"
    lines = chunk.split("\n")
    kept: list[tuple[int, str, str]] = []
    dropped = 0
    for it in items[:8]:
        if not isinstance(it, dict):
            dropped += 1
            continue
        quote = str(it.get("quote", "")).strip()
        issue = " ".join(str(it.get("issue", "")).split())[:160]
        # exact whole-line match first; substring match only for distinctive quotes (>= 8 chars),
        # otherwise a tiny quote like "x" or "return" would anchor to the wrong line
        line_no = next((first_line + i for i, ln in enumerate(lines) if quote and ln.strip() == quote), None)
        if line_no is None and len(quote) >= 8:
            line_no = next((first_line + i for i, ln in enumerate(lines) if quote in ln), None)
        if line_no is None or not issue:
            dropped += 1  # hallucinated or empty finding
            continue
        kept.append((line_no, issue, quote[:80]))
    return kept, dropped, "OK"


def cmd_ping(cfgs: list[ScoutConfig]) -> int:
    ok = 0
    for cfg in cfgs:  # test every configured model so the user sees which ones work
        content, status = call_chat(cfg, "Reply with exactly: OK", "ping")
        print(f"{'OK  ' if content else 'FAIL'} {cfg.model} | {status} | reply={(content or '')[:30]!r}")
        ok += content is not None
    return 0 if ok else 2


def cmd_models(provider: str) -> int:
    """List model ids the saved key can use for one provider (no content is sent)."""
    import urllib.request
    cfgs = build_configs(provider, "probe")
    if not cfgs:
        print(f"FALLBACK no key for {provider}: run scripts/set_key.py {provider}")
        return 2
    req = urllib.request.Request(f"{cfgs[0].base_url}/models",
                                 headers={"Authorization": f"Bearer {cfgs[0].api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8")).get("data", [])
    except Exception as exc:  # noqa: BLE001 - report and fall back, never crash
        print(f"FALLBACK {type(exc).__name__}: {getattr(exc, 'code', '')}")
        return 2
    ids = sorted(str(m.get("id", "")).removeprefix("models/") for m in data if isinstance(m, dict))
    print(f"{len(ids)} models for {provider}:")
    print("\n".join(ids[:80]))
    return 0


def cmd_extract(cfgs: list[ScoutConfig], text: str, hint: str) -> int:
    quotes: list[str] = []
    for _, chunk in chunk_lines(text):
        verified, status = scout_rotating(cfgs, chunk, hint)
        if verified is None:
            print(f"FALLBACK {status}: read the source directly.")
            return 2
        quotes += verified["relevant_quotes"]
    if not quotes:
        print("FALLBACK SCOUT_EMPTY_EXTRACTION: read the source directly.")
        return 2
    print(f"VERIFIED_EXTRACTIVE {len(quotes)} quotes ({sum(map(len, quotes))}/{len(text)} chars):")
    print("\n".join(quotes))
    return 0


def swarm_vote(per_model: list[list[tuple[int, str, str]]], min_votes: int) -> list[tuple[int, int, str, str]]:
    """Keep a line only if >= min_votes DIFFERENT models flagged it. Returns (line, votes, issue, quote)."""
    votes: dict[int, list[tuple[str, str]]] = {}
    for findings in per_model:
        for line_no in {f[0] for f in findings}:  # one vote per model per line
            first = next(f for f in findings if f[0] == line_no)
            votes.setdefault(line_no, []).append((first[1], first[2]))
    return sorted((ln, len(v), v[0][0], v[0][1]) for ln, v in votes.items() if len(v) >= min_votes)


def cmd_review(cfgs: list[ScoutConfig] | ScoutConfig, text: str, swarm: int = 3) -> int:
    """
    Swarm review: up to `swarm` different free models review the same code independently.
    Layer 1: every finding must quote a real line (hallucinated quotes dropped).
    Layer 2: a line is reported only if >= 2 models agree (single-model findings are not trusted).
    Layer 3 (caller): Claude verifies each reported line in the real code before acting.
    """
    cfgs = [cfgs] if isinstance(cfgs, ScoutConfig) else cfgs
    chunks = chunk_lines(text)

    def member(cfg: ScoutConfig) -> tuple[list[tuple[int, str, str]], int] | None:
        findings: list[tuple[int, str, str]] = []
        dropped_here = 0
        for first_line, chunk in chunks:
            content, status = chat_rotating([cfg], REVIEW_PROMPT, f"CODE:\n{chunk}")
            kept, d, vstatus = verify_review(content, chunk, first_line) if content else ([], 0, status)
            if vstatus != "OK":
                return None  # this member failed (rate limit / timeout / bad JSON); the others continue
            findings += kept
            dropped_here += d
        return findings, dropped_here

    # All candidates run IN PARALLEL, so one slow or overloaded provider no longer stalls the swarm.
    # Daemon threads: stop waiting once `swarm` members answered, or GRACE_SEC after a 2-model quorum.
    candidates = cfgs[: max(swarm * 2, swarm)]
    done: queue.Queue = queue.Queue()
    for idx, cfg in enumerate(candidates):
        threading.Thread(target=lambda i=idx, c=cfg: done.put((i, member(c))), daemon=True).start()
    results: dict[int, tuple[list[tuple[int, str, str]], int]] = {}
    pending, quorum_at = len(candidates), None
    while pending:
        timeout = None if quorum_at is None else max(0.0, quorum_at + GRACE_SEC - time.monotonic())
        try:
            idx, res = done.get(timeout=timeout)
        except queue.Empty:
            break  # quorum reached and slow members exceeded the grace period
        pending -= 1
        if res is not None:
            results[idx] = res
            if len(results) >= swarm:
                break
            if len(results) >= 2 and quorum_at is None:
                quorum_at = time.monotonic()
    per_model: list[list[tuple[int, str, str]]] = []
    used: list[str] = []
    dropped = 0
    for idx in sorted(results):  # keep configured priority order
        per_model.append(results[idx][0])
        dropped += results[idx][1]
        used.append(candidates[idx].model.split("/")[-1])
    if not per_model:
        print("FALLBACK no swarm member answered: review it yourself.")
        return 2
    min_votes = 2 if len(per_model) >= 2 else 1
    agreed = swarm_vote(per_model, min_votes)
    trust = "consensus" if min_votes == 2 else "SINGLE MODEL - low confidence"
    print(f"REVIEW swarm={len(per_model)} ({', '.join(used)}) | {trust} | {len(agreed)} line(s) | "
          f"{dropped} hallucinated dropped. UNTRUSTED - verify each in the code:")
    for line_no, n, issue, quote in agreed:
        print(f"L{line_no} [{n}/{len(per_model)}]: {issue} | {quote}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows cp1252 consoles must not crash on model text
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Radon 1 free-model worker (verified output only)")
    ap.add_argument("cmd", choices=["ping", "extract", "review", "models"])
    ap.add_argument("--provider", choices=sorted(PRESETS), help="preset base URL (env RADON_SCOUT_BASE_URL wins)")
    ap.add_argument("--model", help="model id (else env RADON_SCOUT_MODEL)")
    ap.add_argument("--file", type=Path, help="input file (else stdin)")
    ap.add_argument("--hint", default="the most relevant facts", help="what to extract")
    ap.add_argument("--swarm", type=int, default=3, help="review: number of independent models that vote")
    ap.add_argument("--private", action="store_true", help="local Ollama only - nothing leaves this PC")
    args = ap.parse_args(argv)

    if args.private:
        args.provider = "ollama"
    if args.cmd == "models":
        return cmd_models(args.provider or "openrouter")
    reason = privacy_guard.block_reason(args.file)
    if reason:
        print(f"BLOCKED {reason}: never sent to an external model. Handle it locally.")
        return 2
    model_var = "RADON_SCOUT_REVIEW_MODEL" if args.cmd == "review" else "RADON_SCOUT_MODEL"
    cfgs = build_configs(args.provider, args.model, model_var=model_var)
    if args.private:
        cfgs = [c for c in cfgs if "localhost" in c.base_url or "127.0.0.1" in c.base_url]
    if not cfgs:
        print(f"FALLBACK SCOUT_SKIPPED_UNCONFIGURED: run scripts/set_key.py to save the key to {SECRETS} "
              "(or use --provider ollama). Do the task without the free model.")
        return 2
    if args.cmd == "ping":
        return cmd_ping(cfgs)
    try:
        text = args.file.read_text(encoding="utf-8-sig", errors="replace") if args.file else sys.stdin.read()
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not text.strip():
        print("FALLBACK SCOUT_SKIPPED_EMPTY")
        return 2
    is_local = all("localhost" in c.base_url or "127.0.0.1" in c.base_url for c in cfgs)
    if not is_local:
        text, counts = privacy_guard.redact(text)
        if counts:
            print("REDACTED before sending: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if args.cmd == "extract":
        return cmd_extract(cfgs, text, args.hint)
    return cmd_review(cfgs, text, swarm=max(1, args.swarm))


if __name__ == "__main__":
    raise SystemExit(main())
