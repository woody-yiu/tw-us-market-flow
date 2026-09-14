"""Rebuild Taiwan market data with one FinLab exchange industry per stock."""

from __future__ import annotations

import json
import hashlib
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from finlab import data


ROOT = Path(__file__).resolve().parent
DATA_JS = ROOT / "dist" / "data.js"
INDEX_HTML = ROOT / "dist" / "index.html"
START_OF_YEAR = pd.Timestamp("2026-01-01")
MARKETS = {"sii", "otc"}
EXCLUDED_CATEGORIES = {"存託憑證", "其他證券"}
PERIOD_DAYS = {"day": 1, "week": 5, "month": 21}

DATASETS = {
    "close": "price:收盤價",
    "foreign": "institutional_investors_trading_summary:外陸資買賣超股數(不含外資自營商)",
    "trust": "institutional_investors_trading_summary:投信買賣超股數",
    "dealer": "institutional_investors_trading_summary:自營商買賣超股數(自行買賣)",
}


def load_site_data() -> dict:
    text = DATA_JS.read_text(encoding="utf-8").strip()
    prefix = "window.FLOW_DATA="
    if not text.startswith(prefix) or not text.endswith(";"):
        raise ValueError("dist/data.js format is not recognized")
    return json.loads(text[len(prefix) : -1])


def normalized_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = pd.DataFrame(frame).copy()
    result.index = pd.to_datetime(result.index)
    result.columns = result.columns.astype(str)
    return result.sort_index()


def json_number(value: float, digits: int) -> float | None:
    return None if pd.isna(value) else round(float(value), digits)


def main() -> None:
    data.set_market("tw")
    categories = pd.DataFrame(data.get("security_categories")).copy()
    categories["stock_id"] = categories["stock_id"].astype(str)
    categories = categories.loc[
        categories["market"].isin(MARKETS)
        & categories["category"].notna()
        & ~categories["category"].isin(EXCLUDED_CATEGORIES),
        ["stock_id", "name", "category"],
    ].drop_duplicates("stock_id")
    categories["category"] = categories["category"].replace({"金融業": "金融保險業"})

    frames = {name: normalized_frame(data.get(key)) for name, key in DATASETS.items()}
    dates = sorted(set.intersection(*(set(frame.index) for frame in frames.values())))
    dates = pd.DatetimeIndex([day for day in dates if day >= START_OF_YEAR])
    if dates.empty:
        raise RuntimeError("No common 2026 trading dates were returned by FinLab")

    categories = categories.set_index("stock_id")
    stock_ids = categories.index.tolist()

    close = frames["close"].reindex(index=dates, columns=stock_ids)
    net_shares = (
        frames["foreign"].reindex(index=dates, columns=stock_ids)
        + frames["trust"].reindex(index=dates, columns=stock_ids)
        + frames["dealer"].reindex(index=dates, columns=stock_ids)
    )
    daily_flow = net_shares * close / 100_000_000
    latest = dates[-1]
    close_filled = close.ffill()

    prior_close = frames["close"].loc[frames["close"].index < START_OF_YEAR].reindex(columns=stock_ids).ffill()
    year_base = prior_close.iloc[-1] if not prior_close.empty else pd.Series(np.nan, index=stock_ids)
    returns = {
        key: close_filled.pct_change(days, fill_method=None).loc[latest] * 100
        for key, days in PERIOD_DAYS.items()
    }
    returns["year"] = (close_filled.loc[latest] / year_base - 1) * 100

    period_dates = {
        key: dates[-days:] for key, days in PERIOD_DAYS.items()
    }
    period_dates["year"] = dates

    periods: dict[str, dict] = {}
    for period, selected_dates in period_dates.items():
        window = daily_flow.loc[selected_dates]
        stock_flow = window.sum(axis=0, min_count=1)
        period_return = returns[period]

        sector_rows = []
        for category_name, members in categories.groupby("category", sort=False):
            ids = members.index.tolist()
            values = stock_flow.reindex(ids).dropna()
            observed_ids = values.index.tolist()
            valid_returns = period_return.reindex(observed_ids).dropna()
            coverage = window.reindex(columns=ids).notna().to_numpy().mean() * 100 if ids else 0
            sector_rows.append(
                {
                    "name": category_name,
                    "flow": round(float(values.sum()) / 10, 3),
                    "return": round(float(valid_returns.mean()), 2) if len(valid_returns) else None,
                    "breadth": round(float((values > 0).mean() * 100), 1) if len(values) else 0,
                    "coverage": round(float(coverage), 1),
                    "names": len(ids),
                    "observed_names": len(observed_ids),
                }
            )
        sector_rows.sort(key=lambda row: row["flow"], reverse=True)

        stock_rows = []
        for stock_id in stock_ids:
            row = categories.loc[stock_id]
            flow_value = stock_flow.get(stock_id)
            stock_rows.append(
                {
                    "ticker": stock_id,
                    "name": row["name"],
                    "sector": row["category"],
                    "flow": json_number(flow_value, 2),
                    "return": json_number(period_return.get(stock_id), 2),
                }
            )
        stock_rows.sort(key=lambda row: (row["flow"] is None, -(row["flow"] or 0)))
        positive = [row for row in stock_rows if row["flow"] is not None and row["flow"] > 0][:6]
        negative = sorted(
            (row for row in stock_rows if row["flow"] is not None and row["flow"] < 0),
            key=lambda row: row["flow"],
        )[:4]
        periods[period] = {"sectors": sector_rows, "movers": positive + negative, "stocks": stock_rows}

    leaders = [
        row["name"]
        for row in sorted(periods["month"]["sectors"], key=lambda row: abs(row["flow"]), reverse=True)[:4]
    ]
    trend_points = []
    for trading_day in dates[-90:]:
        flows = daily_flow.loc[trading_day]
        point = {"date": trading_day.strftime("%Y-%m-%d")}
        for leader in leaders:
            ids = categories.index[categories["category"].eq(leader)]
            point[leader] = round(float(flows.reindex(ids).sum(min_count=1)) / 10, 3)
        trend_points.append(point)

    history = {
        "dates": [trading_day.strftime("%Y-%m-%d") for trading_day in dates],
        "tickers": stock_ids,
        "flows": [
            [json_number(value, 5) for value in daily_flow[stock_id].tolist()]
            for stock_id in stock_ids
        ],
        "closes": [
            [json_number(value, 2) for value in close[stock_id].tolist()]
            for stock_id in stock_ids
        ],
        "base_closes": [json_number(year_base.get(stock_id), 2) for stock_id in stock_ids],
    }

    site_data = load_site_data()
    site_data["tw"] = {
        "meta": {
            "flow_date": latest.strftime("%Y-%m-%d"),
            "price_date": latest.strftime("%Y-%m-%d"),
            "classification_snapshot_date": date.today().isoformat(),
            "classification_source": "FinLab security_categories（上市櫃單一產業）",
            "universe_count": len(stock_ids),
            "latest_observed_count": int(daily_flow.loc[latest].notna().sum()),
            "investor_scope": "外陸資（不含外資自營商）＋投信＋自營商自行買賣",
            "definition": "sum(net buy shares × same-day close)",
        },
        "periods": periods,
        "trend": {"leaders": leaders, "points": trend_points},
        "history": history,
    }
    payload = "window.FLOW_DATA=" + json.dumps(site_data, ensure_ascii=False, separators=(",", ":")) + ";\n"
    DATA_JS.write_text(payload, encoding="utf-8")

    # Version the data URL by content so a daily rebuild cannot be hidden by a
    # browser's cached copy of data.js.
    data_version = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    html = INDEX_HTML.read_text(encoding="utf-8")
    html, replacements = re.subn(
        r'<script src="\./data\.js(?:\?v=[^"]*)?"></script>',
        f'<script src="./data.js?v={data_version}"></script>',
        html,
        count=1,
    )
    if replacements != 1:
        raise RuntimeError("Could not update the data.js version in dist/index.html")
    INDEX_HTML.write_text(html, encoding="utf-8")

    print(f"Updated {DATA_JS}")
    print(f"Data version: {data_version}")
    observed = sum(row["observed_names"] for row in periods["day"]["sectors"])
    print(f"Flow date: {latest.date()} | Industries: {len(periods['day']['sectors'])} | Stocks: {observed}/{len(periods['day']['stocks'])}")
    for ticker in ("2344", "2408", "2330", "2454"):
        print(ticker, categories.loc[ticker, "category"])


if __name__ == "__main__":
    main()
