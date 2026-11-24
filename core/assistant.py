"""'Ask your receipts': a Claude tool-use loop over a read-only view of the ledger."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from typing import Callable

import pandas as pd

from . import ai, db

MAX_TURNS = 10

TOOLS = [
    {
        "name": "run_sql",
        "description": (
            "Run one read-only SQLite SELECT against the user's expense database and get the rows back "
            "(max 200). Use it to answer any question about spending. Tables: receipts, line_items, budgets."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT (or WITH ... SELECT) statement"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
    {
        "name": "show_chart",
        "description": (
            "Draw a chart for the user from a SELECT query. Use it when a trend or comparison is easier to see "
            "than read. The query must return the x column and one numeric y column."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "sql": {"type": "string"},
                "chart_type": {"type": "string", "enum": ["bar", "horizontal_bar", "line"]},
                "x": {"type": "string", "description": "Column for the x axis (categories for horizontal_bar)"},
                "y": {"type": "string", "description": "Numeric column to plot"},
            },
            "required": ["title", "sql", "chart_type", "x", "y"],
            "additionalProperties": False,
        },
    },
]

SCHEMA_DOC = """receipts(
  id INTEGER, merchant TEXT, merchant_key TEXT (normalised lowercase name, good for GROUP BY),
  purchase_date TEXT 'YYYY-MM-DD', total REAL (in the receipt's own currency), currency TEXT (ISO code),
  total_base REAL (total converted to the base currency at the purchase-date rate; USE THIS for sums across currencies),
  base_currency TEXT, subtotal REAL, tax REAL, tip REAL (native currency, may be NULL),
  payment_method TEXT, category TEXT, purpose TEXT, notes TEXT, tags TEXT (comma separated),
  is_business INTEGER 0/1, reimbursable INTEGER 0/1, reimburse_status TEXT ('n/a','pending','submitted','reimbursed'),
  source TEXT ('ai','ocr','manual','import','legacy','demo'))
line_items(receipt_id INTEGER -> receipts.id, description TEXT, quantity REAL, unit_price REAL, amount REAL (native currency))
budgets(category TEXT, monthly_limit REAL (base currency))"""


@dataclass
class Turn:
    """What the UI shows for one assistant answer."""
    text: str = ""
    charts: list[dict] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)


def system_prompt(base: str, categories: list[str]) -> str:
    return f"""You are the analyst inside a personal receipt and expense tracker. Answer questions about the user's spending by querying their SQLite database with the tools.

Schema:
{SCHEMA_DOC}

Facts:
- Today is {Date.today().isoformat()}. Base currency: {base}.
- Categories: {', '.join(categories)}.
- Dates are TEXT; use strftime('%Y-%m', purchase_date), date('now','start of month') and similar.
- Match merchants with merchant_key LIKE '%term%' (lowercase) since names vary.

How to answer:
- Always check the data with run_sql before stating numbers; never guess.
- Report money in {base} using total_base unless the user asks about a specific currency.
- Lead with the direct answer, then any useful context, in a few short lines of Markdown. Use a small table only when it helps.
- Call show_chart when a trend over time or a ranking is the clearest answer.
- If the data can't answer the question, say what's missing."""


def _frame_text(df: pd.DataFrame) -> str:
    if df.empty:
        return "(no rows)"
    return df.to_csv(index=False)[:12000]


def ask(history: list, question: str, base: str, categories: list[str],
        on_status: Callable[[str], None] = lambda s: None) -> tuple[list, Turn]:
    """Run the tool loop. ``history`` holds raw API messages and is returned extended."""
    messages = [*history, {"role": "user", "content": question}]
    turn = Turn()
    for _ in range(MAX_TURNS):
        on_status("Thinking…")
        resp = ai.call(
            max_tokens=16000,
            system=system_prompt(base, categories),
            tools=TOOLS,
            output_config={"effort": "medium"},
            messages=messages,
        )
        # Pass content back unchanged: thinking blocks must be replayed as-is.
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            turn.text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if resp.stop_reason == "max_tokens":
                turn.text += "\n\n_(answer was cut off)_"
            return messages, turn

        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            args = block.input if isinstance(block.input, dict) else {}
            sql = str(args.get("sql", ""))
            turn.queries.append(sql)
            try:
                on_status(f"Querying: `{sql[:90]}`")
                df = db.run_readonly_query(sql)
                if block.name == "show_chart":
                    x, y = args.get("x"), args.get("y")
                    if x not in df.columns or y not in df.columns:
                        raise ValueError(f"Columns {x!r}/{y!r} not in result {list(df.columns)}")
                    turn.charts.append({"title": args.get("title", ""), "df": df,
                                        "type": args.get("chart_type", "bar"), "x": x, "y": y})
                    content = f"Chart shown to the user ({len(df)} rows)."
                else:
                    content = _frame_text(df)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
            except Exception as exc:  # report SQL errors back so Claude can fix its query
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": f"Error: {exc}", "is_error": True})
        messages.append({"role": "user", "content": results})

    turn.text = "I ran out of steps answering that. Try a narrower question."
    return messages, turn
