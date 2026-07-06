#!/usr/bin/env python3
"""出来高ショック実務版 シグナル生成器（カタログ§2-C #4・v0リスト#4・初期構成）。

docs/stock-algo-kpi-catalog.md の §2-C「出来高ショック」実務版を実装する。
`data/jquants/bars/YYYYMMDD.json.gz`（四本値・出来高）のみで完結する（fins不使用）。

定義（グリッドサーチはしない・1構成のみ）:
    - 各営業日D・各銘柄について判定:
        (1) Va(D) >= 直近20回の有効Va観測値の平均 × 5倍（volume shock）
            ※「直近20営業日」の厳密な暦固定窓ではなく「直近20回の有効出来高観測値」を採用した
              実務版の定義（売買停止等の欠測日を挟んでも自然にスキップされ、演算量も抑えられる。
              PEAD側 compute_max20/va_ratio の暦固定窓とは定義が異なる点に留意）
        (2) 陽線: AdjC(D) > AdjO(D)
        (3) 前日終値比 +2%〜+8%（AdjC(D)/AdjC(D-1)-1。まだ大きく噴いていない初動のみ採用）
    - シグナル確定日 = D（Dそのものの四本値で判定。Dの出来高・終値を使うためlook-aheadではないが、
      「Dの引け後に確定するシグナル」である点はPEAD等の反応日Gと同じ性質）
    - Va(D)自体・当日の陽線判定・前日比判定は全てD時点までのデータのみ使用（D+1以降は一切参照しない）

このKPIはユニバース新規流入（新規上場・急上昇銘柄の直近ランクイン等）と重なりやすいため
（カタログ§6手順5）、判定対象（ユニバース内）シグナルの membership(new/existing) 内訳を
scripts/measure_base_rate.py が既に出力済みの `output/base_rate/universes_w{window}.csv.gz`
と突合して算出し、report.mdに追記する（Canonical Module原則・membership判定ロジックの
再実装はしない。既存出力を読むだけ）。

フォワードリターン計算・ユニバース判定・ブートストラップCI等は scripts/kpi_event_study.py の
run_event_study() に委譲する。第5周より --defer-entry を既定Trueとする（team-lead方針）。

Usage:
    python3 scripts/kpi_volshock_signals.py --start 2016-11 --end 2022-11
    python3 scripts/kpi_volshock_signals.py --start 2016-11 --end 2022-11 --skip-harness
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import kpi_event_study  # noqa: E402  (Canonical Module: run_event_study を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・MONTH_RE を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読み込みを再利用)

KPI_NAME = "volshock_5x"

VOL_MULTIPLIER_DEFAULT = 5.0
VOL_HISTORY_WINDOW = 20  # 直近何回の有効Va観測値を平均するか
DAY_RET_MIN_DEFAULT = 0.02
DAY_RET_MAX_DEFAULT = 0.08
WARMUP_BDAYS = 40  # start_bdより前に確保するウォームアップ営業日数（VOL_HISTORY_WINDOWを確実に満たすための安全マージン）


# --- シグナル生成 ---------------------------------------------------------------


def generate_volshock_signals(
    start_bd: str,
    end_bd: str,
    vol_multiplier: float = VOL_MULTIPLIER_DEFAULT,
    day_ret_min: float = DAY_RET_MIN_DEFAULT,
    day_ret_max: float = DAY_RET_MAX_DEFAULT,
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]（YYYYMMDD営業日）内の全銘柄・全営業日から出来高ショックシグナルを生成する。

    1営業日1パスの逐次スキャンで実装する（各銘柄のVa履歴をdeque(maxlen=20)で保持し、
    20営業日分のbarsファイルを毎日読み直すことを避ける＝計算量O(全日数×全銘柄)に抑える）。

    Returns:
        (signals_df, diag)。signals_df の列: signal_date, code, va, va_avg20, day_ret, adjc, adjo。
        diag はフィルタ段階別の件数内訳。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    idx_start = bday_index[start_bd]
    idx_end = bday_index[end_bd]
    warmup_idx = max(0, idx_start - WARMUP_BDAYS)
    scan_days = all_bdays[warmup_idx : idx_end + 1]

    va_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=VOL_HISTORY_WINDOW))
    prev_bars: Optional[dict] = None

    diag = {
        "business_days_scanned": 0,  # イベント期間内（ウォームアップ除く）の営業日数
        "code_day_observations": 0,
        "insufficient_volume_history": 0,
        "volume_shock_5x": 0,
        "not_green_candle": 0,
        "no_prev_close": 0,
        "day_ret_out_of_range": 0,
        "signals_volshock5x": 0,
    }

    rows: list[dict] = []

    for d in scan_days:
        bars_d = measure_base_rate.load_bars_day(d)
        in_event_window = start_bd <= d <= end_bd
        if in_event_window:
            diag["business_days_scanned"] += 1

        for code, rec in bars_d.items():
            va_d = rec.get("Va")

            if in_event_window:
                diag["code_day_observations"] += 1
                hist = va_hist.get(code)
                if hist is not None and len(hist) == VOL_HISTORY_WINDOW:
                    va_avg = sum(hist) / len(hist)
                    if va_d and va_avg > 0 and va_d >= va_avg * vol_multiplier:
                        diag["volume_shock_5x"] += 1
                        adjc = rec.get("AdjC")
                        adjo = rec.get("AdjO")
                        if adjc is not None and adjo is not None and adjc > adjo:
                            prev_rec = (prev_bars or {}).get(code)
                            prev_close = prev_rec.get("AdjC") if prev_rec else None
                            if prev_close:
                                day_ret = adjc / prev_close - 1
                                if day_ret_min <= day_ret <= day_ret_max:
                                    diag["signals_volshock5x"] += 1
                                    rows.append(
                                        {
                                            "signal_date": d,
                                            "code": code,
                                            "va": va_d,
                                            "va_avg20": va_avg,
                                            "day_ret": day_ret,
                                            "adjc": adjc,
                                            "adjo": adjo,
                                        }
                                    )
                                else:
                                    diag["day_ret_out_of_range"] += 1
                            else:
                                diag["no_prev_close"] += 1
                        else:
                            diag["not_green_candle"] += 1
                else:
                    diag["insufficient_volume_history"] += 1

            # 履歴更新は当日の判定より後（＝当日のVaは当日自身の判定には使うが、次回以降の
            # 「直近20回」の一員としてのみ蓄積する。翌日以降の判定にのみ影響しlook-aheadではない）
            if va_d:
                va_hist[code].append(va_d)

        prev_bars = bars_d

    return pd.DataFrame(rows), diag


# --- membership(new/existing)内訳（既存のuniverses_w{window}.csv.gzを突合するのみ） -----


def compute_membership_breakdown(returns_path: Path, base_rate_dir: Path, universe_window: int) -> dict:
    """判定対象（in_universe）シグナルのmembership(new/existing)内訳を算出する。

    scripts/measure_base_rate.py が既に出力済みの universes_w{window}.csv.gz
    （month, code, rank, turnover_sum, membership 列）と突合するだけで、membership判定
    ロジック自体は一切再実装しない（Canonical Module原則）。
    """
    universes_path = base_rate_dir / f"universes_w{universe_window}.csv.gz"
    universes_df = pd.read_csv(universes_path, dtype={"month": str, "code": str})
    lookup = {(m, c): mem for m, c, mem in zip(universes_df["month"], universes_df["code"], universes_df["membership"])}

    returns_df = pd.read_csv(
        returns_path, dtype={"code": str, "universe_month_used": str}
    )
    in_universe_df = returns_df[returns_df["in_universe"] == True]  # noqa: E712 (CSV読込のbool比較)

    memberships = [
        lookup.get((row.universe_month_used, row.code), "unknown") for row in in_universe_df.itertuples()
    ]
    counts: dict[str, int] = defaultdict(int)
    for m in memberships:
        counts[m] += 1
    return dict(counts)


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="出来高ショック実務版 シグナル生成器（カタログ§2-C #4）+ KPI検証ハーネス実行"
    )
    parser.add_argument(
        "--start", default=kpi_pead_signals.IN_SAMPLE_START,
        help=f"検索開始月 YYYY-MM（既定 {kpi_pead_signals.IN_SAMPLE_START}）",
    )
    parser.add_argument(
        "--end", default=kpi_pead_signals.IN_SAMPLE_END,
        help=f"検索終了月 YYYY-MM（既定 {kpi_pead_signals.IN_SAMPLE_END}）",
    )
    parser.add_argument("--vol-multiplier", type=float, default=VOL_MULTIPLIER_DEFAULT)
    parser.add_argument("--day-ret-min", type=float, default=DAY_RET_MIN_DEFAULT)
    parser.add_argument("--day-ret-max", type=float, default=DAY_RET_MAX_DEFAULT)
    parser.add_argument("--kpi-name", default=KPI_NAME)
    parser.add_argument("--output-dir", default="output/kpi", help="ハーネス出力先ルート（kpi-name配下に生成）")
    parser.add_argument("--base-rate-dir", default=str(kpi_event_study.DEFAULT_BASE_RATE_DIR))
    parser.add_argument("--universe-window", type=int, default=21, choices=[21, 126])
    parser.add_argument(
        "--skip-harness", action="store_true", help="シグナル生成のみ行いハーネス実行をスキップ（デバッグ用）"
    )
    parser.add_argument(
        "--no-defer-entry", action="store_true",
        help="第5周より--defer-entryが既定Trueのため、従来のT+1固定挙動に戻したい場合のみ指定",
    )
    args = parser.parse_args()

    if not kpi_pead_signals.MONTH_RE.match(args.start) or not kpi_pead_signals.MONTH_RE.match(args.end):
        raise SystemExit("FATAL: --start/--end は YYYY-MM 形式で指定してください")
    if args.end > kpi_pead_signals.IN_SAMPLE_END:
        print(
            f"WARN: --end={args.end} は§6で凍結したholdout期間(2023年以降)に抵触します。"
            f"in-sample評価は{kpi_pead_signals.IN_SAMPLE_END}までに限定してください"
            f"（holdoutは選抜済み候補の最終確認にのみ使用）。",
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

    signals_df, diag = generate_volshock_signals(
        start_bd, end_bd, args.vol_multiplier, args.day_ret_min, args.day_ret_max
    )

    output_root = Path(args.output_dir)
    kpi_dir = output_root / args.kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_path = kpi_dir / "signals_raw.csv"
    signals_df.to_csv(signals_path, index=False)

    print(
        f"シグナル生成完了: {len(signals_df)}件"
        f"（vol_multiplier={args.vol_multiplier}x, day_ret={args.day_ret_min:.0%}〜{args.day_ret_max:.0%}）"
    )
    print(
        f"営業日走査={diag['business_days_scanned']}日 / 銘柄日観測={diag['code_day_observations']} "
        f"(出来高履歴不足={diag['insufficient_volume_history']}, 出来高5倍超={diag['volume_shock_5x']}, "
        f"陽線でない除外={diag['not_green_candle']}, 前日終値なし={diag['no_prev_close']}, "
        f"前日比レンジ外={diag['day_ret_out_of_range']}, シグナル成立={diag['signals_volshock5x']})"
    )
    print(f"出力: {signals_path}")

    if args.skip_harness:
        return 0
    if signals_df.empty:
        print("WARN: シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    defer_entry = not args.no_defer_entry
    params = {
        "vol_multiplier": args.vol_multiplier,
        "vol_history_window": VOL_HISTORY_WINDOW,
        "day_ret_min": args.day_ret_min,
        "day_ret_max": args.day_ret_max,
        "defer_entry": defer_entry,
    }
    result = kpi_event_study.run_event_study(
        signals_df=signals_df[["signal_date", "code"]],
        kpi_name=args.kpi_name,
        params=params,
        period=(args.start, args.end),
        output_dir=output_root,
        base_rate_dir=Path(args.base_rate_dir),
        universe_window=args.universe_window,
        defer_entry=defer_entry,
    )
    lift_str = f"{result['lift']:.2f}" if result["lift"] is not None else "-"
    ci_str = (
        f"[{result['ci_low']:.2f}, {result['ci_high']:.2f}]" if result["ci_low"] is not None else "[-, -]"
    )
    print(f"ハーネス実行完了: n={result['n']} lift={lift_str} ci95={ci_str} verdict={result['verdict']}")
    print(f"report: {result['report_path']}")
    print(f"returns: {result['returns_path']}")

    membership_counts = compute_membership_breakdown(
        result["returns_path"], Path(args.base_rate_dir), args.universe_window
    )
    membership_line = " / ".join(f"{k}={v}" for k, v in sorted(membership_counts.items()))
    print(f"membership内訳（判定対象={result['n']}件）: {membership_line}")

    with open(result["report_path"], "a", encoding="utf-8") as f:
        f.write("\n## ユニバースmembership内訳（第5周・§6手順5注記）\n")
        f.write(
            f"- 判定対象（in_universe）シグナル{result['n']}件のmembership内訳: {membership_line}"
            f"（`output/base_rate/universes_w{args.universe_window}.csv.gz`と突合。"
            f"membership判定ロジック自体はmeasure_base_rate.py側のCanonical Moduleをそのまま参照）\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
