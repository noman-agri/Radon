# Radon 1

A token-lean, maximum-reasoning work protocol (skill) for Claude Code, with an optional **orchestrator mode** that offloads cheap work to a swarm of free / local LLMs and verifies everything they return.

## What it does

- **Lean protocol (`SKILL.md`)**: reasoning steps, hard token rules, and a quality floor (token saving never skips verification).
- **Knowledge primers**: short domain briefs (ML, data analysis, software) loaded before unfamiliar work.
- **`/orch` mode**: Claude plans and judges; free models extract from large files and give a second-opinion code review.
- **Anti-hallucination layers**: verbatim-quote verification, quote-anchored findings, multi-model swarm voting (>= 2 must agree), final verification by Claude, deterministic tests on top.
- **Data protection**: blocks sensitive files (`.env`, keys, credentials, raw data), redacts secrets / emails / IPs before sending, metadata-only audit log, `--private` mode (local Ollama only).

## License

**Proprietary - All Rights Reserved.** The source is visible for viewing only. Copying, modifying,
redistributing, or building customized versions of this skill requires written permission.
See [LICENSE](LICENSE).

## Install (owner / permitted users)

```bash
git clone https://github.com/noman-agri/Radon.git ~/.claude/skills/radon-1
```

Python 3.9+ (stdlib only). Run tests: `python -m unittest discover -s tests`.

## Free models (optional)

```bash
python scripts/set_key.py openrouter   # or: gemini, groq ...
python scripts/free_llm.py ping
python scripts/free_llm.py review --file path/to/code.py
```

Keys are stored in `~/.radon/secrets.env`, **outside** the skill folder, and are never committed. Model list: `models.env`. Providers and links: `references/orchestrator.md`.

Free tiers may log or train on prompts. Do not send confidential code; use `--private` (local Ollama) instead.
