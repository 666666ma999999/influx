#!/usr/bin/env python3
"""PEAD×進捗率異常 交差シグナル生成器（カタログ§2-D #10 × §2-E #8・第6周複合検証）。

第1周で検証したPEAD初動（決算翌日ギャップ+8%×出来高3倍・kpi_pead_signals.py）のうち、
その反応日Gを引き起こした開示レコード自体が第6周で全四半期拡張した進捗率異常条件
（1Q>=40%/2Q>=65%/3Q>=85%・kpi_shinchoku_signals.py）も満たすものだけを抽出する。

仮説: 「値動きの初動（ギャップ+大商い）」と「ファンダの裏付け（進捗率異常）」が同時成立する
イベントは、初動だけのPEADシグナルよりドリフトの質が高い（=リフトCI下限・EVが改善する）。

母集団生成・交差判定ともに既存の生成器の関数をそのまま再利用し、ロジックを複製しない
（Canonical Module原則）:
    - PEAD側の母集団生成（ギャップ+8%×出来高3倍・15:00反応日ルール・重複集約）は
      scripts/kpi_pead_signals.py の generate_pead_signals() にそのまま委譲する。
    - 進捗率異常の判定（四半期別閾値・REIT除外・OP/FOPパース）は
      scripts/kpi_shinchoku_signals.py の evaluate_shinchoku_condition() にそのまま委譲する。
    - フォワードリターン計算・ユニバース判定・ブートストラップCI等は
      scripts/kpi_event_study.py の run_event_study() に委譲する（変更禁止モジュール）。

交差判定の実装方針: generate_pead_signals() が返す各シグナル行は
(disclosed_date, code, doc_type) で反応日Gを引き起こした開示レコードを一意に特定できる
（同日同codeで複数レコードがある場合はkpi_pead_signals内のby_code集約でFinancialStatements系が
優先選択済みのため、doc_typeも含めて再照合すれば同一レコードに一致する）。そのレコードを
fins キャッシュから再取得し、OP/FOPを取り出してevaluate_shinchoku_condition()に渡す。

Usage:
    python3 scripts/kpi_pead_x_shinchoku_signals.py --start 2016-11 --end 2022-11
    python3 scripts/kpi_pead_x_shinchoku_signals.py --start 2016-11 --end 2022-11 --skip-harness
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import kpi_event_study  # noqa: E402  (Canonical Module: run_event_study を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: generate_pead_signals/load_fins_day/
# IN_SAMPLE_START・END/MONTH_RE/GAP_THRESHOLD_DEFAULT等を再利用)
import kpi_shinchoku_signals  # noqa: E402  (Canonical Module: evaluate_shinchoku_condition/
# QUARTER_THRESHOLDS_DEFAULT/ALL_QUARTERSを再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー読み込みを再利用)

KPI_NAME = "pead_x_shinchoku"


# --- シグナル生成 ---------------------------------------------------------------


def generate_pead_x_shinchoku_signals(
    start_bd: str,
    end_bd: str,
    gap_threshold: float = kpi_pead_signals.GAP_THRESHOLD_DEFAULT,
    vol_multiplier: float = kpi_pead_signals.VOL_MULTIPLIER_DEFAULT,
    vol_window: int = kpi_pead_signals.VOL_WINDOW_DEFAULT,
    quarters: tuple[int, ...] = kpi_shinchoku_signals.ALL_QUARTERS,
    thresholds: dict[int, float] = kpi_shinchoku_signals.QUARTER_THRESHOLDS_DEFAULT,
) -> tuple[pd.DataFrame, dict]:
    """PEAD初動シグナルのうち、反応日Gを起こした開示レコード自体が進捗率異常も満たすものを抽出する。

    Returns:
        (signals_df, diag)。signals_df はPEAD側の全列
        (signal_date, code, disclosed_date, disc_time, reaction_rule, doc_type,
        gap_pct, va_ratio, max20, dev25) + quarter, op, fop, progress_ratio, shinchoku_threshold。
        diag はPEAD側件数と進捗率条件不成立の内訳。
    """
    quarters_set = set(quarters)
    pead_df, pead_diag = kpi_pead_signals.generate_pead_signals(
        start_bd, end_bd, gap_threshold, vol_multiplier, vol_window
    )

    diag = {
        "pead_disclosure_records_total": pead_diag["total_disclosure_records"],
        "pead_signals_statement_only": pead_diag["signals_statement_only"],
        "shinchoku_condition_not_met": 0,
        "signals_pead_x_shinchoku": 0,
    }

    if pead_df.empty:
        return pead_df, diag

    rows: list[dict] = []
    fins_cache: dict[str, list[dict]] = {}
    for _, sig in pead_df.iterrows():
        disclosed_date = str(sig["disclosed_date"])
        code = sig["code"]
        doc_type = sig["doc_type"]
        if disclosed_date not in fins_cache:
            fins_cache[disclosed_date] = kpi_pead_signals.load_fins_day(disclosed_date)
        rec = next(
            (
                r
                for r in fins_cache[disclosed_date]
                if r.get("Code") == code and (r.get("DocType", "") or "") == doc_type
            ),
            None,
        )
        if rec is None:
            # PEAD側で選ばれたレコードが同日再取得で見つからない場合はデータ不整合であり、
            # サイレントスキップせず明確なFATALで停止する。
            raise SystemExit(
                f"FATAL: disclosed_date={disclosed_date} code={code} doc_type={doc_type} の"
                f"開示レコードがfinsキャッシュ再走査で見つかりません（kpi_pead_signalsの母集団と不整合）。"
            )
        shinchoku = kpi_shinchoku_signals.evaluate_shinchoku_condition(
            doc_type, rec.get("OP"), rec.get("FOP"), quarters_set, thresholds
        )
        if shinchoku is None:
            diag["shinchoku_condition_not_met"] += 1
            continue

        diag["signals_pead_x_shinchoku"] += 1
        row = sig.to_dict()
        row.update(
            {
                "quarter": shinchoku["quarter"],
                "op": shinchoku["op"],
                "fop": shinchoku["fop"],
                "progress_ratio": shinchoku["progress_ratio"],
                "shinchoku_threshold": shinchoku["threshold"],
            }
        )
        rows.append(row)

    return pd.DataFrame(rows), diag


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PEAD×進捗率異常 交差シグナル生成器（第6周複合検証）+ KPI検証ハーネス実行"
    )
    parser.add_argument(
        "--start", default=kpi_pead_signals.IN_SAMPLE_START,
        help=f"検索開始月 YYYY-MM（既定 {kpi_pead_signals.IN_SAMPLE_START}）",
    )
    parser.add_argument(
        "--end", default=kpi_pead_signals.IN_SAMPLE_END,
        help=f"検索終了月 YYYY-MM（既定 {kpi_pead_signals.IN_SAMPLE_END}）",
    )
    parser.add_argument("--gap-threshold", type=float, default=kpi_pead_signals.GAP_THRESHOLD_DEFAULT)
    parser.add_argument("--vol-multiplier", type=float, default=kpi_pead_signals.VOL_MULTIPLIER_DEFAULT)
    parser.add_argument("--vol-window", type=int, default=kpi_pead_signals.VOL_WINDOW_DEFAULT)
    parser.add_argument(
        "--quarters", default="1,2,3",
        help="進捗率異常側の対象四半期（カンマ区切り。1/2/3のみ。既定 '1,2,3'）",
    )
    parser.add_argument("--kpi-name", default=KPI_NAME)
    parser.add_argument("--output-dir", default="output/kpi", help="ハーネス出力先ルート（kpi-name配下に生成）")
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

    quarters = kpi_shinchoku_signals._parse_quarters_arg(args.quarters)

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays_all = measure_base_rate.all_business_days(calendar_days)
    start_bound = args.start.replace("-", "") + "01"
    end_bound = args.end.replace("-", "") + "31"
    start_bd = next((d for d in all_bdays_all if d >= start_bound), None)
    end_bd = next((d for d in reversed(all_bdays_all) if d <= end_bound), None)
    if start_bd is None or end_bd is None:
        raise SystemExit("FATAL: 指定期間に営業日が見つかりません")

    signals_df, diag = generate_pead_x_shinchoku_signals(
        start_bd, end_bd, args.gap_threshold, args.vol_multiplier, args.vol_window,
        quarters, kpi_shinchoku_signals.QUARTER_THRESHOLDS_DEFAULT,
    )

    output_root = Path(args.output_dir)
    kpi_dir = output_root / args.kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_path = kpi_dir / "signals_raw.csv"
    signals_df.to_csv(signals_path, index=False)

    print(
        f"シグナル生成完了: {len(signals_df)}件"
        f"（PEAD母集団{diag['pead_signals_statement_only']}件中、進捗率異常不成立"
        f"={diag['shinchoku_condition_not_met']}件を除外）"
    )
    print(f"出力: {signals_path}")

    if args.skip_harness:
        return 0
    if signals_df.empty:
        print("WARN: シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    defer_entry = not args.no_defer_entry
    params = {
        "gap_threshold": args.gap_threshold,
        "vol_multiplier": args.vol_multiplier,
        "vol_window": args.vol_window,
        "quarters": list(quarters),
        "quarter_thresholds": {
            str(q): kpi_shinchoku_signals.QUARTER_THRESHOLDS_DEFAULT[q] for q in quarters
        },
        "reaction_cutoff": "15:00",
        "doc_type_filter": "FinancialStatements(PEAD側) x 1Q/2Q/3QFinancialStatements(進捗率側)",
        "defer_entry": defer_entry,
    }
    result = kpi_event_study.run_event_study(
        signals_df=signals_df[["signal_date", "code"]],
        kpi_name=args.kpi_name,
        params=params,
        period=(args.start, args.end),
        output_dir=output_root,
        defer_entry=defer_entry,
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
