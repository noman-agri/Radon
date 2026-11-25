#!/usr/bin/env python3
"""ab_token_compare.py - Audited Radon 1 A/B Token Cost & Accuracy Comparator."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


def _require_nonneg_int(val: Any, field: str) -> int:
    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
        raise ValueError(f"Field '{field}' must be a non-negative int, got {val!r}")
    return val


def _require_nonneg_num(val: Any, field: str) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or val < 0:
        raise ValueError(f"Field '{field}' must be a finite non-negative number, got {val!r}")
    return float(val)


@dataclass
class PriceWeights:
    """Cost relative to 1 uncached input token. Defaults follow Claude list pricing:
    output = 5x input on every current Claude model, 5-minute cache write = 1.25x,
    cache read = 0.1x (0.05x on Opus 5.5 / Sonnet 5.5, 0.025x on Fable 5.x - override per model)."""

    uncached_in: float = 1.0
    cache_write_in: float = 1.25
    cached_read_in: float = 0.10
    output: float = 5.0
    thinking: float = 5.0

    def __post_init__(self) -> None:
        self.uncached_in = _require_nonneg_num(self.uncached_in, "uncached_in")
        self.cache_write_in = _require_nonneg_num(self.cache_write_in, "cache_write_in")
        self.cached_read_in = _require_nonneg_num(self.cached_read_in, "cached_read_in")
        self.output = _require_nonneg_num(self.output, "output")
        self.thinking = _require_nonneg_num(self.thinking, "thinking")


@dataclass
class RunMetrics:
    uncached_in: int
    output: int
    passed: bool
    turns: int = 1
    cached_read_in: int = 0
    cache_write_in: int = 0
    thinking: int = 0
    # Claude `usage.output_tokens` (and OpenAI `completion_tokens`) already INCLUDE thinking/reasoning
    # tokens, so the safe default is True; set False only if your log reports thinking separately.
    thinking_included_in_output: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.passed, bool):
            raise ValueError(f"Field 'passed' must be bool, got {self.passed!r}")
        if not isinstance(self.thinking_included_in_output, bool):
            raise ValueError("Field 'thinking_included_in_output' must be bool")
        self.uncached_in = _require_nonneg_int(self.uncached_in, "uncached_in")
        self.output = _require_nonneg_int(self.output, "output")
        self.cached_read_in = _require_nonneg_int(self.cached_read_in, "cached_read_in")
        self.cache_write_in = _require_nonneg_int(self.cache_write_in, "cache_write_in")
        self.thinking = _require_nonneg_int(self.thinking, "thinking")
        self.turns = _require_nonneg_int(self.turns, "turns")
        if self.turns < 1:
            raise ValueError("Field 'turns' must be >= 1")
        if self.thinking_included_in_output and self.thinking > self.output:
            raise ValueError("thinking cannot exceed output when thinking_included_in_output=True")

    @classmethod
    def from_dict(cls, raw: Any, label: str) -> RunMetrics:
        if not isinstance(raw, dict):
            raise ValueError(f"'{label}' must be a JSON object")
        allowed = {f.name for f in fields(cls)}
        unknown = sorted(set(raw) - allowed - {"notes", "comment"})
        if unknown:
            # A typo like "cached_read" would otherwise be dropped silently and under-count cost.
            raise ValueError(f"'{label}' has unknown keys {unknown}; allowed: {sorted(allowed)}")
        filtered = {k: v for k, v in raw.items() if k in allowed}
        missing = {"uncached_in", "output", "passed"} - filtered.keys()
        if missing:
            raise ValueError(f"'{label}' missing required keys: {sorted(missing)}")
        return cls(**filtered)

    def weighted_cost(self, w: PriceWeights) -> float:
        visible_output = self.output - self.thinking if self.thinking_included_in_output else self.output
        return (
            self.uncached_in * w.uncached_in
            + self.cache_write_in * w.cache_write_in
            + self.cached_read_in * w.cached_read_in
            + visible_output * w.output
            + self.thinking * w.thinking
        )


def estimate_tokens(text: str, encoding_name: str = "o200k_base") -> tuple[int, str]:
    """
    ESTIMATE tokens via tiktoken (OpenAI tokenizer) or a stdlib heuristic.
    Neither equals Claude's tokenizer - for exact Claude counts use the API's
    count_tokens endpoint or real `usage` logs in 'suite' mode.
    """
    if not text:
        return 0, "empty"
    try:
        import tiktoken  # type: ignore

        try:
            enc = tiktoken.get_encoding(encoding_name)
            label = f"tiktoken:{encoding_name}"
        except (KeyError, ValueError):  # tiktoken raises ValueError for unknown encodings
            enc = tiktoken.get_encoding("cl100k_base")
            label = "tiktoken:cl100k_base"
        return len(enc.encode(text, disallowed_special=())), label
    except Exception:  # ImportError, or offline first-run BPE download failure -> heuristic
        return _heuristic_tokens(text), "approx-heuristic"


def _heuristic_tokens(text: str) -> int:
    tokens = 0
    pattern = r"[A-Za-z]+|\d+|_+|[\x80-\u024f]+|[^\x00-\u024f\s]+|\r\n|\n|\r|\t+| {2,}| |[^\w\s]+|."
    for chunk in re.findall(pattern, text):
        if chunk in (" ", "\u00a0"):
            continue
        if chunk in ("\n", "\r\n", "\r"):
            tokens += 1
        elif chunk[0] == "\t":
            tokens += len(chunk)
        elif chunk[0] == " ":
            tokens += math.ceil(len(chunk) / 4.0)
        elif chunk[0] == "_":
            tokens += math.ceil(len(chunk) / 2.0)
        elif chunk.isascii() and chunk.isalpha():
            tokens += math.ceil(len(chunk) / 4.0)
        elif chunk.isascii() and chunk.isdigit():
            tokens += math.ceil(len(chunk) / 3.0)
        elif 0x80 <= ord(chunk[0]) <= 0x024F:
            tokens += max(1, math.ceil(len(chunk) * 0.5))
        elif ord(chunk[0]) > 0x024F:
            tokens += max(1, math.ceil(len(chunk) * 1.1))
        else:
            tokens += max(1, math.ceil(len(chunk) * 0.8))
    return max(1, tokens)


def compare_pair(name: str, base: RunMetrics, treat: RunMetrics, w: PriceWeights) -> dict[str, Any]:
    bc = base.weighted_cost(w)
    tc = treat.weighted_cost(w)
    dc = bc - tc
    dp: float | None = (dc / bc * 100.0) if bc > 0 else None
    if not treat.passed and base.passed:
        verdict = "FAIL (Charter broke accuracy)"
    elif treat.passed and not base.passed:
        verdict = "WIN (Charter fixed failure)"
    elif not treat.passed and not base.passed:
        verdict = "BOTH FAILED"
    elif dc > 0:
        verdict = f"WIN (Saved {dp:.1f}%)" if dp is not None else "WIN"
    elif dc < 0:
        verdict = f"LOSS (Increased {abs(dp):.1f}%)" if dp is not None else "LOSS"
    else:
        verdict = "TIE (0.0%)"
    return {
        "test": name,
        "base_cost_exact": bc,
        "treat_cost_exact": tc,
        "delta_pct": dp,
        "base_pass": base.passed,
        "treat_pass": treat.passed,
        "base_turns": base.turns,
        "treat_turns": treat.turns,
        "verdict": verdict,
    }


def write_sample_json(dest: Path, force: bool = False) -> int:
    if dest.exists() and not force:
        raise FileExistsError(f"{dest} already exists. Pass --force to overwrite.")
    sample = [
        {
            "test": "T1_Short",
            "baseline": {"uncached_in": 35, "output": 180, "thinking": 0, "turns": 1, "passed": True},
            "charter": {"uncached_in": 195, "output": 135, "thinking": 0, "turns": 1, "passed": True},
        },
        {
            "test": "T2_Report",
            "baseline": {"uncached_in": 50, "output": 1800, "thinking": 0, "turns": 1, "passed": True},
            "charter": {"uncached_in": 210, "output": 720, "thinking": 0, "turns": 1, "passed": True},
        },
    ]
    dest.write_text(json.dumps(sample, indent=2), encoding="utf-8")
    print(f"Wrote sample template to {dest} (replace placeholder numbers with your real logs).")
    return 0


def run_json_suite(json_path: Path, w: PriceWeights) -> int:
    data = json.loads(json_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list) or not data:
        raise ValueError("JSON root must be a non-empty list of test objects.")
    both_base = both_treat = 0.0
    both_cnt = base_pass = treat_pass = reg = 0
    print(f"{'Test':<16} | {'BaseCost':>9} | {'CharCost':>9} | {'Delta%':>8} | {'Turns':>6} | {'Pass':>5} | Verdict")
    print("-" * 98)
    for idx, item in enumerate(data):
        if not isinstance(item, dict) or not {"test", "baseline", "charter"} <= item.keys():
            raise ValueError(f"Item #{idx} must contain 'test', 'baseline', and 'charter'.")
        name = str(item["test"]).strip() or f"test_{idx}"
        base = RunMetrics.from_dict(item["baseline"], f"item[{idx}].baseline")
        treat = RunMetrics.from_dict(item["charter"], f"item[{idx}].charter")
        r = compare_pair(name, base, treat, w)
        base_pass += int(base.passed)
        treat_pass += int(treat.passed)
        if base.passed and treat.passed:
            both_base += r["base_cost_exact"]
            both_treat += r["treat_cost_exact"]
            both_cnt += 1
        elif base.passed and not treat.passed:
            reg += 1
        d_str = f"{r['delta_pct']:>+7.1f}%" if r["delta_pct"] is not None else f"{'N/A':>8}"
        t_str = f"{base.turns}/{treat.turns}"
        p_str = f"{'Y' if base.passed else 'N'}/{'Y' if treat.passed else 'N'}"
        print(
            f"{r['test']:<16} | {r['base_cost_exact']:>9.1f} | {r['treat_cost_exact']:>9.1f} | "
            f"{d_str} | {t_str:>6} | {p_str:>5} | {r['verdict']}"
        )
    print("-" * 98)
    print(f"Pass Rate: Baseline {base_pass}/{len(data)} | Charter {treat_pass}/{len(data)}")
    if both_cnt > 0 and both_base > 0:
        net = (both_base - both_treat) / both_base * 100.0
        print(
            f"Fair Cost (Both-Passed {both_cnt}/{len(data)}): "
            f"Base={both_base:.1f} | Charter={both_treat:.1f} | Net={net:+.1f}%"
        )
    else:
        print("Fair Cost (Both-Passed): N/A")
    if reg > 0:
        print(f"WARNING: {reg} accuracy regression(s) detected.")
    return 0


def run_text_pair(
    base_in: Path, base_out: Path, treat_in: Path, treat_out: Path,
    w: PriceWeights, base_pass: bool, treat_pass: bool,
) -> int:
    bi, method = estimate_tokens(base_in.read_text(encoding="utf-8-sig"))
    bo, _ = estimate_tokens(base_out.read_text(encoding="utf-8-sig"))
    ti, _ = estimate_tokens(treat_in.read_text(encoding="utf-8-sig"))
    to, _ = estimate_tokens(treat_out.read_text(encoding="utf-8-sig"))
    r = compare_pair(
        "text-pair",
        RunMetrics(uncached_in=bi, output=bo, passed=base_pass),
        RunMetrics(uncached_in=ti, output=to, passed=treat_pass),
        w,
    )
    print(f"Method      : {method}")
    print(f"Baseline(A) : in={bi}, out={bo}, weighted={r['base_cost_exact']:.1f}")
    print(f"Charter (B) : in={ti}, out={to}, weighted={r['treat_cost_exact']:.1f}")
    print(f"Verdict     : {r['verdict']}")
    print("[Note] Text mode is an ESTIMATE (non-Claude tokenizer, no hidden thinking tokens); use 'suite' mode with real usage logs for billing.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Audited Radon 1 A/B Token & Accuracy Comparator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-template", help="Create a sample ab_results.json template")
    p_init.add_argument("--out", type=Path, default=Path("ab_results.json"))
    p_init.add_argument("--force", action="store_true", help="Overwrite if file exists")

    for p in (
        p_suite := sub.add_parser("suite", help="Evaluate JSON usage suite"),
        p_text := sub.add_parser("text", help="Estimate A/B pair from .txt files"),
    ):
        p.add_argument("--out-weight", type=float, default=5.0)
        p.add_argument("--think-weight", type=float, default=None)
        p.add_argument("--cache-read-weight", type=float, default=0.10)
        p.add_argument("--cache-write-weight", type=float, default=1.25)

    p_suite.add_argument("--input", type=Path, required=True)
    p_text.add_argument("--base-in", type=Path, required=True)
    p_text.add_argument("--base-out", type=Path, required=True)
    p_text.add_argument("--treat-in", type=Path, required=True)
    p_text.add_argument("--treat-out", type=Path, required=True)
    p_text.add_argument("--base-fail", action="store_true")
    p_text.add_argument("--treat-fail", action="store_true")

    args = parser.parse_args()
    try:
        if args.cmd == "init-template":
            return write_sample_json(args.out, force=args.force)
        w = PriceWeights(
            cache_write_in=args.cache_write_weight,
            cached_read_in=args.cache_read_weight,
            output=args.out_weight,
            thinking=args.think_weight if args.think_weight is not None else args.out_weight,
        )
        if args.cmd == "suite":
            return run_json_suite(args.input, w)
        return run_text_pair(
            args.base_in, args.base_out, args.treat_in, args.treat_out,
            w, base_pass=not args.base_fail, treat_pass=not args.treat_fail,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
