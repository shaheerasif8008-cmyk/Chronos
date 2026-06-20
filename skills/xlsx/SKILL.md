---
name: xlsx
description: Create and analyze Microsoft Excel (.xlsx) spreadsheets — tables, formulas, multiple sheets, and charts. Use when the user asks for a spreadsheet, Excel file, or data workbook with charts.
requires_connectors: []
spawns_sub_agent: false
---

# XLSX

Build Excel workbooks in the task workspace.

## When to use
- "Create a spreadsheet / Excel file / .xlsx"
- "Put this data into a workbook with charts"
- "Add formulas / multiple sheets"

## Creating a workbook
`.xlsx` is binary — use `code__python` with openpyxl (preinstalled):

```python
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference

wb = Workbook()
ws = wb.active
ws.title = "Summary"
ws.append(["Month", "Revenue"])
for row in [["Jan", 1200], ["Feb", 1500], ["Mar", 1800]]:
    ws.append(row)
ws["D1"] = "Total"
ws["D2"] = "=SUM(B2:B4)"

chart = BarChart()
data = Reference(ws, min_col=2, min_row=1, max_row=4)
chart.add_data(data, titles_from_data=True)
ws.add_chart(chart, "F2")
wb.save("workbook.xlsx")
```

For heavier analysis, load with pandas (`pd.read_excel`), compute, then write back with `df.to_excel(...)`.

## Output rules
Save with a relative path, summarize briefly, and surface the artifact.
