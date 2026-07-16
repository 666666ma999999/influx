#!/usr/bin/env python3
"""第35周: fins未使用フィールド三本バッチ — ガイダンス・キャッシュフロー（カタログ§7-X・T1〜T3）。

docs/stock-algo-kpi-catalog.md §7-X の事前登録定義（凍結・Codexレビュー㊾GO）を実装する。
3試行とも in-sample期間（2016-11〜2022-11・holdout 2023年以降は使わない）で実行し、共通の
探索的一次結論ルール（第18周基準）を適用する。実装の流儀は scripts/kpi_round33_signals.py の
run_trial 標準フロー・prefilter_in_universe を踏襲し、前年同期照合は scripts/kpi_round26_signals.py
の§7-O様式（期末365±8日・累計期間長差≤7日・Scope×Standard一致）の as-of 実装を踏襲する。

Canonical Module再利用（新規ロジックはT1/T2/T3の生成関数のみ）:
- T1 `guidance_fy_strong`: FY開示（DocType `^FYFinancialStatements`・同日複数は(DiscDate,DiscTime,
  DiscNo)最新）で NxFSales（来期通期売上予想）と Sales（当期通期実績）がともに>0 かつ
  NxFSales/Sales−1 ≥ +10%。変則決算ガード（凍結）: CurFYSt/CurFYEn と NxtFYSt/NxtFYEn の双方が
  365±8日の期間長 ∩ 1 ≤ NxtFYSt−CurFYEn ≤ 8暦日。欠損・変則期は非シグナル。「初出し」判定はしない。
  レコード自身のフィールドのみ使用（as-of履歴不要）。
- T2 `cfo_margin_improve`: `^(2Q|FY)FinancialStatements` 開示でレコード自身の CFO/Sales（累計）が
  前年同期比 +3pt以上改善 ∩ 増収 ∩ 当期・前年ともSales>0 ∩ CFO有効。前年同期照合は§7-O様式
  （開示日ベースの365日上限は適用しない）。1Q/3Qや別開示からのforward-fill禁止。
- T3 `cfo_turnaround`: CFO(当期)/Sales(当期) ≥ +2% ∩ CFO(前年同期)/Sales(前年同期) ≤ −2%。
  前年同期照合・レコード自身限定・Sales>0 は T2 と同一規約。T2 と同一の CFO ペア母集団から
  閾値で分岐する（同一in-sample期間・関連2試行）。
- ユニバース事前フィルタ: kpi_round23_signals.prefilter_in_universe（統計結果不変・第20周§7-I前例）。
- フォワードリターン・重複除去・集計・§6判定・レポート・台帳: kpi_event_study の
  compute_signal_returns/compute_stats/judge/bootstrap_ev_ci/write_report_md/append_trial を再利用。
- 前年同期照合の純ユーティリティ（_disc_key/_span_days/_asof_latest/_yoy_fy_en）は
  kpi_round26_signals から再利用。
- beat系Jaccard診断: sue_beat=kpi_event_batch_signals.generate_sue_beat_signals /
  sales_beat=kpi_round33_signals.generate_sales_beat_signals を再利用してイベントキー突合。
- 探索的一次結論ルール: kpi_event_batch_signals.classify_exploratory を再利用。

3試行とも defer_entry=True（§6手順6の第5周以降既定方針＝S高で買えない日は翌日繰り延べ）。

家族の割引解釈（§7-X凍結・Codex㊾M対応）: fins_unused_family = {T1, T2, T3} で固定。
sales_beat成功後の適応的フィールド採掘であり、同一in-sample期間での fins 追加フィールド試行を
以後禁止。結果は前向き優先順位付けに限定。T2/T3 は同一のCFO系（2本の関連試行）。

Usage:
    python3 scripts/kpi_round35_signals.py --trial all
    python3 scripts/kpi_round35_signals.py --trial t1
    python3 scripts/kpi_round35_signals.py --trial all --start 2017-01 --end 2017-12 --no-trials-append
"""
from __future__ import annotations

import argparse
import re
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import kpi_event_batch_signals  # noqa: E402  (Canonical Module: classify_exploratory/
# generate_sue_beat_signals を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: compute_signal_returns/compute_stats/judge/
# write_report_md/append_trial/bootstrap_ev_ci を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・MONTH_RE・
# load_fins_day・_parse_disc_time_minutes・reaction_day を再利用)
import kpi_round23_signals  # noqa: E402  (Canonical Module: prefilter_in_universe を再利用)
import kpi_round26_signals as r26  # noqa: E402  (Canonical Module: 前年同期照合の純ユーティリティ
# _disc_key/_span_days/_asof_latest/_yoy_fy_en を再利用)
import kpi_round33_signals as r33  # noqa: E402  (Canonical Module: generate_sales_beat_signals を
# beat系Jaccard診断で再利用)
import kpi_uprev_signals  # noqa: E402  (Canonical Module: _parse_numeric/_days_between/
# FINS_HISTORY_START_BD を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・master・regime 読込を再利用)

PERIOD = (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_ROOT = Path("output/kpi")

# --- 事前登録パラメータ（カタログ§7-X・事後変更禁止） -----------------------------

# 前年同期照合の共通許容幅（§7-O様式・round26 と同一凍結値）。
MAX_SPAN_DIFF_DAYS = 7   # 累計期間長（CurPerEn−CurPerSt）の当年/前年の許容差（暦日）
MAX_YOY_END_GAP_DAYS = 8  # 前年同期の期末(CurPerEn)が当年の365±8日前であること

# T1: guidance_fy_strong（FY開示の来期売上ガイダンス強気）。
T1_KPI_NAME = "guidance_fy_strong"
T1_MIN_GROWTH = 0.10           # NxFSales/Sales − 1 ≥ +10%（単一探索値・凍結）
T1_FY_SPAN_TARGET = 365        # CurFY/NxtFY の期間長中心（±8日）
T1_FY_SPAN_TOL = 8             # 期間長許容差（§7-Oと同一の許容幅）
T1_NXT_GAP_MIN = 1             # 1 ≤ NxtFYSt − CurFYEn
T1_NXT_GAP_MAX = 8             #        NxtFYSt − CurFYEn ≤ 8暦日
T1_DOCTYPE_RE = re.compile(r"^FYFinancialStatements")

# T2/T3: CFOマージン系（record-self・前年同期照合）。
T2_KPI_NAME = "cfo_margin_improve"
T2_MARGIN_IMPROVE = 0.03       # CFO/Sales の前年同期比 +3pt以上改善（凍結）
T3_KPI_NAME = "cfo_turnaround"
T3_CUR_MIN = 0.02              # CFO(当期)/Sales(当期) ≥ +2%（凍結）
T3_YOY_MAX = -0.02             # CFO(前年同期)/Sales(前年同期) ≤ −2%（凍結）
# DocType `^(2Q|FY)FinancialStatements_<Scope>_<Standard>`。scope_key=Scope×Standard 両方一致。
CFO_DOCTYPE_RE = re.compile(r"^(2Q|FY)FinancialStatements_([A-Za-z]+)_([A-Za-z]+)")

ROUND_TAG = "35_fins_unused_family"

MULTI_TRIAL_NOTE = (
    "本ラウンドはT1〜T3の3試行同時登録であり累積試行数割引の対象。"
    "この結果単独で運用変更しない。"
)

FINS_UNUSED_FAMILY_NOTE = (
    "fins_unused_family={guidance_fy_strong,cfo_margin_improve,cfo_turnaround}の3本で固定。"
    "sales_beat成功後の適応的フィールド採掘であり、同一in-sample期間でのfins追加フィールド試行を"
    "以後禁止。本家族の結果は統計的確認ではなく前向き候補の優先順位付けに限定（家族単位の割引解釈）。"
    "T2/T3は同一のCFO系(2本の関連試行)。T2はmargin_expand_yoy(実績マージン改善・lift1.79/EV+0.42%)"
    "のCF版にあたる近傍性を宣言。"
)

DEFER_RATIONALE = (
    "§7-X各試行はエントリー=T+1寄付。§6手順6『S高で買えない日は翌日繰り延べ(第5周以降の既定方針)』"
    "に従いdefer_entry=True。ガイダンス強気・CFマージン改善いずれもT+1寄付がS高張り付きで買えない"
    "ことがあり得るため通常の繰延で扱う。"
)


# --- T1: guidance_fy_strong シグナル生成（新規ロジック・レコード自身のみ） -----------


def generate_guidance_fy_strong_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict,
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd] のFY決算短信から来期売上ガイダンス強気シグナルを抽出する（§7-X T1）。

    条件（凍結）: NxFSales(来期通期売上予想)/Sales(当期通期実績) − 1 ≥ +10% ∩ 両者>0 ∩
    変則決算ガード（CurFYSt→CurFYEn と NxtFYSt→NxtFYEn の双方が365±8日の期間長
    ∩ 1 ≤ NxtFYSt−CurFYEn ≤ 8暦日）。すべてレコード自身のフィールドのみ使用（as-of不要）。
    同日複数の(Code,CurFYEn)は(DiscDate,DiscTime,DiscNo)最新1件へ集約。
    シグナル日=反応日G（reaction_day）。
    """
    event_days = [d for d in all_bdays if start_bd <= d <= end_bd]

    diag = {
        "event_days_scanned": len(event_days),
        "total_disclosure_records": 0,
        "fy_events": 0,
        "same_day_collapsed": 0,          # 同一(Code,CurFYEn)×同日を最新1件へ集約した破棄数
        "sales_missing_or_nonpos": 0,     # Sales欠損/≤0
        "nxfsales_missing_or_nonpos": 0,  # NxFSales欠損/≤0
        "span_irregular_excluded": 0,     # CurFY/NxtFY期間長が365±8日外（変則決算）
        "gap_irregular_excluded": 0,      # NxtFYSt−CurFYEn が[1,8]外（連続条件違反）
        "growth_below_threshold": 0,      # 増収率が+10%未満
        "signals_total": 0,
    }

    # 同一(Code,CurFYEn)×同日は最新 disc_key の1件へ集約（同日訂正の取りこぼし防止・§7-X T1凍結）。
    by_key_day: dict[tuple, tuple] = {}
    raw_fy_count = 0
    for d in event_days:
        for rec in kpi_pead_signals.load_fins_day(d):
            diag["total_disclosure_records"] += 1
            code = rec.get("Code")
            doc_type = rec.get("DocType", "") or ""
            if not code or not T1_DOCTYPE_RE.match(doc_type):
                continue
            cur_fy_en = rec.get("CurFYEn") or ""
            if not cur_fy_en:
                continue
            diag["fy_events"] += 1
            disc_time_minutes = kpi_pead_signals._parse_disc_time_minutes(rec.get("DiscTime"))
            disc_time_minutes = disc_time_minutes if disc_time_minutes is not None else 0
            disc_no = str(rec.get("DiscNo") or "")
            dk = r26._disc_key(d, disc_time_minutes, disc_no)
            raw_fy_count += 1
            collapse_key = (code, cur_fy_en, d)
            existing = by_key_day.get(collapse_key)
            if existing is None or existing[0] < dk:
                by_key_day[collapse_key] = (dk, d, rec)
    diag["same_day_collapsed"] = raw_fy_count - len(by_key_day)

    rows: list[dict] = []
    for dk, d, rec in sorted(by_key_day.values(), key=lambda t: t[0]):
        code = rec.get("Code")
        cur_fy_en = rec.get("CurFYEn") or ""
        sales = kpi_uprev_signals._parse_numeric(rec.get("Sales"))
        nxf_sales = kpi_uprev_signals._parse_numeric(rec.get("NxFSales"))
        if sales is None or sales <= 0:
            diag["sales_missing_or_nonpos"] += 1
            continue
        if nxf_sales is None or nxf_sales <= 0:
            diag["nxfsales_missing_or_nonpos"] += 1
            continue

        cur_fy_st = rec.get("CurFYSt") or ""
        nxt_fy_st = rec.get("NxtFYSt") or ""
        nxt_fy_en = rec.get("NxtFYEn") or ""
        span_cur = r26._span_days(cur_fy_st, cur_fy_en)
        span_nxt = r26._span_days(nxt_fy_st, nxt_fy_en)
        if (
            span_cur is None or span_nxt is None
            or abs(span_cur - T1_FY_SPAN_TARGET) > T1_FY_SPAN_TOL
            or abs(span_nxt - T1_FY_SPAN_TARGET) > T1_FY_SPAN_TOL
        ):
            diag["span_irregular_excluded"] += 1
            continue
        # 方向付きの連続条件 1 ≤ NxtFYSt − CurFYEn ≤ 8暦日
        gap = r26._span_days(cur_fy_en, nxt_fy_st)  # = _days_between(CurFYEn, NxtFYSt)
        if gap is None or not (T1_NXT_GAP_MIN <= gap <= T1_NXT_GAP_MAX):
            diag["gap_irregular_excluded"] += 1
            continue

        growth = nxf_sales / sales - 1.0
        if growth < T1_MIN_GROWTH:
            diag["growth_below_threshold"] += 1
            continue

        disc_time_minutes = kpi_pead_signals._parse_disc_time_minutes(rec.get("DiscTime"))
        g_date, rule = kpi_pead_signals.reaction_day(d, disc_time_minutes, bday_index, all_bdays)
        if g_date is None:
            continue  # 検証期間終端でカレンダー範囲外

        diag["signals_total"] += 1
        rows.append(
            {
                "signal_date": g_date,
                "code": code,
                "disclosed_date": d,
                "disc_time": rec.get("DiscTime"),
                "reaction_rule": rule,
                "period_type": "FY",
                "cur_fy_en": cur_fy_en,
                "sales_actual": sales,
                "nxf_sales_forecast": nxf_sales,
                "growth_pct": growth,
                "cur_fy_span_days": span_cur,
                "nxt_fy_span_days": span_nxt,
                "nxt_gap_days": gap,
            }
        )

    return pd.DataFrame(rows), diag


# --- T2/T3 共通: CFO履歴（record-self・前年同期照合用ストア） ------------------------


def build_cfo_history(hist_start_bd: str, end_bd: str) -> tuple[dict, dict, dict]:
    """[hist_start_bd, end_bd] の全2Q/FY開示から、前年同期as-of照合に必要なインデックスを作る。

    Returns:
        (store, discdate_index, diag)
        store: dict[(code, qtype, cur_fy_en, scope_key)] -> list of dict
               各 dict = {disc_key, disc_date, cfo, sales, per_st, per_en}（disc_key 昇順ソート済み）。
               cfo/sales は CFO/Sales を _parse_numeric（欠損は None のまま格納・参照側で不成立扱い）。
        discdate_index: dict[(code, qtype, disc_date)] -> cur_fy_en（beat系Jaccard診断の逆引き用・
               同キー複数は最新 disc_key）。
        diag: 走査件数の内訳。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    scan_days = [d for d in all_bdays if hist_start_bd <= d <= end_bd]

    store: dict[tuple, list[dict]] = defaultdict(list)
    discdate_index: dict[tuple, tuple] = {}  # (code,qtype,disc_date) -> (fy_en, disc_key)
    diag = {
        "history_days_scanned": len(scan_days),
        "history_total_records": 0,
        "history_fs_records": 0,       # DocType が CFO_DOCTYPE_RE に一致した件数（2Q/FY）
        "history_cur_fy_en_missing": 0,
    }

    for d in scan_days:
        for rec in kpi_pead_signals.load_fins_day(d):
            diag["history_total_records"] += 1
            code = rec.get("Code")
            doc_type = rec.get("DocType", "") or ""
            m = CFO_DOCTYPE_RE.match(doc_type)
            if not code or not m:
                continue
            diag["history_fs_records"] += 1
            qtype = m.group(1)
            scope_key = f"{m.group(2)}_{m.group(3)}"
            cur_fy_en = rec.get("CurFYEn") or ""
            if not cur_fy_en:
                diag["history_cur_fy_en_missing"] += 1
                continue
            disc_time_minutes = kpi_pead_signals._parse_disc_time_minutes(rec.get("DiscTime"))
            disc_time_minutes = disc_time_minutes if disc_time_minutes is not None else 0
            disc_no = str(rec.get("DiscNo") or "")
            dk = r26._disc_key(d, disc_time_minutes, disc_no)
            store[(code, qtype, cur_fy_en, scope_key)].append(
                {
                    "disc_key": dk,
                    "disc_date": d,
                    "cfo": kpi_uprev_signals._parse_numeric(rec.get("CFO")),
                    "sales": kpi_uprev_signals._parse_numeric(rec.get("Sales")),
                    "per_st": rec.get("CurPerSt") or "",
                    "per_en": rec.get("CurPerEn") or "",
                }
            )
            idx_key = (code, qtype, d)
            prev = discdate_index.get(idx_key)
            if prev is None or prev[1] < dk:
                discdate_index[idx_key] = (cur_fy_en, dk)

    for key in store:
        store[key].sort(key=lambda r: r["disc_key"])
    discdate_index_out = {k: v[0] for k, v in discdate_index.items()}
    return store, discdate_index_out, diag


def generate_cfo_pairs(
    start_bd: str,
    end_bd: str,
    hist_start_bd: str,
    all_bdays: list[str],
    bday_index: dict,
    prebuilt: Optional[tuple] = None,
) -> tuple[pd.DataFrame, dict, dict, dict]:
    """[start_bd, end_bd] の2Q/FY開示から (当期CFOマージン, 前年同期CFOマージン) のペア母集団を作る。

    閾値は課さない（T2/T3 が同一母集団を各自の閾値で絞る）。当期はレコード自身の CFO/Sales（累計）を
    使い、前年同期は§7-O様式（同一period_type・CurFYEn年-1・CurPerEn当年365±8日前・累計期間長差≤7日・
    Scope×Standard一致）の as-of レコードの CFO/Sales をそのまま使う（forward-fill・単Q差分は行わない）。
    両期 Sales>0 ∩ 両期 CFO有効 を満たすペアのみ行にする。開示日ベースの365日上限は適用しない。
    同一(Code,CurFYEn,qtype)×同日は最新 disc_key の1件へ集約。

    Returns:
        (pairs_df, diag, store, discdate_index)。pairs_df 列: signal_date, code, disclosed_date,
        disc_time, period_type, cur_fy_en, scope_key, cfo_cur, sales_cur, margin_cur,
        cfo_yoy, sales_yoy, margin_yoy, margin_delta_pt, sales_increase。
    """
    if prebuilt is not None:
        store, discdate_index, hist_diag = prebuilt
    else:
        store, discdate_index, hist_diag = build_cfo_history(hist_start_bd, end_bd)

    event_days = [d for d in all_bdays if start_bd <= d <= end_bd]

    diag = {
        **hist_diag,
        "event_days_scanned": len(event_days),
        "same_day_collapsed": 0,
        "events_by_qtype": {q: 0 for q in ("2Q", "FY")},
        "cur_cfo_or_sales_missing": 0,      # 当期のCFOまたはSales欠損
        "yoy_fy_en_unparsable": 0,
        "yoy_current_not_found": 0,         # 前年同期の同一period_type as-of不在
        "yoy_cfo_or_sales_missing": 0,      # 前年同期のCFOまたはSales欠損
        "irregular_period_excluded": 0,     # 累計期間長差>7日（変則決算/変則四半期）
        "fiscal_change_excluded": 0,        # 前年同期の期末が約1年前でない（決算期変更/移行期）
        "current_sales_nonpos": 0,
        "yoy_sales_nonpos": 0,
        "pairs_total": 0,
    }

    # 同一(Code,CurFYEn,qtype)×同日は最新 disc_key の1件へ集約（round26 §7-O 様式）。
    by_key_day: dict[tuple, tuple] = {}
    raw_event_count = 0
    for d in event_days:
        for rec in kpi_pead_signals.load_fins_day(d):
            doc_type = rec.get("DocType", "") or ""
            m = CFO_DOCTYPE_RE.match(doc_type)
            code = rec.get("Code")
            if not code or not m:
                continue
            cur_fy_en = rec.get("CurFYEn") or ""
            if not cur_fy_en:
                continue
            qtype = m.group(1)
            disc_time_minutes = kpi_pead_signals._parse_disc_time_minutes(rec.get("DiscTime"))
            disc_time_minutes = disc_time_minutes if disc_time_minutes is not None else 0
            disc_no = str(rec.get("DiscNo") or "")
            dk = r26._disc_key(d, disc_time_minutes, disc_no)
            raw_event_count += 1
            collapse_key = (code, cur_fy_en, qtype, d)
            existing = by_key_day.get(collapse_key)
            if existing is None or existing[0] < dk:
                by_key_day[collapse_key] = (dk, d, rec, m)
    events = sorted(by_key_day.values(), key=lambda t: t[0])
    diag["same_day_collapsed"] = raw_event_count - len(events)

    rows: list[dict] = []
    for dk, d, rec, m in events:
        code = rec.get("Code")
        qtype = m.group(1)
        scope_key = f"{m.group(2)}_{m.group(3)}"
        cur_fy_en = rec.get("CurFYEn") or ""
        diag["events_by_qtype"][qtype] += 1

        cfo_cur = kpi_uprev_signals._parse_numeric(rec.get("CFO"))
        sales_cur = kpi_uprev_signals._parse_numeric(rec.get("Sales"))
        per_st_cur = rec.get("CurPerSt") or ""
        per_en_cur = rec.get("CurPerEn") or ""
        if cfo_cur is None or sales_cur is None:
            diag["cur_cfo_or_sales_missing"] += 1
            continue

        yoy_fy_en = r26._yoy_fy_en(cur_fy_en)
        if yoy_fy_en is None:
            diag["yoy_fy_en_unparsable"] += 1
            continue
        yoy_rec = r26._asof_latest(store, (code, qtype, yoy_fy_en, scope_key), dk)
        if yoy_rec is None:
            diag["yoy_current_not_found"] += 1
            continue
        # 累計期間長同等（±7日）
        span_cur = r26._span_days(per_st_cur, per_en_cur)
        span_yoy = r26._span_days(yoy_rec["per_st"], yoy_rec["per_en"])
        if span_cur is None or span_yoy is None or abs(span_cur - span_yoy) > MAX_SPAN_DIFF_DAYS:
            diag["irregular_period_excluded"] += 1
            continue
        # 前年同期の期末が当年の365±8日前（決算期変更/移行期除外）
        end_gap = r26._span_days(yoy_rec["per_en"], per_en_cur)
        if end_gap is None or abs(end_gap - 365) > MAX_YOY_END_GAP_DAYS:
            diag["fiscal_change_excluded"] += 1
            continue

        cfo_yoy = yoy_rec["cfo"]
        sales_yoy = yoy_rec["sales"]
        if cfo_yoy is None or sales_yoy is None:
            diag["yoy_cfo_or_sales_missing"] += 1
            continue
        if sales_cur <= 0:
            diag["current_sales_nonpos"] += 1
            continue
        if sales_yoy <= 0:
            diag["yoy_sales_nonpos"] += 1
            continue

        margin_cur = cfo_cur / sales_cur
        margin_yoy = cfo_yoy / sales_yoy

        g_date, rule = kpi_pead_signals.reaction_day(
            d, kpi_pead_signals._parse_disc_time_minutes(rec.get("DiscTime")), bday_index, all_bdays
        )
        if g_date is None:
            continue

        diag["pairs_total"] += 1
        rows.append(
            {
                "signal_date": g_date,
                "code": code,
                "disclosed_date": d,
                "disc_time": rec.get("DiscTime"),
                "reaction_rule": rule,
                "period_type": qtype,
                "cur_fy_en": cur_fy_en,
                "scope_key": scope_key,
                "cfo_cur": cfo_cur,
                "sales_cur": sales_cur,
                "margin_cur": margin_cur,
                "cfo_yoy": cfo_yoy,
                "sales_yoy": sales_yoy,
                "margin_yoy": margin_yoy,
                "margin_delta_pt": (margin_cur - margin_yoy) * 100.0,
                "sales_increase": sales_cur > sales_yoy,
            }
        )

    return pd.DataFrame(rows), diag, store, discdate_index


def filter_cfo_margin_improve(pairs_df: pd.DataFrame) -> pd.DataFrame:
    """T2: CFOマージン前年同期比 +3pt以上改善 ∩ 増収。"""
    if pairs_df.empty:
        return pairs_df
    mask = (pairs_df["margin_delta_pt"] >= T2_MARGIN_IMPROVE * 100.0) & pairs_df["sales_increase"]
    return pairs_df[mask].reset_index(drop=True)


def filter_cfo_turnaround(pairs_df: pd.DataFrame) -> pd.DataFrame:
    """T3: CFO(当期)/Sales ≥ +2% ∩ CFO(前年同期)/Sales ≤ −2%（負→正転換）。"""
    if pairs_df.empty:
        return pairs_df
    mask = (pairs_df["margin_cur"] >= T3_CUR_MIN) & (pairs_df["margin_yoy"] <= T3_YOY_MAX)
    return pairs_df[mask].reset_index(drop=True)


# --- beat系Jaccard診断（診断限定・合否判定には使わない） ----------------------------


def build_beat_key_sets(start_bd: str, end_bd: str) -> dict:
    """sue_beat / sales_beat のイベントキー集合 (code, cur_fy_en, period_type) を1回だけ構築する。

    sue_beat 出力行は CurFYEn を持たないため、sales_beat 生成が返す discdate_index
    ((code, period_type, disclosed_date) -> cur_fy_en) で逆引きしてキー化する。
    """
    sales_df, _sales_diag, discdate_index = r33.generate_sales_beat_signals(start_bd, end_bd)
    sue_df, sue_diag = kpi_event_batch_signals.generate_sue_beat_signals(start_bd, end_bd)

    sales_keys: set = set()
    sales_by_q: dict[str, set] = defaultdict(set)
    for _, r in sales_df.iterrows():
        key = (r["code"], r["cur_fy_en"], r["period_type"])
        sales_keys.add(key)
        sales_by_q[r["period_type"]].add(key)

    sue_keys: set = set()
    sue_by_q: dict[str, set] = defaultdict(set)
    sue_unmapped = 0
    for _, r in sue_df.iterrows():
        fy_en = discdate_index.get((r["code"], r["period_type"], r["disclosed_date"]))
        if fy_en is None:
            sue_unmapped += 1
            continue
        key = (r["code"], fy_en, r["period_type"])
        sue_keys.add(key)
        sue_by_q[r["period_type"]].add(key)

    return {
        "sales_keys": sales_keys, "sales_by_q": sales_by_q, "sales_signal_count": int(len(sales_df)),
        "sue_keys": sue_keys, "sue_by_q": sue_by_q, "sue_signal_count": int(len(sue_df)),
        "sue_unmapped_fy_en": sue_unmapped,
        "sue_gen_signals_total": sue_diag.get("signals_sue_beat"),
    }


def _jaccard(a: set, b: set) -> Optional[float]:
    u = a | b
    return (len(a & b) / len(u)) if u else None


def compute_beat_jaccard(my_keys: set, my_by_q: dict, beat_sets: dict) -> dict:
    """自試行のイベントキー集合と sue_beat/sales_beat の Jaccard（全体+period_type別）。"""
    out = {"my_unique_keys": len(my_keys)}
    for name, keys_field, byq_field in (
        ("sue_beat", "sue_keys", "sue_by_q"),
        ("sales_beat", "sales_keys", "sales_by_q"),
    ):
        bk = beat_sets[keys_field]
        bbyq = beat_sets[byq_field]
        by_q = {}
        for q in ("2Q", "FY"):
            mq = my_by_q.get(q, set())
            bq = bbyq.get(q, set())
            by_q[q] = {
                "my_n": len(mq), "beat_n": len(bq),
                "intersection": len(mq & bq), "jaccard": _jaccard(mq, bq),
            }
        out[name] = {
            "beat_signal_count": beat_sets[f"{name.split('_')[0]}_signal_count"],
            "beat_unique_keys": len(bk),
            "intersection": len(my_keys & bk),
            "union": len(my_keys | bk),
            "jaccard": _jaccard(my_keys, bk),
            "by_qtype": by_q,
        }
    out["sue_unmapped_fy_en"] = beat_sets["sue_unmapped_fy_en"]
    return out


# --- 共通: ハーネス実行 + 探索的一次結論 + 台帳記録（round33 run_trial 標準フロー） -----


def run_trial(
    signals_df: pd.DataFrame,
    kpi_name: str,
    base_params: dict,
    harness_ctx: dict,
    report_extra_lines: Optional[list[str]] = None,
    append_to_ledger: bool = True,
) -> dict:
    """低レベルCanonical関数を直接呼び出してハーネス実行〜探索的結論〜台帳記録まで行う
    （kpi_round33_signals.run_trial と同一の標準フロー。全試行 defer_entry=True・n<30格下げなし）。
    """
    all_bdays = harness_ctx["all_bdays"]
    bday_index = harness_ctx["bday_index"]
    regime_by_day = harness_ctx["regime_by_day"]
    base_rate_by_month = harness_ctx["base_rate_by_month"]
    universes_by_month = harness_ctx["universes_by_month"]

    harness_signals_df = signals_df[["signal_date", "code"]].copy()
    returns_df, diag = kpi_event_study.compute_signal_returns(
        harness_signals_df, bday_index, all_bdays, regime_by_day, universes_by_month, defer_entry=True
    )
    diag["regime_filter"] = None
    diag["pre_regime_filter_count"] = len(harness_signals_df)

    in_universe_df = (
        returns_df[returns_df["in_universe"]].reset_index(drop=True) if len(returns_df) else returns_df
    )
    if in_universe_df.empty:
        raise SystemExit(f"FATAL: {kpi_name}のin_universeシグナルが0件です")

    stats = kpi_event_study.compute_stats(in_universe_df, base_rate_by_month, PERIOD)
    verdict, reasons = kpi_event_study.judge(stats)
    defer_stats = kpi_event_study._compute_defer_stats(in_universe_df)

    ev_ci = kpi_event_study.bootstrap_ev_ci(in_universe_df, ev_column="ret")
    conclusion = kpi_event_batch_signals.classify_exploratory(
        stats.get("point_lift"), ev_ci["point_ev"], ev_ci["ci_low"]
    )

    params = {
        **base_params,
        "defer_entry": True,
        "defer_rationale": DEFER_RATIONALE,
        "ev_none_point": ev_ci["point_ev"],
        "ev_none_ci_low": ev_ci["ci_low"],
        "ev_none_ci_high": ev_ci["ci_high"],
        "ev_ci_n_boot_valid": ev_ci["n_boot_valid"],
        "exploratory_conclusion": conclusion,
        "pre_registered_success_rule": (
            "promising: EV(なし)CI下限>0 かつ point_lift>=1.5 / "
            "rejected: EV点推定<=0 または point_lift<1.0 / inconclusive: それ以外"
        ),
        "round": ROUND_TAG,
        "multi_trial_discount_note": MULTI_TRIAL_NOTE,
        "fins_unused_family_note": FINS_UNUSED_FAMILY_NOTE,
        "entry_missing": diag["entry_missing"],
        "raw_signal_count": diag["raw_signal_count"],
        "duplicate_discarded": diag["duplicate_discarded"],
        "out_of_universe": diag["out_of_universe"],
    }

    kpi_dir = OUTPUT_ROOT / kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    returns_path = kpi_dir / "returns.csv"
    report_path = kpi_dir / "report.md"
    returns_df.to_csv(returns_path, index=False)
    kpi_event_study.write_report_md(
        report_path, kpi_name, params, PERIOD, diag, stats, verdict, reasons, in_universe_df,
        defer_stats=defer_stats,
    )
    with open(report_path, "a", encoding="utf-8") as f:
        f.write("\n## 探索的一次結論（第35周・カタログ§7-X事前登録の共通ルール）\n")
        f.write(
            f"- EV(なし・往復コスト0.3%控除込)点推定 = {ev_ci['point_ev']:.4%}"
            f"（月次ブロック・ブートストラップ95%CI=[{ev_ci['ci_low']:.4%}, {ev_ci['ci_high']:.4%}]"
            f"・有効ブートストラップ数={ev_ci['n_boot_valid']}）\n"
            if ev_ci["point_ev"] is not None
            else "- EV(なし)点推定 = 計算不能\n"
        )
        f.write(f"- リフト点推定 = {stats.get('point_lift')}\n")
        f.write(f"- §6/§7-C verdict = {verdict}\n")
        f.write(f"- **探索的一次結論: {conclusion}**\n")
        f.write(f"- {MULTI_TRIAL_NOTE}\n")
        f.write(f"- {FINS_UNUSED_FAMILY_NOTE}\n")
        if report_extra_lines:
            f.write("\n## 近傍事実・必須診断・注記（§7-X）\n")
            for line in report_extra_lines:
                f.write(f"- {line}\n")

    if append_to_ledger:
        trial_record = {
            "run_id": uuid.uuid4().hex,
            "ts": jq_fetch.now_jst().isoformat(),
            "kpi_name": kpi_name,
            "params": params,
            "period": {"start": PERIOD[0], "end": PERIOD[1]},
            "n": stats["n"],
            "lift": stats.get("point_lift"),
            "ci_low": stats.get("ci_low"),
            "ci_high": stats.get("ci_high"),
            "ev": stats.get("ev_stop8"),
            "verdict": verdict,
            "entry_mode": "defer_max3bd",
            "regime_filter": None,
        }
        kpi_event_study.append_trial(trial_record, TRIALS_PATH)
        print(f"{kpi_name}: 台帳へ append 済み ({TRIALS_PATH})")
    else:
        print(f"{kpi_name}: --no-trials-append 指定のため台帳へは記録しませんでした")

    print(
        f"{kpi_name} 完了: n={stats['n']} lift={stats.get('point_lift')} verdict={verdict} "
        f"EV点推定={ev_ci['point_ev']} CI95=[{ev_ci['ci_low']},{ev_ci['ci_high']}] "
        f"探索的一次結論={conclusion}"
    )
    print(f"{kpi_name} report: {report_path}")
    print(f"{kpi_name} returns: {returns_path}")

    return {
        "kpi_name": kpi_name, "n": stats["n"], "lift": stats.get("point_lift"),
        "ci_low": stats.get("ci_low"), "ci_high": stats.get("ci_high"),
        "ev_point": ev_ci["point_ev"], "ev_ci_low": ev_ci["ci_low"], "ev_ci_high": ev_ci["ci_high"],
        "ev_stop8": stats.get("ev_stop8"),
        "verdict": verdict, "conclusion": conclusion, "returns_path": returns_path,
        "avg_monthly_n": stats.get("avg_monthly_n"),
        "entry_missing": diag["entry_missing"], "raw_signal_count": diag["raw_signal_count"],
        "duplicate_discarded": diag["duplicate_discarded"], "out_of_universe": diag["out_of_universe"],
        "in_universe_df": in_universe_df,
    }


# --- 各トライアルのドライバ ---------------------------------------------------------


def _prefilter_and_save(signals_df: pd.DataFrame, kpi_name: str, hc: dict) -> tuple[pd.DataFrame, int, int]:
    """生シグナルを保存し、ユニバース事前フィルタ（統計結果不変）を適用する。"""
    kpi_dir = OUTPUT_ROOT / kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_df.to_csv(kpi_dir / "signals_raw.csv", index=False)
    filtered_df, n_raw, n_filtered = kpi_round23_signals.prefilter_in_universe(
        signals_df, hc["universes_by_month"]
    )
    print(
        f"{kpi_name} ユニバース事前フィルタ: 生{n_raw}件 → ハーネス投入{n_filtered}件"
        f"(除去{n_raw - n_filtered}件=判定に使われないユニバース外・統計結果不変・第20周§7-I前例)",
        file=sys.stderr,
    )
    if filtered_df.empty:
        raise SystemExit(f"FATAL: {kpi_name}の事前フィルタ後シグナルが0件です")
    return filtered_df, n_raw, n_filtered


def _monthly_counts(signals_df: pd.DataFrame) -> dict:
    """signal_date の月別件数（YYYYMM -> 件数・昇順）。"""
    if signals_df.empty:
        return {}
    return (
        signals_df.assign(month=signals_df["signal_date"].str[:6])["month"]
        .value_counts().sort_index().to_dict()
    )


def _event_keys(signals_df: pd.DataFrame) -> tuple[set, dict]:
    """(code, cur_fy_en, period_type) のイベントキー集合と period_type別集合を返す。"""
    keys: set = set()
    by_q: dict[str, set] = defaultdict(set)
    for _, r in signals_df.iterrows():
        key = (r["code"], r["cur_fy_en"], r["period_type"])
        keys.add(key)
        by_q[r["period_type"]].add(key)
    return keys, by_q


def _append_jaccard_report(report_path: Path, jac: dict) -> None:
    with open(report_path, "a", encoding="utf-8") as f:
        f.write("\n## beat系Jaccard診断（診断限定・イベントキー=(Code,CurFYEn,period_type)）\n")
        for name in ("sue_beat", "sales_beat"):
            j = jac[name]
            f.write(
                f"- {name}: Jaccard={j['jaccard']} (積集合={j['intersection']} / 和集合={j['union']} / "
                f"自試行キー={jac['my_unique_keys']} / {name}キー={j['beat_unique_keys']} / "
                f"{name}生シグナル={j['beat_signal_count']})\n"
            )
            for q, ov in j["by_qtype"].items():
                f.write(
                    f"  - period_type={q}: 自試行n={ov['my_n']} {name}n={ov['beat_n']} "
                    f"積集合={ov['intersection']} Jaccard={ov['jaccard']}\n"
                )
        f.write(f"- sueキー逆引き不能={jac['sue_unmapped_fy_en']}\n")


def run_t1(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T1 {T1_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd = generate_guidance_fy_strong_signals(
        shared["start_bd"], shared["end_bd"], hc["all_bdays"], hc["bday_index"]
    )
    print(
        f"T1 シグナル生成完了: {len(signals_df)}件 (FY開示={gd['fy_events']}, 同日集約破棄={gd['same_day_collapsed']}, "
        f"Sales欠損/≤0={gd['sales_missing_or_nonpos']}, NxFSales欠損/≤0={gd['nxfsales_missing_or_nonpos']}, "
        f"期間長変則除外={gd['span_irregular_excluded']}, gap変則除外={gd['gap_irregular_excluded']}, "
        f"増収率<10%={gd['growth_below_threshold']}, シグナル={gd['signals_total']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T1シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T1_KPI_NAME, hc)
    monthly_counts = _monthly_counts(signals_df)

    nxf_missing_rate = (
        gd["nxfsales_missing_or_nonpos"] / gd["fy_events"] if gd["fy_events"] else None
    )
    base_params = {
        "signal_definition": (
            "FY開示でNxFSales/Sales−1≥+10%∩両者>0∩変則決算ガード"
            "(CurFY/NxtFY期間長365±8日∩1≤NxtFYSt−CurFYEn≤8暦日)。レコード自身のフィールドのみ・as-of不要。"
            "同日複数(Code,CurFYEn)は最新1件。シグナル日=反応日G"
        ),
        "min_growth": T1_MIN_GROWTH,
        "fy_span_target_tol": [T1_FY_SPAN_TARGET, T1_FY_SPAN_TOL],
        "nxt_gap_range": [T1_NXT_GAP_MIN, T1_NXT_GAP_MAX],
        "reaction": "シグナル確定=反応日G終値・エントリー=G+1寄付",
        "diag_fy_events": gd["fy_events"],
        "diag_nxfsales_missing_or_nonpos": gd["nxfsales_missing_or_nonpos"],
        "diag_nxfsales_missing_rate": nxf_missing_rate,
        "diag_span_irregular_excluded": gd["span_irregular_excluded"],
        "diag_gap_irregular_excluded": gd["gap_irregular_excluded"],
        "diag_growth_below_threshold": gd["growth_below_threshold"],
        "diag_same_day_collapsed": gd["same_day_collapsed"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "monthly_cluster": monthly_counts,
        "neighbor_note": (
            "ガイダンス系は72試行で未使用の新axis。日本企業の保守的ガイダンス慣行下で+10%超の増収計画の"
            "FY開示時提示は強い経営シグナル。beat系(sue_beat/sales_beat)とのJaccardで重複を明示"
        ),
    }
    report_extra = [
        f"必須診断(T1): FY開示={gd['fy_events']} / NxFSales欠損率={nxf_missing_rate} "
        f"/ 期間長変則除外={gd['span_irregular_excluded']} / gap変則除外={gd['gap_irregular_excluded']} "
        f"/ 増収率<10%={gd['growth_below_threshold']} / 同日集約破棄={gd['same_day_collapsed']} "
        f"/ シグナル={gd['signals_total']}。",
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        f"月別クラスタ分布: {monthly_counts}。",
        base_params["neighbor_note"],
        FINS_UNUSED_FAMILY_NOTE,
    ]
    res = run_trial(
        filtered_df, T1_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
    )
    # beat系Jaccard診断
    my_keys, my_by_q = _event_keys(signals_df)
    jac = compute_beat_jaccard(my_keys, my_by_q, shared["beat_sets"])
    _append_jaccard_report(OUTPUT_ROOT / T1_KPI_NAME / "report.md", jac)
    res["jaccard"] = jac
    res["signals_raw_df"] = signals_df
    res["monthly_counts"] = monthly_counts
    res["event_keys"] = my_keys
    return res


def _run_cfo_trial(
    shared: dict, kpi_name: str, filter_fn, signal_def: str, threshold_note: str, extra_params: dict,
) -> dict:
    hc = shared["harness_ctx"]
    pairs_df = shared["cfo_pairs_df"]
    gd = shared["cfo_pairs_diag"]
    signals_df = filter_fn(pairs_df)
    print(
        f"{kpi_name}: CFOペア母集団={len(pairs_df)}件 → 閾値通過シグナル={len(signals_df)}件",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit(f"FATAL: {kpi_name}シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, kpi_name, hc)
    monthly_counts = _monthly_counts(signals_df)

    base_params = {
        "signal_definition": signal_def,
        "threshold_note": threshold_note,
        "prior_year_matching": (
            "§7-O様式: 同一period_type・CurFYEn年-1(月日一致)・CurPerEn当年365±8日前・"
            f"累計期間長差≤{MAX_SPAN_DIFF_DAYS}日・Scope×Standard一致。開示日ベースの365日上限は不適用。"
            "当期・前年ともレコード自身のCFO/Sales(累計)のみ・forward-fill/単Q差分なし"
        ),
        "excluded_periods": "1Q/3Q(CF計算書は主に2Q/FY添付・§7-Xで2Q/FYに限定)",
        "reaction": "シグナル確定=反応日G終値・エントリー=G+1寄付",
        "diag_cfo_pairs_total": gd["pairs_total"],
        "diag_events_by_qtype": gd["events_by_qtype"],
        "diag_cur_cfo_or_sales_missing": gd["cur_cfo_or_sales_missing"],
        "diag_yoy_current_not_found": gd["yoy_current_not_found"],
        "diag_yoy_cfo_or_sales_missing": gd["yoy_cfo_or_sales_missing"],
        "diag_irregular_period_excluded": gd["irregular_period_excluded"],
        "diag_fiscal_change_excluded": gd["fiscal_change_excluded"],
        "diag_current_sales_nonpos": gd["current_sales_nonpos"],
        "diag_yoy_sales_nonpos": gd["yoy_sales_nonpos"],
        "diag_same_day_collapsed": gd["same_day_collapsed"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "monthly_cluster": monthly_counts,
        **extra_params,
    }
    report_extra = [
        f"必須診断({kpi_name}): CFOペア母集団={gd['pairs_total']}(2Q={gd['events_by_qtype'].get('2Q')}"
        f"/FY={gd['events_by_qtype'].get('FY')}) / 当期CFO/Sales欠損={gd['cur_cfo_or_sales_missing']} "
        f"/ 前年同期不在={gd['yoy_current_not_found']} / 前年CFO/Sales欠損={gd['yoy_cfo_or_sales_missing']} "
        f"/ 期間長変則除外={gd['irregular_period_excluded']} / 決算期変更除外={gd['fiscal_change_excluded']} "
        f"/ 当期Sales≤0={gd['current_sales_nonpos']} / 前年Sales≤0={gd['yoy_sales_nonpos']} "
        f"/ 同日集約破棄={gd['same_day_collapsed']} / 閾値通過={len(signals_df)}。",
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        f"月別クラスタ分布: {monthly_counts}。",
        threshold_note,
        FINS_UNUSED_FAMILY_NOTE,
    ]
    res = run_trial(
        filtered_df, kpi_name, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
    )
    my_keys, my_by_q = _event_keys(signals_df)
    jac = compute_beat_jaccard(my_keys, my_by_q, shared["beat_sets"])
    _append_jaccard_report(OUTPUT_ROOT / kpi_name / "report.md", jac)
    res["jaccard"] = jac
    res["signals_raw_df"] = signals_df
    res["monthly_counts"] = monthly_counts
    res["event_keys"] = my_keys
    return res


def run_t2(shared: dict) -> dict:
    return _run_cfo_trial(
        shared, T2_KPI_NAME, filter_cfo_margin_improve,
        signal_def=(
            "2Q/FY開示でレコード自身のCFO/Sales(累計)が前年同期比+3pt以上改善∩増収∩当期・前年ともSales>0"
            "∩CFO有効。前年同期照合は§7-O様式(開示日365日上限は不適用)"
        ),
        threshold_note=(
            f"閾値=CFOマージン(CFO/Sales)前年同期比 ≥ +{T2_MARGIN_IMPROVE * 100:.0f}pt改善 ∩ 増収(Sales_cur>Sales_yoy)。"
            "利益より粉飾しにくいキャッシュ創出力の改善=質の高いマージン改善という仮説。"
            "margin_expand_yoy(実績マージン改善・lift1.79/EV+0.42%)のCF版"
        ),
        extra_params={"margin_improve_pt": T2_MARGIN_IMPROVE},
    )


def run_t3(shared: dict) -> dict:
    return _run_cfo_trial(
        shared, T3_KPI_NAME, filter_cfo_turnaround,
        signal_def=(
            "2Q/FY開示でCFO(当期)/Sales(当期)≥+2% ∩ CFO(前年同期)/Sales(前年同期)≤−2%(負→正転換)。"
            "前年同期照合・レコード自身限定・Sales>0はT2と同一規約"
        ),
        threshold_note=(
            f"閾値=CFOマージン(当期)≥+{T3_CUR_MIN * 100:.0f}% ∩ CFOマージン(前年同期)≤{T3_YOY_MAX * 100:.0f}%。"
            "営業CFの黒字転換=事業の資金創出構造の転換点という仮説。T2と同一CFOペア母集団の別閾値(関連2試行)"
        ),
        extra_params={"cur_min": T3_CUR_MIN, "yoy_max": T3_YOY_MAX},
    )


TRIAL_RUNNERS = {"t1": run_t1, "t2": run_t2, "t3": run_t3}


# --- メイン処理 ---------------------------------------------------------------------


def _event_bd_bounds(start_month: str, end_month: str, all_bdays: list[str]) -> tuple[str, str]:
    start_bound = start_month.replace("-", "") + "01"
    end_bound = end_month.replace("-", "") + "31"
    start_bd = next((d for d in all_bdays if d >= start_bound), None)
    end_bd = next((d for d in reversed(all_bdays) if d <= end_bound), None)
    if start_bd is None or end_bd is None:
        raise SystemExit("FATAL: 指定期間に営業日が見つかりません")
    return start_bd, end_bd


def main() -> int:
    parser = argparse.ArgumentParser(
        description="第35周: fins未使用フィールド三本バッチ（カタログ§7-X・T1〜T3）"
    )
    parser.add_argument("--trial", choices=["t1", "t2", "t3", "all"], default="all")
    parser.add_argument(
        "--start", default=kpi_pead_signals.IN_SAMPLE_START,
        help=f"検索開始月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_START})。smokeテスト用",
    )
    parser.add_argument(
        "--end", default=kpi_pead_signals.IN_SAMPLE_END,
        help=f"検索終了月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_END})。smokeテスト用",
    )
    parser.add_argument(
        "--no-trials-append", action="store_true",
        help="台帳(trials.jsonl)へ記録しない(smokeテスト用・成果物のみ生成)",
    )
    args = parser.parse_args()

    if not kpi_pead_signals.MONTH_RE.match(args.start) or not kpi_pead_signals.MONTH_RE.match(args.end):
        raise SystemExit("FATAL: --start/--end は YYYY-MM 形式で指定してください")
    if args.end > kpi_pead_signals.IN_SAMPLE_END:
        raise SystemExit(
            f"FATAL: --end={args.end} はholdout期間(2023年以降)に抵触します。"
            f"§7-Xはin-sample({kpi_pead_signals.IN_SAMPLE_END}まで)で評価します。holdoutは使用しません。"
        )

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    start_bd, end_bd = _event_bd_bounds(args.start, args.end, all_bdays)

    topix_close = measure_base_rate.load_topix_series()
    regime_by_day = measure_base_rate.build_regime_series(topix_close)
    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    universes_by_month = kpi_event_study.load_universe_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

    trials_to_run = ["t1", "t2", "t3"] if args.trial == "all" else [args.trial]

    shared = {
        "start_bd": start_bd,
        "end_bd": end_bd,
        "append_to_ledger": not args.no_trials_append,
        "harness_ctx": {
            "all_bdays": all_bdays,
            "bday_index": bday_index,
            "regime_by_day": regime_by_day,
            "base_rate_by_month": base_rate_by_month,
            "universes_by_month": universes_by_month,
        },
    }

    # beat系Jaccard診断の基礎キー（全試行で共有・1回だけ生成）。
    print("beat系(sue_beat/sales_beat)イベントキー集合を生成中...", file=sys.stderr)
    shared["beat_sets"] = build_beat_key_sets(start_bd, end_bd)

    # T2/T3 が同一のCFOペア母集団を共有するため、必要時に1回だけ生成する。
    if any(t in trials_to_run for t in ("t2", "t3")):
        hist_start_bd = (
            kpi_uprev_signals.FINS_HISTORY_START_BD
            if kpi_uprev_signals.FINS_HISTORY_START_BD <= start_bd
            else start_bd
        )
        print("CFOペア母集団(前年同期as-of照合)を生成中...", file=sys.stderr)
        pairs_df, pairs_diag, _store, _didx = generate_cfo_pairs(
            start_bd, end_bd, hist_start_bd, all_bdays, bday_index
        )
        shared["cfo_pairs_df"] = pairs_df
        shared["cfo_pairs_diag"] = pairs_diag

    results = []
    for t in trials_to_run:
        results.append(TRIAL_RUNNERS[t](shared))

    # T2×T3 相互Jaccard（両方走った時のみ・診断限定）。
    t2t3_jaccard = None
    res_by_name = {r["kpi_name"]: r for r in results}
    if T2_KPI_NAME in res_by_name and T3_KPI_NAME in res_by_name:
        k2 = res_by_name[T2_KPI_NAME]["event_keys"]
        k3 = res_by_name[T3_KPI_NAME]["event_keys"]
        t2t3_jaccard = {
            "t2_keys": len(k2), "t3_keys": len(k3),
            "intersection": len(k2 & k3), "union": len(k2 | k3), "jaccard": _jaccard(k2, k3),
        }
        for name in (T2_KPI_NAME, T3_KPI_NAME):
            with open(OUTPUT_ROOT / name / "report.md", "a", encoding="utf-8") as f:
                f.write(
                    f"\n## T2×T3相互Jaccard（診断限定）\n- Jaccard(cfo_margin_improve×cfo_turnaround)"
                    f"={t2t3_jaccard['jaccard']} (積集合={t2t3_jaccard['intersection']} / "
                    f"和集合={t2t3_jaccard['union']} / T2キー={t2t3_jaccard['t2_keys']} / "
                    f"T3キー={t2t3_jaccard['t3_keys']})\n"
                )

    print("\n=== 第35周バッチ完了サマリー ===")
    for r in results:
        jac = r.get("jaccard", {})
        jac_str = ""
        if jac:
            jac_str = (
                f" Jaccard(sue={jac['sue_beat']['jaccard']}/sales={jac['sales_beat']['jaccard']})"
            )
        print(
            f"{r['kpi_name']}: n={r['n']} 月平均n={r.get('avg_monthly_n')} "
            f"lift={r['lift']}[{r['ci_low']},{r['ci_high']}] "
            f"EV(なし)={r['ev_point']}[{r['ev_ci_low']},{r['ev_ci_high']}] EV(stop8)={r['ev_stop8']} "
            f"verdict={r['verdict']} 一次結論={r['conclusion']} "
            f"entry_missing={r['entry_missing']}/raw={r['raw_signal_count']}{jac_str}"
        )
    if t2t3_jaccard is not None:
        print(
            f"T2×T3相互Jaccard={t2t3_jaccard['jaccard']} "
            f"(積集合={t2t3_jaccard['intersection']}/和集合={t2t3_jaccard['union']})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
