#!/usr/bin/env python3
"""
set_key.py [provider] - paste an API key once (default provider: openrouter; also: gemini, groq, ...).
Saved OUTSIDE the skill folder in ~/.radon/secrets.env as RADON_KEY_<PROVIDER>; other providers' keys
are kept; any legacy key copy inside the skill folder is removed; models are tested. Never printed in full.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SECRETS = Path.home() / ".radon" / "secrets.env"
FORMATS = {
    "openrouter": r"sk-or-v1-[0-9a-f]{64}",
    "gemini": r"(AIza[0-9A-Za-z_-]{35}|AQ\.[A-Za-z0-9_.-]{20,})",
    "groq": r"gsk_[A-Za-z0-9]{40,}",
}


def save(provider: str, key: str) -> None:
    var = f"RADON_KEY_{provider.upper()}"
    lines = SECRETS.read_text(encoding="utf-8").splitlines() if SECRETS.exists() else []
    lines = [ln for ln in lines if not ln.startswith(f"{var}=")]
    if provider == "openrouter":
        lines = [ln for ln in lines if not ln.startswith("RADON_SCOUT_API_KEY=")]  # legacy name
    lines.append(f"{var}={key}")
    SECRETS.parent.mkdir(parents=True, exist_ok=True)
    SECRETS.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    provider = (sys.argv[1] if len(sys.argv) > 1 else "openrouter").lower()
    print(f"{provider} key EKBAR paste korun (Ctrl+V ba mouse right-click), tarpor Enter chapun.\n")
    key = input("Key: ").strip().strip('"').strip("'")
    pattern = FORMATS.get(provider, r"\S{20,}")
    if len(re.findall(pattern, key)) > 1:
        print("\nKey ekadhik bar paste hoyeche. Abar chalan ebong shudhu EKBAR paste korun.")
        return 1
    if not re.fullmatch(pattern, key):
        print(f"\nEta thik {provider} key mone hocche na ({len(key)} okkhor). Abar chalan.")
        return 1
    print("\033[F\033[KKey: (lukano hoyeche)")  # hide the key line after Enter

    save(provider, key)
    old = SKILL_DIR / ".env"  # legacy OpenRouter key location inside the skill folder
    if provider == "openrouter" and old.exists():
        old.unlink()  # replaced by the new key saved outside the skill folder
    print(f"\nSaved {provider}: {key[:6]}...{key[-4:]}  ->  {SECRETS}  (skill folder-er baire)\nTesting...\n")
    free_llm = str(Path(__file__).with_name("free_llm.py"))
    if provider != "openrouter":
        subprocess.call([sys.executable, free_llm, "models", "--provider", provider])
    return subprocess.call([sys.executable, free_llm, "ping"])


if __name__ == "__main__":
    raise SystemExit(main())
