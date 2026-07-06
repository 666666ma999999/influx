#!/usr/bin/env python3
"""四半期進捗率異常 シグナル生成器（カタログ§2-D #10・v0リスト#10・全四半期版）。

docs/stock-algo-kpi-catalog.md の §2-D「四半期進捗率異常」を実装する。
`data/jquants/fins/YYYYMMDD.json.gz` から DocType に "1Q"/"2Q"/"3Q" 始まりの
FinancialStatements を含む開示（REIT系除外）を抽出し、同一レコード内の OP（累計営業利益）と
FOP（通期会社予想・営業利益）から進捗率 OP/FOP を四半期別の固定閾値で判定する。

定義（グリッドサーチはしない・1構成のみ。過去5年同四半期平均比は履歴不足のため次周以降の課題）:
    - イベント: DocType が "1QFinancialStatements"/"2QFinancialStatements"/"3QFinancialStatements"
      で始まる（"_Consolidated_JP" 等の連結/単体/会計基準違いを問わず対象）。
      FY（本決算）・4Q相当は進捗率概念が成立しないため対象外（DocTypeにそもそも"4Q"は存在せず
      本決算は"FY..."表記のため、正規表現が[1-3]Qのみに一致することで自然に除外される）。
      DocTypeに"REIT"を含むものは明示的に除外する。
    - 条件: OP・FOPがともに数値かつFOP>0（黒字予想）、進捗率 = OP/FOP が四半期別閾値以上
      （1Q>=0.40 / 2Q>=0.65 / 3Q>=0.85。カタログの「線形+10pt」方針に準拠した固定値・1構成のみ）
    - 反応日G: PEAD(kpi_pead_signals.py)と同じ15:00ルールを再利用（Canonical Module）。
      Gの価格条件は付けない（純粋イベント）。シグナル確定日 = G。

後方互換: --quarters 1 を指定すると、対象DocTypeを1Qのみ・閾値0.40のみに絞り込み、
第5周までの1Q限定版(shinchoku_1q40)と同一のシグナル集合を再現する
（正規表現をREIT除外→[1-3]Q始まりの順に適用する制御フローも維持済み）。

反応日判定・DiscTimeパース・fins読み込みは scripts/kpi_pead_signals.py の
reaction_day() / _parse_disc_time_minutes() / load_fins_day() を、数値パースは
scripts/kpi_uprev_signals.py の _parse_numeric() を再利用する（Canonical Module原則・
再実装しない）。フォワードリターン計算・ユニバース判定・ブートストラップCI等は
scripts/kpi_event_study.py の run_event_study() に委譲する。

本モジュールの evaluate_shinchoku_condition() は、単一の開示レコード(DocType/OP/FOP)から
進捗率異常条件の成立可否を判定する共通ヘルパーであり、
scripts/kpi_pead_x_shinchoku_signals.py（第6周・PEAD×進捗率交差検証）からも再利用される
（Canonical Module原則・四半期別閾値ロジックを複製しない）。

第5周より: --defer-entry を既定Trueとする（§6手順6準拠モードに統一。team-lead方針）。

Usage:
    python3 scripts/kpi_shinchoku_signals.py --start 2016-11 --end 2022-11
    python3 scripts/kpi_shinchoku_signals.py --start 2016-11 --end 2022-11 --quarters 1 --kpi-name shinchoku_1q40
    python3 scripts/kpi_shinchoku_signals.py --start 2016-11 --end 2022-11 --skip-harness
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import kpi_event_study  # noqa: E402  (Canonical Module: run_event_study を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: reaction_day/_parse_disc_time_minutes/
# load_fins_day/IN_SAMPLE_START・END/MONTH_RE を再利用)
import kpi_uprev_signals  # noqa: E402  (Canonical Module: _parse_numeric を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー読み込みを再利用)

KPI_NAME = "shinchoku_allq"

# 四半期別の進捗率閾値（カタログ§2-D「四半期進捗率異常」・第6周確定値。1構成のみ・グリッドサーチなし）。
QUARTER_THRESHOLDS_DEFAULT: dict[int, float] = {1: 0.40, 2: 0.65, 3: 0.85}
ALL_QUARTERS: tuple[int, ...] = (1, 2, 3)

# "1QFinancialStatements_Consolidated_JP" 等、連結/単体・会計基準違いを問わず
# 1Q/2Q/3Q決算短信本体の先頭一致のみを対象にする（FY本決算・その他は対象外）。
QUARTER_DOCTYPE_RE = re.compile(r"^([123])QFinancialStatements")


# --- 進捗率判定（Canonical Module: 本KPI・PEAD×進捗率交差の双方から再利用） -------------


def evaluate_shinchoku_condition(
    doc_type: Optional[str],
    op_raw,
    fop_raw,
    quarters: set[int],
    thresholds: dict[int, float] = QUARTER_THRESHOLDS_DEFAULT,
) -> Optional[dict]:
    """単一の開示レコード(DocType/OP/FOP)が進捗率異常条件を満たすか判定する。

    Args:
        doc_type: 開示レコードのDocType文字列。
        op_raw: OPフィールドの生値（文字列・空文字・Noneのいずれか）。
        fop_raw: FOPフィールドの生値。
        quarters: 対象とする四半期の集合（例: {1, 2, 3}）。
        thresholds: 四半期 -> 進捗率閾値の辞書。

    Returns:
        条件を満たす場合 {"quarter": int, "op": float, "fop": float,
        "progress_ratio": float, "threshold": float}。満たさない/対象外の場合は None。
    """
    doc_type = doc_type or ""
    if "REIT" in doc_type:
        return None
    m = QUARTER_DOCTYPE_RE.match(doc_type)
    if not m:
        return None
    quarter = int(m.group(1))
    if quarter not in quarters:
        return None
    op = kpi_uprev_signals._parse_numeric(op_raw)
    fop = kpi_uprev_signals._parse_numeric(fop_raw)
    if op is None or fop is None:
        return None
    if fop <= 0:
        return None
    threshold = thresholds[quarter]
    progress_ratio = op / fop
    if progress_ratio < threshold:
        return None
    return {
        "quarter": quarter,
        "op": op,
        "fop": fop,
        "progress_ratio": progress_ratio,
        "threshold": threshold,
    }


# --- シグナル生成 ---------------------------------------------------------------


def generate_shinchoku_signals(
    start_bd: str,
    end_bd: str,
    quarters: tuple[int, ...] = ALL_QUARTERS,
    thresholds: dict[int, float] = QUARTER_THRESHOLDS_DEFAULT,
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]（YYYYMMDD営業日）内の1Q/2Q/3Q決算短信開示から進捗率異常シグナルを生成する。

    Returns:
        (signals_df, diag)。signals_df の列: signal_date, code, disclosed_date, disc_time,
        reaction_rule, doc_type, quarter, op, fop, progress_ratio, threshold_used。
        diag にはフィルタ段階別の件数内訳（四半期別breakdown含む）を含む。
    """
    quarters_set = set(quarters)
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    disclosure_days = [d for d in all_bdays if start_bd <= d <= end_bd]

    diag = {
        "disclosure_days_scanned": len(disclosure_days),
        "total_disclosure_records": 0,
        "reit_excluded": 0,
        "non_target_quarter_statement_records": 0,  # REIT以外・[1-3]Q決算短信でないもの
        "quarter_not_in_scope": 0,  # [1-3]Q決算短信だが --quarters で対象外指定
        "events_by_quarter": {q: 0 for q in ALL_QUARTERS},
        "op_or_fop_missing_nonnumeric": 0,
        "fop_not_positive": 0,
        "progress_ratio_established_by_quarter": {q: 0 for q in ALL_QUARTERS},
        "signals_by_quarter": {q: 0 for q in ALL_QUARTERS},
        "signals_total": 0,
    }

    rows: list[dict] = []

    for d in disclosure_days:
        records = kpi_pead_signals.load_fins_day(d)
        for rec in records:
            diag["total_disclosure_records"] += 1
            code = rec.get("Code")
            doc_type = rec.get("DocType", "") or ""
            if not code:
                continue
            if "REIT" in doc_type:
                diag["reit_excluded"] += 1
                continue
            m = QUARTER_DOCTYPE_RE.match(doc_type)
            if not m:
                diag["non_target_quarter_statement_records"] += 1
                continue
            quarter = int(m.group(1))
            if quarter not in quarters_set:
                diag["quarter_not_in_scope"] += 1
                continue
            diag["events_by_quarter"][quarter] += 1

            op = kpi_uprev_signals._parse_numeric(rec.get("OP"))
            fop = kpi_uprev_signals._parse_numeric(rec.get("FOP"))
            if op is None or fop is None:
                diag["op_or_fop_missing_nonnumeric"] += 1
                continue
            if fop <= 0:
                diag["fop_not_positive"] += 1
                continue
            diag["progress_ratio_established_by_quarter"][quarter] += 1

            threshold = thresholds[quarter]
            progress_ratio = op / fop
            if progress_ratio < threshold:
                continue

            disc_time_raw = rec.get("DiscTime")
            disc_time_minutes = kpi_pead_signals._parse_disc_time_minutes(disc_time_raw)
            g_date, rule = kpi_pead_signals.reaction_day(d, disc_time_minutes, bday_index, all_bdays)
            if g_date is None:
                continue  # 検証期間の終端でカレンダー範囲外

            diag["signals_by_quarter"][quarter] += 1
            diag["signals_total"] += 1
            rows.append(
                {
                    "signal_date": g_date,
                    "code": code,
                    "disclosed_date": d,
                    "disc_time": disc_time_raw,
                    "reaction_rule": rule,
                    "doc_type": doc_type,
                    "quarter": quarter,
                    "op": op,
                    "fop": fop,
                    "progress_ratio": progress_ratio,
                    "threshold_used": threshold,
                }
            )

    return pd.DataFrame(rows), diag


# --- メイン処理 -----------------------------------------------------------------


def _parse_quarters_arg(raw: str) -> tuple[int, ...]:
    quarters = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            q = int(part)
        except ValueError:
            raise SystemExit(f"FATAL: --quarters の値が不正です: {part!r}（1/2/3のカンマ区切りのみ）")
        if q not in ALL_QUARTERS:
            raise SystemExit(f"FATAL: --quarters は1/2/3のみ対応（FY/4Qは進捗率概念が成立しない）: {q}")
        quarters.append(q)
    if not quarters:
        raise SystemExit("FATAL: --quarters が空です")
    return tuple(sorted(set(quarters)))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="四半期進捗率異常 シグナル生成器（カタログ§2-D #10・全四半期版）+ KPI検証ハーネス実行"
    )
    parser.add_argument(
        "--start", default=kpi_pead_signals.IN_SAMPLE_START,
        help=f"検索開始月 YYYY-MM（既定 {kpi_pead_signals.IN_SAMPLE_START}）",
    )
    parser.add_argument(
        "--end", default=kpi_pead_signals.IN_SAMPLE_END,
        help=f"検索終了月 YYYY-MM（既定 {kpi_pead_signals.IN_SAMPLE_END}）",
    )
    parser.add_argument(
        "--quarters", default="1,2,3",
        help="対象四半期（カンマ区切り。1/2/3のみ。既定 '1,2,3'。'1'指定で第5周shinchoku_1q40相当を再現）",
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

    quarters = _parse_quarters_arg(args.quarters)

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays_all = measure_base_rate.all_business_days(calendar_days)
    start_bound = args.start.replace("-", "") + "01"
    end_bound = args.end.replace("-", "") + "31"
    start_bd = next((d for d in all_bdays_all if d >= start_bound), None)
    end_bd = next((d for d in reversed(all_bdays_all) if d <= end_bound), None)
    if start_bd is None or end_bd is None:
        raise SystemExit("FATAL: 指定期間に営業日が見つかりません")

    signals_df, diag = generate_shinchoku_signals(start_bd, end_bd, quarters, QUARTER_THRESHOLDS_DEFAULT)

    output_root = Path(args.output_dir)
    kpi_dir = output_root / args.kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_path = kpi_dir / "signals_raw.csv"
    signals_df.to_csv(signals_path, index=False)

    thresholds_str = ", ".join(f"{q}Q>={QUARTER_THRESHOLDS_DEFAULT[q]:.0%}" for q in quarters)
    print(f"シグナル生成完了: {len(signals_df)}件（対象四半期={quarters}・閾値={thresholds_str}）")
    print(
        f"開示レコード総数={diag['total_disclosure_records']} "
        f"(REIT除外={diag['reit_excluded']}, 対象外DocType={diag['non_target_quarter_statement_records']}, "
        f"四半期対象外={diag['quarter_not_in_scope']})"
    )
    for q in quarters:
        print(
            f"  {q}Q: イベント={diag['events_by_quarter'][q]} / "
            f"進捗率成立={diag['progress_ratio_established_by_quarter'][q]} / "
            f"シグナル={diag['signals_by_quarter'][q]}"
        )
    print(
        f"フィルタ段階(全四半期合算): OP/FOP欠損・非数値={diag['op_or_fop_missing_nonnumeric']} / "
        f"FOP非正={diag['fop_not_positive']} / シグナル合計={diag['signals_total']}"
    )
    print(f"出力: {signals_path}")

    if args.skip_harness:
        return 0
    if signals_df.empty:
        print("WARN: シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    defer_entry = not args.no_defer_entry
    params = {
        "quarters": list(quarters),
        "quarter_thresholds": {str(q): QUARTER_THRESHOLDS_DEFAULT[q] for q in quarters},
        "reaction_cutoff": "15:00",
        "doc_type_filter": "1QFinancialStatements/2QFinancialStatements/3QFinancialStatements",
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
