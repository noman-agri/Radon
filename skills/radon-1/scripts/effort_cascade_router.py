#!/usr/bin/env python3
"""
effort_cascade_router.py - Radon 1 Stage-0 Local Gate + Compiler + Medium-to-Max Cascade.

Connect `llm_call_fn(static_prefix, dynamic_tail, effort) -> str` to your Anthropic,
Gemini, or LiteLLM client. Exceptions raised by llm_call_fn propagate to the caller
(retry/backoff belongs in the client layer, e.g. the SDK's built-in max_retries).
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import textwrap
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Literal

VerifyStatus = Literal["VERIFIED_PASS", "SYNTAX_ONLY", "VERIFIED_FAIL", "UNVERIFIED_PROSE"]

# NOTE: this prefix is ~400 tokens - BELOW every Claude model's minimum cacheable
# prefix (512-4096 tokens). It only gets cached when combined with other stable
# content (tool schemas, repo map, domain spec) above the model's minimum.
DEFAULT_STATIC_PREFIX = (
    "[RADON-1 CORE]\n"
    "1. TRUTH & SETTLED: Never fabricate facts, metrics, or run outputs; state [UNKNOWN] if unverified. "
    "Treat prior answered turns as settled; reuse prior context only if unmodified and same task.\n"
    "2. BLUEPRINT GATE: For multi-step/ambiguous domain tasks (e.g., ML, architecture), outline the standard "
    "domain blueprint before coding/training (skip pause under /bliend while keeping architecture internally).\n"
    "3. MODE SWITCH: [REASONING/CODING] -> first-principles logic, 1 internal falsification check, "
    "simplest generalizable code, unified diffs on files >40 lines. [NARRATIVE/PSYCHOLOGY] -> only "
    "when describing events/persuading, use causal arc (Context->Tension->Turn->Resolution) + human psychology.\n"
    "4. GATE & /bliend: Pause (<=3 lines: harm + fix) ONLY for severe bugs, leakage, or major cost waste; "
    "execute if owner insists. On /bliend (turn only, or /bliend session / /bliend off), execute directly.\n"
    "5. OUTPUT: Direct start. Long reports systemic in <=300 words (exempt full code, schemas, proofs, "
    "or requested expansion). Natural Bangla + exact English tech terms unless requested otherwise.\n"
)

TRIVIAL_GREETINGS = {"hi", "hello", "hey", "yo", "হাই", "হ্যালো", "সালাম", "assalamu alaikum"}
PYTHON_LANGS = {"python", "py", "python3", "py3"}

_RE_BLIND_OFF = re.compile(r"(?:^|\s)/(?:bliend|blind)\s+off(?=\s|$)", flags=re.IGNORECASE)
_RE_BLIND_SESSION = re.compile(r"(?:^|\s)/(?:bliend|blind)\s+session(?=\s|$)", flags=re.IGNORECASE)
_RE_BLIND_TURN = re.compile(r"(?:^|\s)/(?:bliend|blind)(?=\s|$)", flags=re.IGNORECASE)
# CommonMark fence opener: >=3 backticks or tildes, then an optional info string.
_RE_FENCE_OPEN = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")


@dataclass
class RouterState:
    task_id: str = "default"
    hot_index: str = ""
    awaiting_short_reply: bool = False
    last_question: str = ""
    blind_session: bool = False
    max_cache_size: int = 128
    response_cache: OrderedDict[str, tuple[str, VerifyStatus]] = field(default_factory=OrderedDict)

    def __post_init__(self) -> None:
        if self.max_cache_size < 0:
            raise ValueError("max_cache_size must be >= 0")

    def switch_task(self, new_task_id: str, new_hot_index: str = "") -> None:
        """Isolate task context to prevent cross-task memory leakage."""
        self.task_id = new_task_id
        self.hot_index = new_hot_index
        self.awaiting_short_reply = False
        self.last_question = ""

    def cache_put(self, key: str, value: tuple[str, VerifyStatus]) -> None:
        if self.max_cache_size == 0:
            return
        self.response_cache[key] = value
        self.response_cache.move_to_end(key)
        while len(self.response_cache) > self.max_cache_size:
            self.response_cache.popitem(last=False)

    def note_assistant_output(self, output: str) -> None:
        """If the model ended on a question, let a 1-char reply ('y', 'n', '2') through next turn."""
        last_line = next((ln.strip() for ln in reversed(output.splitlines()) if ln.strip()), "")
        if last_line.endswith(("?", "？")):
            self.awaiting_short_reply = True
            self.last_question = last_line[-500:]
        else:
            self.awaiting_short_reply = False
            self.last_question = ""


@dataclass
class RouteResult:
    output: str
    stage_used: str
    effort_used: str
    llm_calls: int
    verify_status: VerifyStatus
    error_note: str = ""


def _clean_preserve_indent(text: str) -> str:
    """Trim surrounding blank lines without destroying leading indentation of pasted code."""
    if not text.strip():
        return ""
    if "\n" not in text and "\r" not in text:
        return text.strip()
    text = re.sub(r"^(?:[ \t]*\r?\n)+", "", text)
    text = re.sub(r"(?:\r?\n[ \t]*)+$", "", text)
    return text


def parse_flags(raw_input: str, state: RouterState) -> tuple[str, bool, str]:
    """Extract /bliend flags safely without corrupting file paths or code indentation."""
    text = _clean_preserve_indent(raw_input)
    if _RE_BLIND_OFF.search(text):
        state.blind_session = False
        return _clean_preserve_indent(_RE_BLIND_OFF.sub("", text)), False, "BLIND_OFF"
    if _RE_BLIND_SESSION.search(text):
        state.blind_session = True
        return _clean_preserve_indent(_RE_BLIND_SESSION.sub("", text)), True, "BLIND_SESSION_ON"
    if _RE_BLIND_TURN.search(text):
        return _clean_preserve_indent(_RE_BLIND_TURN.sub("", text)), True, "BLIND_TURN_ON"
    return text, state.blind_session, ""


def stage0_local_gate(text: str, state: RouterState, flag_event: str) -> RouteResult | None:
    """Handle empty inputs, flag-only commands, greetings, and 1-char idle pings at 0 LLM tokens."""
    if not text:
        if flag_event == "BLIND_OFF":
            msg = "Blind session disabled. Impact Gate is active."
        elif flag_event == "BLIND_SESSION_ON":
            msg = "Blind session enabled. Send your next task."
        elif flag_event == "BLIND_TURN_ON":
            msg = "Single-turn /bliend requires a task in the same message (or use /bliend session)."
        else:
            msg = "No command detected. State the task in one line."
        return RouteResult(msg, "STAGE_0_LOCAL", "none", 0, "VERIFIED_PASS")

    greeting_norm = re.sub(r"[!.,?।]+$", "", text.strip().lower()).strip()
    if greeting_norm in TRIVIAL_GREETINGS:
        return RouteResult("Ready. State the task you want to run.", "STAGE_0_LOCAL", "none", 0, "VERIFIED_PASS")

    if not state.awaiting_short_reply and len(text) == 1 and not text.isdigit():
        return RouteResult(
            f"Ambiguous 1-char input ('{text}'). State the task in one line to avoid token waste.",
            "STAGE_0_LOCAL",
            "none",
            0,
            "VERIFIED_PASS",
        )
    return None


def compile_prompt(text: str, state: RouterState, blind_turn: bool) -> str:
    parts: list[str] = []
    if state.hot_index.strip():
        parts.append(f"[HOT_INDEX | task={state.task_id}]\n{state.hot_index.strip()}")
    if state.awaiting_short_reply and state.last_question:
        parts.append(f"[PREVIOUS_ASSISTANT_QUESTION]\n{state.last_question}")
    mode = "MODE: /bliend (direct execution)" if blind_turn else "MODE: default"
    parts.append(f"[{mode}]\nUSER_TASK:\n{text}")
    return "\n\n".join(parts)


def make_cache_key(
    text: str,
    state: RouterState,
    static_prefix: str,
    blind_turn: bool,
    model_id: str,
    base_effort: str,
    escalate_effort: str,
) -> str:
    """Hash exact text, task, hot_index, pending question, static_prefix, flags, model, and effort tiers."""
    payload = json.dumps(
        {
            "t": text,
            "tid": state.task_id,
            "h": state.hot_index,
            "q": state.last_question if state.awaiting_short_reply else "",
            "sp": hashlib.sha256(static_prefix.encode("utf-8")).hexdigest(),
            "b": blind_turn,
            "m": model_id,
            "be": base_effort,
            "ee": escalate_effort,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extract_python_blocks(output: str) -> tuple[list[str], bool]:
    """
    Line-by-line CommonMark fence parser. Returns (python_blocks, unclosed_python_fence).
    Info strings after the language ("```python title=x.py", "```py {linenos}") are handled,
    so fence open/close parity never desyncs.
    """
    blocks: list[str] = []
    in_fence = False
    fence_delim = ""
    is_py = False
    buf: list[str] = []
    for line in output.splitlines():
        if not in_fence:
            m = _RE_FENCE_OPEN.match(line)
            if not m:
                continue
            delim, info = m.group(1), m.group(2).strip()
            if delim[0] == "`" and "`" in info:
                continue  # inline code like ```x```, not a fence (CommonMark rule)
            lang = info.split()[0].lstrip("{.").rstrip("}").lower() if info else ""
            in_fence, fence_delim, is_py, buf = True, delim, lang in PYTHON_LANGS, []
        else:
            stripped = line.strip()
            if len(stripped) >= len(fence_delim) and set(stripped) == {fence_delim[0]}:
                if is_py:
                    blocks.append("\n".join(buf))
                in_fence, fence_delim, is_py, buf = False, "", False, []
            elif is_py:
                buf.append(line)
    return blocks, (in_fence and is_py)


def default_code_verifier(output: str) -> tuple[VerifyStatus, str]:
    """Verify Python fences with dedent + ast.parse; detect unclosed/empty blocks; label SYNTAX_ONLY."""
    blocks, unclosed_py = extract_python_blocks(output)
    if unclosed_py:
        return "VERIFIED_FAIL", "Unclosed or truncated Python code fence."
    if not blocks:
        return "UNVERIFIED_PROSE", ""
    checked = 0
    for idx, raw_code in enumerate(blocks, start=1):
        code = textwrap.dedent(raw_code)
        if not code.strip():
            return "VERIFIED_FAIL", f"Python block #{idx} is empty."
        if code.lstrip().startswith(">>>"):
            continue  # interactive/doctest transcript, not a module - don't false-fail it
        try:
            ast.parse(code)
        except SyntaxError as exc:
            return "VERIFIED_FAIL", f"Python block #{idx} SyntaxError at line {exc.lineno}: {exc.msg}"
        checked += 1
    return ("SYNTAX_ONLY", "") if checked else ("UNVERIFIED_PROSE", "")


def run_cascade(
    raw_input: str,
    state: RouterState,
    llm_call_fn: Callable[[str, str, str], str],
    verifier_fn: Callable[[str], tuple[VerifyStatus, str]] = default_code_verifier,
    static_prefix: str = DEFAULT_STATIC_PREFIX,
    model_id: str = "claude-opus-5-5",
    base_effort: str = "medium",
    escalate_effort: str = "xhigh",
    use_cache: bool = True,
) -> RouteResult:
    """
    Stage-0 Gate -> Exact Cache -> Medium Pass -> Bounded Single Escalation.
    Pass use_cache=False for retries / non-deterministic requests ("run the tests again").
    Note: the escalation changes `effort`, which invalidates the provider's messages cache
    for that call - keep escalations rare (max one) as designed.
    """
    text, blind_turn, flag_event = parse_flags(raw_input, state)
    local_hit = stage0_local_gate(text, state, flag_event)
    if local_hit is not None:
        return local_hit

    cache_key = make_cache_key(text, state, static_prefix, blind_turn, model_id, base_effort, escalate_effort)
    if use_cache and cache_key in state.response_cache:
        cached_out, cached_status = state.response_cache[cache_key]
        state.response_cache.move_to_end(cache_key)
        state.note_assistant_output(cached_out)
        return RouteResult(cached_out, "STAGE_0_EXACT_CACHE", "none", 0, cached_status)

    dynamic_tail = compile_prompt(text, state, blind_turn)
    out1 = llm_call_fn(static_prefix, dynamic_tail, base_effort)
    status1, err1 = verifier_fn(out1)
    if status1 in ("VERIFIED_PASS", "SYNTAX_ONLY", "UNVERIFIED_PROSE"):
        state.cache_put(cache_key, (out1, status1))
        state.note_assistant_output(out1)
        return RouteResult(out1, "STAGE_2_MEDIUM", base_effort, 1, status1)

    escalated_tail = (
        f"{dynamic_tail}\n\n"
        f"[VERIFIER_FAILURE_FROM_PASS_1]\n{err1}\n"
        "Fix the exact root cause above without inventing unverified claims."
    )
    out2 = llm_call_fn(static_prefix, escalated_tail, escalate_effort)
    status2, err2 = verifier_fn(out2)
    if status2 == "UNVERIFIED_PROSE" and not err2:
        err2 = "Escalated response omitted verifiable code block after Pass-1 failure."
    if status2 in ("VERIFIED_PASS", "SYNTAX_ONLY"):
        state.cache_put(cache_key, (out2, status2))
    state.note_assistant_output(out2)
    return RouteResult(out2, "STAGE_2_ESCALATED", escalate_effort, 2, status2, err2)
