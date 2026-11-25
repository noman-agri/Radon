# Primer: Software Building & Debugging

**Goal & check:** define "done" as a runnable check (test, command output, screenshot) before coding.

**Building**
1. Read the existing conventions first (one similar file, the test setup, the build command).
2. Smallest vertical slice that runs end to end, then extend.
3. Validate input at boundaries; fail loudly with clear messages; no silent `except: pass`.
4. Write or update a test for every behaviour change; run the project's own test/lint commands.

**Debugging**
1. Reproduce with the smallest input; capture the exact error and stack trace.
2. Form one hypothesis, design one check that can falsify it, run it. Repeat.
3. Fix the root cause, not the symptom; add a regression test that failed before the fix.
4. Check neighbours: same bug pattern elsewhere (`rg` the pattern).

**Top pitfalls**
- Off-by-one, `None`/empty handling, timezone/encoding (Windows cp1252 vs UTF-8), path separators.
- Mutable default arguments, shared state between tests, unclosed resources.
- Catching broad exceptions that hide the real error.
- Secrets in code, logs, or commits; SQL/command built by string concatenation.
- "Works on my machine": unpinned dependencies, missing env vars.

**Verification checks:** tests pass, linter clean, the original reproduction now succeeds, and nothing unrelated changed in the diff.
