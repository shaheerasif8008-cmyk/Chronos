---
name: data-analysis
description: Analyze datasets (CSV, Excel, JSON) — clean, aggregate, compute statistics, and produce charts and a written summary. Use when the user shares data or asks for analysis, metrics, trends, or visualizations.
requires_connectors: []
spawns_sub_agent: false
---

# Data Analysis

Turn raw data into insight, charts, and a clear written summary.

## When to use
- "Analyze this CSV / dataset / spreadsheet"
- "What are the trends / outliers / correlations?"
- "Chart this / give me the key metrics"

## Procedure
1. Load the data with pandas via `code__python` (`pd.read_csv` / `pd.read_excel` / `pd.read_json`).
2. Inspect: shape, dtypes, missing values, basic `describe()`.
3. Clean as needed (types, nulls, duplicates) and state what you changed.
4. Compute the metrics the question needs; do not invent numbers.
5. Plot with matplotlib and save figures (e.g. `plt.savefig("trend.png")`) so they become artifacts.
6. Write a short, honest summary: what the data shows, caveats, and any data-quality limits.

```python
import pandas as pd, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

df = pd.read_csv("data.csv")
summary = df.describe(include="all")
ax = df.groupby("month")["revenue"].sum().plot(kind="bar")
plt.tight_layout(); plt.savefig("revenue_by_month.png")
```

## Output rules
Report real results only. If the data can't answer the question, say so. Surface saved charts/files as artifacts and keep the chat summary tight.
