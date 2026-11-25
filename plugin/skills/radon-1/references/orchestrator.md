# ORCH Mode - Claude Orchestrates, Free Models Do Cheap Work

## Division of labour (why this saves tokens without losing quality)

| Role | Who | Does |
| --- | --- | --- |
| Orchestrator + final judge | Claude | plan, hard reasoning, writing code, deciding what is true |
| Checker 1 (free, exact) | local tools | tests, `python -m py_compile`, `ruff`, type checks, schema checks, `rg` |
| Checker 2 (free, fuzzy) | free/local model via `free_llm.py review` | second-opinion bug hunt; findings are UNTRUSTED candidates |
| Worker | free/local model via `free_llm.py extract` | pull relevant lines out of big logs/docs so Claude reads 5% instead of 100% |

## Anti-hallucination layers (free models hallucinate a lot - nothing they say is used unchecked)

| Layer | Where | Guarantee |
| --- | --- | --- |
| 1. Extractive contract + verbatim check | `extract` | every returned quote is an exact substring of the source: hallucinated text is impossible in the output |
| 2. Quote-anchored findings | `review` | each finding must quote a real line; invented lines are dropped and counted |
| 3. Swarm voting | `review --swarm 3` | up to 3 different models review independently; a line is reported only if >= 2 agree. One answering model = marked "SINGLE MODEL - low confidence" |
| 4. Claude verification | orchestrator | Claude reads each reported line in the real code and accepts only what it confirms |
| 5. Deterministic checks | tests / lint / compile | ground truth; always outrank any model opinion |

Layers 1-2 make the *output* hallucination-free; layers 3-5 filter wrong *judgements*. Zero wrong judgements cannot be guaranteed by any model, which is why layer 4-5 always run.

## Data protection (what may leave this PC)

**Trust tiers (set in the toolkit's `models.env`, default `gemini,ollama`):**

| Provider | Code review | Documents | Images |
| --- | --- | --- | --- |
| Trusted (Gemini, local Ollama) | real code, secrets redacted | allowed | allowed (`describe`) |
| Untrusted (OpenRouter, xAI, any other) | masked code only | blocked | blocked |

In one swarm, trusted members review the real code and untrusted members the masked copy; line numbers are identical, so votes still combine.

**Images save Claude tokens:** `free_llm.py describe --file shot.png` sends the image to a trusted vision model and returns text (verbatim text, tables, chart values; unreadable parts marked `[?]`). Claude reads that text instead of the image. Exact numbers that matter must be checked in the image itself.

- **Blocked outright:** `.env*`, key/credential/token/password files, `.pem/.key`, `auth.json`, raw data files (`.csv .parquet .xlsx .db .pkl ...`) -> `BLOCKED`, handle locally.
- **Code is masked before review (default):** every name you wrote becomes `v1`, `v2`..., every string becomes `"S1"`..., comments are removed; only library names (`np.mean`) and structure stay. The provider sees meaningless structure; the mapping stays on this PC and real names are restored locally in the result. Full masking for Python, strings and comments for other languages. `--no-mask` turns it off.
- **Documents never leave by default:** `extract` to a cloud model is blocked unless `--allow-cloud`. Default path: convert locally (`pymupdf4llm` / `markitdown`) and search with `rg` - 0 tokens, 0 bytes sent.
- **Redacted before sending (line structure kept):** access keys, private keys, values of password or secret settings, emails, IPs, long numbers (phone/card/ID), user names in paths. The CLI prints what it redacted.
- **Audit log:** every outbound call appends time, host, model, size and a sha256 to `~/.radon/audit.log` - never the content. Check it any time.
- **`--private`:** Ollama on localhost only; nothing leaves the machine. Use it for client code, personal data, anything confidential.
- Masking hides meaning, not logic: an algorithm's structure is still visible. For code whose structure itself is secret, use `--private` (local Ollama) or skip the free model.

Rules:
- A 7B-class model cannot reliably judge Claude's reasoning. It is a cheap filter, not a judge. Deterministic checks always run first.
- Never send secrets, personal data, or confidential code to a cloud free tier (they may log or train).
- Claude reads only the CLI's verified output, then checks each review finding against the code itself before acting on it.
- If the CLI prints `FALLBACK` (exit code 2), do the step yourself; do not retry in a loop.

## Loop

1. Plan (<= 5 lines) and pick the success check.
2. Large input? `python ~/.radon/toolkit/scripts/free_llm.py extract --file F --hint "<what you need>"` -> read the quotes only.
3. Write or change code.
4. Run deterministic checks; fix failures.
5. Optional for risky code: `python ~/.radon/toolkit/scripts/free_llm.py review --file F` -> verify each `L<n>` candidate, fix only the real ones.
6. Report briefly with what was actually run.

## The Radon toolkit (optional, installed separately)

The free-model CLI is not part of this plugin. The user installs it once from the `toolkit/` folder of https://github.com/noman-agri/Radon into `~/.radon/toolkit` and follows its README there (choosing a provider, saving their own key with the toolkit's key-setup command, testing with `ping`).

- If `~/.radon/toolkit/scripts/free_llm.py` does not exist, or it prints `FALLBACK`, do the work yourself without free models. Never ask the user to paste a key into chat.
- Never read, print, or edit the user's key files; the toolkit handles keys itself.

## Free providers (`--provider` presets)

Free limits and model lists change often - check the link before relying on them. Live community list: https://github.com/cheahjs/free-llm-api-resources

| Preset | Get key / models | Notes |
| --- | --- | --- |
| `ollama` | https://ollama.com/download (models: https://ollama.com/library) | Local, private, unlimited, no key. e.g. `ollama pull qwen2.5-coder:7b`. Needs ~8 GB RAM for 7B. |
| `groq` | https://console.groq.com/keys | Very fast; Llama / Qwen / GPT-OSS models; per-minute and per-day limits. |
| `openrouter` | https://openrouter.ai/keys (free models: https://openrouter.ai/models?max_price=0) | Use model ids ending in `:free`; daily request cap. |
| `gemini` | https://aistudio.google.com/apikey | Gemini Flash models on the free tier; free-tier prompts may be used to improve Google products. |
| `github` | https://github.com/marketplace/models | GitHub token with `models` scope; model ids like `openai/gpt-4o-mini`. |
| `cerebras` | https://cloud.cerebras.ai | Fast Llama / Qwen inference, free daily token quota. |
| `mistral` | https://console.mistral.ai | Free "Experiment" plan; low rate limits. |
| `nvidia` | https://build.nvidia.com | Free credits / rate-limited trial for many open models. |
| `hf` | https://huggingface.co/settings/tokens | Hugging Face Inference Providers router; small monthly free credit. |

Good worker models: small coder/instruct models (Qwen2.5-Coder 7B, Llama 3.1 8B) for extract; the largest free model available (e.g. a 70B on Groq/Cerebras) for review.
