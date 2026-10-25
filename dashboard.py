"""Spend Log dashboard: where the family's money goes.

One overview page. Click a category, a card or a statement to open its own view with every
transaction behind it.

    streamlit run dashboard.py

Reads data/transactions.csv, statements.csv and merchants.csv. Runs on localhost only.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from app.categories import CATEGORIES, NOT_SPEND, apply_categories, merchant_key
from app.forecast import FORECAST, TIPS
from app.csv_store import (MERCHANTS, MERCHANT_FIELDS, STATEMENTS, TRANSACTIONS, TRANSACTION_FIELDS,
                           read_rows, write_rows)
from app.spend import inr, inr_short, spend_of
from app.storage import DATA_DIR, AccountDirs

# Palette "sloth moss" (theme in .streamlit/config.toml): moss green for the main marks and actions, clay
# for highlights, on warm paper with white cards. People get moss, blue, clay and plum, checked with the
# dataviz palette validator (colour-blind safe on white and on the paper background).
MOSS, CLAY, SAGE = "#2F5D46", "#C46A3C", "#C9D8B6"
PAPER, CARD, LINE = "#F3F1EA", "#FFFFFF", "#E2DFD2"
INK, MUTED_INK = "#1E2A22", "#5E6A5E"
PEOPLE_COLORS = ["#1F7A4D", "#3D74B8", "#C8622E", "#8A4F9E"]
TODAY = date.today()

st.set_page_config(page_title="Spend Log", page_icon="💳", layout="wide", initial_sidebar_state="collapsed")
st.markdown(f"""<style>
  [data-testid="stSidebarCollapsedControl"] {{ display: none; }}
  /* Streamlit's transparent top bar (app menu) isn't used here. */
  header[data-testid="stHeader"] {{ display: none; }}
  .block-container {{ padding-top: 1.2rem; }}
  /* White cards on paper: containers keyed "card_…" and the stat tiles. */
  [class*="st-key-card_"] {{ background: {CARD}; border: 1px solid {LINE} !important; border-radius: 14px; }}
  div[data-testid="stMetric"] {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 14px;
      padding: 12px 16px; }}
  [class*="st-key-card_"] div[data-testid="stMetric"] {{ border: none; padding: 0; }}
  div[data-testid="stMetricValue"] {{ font-weight: 600; letter-spacing: -0.02em; }}
  h4 {{ letter-spacing: -0.01em; }}
</style>""", unsafe_allow_html=True)


# --- Data ----------------------------------------------------------------------------------

def _mtime(path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


@st.cache_data
def load(stamp: tuple) -> tuple[pd.DataFrame, pd.DataFrame]:
    """`stamp` (data folder + file times) is part of the cache key, so edits on disk are picked up."""
    txns = pd.DataFrame(read_rows(TRANSACTIONS), columns=TRANSACTION_FIELDS)
    stmts = pd.DataFrame(read_rows(STATEMENTS))
    if txns.empty:
        return txns, stmts
    txns["amount"] = txns["amount"].astype(float)
    txns["date"] = pd.to_datetime(txns["date"])
    txns["month"] = txns["date"].dt.strftime("%Y-%m")
    txns["category"] = txns["category"].replace("", "Uncategorized").fillna("Uncategorized")
    txns["merchant"] = txns["merchant"].where(txns["merchant"] != "", txns["description"])
    txns["person"] = txns["holder"].str.capitalize()
    txns["card_label"] = card_label(txns)
    txns["spend"] = [spend_of(c, d, a) for c, d, a in zip(txns["category"], txns["direction"], txns["amount"])]
    if not stmts.empty:
        stmts["person"] = stmts["holder"].str.capitalize()
        stmts["card_label"] = card_label(stmts)
        stmts["due"] = pd.to_datetime(stmts["due_date"], errors="coerce")
    return txns, stmts


def card_label(df: pd.DataFrame) -> pd.Series:
    return df["bank"] + " " + df["card"] + df["card_last4"].map(lambda d: f" ··{d}" if d else "")


def data_stamp() -> tuple:
    return (str(DATA_DIR), _mtime(TRANSACTIONS), _mtime(STATEMENTS), _mtime(MERCHANTS),
            _mtime(FORECAST), _mtime(TIPS))


@st.cache_data
def load_forecast(stamp: tuple) -> tuple[pd.DataFrame, list[dict]]:
    """`stamp` is part of the cache key, so a new forecast on disk is picked up."""
    rows = pd.DataFrame(read_rows(FORECAST))
    if not rows.empty:
        for col in ("last_cycle", "avg_3", "forecast", "low", "high"):
            rows[col] = rows[col].astype(float)
        rows["person"] = rows["holder"].str.capitalize()
    tips = json.loads(TIPS.read_text()) if TIPS.exists() else []
    return rows, tips


def save_category_changes(changes: dict[str, str]) -> None:
    """Set categories by merchant key (as the user's choice) and re-apply them to every transaction."""
    merchants = {m["key"]: m for m in read_rows(MERCHANTS)}
    txns = read_rows(TRANSACTIONS)
    names = {merchant_key(t["description"]): t["merchant"] or t["description"] for t in txns}
    for key, category in changes.items():
        merchants[key] = {"key": key, "merchant": merchants.get(key, {}).get("merchant") or names.get(key, key),
                          "category": category, "category_source": "user"}
    apply_categories(txns, merchants)
    write_rows(MERCHANTS, MERCHANT_FIELDS, sorted(merchants.values(), key=lambda m: m["key"]))
    write_rows(TRANSACTIONS, TRANSACTION_FIELDS, txns)


# --- Navigation (kept in the URL, so views can be bookmarked) -------------------------------

def go(view: str = "", key: str = "") -> None:
    st.query_params.clear()
    if view:
        st.query_params.update({"view": view, "key": key})
    # Clickable charts and tables remember their last click; new keys after navigating start them clean.
    st.session_state["nav"] = st.session_state.get("nav", 0) + 1
    st.rerun()


def nav_key(name: str) -> str:
    return f"{name}_{st.session_state.get('nav', 0)}"


# --- Charts --------------------------------------------------------------------------------

def _axis(**kw) -> alt.Axis:
    kw.setdefault("labelFontSize", 12)
    return alt.Axis(labelColor=kw.pop("labelColor", MUTED_INK), gridColor=LINE, domainColor=LINE,
                    tickColor=LINE, **kw)


def summarize(df: pd.DataFrame, by: str) -> pd.DataFrame:
    out = df.groupby(by, as_index=False).agg(spend=("spend", "sum"), count=("spend", "size"))
    return out[out["spend"] != 0].sort_values("spend", ascending=False)


def bar_chart(df: pd.DataFrame, label: str, title: str, color: alt.Color | str = MOSS,
              clickable: bool = False, prev: pd.Series | None = None, prev_name: str = "") -> alt.Chart:
    """Horizontal bars sorted by value. Each axis label carries its amount ("Groceries · ₹74,891").
    With `prev` (spend per label in the period before), a tick on each bar marks where it was."""
    df = df.copy()
    total = df["spend"].sum()
    df["amount_label"] = df["spend"].map(lambda v: inr(v, 0))
    df["axis_label"] = df[label].astype(str) + " · " + df["amount_label"]
    df["share"] = df["spend"] / total if total else 0
    order = list(df.sort_values("spend", ascending=False)["axis_label"])
    tooltip = [alt.Tooltip(f"{label}:N", title=title), alt.Tooltip("amount_label:N", title="Spent"),
               alt.Tooltip("share:Q", title="Share", format=".1%"), alt.Tooltip("count:Q", title="Transactions")]
    if prev is not None:
        df["prev"] = df[label].map(prev).fillna(0.0)
        df["prev_label"] = df["prev"].map(lambda v: inr(v, 0))
        diff = df["spend"] - df["prev"]
        df["change"] = ["new" if not b else f"▲ {inr_short(d)} more" if d >= 1 else f"▼ {inr_short(-d)} less"
                        if d <= -1 else "same" for d, b in zip(diff, df["prev"])]
        df["trend"] = ["up" if d >= 1 else "down" if d <= -1 else "same" for d in diff]
        tooltip += [alt.Tooltip("prev_label:N", title=prev_name.capitalize() or "Before"),
                    alt.Tooltip("change:N", title="Change")]
    y = alt.Y("axis_label:N", sort=order, title=None,
              axis=_axis(labelLimit=260, labelPadding=6, labelOverlap=False, labelColor=INK, ticks=False))
    # Room at the right for the "▲ ₹1.2K more" note after each bar.
    x_scale = alt.Scale(domain=[0, df["spend"].max() * 1.45], nice=False) if prev is not None else alt.Undefined
    chart = alt.Chart(df).mark_bar(cornerRadiusEnd=4, size=18, cursor="pointer" if clickable else "default").encode(
        y=y, x=alt.X("spend:Q", title=None, scale=x_scale, axis=_axis(format="~s", grid=True, tickCount=4)),
        color=color if isinstance(color, alt.Color) else alt.value(color), tooltip=tooltip)
    if clickable:
        chart = chart.add_params(alt.selection_point(name="pick", fields=[label], on="click"))
    if prev is not None:
        notes = alt.Chart(df).mark_text(align="left", dx=6, fontSize=12, fontWeight=500).encode(
            y=y, x=alt.X("spend:Q", scale=x_scale), text="change:N", tooltip=tooltip,
            color=alt.Color("trend:N", scale=alt.Scale(domain=["up", "down", "same"], range=[CLAY, MOSS, MUTED_INK]),
                            legend=None))
        chart = alt.layer(chart, notes)
    return chart.properties(height=max(110, 30 * len(df) + 30))


def clicked(event, field: str) -> str | None:
    """The value clicked in a clickable chart, if any."""
    try:
        points = event["selection"]["pick"]
    except (KeyError, TypeError):
        return None
    return points[0].get(field) if points else None


def people_scale(people: list[str]) -> alt.Scale:
    """Each person keeps the same color whatever the filter, by their place in the full list."""
    return alt.Scale(domain=people, range=PEOPLE_COLORS[:len(people)])


def monthly_chart(rows: pd.DataFrame, people: list[str], all_people: list[str], weekly: bool = False) -> alt.Chart:
    """Spend per month (or per week, for short periods), stacked by person."""
    rows = rows.copy()
    if weekly:
        week_start = rows["date"] - pd.to_timedelta(rows["date"].dt.day_of_week, unit="D")
        rows["bucket"], rows["sort"] = week_start.dt.strftime("%d %b"), week_start
    else:
        rows["bucket"], rows["sort"] = pd.to_datetime(rows["month"]).dt.strftime("%b %Y"), rows["month"]
    grouped = rows.groupby(["bucket", "sort", "person"], as_index=False)["spend"].sum()
    grouped["amount_label"] = grouped["spend"].map(lambda v: inr(v, 0))
    order = list(grouped.sort_values("sort")["bucket"].drop_duplicates())
    color = (alt.Color("person:N", scale=people_scale(all_people), title=None,
                       legend=alt.Legend(orient="top", labelFontSize=13, symbolSize=120))
             if len(people) > 1 else alt.value(MOSS))
    label = "Week of" if weekly else "Month"
    return alt.Chart(grouped).mark_bar(cornerRadiusEnd=4, size=28 if not weekly else 22, stroke=PAPER,
                                       strokeWidth=2).encode(
        x=alt.X("bucket:N", title=None, sort=order, axis=_axis(labelAngle=0)),
        y=alt.Y("spend:Q", title=None, stack=True, axis=_axis(format="~s", tickCount=5)),
        color=color, order=alt.Order("person:N"),
        tooltip=[alt.Tooltip("bucket:N", title=label), alt.Tooltip("person:N", title="Person"),
                 alt.Tooltip("amount_label:N", title="Spent")],
    ).properties(height=280)


# --- Shared pieces -------------------------------------------------------------------------

def metrics(rows: pd.DataFrame, extra: tuple[str, str, str] | None = None) -> None:
    """Spent and number of transactions in the selected period (plus one extra tile if given)."""
    spent = rows.loc[rows["spend"] != 0, "spend"].sum()
    cols = st.columns(4)
    cols[0].metric("Spent", inr_short(spent), help=inr(spent))
    cols[1].metric("Transactions", f"{int((rows['spend'] != 0).sum())}")
    if extra:
        cols[2].metric(extra[0], extra[1], help=extra[2] or None)


def transactions_table(rows: pd.DataFrame, key: str, hide: tuple = ()) -> None:
    """Editable transactions; `hide` drops columns that would repeat one value (e.g. Card in a card's view)."""
    rows = rows.sort_values("date", ascending=False)
    table = pd.DataFrame({
        "Date": rows["date"].dt.date, "Person": rows["person"], "Card": rows["card_label"],
        "Merchant": rows["merchant"], "Description": rows["description"],
        "Amount": rows["amount"].map(inr), "Type": rows["direction"].str.capitalize(),
        "Category": rows["category"], "key": rows["description"].map(merchant_key),
    })
    st.markdown(f"**Transactions** · {len(table)}")
    edited = st.data_editor(
        table, hide_index=True, width="stretch", key=nav_key(key),
        disabled=[c for c in table.columns if c != "Category"],
        column_config={"Category": st.column_config.SelectboxColumn(
                           "Category", options=CATEGORIES + ["Uncategorized"], required=True,
                           help="Change it to fix the category for every transaction from that merchant"),
                       "key": None, **{c: None for c in hide}},
    )
    changes = {k: c for k, c, before in zip(edited["key"], edited["Category"], table["Category"]) if c != before}
    if changes:
        st.warning(f"{len(changes)} merchant(s) changed: " + ", ".join(
            f"{table.loc[table['key'] == k, 'Merchant'].iloc[0]} → {c}" for k, c in changes.items()))
        if st.button("Save category changes", type="primary"):
            save_category_changes({k: c for k, c in changes.items() if c != "Uncategorized"})
            st.toast("Saved. Every transaction from those merchants is updated.")
            go(st.query_params.get("view", ""), st.query_params.get("key", ""))


PERIODS = {"Last 30 days": 30, "Past 2 months": 2, "Past 3 months": 3, "Past 6 months": 6, "Custom": "custom"}
PERIOD_PILLS = {"30 days": "Last 30 days", "2 months": "Past 2 months", "3 months": "Past 3 months",
                "6 months": "Past 6 months", "Custom": "Custom"}


def months_ago(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m + 1, day)
        except ValueError:
            continue
    return d


def due_note(d) -> str:
    if pd.isna(d):
        return ""
    days = (d.date() - TODAY).days
    return f"in {days} days" if days > 0 else "today" if days == 0 else f"{-days} days ago"


def statements_table(s: pd.DataFrame, key: str) -> str | None:
    """Clickable list of one card's statements, newest first. Returns the picked statement's id."""
    s = s.sort_values("period_end", ascending=False).reset_index(drop=True)
    def period(r):
        a, b = pd.Timestamp(r["period_start"]), pd.Timestamp(r["period_end"])
        return f"{a:%d %b} – {b:%d %b %Y}"
    latest = s.index == 0  # only the newest statement's bill is still to pay
    shown = pd.DataFrame({
        "Period": s.apply(period, axis=1),
        "Total due": s["total_due"].map(lambda v: inr(float(v)) if v else ""),
        "Due date": [f"{d:%d %b %Y}" if l and pd.notna(d) else "" for d, l in zip(s["due"], latest)],
        "When": [due_note(d) if l else "" for d, l in zip(s["due"], latest)],
        "Transactions": s["row_count"],
    })
    event = st.dataframe(shown, hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row", key=nav_key(key))
    rows = event.selection.rows if event else []
    return s.loc[rows[0], "statement_id"] if rows else None


def _spread(df: pd.DataFrame) -> tuple[float, float, float]:
    """Total forecast with its likely range. Spreads add like independent errors (as in app/forecast.py)."""
    total = df["forecast"].sum()
    low = max(total - ((df["forecast"] - df["low"]) ** 2).sum() ** 0.5, 0)
    return total, low, total + ((df["high"] - df["forecast"]) ** 2).sum() ** 0.5


def forecast_bars(df: pd.DataFrame, tooltip: list, row_height: int = 34) -> alt.LayerChart:
    """One bar per item for its most likely amount, with the likely range written after it.
    `df` needs label, forecast, low and high."""
    df = df.assign(range_text=[f"likely {inr_short(lo)} – {inr_short(hi)}" for lo, hi in zip(df["low"], df["high"])])
    y = alt.Y("label:N", sort=list(df["label"]), title=None, axis=_axis(
        labelLimit=320, labelOverlap=False, labelColor=INK, ticks=False, labelPadding=8,
        labelExpr="split(datum.label, '|')", labelLineHeight=15))
    x = alt.X("forecast:Q", title=None, scale=alt.Scale(domain=[0, df["forecast"].max() * 1.6], nice=False),
              axis=_axis(format="~s", tickCount=4, grid=True))
    bars = alt.Chart(df).mark_bar(cornerRadiusEnd=4, size=18, color=MOSS).encode(y=y, x=x, tooltip=tooltip)
    text = alt.Chart(df).mark_text(align="left", dx=6, fontSize=12, color=MUTED_INK).encode(
        y=y, x=x, text="range_text:N", tooltip=tooltip)
    return (bars + text).properties(height=row_height * len(df) + 30)


def tip_chart(t: dict) -> alt.LayerChart:
    """The last 3 statements and next month's forecast for one tip's category: the "why" at a glance."""
    data = pd.DataFrame({"when": ["3 statements ago", "2 ago", "Latest", "Next (forecast)"],
                         "spend": t["history"] + [t["forecast"]], "kind": ["past"] * 3 + ["next"]})
    data["text"] = data["spend"].map(lambda v: inr_short(v) if v else "₹0")
    x = alt.X("when:N", sort=list(data["when"]), title=None, axis=_axis(labelAngle=0, ticks=False, domain=False))
    y = alt.Y("spend:Q", title=None, axis=None, scale=alt.Scale(domainMax=max(data["spend"].max(), t["avg_3"]) * 1.25))
    bars = alt.Chart(data).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, size=30).encode(
        x=x, y=y, color=alt.Color("kind:N", scale=alt.Scale(domain=["past", "next"], range=[SAGE, MOSS]), legend=None),
        tooltip=[alt.Tooltip("when:N", title="Statement"), alt.Tooltip("text:N", title="Spent")])
    labels = alt.Chart(data).mark_text(dy=-8, fontSize=12, color=INK).encode(x=x, y=y, text="text:N")
    return (bars + labels).resolve_scale(y="shared").properties(height=130, background="transparent")


def forecast_section(fc: pd.DataFrame, tips: list[dict], people: list[str], cards: list[str], key: str) -> None:
    """Next month's bills (all selected cards, per person, per card), the categories behind them, and
    Gemma's reasons and tips for the biggest rises."""
    fc = fc[fc["person"].isin(people) & fc["card_label"].isin(cards)] if not fc.empty else fc
    if fc.empty:
        st.caption("Forecasts appear once a card has 3 or more statements.")
        return
    totals = fc[fc["category"] == "Total"].sort_values("forecast", ascending=False)

    # Headline: every selected card added up, then each person's share
    shown_people = [p for p in people if p in set(totals["person"])]
    tiles = st.columns([1.4] + [1] * len(shown_people) if len(shown_people) > 1 else [1, 2])
    groups = [("All selected cards", totals)] + ([(p, totals[totals["person"] == p]) for p in shown_people]
                                                 if len(shown_people) > 1 else [])
    for i, (col, (name, g)) in enumerate(zip(tiles, groups)):
        total, low, high = _spread(g)
        usual = g["avg_3"].sum()
        dues = pd.to_datetime(g["next_due_date"], errors="coerce").dropna()
        due = (f"due {dues.min():%d %b}" if dues.min() == dues.max() else f"due {dues.min():%d %b} – {dues.max():%d %b}") \
            if not dues.empty else ""
        with col.container(border=True, key=f"card_fc_{i}"):
            st.metric(f"{name} · next bill{'s' if len(g) > 1 else ''}", inr(total, 0),
                      delta=f"{(total - usual) / usual:+.0%} on last 3 months' avg" if usual else None,
                      delta_color="inverse",
                      help=f"Likely between {inr(low, 0)} and {inr(high, 0)}. The last 3 statements averaged "
                           f"{inr(usual, 0)}; one-off big spends in them aren't expected to repeat.")
            st.caption(f"{len(g)} card{'s' if len(g) > 1 else ''} · likely {inr_short(low)} – {inr_short(high)}"
                       + (f" · {due}" if due else ""))

    legend = "Each bar is the most likely amount; the grey note is the range it will probably fall in"
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("**Each card's next bill**")
        st.caption(legend)
        cards_df = totals.copy()
        cards_df["label"] = cards_df["card_label"] + "|" + cards_df["person"] + " · " + cards_df["forecast"].map(lambda v: inr(v, 0))
        for col in ("avg_3", "forecast", "low", "high"):
            cards_df[f"{col}_text"] = cards_df[col].map(lambda v: inr(v, 0))
        cards_df["due_text"] = pd.to_datetime(cards_df["next_due_date"], errors="coerce").dt.strftime("%d %b %Y").fillna("")
        tooltip = [alt.Tooltip("card_label:N", title="Card"), alt.Tooltip("person:N", title="Person"),
                   alt.Tooltip("forecast_text:N", title="Forecast"), alt.Tooltip("low_text:N", title="Likely from"),
                   alt.Tooltip("high_text:N", title="Likely to"), alt.Tooltip("avg_3_text:N", title="Last 3 months' avg"),
                   alt.Tooltip("due_text:N", title="Due around")]
        st.altair_chart(forecast_bars(cards_df, tooltip, row_height=44), width="stretch", key=nav_key(f"{key}_cards"))
    with right:
        # The 5 categories most of next month's money is likely to go to. Rare one-offs (an insurance
        # premium, a flight) forecast near zero and would only clutter the chart.
        cats = (fc[fc["category"] != "Total"].groupby("category", as_index=False)[["avg_3", "forecast", "low", "high"]]
                .sum().sort_values("forecast", ascending=False).head(5))
        cats["label"] = cats["category"] + " · " + cats["forecast"].map(lambda v: inr(v, 0))
        for col in ("avg_3", "forecast", "low", "high"):
            cats[f"{col}_text"] = cats[col].map(lambda v: inr(v, 0))
        tooltip = [alt.Tooltip("category:N", title="Category"), alt.Tooltip("forecast_text:N", title="Forecast"),
                   alt.Tooltip("low_text:N", title="Likely from"), alt.Tooltip("high_text:N", title="Likely to"),
                   alt.Tooltip("avg_3_text:N", title="Last 3 months' avg")]
        st.markdown("**Where the money will likely go** · top 5 categories")
        st.caption(legend)
        st.altair_chart(forecast_bars(cats, tooltip), width="stretch", key=nav_key(f"{key}_forecast"))

    st.markdown("**Where to cut back** · the biggest rises, and why")
    shown = [t for t in tips if t["holder"].capitalize() in people and "history" in t]
    if not shown:
        st.caption("Nothing is forecast to rise much for these filters." if tips is not None else "")
    for row_start in range(0, len(shown), 2):
        cols = st.columns(2, gap="medium")
        for i, (col, t) in enumerate(zip(cols, shown[row_start:row_start + 2])):
            with col.container(border=True, key=f"card_tip_{row_start + i}"):
                st.markdown(f"**{t['category']}** · {t['holder'].capitalize()}")
                st.markdown(f"Likely **{inr(t['forecast'], 0)}** next month, up {inr(t['rise'], 0)} on the last 3 "
                            f"months' average of {inr(t['avg_3'], 0)}")
                st.altair_chart(tip_chart(t), width="stretch")
                top = " · ".join(f"{m['merchant']} ({sum(m['count'])}×, {inr_short(m['total'])})" for m in t["merchants"])
                if top:
                    st.caption(f"Last 3 statements: {top}")
                if t.get("why"):
                    st.markdown(f"**Why** · {t['why']}")
                st.markdown(f"**Try** · {t['tip']}")
    st.caption("Forecast by TabPFN from past statements; reasons and tips by Gemma. Both run on this laptop.")


def forecast_header(title: str) -> None:
    """Section title and when the forecast last ran (it runs whenever a fetch reads new statements)."""
    left, right = st.columns([3, 1], vertical_alignment="bottom")
    left.markdown(f"#### {title}")
    if FORECAST.exists():
        right.caption(f"Updated {ago(pd.Timestamp(FORECAST.stat().st_mtime, unit='s', tz='UTC'))}, "
                      "after new statements were read")


def back_button() -> None:
    if st.button("Overview", icon=":material/arrow_back:"):
        go()


# --- Page ----------------------------------------------------------------------------------

txns, stmts = load(data_stamp())
forecast_rows, forecast_tips = load_forecast(data_stamp())

def _inbox(account: str) -> str:
    return "My inbox" if account == "me" else f"{account.capitalize()}'s inbox"


class FetchProgress(io.TextIOBase):
    """Turns what a fetch prints into a short checklist per inbox, as it happens. The full printout
    is kept for "Details". Reads the progress lines of download_statements.py, build_transactions.py
    and app/forecast.py; a line it doesn't recognise just goes to the details."""

    def __init__(self, box, status):
        self.box, self.status, self.text, self.line = box, status, "", ""
        self.inboxes: dict[str, dict] = {}
        self.current: dict | None = None
        self.forecast = ""  # "", "running", "done" or "skipped"

    def write(self, s: str) -> int:
        self.text += s
        self.line += s
        while "\n" in self.line:
            line, self.line = self.line.split("\n", 1)
            if self._feed(line):
                self._render()
        return len(s)

    # Each label change passes expanded=True: st.status's update() without it collapses the box.
    def _feed(self, line: str) -> bool:
        if m := re.match(r"\[(.+?)\] Connecting to Gmail", line):
            self.current = self.inboxes[m[1]] = {"name": m[1], "emails": 0, "unlocked": 0, "locked": 0,
                                                  "to_read": 0, "read": 0, "reading": "", "phase": "checking"}
            self.status.update(label=f"Checking {_inbox(m[1]).replace('My', 'my')}...", expanded=True)
            return True
        i = self.current
        if line.startswith("Forecasting"):
            self.forecast = "running"
            self.status.update(label="Updating the forecast with TabPFN...", expanded=True)
        elif "tip(s) from" in line or "No tips" in line:
            self.forecast = "done"
        elif line.startswith("Forecast skipped"):
            self.forecast = "skipped"
        elif i is None:
            return False
        elif m := re.match(r"Reading (\d+) new statement", line):
            i["phase"], i["to_read"] = "reading", int(m[1])
            self.status.update(label=f"Reading {m[1]} statement(s) with Gemma...", expanded=True)
        elif m := re.match(r"\d{4}-\d{2}-\d{2}  (.+)$", line):
            if i["phase"] == "reading":
                i["reading"] = m[1]
            else:
                i["emails"] += 1
        elif "Unlocked with" in line or "Not password-protected" in line:
            i["unlocked"] += 1
        elif "Still locked" in line:
            i["locked"] += 1
        elif re.match(r"   .+ \(\w+\), .*: \d+ transactions", line):
            i["read"] += 1
            i["reading"] = ""
        elif line.startswith("No new statements"):
            i["phase"] = "done"
        elif line.startswith("Done.") and i["phase"] == "reading" and i["read"] >= i["to_read"]:
            i["phase"] = "done"
        else:
            return False
        return True

    def _render(self) -> None:
        out = []
        for i in self.inboxes.values():
            out.append(f"**{_inbox(i['name'])}**")
            if i["phase"] == "checking" and not i["emails"]:
                out.append(":material/hourglass_top: Checking for statement emails since the last fetch")
            if i["emails"]:
                out.append(f":material/mail: {i['emails']} new statement email{'s' if i['emails'] > 1 else ''}")
            if i["unlocked"] or i["locked"]:
                out.append(f":material/lock_open: {i['unlocked']} unlocked"
                           + (f" · :material/lock: {i['locked']} still locked" if i["locked"] else ""))
            if i["to_read"]:
                doing = f" · {i['reading']}" if i["reading"] else ""
                out.append(f":material/auto_awesome: Read with Gemma: {i['read']} of {i['to_read']}{doing}")
            if i["phase"] == "done":
                out.append(":material/check_circle: Up to date" if not i["to_read"] else
                           f":material/check_circle: {i['read']} new statement{'s' if i['read'] != 1 else ''} saved")
        if self.forecast:
            out.append("**Forecast**")
            out.append({"running": ":material/hourglass_top: Predicting next month's bills with TabPFN",
                        "done": ":material/check_circle: Forecast updated",
                        "skipped": ":material/info: Forecast skipped (see details)"}[self.forecast])
        self.box.markdown("  \n".join(out))


def ago(when) -> str:
    seconds = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(when)).total_seconds()
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'s' if n > 1 else ''} ago"
    return "just now"


def fetch_new_statements(search: str) -> None:
    """Fetch, unlock and read new statements for every connected Gmail account, showing a live checklist.
    If any were read, the forecast is updated too. What's new is kept for the summary card."""
    import download_statements
    from app import forecast, sync
    from app.cli import DEFAULT_NEWER_THAN
    accounts = download_statements.connected_accounts() or ["default"]
    totals = {"found": 0, "extracted": 0, "to read": 0, "locked": 0, "read": 0}
    before = {r["statement_id"] for r in read_rows(STATEMENTS)}
    with st.status("Fetching new statements...", expanded=True) as status:
        progress = FetchProgress(st.empty(), status)
        try:
            with contextlib.redirect_stdout(progress):
                for account in accounts:
                    since = sync.since_filter(account, search) or f"newer_than:{DEFAULT_NEWER_THAN}"
                    counts = download_statements.fetch(account, search, since)
                    for k in totals:
                        totals[k] += counts[k]
                if totals["read"]:  # new data, so a new forecast; nothing new leaves it as it is
                    print()
                    forecast.update()
        except FileNotFoundError as e:
            status.update(label="Gmail isn't set up yet", state="error")
            st.error(str(e))
            return
        except Exception as e:  # keep the page usable and show what went wrong
            status.update(label="Fetch failed", state="error")
            st.error(f"{type(e).__name__}: {e}")
            st.session_state["fetch_log"] = progress.text
            return
        new_ids = sorted({r["statement_id"] for r in read_rows(STATEMENTS)} - before)
        summary = f"{len(new_ids)} new statement{'s' if len(new_ids) != 1 else ''} read" if new_ids \
            else "No new statements, nothing changed"
        if totals["locked"]:
            summary += f", {totals['locked']} still locked"
        status.update(label=summary, state="complete", expanded=False)
    st.session_state["fetch_log"] = progress.text
    st.session_state["whats_new"] = {"ids": new_ids, "locked": totals["locked"],
                                     "forecast": progress.forecast == "done"}
    st.toast(summary)
    st.rerun()


def whats_new() -> None:
    """After a fetch: which statements just arrived, each one a click away, and whether the forecast moved."""
    news = st.session_state.get("whats_new")
    if not news:
        return
    new = stmts[stmts["statement_id"].isin(news["ids"])].sort_values("due") if not stmts.empty else stmts
    with st.container(border=True, key="card_whatsnew"):
        head, close = st.columns([8, 1], vertical_alignment="center")
        if new.empty:
            head.markdown(":material/check_circle: **Up to date** · no new statements since the last fetch"
                          + (f" · {news['locked']} still locked" if news["locked"] else ""))
        else:
            head.markdown(f":material/new_releases: **Just fetched · {len(new)} new statement"
                          f"{'s' if len(new) > 1 else ''}**"
                          + (" · forecast updated" if news["forecast"] else "")
                          + (f" · {news['locked']} still locked (add the person in Family?)" if news["locked"] else ""))
        if close.button("", icon=":material/close:", key="close_news", help="Dismiss"):
            del st.session_state["whats_new"]
            st.rerun()
        for r in new.itertuples():
            a, b, c, d = st.columns([3.2, 2.2, 1.6, 1], vertical_alignment="center")
            a.markdown(f"**{r.card_label}** · {r.person}")
            b.caption(f"{pd.Timestamp(r.period_start):%d %b} – {pd.Timestamp(r.period_end):%d %b} · "
                      f"{r.row_count} transactions")
            c.markdown(f"{inr(float(r.total_due or 0), 0)}" + (f" · due {r.due:%d %b}" if pd.notna(r.due) else ""))
            if d.button("Open", key=f"open_{r.statement_id}", width="stretch"):
                del st.session_state["whats_new"]
                go("card", r.card_label)
        if st.session_state.get("fetch_log"):
            with st.expander("Details"):
                st.code(st.session_state["fetch_log"])


@st.dialog("Family", width="large")
def family_dialog() -> None:
    """People whose cards are tracked (their name and date of birth unlock statements), and the
    Gmail inboxes statements are fetched from."""
    from app import accounts, settings, sync
    from app.profiles import Profile, load_profiles, save_profiles

    profiles = load_profiles()
    connected = {a: accounts.address(a) for a in accounts.connected()}
    by_address = {addr.lower(): a for a, addr in connected.items() if addr}
    search = settings.gmail_search()
    known = sorted(txns["person"].unique()) if not txns.empty else []

    st.caption("Name and date of birth only unlock statement passwords, here on this laptop. "
               "They're never sent to the model or anywhere else.")
    if not profiles:
        st.info("Add the first person below: whose cards should Spend Log track?")
    for n, p in enumerate(profiles):
        person = p.label.capitalize()
        color = PEOPLE_COLORS[known.index(person)] if person in known and known.index(person) < len(PEOPLE_COLORS) \
            else MUTED_INK
        mine = stmts[stmts["holder"] == p.label] if not stmts.empty else stmts
        account = by_address.get(p.email.lower()) if p.email else None
        with st.container(border=True, key=f"card_person_{n}"):
            avatar, info, status, more = st.columns([0.6, 3.4, 2.6, 0.6], vertical_alignment="center")
            avatar.markdown(f'''<div style="width:40px;height:40px;border-radius:50%;background:{color};color:{CARD};
                display:flex;align-items:center;justify-content:center;font-weight:600;font-size:18px">
                {p.full_name[:1].upper() or person[:1]}</div>''', unsafe_allow_html=True)
            cards_n = mine["card_label"].nunique() if not mine.empty else 0
            info.markdown(f"**{p.full_name}** · {person}")
            info.caption(f"Born {date.fromisoformat(p.dob):%d %b %Y} · "
                         + (f"{cards_n} card{'s' if cards_n != 1 else ''}, {len(mine)} statements" if cards_n
                            else "no statements yet"))
            if account:
                last = sync.last_fetch(account, search)
                status.markdown(f":green-badge[:material/check_circle: Gmail connected]  \n"
                                f"<span style='font-size:12px;color:{MUTED_INK}'>{p.email}"
                                + (f" · fetched {ago(last)}" if last else "") + "</span>", unsafe_allow_html=True)
            elif p.email:
                status.markdown(f":orange-badge[:material/link_off: Not connected]  \n"
                                f"<span style='font-size:12px;color:{MUTED_INK}'>{p.email}</span>",
                                unsafe_allow_html=True)
            else:
                status.markdown(f":gray-badge[:material/forward_to_inbox: No Gmail]  \n"
                                f"<span style='font-size:12px;color:{MUTED_INK}'>statements come to another inbox</span>",
                                unsafe_allow_html=True)
            with more.popover("", icon=":material/edit:", help=f"Manage {person}"):
                if account and st.button("Disconnect Gmail", key=f"disconnect_{p.label}", width="stretch"):
                    accounts.disconnect(account)
                    st.rerun(scope="fragment")
                st.caption(f"Remove {p.full_name}? Statements already read stay.")
                if st.button("Remove person", key=f"remove_{p.label}", type="primary", width="stretch"):
                    save_profiles([q for q in profiles if q.label != p.label])
                    st.rerun(scope="fragment")
            if p.email and not account:
                if st.button(f"Connect {p.email}", key=f"connect_{p.label}", icon=":material/login:",
                             help="Opens a browser window to sign in (read-only access to Gmail)"):
                    with st.spinner(f"Finish signing in as {p.email} in the browser window that opened..."):
                        try:
                            got = accounts.connect(p.label, p.email)
                            if got.lower() != p.email.lower():
                                # Signed in as someone else: keep the connection, but point the person at it.
                                save_profiles([q if q.label != p.label else Profile(**{**q.__dict__, "email": got})
                                               for q in profiles])
                                st.warning(f"You signed in as {got}, not {p.email}; {p.label}'s Gmail is now {got}.")
                            else:
                                st.success(f"Connected {got}.")
                        except ValueError as e:
                            st.warning(str(e))
                        except FileNotFoundError as e:
                            st.error(str(e))
                        except Exception as e:  # sign-in abandoned, blocked, or timed out
                            st.error(f"Sign-in didn't finish ({type(e).__name__}). Check that {p.email} is a test "
                                     "user in your Google Cloud project, then try again.")
                    st.rerun(scope="fragment")

    with st.expander("Add a person", expanded=not profiles):
        with st.form("add_person", clear_on_submit=True):
            c1, c2 = st.columns(2)
            label = c1.text_input("Who", placeholder="me, mom, dad")
            name = c2.text_input("Full name as printed on the card")
            c3, c4 = st.columns(2)
            dob = c3.date_input("Date of birth", value=None, min_value=date(1920, 1, 1), max_value=TODAY,
                                format="DD/MM/YYYY")
            email = c4.text_input("Gmail (optional)", placeholder="mom@gmail.com",
                                  help="If their statements arrive in their own inbox. Leave empty if they're "
                                       "forwarded to yours.")
            if st.form_submit_button("Save person", type="primary"):
                label = label.strip().lower()
                if not (label and name.strip() and dob):
                    st.error("Who, full name and date of birth are needed.")
                else:
                    # Mobile and card digits aren't asked here; keep any saved earlier (e.g. by setup_profile.py).
                    before = next((q for q in profiles if q.label == label), None)
                    person = Profile(label=label, full_name=name.strip(), dob=dob.isoformat(), email=email.strip(),
                                     mobile=before.mobile if before else "",
                                     card_last4=before.card_last4 if before else [])
                    save_profiles([q for q in profiles if q.label != label] + [person])
                    st.rerun(scope="fragment")

    # Inboxes connected without a person (e.g. from the command line) still get fetched; show them here.
    linked = {by_address.get(q.email.lower()) for q in profiles if q.email}
    others = {a: addr for a, addr in connected.items() if a not in linked}
    if others:
        st.markdown("**Other Gmail inboxes fetched from**")
        for account, addr in others.items():
            left, right = st.columns([5, 1], vertical_alignment="center")
            left.markdown(f"`{account}` · {addr or 'address unknown'}")
            if right.button("Disconnect", key=f"disconnect_{account}"):
                accounts.disconnect(account)
                st.rerun(scope="fragment")
    st.caption("Each address has to be listed as a test user in your Google Cloud project "
               "(Google Auth Platform → Audience → Test users) before it can connect.")


family_col, head, fetch_col = st.columns([1.6, 4, 1.8], vertical_alignment="top")
head.markdown(f"""<div style="text-align:center">
  <div style="font-size:2rem;font-weight:700;line-height:1.1;color:{MOSS}">Spend Log</div>
  <div style="font-size:0.9rem;color:{MUTED_INK};margin-top:0.25rem">
    🦥 Your family's card spends, sorted while you do nothing. Nothing leaves this laptop.</div>
</div>""", unsafe_allow_html=True)

from app import settings, sync
search = settings.gmail_search()
accounts = [a for a in AccountDirs.accounts()] or ["default"]
fetched = [t for t in (sync.last_fetch(a, search) for a in accounts) if t]
if family_col.button("Family", icon=":material/group:", width="stretch", help="Add people and connect their Gmail"):
    family_dialog()
if fetch_col.button("Fetch new statements", icon=":material/refresh:", type="primary", width="stretch",
                    help=f"Check Gmail for emails with subject \"{settings.STATEMENT_SUBJECT}\" since the last "
                         "fetch, then unlock and read them (subject set in app/settings.py)"):
    fetch_new_statements(search)
fetch_col.caption(f"Last fetched {ago(max(fetched))}" if fetched else "Not fetched from the app yet")
whats_new()

if txns.empty:  # first run: three steps, ticked off as they're done
    from app import accounts as gmail_accounts
    from app.profiles import load_profiles
    steps = [("Add your family", "Who carries the cards: name and date of birth unlock statement passwords.",
              bool(load_profiles()), ":material/group:"),
             ("Connect Gmail", "In Family, sign in to each inbox that gets statements (read-only).",
              bool(gmail_accounts.connected()), ":material/mail:"),
             ("Fetch new statements", "Spend Log finds, unlocks and reads them with Gemma, on this laptop.",
              False, ":material/refresh:")]
    st.markdown("#### Get started in three steps")
    for col, (n, (title, text, done, icon)) in zip(st.columns(3), enumerate(steps, 1)):
        with col.container(border=True, key=f"card_step_{n}"):
            st.markdown(f"{':material/check_circle:' if done else icon} **{n}. {title}**")
            st.caption(text)
    st.stop()

# Pages: Overview (with its category, card and statement views) and Forecast.
page = "forecast" if st.query_params.get("page") == "forecast" else "overview"
_, nav_col, _ = st.columns([2, 1.2, 2])
picked = nav_col.segmented_control("Page", ["Overview", "Forecast"], default=page.capitalize(),
                                   label_visibility="collapsed", width="stretch", key=nav_key("page"))
if picked and picked.lower() != page:
    st.query_params.clear()
    if picked == "Forecast":
        st.query_params["page"] = "forecast"
    st.session_state["nav"] = st.session_state.get("nav", 0) + 1
    st.rerun()

# Filters across the top (the forecast always uses all history, so it has no period filter)
all_people = sorted(txns["person"].unique())
first_day = txns["date"].min().date()
start, end, period = first_day, TODAY, ""
# Each person's pill wears their chart colour, so the pills double as the legend.
st.markdown("<style>" + "".join(
    f'''.st-key-people_pills [role="toolbar"] > button:nth-child({i + 1})::before {{ content: ""; width: 8px;
        height: 8px; border-radius: 50%; background: {PEOPLE_COLORS[i]}; margin-right: 6px; display: inline-block; }}
    .st-key-people_pills [role="toolbar"] > button[aria-pressed="true"]:nth-child({i + 1}) {{
        background: {PEOPLE_COLORS[i]}; border-color: {PEOPLE_COLORS[i]}; color: {CARD}; }}
    .st-key-people_pills [role="toolbar"] > button[aria-pressed="true"]:nth-child({i + 1})::before {{
        background: {CARD}; }}'''
    for i in range(min(len(all_people), len(PEOPLE_COLORS)))) + "</style>", unsafe_allow_html=True)

with st.container(border=True, key="card_filters"):
    f1, f2, f3 = st.columns([2, 2.6, 2], gap="large")
    people = f1.pills("People", all_people, selection_mode="multi", default=all_people, key="people_pills",
                      help="One person for a solo view, or any group") or all_people
    choice = None
    if page == "overview":
        pill = f2.pills("Period", list(PERIOD_PILLS), default="30 days", key="period_pills")
        choice = PERIOD_PILLS[pill or "30 days"]
    else:
        f2.caption("The forecast learns from every statement fetched so far.")
    if choice == "Custom":
        picked = f2.date_input("From – to", value=(max(first_day, months_ago(TODAY, 3)), TODAY),
                               min_value=min(first_day, TODAY), max_value=TODAY, format="DD/MM/YYYY",
                               label_visibility="collapsed")
        start, end = (picked[0], picked[-1]) if isinstance(picked, (tuple, list)) and picked else (first_day, TODAY)
        period = f"{start:%d %b %Y} – {end:%d %b %Y}"
    elif choice is not None:
        start = TODAY - timedelta(days=30) if PERIODS[choice] == 30 else months_ago(TODAY, PERIODS[choice])
        end = TODAY
        period = f"{choice} ({start:%d %b} – {end:%d %b %Y})"
    if choice is not None:  # the same length of time just before, for "compared with before"
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - (end - start)
        before = {"Last 30 days": "the 30 days before", "Past 2 months": "the 2 months before",
                  "Past 3 months": "the 3 months before", "Past 6 months": "the 6 months before"}.get(
                      choice, "the same stretch before")

    # Cards: a checklist grouped by person, behind one button that says how many are on.
    card_people = (txns[txns["person"].isin(people)].groupby("card_label")["person"].first()
                   .sort_values(kind="stable"))
    cards_available = sorted(card_people.index, key=lambda c: (card_people[c], c))
    # Checkboxes in a closed popover mount only when it opens, so they get an explicit default, and
    # All/None start a fresh set of keys with that default rather than writing widget state.
    gen, default = st.session_state.get("cards_gen", 0), st.session_state.get("cards_default", True)
    cards = [c for c in cards_available if st.session_state.get(f"cardcb_{gen}_{c}", default)]
    f3.markdown('<p style="font-size:14px;margin:0 0 0.45rem">Cards</p>', unsafe_allow_html=True)
    shown = "All" if len(cards) in (0, len(cards_available)) else f"{len(cards)} of"
    with f3.popover(f"{shown} {len(cards_available)} cards", icon=":material/credit_card:", width="stretch"):
        a, b = st.columns(2)
        for col, name, value in ((a, "All", True), (b, "None", False)):
            if col.button(name, width="stretch"):
                st.session_state["cards_gen"], st.session_state["cards_default"] = gen + 1, value
                st.rerun()
        for person in dict.fromkeys(card_people[cards_available]):
            st.caption(person)
            for c in cards_available:
                if card_people[c] == person:
                    st.checkbox(c, value=default, key=f"cardcb_{gen}_{c}")
    cards = cards or cards_available

in_people = txns["person"].isin(people)
in_months = txns["date"].dt.date.between(start, end)  # the selected period
view = st.query_params.get("view", "")
key = st.query_params.get("key", "")

who = "Whole family" if set(people) == set(all_people) and len(all_people) > 1 else ", ".join(people)

# --- Forecast page -------------------------------------------------------------------------
if page == "forecast":
    forecast_header(f"{who} · next statements")
    if not forecast_rows.empty:
        forecast_section(forecast_rows, forecast_tips, people, cards, "forecast")
    else:
        st.caption("No forecast yet. It appears after a fetch, once cards have 3 or more statements.")

# --- Category view -------------------------------------------------------------------------
elif view == "category":
    back_button()
    rows = txns[in_people & in_months & txns["card_label"].isin(cards) & (txns["category"] == key)]
    everything = txns[in_people & in_months & txns["card_label"].isin(cards)]["spend"].sum()
    st.header(key)
    st.caption(f"{', '.join(people)} · {period}")
    share = rows["spend"].sum() / everything if everything else 0
    metrics(rows, ("Share of spend", f"{share:.0%}", f"of {inr(everything, 0)} total"))
    if rows.empty:
        st.info("No transactions in this category for these filters.")
    else:
        left, right = st.columns(2, gap="large")
        with left:
            st.markdown("**Merchants**")
            st.altair_chart(bar_chart(summarize(rows, "merchant").head(12), "merchant", "Merchant"), width="stretch")
        with right:
            st.markdown("**Month by month**")
            st.altair_chart(monthly_chart(rows, people, all_people), width="stretch")
        transactions_table(rows, "category_txns")

# --- Card view -----------------------------------------------------------------------------
elif view == "card":
    back_button()
    st.header(key)
    card_stmts = stmts[stmts["card_label"] == key] if not stmts.empty else stmts
    if not card_stmts.empty:  # statements whose period overlaps the selected one
        card_stmts = card_stmts[(pd.to_datetime(card_stmts["period_end"]).dt.date >= start)
                                & (pd.to_datetime(card_stmts["period_start"]).dt.date <= end)]
    rows = txns[in_months & (txns["card_label"] == key)]
    holder = ", ".join(sorted(txns.loc[txns["card_label"] == key, "person"].unique())) or "—"
    st.caption(f"{holder} · {period}")
    metrics(rows)
    if not (spent := rows[rows["spend"] != 0]).empty:
        st.markdown("**Where it went**")
        st.altair_chart(bar_chart(summarize(spent, "category"), "category", "Category"), width="stretch")
    if card_stmts.empty:
        st.info("No statements for this card in this period. Try a longer period above.")
    else:
        st.markdown(f"**Statements** · {len(card_stmts)} · click one to see its transactions")
        picked = statements_table(card_stmts, "card_stmts")
        if picked:
            checks = card_stmts.loc[card_stmts["statement_id"] == picked, "checks"].iloc[0]
            if checks != "ok":  # the rows read don't add up to the totals printed on the statement
                st.warning(f"This statement's transactions may be incomplete: {checks}")
            transactions_table(txns[txns["statement_id"] == picked], f"txns_{picked}", hide=("Person", "Card"))

# --- Overview ------------------------------------------------------------------------------
else:
    in_cards = txns["card_label"].isin(cards)
    rows = txns[in_people & in_months & in_cards]
    prev = txns[in_people & in_cards & txns["date"].dt.date.between(prev_start, prev_end)]
    spent, prev_spent = rows[rows["spend"] != 0], prev[prev["spend"] != 0]
    total, prev_total = spent["spend"].sum(), prev_spent["spend"].sum()

    # One sentence that answers "how am I doing?"
    names = {"Me": "You", "Mom": "Mom", "Dad": "Dad"}
    subject = ("The family" if set(people) == set(all_people) and len(all_people) > 1
               else " and ".join(names.get(p, p) for p in people))
    change = (total - prev_total) / prev_total if prev_total else None
    compare = (f", {abs(change):.0%} {'more' if change > 0 else 'less'} than {before}" if change is not None
               and abs(change) >= 0.01 else f", about the same as {before}" if change is not None else "")
    st.markdown(f"### {subject} spent {inr(total, 0)} in the {choice.lower() if choice != 'Custom' else 'period'}"
                f"{compare}.")
    st.caption(period)

    # Tiles: spent and transactions (with change), and the next bill due
    upcoming = pd.DataFrame()
    if not stmts.empty:
        s_ = stmts[stmts["person"].isin(people) & stmts["card_label"].isin(cards)]
        latest = s_.sort_values("due").groupby("card_label").tail(1)
        upcoming = latest[latest["due"].dt.date >= TODAY].sort_values("due")
    c1, c2, c3 = st.columns(3)
    c1.metric("Spent", inr_short(total), help=inr(total),
              delta=f"{change:+.0%} vs {before}" if change is not None else None, delta_color="inverse")
    n, prev_n = len(spent), len(prev_spent)
    c2.metric("Transactions", f"{n}", delta=f"{n - prev_n:+d} vs {before}" if prev_n else None, delta_color="off")
    if not upcoming.empty:
        nxt = upcoming.iloc[0]
        c3.metric("Next bill due", inr(float(nxt["total_due"] or 0), 0),
                  delta=f"{nxt['person']} · {nxt['card_label'].split()[0]} {nxt['card_label'].rsplit(' ', 1)[-1]} · {nxt['due']:%d %b}, "
                        f"{due_note(nxt['due'])}", delta_color="off", delta_arrow="off",
                  help=f"{len(upcoming)} bill(s) coming up: "
                       + "; ".join(f"{r.card_label} ({r.person}) {inr(float(r.total_due or 0), 0)} on {r.due:%d %b}"
                                   for r in upcoming.itertuples()))
    else:
        c3.metric("Next bill due", "None", help="No upcoming due dates in the statements")

    if spent.empty:
        st.info("No spending matches these filters.")
    else:
        # Worth a look: three things you'd want to know, worked out for you
        st.markdown("#### Worth a look")
        cur_cat = spent.groupby("category")["spend"].sum()
        prev_cat = prev_spent.groupby("category")["spend"].sum()
        diff = cur_cat.sub(prev_cat, fill_value=0).sort_values(ascending=False)
        top_spend = spent.loc[spent["spend"].idxmax()]
        freq = spent.groupby("merchant").agg(times=("spend", "size"), amount=("spend", "sum")).sort_values(
            ["times", "amount"], ascending=False).iloc[0]
        w1, w2, w3 = st.columns(3)
        with w1.container(border=True, key="card_w1"):
            if not prev_spent.empty and diff.iloc[0] > 0:
                cat, rise = diff.index[0], diff.iloc[0]
                pct = f" (+{rise / prev_cat[cat]:.0%})" if prev_cat.get(cat, 0) else ""
                st.caption(":material/trending_up: Biggest jump")
                st.markdown(f"**{cat}** up {inr(rise, 0)}{pct}")
                st.caption(f"compared with {before}")
            else:
                st.caption(":material/trending_flat: Nothing went up")
                st.markdown("**No category rose**")
                st.caption(f"compared with {before}")
        with w2.container(border=True, key="card_w2"):
            st.caption(":material/payments: Biggest single spend")
            st.markdown(f"**{top_spend['merchant']}** · {inr(top_spend['spend'], 0)}")
            st.caption(f"{top_spend['date']:%d %b} · {top_spend['person']} · {top_spend['category']}")
        with w3.container(border=True, key="card_w3"):
            st.caption(":material/repeat: Most often")
            st.markdown(f"**{freq.name}** · {int(freq['times'])} times")
            st.caption(f"{inr(freq['amount'], 0)} in total")

        left, right = st.columns(2, gap="large")
        with left:
            st.markdown("#### Where it went")
            st.caption(f"Click a category to see what's in it · ▲ more or ▼ less than {before}"
                       if not prev_spent.empty else "Click a category to see what's in it")
            prev_cat = prev_spent.groupby("category")["spend"].sum() if not prev_spent.empty else None
            event = st.altair_chart(bar_chart(summarize(spent, "category"), "category", "Category", clickable=True,
                                              prev=prev_cat, prev_name=before),
                                    width="stretch", on_select="rerun", key=nav_key("categories"))
            if picked := clicked(event, "category"):
                go("category", picked)
        with right:
            weekly = (end - start).days <= 62  # weeks say more than two half-months
            st.markdown("#### Week by week" if weekly else "#### Month by month")
            st.caption("Each person's share of the spending")
            st.altair_chart(monthly_chart(spent, people, all_people, weekly=weekly), width="stretch")

        left, right = st.columns(2, gap="large")
        with left:
            st.markdown("#### By card")
            st.caption("Click a card to see its statements")
            event = st.altair_chart(bar_chart(summarize(spent, "card_label"), "card_label", "Card", clickable=True),
                                    width="stretch", on_select="rerun", key=nav_key("cards"))
            if picked := clicked(event, "card_label"):
                go("card", picked)
        with right:
            if len(people) > 1:
                st.markdown("#### By person")
                st.caption("Who spent how much")
                st.altair_chart(bar_chart(summarize(spent, "person"), "person", "Person",
                                          color=alt.Color("person:N", scale=people_scale(all_people), legend=None)),
                                width="stretch")

