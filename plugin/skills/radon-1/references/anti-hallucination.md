# Anti-Hallucination - Detailed Checks

Load when the task is high-stakes (production code, data/ML results, numbers going into a report, legal/financial/medical facts) or when the user asks for "no hallucination".

## Where hallucinations come from, and the check that stops each

| Source of error | Typical symptom | Check |
| --- | --- | --- |
| Invented API | calling `df.fillna_mean()`, a flag that does not exist | grep the installed package or run `--help` / `help(obj)` before use |
| Wrong version | code for v2 API on a v1 install | read the installed version (`pip show`, `package.json`, lock file) |
| Imagined file contents | "the config sets X" without opening it | open the exact lines; cite `file:line` |
| Fabricated output | "tests pass" without running | run it; paste the summary line |
| Invented numbers | accuracy, speed-ups, prices, counts | only from output or a cited source; otherwise "not measured" |
| Stale knowledge | old model names, prices, endpoints | look up the official page; date it |
| Misread requirement | solving a different problem | quote the user's words that define success |
| Over-confident inference | "this must be the cause" | state it as a hypothesis, then test it |
| Merged sources | mixing what docs say with what you assume | label each: observed / documented / inferred |

## Claim labels (use only on claims that matter)

- **Observed** - seen in this session (command output, file contents). Cite it.
- **Documented** - from an official source. Give the URL.
- **Inferred** - reasoning from the above. Say so.
- **[UNVERIFIED]** - not checked. Say how to check it.

## Code-specific rules

1. Read the function you call or change before using it; do not rely on its name.
2. After an edit, run the narrowest check that exercises it (single test, import, compile).
3. When a fix "should work", it is not done until the check output says so.
4. If a library behaves unlike memory, trust the installed source over memory.

## Data / ML-specific rules

1. Every reported metric names the split it came from (CV fold mean +/- std, validation, test).
2. Report the baseline next to the model's score.
3. A result that looks too good (near-perfect score, huge jump) is treated as leakage until proven otherwise (shuffled-target check, feature-importance sanity).

## Answer-level check (before sending)

1. List the key claims in the answer.
2. For each: where did it come from? If nowhere, verify or label it.
3. Remove adjectives that imply certainty you do not have ("guaranteed", "always", "100%").
4. If the honest answer is "I don't know" or "it depends on X", say that.
