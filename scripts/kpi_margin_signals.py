#!/usr/bin/env python3
"""信用倍率・売り長×トレンド シグナル生成器（踏み上げ候補・カタログ§2-B #5）。

docs/stock-algo-kpi-catalog.md の §2-B「信用倍率・売り長（踏み上げ候補）」を実装する。
`data/jquants/margin/YYYYMMDD.json.gz`（週次信用取引残高・別ビルダー取得分）から
信用倍率(=買残LongVol÷売残ShrtVol)を計算し、「売り長」(倍率<1) かつ
「(AdjC>SMA25) または (20日高値更新)」が**成立に遷移した日**をシグナル確定日とする。

公表ラグ（look-ahead回避・本KPIの生命線）:
    - margin-interest レスポンスには公表日フィールドが無い（実API疎通で確認済み）。
    - 基準日（週末営業日。祝日で金曜休場の週は木曜等その週最後の営業日に繰り上がる）
      から保守的に MARGIN_PUBLISH_LAG_BDAYS 営業日後を「使用可能日」とする
      （実際の公表は基準日+2営業日〔翌週火曜〕とされるため、+4営業日は安全側のバッファ）。
    - 使用可能日以降、次の週の使用可能日に更新されるまで同じ信用倍率状態を forward-fill する。

定義（グリッドサーチはしない・1構成のみ。カタログ§0-1「閾値は初期値」に反し固定運用する）:
    - 信用倍率 = LongVol(買残) ÷ ShrtVol(売残)。ShrtVol<=0 は倍率定義不能として「売り長ではない」扱い
    - 価格トレンド条件: (AdjC(d) > SMA25(d)（当日を含む trailing 25営業日平均・標準的なテクニカル指標の慣行))
      OR (AdjH(d) > 過去20営業日〔当日を含まない〕の最高値)
    - シグナル確定日 = (売り長 AND 価格トレンド条件) が False→True に遷移した最初の営業日
      （継続中は再発火しない=同一銘柄の連続日発火を初日のみに間引く仕様を兼ねる）

フォワードリターン計算・ユニバース判定・ブートストラップCI等は scripts/kpi_event_study.py の
run_event_study() に委譲する（Canonical Module原則・再実装しない）。価格系列の読み込みは
scripts/measure_base_rate.py の load_bars_day/load_calendar_days を再利用する。

Usage:
    python3 scripts/kpi_margin_signals.py --start 2016-11 --end 2022-11
    python3 scripts/kpi_margin_signals.py --start 2016-11 --end 2022-11 --skip-harness
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: DATA_ROOT / read_json_gz を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: run_event_study を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読み込みを再利用)

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

MARGIN_DIR = jq_fetch.DATA_ROOT / "margin"

# §0確認事項5・§6標本分割で凍結: in-sampleは2016-11〜2022-11。holdout(2023以降)はロック中。
IN_SAMPLE_START = "2016-11"
IN_SAMPLE_END = "2022-11"

KPI_NAME = "margin_urinaga_trend"

# レスポンスに公表日フィールドが無いための保守的ラグ（team lead指示: 「なければ保守的に基準日+4営業日から」）
MARGIN_PUBLISH_LAG_BDAYS_DEFAULT = 4
SMA25_WINDOW_DEFAULT = 25
HIGH20_WINDOW_DEFAULT = 20

# ウォームアップ用に in-sample 開始より前のデータも計算に含める（SMA25/高値20の窓を確保するため）。
# margin/bars とも実データは2016-07/08開始のため、この日以降で全期間計算する。
COMPUTE_FLOOR = "20160701"


# --- margin 読み込み・使用可能日マップ ---------------------------------------------


def load_margin_snapshots() -> list[tuple[str, dict[str, tuple[float, float]]]]:
    """data/jquants/margin/*.json.gz を基準日昇順で読み込み、
    [(basis_date, {code: (long_vol, short_vol)}), ...] を返す（未取得なら明確なエラーで停止）。
    """
    if not MARGIN_DIR.exists() or not any(MARGIN_DIR.glob("*.json.gz")):
        raise SystemExit(
            f"FATAL: marginキャッシュが見つかりません: {MARGIN_DIR}\n"
            f"先に `python3 scripts/jq_fetch.py --only margin` を実行してください。"
        )
    records = []
    for path in sorted(MARGIN_DIR.glob("*.json.gz")):
        basis_date = path.name.removesuffix(".json.gz")
        obj = jq_fetch.read_json_gz(path)
        by_code: dict[str, tuple[float, float]] = {}
        for rec in obj["data"]:
            code = rec.get("Code")
            long_v = rec.get("LongVol")
            short_v = rec.get("ShrtVol")
            if code is None or long_v is None or short_v is None:
                continue
            by_code[code] = (long_v, short_v)
        records.append((basis_date, by_code))
    return records


def build_margin_urinaga_df(
    bday_index: dict[str, int],
    all_bdays: list[str],
    publish_lag_bdays: int,
) -> pd.DataFrame:
    """週次基準日データを使用可能日にマップし、長形式DataFrame(usable_date, code, urinaga)を返す。

    urinaga は 0/1 の int（売り長=1）。ShrtVol<=0 は倍率定義不能として0（売り長ではない）扱い。
    """
    snapshots = load_margin_snapshots()
    rows = []
    skipped_no_bday = 0
    skipped_out_of_calendar = 0
    for basis_date, by_code in snapshots:
        if basis_date not in bday_index:
            skipped_no_bday += 1
            continue
        idx = bday_index[basis_date]
        usable_idx = idx + publish_lag_bdays
        if usable_idx >= len(all_bdays):
            skipped_out_of_calendar += 1
            continue
        usable_date = all_bdays[usable_idx]
        for code, (long_v, short_v) in by_code.items():
            urinaga = 1 if (short_v is not None and short_v > 0 and long_v / short_v < 1.0) else 0
            rows.append({"usable_date": usable_date, "code": code, "urinaga": urinaga})
    if skipped_no_bday or skipped_out_of_calendar:
        print(
            f"WARN: [margin] 基準日がカレンダー外: {skipped_no_bday}件 / "
            f"使用可能日がカレンダー終端超過: {skipped_out_of_calendar}件（該当週は評価対象外）",
            file=sys.stderr,
        )
    return pd.DataFrame(rows)


# --- 価格トレンド条件 -------------------------------------------------------------


def build_price_condition_df(
    all_bdays: list[str],
    sma25_window: int,
    high20_window: int,
) -> pd.DataFrame:
    """全営業日・全銘柄の (AdjC, AdjH) を読み込み、価格トレンド条件を計算する。

    SMA25は当日を含む trailing window（標準的なテクニカル指標の慣行=look-ahead無し）。
    高値更新は「過去」＝当日を含まない直前high20_window営業日が対象（shift(1)で除外）。
    """
    rows = []
    for d in all_bdays:
        bars = measure_base_rate.load_bars_day(d)
        for code, rec in bars.items():
            adjc = rec.get("AdjC")
            adjh = rec.get("AdjH")
            if not adjc or not adjh:
                continue
            rows.append((d, code, adjc, adjh))
    df = pd.DataFrame(rows, columns=["date", "code", "AdjC", "AdjH"])
    df.sort_values(["code", "date"], inplace=True)
    g = df.groupby("code", sort=False)
    df["sma25"] = g["AdjC"].transform(lambda s: s.rolling(sma25_window, min_periods=sma25_window).mean())
    df["past_high20"] = g["AdjH"].transform(
        lambda s: s.rolling(high20_window, min_periods=high20_window).max().shift(1)
    )
    df["price_cond"] = (df["AdjC"] > df["sma25"]) | (df["AdjH"] > df["past_high20"])
    return df[["date", "code", "price_cond"]]


# --- シグナル生成 ---------------------------------------------------------------


def generate_margin_signals(
    start_bd: str,
    end_bd: str,
    publish_lag_bdays: int = MARGIN_PUBLISH_LAG_BDAYS_DEFAULT,
    sma25_window: int = SMA25_WINDOW_DEFAULT,
    high20_window: int = HIGH20_WINDOW_DEFAULT,
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]（YYYYMMDD営業日）内で信用倍率・売り長×トレンドのシグナルを生成する。

    ウォームアップ（SMA25/高値20の窓確保・margin状態のforward-fill起点確保）のため、
    実際の計算は COMPUTE_FLOOR（データ開始日）から行い、出力のみ [start_bd, end_bd] に絞り込む。

    Returns:
        (signals_df, diag)。signals_df 列: signal_date, code。
        状態が (urinaga AND price_cond) に遷移した最初の日のみをシグナルとする
        （状態が継続する限り再発火しない=同一銘柄の連続日発火の間引きを兼ねる）。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays_full = measure_base_rate.all_business_days(calendar_days)
    compute_start = max(COMPUTE_FLOOR, all_bdays_full[0])
    all_bdays = [d for d in all_bdays_full if compute_start <= d <= end_bd]
    bday_index = {d: i for i, d in enumerate(all_bdays_full)}

    margin_df = build_margin_urinaga_df(bday_index, all_bdays_full, publish_lag_bdays)
    price_df = build_price_condition_df(all_bdays, sma25_window, high20_window)

    margin_df = margin_df.sort_values("usable_date").rename(columns={"usable_date": "date"})
    price_df = price_df.sort_values("date")

    # merge_asof は on= 列に数値/日時 dtype を要求するため、YYYYMMDD文字列を整数に変換して結合する
    # （文字列の辞書順ソートはYYYYMMDD形式であれば数値順と一致するため、変換自体に情報損失は無い）。
    price_df["date_int"] = price_df["date"].astype(int)
    margin_df["date_int"] = margin_df["date"].astype(int)
    merged = pd.merge_asof(
        price_df, margin_df.drop(columns=["date"]), on="date_int", by="code", direction="backward",
    )
    merged.drop(columns=["date_int"], inplace=True)
    merged["urinaga_bool"] = merged["urinaga"].fillna(0) >= 1
    merged["price_cond"] = merged["price_cond"].fillna(False)
    merged["combined"] = merged["urinaga_bool"] & merged["price_cond"]
    merged.sort_values(["code", "date"], inplace=True)
    merged["combined_prev"] = merged.groupby("code", sort=False)["combined"].shift(1, fill_value=False)
    merged["is_transition"] = merged["combined"] & (~merged["combined_prev"])

    diag = {
        "price_rows": int(len(price_df)),
        "margin_rows": int(len(margin_df)),
        "merged_rows": int(len(merged)),
        "urinaga_true_rows": int(merged["urinaga_bool"].sum()),
        "price_cond_true_rows": int(merged["price_cond"].sum()),
        "combined_true_rows": int(merged["combined"].sum()),
        "transitions_total": int(merged["is_transition"].sum()),
    }

    signals = merged[merged["is_transition"] & (merged["date"] >= start_bd) & (merged["date"] <= end_bd)]
    signals_df = signals[["date", "code"]].rename(columns={"date": "signal_date"}).reset_index(drop=True)
    diag["signals_in_range"] = int(len(signals_df))

    return signals_df, diag


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="信用倍率・売り長×トレンドシグナル生成器 + KPI検証ハーネス実行"
    )
    parser.add_argument("--start", default=IN_SAMPLE_START, help=f"検索開始月 YYYY-MM（既定 {IN_SAMPLE_START}）")
    parser.add_argument("--end", default=IN_SAMPLE_END, help=f"検索終了月 YYYY-MM（既定 {IN_SAMPLE_END}）")
    parser.add_argument("--publish-lag-bdays", type=int, default=MARGIN_PUBLISH_LAG_BDAYS_DEFAULT)
    parser.add_argument("--sma25-window", type=int, default=SMA25_WINDOW_DEFAULT)
    parser.add_argument("--high20-window", type=int, default=HIGH20_WINDOW_DEFAULT)
    parser.add_argument("--kpi-name", default=KPI_NAME)
    parser.add_argument("--output-dir", default="output/kpi", help="ハーネス出力先ルート（kpi-name配下に生成）")
    parser.add_argument(
        "--skip-harness", action="store_true", help="シグナル生成のみ行いハーネス実行をスキップ（デバッグ用）"
    )
    parser.add_argument(
        "--defer-entry", action="store_true",
        help="T+1にAdjOが無い(S高等)場合、最大3営業日までエントリーを繰り延べる（第4周・§6手順6実装）",
    )
    args = parser.parse_args()

    if not MONTH_RE.match(args.start) or not MONTH_RE.match(args.end):
        raise SystemExit("FATAL: --start/--end は YYYY-MM 形式で指定してください")
    if args.end > IN_SAMPLE_END:
        print(
            f"WARN: --end={args.end} は§6で凍結したholdout期間(2023年以降)に抵触します。"
            f"in-sample評価は{IN_SAMPLE_END}までに限定してください（holdoutは選抜済み候補の最終確認にのみ使用）。",
            file=sys.stderr,
        )

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays_all = measure_base_rate.all_business_days(calendar_days)
    start_bound = args.start.replace("-", "") + "01"
    end_bound = args.end.replace("-", "") + "31"
    start_bd = next((d for d in all_bdays_all if d >= start_bound), None)
    end_bd = next((d for d in reversed(all_bdays_all) if d <= end_bound), None)
    if start_bd is None or end_bd is None:
        raise SystemExit("FATAL: 指定期間に営業日が見つかりません")

    signals_df, diag = generate_margin_signals(
        start_bd, end_bd, args.publish_lag_bdays, args.sma25_window, args.high20_window
    )

    output_root = Path(args.output_dir)
    kpi_dir = output_root / args.kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_path = kpi_dir / "signals_raw.csv"
    signals_df.to_csv(signals_path, index=False)

    print(f"シグナル生成完了: {len(signals_df)}件")
    print(
        f"内訳: price_rows={diag['price_rows']} margin_rows={diag['margin_rows']} "
        f"merged_rows={diag['merged_rows']} urinaga_true={diag['urinaga_true_rows']} "
        f"price_cond_true={diag['price_cond_true_rows']} combined_true={diag['combined_true_rows']} "
        f"transitions_total={diag['transitions_total']}"
    )
    print(f"出力: {signals_path}")

    if args.skip_harness:
        return 0
    if signals_df.empty:
        print("WARN: シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    params = {
        "publish_lag_bdays": args.publish_lag_bdays,
        "sma25_window": args.sma25_window,
        "high20_window": args.high20_window,
        "defer_entry": args.defer_entry,
    }
    result = kpi_event_study.run_event_study(
        signals_df=signals_df,
        kpi_name=args.kpi_name,
        params=params,
        period=(args.start, args.end),
        output_dir=output_root,
        defer_entry=args.defer_entry,
    )
    lift_str = f"{result['lift']:.2f}" if result["lift"] is not None else "-"
    ci_str = (
        f"[{result['ci_low']:.2f}, {result['ci_high']:.2f}]" if result["ci_low"] is not None else "[-, -]"
    )
    print(f"ハーネス実行完了: n={result['n']} lift={lift_str} ci95={ci_str} verdict={result['verdict']}")
    print(f"report: {result['report_path']}")
    print(f"returns: {result['returns_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
