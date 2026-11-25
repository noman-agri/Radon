# Primer: Data Analysis / Reporting

**Goal & metric:** write the question as one sentence and the decision it informs. Define every KPI precisely (numerator, denominator, time window, filters) before computing.

**Standard pipeline**
1. Profile: row count, grain (what one row means), date range, nulls, duplicates, outliers.
2. Clean with logged rules (never silently drop rows; report how many were removed and why).
3. Aggregate in SQL/DuckDB or pandas; keep raw -> clean -> metric steps reproducible in one script/notebook.
4. Compare against a baseline (previous period, control group, overall average).
5. Visualise one message per chart; label units and sample size.
6. Conclude with the answer, the confidence, and the caveats.

**Top pitfalls**
- Wrong grain: joining one-to-many and double counting.
- Simpson's paradox: aggregate trend reverses inside segments.
- Survivorship / selection bias in the sample.
- Correlation reported as causation.
- Percent change on tiny bases; mean used on skewed data (use median/percentiles).
- Time-zone and partial-period effects (current month incomplete).

**Verification checks**
- Totals reconcile with the source system.
- Row counts before/after every join and filter.
- Spot-check 3 raw records end to end.

**Minimal tools:** DuckDB (SQL on CSV/Parquet), pandas/polars, matplotlib.
