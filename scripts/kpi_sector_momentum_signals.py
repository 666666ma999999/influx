#!/usr/bin/env python3
"""業種モメンタム×業種内出遅れ（もふ社長型・業種波及の機械化） シグナル生成器（カタログ§2-F・第15周T1）。

docs/stock-algo-kpi-catalog.md の §2-F「業種モメンタム×業種内出遅れ」を実装する。
tasks指示（第15周・頻度直交セット）のT1定義に忠実:
    - 各月末営業日Tについて、前月末ユニバース（look-ahead回避・kpi_strev_signals.py の
      「signal_month より厳密に前の月のうち最大」規則と同一）のメンバー全銘柄で
      R60 = AdjC(T)/AdjC(T-60営業日)-1 を計算する（strevのR20と同じ暦固定窓の流儀。60営業日）
    - 33業種（マスタのS33フィールド）でグループ化し、業種内銘柄R60の等加重平均を業種リターンとする
      （母集団はkpi_strev_signals.pyと同じく前月末ユニバース内に限定。公式業種指数は使わない）
    - 業種リターンが全業種分布の上位20%（quantile(0.80)以上）の業種に属し、かつ業種内で
      自銘柄R60が業種内分布の下位40%（quantile(0.40)以下）の銘柄をシグナル化

MIN_SECTOR_SIZE（実装ノート・グリッドサーチではなくデータ健全性のための必須ガード）:
    業種内メンバー数が1の場合、「業種リターン＝自身のR60」となり「業種内で下位40%」が
    自分自身に対して数学的に自明に成立してしまう（quantile(0.40)は単一値の分布ではその値
    そのものになるため）。これは閾値のチューニングではなく退化ケース（自己参照）の排除であり、
    最小メンバー数(既定5)未満の業種はその月の業種リターン算出対象から除外する
    （diagで除外業種数・除外候補銘柄数を記録し報告に明記する）。

シグナル確定日 = T（Tそのものの終値とT-60営業日の終値のみ使用・look-aheadなし。マスタは
T自身の月末スナップショット=data/jquants/master/{T}.json.gzを使用。業種分類自体の変更は
稀であり、§6横断的PIT実装ルール「業種分類の変更履歴のみ注意」に従いT時点の分類をそのまま使う）。

このKPIは strev_20d と同じ月次スナップショット型のためユニバース入替効果と混同されやすい
（カタログ§6手順5）。判定対象（ユニバース内）シグナルのmembership(new/existing)内訳は
scripts/kpi_volshock_signals.compute_membership_breakdown()で算出する（Canonical Module原則）。

フォワードリターン計算・ユニバース判定・ブートストラップCI等は scripts/kpi_event_study.py の
run_event_study() に委譲する（--defer-entryは既定True・他KPIと同じ流儀）。

Usage:
    python3 scripts/kpi_sector_momentum_signals.py --start 2016-11 --end 2022-11
    python3 scripts/kpi_sector_momentum_signals.py --start 2016-11 --end 2022-11 --skip-harness
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import kpi_event_study  # noqa: E402  (Canonical Module: run_event_study を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・MONTH_RE を再利用)
import kpi_volshock_signals  # noqa: E402  (Canonical Module: compute_membership_breakdown を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars/master読み込みを再利用)

KPI_NAME = "sector_momentum_laggard"

R60_WINDOW = 60  # 業種リターン・自銘柄リターンとも共通の暦固定窓（AdjC(T)/AdjC(T-60営業日)-1）
SECTOR_TOP_PCT = 0.20  # 業種リターン上位20%
LAGGARD_BOTTOM_PCT = 0.40  # 業種内で自銘柄R60が下位40%
MIN_SECTOR_SIZE_DEFAULT = 5  # 退化(自己参照)ケース排除のための最小業種内メンバー数（実装ノート参照）


# --- シグナル生成 ---------------------------------------------------------------


def generate_sector_momentum_signals(
    start_month: str,
    end_month: str,
    base_rate_dir: Path,
    universe_window: int = 21,
    sector_top_pct: float = SECTOR_TOP_PCT,
    laggard_bottom_pct: float = LAGGARD_BOTTOM_PCT,
    min_sector_size: int = MIN_SECTOR_SIZE_DEFAULT,
) -> tuple[pd.DataFrame, dict]:
    """[start_month, end_month](YYYY-MM)内の各月末営業日Tから業種モメンタム×出遅れシグナルを生成する。

    Returns:
        (signals_df, diag)。signals_df の列: signal_date, code, sector, sector_return, r60,
        sector_size, prior_universe_month。diag はフィルタ段階別の件数内訳。
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
        "missing_sector": 0,
        "r60_computed": 0,
        "sectors_excluded_min_size": 0,  # 延べ（月×業種）。MIN_SECTOR_SIZE未満で業種リターン算出から除外
        "codes_excluded_via_small_sector": 0,  # 上記により候補から漏れた延べ銘柄数
        "months_with_lt2_valid_sectors": 0,  # 上位20%分位を意味のある形で計算できない月（有効業種<2）
        "signals_sector_momentum": 0,
    }

    rows: list[dict] = []

    for t_date in month_ends:
        signal_month = t_date[:6]  # kpi_event_study._universe_membership と同じ6桁(ハイフン無し)表記
        # kpi_strev_signals.py と同一のlook-ahead回避規則: signal_month より厳密に前の月のうち
        # 最大のものを前月末ユニバースとして使う。
        earlier_months = [m for m in universe_months_sorted if m < signal_month]
        if not earlier_months:
            diag["no_prior_universe_month"] += 1
            continue
        prior_month = max(earlier_months)
        prior_codes = universe_by_month[prior_month]
        diag["prior_universe_total_codes"] += len(prior_codes)

        idx = bday_index[t_date]
        idx_prior60 = idx - R60_WINDOW
        if idx_prior60 < 0:
            continue  # in-sample開始が十分後ろのため通常は発生しない
        t_minus60 = all_bdays[idx_prior60]

        bars_t = measure_base_rate.load_bars_day(t_date)
        bars_prior60 = measure_base_rate.load_bars_day(t_minus60)
        master_t = measure_base_rate.load_master_day(t_date)

        r60_by_code: dict[str, float] = {}
        sector_by_code: dict[str, str] = {}
        for code in prior_codes:
            rec_t = bars_t.get(code)
            rec_prior = bars_prior60.get(code)
            adjc_t = rec_t.get("AdjC") if rec_t else None
            adjc_prior = rec_prior.get("AdjC") if rec_prior else None
            if not adjc_t or not adjc_prior:
                diag["missing_bars_t_or_prior"] += 1
                continue
            master_rec = master_t.get(code)
            sector = master_rec.get("S33") if master_rec else None
            if not sector:
                diag["missing_sector"] += 1
                continue
            r60_by_code[code] = adjc_t / adjc_prior - 1
            sector_by_code[code] = sector
            diag["r60_computed"] += 1

        if not r60_by_code:
            continue

        sector_groups: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for code, r60 in r60_by_code.items():
            sector_groups[sector_by_code[code]].append((code, r60))

        sector_returns: dict[str, float] = {}
        for sector, items in sector_groups.items():
            if len(items) < min_sector_size:
                diag["sectors_excluded_min_size"] += 1
                diag["codes_excluded_via_small_sector"] += len(items)
                continue
            sector_returns[sector] = sum(r for _, r in items) / len(items)

        if len(sector_returns) < 2:
            diag["months_with_lt2_valid_sectors"] += 1
            continue

        sector_ret_series = pd.Series(sector_returns)
        sector_threshold = sector_ret_series.quantile(1 - sector_top_pct)
        hot_sectors = sector_ret_series[sector_ret_series >= sector_threshold].index

        for sector in hot_sectors:
            items = sector_groups[sector]
            r_series = pd.Series({code: r for code, r in items})
            laggard_threshold = r_series.quantile(laggard_bottom_pct)
            laggards = r_series[r_series <= laggard_threshold]
            for code, r60 in laggards.items():
                diag["signals_sector_momentum"] += 1
                rows.append(
                    {
                        "signal_date": t_date,
                        "code": code,
                        "sector": sector,
                        "sector_return": sector_returns[sector],
                        "r60": r60,
                        "sector_size": len(items),
                        "prior_universe_month": prior_month,
                    }
                )

    return pd.DataFrame(rows), diag


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="業種モメンタム×業種内出遅れ シグナル生成器(カタログ§2-F・第15周T1) + KPI検証ハーネス実行"
    )
    parser.add_argument(
        "--start", default=kpi_pead_signals.IN_SAMPLE_START,
        help=f"検索開始月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_START})",
    )
    parser.add_argument(
        "--end", default=kpi_pead_signals.IN_SAMPLE_END,
        help=f"検索終了月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_END})",
    )
    parser.add_argument("--sector-top-pct", type=float, default=SECTOR_TOP_PCT)
    parser.add_argument("--laggard-bottom-pct", type=float, default=LAGGARD_BOTTOM_PCT)
    parser.add_argument("--min-sector-size", type=int, default=MIN_SECTOR_SIZE_DEFAULT)
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
    signals_df, diag = generate_sector_momentum_signals(
        args.start, args.end, base_rate_dir, args.universe_window,
        args.sector_top_pct, args.laggard_bottom_pct, args.min_sector_size,
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
        f"シグナル生成完了: {len(signals_df)}件"
        f"(sector_top_pct={args.sector_top_pct:.0%}, laggard_bottom_pct={args.laggard_bottom_pct:.0%}, "
        f"min_sector_size={args.min_sector_size})"
    )
    print(
        f"月末走査={diag['month_ends_scanned']}ヶ月 / 前月ユニバース無し(スキップ)={diag['no_prior_universe_month']} / "
        f"前月ユニバース平均サイズ={avg_prior_universe:.1f} / bars欠損除外={diag['missing_bars_t_or_prior']} / "
        f"業種コード欠損除外={diag['missing_sector']} / R60算出={diag['r60_computed']}"
    )
    print(
        f"最小業種サイズ({args.min_sector_size})未満で除外(延べ業種数)={diag['sectors_excluded_min_size']} "
        f"(延べ銘柄数={diag['codes_excluded_via_small_sector']}) / "
        f"有効業種2未満で分位計算不能の月={diag['months_with_lt2_valid_sectors']} / "
        f"シグナル成立={diag['signals_sector_momentum']}"
    )
    print(f"出力: {signals_path}")

    if args.skip_harness:
        return 0
    if signals_df.empty:
        print("WARN: シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    defer_entry = not args.no_defer_entry
    params = {
        "r60_window": R60_WINDOW,
        "sector_top_pct": args.sector_top_pct,
        "laggard_bottom_pct": args.laggard_bottom_pct,
        "min_sector_size": args.min_sector_size,
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
        f.write("\n## ユニバースmembership内訳(第15周・§6手順5注記)\n")
        f.write(
            f"- 判定対象(in_universe)シグナル{result['n']}件のmembership内訳: {membership_line}"
            f"(`output/base_rate/universes_w{args.universe_window}.csv.gz`と突合。"
            f"membership判定ロジック自体はkpi_volshock_signals.compute_membership_breakdown()"
            f"をそのまま再利用)\n"
        )
        f.write(
            f"\n## 実装ノート追記(第15周T1)\n"
            f"- 最小業種サイズガード(min_sector_size={args.min_sector_size}): "
            f"業種内メンバー数がこの値未満の業種はその月の「業種リターン上位20%」判定対象から除外する"
            f"(単一メンバー業種は「業種リターン=自身のR60」となり「業種内で下位40%」が自明に成立してしまう"
            f"退化ケースを排除するための必須ガードであり、閾値のグリッドサーチではない)。"
            f"延べ除外業種数={diag['sectors_excluded_min_size']}(延べ銘柄数={diag['codes_excluded_via_small_sector']})\n"
            f"- 業種分類は各月末営業日T自身のマスタスナップショット(`data/jquants/master/{{T}}.json.gz`のS33"
            f"フィールド)を使用(§6横断的PIT実装ルール「業種分類の変更履歴のみ注意」に従い、稀な再分類以外は"
            f"変更履歴管理を行わない初期実装とした)\n"
            f"- 業種リターン・自銘柄リターンとも計算母集団は前月末ユニバース(TOP{args.universe_window}日売買代金"
            f"上位500)内に限定(strev_20dと同一のlook-ahead回避規則を踏襲)。公式33業種指数は使用していない\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
