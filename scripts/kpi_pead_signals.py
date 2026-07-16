#!/usr/bin/env python3
"""PEAD初動シグナル生成器（決算翌日ギャップ×出来高ドリフト・カタログ§2-E #8）。

docs/stock-algo-kpi-catalog.md の §2-E「決算翌日ギャップ×出来高ドリフト（PEAD初動型）」を実装する。
`data/jquants/fins/YYYYMMDD.json.gz`（決算短信開示・別ビルダー取得分）から各営業日の決算開示銘柄を
取得し、反応日Gでの (ギャップ+8%以上) AND (出来高20日平均比3倍以上) をシグナル条件として判定する。

定義（グリッドサーチはしない・1構成のみ。カタログ§0-1「閾値は初期値」に反し固定運用する）:
    - 反応日G: 開示時刻(DiscTime)が15:00より前 -> 開示日当日 / 15:00以降 or 時刻不明 -> 翌営業日
    - シグナル条件: (AdjC(G)/AdjC(G前営業日)-1 >= +8%) AND (Va(G) >= 直近20営業日平均Va × 3)
    - シグナル確定日 = G（G終値までの情報のみ使用・look-aheadなし）
    - 開示種別(DocType)は "FinancialStatements" を含むもの（四半期・本決算）に限定する。
      業績予想修正(EarnForecastRevision)等は対象外（カタログ#9用の別KPI）。

フォワードリターン計算・ユニバース判定・ブートストラップCI等は scripts/kpi_event_study.py の
run_event_study() に委譲する（Canonical Module原則・再実装しない）。カレンダー・株価四本値の
読み込みは scripts/measure_base_rate.py の関数を再利用する。

Usage:
    python3 scripts/kpi_pead_signals.py --start 2016-11 --end 2022-11
    python3 scripts/kpi_pead_signals.py --start 2016-11 --end 2022-11 --skip-harness  # シグナル生成のみ
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: DATA_ROOT / read_json_gz を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: run_event_study を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読み込みを再利用)

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

FINS_DIR = jq_fetch.DATA_ROOT / "fins"

# §0確認事項5・§6標本分割で凍結: in-sampleは2016-11〜2022-11。holdout(2023以降)はロック中。
IN_SAMPLE_START = "2016-11"
IN_SAMPLE_END = "2022-11"

KPI_NAME = "pead_initial_gap8_vol3"

GAP_THRESHOLD_DEFAULT = 0.08
VOL_MULTIPLIER_DEFAULT = 3.0
VOL_WINDOW_DEFAULT = 20
REACTION_CUTOFF_MINUTES = 15 * 60  # 15:00（〜2024-11-04・東証現物の旧引け時刻）
REACTION_CUTOFF_MINUTES_EXTENDED = 15 * 60 + 30  # 15:30（2024-11-05以降・東証現物の引け時刻延長後）
REACTION_CUTOFF_CHANGE_BD = "20241105"  # §7-J「反応日境界の制度対応」: 東証現物の取引時間延長日

# 第2周: PEAD × 負けフィルタ1層の複合検証用（カタログ§2-A「MAX効果」・§2-A「25日移動平均線乖離率」）。
# いずれもGを含まない/G-1までのデータのみで計算するPIT安全な指標（Gは+8%ギャップ発生日そのものなので
# ウィンドウから必ず除外する）。
MAX20_WINDOW = 20  # 反応日Gを含まない直前20営業日(G-20〜G-1)の日次リターン最大値
DEV25_WINDOW = 25  # G-1終値時点の25日移動平均乖離率

# 決算短信本体（四半期・本決算）のみを対象にする。業績予想修正・配当予想修正等は含めない
# （カタログ#9・増配KPI用の別開示イベントであり、本KPIの定義とは異なる母集団のため）。
STATEMENT_DOCTYPE_RE = re.compile(r"FinancialStatements")


# --- fins読み込み ---------------------------------------------------------------


def load_fins_day(date_str: str) -> list[dict]:
    """指定営業日の決算開示レコード一覧を返す（未取得なら明確なエラーで停止）。"""
    path = FINS_DIR / f"{date_str}.json.gz"
    if not path.exists():
        raise SystemExit(
            f"FATAL: finsキャッシュが見つかりません: {path}\n"
            f"決算開示データ取得（jq_fetch.py の fins 系統）がこの日付までまだ到達していない"
            f"可能性があります。`data/jquants/fins/` の充足状況を確認してください。"
        )
    obj = jq_fetch.read_json_gz(path)
    return obj["data"]


def _parse_disc_time_minutes(disc_time: Optional[str]) -> Optional[int]:
    """DiscTime文字列（"15:00" / "15:00:00" 等）を当日0時からの分数に変換する。パース不能/欠損はNone。"""
    if not disc_time or not isinstance(disc_time, str):
        return None
    parts = disc_time.split(":")
    if len(parts) < 2:
        return None
    try:
        hh = int(parts[0])
        mm = int(parts[1])
    except ValueError:
        return None
    return hh * 60 + mm


def reaction_day(
    disclosed_date: str,
    disc_time_minutes: Optional[int],
    bday_index: dict[str, int],
    all_bdays: list[str],
) -> tuple[Optional[str], str]:
    """開示日から反応日Gを判定する（カタログ§2-E準拠 + §7-J「反応日境界の制度対応」）。

    引け時刻より前の開示 -> 開示日当日 / 引け時刻以降 or 時刻不明 -> 翌営業日。
    引け時刻は開示日に応じた日付依存境界（開示日<2024-11-05 -> 15:00 / 開示日>=2024-11-05 -> 15:30・
    東証現物の取引時間延長対応）。時刻が不明（フィールド欠損・パース不能）な場合は安全側（翌営業日）に倒す。
    in-sample期間（〜2022-11）は全開示が2024-11-05より前のため、本変更後も歴史結果は構造的に不変。

    Returns:
        (G日付, 判定ルール名)。カレンダー範囲外に達した場合は (None, ルール名)。
    """
    idx = bday_index[disclosed_date]
    cutoff_minutes = (
        REACTION_CUTOFF_MINUTES_EXTENDED if disclosed_date >= REACTION_CUTOFF_CHANGE_BD else REACTION_CUTOFF_MINUTES
    )
    if disc_time_minutes is not None and disc_time_minutes < cutoff_minutes:
        g_idx = idx
        rule = "same_day_before_close"
    else:
        g_idx = idx + 1
        rule = "next_day_after_close_or_unknown"
    if g_idx >= len(all_bdays):
        return None, rule
    return all_bdays[g_idx], rule


def compute_max20(code: str, idx_g: int, all_bdays: list[str]) -> Optional[float]:
    """反応日Gを含まない直前20営業日(G-20〜G-1)の日次リターン最大値(MAX20)を計算する。

    日次リターンは各日の前日比 AdjC(d)/AdjC(d-1)-1。20個の日次リターンを得るには
    G-21〜G-1の21営業日分の終値が要る（Gそのものは常に窓外＝look-ahead回避）。
    """
    window_start = idx_g - MAX20_WINDOW - 1
    if window_start < 0:
        return None
    window_days = all_bdays[window_start:idx_g]  # G-21 ... G-1（21日）
    closes = [
        c
        for wd in window_days
        if (c := measure_base_rate.load_bars_day(wd).get(code, {}).get("AdjC"))
    ]
    if len(closes) < 2:
        return None
    daily_rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    return max(daily_rets) if daily_rets else None


def compute_dev25(code: str, idx_g: int, all_bdays: list[str], adjc_prev: float) -> Optional[float]:
    """G-1終値時点の25日移動平均乖離率 dev25=(AdjC(G-1)-SMA25(G-1))/SMA25(G-1) を計算する。

    SMA25はG-1を含む直近25営業日（G-25〜G-1）の終値平均（Gは含まない・look-ahead回避）。
    adjc_prevはAdjC(G-1)（呼び出し側でgap_pct計算のため既に取得済みの値を再利用）。
    """
    window_start = idx_g - DEV25_WINDOW
    if window_start < 0:
        return None
    window_days = all_bdays[window_start:idx_g]  # G-25 ... G-1（25日）
    closes = [
        c
        for wd in window_days
        if (c := measure_base_rate.load_bars_day(wd).get(code, {}).get("AdjC"))
    ]
    if len(closes) < DEV25_WINDOW // 2:
        return None
    sma25 = sum(closes) / len(closes)
    if sma25 <= 0:
        return None
    return (adjc_prev - sma25) / sma25


# --- シグナル生成 ---------------------------------------------------------------


def generate_pead_signals(
    start_bd: str,
    end_bd: str,
    gap_threshold: float = GAP_THRESHOLD_DEFAULT,
    vol_multiplier: float = VOL_MULTIPLIER_DEFAULT,
    vol_window: int = VOL_WINDOW_DEFAULT,
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]（YYYYMMDD営業日）内の決算開示からPEAD初動シグナルを生成する。

    Returns:
        (signals_df, diag)。signals_df は決算短信(FinancialStatements)限定のシグナルのみ
        （列: signal_date, code, disclosed_date, disc_time, reaction_rule, doc_type,
        gap_pct, va_ratio, max20, dev25）。max20/dev25 は第2周の負けフィルタ（MAX効果・
        25日乖離率）用に常時計算して保持する列で、この関数自体はフィルタしない
        （フィルタ適用は呼び出し側=main()の責務。母集団生成KPIとフィルタ層を分離する§3の
        3層構造に合わせた設計）。diag には DocType限定なしで同条件を適用した場合の参考件数も含む
        （decisions: 業績予想修正等を含めるべきかの次回設計判断用）。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    disclosure_days = [d for d in all_bdays if start_bd <= d <= end_bd]

    diag = {
        "disclosure_days_scanned": len(disclosure_days),
        "total_disclosure_records": 0,
        "non_statement_doctype_records": 0,
        "missing_disc_time": 0,
        "signals_statement_only": 0,
        "signals_all_doctypes": 0,  # 参考値: DocType不問で同条件を適用した場合の件数
    }

    rows_statement: list[dict] = []

    for d in disclosure_days:
        records = load_fins_day(d)

        # 同一 code に複数レコード（訂正・別文書種別の同日同時開示等）がある場合、
        # FinancialStatements系のレコードを優先して1件に集約する
        # （非統計系レコードが先に来て決算短信本体を隠してしまうのを防ぐ）。
        by_code: dict[str, dict] = {}
        for rec in records:
            diag["total_disclosure_records"] += 1
            code = rec.get("Code")
            if not code:
                continue
            existing = by_code.get(code)
            if existing is None:
                by_code[code] = rec
            elif STATEMENT_DOCTYPE_RE.search(rec.get("DocType", "") or "") and not STATEMENT_DOCTYPE_RE.search(
                existing.get("DocType", "") or ""
            ):
                by_code[code] = rec

        for code, rec in by_code.items():
            doc_type = rec.get("DocType", "") or ""
            is_statement = bool(STATEMENT_DOCTYPE_RE.search(doc_type))
            if not is_statement:
                diag["non_statement_doctype_records"] += 1

            disc_time_raw = rec.get("DiscTime")
            if not disc_time_raw:
                diag["missing_disc_time"] += 1
            disc_time_minutes = _parse_disc_time_minutes(disc_time_raw)

            g_date, rule = reaction_day(d, disc_time_minutes, bday_index, all_bdays)
            if g_date is None:
                continue  # 検証期間の終端でカレンダー範囲外

            idx_g = bday_index[g_date]
            if idx_g < vol_window + 1:
                continue  # 期間先頭でルックバック窓が確保できない

            prev_day = all_bdays[idx_g - 1]
            rec_g = measure_base_rate.load_bars_day(g_date).get(code)
            rec_prev = measure_base_rate.load_bars_day(prev_day).get(code)
            if not rec_g or not rec_prev:
                continue
            adjc_g = rec_g.get("AdjC")
            adjc_prev = rec_prev.get("AdjC")
            if not adjc_g or not adjc_prev:
                continue
            gap_pct = adjc_g / adjc_prev - 1

            # G「前」の直近vol_window営業日（Gを含まない・look-ahead回避）
            window_days = all_bdays[idx_g - vol_window : idx_g]
            va_values = [
                v
                for wd in window_days
                if (v := measure_base_rate.load_bars_day(wd).get(code, {}).get("Va"))
            ]
            if len(va_values) < vol_window // 2:
                continue  # 売買停止等でデータが疎な銘柄はスキップ
            va_avg = sum(va_values) / len(va_values)
            va_g = rec_g.get("Va")
            if not va_g or va_avg <= 0:
                continue
            va_ratio = va_g / va_avg

            if not (gap_pct >= gap_threshold and va_ratio >= vol_multiplier):
                continue

            diag["signals_all_doctypes"] += 1
            if not is_statement:
                continue  # 参考カウントのみ・本KPIの母集団には含めない

            diag["signals_statement_only"] += 1
            rows_statement.append(
                {
                    "signal_date": g_date,
                    "code": code,
                    "disclosed_date": d,
                    "disc_time": disc_time_raw,
                    "reaction_rule": rule,
                    "doc_type": doc_type,
                    "gap_pct": gap_pct,
                    "va_ratio": va_ratio,
                    "max20": compute_max20(code, idx_g, all_bdays),
                    "dev25": compute_dev25(code, idx_g, all_bdays, adjc_prev),
                }
            )

    return pd.DataFrame(rows_statement), diag


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PEAD初動シグナル生成器（決算翌日ギャップ×出来高ドリフト）+ KPI検証ハーネス実行"
    )
    parser.add_argument("--start", default=IN_SAMPLE_START, help=f"検索開始月 YYYY-MM（既定 {IN_SAMPLE_START}）")
    parser.add_argument("--end", default=IN_SAMPLE_END, help=f"検索終了月 YYYY-MM（既定 {IN_SAMPLE_END}）")
    parser.add_argument("--gap-threshold", type=float, default=GAP_THRESHOLD_DEFAULT)
    parser.add_argument("--vol-multiplier", type=float, default=VOL_MULTIPLIER_DEFAULT)
    parser.add_argument("--vol-window", type=int, default=VOL_WINDOW_DEFAULT)
    parser.add_argument("--kpi-name", default=KPI_NAME)
    parser.add_argument("--output-dir", default="output/kpi", help="ハーネス出力先ルート（kpi-name配下に生成）")
    parser.add_argument(
        "--filter-max20",
        type=float,
        default=None,
        help="MAX20（Gを含まないG-20〜G-1の日次リターン最大値）がこの閾値以上のシグナルを除外（負けフィルタ・第2周）",
    )
    parser.add_argument(
        "--filter-dev25",
        type=float,
        default=None,
        help="dev25（G-1終値時点の25日移動平均乖離率）がこの閾値以上のシグナルを除外（負けフィルタ・第2周）",
    )
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

    signals_df, diag = generate_pead_signals(
        start_bd, end_bd, args.gap_threshold, args.vol_multiplier, args.vol_window
    )

    output_root = Path(args.output_dir)
    kpi_dir = output_root / args.kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_path = kpi_dir / "signals_raw.csv"
    signals_df.to_csv(signals_path, index=False)

    print(
        f"シグナル生成完了: {len(signals_df)}件"
        f"（決算短信限定。DocType不問なら{diag['signals_all_doctypes']}件 = "
        f"差分{diag['signals_all_doctypes'] - diag['signals_statement_only']}件）"
    )
    print(
        f"開示レコード総数={diag['total_disclosure_records']} "
        f"(非決算短信DocType={diag['non_statement_doctype_records']}, "
        f"DiscTime欠損={diag['missing_disc_time']})"
    )
    print(f"出力: {signals_path}")

    # 第2周: 負けフィルタ（MAX20/dev25）を母集団生成後に適用する（§3の3層構造＝
    # 母集団生成KPIとフィルタ層を分離する設計。generate_pead_signals自体はフィルタしない）。
    filtered_df = signals_df
    if args.filter_max20 is not None and not filtered_df.empty:
        before = len(filtered_df)
        filtered_df = filtered_df[filtered_df["max20"] < args.filter_max20].reset_index(drop=True)
        print(f"MAX20フィルタ(閾値{args.filter_max20}以上を除外): {before}件 -> {len(filtered_df)}件")
    if args.filter_dev25 is not None and not filtered_df.empty:
        before = len(filtered_df)
        filtered_df = filtered_df[filtered_df["dev25"] < args.filter_dev25].reset_index(drop=True)
        print(f"dev25フィルタ(閾値{args.filter_dev25}以上を除外): {before}件 -> {len(filtered_df)}件")
    if args.filter_max20 is not None or args.filter_dev25 is not None:
        filtered_path = kpi_dir / "signals_filtered.csv"
        filtered_df.to_csv(filtered_path, index=False)
        print(f"フィルタ後出力: {filtered_path}（除外前{len(signals_df)}件 -> 除外後{len(filtered_df)}件）")

    if args.skip_harness:
        return 0
    if filtered_df.empty:
        print("WARN: フィルタ後のシグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    params = {
        "gap_threshold": args.gap_threshold,
        "vol_multiplier": args.vol_multiplier,
        "vol_window": args.vol_window,
        "reaction_cutoff": "15:00",
        "doc_type_filter": "FinancialStatements",
        "filter_max20": args.filter_max20,
        "filter_dev25": args.filter_dev25,
        "defer_entry": args.defer_entry,
    }
    result = kpi_event_study.run_event_study(
        signals_df=filtered_df[["signal_date", "code"]],
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
