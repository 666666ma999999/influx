#!/usr/bin/env python3
"""短期リバーサル(20日リターン反転) シグナル生成器(カタログ§2-A・v0リスト#1・第9周新規実装)。

docs/stock-algo-kpi-catalog.md の §2-A「短期リバーサル(20日リターン反転)」を実装する。
`data/jquants/bars/YYYYMMDD.json.gz`(調整済み終値)と `scripts/measure_base_rate.py` が
既に出力済みの `output/base_rate/universes_w{window}.csv.gz` のみで完結する(fins不使用)。

定義(グリッドサーチはしない・1構成のみ・月次スナップショット型):
    - 各月末営業日Tについて、前月末ユニバース(look-ahead回避・kpi_event_study.run_event_study の
      _universe_membership と同一の「signal_month より厳密に前の月のうち最大」規則)のメンバー
      全銘柄で R20 = AdjC(T)/AdjC(T-20営業日)-1 を計算する
    - そのメンバー内でのR20下位10%(分位点quantile(0.10)以下)をシグナル化
    - シグナル確定日 = T(Tそのものの終値とT-20営業日の終値のみ使用・look-aheadなし)

R20の20営業日は暦固定窓(bday_indexでのT-20)である点に注意
(kpi_volshock_signals.py の出来高ショックが採用する「直近N回の有効観測値」実務版とは異なり、
カタログ本文の「AdjC(T)/AdjC(T-20営業日)-1」という明示式に忠実に固定窓で実装した)。

このKPIは値上がり・値下がり銘柄のユニバース入替効果と混同されやすいため
(カタログ§2-A注記・§6手順5)、判定対象(ユニバース内)シグナルのmembership(new/existing)内訳を
scripts/kpi_volshock_signals.compute_membership_breakdown()で算出する
(Canonical Module原則・membership判定ロジックの再実装はしない)。

フォワードリターン計算・ユニバース判定・ブートストラップCI等は scripts/kpi_event_study.py の
run_event_study() に委譲する(--defer-entryは既定True)。

Usage:
    python3 scripts/kpi_strev_signals.py --start 2016-11 --end 2022-11
    python3 scripts/kpi_strev_signals.py --start 2016-11 --end 2022-11 --skip-harness
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import kpi_event_study  # noqa: E402  (Canonical Module: run_event_study を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・MONTH_RE を再利用)
import kpi_volshock_signals  # noqa: E402  (Canonical Module: compute_membership_breakdown を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読み込みを再利用)

KPI_NAME = "strev_20d"

R20_WINDOW = 20  # AdjC(T)/AdjC(T-20営業日)-1 の営業日オフセット(暦固定窓)
BOTTOM_PCT_DEFAULT = 0.10  # ユニバース内R20下位10%


# --- シグナル生成 ---------------------------------------------------------------


def generate_strev_signals(
    start_month: str,
    end_month: str,
    base_rate_dir: Path,
    universe_window: int = 21,
    bottom_pct: float = BOTTOM_PCT_DEFAULT,
) -> tuple[pd.DataFrame, dict]:
    """[start_month, end_month](YYYY-MM)内の各月末営業日Tから短期リバーサルシグナルを生成する。

    Returns:
        (signals_df, diag)。signals_df の列: signal_date, code, r20, prior_universe_month。
        diag はフィルタ段階別の件数内訳。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    month_ends = measure_base_rate.month_ends_in_range(calendar_days, start_month, end_month)
    if not month_ends:
        raise SystemExit(f"FATAL: 指定範囲 {start_month}〜{end_month} に月末営業日が見つかりません。")

    universes_path = base_rate_dir / f"universes_w{universe_window}.csv.gz"
    if not universes_path.exists():
        raise SystemExit(
            f"FATAL: ユニバースCSVが見つかりません: {universes_path}\n"
            f"先に `scripts/measure_base_rate.py` を実行してください。"
        )
    universes_df = pd.read_csv(universes_path, dtype={"month": str, "code": str})
    universe_by_month: dict[str, set] = {m: set(g["code"]) for m, g in universes_df.groupby("month")}
    universe_months_sorted = sorted(universe_by_month.keys())

    diag = {
        "month_ends_scanned": len(month_ends),
        "no_prior_universe_month": 0,
        "prior_universe_total_codes": 0,
        "missing_bars_t_or_prior": 0,
        "r20_computed": 0,
        "signals_strev20d": 0,
    }

    rows: list[dict] = []

    for t_date in month_ends:
        signal_month = t_date[:6]  # kpi_event_study._universe_membership と同じ6桁(ハイフン無し)表記
        # ハーネス(kpi_event_study._universe_membership)と同一の「厳密に前の月」規則を踏襲:
        # signal_month より文字列比較で厳密に小さい月のうち最大のものを前月ユニバースとして使う
        # (look-ahead回避。当月の月末ユニバースは未来の出来高データで構築されているため使わない)。
        earlier_months = [m for m in universe_months_sorted if m < signal_month]
        if not earlier_months:
            diag["no_prior_universe_month"] += 1
            continue
        prior_month = max(earlier_months)
        prior_codes = universe_by_month[prior_month]
        diag["prior_universe_total_codes"] += len(prior_codes)

        idx = bday_index[t_date]
        idx_prior20 = idx - R20_WINDOW
        if idx_prior20 < 0:
            continue  # in-sample開始が十分後ろのため通常は発生しない
        t_minus20 = all_bdays[idx_prior20]

        bars_t = measure_base_rate.load_bars_day(t_date)
        bars_prior20 = measure_base_rate.load_bars_day(t_minus20)

        r20_by_code: dict[str, float] = {}
        for code in prior_codes:
            rec_t = bars_t.get(code)
            rec_prior = bars_prior20.get(code)
            adjc_t = rec_t.get("AdjC") if rec_t else None
            adjc_prior = rec_prior.get("AdjC") if rec_prior else None
            if not adjc_t or not adjc_prior:
                diag["missing_bars_t_or_prior"] += 1
                continue
            r20_by_code[code] = adjc_t / adjc_prior - 1
            diag["r20_computed"] += 1

        if not r20_by_code:
            continue

        r20_series = pd.Series(r20_by_code)
        threshold = r20_series.quantile(bottom_pct)
        selected = r20_series[r20_series <= threshold]
        for code, r20 in selected.items():
            diag["signals_strev20d"] += 1
            rows.append(
                {
                    "signal_date": t_date,
                    "code": code,
                    "r20": r20,
                    "prior_universe_month": prior_month,
                }
            )

    return pd.DataFrame(rows), diag


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="短期リバーサル(20日リターン反転) シグナル生成器(カタログ§2-A・v0リスト#1)+ KPI検証ハーネス実行"
    )
    parser.add_argument(
        "--start", default=kpi_pead_signals.IN_SAMPLE_START,
        help=f"検索開始月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_START})",
    )
    parser.add_argument(
        "--end", default=kpi_pead_signals.IN_SAMPLE_END,
        help=f"検索終了月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_END})",
    )
    parser.add_argument("--bottom-pct", type=float, default=BOTTOM_PCT_DEFAULT)
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

    base_rate_dir = Path(args.base_rate_dir)
    signals_df, diag = generate_strev_signals(
        args.start, args.end, base_rate_dir, args.universe_window, args.bottom_pct
    )

    output_root = Path(args.output_dir)
    kpi_dir = output_root / args.kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_path = kpi_dir / "signals_raw.csv"
    signals_df.to_csv(signals_path, index=False)

    avg_prior_universe = (
        diag["prior_universe_total_codes"] / max(1, diag["month_ends_scanned"] - diag["no_prior_universe_month"])
    )
    print(
        f"シグナル生成完了: {len(signals_df)}件(bottom_pct={args.bottom_pct:.0%})"
    )
    print(
        f"月末走査={diag['month_ends_scanned']}ヶ月 / 前月ユニバース無し(スキップ)={diag['no_prior_universe_month']} / "
        f"前月ユニバース平均サイズ={avg_prior_universe:.1f} / "
        f"bars欠損除外={diag['missing_bars_t_or_prior']} / R20算出={diag['r20_computed']} / "
        f"シグナル成立={diag['signals_strev20d']}"
    )
    print(f"出力: {signals_path}")

    if args.skip_harness:
        return 0
    if signals_df.empty:
        print("WARN: シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    defer_entry = not args.no_defer_entry
    params = {
        "r20_window": R20_WINDOW,
        "bottom_pct": args.bottom_pct,
        "defer_entry": defer_entry,
    }
    result = kpi_event_study.run_event_study(
        signals_df=signals_df[["signal_date", "code"]],
        kpi_name=args.kpi_name,
        params=params,
        period=(args.start, args.end),
        output_dir=output_root,
        base_rate_dir=base_rate_dir,
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
        result["returns_path"], base_rate_dir, args.universe_window
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
        f.write(
            f"- シグナル母集団(前月末ユニバース)平均サイズ: {avg_prior_universe:.1f}銘柄/月"
            f"(このKPI自体が「前月末ユニバースの下位10%」という定義のため、シグナルのユニバース内外は"
            f"当月ユニバースとの入替で生じる。前月時点で存在した銘柄が当月ユニバースから外れて"
            f"out_of_universe判定になるケースがこのKPI特有の入替バイアス経路である)\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
