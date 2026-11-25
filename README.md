<p align="center">
  <img src="assets/radon-logo.png" alt="Radon 1" width="180">
</p>

<h1 align="center">Radon 1</h1>

<p align="center">
  Token-lean, maximum-reasoning work protocol for Claude Code,<br>
  with an optional verified, privacy-guarded swarm of free LLMs.
</p>

## Install (Claude Code plugin)

Inside Claude Code:

```
/plugin marketplace add noman-agri/Radon
/plugin install radon-1@radon
```

Restart Claude Code after installing. The skill works on its own (no keys, no scripts): it activates on non-trivial work and applies the lean protocol, token rules, and knowledge primers.

## Repository layout

| Folder | What | Needed? |
| --- | --- | --- |
| `plugin/` | The Claude Code plugin: the Radon 1 skill (Markdown only) and its icon | Yes |
| `toolkit/` | Optional Python CLI for `/orch` mode: free-model extract and swarm review | Only for `/orch` with free models - see [toolkit/README.md](toolkit/README.md) |

## What it does

- **Lean protocol**: reasoning steps, hard token rules, and a quality floor (saving tokens never skips verification).
- **Knowledge primers**: short domain briefs (ML, data analysis, software) loaded before unfamiliar work.
- **`/orch` mode** (needs the toolkit): free models extract from large files and give a second-opinion review; Claude verifies everything.
- **Anti-hallucination layers** (toolkit): verbatim-quote verification, quote-anchored findings, multi-model voting (>= 2 must agree), final verification by Claude.
- **Data protection** (toolkit): blocks sensitive files, redacts secrets and personal data before sending, metadata-only audit log, `--private` local-only mode.

## License

Proprietary. You may install and use Radon 1 for your own work. Copying, modifying, redistributing, or publishing customized or renamed versions requires written permission. See [LICENSE](LICENSE).
