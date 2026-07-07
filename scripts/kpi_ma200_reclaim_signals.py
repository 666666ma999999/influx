#!/usr/bin/env python3
"""200日線奪回クロス（グランビル/BKLZ延長） シグナル生成器（カタログ§2-A・第15周T3）。

docs/stock-algo-kpi-catalog.md の §2-A「200日線奪回クロス」（強化版）を実装する（tasks指示・
第15周「頻度直交セット」T3）。`data/jquants/bars/YYYYMMDD.json.gz`（四本値・出来高）のみで
完結する（fins不使用）。SMA200の計算規約は scripts/kpi_volshock_signals.py の dev200
（「D自身を含む直近200回の有効AdjC観測値の平均」）と完全に同一のものを踏襲する。

定義（グリッドサーチはしない・1構成のみ・tasks指示どおり）:
    各営業日D・各銘柄について判定:
        (1) 前日終値 < SMA200(前日) かつ 当日終値 > SMA200(当日)（200日線を下から上へクロス）
        (2) 直近60営業日のうち50日以上で終値<SMA200（長期滞在後の初回奪回。D-60..D-1を判定対象
            とし、D自身は含めない。「終値<SMA200」の判定は各日そのSMA200の値を使う逐次判定）
        (3) Va(D) >= 直近20営業日の有効Va観測値の平均 × 1.5倍（D自身は含まない）
    シグナル確定日 = D（Dそのものの四本値で判定・look-aheadなし）
    200日分の有効履歴が無い銘柄・期間、および below-count用の60日分の履歴が無い期間はスキップする
    （insufficient_history）

below200カウントの実装詳細（「直近N回の有効観測値」の実務版・kpi_high52/kpi_volshockと同じ流儀）:
    各営業日についてその日自身の「終値<SMA200(当日)」の真偽値を、その日の判定が終わった**後**に
    deque(maxlen=60)へ追加する。したがって日Dの判定時点でdequeが保持しているのは D-60..D-1
    （直近60回の有効判定日）の真偽値であり、D自身の値はまだ含まれない（look-aheadなし）。

このKPIはユニバース新規流入と重なりやすいため（カタログ§6手順5）、判定対象（ユニバース内）
シグナルのmembership(new/existing)内訳を scripts/kpi_volshock_signals.compute_membership_breakdown()
で算出する（Canonical Module原則）。

フォワードリターン計算・ユニバース判定・ブートストラップCI等は scripts/kpi_event_study.py の
run_event_study() に委譲する（--defer-entryは既定True）。

Usage:
    python3 scripts/kpi_ma200_reclaim_signals.py --start 2016-11 --end 2022-11
    python3 scripts/kpi_ma200_reclaim_signals.py --start 2016-11 --end 2022-11 --skip-harness
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

KPI_NAME = "ma200_reclaim"

MA200_WINDOW = 200  # SMA200のウィンドウ（D自身を含む直近200回の有効AdjC観測値。kpi_volshock_signalsと同一規約）
LONG_STAY_WINDOW = 60  # below200カウントの対象窓（D-60..D-1）
LONG_STAY_MIN_DAYS_DEFAULT = 50  # 直近60営業日のうち50日以上でbelow200
VOL_WINDOW = 20  # 直近何回の有効Va観測値を平均するか（D自身は含まない）
VOL_MULTIPLIER_DEFAULT = 1.5
# MA200_WINDOW(200) + LONG_STAY_WINDOW(60)の合計260日分の履歴が貯まって初めてbelow200_histが
# 満杯になる。既存のWARMUP_BDAYS(260・kpi_volshock_signals.py)にさらに安全マージンを載せる。
WARMUP_BDAYS_MARGIN = 330


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


def generate_ma200_reclaim_signals(
    start_bd: str,
    end_bd: str,
    long_stay_min_days: int = LONG_STAY_MIN_DAYS_DEFAULT,
    vol_multiplier: float = VOL_MULTIPLIER_DEFAULT,
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd](YYYYMMDD営業日)内の全銘柄・全営業日から200日線奪回クロスシグナルを生成する。

    1営業日1パスの逐次スキャンで実装する（各銘柄のAdjC/Va履歴をdequeで保持し、
    200/60/20営業日分のbarsファイルを毎日読み直すことを避ける）。

    Returns:
        (signals_df, diag)。signals_df の列: signal_date, code, adjc, sma200, prev_adjc,
        prev_sma200, below200_count60, va, va_avg20。diag はフィルタ段階別の件数内訳。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    idx_start = bday_index[start_bd]
    idx_end = bday_index[end_bd]
    idx_earliest_bars = bday_index[_earliest_bars_date()]
    warmup_idx = max(idx_earliest_bars, idx_start - WARMUP_BDAYS_MARGIN)
    scan_days = all_bdays[warmup_idx : idx_end + 1]

    adjc200_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=MA200_WINDOW))
    below200_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=LONG_STAY_WINDOW))
    vol_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=VOL_WINDOW))
    prev_close: dict[str, float] = {}
    prev_sma200: dict[str, float] = {}

    diag = {
        "business_days_scanned": 0,
        "code_day_observations": 0,
        "insufficient_history": 0,  # SMA200/前日SMA200/below200_hist(60)/va_hist(20)のいずれかが未充足
        "cross_up": 0,  # (1)成立(below-count/出来高判定より前)
        "insufficient_long_stay": 0,  # (1)成立だが(2)below_count<50で除外
        "volume_below_threshold": 0,  # (1)(2)成立だが(3)出来高未達で除外
        "signals_ma200_reclaim": 0,
    }

    rows: list[dict] = []

    for d in scan_days:
        bars_d = measure_base_rate.load_bars_day(d)
        in_event_window = start_bd <= d <= end_bd
        if in_event_window:
            diag["business_days_scanned"] += 1

        for code, rec in bars_d.items():
            adjc_d = rec.get("AdjC")
            va_d = rec.get("Va")

            # SMA200(D)は「D自身を含む」規約のため、判定より先に更新する（kpi_volshock_signalsのdev200と同じ順序）
            hist200 = adjc200_hist[code]
            if adjc_d is not None:
                hist200.append(adjc_d)
            sma200_d = (sum(hist200) / len(hist200)) if len(hist200) == MA200_WINDOW else None

            if in_event_window:
                diag["code_day_observations"] += 1
                p_close = prev_close.get(code)
                p_sma200 = prev_sma200.get(code)
                below_hist = below200_hist.get(code)
                v_hist = vol_hist.get(code)
                history_ready = (
                    sma200_d is not None and p_close is not None and p_sma200 is not None
                    and below_hist is not None and len(below_hist) == LONG_STAY_WINDOW
                    and v_hist is not None and len(v_hist) == VOL_WINDOW
                )
                if history_ready:
                    crossed = (p_close < p_sma200) and (adjc_d is not None and adjc_d > sma200_d)
                    if crossed:
                        diag["cross_up"] += 1
                        below_count = sum(below_hist)
                        if below_count >= long_stay_min_days:
                            va_avg = sum(v_hist) / len(v_hist)
                            if va_d and va_avg > 0 and va_d >= va_avg * vol_multiplier:
                                diag["signals_ma200_reclaim"] += 1
                                rows.append(
                                    {
                                        "signal_date": d,
                                        "code": code,
                                        "adjc": adjc_d,
                                        "sma200": sma200_d,
                                        "prev_adjc": p_close,
                                        "prev_sma200": p_sma200,
                                        "below200_count60": below_count,
                                        "va": va_d,
                                        "va_avg20": va_avg,
                                    }
                                )
                            else:
                                diag["volume_below_threshold"] += 1
                        else:
                            diag["insufficient_long_stay"] += 1
                else:
                    diag["insufficient_history"] += 1

            # 履歴更新は当日の判定より後（below200_hist/vol_histとも「次回以降の直近N回」の
            # 一員としてのみ蓄積。翌日以降の判定にのみ影響しlook-aheadではない）
            if adjc_d is not None and sma200_d is not None:
                below200_hist[code].append(adjc_d < sma200_d)
            if va_d:
                vol_hist[code].append(va_d)
            if adjc_d is not None:
                prev_close[code] = adjc_d
            if sma200_d is not None:
                prev_sma200[code] = sma200_d

    return pd.DataFrame(rows), diag


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="200日線奪回クロス シグナル生成器(カタログ§2-A・第15周T3) + KPI検証ハーネス実行"
    )
    parser.add_argument(
        "--start", default=kpi_pead_signals.IN_SAMPLE_START,
        help=f"検索開始月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_START})",
    )
    parser.add_argument(
        "--end", default=kpi_pead_signals.IN_SAMPLE_END,
        help=f"検索終了月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_END})",
    )
    parser.add_argument("--long-stay-min-days", type=int, default=LONG_STAY_MIN_DAYS_DEFAULT)
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

    signals_df, diag = generate_ma200_reclaim_signals(
        start_bd, end_bd, args.long_stay_min_days, args.vol_multiplier
    )

    output_root = Path(args.output_dir)
    kpi_dir = output_root / args.kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_path = kpi_dir / "signals_raw.csv"
    signals_df.to_csv(signals_path, index=False)

    print(
        f"シグナル生成完了: {len(signals_df)}件"
        f"(long_stay_min_days={args.long_stay_min_days}/60日, vol_multiplier={args.vol_multiplier}x)"
    )
    print(
        f"営業日走査={diag['business_days_scanned']}日 / 銘柄日観測={diag['code_day_observations']} "
        f"(履歴不足(200/60/20日未満)={diag['insufficient_history']}, "
        f"200日線クロス成立={diag['cross_up']}, "
        f"長期滞在未達(below60<{args.long_stay_min_days})で除外={diag['insufficient_long_stay']}, "
        f"出来高{args.vol_multiplier}倍未満で除外={diag['volume_below_threshold']}, "
        f"シグナル成立={diag['signals_ma200_reclaim']})"
    )
    print(f"出力: {signals_path}")

    if args.skip_harness:
        return 0
    if signals_df.empty:
        print("WARN: シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    defer_entry = not args.no_defer_entry
    params = {
        "ma200_window": MA200_WINDOW,
        "long_stay_window": LONG_STAY_WINDOW,
        "long_stay_min_days": args.long_stay_min_days,
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
