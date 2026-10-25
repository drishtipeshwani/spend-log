"""Forecast each card's next statement with TabPFN, and ask Gemma where to cut back.

Every card's past statement cycles become one table: a row per (card, cycle, category) with what was
spent, plus that card and category's spend in the three cycles before. TabPFN learns from all rows at
once (pooling cards and categories is what makes six months of history enough) and predicts the next
cycle's spend for each card and category as a range (10th, 50th and 90th percentile).

TabPFN runs locally; the first run asks you to accept its licence in the browser and downloads the
weights, after that it works offline and no data leaves the laptop.

Results go to data/forecast.csv (one row per card and category, plus a "Total" row per card) and
data/forecast_tips.json (Gemma's suggestions).
"""

from __future__ import annotations

import json
import os
from datetime import date

import pandas as pd

from app import llm
from app.categories import NOT_SPEND
from app.csv_store import STATEMENTS, TRANSACTIONS, read_rows, write_rows
from app.spend import inr, spend_of
from app.storage import DATA_DIR, write_private

os.environ.setdefault("TABPFN_DISABLE_TELEMETRY", "1")

FORECAST = DATA_DIR / "forecast.csv"
TIPS = DATA_DIR / "forecast_tips.json"
FORECAST_FIELDS = ["card_label", "holder", "next_period_start", "next_period_end", "next_due_date", "category",
                   "last_cycle", "avg_3", "forecast", "low", "high", "cycles_used"]
MIN_CYCLES = 3        # a card needs at least this many statements to be forecast
QUANTILES = [0.1, 0.5, 0.9]
SKIP = NOT_SPEND | {"Refund"}


def _card_label(row) -> str:
    return f"{row['bank']} {row['card']}" + (f" ··{row['card_last4']}" if row["card_last4"] else "")


def _add_month(d: date) -> date:
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    return d


def build_panel() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (history rows, rows to predict). One row per card x cycle x category."""
    txns = pd.DataFrame(read_rows(TRANSACTIONS))
    stmts = pd.DataFrame(read_rows(STATEMENTS))
    if txns.empty or stmts.empty:
        return pd.DataFrame(), pd.DataFrame()
    stmts = stmts[stmts["period_end"] != ""].copy()
    stmts["card_label"] = stmts.apply(_card_label, axis=1)
    stmts["period_end"] = pd.to_datetime(stmts["period_end"]).dt.date
    stmts = stmts.sort_values(["card_label", "period_end"])
    stmts["cycle"] = stmts.groupby("card_label").cumcount()

    txns["amount"] = txns["amount"].astype(float)
    txns["spend"] = [spend_of(c, d, a) for c, d, a in zip(txns["category"], txns["direction"], txns["amount"])]
    txns = txns[~txns["category"].isin(SKIP) & (txns["category"] != "")]
    per_cycle = (txns.merge(stmts[["statement_id", "card_label", "cycle"]], on="statement_id")
                 .groupby(["card_label", "cycle", "category"], as_index=False)["spend"].sum())

    history, future = [], []
    for card, s in stmts.groupby("card_label"):
        if len(s) < MIN_CYCLES:
            continue
        holder = s["holder"].iloc[-1]
        spent = per_cycle[per_cycle["card_label"] == card]
        categories = sorted(spent["category"].unique())
        table = (spent.pivot_table(index="cycle", columns="category", values="spend", aggfunc="sum")
                 .reindex(index=range(len(s)), columns=categories).fillna(0.0))
        months = [d.month for d in s["period_end"]] + [_add_month(s["period_end"].iloc[-1]).month]
        for category in categories:
            series = list(table[category])
            for k in range(len(series) + 1):  # the extra k is the cycle to predict
                lags = [series[k - j] if k - j >= 0 else None for j in (1, 2, 3)]
                past = series[:k]
                row = {"card_label": card, "holder": holder, "category": category, "cycle": k,
                       "month": months[k], "lag1": lags[0], "lag2": lags[1], "lag3": lags[2],
                       "mean_past": sum(past) / len(past) if past else None,
                       "seen_share": sum(1 for v in past if v > 0) / len(past) if past else None}
                if k < len(series):
                    history.append({**row, "spend": series[k]})
                else:
                    future.append(row)
    return pd.DataFrame(history), pd.DataFrame(future)


FEATURES = ["card_label", "holder", "category", "cycle", "month", "lag1", "lag2", "lag3", "mean_past", "seen_share"]
CATEGORICAL = ["card_label", "holder", "category"]


def predict(history: pd.DataFrame, future: pd.DataFrame) -> pd.DataFrame:
    """TabPFN quantile predictions for the future rows."""
    from tabpfn import TabPFNRegressor  # heavy import, only when forecasting

    X, Xf = history[FEATURES].copy(), future[FEATURES].copy()
    for col in CATEGORICAL:  # same integer codes for history and future
        codes = {v: i for i, v in enumerate(sorted(set(X[col]) | set(Xf[col])))}
        X[col], Xf[col] = X[col].map(codes), Xf[col].map(codes)
    model = TabPFNRegressor(random_state=0, categorical_features_indices=[FEATURES.index(c) for c in CATEGORICAL])
    model.fit(X.astype(float), history["spend"].astype(float))
    low, mid, high = model.predict(Xf.astype(float), output_type="quantiles", quantiles=QUANTILES)
    out = future.copy()
    out["low"], out["forecast"], out["high"] = [pd.Series(q).clip(lower=0).round(2).values for q in (low, mid, high)]
    return out


def run(log=print) -> pd.DataFrame | None:
    """Forecast every card with enough history and write data/forecast.csv. Returns the rows."""
    history, future = build_panel()
    if future.empty:
        log(f"Not enough history to forecast yet (each card needs {MIN_CYCLES}+ statements).")
        return None
    log(f"Forecasting {future['card_label'].nunique()} card(s) from {len(history)} rows of history with TabPFN...")
    pred = predict(history, future)

    stmts = pd.DataFrame(read_rows(STATEMENTS))
    stmts["card_label"] = stmts.apply(_card_label, axis=1)
    rows = []
    for card, p in pred.groupby("card_label"):
        s = stmts[stmts["card_label"] == card].sort_values("period_end")
        last = s.iloc[-1]
        last_end = date.fromisoformat(last["period_end"])
        next_end = _add_month(last_end)
        next_due = _add_month(date.fromisoformat(last["due_date"])) if last["due_date"] else None
        hist = history[history["card_label"] == card]
        common = {"card_label": card, "holder": last["holder"],
                  "next_period_start": (last_end + pd.Timedelta(days=1)).isoformat(), "next_period_end": next_end.isoformat(),
                  "next_due_date": next_due.isoformat() if next_due else "", "cycles_used": len(s)}
        for _, r in p.sort_values("forecast", ascending=False).iterrows():
            h = hist[hist["category"] == r["category"]].sort_values("cycle")["spend"]
            rows.append({**common, "category": r["category"], "last_cycle": round(h.iloc[-1], 2),
                         "avg_3": round(h.tail(3).mean(), 2), "forecast": r["forecast"], "low": r["low"],
                         "high": r["high"]})
        total = p["forecast"].sum()
        # Categories rarely all hit their extremes in the same month, so spreads add like independent
        # errors (square root of the sum of squares), not by summing every category's low and high.
        low = max(total - ((p["forecast"] - p["low"]) ** 2).sum() ** 0.5, 0)
        high = total + ((p["high"] - p["forecast"]) ** 2).sum() ** 0.5
        last_total = hist[hist["cycle"] == hist["cycle"].max()]["spend"].sum()
        avg_total = hist.groupby("cycle")["spend"].sum().tail(3).mean()
        rows.append({**common, "category": "Total", "last_cycle": round(last_total, 2), "avg_3": round(avg_total, 2),
                     "forecast": round(total, 2), "low": round(low, 2), "high": round(high, 2)})
        log(f"   {card} ({last['holder']}): next statement ~{inr(total, 0)} "
            f"({inr(low, 0)} to {inr(high, 0)})")
    write_rows(FORECAST, FORECAST_FIELDS, rows)
    return pd.DataFrame(rows)


# --- Gemma: where to cut back --------------------------------------------------------------

_TIPS_SCHEMA = {
    "type": "object",
    "properties": {"tips": {"type": "array", "items": {
        "type": "object",
        "properties": {"holder": {"type": "string"}, "category": {"type": "string"},
                       "why": {"type": "string"}, "tip": {"type": "string"}},
        "required": ["holder", "category", "why", "tip"]}}},
    "required": ["tips"],
}

_TIPS_SYSTEM = """You help an Indian family plan next month's credit card spending. Each block is one person's \
category that is forecast to rise next month. It gives the forecast, the usual (average of the last 3 \
statements), what was spent in each of the last 3 monthly statements, and the merchants behind it with \
their number of transactions and amount in each of those statements.

For every block write:
- why: one or two sentences explaining why the forecast is that high, using only the numbers given, and \
describe the pattern exactly as the numbers show it. Point to what is driving it: spending climbing \
statement after statement, a merchant used more often or costing more per order, a big latest month, or a \
charge that comes every few months (like a 3-month recharge) and is due again. If the numbers are \
roughly flat, say so. Name the merchants.
- tip: one or two friendly sentences with one concrete action aimed at those merchants or that habit, for \
example: Food delivery -> cook on two weeknights or order once less a week; Shopping -> wait 48 hours \
before non-essential orders or set a monthly cap; Fuel -> combine trips, carpool or check tyre pressure; \
Groceries -> plan a weekly list instead of many small quick-commerce orders; Bills & utilities -> AC at \
24-26°C, switch off standby devices; Subscriptions -> pause or share plans you rarely use; Cabs -> metro or \
bus for regular routes.
Never make up numbers or estimate savings. Use the ₹ sign and Indian digit grouping. No investment advice."""

TIPS_COUNT = 4
_WHEN = ("3 statements ago", "2 statements ago", "latest statement")


def _evidence(holder: str, category: str) -> dict:
    """What a person spent on a category in each of their last 3 statements (cards lined up by recency),
    and which merchants it went to: the facts behind a tip, shown on the dashboard and given to Gemma."""
    txns = pd.DataFrame(read_rows(TRANSACTIONS))
    stmts = pd.DataFrame(read_rows(STATEMENTS))
    stmts = stmts[(stmts["holder"] == holder) & (stmts["period_end"] != "")].copy()
    stmts["card_label"] = stmts.apply(_card_label, axis=1)
    stmts["ago"] = stmts.sort_values("period_end").groupby("card_label").cumcount(ascending=False)
    recent = stmts[stmts["ago"] < 3].set_index("statement_id")["ago"]
    t = txns[txns["statement_id"].isin(recent.index) & (txns["category"] == category)].copy()
    t["amount"] = t["amount"].astype(float)
    t["spend"] = [spend_of(c, d, a) for c, d, a in zip(t["category"], t["direction"], t["amount"])]
    t["slot"] = 2 - t["statement_id"].map(recent)  # 0 = oldest of the three, 2 = latest
    t["merchant"] = t["merchant"].where(t["merchant"] != "", t["description"])
    history = [round(float(t.loc[t["slot"] == k, "spend"].sum()), 2) for k in range(3)]
    merchants = []
    for name, m in t.groupby("merchant"):
        merchants.append({"merchant": name, "total": round(float(m["spend"].sum()), 2),
                          "count": [int((m["slot"] == k).sum()) for k in range(3)],
                          "spent": [round(float(m.loc[m["slot"] == k, "spend"].sum()), 2) for k in range(3)]})
    merchants.sort(key=lambda m: -m["total"])
    return {"history": history, "merchants": merchants[:3]}


def tips(forecast: pd.DataFrame, log=print) -> list[dict]:
    """Ask the local model why the biggest rises are forecast and where to cut back.
    Saved to data/forecast_tips.json with the numbers behind each one."""
    rows = forecast[forecast["category"] != "Total"]
    # One line per person and category (across their cards); only rises, biggest first.
    per = rows.groupby(["holder", "category"], as_index=False)[["forecast", "avg_3"]].sum()
    per["rise"] = per["forecast"] - per["avg_3"]
    rising = per[(per["rise"] >= 100) & (per["rise"] >= 0.03 * per["avg_3"])]
    rising = rising.sort_values("rise", ascending=False).head(TIPS_COUNT)
    if rising.empty:
        log("   No tips: nothing is forecast to rise.")
        write_private(TIPS, "[]")
        return []

    facts, blocks = {}, []
    for r in rising.itertuples():
        ev = _evidence(r.holder, r.category)
        facts[(r.holder, r.category)] = {"holder": r.holder, "category": r.category,
                                         "forecast": round(float(r.forecast), 2), "avg_3": round(float(r.avg_3), 2),
                                         "rise": round(float(r.rise), 2), **ev}
        lines = [f"{r.holder} | {r.category}: forecast {inr(r.forecast, 0)}, usual {inr(r.avg_3, 0)}, "
                 f"up {inr(r.rise, 0)}",
                 "  spent: " + "; ".join(f"{w} {inr(v, 0)}" for w, v in zip(_WHEN, ev["history"]))]
        h = ev["history"]
        if h[0] > 0 and not h[1] and not h[2]:
            lines.append("  note: spent only 3 statements ago and nothing since, so this looks like a charge "
                         "that comes every few months and is due again")
        lines += [f"  {m['merchant']}: " + "; ".join(f"{w} {c} transaction(s) {inr(v, 0)}"
                                                   for w, c, v in zip(_WHEN, m["count"], m["spent"]))
                  for m in ev["merchants"]]
        blocks.append("\n".join(lines))
    try:
        reply = llm.chat_json(_TIPS_SYSTEM, "\n\n".join(blocks), _TIPS_SCHEMA)
    except llm.LLMUnavailable as e:
        log(f"   No tips: model unavailable ({e})")
        return []
    good = []
    for t in reply.get("tips", []):
        key = (t.get("holder"), t.get("category"))
        if key in facts and t.get("tip") and key not in {(g["holder"], g["category"]) for g in good}:
            good.append({**facts[key], "why": t.get("why", "").strip(), "tip": t["tip"].strip()})
    write_private(TIPS, json.dumps(good, indent=2, ensure_ascii=False))
    log(f"   {len(good)} tip(s) from {llm.MODEL}")
    return good


def update(log=print) -> None:
    """Forecast, then tips. Safe to call after every build: problems are reported, not raised."""
    try:
        result = run(log)
    except ImportError:
        log("Forecast skipped: TabPFN isn't installed (pip install tabpfn).")
        return
    except Exception as e:  # e.g. licence not accepted yet, or no network for the first download
        log(f"Forecast skipped: {type(e).__name__}: {e}")
        return
    if result is not None:
        tips(result, log)
