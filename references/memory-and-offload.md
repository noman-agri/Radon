# Brain-Style Graph Memory, KV-Cache Layout, and Open-Source Offload Stack

Load this reference when compacting long multi-turn sessions, structuring byte-locked API prefixes, or offloading documents, codebases, data, and videos to local open-source tools.

## 1. Three-Layer Brain-Style Memory Model

Store raw transcripts outside the prompt; keep only verified working memory and associative source pointers inside context:

- **L1 Working Memory (`HOT_INDEX`):** Compact snapshot of active task ID, verified paths, schemas, locked decisions, and open unknowns. Answer directly from L1 without extra tool calls ONLY when `modified_since_check` is `none` and `task_id` matches the active task.
- **L2 Symbolic Pointer Graph:** After completing a subtask, consolidate raw logs and failed drafts into short triples anchored to exact locations:

```text
[E01:CacheMiss] --caused_by--> [E02:TimestampInZone0] @src/api_client.py:28
[E03:ParserFix] --verified_by--> [pytest:18_passed] @tests/test_router.py
```

- **Vector Index vs Prompt Boundary:** Use numeric vector embeddings strictly inside local storage (`sqlite-vec` / `NetworkX` / `LightRAG`) for fast semantic recall. Never inject raw float vectors into the LLM prompt; inject only compact symbolic triples and verified excerpts.

### Universal `HOT_INDEX` Template

```text
[HOT_INDEX - L1 VERIFIED]
- task_id: project_alpha_refactor | last_verified: turn_4
- modified_since_check: none
- verified_facts:
  - src/router.py (L1-160): Stage-0 local gate + bounded single escalation
  - api_contract: static_prefix byte-locked; dynamic tail holds diff + prompt
- locked_decisions: unified diffs on files >40 lines; no unverified pass claims
- open_unknowns: production rate-limit headers not yet inspected
```

## 2. Byte-Locked 4-Zone KV-Cache Prefix Layout

Prefix caching (Claude Prompt Caching and Gemini Context Caching) requires byte-identical tokens from index 0:

```text
[ZONE 0 - IMMUTABLE CORE]
Radon 1 core charter, integrity rules, settled-answer rule, output contract.

[ZONE 1 - STATIC REFERENCE]
Tool schemas, compressed repo map, domain specs, canonical templates.

[ZONE 2 - MILESTONE POINTER GRAPH]
Bounded HOT_INDEX + Symbolic Pointer Graph (updated only at subtask milestones).

[ZONE 3 - VOLATILE TURN TAIL (ALWAYS LAST)]
Timestamps, turn IDs, git diff -U3, exact error trace, current user prompt.
```

- Never place dates, request IDs, or dynamic counters in Zones 0-2.
- **Claude render order is `tools` -> `system` -> `messages`.** Tool schemas are therefore cached *ahead of* the Zone 0 charter: any change to the tool list (order, descriptions, add/remove) invalidates Zones 0-2 too. Keep the tool set deterministic per route.
- **Minimum cacheable prefix:** Claude silently skips caching below 512 tokens (Opus 5.5 and newer) and up to 1024-4096 tokens on older models; Zones 0-1 combined must exceed the active model's minimum. Verify `usage.cache_read_input_tokens > 0` on the second call - zero means nothing was cached.
- **Effort/thinking changes are NOT free:** on Claude, changing top-level `effort` or `thinking` always invalidates the messages cache (and, model-specifically, tools/system too). Pin effort per route; for a mid-conversation effort change use the per-message effort system message where the model supports it. The Tier-2 escalation therefore pays one cache miss - another reason it is capped at one.

## 3. Universal Open-Source Offload Stack (Lazy On-Demand)

Start with the lightweight **Core 4** (covers ~90% of workflows without bloating local RAM); install heavy specialists only when a specific task requires them:

### Core 4 (Default Lightweight Stack)

1. **Documents (PDF/DOCX/PPTX/XLSX/HTML):** Microsoft `markitdown` (general) or `pymupdf4llm` (fast PDF to Markdown) at 0 LLM tokens.
2. **Codebases (Any Language):** `ripgrep` (`rg -n`) for exact line search + `repomix --compress` (Tree-sitter signature map) + `git diff -U3`. Always read full function bodies for logic debugging.
3. **Tabular Data and Logs (CSV/Parquet/JSON):** `DuckDB` to run local SQL aggregations, schema checks, and anomaly filters before sending tiny summaries to the LLM.
4. **Multi-Cloud Routing and Caching:** `LiteLLM` for unified Claude/Gemini routing, static Prompt Caching headers, per-turn `effort` control, and Batch API (50% discount for non-urgent batch jobs).

### On-Demand Specialists (Install Only When Needed)

| Scenario | Open-Source Tool | Important Constraint |
| --- | --- | --- |
| Complex scanned PDFs and OCR tables | IBM `docling` | Heavy CPU/RAM; use only when `markitdown`/`pymupdf4llm` fails |
| Long video or audio lectures | `yt-dlp` / `youtube-transcript-api` / `faster-whisper` | Extract timestamped text instead of sending raw media |
| Very long prose RAG compression | Microsoft `LLMLingua-2` | Use ONLY on prose/articles; never run on code, JSON, or math |
| Local hybrid memory graph | `SQLite (FTS5 + sqlite-vec)` + `NetworkX` (or `LightRAG` / `Graphiti`) | Keep local index numeric, prompt injection symbolic |
| Automated A/B prompt evaluation | `Promptfoo` | Gate prompt changes on accuracy + weighted cost before adoption |

- **Data Serialization Rule:** Use compact pipe tables or TSV for read-only summaries, and native compact JSON whenever exact nesting or strict types are required (avoid custom escape dialects in prompts).
