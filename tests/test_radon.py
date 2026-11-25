"""Offline regression tests for Radon 1 scripts (stdlib unittest, no network).

Run:  python -m unittest discover -s tests -v      (from the skill root)
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ab_token_compare as ab  # noqa: E402
import effort_cascade_router as er  # noqa: E402
import free_scout_gate as fs  # noqa: E402

CFG = fs.ScoutConfig("https://example.invalid", "m", "KEY")


def _fake(prefix: str, mid: str, n: int, ch: str = "X") -> str:
    """Build token-shaped test strings at runtime, so no token-like literal sits in the source
    (keeps GitHub secret scanning / push protection quiet)."""
    return prefix + mid + ch * n


class _Resp:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def _chat(content) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


def _by_model(table):
    """Fake urlopen that answers by the request's model (order-independent, safe with threads)."""
    def fake(req, timeout=0):
        out = table[json.loads(req.data)["model"]]
        if isinstance(out, Exception):
            raise out
        return _Resp(out)
    return fake


class ScoutGateTests(unittest.TestCase):
    SRC = "ERROR: disk full at /var/log/app. Retrying in 5s. " * 10

    def test_verbatim_quote_passes(self):
        out, st = fs.verify_extractive_json('{"relevant_quotes":["disk full"],"keys":["ERROR"]}', self.SRC)
        self.assertEqual(st, "VERIFIED_EXTRACTIVE")
        self.assertEqual(out["relevant_quotes"], ["disk full"])

    def test_hallucinated_quote_rejected(self):
        out, st = fs.verify_extractive_json('{"relevant_quotes":["disk is broken"]}', self.SRC)
        self.assertIsNone(out)
        self.assertTrue(st.startswith("HALLUCINATION_REJECTED"))

    def test_preamble_and_fence_extraction(self):
        raw = 'Sure! Here you go:\n```json\n{"relevant_quotes":["disk full"]}\n```'
        self.assertEqual(fs.verify_extractive_json(raw, self.SRC)[1], "VERIFIED_EXTRACTIVE")
        raw2 = '```json {"relevant_quotes":["disk full"]}```'
        self.assertEqual(fs.verify_extractive_json(raw2, self.SRC)[1], "VERIFIED_EXTRACTIVE")

    def test_null_keys_tolerated(self):
        self.assertEqual(fs.verify_extractive_json('{"relevant_quotes":[],"keys":null}', self.SRC)[1],
                         "VERIFIED_EXTRACTIVE")

    def test_no_compression_gain_rejected(self):
        out, st = fs.verify_extractive_json(json.dumps({"relevant_quotes": [self.SRC]}), self.SRC)
        self.assertEqual(st, "SCOUT_NO_COMPRESSION_GAIN")

    # --- regressions -------------------------------------------------------
    def test_B1_empty_extraction_falls_back_to_full_doc(self):
        with mock.patch.object(fs.urllib.request, "urlopen",
                               return_value=_Resp(_chat('{"relevant_quotes": [], "keys": []}'))):
            ctx, st = fs.build_main_context(self.SRC, "find causes", CFG)
        self.assertEqual(ctx, self.SRC)
        self.assertEqual(st, "SCOUT_EMPTY_EXTRACTION_FALLBACK")

    def test_B2_secret_in_task_hint_never_sent(self):
        with mock.patch.object(fs.urllib.request, "urlopen") as m:
            _, st = fs.run_free_scout("harmless", "key " + _fake("sk", "-ant-api03-", 24), CFG)
        self.assertEqual(st, "SCOUT_BLOCKED_SECRET_DETECTED")
        m.assert_not_called()

    def test_B2_extra_secret_patterns(self):
        for s in (_fake("github", "_pat_11", 30), _fake("gh", "o_", 26),
                  _fake("xo", "xb-1234567890-", 10), _fake("AS", "IA", 16, "Q")):
            self.assertTrue(fs.contains_secret(f"x {s} y"), s)
        self.assertFalse(fs.contains_secret("ask-questions-about-this-long-identifier"))

    def test_B3_plain_http_blocked_but_localhost_allowed(self):
        with mock.patch.object(fs.urllib.request, "urlopen") as m:
            _, st = fs.run_free_scout("abc", "h", fs.ScoutConfig("http://evil.example", "m", "K"))
        self.assertEqual(st, "SCOUT_BLOCKED_INSECURE_ENDPOINT")
        m.assert_not_called()
        self.assertTrue(fs._endpoint_is_safe("http://localhost:11434/v1"))

    def test_B4_null_content_is_safe_fallback(self):
        with mock.patch.object(fs.urllib.request, "urlopen", return_value=_Resp(_chat(None))):
            self.assertEqual(fs.run_free_scout("abc", "h", CFG), (None, "SCOUT_FALLBACK_ERROR: empty_content"))

    def test_B4_connection_reset_and_bad_bytes_are_safe(self):
        for exc_or_body in (ConnectionResetError("reset"), TimeoutError()):
            with mock.patch.object(fs.urllib.request, "urlopen", side_effect=exc_or_body):
                out, st = fs.run_free_scout("abc", "h", CFG)
            self.assertIsNone(out)
            self.assertIn("SCOUT_FALLBACK_ERROR", st)
        with mock.patch.object(fs.urllib.request, "urlopen", return_value=_Resp(b"\xff\xfe")):
            self.assertIn("SCOUT_FALLBACK_ERROR", fs.run_free_scout("abc", "h", CFG)[1])
        err = urllib.error.HTTPError("u", 429, "rate", {}, None)
        with mock.patch.object(fs.urllib.request, "urlopen", side_effect=err):
            self.assertEqual(fs.run_free_scout("abc", "h", CFG)[1], "SCOUT_FALLBACK_HTTP_429")

    def test_config_repr_hides_key_and_dotenv_loads(self):
        self.assertNotIn("KEY", repr(CFG).replace("api_key", ""))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text('RADON_SCOUT_BASE_URL="https://x.test/"\nRADON_SCOUT_MODEL=m\n'
                         "export RADON_SCOUT_API_KEY=k\n", encoding="utf-8")
            with mock.patch.dict(fs.os.environ, {}, clear=True):
                cfg = fs.ScoutConfig.from_env(p)
        self.assertEqual((cfg.base_url, cfg.model, cfg.api_key), ("https://x.test", "m", "k"))


class RouterTests(unittest.TestCase):
    def test_stage0_greeting_and_flags(self):
        st = er.RouterState()
        self.assertEqual(er.run_cascade("Hello!", st, lambda *a: "x").llm_calls, 0)
        r = er.run_cascade("/bliend session", st, lambda *a: "x")
        self.assertTrue(st.blind_session)
        self.assertEqual(r.stage_used, "STAGE_0_LOCAL")
        text, blind, _ = er.parse_flags("open /blind/test.py", er.RouterState())
        self.assertEqual((text, blind), ("open /blind/test.py", False))

    def test_valid_code_is_syntax_only_and_bad_code_escalates_once(self):
        calls = []

        def llm(prefix, tail, effort):
            calls.append(effort)
            return "```python\nx = (\n```" if effort == "medium" else "```python\nx = 1\n```"

        r = er.run_cascade("write code", er.RouterState(), llm)
        self.assertEqual((calls, r.verify_status, r.llm_calls), (["medium", "xhigh"], "SYNTAX_ONLY", 2))

    def test_unclosed_fence_fails(self):
        self.assertEqual(er.default_code_verifier("```python\nx = 1\n")[0], "VERIFIED_FAIL")

    # --- regressions -------------------------------------------------------
    def test_B5_info_string_does_not_desync_fences(self):
        out = "```python title=x.py\nprint(1)\n```\ntext\n```python\nx = (\n```\n"
        self.assertEqual(er.default_code_verifier(out)[0], "VERIFIED_FAIL")
        self.assertEqual(er.extract_python_blocks("```py {linenos}\na = 1\n```")[0], ["a = 1"])
        self.assertEqual(er.extract_python_blocks("use ```x``` inline\n```python\nb=2\n```")[0], ["b=2"])

    def test_B6_doctest_block_not_false_failed(self):
        self.assertEqual(er.default_code_verifier("```python\n>>> 1+1\n2\n```")[0], "UNVERIFIED_PROSE")

    def test_B7_short_reply_after_question_reaches_llm_with_context(self):
        st = er.RouterState()
        seen = []
        er.run_cascade("Delete the temp dir", st, lambda *a: "Found 3 dirs. Should I proceed? (y/n)?")
        r = er.run_cascade("y", st, lambda p, tail, e: seen.append(tail) or "Deleted.")
        self.assertEqual(r.stage_used, "STAGE_2_MEDIUM")
        self.assertIn("Should I proceed", seen[0])
        self.assertFalse(st.awaiting_short_reply)
        # same 'y' to a DIFFERENT question must not hit the first answer's cache
        er.run_cascade("Rename files", st, lambda *a: "Rename 10 files?")
        r2 = er.run_cascade("y", st, lambda *a: "Renamed.")
        self.assertEqual(r2.output, "Renamed.")

    def test_B8_cache_bypass_for_retries(self):
        calls = []
        st = er.RouterState()
        llm = lambda *a: calls.append(1) or "ok"  # noqa: E731
        er.run_cascade("run the tests again", st, llm)
        self.assertEqual(er.run_cascade("run the tests again", st, llm).stage_used, "STAGE_0_EXACT_CACHE")
        er.run_cascade("run the tests again", st, llm, use_cache=False)
        self.assertEqual(len(calls), 2)

    def test_switch_task_isolates_cache(self):
        st = er.RouterState()
        er.run_cascade("same text", st, lambda *a: "A")
        st.switch_task("other")
        self.assertEqual(er.run_cascade("same text", st, lambda *a: "B").output, "B")


class ComparatorTests(unittest.TestCase):
    def test_B9_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            ab.RunMetrics.from_dict({"uncached_in": 1, "output": 1, "passed": True, "cached_read": 9}, "x")

    def test_B10_default_weights_match_claude_pricing(self):
        w = ab.PriceWeights()
        self.assertEqual((w.output, w.thinking, w.cached_read_in, w.cache_write_in), (5.0, 5.0, 0.10, 1.25))

    def test_B10_thinking_not_double_counted_by_default(self):
        m = ab.RunMetrics(uncached_in=0, output=100, thinking=60, passed=True)
        self.assertEqual(m.weighted_cost(ab.PriceWeights()), 500.0)
        with self.assertRaises(ValueError):
            ab.RunMetrics(uncached_in=0, output=10, thinking=60, passed=True)

    def test_B11_estimator_survives_broken_tiktoken(self):
        broken = mock.MagicMock()
        broken.get_encoding.side_effect = OSError("offline")
        with mock.patch.dict(sys.modules, {"tiktoken": broken}):
            n, how = ab.estimate_tokens("hello world")
        self.assertEqual(how, "approx-heuristic")
        self.assertGreater(n, 0)

    def test_suite_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ab.json"
            self.assertEqual(ab.write_sample_json(p), 0)
            with self.assertRaises(FileExistsError):
                ab.write_sample_json(p)
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(ab.run_json_suite(p, ab.PriceWeights()), 0)
        self.assertIn("Fair Cost (Both-Passed 2/2)", buf.getvalue())

    def test_default_static_prefix_is_below_min_cacheable(self):
        # Documents the gotcha: ~400 tokens < 512 (smallest Claude cache minimum).
        self.assertLess(ab._heuristic_tokens(er.DEFAULT_STATIC_PREFIX), 512)


import free_llm as fl  # noqa: E402


class FreeLlmCliTests(unittest.TestCase):
    CODE = "def f(x):\n    return x / 0\n\nprint(f(1))\n"

    def test_review_keeps_verified_drops_hallucinated(self):
        raw = json.dumps({"findings": [
            {"quote": "return x / 0", "issue": "division by zero"},
            {"quote": "eval(user_input)", "issue": "invented line"},
        ]})
        kept, dropped, st = fl.verify_review(raw, self.CODE, 1)
        self.assertEqual((st, dropped), ("OK", 1))
        self.assertEqual(kept[0][:2], (2, "division by zero"))

    def test_review_bad_json_is_fallback(self):
        self.assertEqual(fl.verify_review("not json", self.CODE, 1)[2], "REVIEW_REJECTED_JSON")

    def test_chunking_keeps_line_numbers(self):
        text = "\n".join(f"line {i}" for i in range(1, 101))
        chunks = fl.chunk_lines(text, limit=200)
        self.assertGreater(len(chunks), 1)
        first, body = chunks[1]
        self.assertTrue(body.startswith(f"line {first}"))

    def test_config_presets_and_ollama_without_key(self):
        with mock.patch.dict(fl.os.environ, {}, clear=True), \
                mock.patch.object(fl, "SKILL_DOTENV", Path("__none__")), \
                mock.patch.object(fl, "SECRETS", Path("__none__")), \
                mock.patch.object(fl, "SKILL_MODELS", Path("__none__")):
            cfg = fl.build_config("ollama", "qwen2.5-coder:7b", dotenv="__none__")
            self.assertEqual(cfg.base_url, "http://localhost:11434/v1")
            self.assertIsNone(fl.build_config("groq", "m", dotenv="__none__"))
        env = {"RADON_SCOUT_API_KEY": "PASTE_YOUR_OPENROUTER_KEY_HERE", "RADON_SCOUT_MODEL": "a:free"}
        with mock.patch.dict(fl.os.environ, env, clear=True), \
                mock.patch.object(fl, "SKILL_DOTENV", Path("__none__")), \
                mock.patch.object(fl, "SECRETS", Path("__none__")):
            self.assertEqual(fl.build_configs("openrouter", None, "__none__"), [])  # placeholder key

    def test_mixed_providers_each_get_own_key(self):
        env = {"RADON_KEY_OPENROUTER": "k1", "RADON_KEY_GEMINI": "k2", "RADON_SCOUT_PROVIDER": "openrouter",
               "RADON_SCOUT_MODEL": "openrouter:x/y:free,gemini:gemini-flash,groq:llama"}
        with mock.patch.dict(fl.os.environ, env, clear=True), \
                mock.patch.object(fl, "SKILL_DOTENV", Path("__none__")), \
                mock.patch.object(fl, "SECRETS", Path("__none__")), \
                mock.patch.object(fl, "SKILL_MODELS", Path("__none__")):
            cfgs = fl.build_configs(None, None, "__none__")
            only_gem = fl.build_configs("gemini", None, "__none__")
        self.assertEqual([(c.model, c.api_key) for c in cfgs], [("x/y:free", "k1"), ("gemini-flash", "k2")])
        self.assertIn("generativelanguage", cfgs[1].base_url)
        self.assertEqual([c.model for c in only_gem], ["gemini-flash"])  # groq skipped: no key

    def test_model_rotation_on_rate_limit(self):
        env = {"RADON_SCOUT_API_KEY": "k", "RADON_SCOUT_MODEL": "a:free, b:free"}
        with mock.patch.dict(fl.os.environ, env, clear=True), \
                mock.patch.object(fl, "SKILL_DOTENV", Path("__none__")), \
                mock.patch.object(fl, "SECRETS", Path("__none__")), \
                mock.patch.object(fl, "SKILL_MODELS", Path("__none__")):
            cfgs = fl.build_configs("openrouter", None, "__none__")
        self.assertEqual([c.model for c in cfgs], ["a:free", "b:free"])
        err = urllib.error.HTTPError("u", 429, "rate", {}, None)
        with mock.patch.object(fs.urllib.request, "urlopen", side_effect=[err, _Resp(_chat("hi"))]):
            content, status = fl.chat_rotating(cfgs, "s", "u")
        self.assertEqual((content, status), ("hi", "OK via b:free"))

    def test_unconfigured_cli_falls_back_with_exit_2(self):
        buf = io.StringIO()
        with mock.patch.dict(fl.os.environ, {}, clear=True), redirect_stdout(buf), \
                mock.patch.object(fl, "_read_dotenv", return_value={}):
            self.assertEqual(fl.main(["ping", "--provider", "groq"]), 2)
        self.assertIn("SCOUT_SKIPPED_UNCONFIGURED", buf.getvalue())

    def test_review_end_to_end_with_mocked_provider(self):
        reply = json.dumps({"findings": [{"quote": "return x / 0", "issue": "division by zero"}]})
        cfg = fs.ScoutConfig("https://example.invalid", "m", "K")
        buf = io.StringIO()
        with mock.patch.object(fs.urllib.request, "urlopen", return_value=_Resp(_chat(reply))), \
                redirect_stdout(buf):
            self.assertEqual(fl.cmd_review(cfg, self.CODE), 0)
        self.assertIn("L2 [1/1]: division by zero", buf.getvalue())
        self.assertIn("SINGLE MODEL", buf.getvalue())


import privacy_guard as pg  # noqa: E402

pg.AUDIT_LOG = Path(tempfile.gettempdir()) / "radon_test_audit.log"


class SwarmAndPrivacyTests(unittest.TestCase):
    def test_swarm_keeps_only_consensus(self):
        a = [(2, "div by zero", "q"), (5, "invented", "x")]
        b = [(2, "zero division", "q")]
        c = [(9, "other", "y")]
        self.assertEqual([r[:2] for r in fl.swarm_vote([a, b, c], 2)], [(2, 2)])

    def test_swarm_review_end_to_end(self):
        good = _chat(json.dumps({"findings": [{"quote": "return x / 0", "issue": "division by zero"}]}))
        lone = _chat(json.dumps({"findings": [{"quote": "print(f(1))", "issue": "made-up issue"}]}))
        cfgs = [fs.ScoutConfig("https://example.invalid", m, "K") for m in ("a", "b", "c")]
        buf = io.StringIO()
        with mock.patch.object(fs.urllib.request, "urlopen",
                               side_effect=_by_model({"a": good, "b": good, "c": lone})), redirect_stdout(buf):
            self.assertEqual(fl.cmd_review(cfgs, FreeLlmCliTests.CODE), 0)
        out = buf.getvalue()
        self.assertIn("L2 [2/3]: division by zero", out)
        self.assertNotIn("made-up", out)

    def test_swarm_survives_one_rate_limited_member(self):
        good = _chat(json.dumps({"findings": [{"quote": "return x / 0", "issue": "div"}]}))
        err = urllib.error.HTTPError("u", 429, "rate", {}, None)
        cfgs = [fs.ScoutConfig("https://example.invalid", m, "K") for m in ("a", "b", "c")]
        buf = io.StringIO()
        with mock.patch.object(fs.urllib.request, "urlopen",
                               side_effect=_by_model({"a": err, "b": good, "c": good})), redirect_stdout(buf):
            self.assertEqual(fl.cmd_review(cfgs, FreeLlmCliTests.CODE), 0)
        self.assertIn("swarm=2", buf.getvalue())

    def test_swarm_runs_members_in_parallel(self):
        import threading
        import time
        good = _chat(json.dumps({"findings": [{"quote": "return x / 0", "issue": "div"}]}))
        barrier = threading.Barrier(3, timeout=5)

        def slow(req, timeout=0):
            barrier.wait()  # deadlocks (BrokenBarrierError) if members ran one after another
            time.sleep(0.05)
            return _Resp(good)

        cfgs = [fs.ScoutConfig("https://example.invalid", m, "K") for m in ("a", "b", "c")]
        buf = io.StringIO()
        with mock.patch.object(fs.urllib.request, "urlopen", side_effect=slow), redirect_stdout(buf):
            self.assertEqual(fl.cmd_review(cfgs, FreeLlmCliTests.CODE), 0)
        self.assertIn("swarm=3", buf.getvalue())

    def test_short_quote_does_not_anchor_to_wrong_line(self):
        code = "x = 1\ny = x + 2\nreturn_value = x\n"
        raw = json.dumps({"findings": [{"quote": "x", "issue": "vague"},
                                       {"quote": "y = x + 2", "issue": "real"}]})
        kept, dropped, _ = fl.verify_review(raw, code, 1)
        self.assertEqual([(k[0], k[1]) for k in kept], [(2, "real")])
        self.assertEqual(dropped, 1)

    def test_redaction_preserves_lines(self):
        src = ("email = 'alice@example.com'\npassword = 'hunter22'\nip = '10.0.0.5'\n"
               "path = r'C:\\Users\\alice\\data'\nkey = '" + _fake("sk", "-ant-api03-", 22) + "'\nx = 1\n")
        red, counts = pg.redact(src)
        self.assertEqual(red.count("\n"), src.count("\n"))
        for leaked in ("alice@example.com", "hunter22", "10.0.0.5", "\\alice\\", "sk-ant"):
            self.assertNotIn(leaked, red)
        self.assertIn("x = 1", red)
        self.assertGreaterEqual(len(counts), 4)

    def test_extract_redacts_hint_before_cloud_send(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "source.txt"
            source.write_text("safe source", encoding="utf-8")
            cfg = fs.ScoutConfig("https://example.invalid", "m", "K")
            with mock.patch.object(fl, "build_configs", return_value=[cfg]), \
                    mock.patch.object(fl, "scout_rotating", return_value=(
                        {"relevant_quotes": ["safe source"]}, "VERIFIED_EXTRACTIVE"
                    )) as scout, redirect_stdout(io.StringIO()):
                self.assertEqual(fl.main([
                    "extract", "--file", str(source),
                    "--hint", "find alice@example.com at 10.0.0.5",
                ]), 0)
        sent_hint = scout.call_args.args[2]
        self.assertNotIn("alice@example.com", sent_hint)
        self.assertNotIn("10.0.0.5", sent_hint)
        self.assertIn("<EMAIL>", sent_hint)
        self.assertIn("<IP>", sent_hint)

    def test_redaction_never_breaks_code_expressions(self):
        code = ("cfgs.append(ScoutConfig(base_url=u, model=m, api_key=key))\n"
                "token = tokenizer(text)\nhost = '127.0.0.1'\n")
        red, _ = pg.redact(code)
        self.assertEqual(red, code)  # identifiers/expressions untouched; loopback kept
        cfg = "password: hunter22\nAPI_KEY=abcd1234efgh\n"
        red_cfg, counts = pg.redact(cfg)
        self.assertNotIn("hunter22", red_cfg)
        self.assertNotIn("abcd1234efgh", red_cfg)
        self.assertEqual(counts.get("SECRET"), 2)

    def test_sensitive_files_blocked(self):
        for name in (".env", "credentials.json", "id_rsa", "server.pem", "train.csv", "auth.json", "token.txt"):
            self.assertIsNotNone(pg.block_reason(Path(name)), name)
        for name in ("train.py", "ab_token_compare.py", "tokenizer.py", "password_policy.md"):
            self.assertIsNone(pg.block_reason(Path(name)), name)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(fl.main(["review", "--file", "secrets.env"]), 2)
        self.assertIn("BLOCKED", buf.getvalue())

    def test_audit_logs_metadata_not_content(self):
        pg.AUDIT_LOG.unlink(missing_ok=True)
        pg.audit("https://openrouter.ai/api/v1", "m:free", "TOP SECRET CONTENT", "chat")
        line = pg.AUDIT_LOG.read_text(encoding="utf-8")
        self.assertIn("openrouter.ai", line)
        self.assertNotIn("TOP SECRET", line)

    def test_private_mode_never_uses_cloud(self):
        env = {"RADON_SCOUT_API_KEY": "k", "RADON_SCOUT_MODEL": "a:free"}
        buf = io.StringIO()
        with mock.patch.dict(fl.os.environ, env, clear=True), \
                mock.patch.object(fl, "SKILL_DOTENV", Path("__none__")), \
                mock.patch.object(fl, "SECRETS", Path("__none__")), \
                mock.patch.object(fs.urllib.request, "urlopen", side_effect=OSError("no ollama")) as m, \
                redirect_stdout(buf):
            fl.main(["ping", "--private"])
        self.assertTrue(all("localhost" in c.args[0].full_url for c in m.call_args_list))


if __name__ == "__main__":
    unittest.main()
