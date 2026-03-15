"""
BTC-USD monthly median vs next month minimum divergence analysis.

For each month, calculates:
- Monthly median closing price
- Next month's minimum closing price and its date
- Divergence rate: (next_month_min - current_month_median) / current_month_median * 100
"""

import json
import sys
from pathlib import Path
from datetime import datetime

import yfinance as yf
import pandas as pd


def fetch_btc_data(start: str, end: str) -> pd.DataFrame:
    """Fetch BTC-USD daily closing data from yfinance."""
    ticker = yf.Ticker("BTC-USD")
    df = ticker.history(start=start, end=end)
    if df.empty:
        raise RuntimeError("Failed to fetch BTC-USD data from yfinance")
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def compute_monthly_stats(df: pd.DataFrame) -> list[dict]:
    """Compute median per month and next month's min."""
    df["year_month"] = df.index.to_period("M")
    months = sorted(df["year_month"].unique())

    results = []
    for i, period in enumerate(months):
        if i == len(months) - 1:
            break  # last month has no "next month"

        current_mask = df["year_month"] == period
        next_period = months[i + 1]
        next_mask = df["year_month"] == next_period

        current_close = df.loc[current_mask, "Close"]
        next_close = df.loc[next_mask, "Close"]

        if current_close.empty or next_close.empty:
            continue

        median_price = float(current_close.median())
        next_min = float(next_close.min())
        next_min_date = next_close.idxmin().strftime("%Y-%m-%d")
        divergence = (next_min - median_price) / median_price * 100

        results.append({
            "year": period.year,
            "month": period.month,
            "label": f"{period.year}/{period.month:02d}",
            "median_price": round(median_price),
            "next_month_min": round(next_min),
            "next_month_min_date": next_min_date,
            "divergence_pct": round(divergence, 1),
        })

    return results


def build_matrix(data: list[dict]) -> dict:
    """Build a years x months matrix for heatmap rendering."""
    years = sorted(set(d["year"] for d in data))
    months = list(range(1, 13))

    lookup = {(d["year"], d["month"]): d["divergence_pct"] for d in data}

    values = []
    for year in years:
        row = [lookup.get((year, m)) for m in months]
        values.append(row)

    return {"years": years, "months": months, "values": values}


def build_summary(data: list[dict]) -> dict:
    """Build summary statistics."""
    divergences = [d["divergence_pct"] for d in data]
    negative = [d for d in divergences if d < 0]
    positive = [d for d in divergences if d >= 0]

    worst = min(data, key=lambda d: d["divergence_pct"])
    best = max(data, key=lambda d: d["divergence_pct"])

    return {
        "total_months": len(data),
        "avg_divergence": round(sum(divergences) / len(divergences), 1),
        "worst_divergence": {"label": worst["label"], "value": worst["divergence_pct"]},
        "best_divergence": {"label": best["label"], "value": best["divergence_pct"]},
        "negative_count": len(negative),
        "positive_count": len(positive),
    }


def main():
    start_date = "2022-04-01"
    end_date = "2026-03-14"

    print(f"Fetching BTC-USD data: {start_date} to {end_date} ...")
    df = fetch_btc_data(start_date, end_date)
    print(f"Fetched {len(df)} daily records.")

    data = compute_monthly_stats(df)
    summary = build_summary(data)
    matrix = build_matrix(data)

    output = {
        "data": data,
        "summary": summary,
        "matrix": matrix,
    }

    output_dir = Path(__file__).resolve().parent.parent / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / "btc_divergence_data.json"

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\nOutput saved to: {output_path}")
    print(f"\n{'='*60}")
    print(f"BTC Monthly Median -> Next Month Min Divergence Analysis")
    print(f"{'='*60}")
    print(f"Period: {start_date} to {end_date}")
    print(f"Total months analyzed: {summary['total_months']}")
    print(f"Average divergence: {summary['avg_divergence']}%")
    print(f"Worst divergence: {summary['worst_divergence']['label']} = {summary['worst_divergence']['value']}%")
    print(f"Best divergence: {summary['best_divergence']['label']} = {summary['best_divergence']['value']}%")
    print(f"Negative months: {summary['negative_count']}")
    print(f"Positive months: {summary['positive_count']}")
    print(f"{'='*60}")

    print(f"\nMonthly detail:")
    print(f"{'Label':<10} {'Median':>10} {'Next Min':>10} {'Min Date':<12} {'Div%':>8}")
    print(f"{'-'*52}")
    for d in data:
        print(
            f"{d['label']:<10} "
            f"${d['median_price']:>9,} "
            f"${d['next_month_min']:>9,} "
            f"{d['next_month_min_date']:<12} "
            f"{d['divergence_pct']:>7.1f}%"
        )


if __name__ == "__main__":
    main()
