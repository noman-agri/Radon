# Effort Cascade Router, Zero-Trust Free Scout, and A/B Cost Comparator

Load this reference when deploying the Zero-Trust Free Scout pre-processor, local Stage-0 zero-token gating, medium-to-max API effort cascading, or controlled A/B token and accuracy benchmarking.

All code lives in the optional Radon toolkit (`~/.radon/toolkit/scripts/`, Python 3.9+, stdlib only), installed separately from the `toolkit/` folder of https://github.com/noman-agri/Radon. Run or import it; do not paste it into prompts. If it is not installed, use this page as a design reference only.

## 1. Controlled A/B Benchmark Protocol

1. **Lock prompt placement:** Put the charter in the exact same slot (System Prompt) for all Treatment runs; never mix System Prompt and User Message placement.
2. **Control variance:** Use `temperature=0` where the API still accepts it (current Claude models reject sampling parameters - use `n>=3` repeated runs instead), or average `n=3` runs in web chats.
3. **Measure to correctness:** Sum tokens across all turns needed to reach a verified correct answer.
4. **Fair Cost Delta:** Compute aggregate cost savings ONLY across tests where BOTH Baseline and Charter passed accuracy; report pass rates and accuracy regressions separately.
5. **Use real usage logs:** `suite` mode with API `usage` numbers is the source of truth. `text` mode is an estimate only (tiktoken / heuristic tokenizer, not Claude's; no hidden thinking tokens).

## 2. Zero-Trust Free Scout (`free_scout_gate.py`)

Free-tier 7B-8B models hallucinate on open-ended tasks, so the Scout is caged to **extractive-only JSON** and every output is verified locally:

| Guard | Behaviour |
| --- | --- |
| Secret scan (source **and** task hint) | `SCOUT_BLOCKED_SECRET_DETECTED`, no network call |
| Endpoint check | Key sent over HTTPS only (HTTP allowed for localhost) -> else `SCOUT_BLOCKED_INSECURE_ENDPOINT` |
| Size cap | `> 24000` chars -> `SCOUT_SKIPPED_TOO_LARGE` |
| Verbatim check | Any quote not an exact substring of the newline-normalized source -> `HALLUCINATION_REJECTED` |
| ROI gate | Quotes `>= 85%` of source (source >= 200 chars) -> `SCOUT_NO_COMPRESSION_GAIN` |
| Network / provider errors | `SCOUT_FALLBACK_HTTP_<code>` or `SCOUT_FALLBACK_ERROR: <Type>`; never raises |
| Empty extraction | `build_main_context()` returns the full raw document (`SCOUT_EMPTY_EXTRACTION_FALLBACK`) |

Provider and key setup are done by the user with the toolkit's own README (keys never go in chat, code, or this plugin). Check the provider's current base URL and model id in its docs before use (e.g. GitHub Models has moved endpoints over time). Free tiers may log or train on prompts: secret scanning does not catch personal or confidential data, so do not send such documents.

Tier 1 -> Tier 2 glue (always use this, never index the raw result yourself):

```python
from free_scout_gate import build_main_context
context_for_main, scout_status = build_main_context(raw_doc, task_hint="extract error causes")
```

## 3. Effort Cascade Router (`effort_cascade_router.py`)

Flow: **Stage-0 local gate -> exact cache -> `medium` pass -> verifier -> at most one `xhigh` escalation.**

```python
from effort_cascade_router import RouterState, run_cascade

state = RouterState(task_id="project_alpha")
result = run_cascade(user_text, state, llm_call_fn=my_client_call)        # my_client_call(prefix, tail, effort) -> str
retry  = run_cascade(user_text, state, my_client_call, use_cache=False)   # force a fresh call
```

Behaviour covered by `tests/test_radon.py`:

- `/bliend`, `/bliend session`, `/bliend off` parsing that never corrupts paths like `/blind/test.py`.
- Greetings, empty input, and 1-char pings answered locally at 0 tokens - **except** when the previous answer ended with a question: then `y` / `n` / `2` is forwarded together with that question (`[PREVIOUS_ASSISTANT_QUESTION]`), and the cache key includes the question so one `y` never returns the answer to another.
- CommonMark fence parser: info strings (`python title=x.py`, `py {linenos}`), tilde fences, inline triple backticks, unclosed/truncated fences; `>>>` doctest transcripts are not false-failed.
- `SYNTAX_ONLY` = parses with `ast.parse`; it is **not** a correctness pass. Plug a real test runner into `verifier_fn` to get `VERIFIED_PASS`.
- Task-isolated, bounded LRU cache keyed on text, task, HOT_INDEX, pending question, static-prefix hash, flags, model, and efforts; `use_cache=False` for retries or time-dependent requests.
- Exceptions from `llm_call_fn` propagate; put retry/backoff in the client (SDK `max_retries`).

Cache economics to respect when wiring `llm_call_fn` (Claude):

- `DEFAULT_STATIC_PREFIX` is ~400 tokens, **below every model's minimum cacheable prefix (512-4096)**. Put it together with other stable content (tool schemas, repo map) above that minimum, mark it with `cache_control`, and confirm `usage.cache_read_input_tokens > 0`.
- The escalation changes `effort`, which invalidates the messages cache for that call.

## 4. Weighted A/B Cost Comparator (`ab_token_compare.py`)

```bash
python ~/.radon/toolkit/scripts/ab_token_compare.py init-template --out ab_results.json   # refuses to overwrite without --force
python ~/.radon/toolkit/scripts/ab_token_compare.py suite --input ab_results.json
python ~/.radon/toolkit/scripts/ab_token_compare.py text --base-in a_in.txt --base-out a_out.txt --treat-in b_in.txt --treat-out b_out.txt
```

- Default weights follow Claude list pricing relative to 1 uncached input token: output/thinking `5.0`, cache write `1.25` (5-min TTL; use `2.0` for 1-hour), cache read `0.10`. Override for your model (`--cache-read-weight 0.05` on Opus 5.5 / Sonnet 5.5, `0.025` on Fable 5.x).
- `thinking_included_in_output` defaults to `true` because Claude `usage.output_tokens` already includes thinking tokens; set `false` only if your log reports thinking separately.
- Unknown JSON keys are rejected (a typo such as `cached_read` would otherwise silently under-count cost). `notes` / `comment` keys are allowed.
- Per-run fields: `uncached_in`, `output`, `passed` (required); `turns`, `cached_read_in`, `cache_write_in`, `thinking`, `thinking_included_in_output` (optional).
