# Radon toolkit (optional)

Python CLI that lets the Radon 1 skill offload cheap work (`extract`, swarm `review`) to free or local LLMs, with verbatim verification, multi-model voting, redaction, and an audit log. Python 3.9+, standard library only. Not part of the plugin itself.

## Install

Copy this `toolkit` folder to `~/.radon/toolkit` (Windows: `%USERPROFILE%\.radon\toolkit`), so the skill can find `~/.radon/toolkit/scripts/free_llm.py`.

## Add a key (once per provider)

```bash
python ~/.radon/toolkit/scripts/set_key.py openrouter   # or: gemini, groq ...
```

Paste the key in the terminal when asked, never in chat. It is saved to `~/.radon/secrets.env`, outside the toolkit and the plugin. Example placeholder format: `YOUR_API_KEY`.

## Use

```bash
python ~/.radon/toolkit/scripts/free_llm.py ping
python ~/.radon/toolkit/scripts/free_llm.py extract --file build.log --hint "root cause"
python ~/.radon/toolkit/scripts/free_llm.py review --file src/app.py
python ~/.radon/toolkit/scripts/free_llm.py review --file src/app.py --private   # local Ollama only
python ~/.radon/toolkit/scripts/free_llm.py describe --file screenshot.png         # image -> text (trusted only)
```

Models and provider order: `models.env` (non-secret). Audit log: `~/.radon/audit.log` (metadata only). Tests: `python -m unittest discover -s ~/.radon/toolkit/tests`.

Free tiers may log or train on prompts. Sensitive files are blocked and secrets are redacted, but use `--private` for confidential code.
