#!/usr/bin/env python3
"""レンジ収縮→上放れ（ボックス/BBスクイーズ統合） シグナル生成器（カタログ§2-A・第15周T2）。

docs/stock-algo-kpi-catalog.md の §2-A「レンジ収縮→上放れ」を実装する（tasks指示・第15周
「頻度直交セット」T2）。`data/jquants/bars/YYYYMMDD.json.gz`（四本値・出来高）のみで完結する
（fins不使用）。実装の流儀は scripts/kpi_high52_signals.py / scripts/kpi_volshock_signals.py
（bars全走査系シグナル生成器・dequeによる1営業日1パスの逐次スキャン）を踏襲する。

定義（グリッドサーチはしない・1構成のみ・tasks指示どおり）:
    各営業日D・各銘柄について判定:
        (1) 過去60営業日[D-60..D-1]のレンジ幅 = (max(AdjH)-min(AdjL))/min(AdjL) < 0.15
            （D自身は含まない。「直近60営業日」は kpi_high52_signals.py と同じ流儀で
              「直近60回の有効AdjH/AdjL観測値」の実務版＝暦固定窓ではない）
        (2) AdjC(D) > max(AdjH[D-60..D-1]) × 1.02（レンジ上限×1.02の上放れ。(1)と同じ60日窓）
        (3) Va(D) >= 直近20営業日の有効Va観測値の平均 × 2倍（D自身は含まない）
    シグナル確定日 = D（Dそのものの四本値で判定・look-aheadなし）
    60日分の有効履歴が無い銘柄・期間はスキップする（insufficient_range_history）

このKPIはユニバース新規流入と重なりやすいため（カタログ§6手順5）、判定対象（ユニバース内）
シグナルのmembership(new/existing)内訳を scripts/kpi_volshock_signals.compute_membership_breakdown()
で算出する（Canonical Module原則・membership判定ロジックの再実装はしない）。

フォワードリターン計算・ユニバース判定・ブートストラップCI等は scripts/kpi_event_study.py の
run_event_study() に委譲する（--defer-entryは既定True）。

Usage:
    python3 scripts/kpi_range_breakout_signals.py --start 2016-11 --end 2022-11
    python3 scripts/kpi_range_breakout_signals.py --start 2016-11 --end 2022-11 --skip-harness
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict, deque
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: DATA_ROOT を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: run_event_study を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・MONTH_RE を再利用)
import kpi_volshock_signals  # noqa: E402  (Canonical Module: compute_membership_breakdown を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読み込みを再利用)

KPI_NAME = "range_breakout"

RANGE_WINDOW = 60  # レンジ幅・レンジ上限とも共通の窓（D-60..D-1・D自身は含まない）
RANGE_WIDTH_MAX_DEFAULT = 0.15  # (max(AdjH)-min(AdjL))/min(AdjL) < 0.15
BREAKOUT_MULT_DEFAULT = 1.02  # AdjC(D) > レンジ上限 × 1.02
VOL_WINDOW = 20  # 直近何回の有効Va観測値を平均するか（D自身は含まない）
VOL_MULTIPLIER_DEFAULT = 2.0
WARMUP_BDAYS_MARGIN = 70  # RANGE_WINDOW(60)よりわずかに大きいマージン。実データ最古日でさらに切り上げる


def _earliest_bars_date() -> str:
    """data/jquants/bars/ に実在する最古の営業日(YYYYMMDD)を返す(ハードコードせず実ファイルから確認)。

    kpi_high52_signals.py / kpi_volshock_signals.py / kpi_exit_study.py と同じ役割のヘルパー
    （このプロジェクトでは各kpiスクリプトが自己完結する既存慣習に合わせ複製する）。
    """
    bars_dir = jq_fetch.DATA_ROOT / "bars"
    dates = sorted(p.name[:8] for p in bars_dir.glob("*.json.gz"))
    if not dates:
        raise SystemExit(f"FATAL: bars キャッシュが1件も見つかりません: {bars_dir}")
    return dates[0]


# --- シグナル生成 ---------------------------------------------------------------


def generate_range_breakout_signals(
    start_bd: str,
    end_bd: str,
    range_width_max: float = RANGE_WIDTH_MAX_DEFAULT,
    breakout_mult: float = BREAKOUT_MULT_DEFAULT,
    vol_multiplier: float = VOL_MULTIPLIER_DEFAULT,
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd](YYYYMMDD営業日)内の全銘柄・全営業日からレンジ収縮→上放れシグナルを生成する。

    kpi_high52_signals.generate_high52_signals と同じ1営業日1パスの逐次スキャンで実装する
    （各銘柄のAdjH/AdjL/Va履歴をdeque(maxlen=...)で保持し、60/20営業日分のbarsファイルを
    毎日読み直すことを避ける）。

    Returns:
        (signals_df, diag)。signals_df の列: signal_date, code, adjc, range_high60, range_low60,
        range_width, va, va_avg20。diag はフィルタ段階別の件数内訳。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    idx_start = bday_index[start_bd]
    idx_end = bday_index[end_bd]
    idx_earliest_bars = bday_index[_earliest_bars_date()]
    warmup_idx = max(idx_earliest_bars, idx_start - WARMUP_BDAYS_MARGIN)
    scan_days = all_bdays[warmup_idx : idx_end + 1]

    high_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=RANGE_WINDOW))
    low_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=RANGE_WINDOW))
    vol_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=VOL_WINDOW))

    diag = {
        "business_days_scanned": 0,  # イベント期間内(ウォームアップ除く)の営業日数
        "code_day_observations": 0,
        "insufficient_range_history": 0,
        "insufficient_vol_history": 0,
        "range_narrow": 0,  # レンジ幅<15%成立(出来高判定より前)
        "breakout_over_range": 0,  # レンジ収縮 かつ 上放れ成立(出来高判定より前)
        "volume_below_threshold": 0,
        "signals_range_breakout": 0,
    }

    rows: list[dict] = []

    for d in scan_days:
        bars_d = measure_base_rate.load_bars_day(d)
        in_event_window = start_bd <= d <= end_bd
        if in_event_window:
            diag["business_days_scanned"] += 1

        for code, rec in bars_d.items():
            adjc_d = rec.get("AdjC")
            adjh_d = rec.get("AdjH")
            adjl_d = rec.get("AdjL")
            va_d = rec.get("Va")

            if in_event_window:
                diag["code_day_observations"] += 1
                h_hist = high_hist.get(code)
                l_hist = low_hist.get(code)
                v_hist = vol_hist.get(code)
                if (
                    h_hist is not None and len(h_hist) == RANGE_WINDOW
                    and l_hist is not None and len(l_hist) == RANGE_WINDOW
                ):
                    range_high60 = max(h_hist)
                    range_low60 = min(l_hist)
                    if range_low60 and range_low60 > 0:
                        range_width = (range_high60 - range_low60) / range_low60
                        if range_width < range_width_max:
                            diag["range_narrow"] += 1
                            if adjc_d is not None and adjc_d > range_high60 * breakout_mult:
                                diag["breakout_over_range"] += 1
                                if v_hist is not None and len(v_hist) == VOL_WINDOW:
                                    va_avg = sum(v_hist) / len(v_hist)
                                    if va_d and va_avg > 0 and va_d >= va_avg * vol_multiplier:
                                        diag["signals_range_breakout"] += 1
                                        rows.append(
                                            {
                                                "signal_date": d,
                                                "code": code,
                                                "adjc": adjc_d,
                                                "range_high60": range_high60,
                                                "range_low60": range_low60,
                                                "range_width": range_width,
                                                "va": va_d,
                                                "va_avg20": va_avg,
                                            }
                                        )
                                    else:
                                        diag["volume_below_threshold"] += 1
                                else:
                                    diag["insufficient_vol_history"] += 1
                    else:
                        diag["insufficient_range_history"] += 1
                else:
                    diag["insufficient_range_history"] += 1

            # 履歴更新は当日の判定より後(=当日のAdjH/AdjL/Vaは当日自身の判定には使うが、
            # 次回以降の「直近N回」の一員としてのみ蓄積する。翌日以降の判定にのみ影響しlook-aheadではない)
            if adjh_d is not None:
                high_hist[code].append(adjh_d)
            if adjl_d is not None:
                low_hist[code].append(adjl_d)
            if va_d:
                vol_hist[code].append(va_d)

    return pd.DataFrame(rows), diag


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="レンジ収縮→上放れ シグナル生成器(カタログ§2-A・第15周T2) + KPI検証ハーネス実行"
    )
    parser.add_argument(
        "--start", default=kpi_pead_signals.IN_SAMPLE_START,
        help=f"検索開始月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_START})",
    )
    parser.add_argument(
        "--end", default=kpi_pead_signals.IN_SAMPLE_END,
        help=f"検索終了月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_END})",
    )
    parser.add_argument("--range-width-max", type=float, default=RANGE_WIDTH_MAX_DEFAULT)
    parser.add_argument("--breakout-mult", type=float, default=BREAKOUT_MULT_DEFAULT)
    parser.add_argument("--vol-multiplier", type=float, default=VOL_MULTIPLIER_DEFAULT)
    parser.add_argument("--kpi-name", default=KPI_NAME)
    parser.add_argument("--output-dir", default="output/kpi", help="ハーネス出力先ルート(kpi-name配下に生成)")
    parser.add_argument("--base-rate-dir", default=str(kpi_event_study.DEFAULT_BASE_RATE_DIR))
    parser.add_argument("--universe-window", type=int, default=21, choices=[21, 126])
    parser.add_argument(
        "--skip-harness", action="store_true", help="シグナル生成のみ行いハーネス実行をスキップ(デバッグ用)"
    )
    parser.add_argument(
        "--no-defer-entry", action="store_true",
        help="--defer-entryが既定Trueのため、従来のT+1固定挙動に戻したい場合のみ指定",
    )
    args = parser.parse_args()

    if not kpi_pead_signals.MONTH_RE.match(args.start) or not kpi_pead_signals.MONTH_RE.match(args.end):
        raise SystemExit("FATAL: --start/--end は YYYY-MM 形式で指定してください")
    if args.end > kpi_pead_signals.IN_SAMPLE_END:
        print(
            f"WARN: --end={args.end} は§6で凍結したholdout期間(2023年以降)に抵触します。"
            f"in-sample評価は{kpi_pead_signals.IN_SAMPLE_END}までに限定してください"
            f"(holdoutは選抜済み候補の最終確認にのみ使用)。",
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

    signals_df, diag = generate_range_breakout_signals(
        start_bd, end_bd, args.range_width_max, args.breakout_mult, args.vol_multiplier
    )

    output_root = Path(args.output_dir)
    kpi_dir = output_root / args.kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_path = kpi_dir / "signals_raw.csv"
    signals_df.to_csv(signals_path, index=False)

    print(
        f"シグナル生成完了: {len(signals_df)}件"
        f"(range_width_max={args.range_width_max:.0%}, breakout_mult={args.breakout_mult}, "
        f"vol_multiplier={args.vol_multiplier}x)"
    )
    print(
        f"営業日走査={diag['business_days_scanned']}日 / 銘柄日観測={diag['code_day_observations']} "
        f"(レンジ履歴不足(60日未満)={diag['insufficient_range_history']}, "
        f"レンジ収縮(<{args.range_width_max:.0%})成立={diag['range_narrow']}, "
        f"上放れ成立(収縮×上放れ)={diag['breakout_over_range']}, "
        f"出来高履歴不足={diag['insufficient_vol_history']}, "
        f"出来高{args.vol_multiplier}倍未満で除外={diag['volume_below_threshold']}, "
        f"シグナル成立={diag['signals_range_breakout']})"
    )
    print(f"出力: {signals_path}")

    if args.skip_harness:
        return 0
    if signals_df.empty:
        print("WARN: シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    defer_entry = not args.no_defer_entry
    params = {
        "range_window": RANGE_WINDOW,
        "range_width_max": args.range_width_max,
        "breakout_mult": args.breakout_mult,
        "vol_window": VOL_WINDOW,
        "vol_multiplier": args.vol_multiplier,
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

    membership_counts = kpi_volshock_signals.compute_membership_breakdown(
        result["returns_path"], Path(args.base_rate_dir), args.universe_window
    )
    membership_line = " / ".join(f"{k}={v}" for k, v in sorted(membership_counts.items()))
    print(f"membership内訳(判定対象={result['n']}件): {membership_line}")

    with open(result["report_path"], "a", encoding="utf-8") as f:
        f.write("\n## ユニバースmembership内訳(第15周・§6手順5注記)\n")
        f.write(
            f"- 判定対象(in_universe)シグナル{result['n']}件のmembership内訳: {membership_line}"
            f"(`output/base_rate/universes_w{args.universe_window}.csv.gz`と突合。"
            f"membership判定ロジック自体はkpi_volshock_signals.compute_membership_breakdown()"
            f"をそのまま再利用)\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
