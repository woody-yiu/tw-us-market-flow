"""Rebuild US market data from the latest frozen LSEG S&P 500 release."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent
DATA_JS = ROOT / "dist" / "data.js"
INDEX_HTML = ROOT / "dist" / "index.html"
DEFAULT_DB_ROOT = Path(
    r"G:\共用雲端硬碟\Terawise 資料庫\LSEG資料庫副本"
    r"\data\markets\US\sp500_backtest"
)
PERIOD_DAYS = {"day": 1, "week": 5, "month": 21}


def load_site_data() -> dict:
    text = DATA_JS.read_text(encoding="utf-8").strip()
    prefix = "window.FLOW_DATA="
    if not text.startswith(prefix) or not text.endswith(";"):
        raise ValueError("dist/data.js format is not recognized")
    return json.loads(text[len(prefix) : -1])


def json_number(value: float, digits: int) -> float | None:
    return None if pd.isna(value) else round(float(value), digits)


def identifier_key(value: str) -> str:
    root = re.split(r"[.^]", str(value), maxsplit=1)[0]
    return re.sub(r"[^A-Za-z0-9]", "", root).upper()


def latest_release(db_root: Path) -> Path:
    releases = sorted((db_root / "frozen_releases").glob("release=*"))
    if not releases:
        raise FileNotFoundError(f"No frozen LSEG release found under {db_root}")
    return releases[-1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-root", type=Path, default=DEFAULT_DB_ROOT)
    parser.add_argument("--release-root", type=Path)
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--universe", type=Path)
    parser.add_argument("--classification", type=Path)
    return parser.parse_args()


def input_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    release = args.release_root or latest_release(args.db_root)
    prices = args.prices or release / "prices" / "year=2026" / "part.parquet"
    universe = args.universe or release / "universe" / "sp500_universe_daily.parquet"
    classification = args.classification or (
        release / "pit" / "reference" / "sp500_industry_classification_snapshots.parquet"
    )
    for path in (prices, universe, classification):
        if not path.is_file():
            raise FileNotFoundError(path)
    return release, prices, universe, classification


def main() -> None:
    args = parse_args()
    release, prices_path, universe_path, classification_path = input_paths(args)

    prices = pd.read_parquet(prices_path).copy()
    universe = pd.read_parquet(universe_path).copy()
    classifications = pd.read_parquet(classification_path).copy()
    prices["date"] = pd.to_datetime(prices["date"])
    universe["as_of_date"] = pd.to_datetime(universe["as_of_date"])
    classifications["snapshot_date"] = pd.to_datetime(classifications["snapshot_date"])

    latest_price = prices["date"].max()
    latest_universe = universe["as_of_date"].max()
    flow_date = min(latest_price, latest_universe)
    members = universe.loc[
        universe["as_of_date"].eq(flow_date), ["ticker", "ric"]
    ].drop_duplicates("ric")
    if len(members) < 490:
        raise RuntimeError(f"Unexpected S&P 500 member count on {flow_date.date()}: {len(members)}")

    classification_date = classifications["snapshot_date"].max()
    current_classes = classifications.loc[
        classifications["snapshot_date"].eq(classification_date),
        ["ric", "ticker", "company_name", "gics_industry_group_name"],
    ].drop_duplicates("ticker")
    members = members.merge(
        current_classes.drop(columns="ric"), on="ticker", how="left", validate="one_to_one"
    )
    if members["gics_industry_group_name"].isna().any():
        missing = members.loc[members["gics_industry_group_name"].isna(), "ticker"].tolist()
        raise RuntimeError(f"Missing GICS Industry Group for: {missing}")

    members["identifier_key"] = members["ticker"].map(identifier_key)
    if members["identifier_key"].duplicated().any():
        raise RuntimeError("Normalized S&P 500 ticker identifiers are not unique")
    ticker_by_key = members.set_index("identifier_key")["ticker"]
    prices["identifier_key"] = prices["ric"].map(identifier_key)
    prices["ticker"] = prices["identifier_key"].map(ticker_by_key)
    prices = prices.loc[prices["ticker"].notna() & prices["date"].le(flow_date)].copy()
    # Identifier transitions can leave old and new RICs on the same date. The
    # primary listing has the larger volume, so retain one canonical row per ticker.
    prices = prices.sort_values(["date", "ticker", "volume"], ascending=[True, True, False])
    prices = prices.drop_duplicates(["date", "ticker"], keep="first")
    tickers = members["ticker"].tolist()
    close = prices.pivot(index="date", columns="ticker", values="close").sort_index().reindex(columns=tickers)
    volume = prices.pivot(index="date", columns="ticker", values="volume").sort_index().reindex(columns=tickers)
    dates = close.index
    if len(dates) < PERIOD_DAYS["month"] + 1:
        raise RuntimeError("Not enough 2026 price history for the requested periods")

    daily_return = close.pct_change(fill_method=None)
    daily_turnover = close * volume / 1_000_000_000
    daily_flow = daily_return * close * volume / 1_000_000_000
    member_info = members.set_index("ticker")

    periods: dict[str, dict] = {}
    for period in ("day", "week", "month", "year"):
        window = daily_flow if period == "year" else daily_flow.tail(PERIOD_DAYS[period])
        stock_flow = window.sum(axis=0, min_count=1)
        if period == "year":
            period_return = (close.iloc[-1] / close.iloc[0] - 1) * 100
        else:
            period_return = (close.iloc[-1] / close.shift(PERIOD_DAYS[period]).iloc[-1] - 1) * 100

        sector_rows = []
        for group_name, group in member_info.groupby("gics_industry_group_name", sort=False):
            ids = group.index.tolist()
            values = stock_flow.reindex(ids).dropna()
            observed_ids = values.index.tolist()
            valid_returns = period_return.reindex(observed_ids).dropna()
            coverage = window.reindex(columns=ids).notna().to_numpy().mean() * 100 if ids else 0
            turnover = daily_turnover.loc[window.index, ids].sum(axis=0, min_count=1).sum(min_count=1)
            sector_rows.append(
                {
                    "name": group_name,
                    "flow": round(float(values.sum()) if len(values) else 0.0, 3),
                    "turnover": json_number(turnover, 1),
                    "return": round(float(valid_returns.mean()), 2) if len(valid_returns) else None,
                    "breadth": round(float((values > 0).mean() * 100), 1) if len(values) else 0,
                    "coverage": round(float(coverage), 1),
                    "names": len(ids),
                    "observed_names": len(observed_ids),
                }
            )
        sector_rows.sort(key=lambda row: row["flow"], reverse=True)

        stock_rows = []
        for ticker, row in member_info.iterrows():
            stock_rows.append(
                {
                    "ticker": ticker,
                    "name": row["company_name"],
                    "sector": row["gics_industry_group_name"],
                    "flow": json_number(stock_flow.get(ticker), 3),
                    "return": json_number(period_return.get(ticker), 2),
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
            ids = member_info.index[member_info["gics_industry_group_name"].eq(leader)]
            point[leader] = json_number(flows.reindex(ids).sum(min_count=1), 3)
        trend_points.append(point)

    history = {
        "dates": [trading_day.strftime("%Y-%m-%d") for trading_day in dates],
        "tickers": tickers,
        "flows": [
            [json_number(value, 6) for value in daily_flow[ticker].tolist()]
            for ticker in tickers
        ],
        "closes": [
            [json_number(value, 4) for value in close[ticker].tolist()]
            for ticker in tickers
        ],
    }

    site_data = load_site_data()
    site_data["us"] = {
        "meta": {
            "price_date": flow_date.strftime("%Y-%m-%d"),
            "classification_date": classification_date.strftime("%Y-%m-%d"),
            "constituents": len(members),
            "latest_observed_count": int(daily_flow.loc[flow_date].notna().sum()),
            "classification_level": "GICS Industry Group",
            "groups": int(members["gics_industry_group_name"].nunique()),
            "release": release.name,
            "definition": "sum(daily return × close × volume)",
        },
        "periods": periods,
        "trend": {"leaders": leaders, "points": trend_points},
        "history": history,
    }
    payload = "window.FLOW_DATA=" + json.dumps(site_data, ensure_ascii=False, separators=(",", ":")) + ";\n"
    DATA_JS.write_text(payload, encoding="utf-8")

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

    observed = sum(row["observed_names"] for row in periods["day"]["sectors"])
    print(f"Updated {DATA_JS}")
    print(f"Data version: {data_version}")
    print(
        f"Flow date: {flow_date.date()} | GICS groups: {len(periods['day']['sectors'])} "
        f"| Stocks: {observed}/{len(periods['day']['stocks'])}"
    )


if __name__ == "__main__":
    main()
