#!/usr/bin/env python3
"""52週新高値ブレイク×出来高 シグナル生成器(カタログ§2-A・v0リスト#2・第9周新規実装)。

docs/stock-algo-kpi-catalog.md の §2-A「52週新高値ブレイク×出来高」を実装する。
`data/jquants/bars/YYYYMMDD.json.gz`(四本値・出来高)のみで完結する(fins不使用)。
実装の流儀は scripts/kpi_volshock_signals.py(bars全走査系シグナル生成器)を踏襲する。

定義(グリッドサーチはしない・1構成のみ):
    - 各営業日D・各銘柄について判定:
        (1) AdjH(D) > 過去252営業日の有効AdjH観測値の max(D自身は含まない)
        (2) Va(D) >= 直近20営業日の有効Va観測値の平均 × 2倍
    - シグナル確定日 = D(Dそのものの四本値で判定・look-aheadなし)
    - 252日分の有効履歴が無い銘柄・期間はスキップする(insufficient_history)
    - 決算増収増益のANDオプションは付けない(初期1構成。カタログ§2-A注記どおり)

kpi_volshock_signals.py と同じく、「直近N営業日」は厳密な暦固定窓ではなく「直近N回の有効観測値」
を採用する実務版の定義(欠測日を挟んでも自然にスキップされる)。252営業日ウィンドウの都合上、
実データ開始日(data/jquants/bars/ の最古ファイル)より前は走査できない。ウォームアップ起点は
「開始日からWARMUP_BDAYS_MARGIN遡った日」と「実データ最古日」のうち遅い方とする。したがって
in-sample開始直後(2016-11〜)の一定期間は252日分の履歴が貯まりきらずシグナルが少ない/0になる
(insufficient_history。実データ制約でありlook-aheadではない。§6横断的PIT実装ルール準拠)。

このKPIはユニバース新規流入(新規上場・急上昇銘柄の直近ランクイン等)と重なりやすいため
(カタログ§6手順5)、判定対象(ユニバース内)シグナルのmembership(new/existing)内訳を
scripts/kpi_volshock_signals.compute_membership_breakdown()で算出する
(Canonical Module原則・membership判定ロジックの再実装はしない)。

フォワードリターン計算・ユニバース判定・ブートストラップCI等は scripts/kpi_event_study.py の
run_event_study() に委譲する(--defer-entryは既定True)。

Usage:
    python3 scripts/kpi_high52_signals.py --start 2016-11 --end 2022-11
    python3 scripts/kpi_high52_signals.py --start 2016-11 --end 2022-11 --skip-harness
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

KPI_NAME = "high52_breakout"

HIGH_WINDOW = 252  # 過去何回の有効AdjH観測値をmaxに使うか(D自身は含まない)
VOL_WINDOW = 20  # 直近何回の有効Va観測値を平均するか(D自身は含まない)
VOL_MULTIPLIER_DEFAULT = 2.0
WARMUP_BDAYS_MARGIN = 260  # HIGH_WINDOW(252)よりわずかに大きいマージン。実データ最古日でさらに切り上げる


def _earliest_bars_date() -> str:
    """data/jquants/bars/ に実在する最古の営業日(YYYYMMDD)を返す(ハードコードせず実ファイルから確認)。"""
    bars_dir = jq_fetch.DATA_ROOT / "bars"
    dates = sorted(p.name[:8] for p in bars_dir.glob("*.json.gz"))
    if not dates:
        raise SystemExit(f"FATAL: bars キャッシュが1件も見つかりません: {bars_dir}")
    return dates[0]


# --- シグナル生成 ---------------------------------------------------------------


def generate_high52_signals(
    start_bd: str,
    end_bd: str,
    vol_multiplier: float = VOL_MULTIPLIER_DEFAULT,
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd](YYYYMMDD営業日)内の全銘柄・全営業日から52週高値ブレイク×出来高シグナルを生成する。

    kpi_volshock_signals.generate_volshock_signals と同じ1営業日1パスの逐次スキャンで実装する
    (各銘柄のAdjH/Va履歴をdeque(maxlen=...)で保持し、252/20営業日分のbarsファイルを
    毎日読み直すことを避ける=計算量O(全日数×全銘柄)に抑える)。

    Returns:
        (signals_df, diag)。signals_df の列: signal_date, code, adjh, high_max252, va, va_avg20。
        diag はフィルタ段階別の件数内訳。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    idx_start = bday_index[start_bd]
    idx_end = bday_index[end_bd]
    idx_earliest_bars = bday_index[_earliest_bars_date()]
    warmup_idx = max(idx_earliest_bars, idx_start - WARMUP_BDAYS_MARGIN)
    scan_days = all_bdays[warmup_idx : idx_end + 1]

    high_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=HIGH_WINDOW))
    vol_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=VOL_WINDOW))

    diag = {
        "business_days_scanned": 0,  # イベント期間内(ウォームアップ除く)の営業日数
        "code_day_observations": 0,
        "insufficient_history": 0,
        "new_high_252": 0,
        "volume_below_threshold": 0,
        "signals_high52": 0,
    }

    rows: list[dict] = []

    for d in scan_days:
        bars_d = measure_base_rate.load_bars_day(d)
        in_event_window = start_bd <= d <= end_bd
        if in_event_window:
            diag["business_days_scanned"] += 1

        for code, rec in bars_d.items():
            adjh_d = rec.get("AdjH")
            va_d = rec.get("Va")

            if in_event_window:
                diag["code_day_observations"] += 1
                h_hist = high_hist.get(code)
                v_hist = vol_hist.get(code)
                if (
                    h_hist is not None and len(h_hist) == HIGH_WINDOW
                    and v_hist is not None and len(v_hist) == VOL_WINDOW
                ):
                    high_max252 = max(h_hist)
                    if adjh_d is not None and adjh_d > high_max252:
                        diag["new_high_252"] += 1
                        va_avg = sum(v_hist) / len(v_hist)
                        if va_d and va_avg > 0 and va_d >= va_avg * vol_multiplier:
                            diag["signals_high52"] += 1
                            rows.append(
                                {
                                    "signal_date": d,
                                    "code": code,
                                    "adjh": adjh_d,
                                    "high_max252": high_max252,
                                    "va": va_d,
                                    "va_avg20": va_avg,
                                }
                            )
                        else:
                            diag["volume_below_threshold"] += 1
                else:
                    diag["insufficient_history"] += 1

            # 履歴更新は当日の判定より後(=当日のAdjH/Vaは当日自身の判定には使うが、次回以降の
            # 「直近N回」の一員としてのみ蓄積する。翌日以降の判定にのみ影響しlook-aheadではない)
            if adjh_d is not None:
                high_hist[code].append(adjh_d)
            if va_d:
                vol_hist[code].append(va_d)

    return pd.DataFrame(rows), diag


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="52週新高値ブレイク×出来高 シグナル生成器(カタログ§2-A・v0リスト#2)+ KPI検証ハーネス実行"
    )
    parser.add_argument(
        "--start", default=kpi_pead_signals.IN_SAMPLE_START,
        help=f"検索開始月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_START})",
    )
    parser.add_argument(
        "--end", default=kpi_pead_signals.IN_SAMPLE_END,
        help=f"検索終了月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_END})",
    )
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

    signals_df, diag = generate_high52_signals(start_bd, end_bd, args.vol_multiplier)

    output_root = Path(args.output_dir)
    kpi_dir = output_root / args.kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_path = kpi_dir / "signals_raw.csv"
    signals_df.to_csv(signals_path, index=False)

    print(
        f"シグナル生成完了: {len(signals_df)}件(vol_multiplier={args.vol_multiplier}x, high_window={HIGH_WINDOW})"
    )
    print(
        f"営業日走査={diag['business_days_scanned']}日 / 銘柄日観測={diag['code_day_observations']} "
        f"(履歴不足(252/20日未満)={diag['insufficient_history']}, 新高値252ブレイク={diag['new_high_252']}, "
        f"出来高2倍未満で除外={diag['volume_below_threshold']}, シグナル成立={diag['signals_high52']})"
    )
    print(f"出力: {signals_path}")

    if args.skip_harness:
        return 0
    if signals_df.empty:
        print("WARN: シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    defer_entry = not args.no_defer_entry
    params = {
        "high_window": HIGH_WINDOW,
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
        f.write("\n## ユニバースmembership内訳(第9周・§6手順5注記)\n")
        f.write(
            f"- 判定対象(in_universe)シグナル{result['n']}件のmembership内訳: {membership_line}"
            f"(`output/base_rate/universes_w{args.universe_window}.csv.gz`と突合。"
            f"membership判定ロジック自体はkpi_volshock_signals.compute_membership_breakdown()"
            f"をそのまま再利用)\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
