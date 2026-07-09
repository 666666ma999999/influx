#!/usr/bin/env python3
"""上方修正発表フラグ シグナル生成器（カタログ§2-E #9・v0リスト#9）。

docs/stock-algo-kpi-catalog.md の §2-E「上方修正発表フラグ」を実装する。
`data/jquants/fins/YYYYMMDD.json.gz`（決算短信・業績予想修正等の開示・別ビルダー取得分）から
DocType=="EarnForecastRevision"（業績予想の修正）のレコードを抽出し、同一Codeの過去の直近開示
レコード（種別不問）が持つ通期営業利益予想(FOP)を「修正前」として修正幅を判定する。

定義（グリッドサーチはしない・1構成のみ。カタログ§0-1「閾値は初期値」に反し固定運用する）:
    - イベント: DocType=="EarnForecastRevision"（REITEarnForecastRevisionは明示的に除外。
      ユニバースのProdCat=011フィルタで実質落ちるが、母集団定義として明示的に除く）
    - 修正前FOP: 同一Codeの過去の直近開示レコード（種別不問・FOPが数値のもの）を、
      (DiscDate, DiscTime)の厳密な過去方向のみで探索する（遡り上限365暦日）。
      FOP前が数値でない/存在しない場合はスキップ。FOP前<=0（赤字予想からの黒字化等）は
      別集計のうえ今回は母集団から除外する（次周の検討材料）。
    - シグナル条件: uprev_pct = FOP新/FOP前 - 1 >= +10%
    - 反応日G: PEAD(kpi_pead_signals.py)と同じ15:00ルールを再利用（Canonical Module）。
      Gの価格条件は付けない（純粋イベント）。シグナル確定日 = G。

反応日判定・DiscTimeパース・fins読み込みは scripts/kpi_pead_signals.py の
reaction_day() / _parse_disc_time_minutes() / load_fins_day() を再利用する（Canonical Module原則・
再実装しない）。フォワードリターン計算・ユニバース判定・ブートストラップCI等は
scripts/kpi_event_study.py の run_event_study() に委譲する。

Usage:
    python3 scripts/kpi_uprev_signals.py --start 2016-11 --end 2022-11
    python3 scripts/kpi_uprev_signals.py --start 2016-11 --end 2022-11 --skip-harness
"""
from __future__ import annotations

import argparse
import datetime
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import kpi_event_study  # noqa: E402  (Canonical Module: run_event_study を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: reaction_day/_parse_disc_time_minutes/
# load_fins_day/IN_SAMPLE_START・END/MONTH_RE を再利用。PEADと同じ15:00反応日ルール・
# 標本分割の凍結値を二重定義しない)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー読み込みを再利用)

KPI_NAME = "uprev_fop10"

UPREV_THRESHOLD_DEFAULT = 0.10
LOOKBACK_DAYS_DEFAULT = 365  # FOP前の探索を遡る暦日数の上限

# finsキャッシュの実データ先頭日（2026-07-06時点で確認済み。過去FOP探索の履歴走査開始点として使う。
# もし将来キャッシュの先頭が変わっていても、存在しない日付は kpi_pead_signals.load_fins_day が
# 明確なFATALで停止するため、誤った値のまま静かに動き続けることはない）。
FINS_HISTORY_START_BD = "20160706"


# --- 数値パース・日付演算 ------------------------------------------------------


def _parse_numeric(raw) -> Optional[float]:
    """finsの数値フィールド（文字列 or 空文字 "" or None）をfloatにパースする。
    空文字・非数値はNone（スキップ対象として呼び出し側で件数集計する）。"""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _days_between(date_a: str, date_b: str) -> int:
    """YYYYMMDD文字列2つの暦日差 (date_b - date_a) を返す。"""
    da = datetime.date(int(date_a[:4]), int(date_a[4:6]), int(date_a[6:8]))
    db = datetime.date(int(date_b[:4]), int(date_b[4:6]), int(date_b[6:8]))
    return (db - da).days


# --- FOP履歴構築（Code別・種別不問・前回FOP探索用） ------------------------------


def build_fop_history(
    hist_start_bd: str,
    hist_end_bd: str,
    all_bdays: list[str],
    field: str = "FOP",
    fiscal_year_key: bool = False,
) -> tuple[dict[str, list[tuple]], dict]:
    """[hist_start_bd, hist_end_bd] 内の全fins開示からCode別の`field`数値履歴を構築する。

    種別（DocType）は問わない（決算短信・業績予想修正のいずれも`field`を持ちうる。企業は
    四半期決算のたびに「現時点の予想」を再掲するため、これらも「前回値」の正当な参照元になる）。
    返り値の各リストは (DiscDate, DiscTime分, 値[, CurFYEn]) を (日付, 時刻) 昇順でソート済み。

    `field`は第18周（カタログ§7-G T1/T2）で汎用化した引数（既定"FOP"は既存呼び出し元
    =uprev_fop10と完全後方互換）。"FDivFY"（配当予想）・"FOP2Q"（中間期予想）でも同じ
    走査ロジックをそのまま再利用する（新規実装なし）。

    `fiscal_year_key`（Codexレビュー⑥指摘・2026-07-09追加。既定False=既存呼び出し元と完全
    後方互換）: Trueの場合、各タプルに対象会計年度末日`CurFYEn`を4番目の要素として追加する。
    第18周T1/T2/T5は別会計年度の予想値を「直前予想」として誤比較していた（例: FDivFYが
    14→70のような年度跨ぎを増額と誤検知）ため、呼び出し側で同一CurFYEnの開示のみを
    「直前予想」候補として絞り込めるようにする。

    Returns:
        (history, stats)。historyはcode -> [(disc_date, disc_time_minutes, value[, cur_fy_en]), ...]。
    """
    history: dict[str, list[tuple]] = defaultdict(list)
    stats = {
        "scanned_days": 0, "total_records": 0, "fop_numeric_records": 0,
        "missing_cur_fy_en": 0,  # fiscal_year_key=True時のみ意味を持つ（CurFYEn欠損レコード数）
    }
    scan_days = [d for d in all_bdays if hist_start_bd <= d <= hist_end_bd]
    stats["scanned_days"] = len(scan_days)
    for d in scan_days:
        records = kpi_pead_signals.load_fins_day(d)
        for rec in records:
            stats["total_records"] += 1
            code = rec.get("Code")
            if not code:
                continue
            fop_val = _parse_numeric(rec.get(field))
            if fop_val is None:
                continue
            stats["fop_numeric_records"] += 1
            disc_time_minutes = kpi_pead_signals._parse_disc_time_minutes(rec.get("DiscTime"))
            # 時刻不明は保守的に0分(当日の最早時刻)扱いにする。同日内の順序判定で
            # 「時刻不明の候補」を誤って「後発」扱いにしない（=誤って将来データ扱いで
            # 却下しない）ための安全側の選択。DiscTime欠損はこのデータセットでは実質0件
            # （kpi_pead_signals.pyの実行ログで確認済み）。
            disc_time_minutes = disc_time_minutes if disc_time_minutes is not None else 0
            if fiscal_year_key:
                cur_fy_en = rec.get("CurFYEn") or ""
                if not cur_fy_en:
                    stats["missing_cur_fy_en"] += 1
                history[code].append((d, disc_time_minutes, fop_val, cur_fy_en))
            else:
                history[code].append((d, disc_time_minutes, fop_val))
    for code in history:
        history[code].sort(key=lambda t: (t[0], t[1]))
    return history, stats


# --- シグナル生成 ---------------------------------------------------------------


def generate_uprev_signals(
    start_bd: str,
    end_bd: str,
    threshold: float = UPREV_THRESHOLD_DEFAULT,
    lookback_days: int = LOOKBACK_DAYS_DEFAULT,
    doc_type: str = "EarnForecastRevision",
    reit_doc_type: str = "REITEarnForecastRevision",
    field: str = "FOP",
    fiscal_year_key: bool = False,
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]（YYYYMMDD営業日）内の`doc_type`開示から上方修正シグナルを生成する。

    `doc_type`/`reit_doc_type`/`field`は第18周（カタログ§7-G T1）で汎用化した引数
    （既定値は既存呼び出し元=uprev_fop10と完全後方互換）。T1（配当予想の増額）は
    `doc_type="DividendForecastRevision", reit_doc_type="REITDividendForecastRevision",
    field="FDivFY"`で呼び出す。

    `fiscal_year_key`（Codexレビュー⑥指摘・2026-07-09追加。既定False=既存呼び出し元
    =uprev_fop10と完全後方互換・過去の確定試行を遡及修正しない）: Trueの場合、「直前予想」の
    探索を同一Code**かつ同一対象会計年度(CurFYEn)**の直近開示に限定する。会計年度が一致する
    prior が存在しない場合はシグナル不成立(unknown扱い)とし、`diag["prior_fy_mismatch_excluded"]`
    に計上する（別年度の値を「修正前」として誤比較しない）。

    Returns:
        (signals_df, diag)。signals_df の列: signal_date, code, disclosed_date, disc_time,
        reaction_rule, doc_type, fop_before, fop_before_date, fop_new, uprev_pct
        （列名は既存のFOP由来のまま=後方互換。fieldが"FDivFY"等でも値の意味が変わるだけで
        列構造は共通）。diag にはフィルタ段階別の件数内訳を含む。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    # 過去値探索のため、履歴走査はfins実データ先頭日（またはstart_bdの方が後ならstart_bd）
    # からend_bdまでを対象にする（=query開始より前の期間も遡り候補として含める）。
    hist_start_bd = FINS_HISTORY_START_BD if FINS_HISTORY_START_BD <= start_bd else start_bd
    history, hist_stats = build_fop_history(
        hist_start_bd, end_bd, all_bdays, field=field, fiscal_year_key=fiscal_year_key
    )

    event_days = [d for d in all_bdays if start_bd <= d <= end_bd]

    diag = {
        "history_scanned_days": hist_stats["scanned_days"],
        "history_total_records": hist_stats["total_records"],
        "history_fop_numeric_records": hist_stats["fop_numeric_records"],
        "event_days_scanned": len(event_days),
        "total_disclosure_records": 0,
        "reit_earn_forecast_revision_excluded": 0,
        "revision_events": 0,  # DocType=="EarnForecastRevision"の件数
        "fop_new_missing_or_nonnumeric": 0,
        "no_prior_fop_found": 0,
        "current_cur_fy_en_missing": 0,  # fiscal_year_key=True時のみ: 当該イベント自体のCurFYEn欠損
        "prior_fy_mismatch_excluded": 0,  # fiscal_year_key=True時のみ: 同一年度priorなしで不成立
        "prior_fop_beyond_lookback": 0,
        "fop_pair_established": 0,  # 修正前後のFOPペアが成立した件数（遡り上限内・同一年度確認済み）
        "deficit_to_profit_excluded": 0,  # FOP前<=0（赤字予想からの黒字化等）で今回除外
        "signals_uprev10": 0,  # 閾値以上のシグナル成立件数
    }

    rows: list[dict] = []

    for d in event_days:
        records = kpi_pead_signals.load_fins_day(d)
        for rec in records:
            diag["total_disclosure_records"] += 1
            code = rec.get("Code")
            rec_doc_type = rec.get("DocType", "") or ""
            if not code:
                continue
            if rec_doc_type == reit_doc_type:
                diag["reit_earn_forecast_revision_excluded"] += 1
                continue
            if rec_doc_type != doc_type:
                continue
            diag["revision_events"] += 1

            fop_new = _parse_numeric(rec.get(field))
            if fop_new is None:
                diag["fop_new_missing_or_nonnumeric"] += 1
                continue

            disc_time_raw = rec.get("DiscTime")
            disc_time_minutes = kpi_pead_signals._parse_disc_time_minutes(disc_time_raw)
            cur_key = (d, disc_time_minutes if disc_time_minutes is not None else 0)

            # 過去の直近開示レコード（種別不問）を探索。cur_keyより厳密に過去のもののみ
            # （look-ahead回避。同日複数開示の順序はDiscTime分で比較する）。
            prior_records = [t for t in history.get(code, []) if (t[0], t[1]) < cur_key]
            if not prior_records:
                diag["no_prior_fop_found"] += 1
                continue
            if fiscal_year_key:
                cur_fy_en = rec.get("CurFYEn") or ""
                if not cur_fy_en:
                    diag["current_cur_fy_en_missing"] += 1
                    continue
                same_fy_prior = [t for t in prior_records if len(t) > 3 and t[3] == cur_fy_en]
                if not same_fy_prior:
                    diag["prior_fy_mismatch_excluded"] += 1
                    continue
                prior_records = same_fy_prior
            prev_date, prev_time, fop_before = max(prior_records, key=lambda t: (t[0], t[1]))[:3]
            # look-ahead防止の明示的ガード（§検証2）: 採用した「前回」は必ずcur_keyより過去でなければならない。
            assert (prev_date, prev_time) < cur_key, (
                f"FATAL: look-ahead検出 code={code} event={cur_key} "
                f"prev=({prev_date},{prev_time})"
            )

            if _days_between(prev_date, d) > lookback_days:
                diag["prior_fop_beyond_lookback"] += 1
                continue

            diag["fop_pair_established"] += 1

            if fop_before <= 0:
                diag["deficit_to_profit_excluded"] += 1
                continue  # 赤字予想からの黒字化等・今回は母集団から除外（次周の検討材料）

            uprev_pct = fop_new / fop_before - 1
            if uprev_pct < threshold:
                continue

            g_date, rule = kpi_pead_signals.reaction_day(d, disc_time_minutes, bday_index, all_bdays)
            if g_date is None:
                continue  # 検証期間の終端でカレンダー範囲外

            diag["signals_uprev10"] += 1
            rows.append(
                {
                    "signal_date": g_date,
                    "code": code,
                    "disclosed_date": d,
                    "disc_time": disc_time_raw,
                    "reaction_rule": rule,
                    "doc_type": rec_doc_type,
                    "fop_before": fop_before,
                    "fop_before_date": prev_date,
                    "fop_new": fop_new,
                    "uprev_pct": uprev_pct,
                }
            )

    return pd.DataFrame(rows), diag


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="上方修正発表フラグ シグナル生成器（カタログ§2-E #9）+ KPI検証ハーネス実行"
    )
    parser.add_argument(
        "--start", default=kpi_pead_signals.IN_SAMPLE_START,
        help=f"検索開始月 YYYY-MM（既定 {kpi_pead_signals.IN_SAMPLE_START}）",
    )
    parser.add_argument(
        "--end", default=kpi_pead_signals.IN_SAMPLE_END,
        help=f"検索終了月 YYYY-MM（既定 {kpi_pead_signals.IN_SAMPLE_END}）",
    )
    parser.add_argument("--uprev-threshold", type=float, default=UPREV_THRESHOLD_DEFAULT)
    parser.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS_DEFAULT)
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

    signals_df, diag = generate_uprev_signals(start_bd, end_bd, args.uprev_threshold, args.lookback_days)

    output_root = Path(args.output_dir)
    kpi_dir = output_root / args.kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_path = kpi_dir / "signals_raw.csv"
    signals_df.to_csv(signals_path, index=False)

    print(f"シグナル生成完了: {len(signals_df)}件（uprev_threshold=+{args.uprev_threshold:.0%}以上）")
    print(
        f"開示レコード総数={diag['total_disclosure_records']} "
        f"(REITEarnForecastRevision除外={diag['reit_earn_forecast_revision_excluded']}, "
        f"EarnForecastRevisionイベント={diag['revision_events']})"
    )
    print(
        f"フィルタ段階: FOP新値なし/非数値={diag['fop_new_missing_or_nonnumeric']} / "
        f"前回FOPなし={diag['no_prior_fop_found']} / 遡り{args.lookback_days}日超で除外="
        f"{diag['prior_fop_beyond_lookback']} / FOPペア成立={diag['fop_pair_established']} / "
        f"黒字化転換等(FOP前<=0)で除外={diag['deficit_to_profit_excluded']} / "
        f"+{args.uprev_threshold:.0%}以上シグナル={diag['signals_uprev10']}"
    )
    print(
        f"FOP履歴走査: {diag['history_scanned_days']}日分 "
        f"(総レコード={diag['history_total_records']}, FOP数値あり={diag['history_fop_numeric_records']})"
    )
    print(f"出力: {signals_path}")

    if args.skip_harness:
        return 0
    if signals_df.empty:
        print("WARN: シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    params = {
        "uprev_threshold": args.uprev_threshold,
        "lookback_days": args.lookback_days,
        "reaction_cutoff": "15:00",
        "doc_type_filter": "EarnForecastRevision",
        "defer_entry": args.defer_entry,
    }
    result = kpi_event_study.run_event_study(
        signals_df=signals_df[["signal_date", "code"]],
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
