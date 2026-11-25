# Token Playbook - Map, Locate, Zoom, Verify

Most token waste in coding sessions is tool output nobody needed: full logs, whole files, verbose test runs. Output compressors cut it by rewriting what the tool returned, which can silently drop the one line that mattered. Radon removes the waste at the source instead: ask for the summary first, then zoom into exactly what is needed. Nothing is hidden - every detail is one targeted command away.

## The loop

1. **Map** - get the shape: file list, function index, test summary, `--stat`.
2. **Locate** - find the exact spot: `rg -n`, the first failing test, the error line.
3. **Zoom** - read only that range in full (never truncate code you will edit).
4. **Verify** - re-run the check in quiet mode; show only the result.

## Measured on a real repository (chars / 4 ~ tokens)

| Need | Wasteful | Tokens | Radon way | Tokens | Saved |
| --- | --- | ---: | --- | ---: | ---: |
| Did tests pass? | `unittest -v` | 1313 | quiet run, last 3 lines | 33 | 97% |
| What changed in a commit? | `git show HEAD` | 5940 | `git show --stat` | 377 | 94% |
| Recent history | `git log` | 426 | `git log --oneline -n 10` | 56 | 87% |
| Which files exist? | `ls -laR` | 820 | `git ls-files` | 194 | 76% |
| Where is the logic? | read whole file | 4100 | `grep -n '^def \|^class '` then read range | 254 + range | 70-90% |
| Where is X used? | `grep -rn X` | 982 | `rg -l X`, then `rg -n` in 1 file | 66 + hits | 80-93% |

## Recipes

**Git**: `git status -sb` | `git diff --stat` then `git diff -U2 -- <file>` | `git log --oneline -n 10` | `git show --stat <sha>`.

**Tests**: `pytest -q --tb=short -x` | `python -m unittest -q` | `npm test -- --silent` | `go test ./... 2>&1 | tail -20`. On failure, re-run only the first failing test with its full traceback.

**Search**: `rg -l PATTERN` (files only) -> `rg -n -m 20 --max-columns 200 PATTERN <file>`. Exclude `node_modules`, `.venv`, `dist`, data folders.

**Reading code**: symbol map first (`grep -n '^def \|^class \|^function \|^export '`), then Read with offset/limit around the target. Read the full function you will change, never a fragment.

**Logs and build output**: `tail -n 80` | `grep -n -m 30 -iE 'error|exception|fail'` | build with `--quiet` and filter to errors and warnings.

**Installs**: `pip install -q` | `npm ci --silent --no-audit --no-fund`. Report only failures.

**Data**: `df.shape`, `df.dtypes`, `df.head(5)`, `df.describe()`, `df.isna().mean()` - never print a whole dataframe or array. For CSV/Parquet, aggregate with DuckDB or pandas and print the summary.

**Listings**: `git ls-files` | `tree -L 2 -I 'node_modules|.venv|.git'`.

## Documents (PDF, DOCX, PPTX, XLSX, HTML, scans)

Never feed a large document to the model page by page. Convert it once, locally, to Markdown (0 LLM tokens), then Map/Locate/Zoom inside the Markdown with `rg`. Conversion is lossless for text: nothing is summarised.

| Document | Tool (open source, local) | Why |
| --- | --- | --- |
| Normal digital PDF | `pymupdf4llm` | fast; keeps headings, lists, tables as Markdown; per-page chunks |
| DOCX / PPTX / XLSX / HTML / mixed | Microsoft `markitdown` | one tool for most office formats |
| Complex layout, multi-column, heavy tables | IBM `docling` | best table and layout fidelity; slower, more RAM |
| Scanned / image-only PDF | `ocrmypdf` (Tesseract), then `pymupdf4llm` | adds a text layer first |
| Just need raw text, fastest | `pdftotext -layout` (poppler) | no formatting, very small output |

Install with each tool's own instructions (see their project pages); use them only if the user has them or agrees to install.

Pipeline:
1. **Convert**: e.g. `pymupdf4llm` to `doc.md`, keeping page markers so facts can be cited by page.
2. **Map**: headings only (`rg -n '^#' doc.md`), page count, table count.
3. **Locate**: `rg -n -i 'keyword' doc.md` for the parts the task needs.
4. **Zoom**: read only those sections in full.
5. **Quality check** before trusting the conversion: page count matches the source; numbers in one table spot-checked against the original; if a page converted empty or garbled (scan, odd font), re-run that page with OCR or read that single page directly.

Measure the gain each time: `wc -c doc.md` (chars / 4 ~ tokens) versus what reading the original would cost.

## Context hygiene

- Never re-read a file you just edited; never re-run a command whose output is still in context.
- Keep a mental index (`file:line -> what`) instead of re-opening files.
- `/compact` with a focus when the session is long, e.g. `/compact keep: decisions, file paths, failing tests, next step`. `/clear` between unrelated tasks.
- Biggest hidden cost: every installed plugin, MCP server, and skill adds text to every session start. Disable the ones you do not use (`/plugin`, `/mcp`).

## Response hygiene

Answer first; no restating the question; no closing recap of what the user just saw; show diffs or the changed lines, not whole files; one table, not the same facts twice.

## Never do (where token savers lose quality)

- Lossy-compress or summarise source code before editing it.
- Let a model summarise a stack trace or error message - read the real lines.
- Truncate inside the function, test, or config block you are about to change.
- Skip the verification run to save tokens (the quality floor in SKILL.md wins).
