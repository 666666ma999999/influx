#!/usr/bin/env python3
"""第26周: マージン・インフレクション単独試行 margin_expand_yoy（カタログ§7-O・T1）。

docs/stock-algo-kpi-catalog.md §7-O の事前登録定義（Codexレビュー㉑㉒GO・凍結）を実装する。
in-sample期間（2016-11〜2022-11・holdout 2023年以降は使わない）で1試行だけ実行し、探索的
一次結論ルール（第18周基準）を適用する。実装の流儀は scripts/kpi_round24_signals.py（第24周・
直近の単独試行）の run_trial 呼び出し契約・prefilter_in_universe 再利用・holdout FATAL化
（第25周 kpi_round25_signals.py と同様）をそのまま踏襲する。

シグナル定義（凍結・§7-O）:
- 決算短信開示イベント（DocType `^(1Q|2Q|3Q|FY)FinancialStatements`）で、累計 OP/Sales の
  単四半期差分から単Q営業利益率 OPM_q を構築し、前年同期 OPM_qYoY との差が +2.0pt 以上
  かつ増収（S_q > S_qYoY）ならシグナル。
- 反応日は kpi_pead_signals.reaction_day（15:00境界）。エントリーは §6手順6の既定（defer_entry=True）。

時点整合性（Codexレビュー㉑CRITICAL・as-of構築）:
- 各イベントの判定は「そのイベント自身の開示（DiscDate,DiscTime,DiscNo）より前に公表された
  レコードのみ」を直前四半期・前年同期の参照に使う（最終訂正値の過去イベントへの遡及適用を禁止）。
- 訂正開示は同一 DocType 種別の再開示として現れる（実データ確認: 財務諸表訂正専用 DocType は
  存在せず、同一(Code,CurFYEn,CurPerType)の後日再開示＝訂正）。同一経済四半期の再発火は、
  「その訂正直前のas-of判定が不通過だった」場合のみ許可する（assertで確認・Codexレビュー㉒）。

単Q差分の対応関係（Codexレビュー㉒・凍結）:
- 許可ペアは 2Q−1Q / 3Q−2Q / FY−3Q のみ（1Qは累計そのまま）。飛び差分は禁止＝直前四半期種別が
  as-ofで欠損なら非シグナル。集計スコープ（連結/非連結×会計基準）はペア内・前年同期比較の全
  レコードで一致必須（OP/Sales のみ使用しNC*と混在しない。異スコープの直前四半期が存在する
  場合はスコープ不一致として診断カウントし非シグナル）。同日複数は(DiscDate,DiscTime,DiscNo)最新。

前年同期（Codexレビュー㉒・凍結）:
- 同一Code・同一四半期種別で CurFYEn の年部分がちょうど1年前（月日は一致必須＝決算期変更を除外）、
  かつ累計期間長（CurPerEn−CurPerSt の暦日数）が同等（|差|≤7日・近似日付フォールバック禁止）の
  レコードの単Q値。構築不能は非シグナル。

Canonical Module再利用（新規ロジックは単Q as-of構築 build_fins_single_q_history / 判定のみ）:
- fins読み込み・反応日・DiscTimeパース: kpi_pead_signals.load_fins_day/reaction_day/
  _parse_disc_time_minutes。数値パース・暦日差: kpi_uprev_signals._parse_numeric/_days_between。
- SUE突合診断の sue_beat 母集団: kpi_event_batch_signals.generate_sue_beat_signals。
- ユニバース事前フィルタ: kpi_round23_signals.prefilter_in_universe。
- フォワードリターン・集計・§6判定・レポート・台帳: kpi_event_study.*。
- 探索的一次結論: kpi_event_batch_signals.classify_exploratory。

Usage:
    python3 scripts/kpi_round26_signals.py --trial t1
    python3 scripts/kpi_round26_signals.py --trial t1 --start 2019-01 --end 2019-12 --no-trials-append
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
import kpi_event_batch_signals  # noqa: E402  (Canonical Module: classify_exploratory /
# generate_sue_beat_signals を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: compute_signal_returns/compute_stats/judge/
# write_report_md/append_trial/bootstrap_ev_ci/_compute_defer_stats を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: load_fins_day/reaction_day/
# _parse_disc_time_minutes/IN_SAMPLE_START・END/MONTH_RE を再利用)
import kpi_round23_signals  # noqa: E402  (Canonical Module: prefilter_in_universe を再利用)
import kpi_uprev_signals  # noqa: E402  (Canonical Module: _parse_numeric/_days_between/
# FINS_HISTORY_START_BD を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー読込を再利用)

PERIOD = (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_ROOT = Path("output/kpi")

# --- 事前登録パラメータ（カタログ§7-O・事後変更禁止） -----------------------------

T1_KPI_NAME = "margin_expand_yoy"
OPM_DELTA_MIN_PT = 2.0          # OPM_q − OPM_qYoY のシグナル閾値（+2.0pt・単一値凍結）
MAX_SPAN_DIFF_DAYS = 7          # 前年同期の累計期間長の許容差（|CurPerEn−CurPerSt の暦日差| ≤ 7）
# 決算期変更/移行期変則決算の除外。CurFYEn の年-1・月日一致でも、移行期の14ヶ月決算等では
# 当年四半期末と前年同四半期末が約1年離れない（例: 移行でカレンダーが2ヶ月ずれると四半期末は
# 約14ヶ月前になる）。前年同期の四半期末が当年の約1年前(365日)から MAX_YOY_END_GAP_DAYS 以内で
# なければ「同一四半期種別のYoY」として不成立とみなす（うるう年・週末調整の吸収に8日）。
MAX_YOY_END_GAP_DAYS = 8
PREV_QTR = {"2Q": "1Q", "3Q": "2Q", "FY": "3Q"}  # 単Q差分の直前四半期種別（1Qは累計そのまま）

# DocType `^(1Q|2Q|3Q|FY)FinancialStatements_<Scope>_<Standard>`。scope_key は Scope×Standard の
# 両方を一致条件に含める（連結/非連結の混在禁止に加え会計基準の跨ぎも比較不能なため保守側）。
FS_DOCTYPE_RE = re.compile(r"^(1Q|2Q|3Q|FY)FinancialStatements_([A-Za-z]+)_([A-Za-z]+)")

ROUND_TAG = "26_margin_expand_single"
SINGLE_TRIAL_NOTE = (
    "本ラウンドはT1単独試行だが累積試行数割引の対象（第26周時点で通算73試行目）。"
    "この結果単独で運用変更しない。"
)


# --- 単Q as-of 履歴の構築（新規ロジック） -----------------------------------------


def _disc_key(disc_date: str, disc_time_minutes: int, disc_no: str) -> tuple:
    """開示の時点順キー (DiscDate, DiscTime分, DiscNo)。同日複数の最新選択・as-of比較に使う。"""
    return (disc_date, disc_time_minutes, disc_no)


def _span_days(per_st: Optional[str], per_en: Optional[str]) -> Optional[int]:
    """CurPerSt/CurPerEn（"YYYY-MM-DD"）の累計期間長（暦日数）。欠損/不正はNone。"""
    if not per_st or not per_en:
        return None
    try:
        return kpi_uprev_signals._days_between(per_st.replace("-", ""), per_en.replace("-", ""))
    except (ValueError, TypeError):
        return None


def build_fins_single_q_history(
    hist_start_bd: str,
    end_bd: str,
) -> tuple[dict, dict, dict]:
    """[hist_start_bd, end_bd] の全fins開示から、単Q as-of構築に必要なインデックスを作る。

    Returns:
        (store, discdate_index, diag)
        store: dict[(code, qtype, cur_fy_en, scope_key)] -> list of dict
               各 dict = {disc_key, disc_date, cum_op, cum_sales, per_st, per_en}
               （disc_key 昇順ソート済み）。qtype/scope_key は DocType から、cur_fy_en は CurFYEn。
               cum_op/cum_sales は OP/Sales（連結系のみ・NC*不使用）を _parse_numeric したもの
               （欠損は None のまま格納し、参照側で不成立として扱う）。
        discdate_index: dict[(code, qtype, disc_date)] -> cur_fy_en
               （SUE突合診断で sue_beat 開示の CurFYEn を逆引きするため。同キー複数は最新 disc_key）。
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
        "history_fs_records": 0,     # DocType が FS_DOCTYPE_RE に一致した件数
        "history_cur_fy_en_missing": 0,
    }

    for d in scan_days:
        for rec in kpi_pead_signals.load_fins_day(d):
            diag["history_total_records"] += 1
            code = rec.get("Code")
            doc_type = rec.get("DocType", "") or ""
            m = FS_DOCTYPE_RE.match(doc_type)
            if not code or not m:
                continue
            diag["history_fs_records"] += 1
            qtype = m.group(1)
            scope_key = f"{m.group(2)}_{m.group(3)}"  # 例 "Consolidated_JP"
            cur_fy_en = rec.get("CurFYEn") or ""
            if not cur_fy_en:
                diag["history_cur_fy_en_missing"] += 1
                continue
            disc_time_minutes = kpi_pead_signals._parse_disc_time_minutes(rec.get("DiscTime"))
            disc_time_minutes = disc_time_minutes if disc_time_minutes is not None else 0
            disc_no = str(rec.get("DiscNo") or "")
            dk = _disc_key(d, disc_time_minutes, disc_no)
            entry = {
                "disc_key": dk,
                "disc_date": d,
                "cum_op": kpi_uprev_signals._parse_numeric(rec.get("OP")),
                "cum_sales": kpi_uprev_signals._parse_numeric(rec.get("Sales")),
                "per_st": rec.get("CurPerSt") or "",
                "per_en": rec.get("CurPerEn") or "",
            }
            store[(code, qtype, cur_fy_en, scope_key)].append(entry)

            idx_key = (code, qtype, d)
            prev = discdate_index.get(idx_key)
            if prev is None or prev[1] < dk:
                discdate_index[idx_key] = (cur_fy_en, dk)

    for key in store:
        store[key].sort(key=lambda r: r["disc_key"])
    # discdate_index は fy_en のみを露出（disc_key はタイブレーク用の内部値）
    discdate_index_out = {k: v[0] for k, v in discdate_index.items()}
    return store, discdate_index_out, diag


def _asof_latest(store: dict, key: tuple, before_key: tuple) -> Optional[dict]:
    """store[key] のうち disc_key < before_key の最新レコード（as-of）。無ければ None。"""
    lst = store.get(key)
    if not lst:
        return None
    cand = [r for r in lst if r["disc_key"] < before_key]
    if not cand:
        return None
    return max(cand, key=lambda r: r["disc_key"])


def _any_scope_prev_exists(store: dict, code: str, prev_qtype: str, fy_en: str, before_key: tuple) -> bool:
    """scope を問わず (code, prev_qtype, fy_en) の直前四半期 as-of レコードが存在するか
    （スコープ不一致による欠損を『単なる欠損』と切り分けて診断カウントするため）。"""
    for (c, q, fy, _scope), lst in store.items():
        if c == code and q == prev_qtype and fy == fy_en:
            if any(r["disc_key"] < before_key for r in lst):
                return True
    return False


def _yoy_fy_en(cur_fy_en: str) -> Optional[str]:
    """CurFYEn（"YYYY-MM-DD"）の年を1つ減じた文字列（月日一致＝決算期変更除外）。不正はNone。"""
    parts = cur_fy_en.split("-")
    if len(parts) != 3:
        return None
    try:
        y = int(parts[0])
    except ValueError:
        return None
    return f"{y - 1:04d}-{parts[1]}-{parts[2]}"


def _build_single_q(
    store: dict,
    code: str,
    qtype: str,
    fy_en: str,
    scope_key: str,
    cur_rec: dict,
    before_key: tuple,
    diag: dict,
    diag_prefix: str,
) -> Optional[tuple]:
    """cur_rec（当該四半期の累計レコード）から単Q (op_q, s_q) を as-of 構築する。

    1Q は累計そのまま。2Q/3Q/FY は同一(code,fy_en,scope)・直前四半期の as-of 累計との差分。
    直前四半期が同一スコープで見つからない場合、他スコープに存在すれば scope_mismatch、
    無ければ prev_missing として diag に計上して None を返す。
    """
    if cur_rec["cum_op"] is None or cur_rec["cum_sales"] is None:
        diag[f"{diag_prefix}_cum_missing"] += 1
        return None
    if qtype == "1Q":
        return cur_rec["cum_op"], cur_rec["cum_sales"], None
    prev_qtype = PREV_QTR[qtype]
    prev = _asof_latest(store, (code, prev_qtype, fy_en, scope_key), before_key)
    if prev is None:
        if _any_scope_prev_exists(store, code, prev_qtype, fy_en, before_key):
            diag[f"{diag_prefix}_scope_mismatch"] += 1
        else:
            diag[f"{diag_prefix}_prev_missing"] += 1
        return None
    if prev["cum_op"] is None or prev["cum_sales"] is None:
        diag[f"{diag_prefix}_prev_missing"] += 1
        return None
    return cur_rec["cum_op"] - prev["cum_op"], cur_rec["cum_sales"] - prev["cum_sales"], prev


def generate_margin_expand_signals(
    start_bd: str,
    end_bd: str,
    hist_start_bd: str,
    yoy_end_gap_days: int = MAX_YOY_END_GAP_DAYS,
    prebuilt: Optional[tuple] = None,
) -> tuple[pd.DataFrame, dict, dict, dict]:
    """[start_bd, end_bd] の決算短信開示から margin_expand_yoy シグナルを as-of 構築で生成する。

    `yoy_end_gap_days`: 決算期変更除外の許容幅（既定=MAX_YOY_END_GAP_DAYS=8・本試行の凍結値）。
    感度診断で ±1/±7 を渡すためだけの引数（判定は 8 で凍結）。`prebuilt`=(store,discdate_index,
    hist_diag) を渡すと fins 履歴の再構築を省く（感度ループで同一 store を使い回すため）。

    Returns:
        (signals_df, diag, store, discdate_index)。signals_df 列: signal_date, code,
        disclosed_date, disc_time, doc_type, qtype, cur_fy_en, scope_key, op_q, s_q, opm_q,
        op_qyoy, s_qyoy, opm_qyoy, opm_delta_pt, is_correction_refire。
    """
    if prebuilt is not None:
        store, discdate_index, hist_diag = prebuilt
    else:
        store, discdate_index, hist_diag = build_fins_single_q_history(hist_start_bd, end_bd)

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    event_days = [d for d in all_bdays if start_bd <= d <= end_bd]

    diag = {
        **hist_diag,
        "event_days_scanned": len(event_days),
        "same_day_collapsed": 0,     # 同一(Code,CurFYEn,四半期種別)×同日を最新1件へ集約した際の破棄件数
        "events_by_qtype": {q: 0 for q in ("1Q", "2Q", "3Q", "FY")},
        "cur_cum_missing": 0,
        "cur_prev_missing": 0,       # 直前四半期欠損（当該年）
        "cur_scope_mismatch": 0,     # スコープ不一致（当該年の直前四半期）
        "yoy_fy_en_unparsable": 0,
        "yoy_current_not_found": 0,  # 前年同期の当該四半期レコード不在
        "yoy_cum_missing": 0,
        "yoy_prev_missing": 0,       # 前年同期の直前四半期欠損
        "yoy_scope_mismatch": 0,     # スコープ不一致（前年同期の直前四半期）
        "irregular_period_excluded": 0,  # 累計期間長が±7日超（変則決算/変則四半期）
        "fiscal_change_excluded": 0,     # 前年同期の四半期末が約1年前でない（決算期変更/移行期）
        "current_sales_nonpos": 0,
        "yoy_sales_nonpos": 0,
        "condition_evaluated": 0,    # OPM_q/OPM_qYoY まで到達した件数
        "corr_refire_suppressed": 0,  # 同一経済四半期が既通過のため抑止
        "corr_refire_allowed": 0,     # 訂正直前as-of不通過→再発火許可
        "signals_by_qtype": {q: 0 for q in ("1Q", "2Q", "3Q", "FY")},
        "signal_opm_sign_breakdown": {
            "both_positive": 0, "deficit_to_deficit": 0, "deficit_to_positive": 0, "other": 0,
        },
        "signals_total": 0,
    }

    # 経済四半期キー -> 直近のas-of判定（通過True/不通過False）。訂正再発火の可否判定に使う。
    last_judgment: dict[tuple, bool] = {}
    rows: list[dict] = []

    # イベントを時点順（disc_key）に処理する（訂正再発火の逐次判定のため）。
    # §7-O凍結「同日複数は(DiscDate,DiscTime,DiscNo)の最新を採用」: 同一経済四半期(Code,CurFYEn,
    # 四半期種別)×同一開示日は判定前に最新 disc_key の1件へ集約する（同日の早いレコードで通過→
    # 最新で不通過のケースの取りこぼしを防ぐ）。日を跨ぐ再開示（訂正）は集約せず last_judgment で扱う。
    by_key_day: dict[tuple, tuple] = {}
    raw_event_count = 0
    for d in event_days:
        for rec in kpi_pead_signals.load_fins_day(d):
            doc_type = rec.get("DocType", "") or ""
            m = FS_DOCTYPE_RE.match(doc_type)
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
            dk = _disc_key(d, disc_time_minutes, disc_no)
            raw_event_count += 1
            collapse_key = (code, cur_fy_en, qtype, d)
            existing = by_key_day.get(collapse_key)
            if existing is None or existing[0] < dk:
                by_key_day[collapse_key] = (dk, d, rec, m)
    events = sorted(by_key_day.values(), key=lambda t: t[0])
    diag["same_day_collapsed"] = raw_event_count - len(events)

    for dk, d, rec, m in events:
        code = rec.get("Code")
        doc_type = rec.get("DocType", "")
        qtype = m.group(1)
        scope_key = f"{m.group(2)}_{m.group(3)}"
        cur_fy_en = rec.get("CurFYEn") or ""
        diag["events_by_qtype"][qtype] += 1
        econ_key = (code, cur_fy_en, qtype)

        cur_rec = {
            "cum_op": kpi_uprev_signals._parse_numeric(rec.get("OP")),
            "cum_sales": kpi_uprev_signals._parse_numeric(rec.get("Sales")),
            "per_st": rec.get("CurPerSt") or "",
            "per_en": rec.get("CurPerEn") or "",
        }

        # --- 当該四半期の単Q（as-of: 自身の disc_key より前の直前四半期のみ） ---
        cur_sq = _build_single_q(
            store, code, qtype, cur_fy_en, scope_key, cur_rec, dk, diag, "cur"
        )
        if cur_sq is None:
            continue
        op_q, s_q, _cur_prev = cur_sq

        # --- 前年同期の単Q ---
        yoy_fy_en = _yoy_fy_en(cur_fy_en)
        if yoy_fy_en is None:
            diag["yoy_fy_en_unparsable"] += 1
            continue
        yoy_cur = _asof_latest(store, (code, qtype, yoy_fy_en, scope_key), dk)
        if yoy_cur is None:
            diag["yoy_current_not_found"] += 1
            continue
        # 期間長同等（累計期間長の暦日差 ≤ 7日・決算期変更/変則決算除外）
        span_cur = _span_days(cur_rec["per_st"], cur_rec["per_en"])
        span_yoy = _span_days(yoy_cur["per_st"], yoy_cur["per_en"])
        if span_cur is None or span_yoy is None or abs(span_cur - span_yoy) > MAX_SPAN_DIFF_DAYS:
            diag["irregular_period_excluded"] += 1
            continue
        # 決算期変更/移行期変則決算の除外: 前年同期の四半期末が当年の約1年前でなければ非シグナル。
        end_gap = _span_days(yoy_cur["per_en"], cur_rec["per_en"])
        if end_gap is None or abs(end_gap - 365) > yoy_end_gap_days:
            diag["fiscal_change_excluded"] += 1
            continue
        yoy_sq = _build_single_q(
            store, code, qtype, yoy_fy_en, scope_key, yoy_cur, dk, diag, "yoy"
        )
        if yoy_sq is None:
            continue
        op_qyoy, s_qyoy, _yoy_prev = yoy_sq

        # --- OPM とシグナル条件 ---
        if s_q <= 0:
            diag["current_sales_nonpos"] += 1
            continue
        if s_qyoy <= 0:
            diag["yoy_sales_nonpos"] += 1
            continue
        diag["condition_evaluated"] += 1
        opm_q = op_q / s_q
        opm_qyoy = op_qyoy / s_qyoy
        opm_delta_pt = (opm_q - opm_qyoy) * 100.0
        passing = (opm_delta_pt >= OPM_DELTA_MIN_PT) and (s_q > s_qyoy)

        # --- 訂正再発火の逐次判定（Codexレビュー㉒） ---
        prev_judgment = last_judgment.get(econ_key)
        is_refire = False
        if passing and prev_judgment is True:
            # 既に通過済みの経済四半期の再開示（訂正含む）→ 再発火抑止
            diag["corr_refire_suppressed"] += 1
            last_judgment[econ_key] = passing
            continue
        if passing and prev_judgment is False:
            # 訂正直前のas-of判定が不通過だった場合のみ再発火を許可
            assert prev_judgment is False, (
                f"FATAL: 再発火の前提（訂正直前as-of不通過）に反する econ_key={econ_key}"
            )
            diag["corr_refire_allowed"] += 1
            is_refire = True
        last_judgment[econ_key] = passing
        if not passing:
            continue

        disc_time_minutes = kpi_pead_signals._parse_disc_time_minutes(rec.get("DiscTime"))
        g_date, rule = kpi_pead_signals.reaction_day(d, disc_time_minutes, bday_index, all_bdays)
        if g_date is None:
            continue  # 検証期間終端でカレンダー範囲外

        # 符号別内訳（落とし穴対策・赤字→赤字縮小等の可視化）
        if op_q >= 0 and op_qyoy >= 0:
            diag["signal_opm_sign_breakdown"]["both_positive"] += 1
        elif op_q < 0 and op_qyoy < 0:
            diag["signal_opm_sign_breakdown"]["deficit_to_deficit"] += 1
        elif op_q >= 0 and op_qyoy < 0:
            diag["signal_opm_sign_breakdown"]["deficit_to_positive"] += 1
        else:
            diag["signal_opm_sign_breakdown"]["other"] += 1

        diag["signals_by_qtype"][qtype] += 1
        diag["signals_total"] += 1
        rows.append(
            {
                "signal_date": g_date,
                "code": code,
                "disclosed_date": d,
                "disc_time": rec.get("DiscTime"),
                "reaction_rule": rule,
                "doc_type": doc_type,
                "qtype": qtype,
                "cur_fy_en": cur_fy_en,
                "scope_key": scope_key,
                "op_q": op_q,
                "s_q": s_q,
                "opm_q": opm_q,
                "op_qyoy": op_qyoy,
                "s_qyoy": s_qyoy,
                "opm_qyoy": opm_qyoy,
                "opm_delta_pt": opm_delta_pt,
                "is_correction_refire": is_refire,
            }
        )

    return pd.DataFrame(rows), diag, store, discdate_index


# --- SUE突合診断（診断限定・合否判定には使わない） ---------------------------------


def compute_sue_overlap_diagnostic(
    signals_df: pd.DataFrame,
    in_universe_df: pd.DataFrame,
    event_key_map: dict,
    start_bd: str,
    end_bd: str,
    discdate_index: dict,
    base_rate_by_month: dict,
) -> dict:
    """sue_beat 母集団と margin_expand_yoy のイベントキー(Code,CurFYEn,四半期種別)突合診断。

    ジャッカード係数・四半期種別別重複率・SUE非重複群の n/EV/lift を返す（診断限定）。
    """
    sue_df, sue_diag = kpi_event_batch_signals.generate_sue_beat_signals(start_bd, end_bd)
    sue_keys: set = set()
    sue_keys_by_qtype: dict[str, set] = defaultdict(set)
    sue_unmapped = 0
    for _, r in sue_df.iterrows():
        code = r["code"]
        qtype = r["period_type"]  # sue_beat は 2Q / FY のみ
        d = r["disclosed_date"]
        fy_en = discdate_index.get((code, qtype, d))
        if fy_en is None:
            sue_unmapped += 1
            continue
        key = (code, fy_en, qtype)
        sue_keys.add(key)
        sue_keys_by_qtype[qtype].add(key)

    # margin シグナルのイベントキー集合（全生成シグナル）
    margin_keys: set = set()
    margin_keys_by_qtype: dict[str, set] = defaultdict(set)
    for _, r in signals_df.iterrows():
        key = (r["code"], r["cur_fy_en"], r["qtype"])
        margin_keys.add(key)
        margin_keys_by_qtype[r["qtype"]].add(key)

    inter = margin_keys & sue_keys
    union = margin_keys | sue_keys
    jaccard = (len(inter) / len(union)) if union else None
    overlap_by_qtype = {}
    for q in ("2Q", "FY"):
        mk = margin_keys_by_qtype.get(q, set())
        sk = sue_keys_by_qtype.get(q, set())
        u = mk | sk
        overlap_by_qtype[q] = {
            "margin_n": len(mk), "sue_n": len(sk), "intersection": len(mk & sk),
            "union": len(u), "jaccard": (len(mk & sk) / len(u)) if u else None,
        }

    # SUE非重複群（in_universe のうち sue キーに含まれないもの）の成績（診断限定）
    non_overlap_stats = {"n": 0, "point_lift": None, "ev_point": None, "ev_ci_low": None,
                         "ev_ci_high": None}
    if len(in_universe_df):
        iu = in_universe_df.copy()
        iu["_event_key"] = [event_key_map.get((sd, c)) for sd, c in zip(iu["signal_date"], iu["code"])]
        non_overlap = iu[~iu["_event_key"].isin(sue_keys)].reset_index(drop=True)
        if len(non_overlap):
            st = kpi_event_study.compute_stats(non_overlap, base_rate_by_month, PERIOD)
            ev = kpi_event_study.bootstrap_ev_ci(non_overlap, ev_column="ret")
            non_overlap_stats = {
                "n": st["n"], "point_lift": st.get("point_lift"),
                "ev_point": ev["point_ev"], "ev_ci_low": ev["ci_low"], "ev_ci_high": ev["ci_high"],
            }

    return {
        "sue_signal_count": int(len(sue_df)),
        "sue_unique_event_keys": len(sue_keys),
        "sue_unmapped_fy_en": sue_unmapped,
        "margin_unique_event_keys": len(margin_keys),
        "intersection": len(inter),
        "union": len(union),
        "jaccard": jaccard,
        "overlap_by_qtype": overlap_by_qtype,
        "non_overlap_subset": non_overlap_stats,
        "sue_gen_signals_total": sue_diag.get("signals_sue_beat"),
    }


# --- ハーネス実行 + 探索的一次結論 + 台帳記録 -------------------------------------


def run_trial(
    signals_df: pd.DataFrame,
    base_params: dict,
    harness_ctx: dict,
    gen_diag: dict,
    report_extra_lines: list[str],
    append_to_ledger: bool,
    discdate_index: dict,
    start_bd: str,
    end_bd: str,
) -> dict:
    """低レベルCanonical関数を直接呼び出しハーネス実行〜探索的結論〜台帳記録まで行う
    （実装の流儀は kpi_round24_signals.run_trial を踏襲）。defer_entry は §6既定の True 固定。"""
    kpi_name = T1_KPI_NAME
    defer_entry = True
    all_bdays = harness_ctx["all_bdays"]
    bday_index = harness_ctx["bday_index"]
    regime_by_day = harness_ctx["regime_by_day"]
    base_rate_by_month = harness_ctx["base_rate_by_month"]
    universes_by_month = harness_ctx["universes_by_month"]

    # (signal_date, code) -> イベントキー（SUE非重複群の分割に使う）
    event_key_map = {
        (r["signal_date"], r["code"]): (r["code"], r["cur_fy_en"], r["qtype"])
        for _, r in signals_df.iterrows()
    }

    harness_signals_df = signals_df[["signal_date", "code"]].copy()
    returns_df, diag = kpi_event_study.compute_signal_returns(
        harness_signals_df, bday_index, all_bdays, regime_by_day, universes_by_month,
        defer_entry=defer_entry,
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

    # SUE突合診断（診断限定・合否には使わない）
    sue_diag = compute_sue_overlap_diagnostic(
        signals_df, in_universe_df, event_key_map, start_bd, end_bd, discdate_index, base_rate_by_month
    )

    params = {
        **base_params,
        "defer_entry": defer_entry,
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
        "single_trial_discount_note": SINGLE_TRIAL_NOTE,
        "generation_diag": gen_diag,
        "sue_overlap_diag": sue_diag,
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
        f.write("\n## 探索的一次結論（第26周・カタログ§7-O事前登録ルール）\n")
        f.write(
            f"- EV(なし・往復コスト0.3%控除込)点推定 = {ev_ci['point_ev']:.4%}"
            f"（月次ブロック・ブートストラップ95%CI=[{ev_ci['ci_low']:.4%}, {ev_ci['ci_high']:.4%}]"
            f"・有効ブートストラップ数={ev_ci['n_boot_valid']}）\n"
            if ev_ci["point_ev"] is not None
            else "- EV(なし)点推定 = 計算不能\n"
        )
        f.write(f"- リフト点推定 = {stats.get('point_lift')}\n")
        f.write(f"- **探索的一次結論: {conclusion}**\n")
        f.write(f"- {SINGLE_TRIAL_NOTE}\n")
        f.write("\n## §7-O 必須診断（シグナル生成段階）\n")
        eq = gen_diag["events_by_qtype"]
        sq = gen_diag["signals_by_qtype"]
        f.write(f"- 四半期種別別raw件数: {eq}\n")
        f.write(
            f"- 直前四半期欠損数: 当該年={gen_diag['cur_prev_missing']} / "
            f"前年同期={gen_diag['yoy_prev_missing']}\n"
        )
        f.write(
            f"- 前年差構築不能数: 前年当該四半期不在={gen_diag['yoy_current_not_found']} / "
            f"前年累計欠損={gen_diag['yoy_cum_missing']} / 前年直前欠損={gen_diag['yoy_prev_missing']}\n"
        )
        f.write(
            f"- 変則決算除外数: 累計期間長±7日超={gen_diag['irregular_period_excluded']} / "
            f"決算期変更(前年四半期末が約1年前でない)={gen_diag['fiscal_change_excluded']}\n"
        )
        f.write(f"- 訂正由来再発火数: 許可={gen_diag['corr_refire_allowed']} / 抑止={gen_diag['corr_refire_suppressed']}\n")
        f.write(
            f"- スコープ不一致数: 当該年直前={gen_diag['cur_scope_mismatch']} / "
            f"前年直前={gen_diag['yoy_scope_mismatch']}\n"
        )
        f.write(f"- 最終シグナル件数: 合計={gen_diag['signals_total']} / 四半期別={sq}\n")
        f.write(f"- OPM符号別内訳（シグナル成立）: {gen_diag['signal_opm_sign_breakdown']}\n")
        f.write(
            f"- 同日複数レコード集約（§7-O・最新1件採用で破棄した件数）: {gen_diag['same_day_collapsed']}\n"
        )
        sens = params.get("max_yoy_end_gap_days_sensitivity")
        if sens:
            f.write(
                f"\n## 決算期変更除外幅の感度診断（判定は±{params.get('max_yoy_end_gap_days')}日で凍結・診断限定）\n"
            )
            f.write(
                "- ±8日は実データ(code23050の決算期変更)を見て時点整合性の観点で選定・"
                "リターンは見ていない（詳細: params.max_yoy_end_gap_days_rationale）\n"
            )
            for gap_key in ("gap_1", "gap_7", "gap_8"):
                v = sens.get(gap_key)
                if v:
                    f.write(
                        f"- {gap_key}: n={v['n']} / lift={v['lift']} / EV(なし)={v['ev_point']} "
                        f"[{v['ev_ci_low']},{v['ev_ci_high']}] / 一次結論={v['conclusion']}\n"
                    )
        f.write("\n## SUE直交性診断（診断限定・合否判定に使わない）\n")
        f.write(
            f"- sue_beat シグナル数={sue_diag['sue_signal_count']} / "
            f"ユニークイベントキー数={sue_diag['sue_unique_event_keys']} "
            f"(fy_en逆引き不能={sue_diag['sue_unmapped_fy_en']})\n"
        )
        f.write(
            f"- margin ユニークイベントキー数={sue_diag['margin_unique_event_keys']} / "
            f"共通={sue_diag['intersection']} / 和={sue_diag['union']} / "
            f"ジャッカード={sue_diag['jaccard']}\n"
        )
        f.write(f"- 四半期種別別重複: {sue_diag['overlap_by_qtype']}\n")
        no = sue_diag["non_overlap_subset"]
        f.write(
            f"- SUE非重複群（診断限定）: n={no['n']} / lift={no['point_lift']} / "
            f"EV(なし)点推定={no['ev_point']} CI=[{no['ev_ci_low']},{no['ev_ci_high']}]\n"
        )
        f.write("\n## 件数注記（§7-O）\n")
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
            "entry_mode": "defer_max3bd" if defer_entry else "fixed_t1",
            "regime_filter": None,
        }
        kpi_event_study.append_trial(trial_record, TRIALS_PATH)
        print(f"{kpi_name}: 台帳へ append 済み ({TRIALS_PATH})")
    else:
        print(f"{kpi_name}: --no-trials-append 指定のため台帳へは記録しませんでした")

    print(
        f"{kpi_name} 完了: n={stats['n']} lift={stats.get('point_lift')} verdict(§6)={verdict} "
        f"EV点推定={ev_ci['point_ev']} CI95=[{ev_ci['ci_low']},{ev_ci['ci_high']}] "
        f"探索的一次結論={conclusion}"
    )
    print(f"{kpi_name} report: {report_path}")

    return {
        "kpi_name": kpi_name, "n": stats["n"], "lift": stats.get("point_lift"),
        "ci_low": stats.get("ci_low"), "ci_high": stats.get("ci_high"),
        "ev_point": ev_ci["point_ev"], "ev_ci_low": ev_ci["ci_low"], "ev_ci_high": ev_ci["ci_high"],
        "ev_stop8": stats.get("ev_stop8"),
        "verdict": verdict, "conclusion": conclusion, "returns_path": returns_path,
        "avg_monthly_n": stats.get("avg_monthly_n"),
        "sue_diag": sue_diag,
        "entry_missing": diag["entry_missing"], "raw_signal_count": diag["raw_signal_count"],
        "duplicate_discarded": diag["duplicate_discarded"], "out_of_universe": diag["out_of_universe"],
        "in_universe_df": in_universe_df,
    }


# --- メイン処理 ---------------------------------------------------------------------


def _event_bd_bounds(start_month: str, end_month: str, all_bdays: list[str]) -> tuple[str, str]:
    start_bound = start_month.replace("-", "") + "01"
    end_bound = end_month.replace("-", "") + "31"
    start_bd = next((d for d in all_bdays if d >= start_bound), None)
    end_bd = next((d for d in reversed(all_bdays) if d <= end_bound), None)
    if start_bd is None or end_bd is None:
        raise SystemExit("FATAL: 指定期間に営業日が見つかりません")
    return start_bd, end_bd


def _variant_stats(
    signals_df: pd.DataFrame,
    harness_ctx: dict,
) -> dict:
    """感度診断用: 生成済みシグナルをハーネスに通し n/lift/EV/一次結論だけを返す
    （台帳append・レポート書き出しなし・判定は行わない診断限定）。"""
    universes_by_month = harness_ctx["universes_by_month"]
    filtered_df, _n_raw, _n_filtered = kpi_round23_signals.prefilter_in_universe(
        signals_df, universes_by_month
    )
    if filtered_df.empty:
        return {"n": 0, "lift": None, "ev_point": None, "ev_ci_low": None,
                "ev_ci_high": None, "conclusion": "n0"}
    hs = filtered_df[["signal_date", "code"]].copy()
    returns_df, _diag = kpi_event_study.compute_signal_returns(
        hs, harness_ctx["bday_index"], harness_ctx["all_bdays"], harness_ctx["regime_by_day"],
        universes_by_month, defer_entry=True,
    )
    iu = returns_df[returns_df["in_universe"]].reset_index(drop=True) if len(returns_df) else returns_df
    if iu.empty:
        return {"n": 0, "lift": None, "ev_point": None, "ev_ci_low": None,
                "ev_ci_high": None, "conclusion": "n0"}
    stats = kpi_event_study.compute_stats(iu, harness_ctx["base_rate_by_month"], PERIOD)
    ev = kpi_event_study.bootstrap_ev_ci(iu, ev_column="ret")
    conclusion = kpi_event_batch_signals.classify_exploratory(
        stats.get("point_lift"), ev["point_ev"], ev["ci_low"]
    )
    return {
        "n": stats["n"], "lift": stats.get("point_lift"),
        "ev_point": ev["point_ev"], "ev_ci_low": ev["ci_low"], "ev_ci_high": ev["ci_high"],
        "conclusion": conclusion,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="第26周: マージン・インフレクション単独試行 margin_expand_yoy（カタログ§7-O・T1）"
    )
    parser.add_argument("--trial", default="t1", choices=["t1"], help="実行する試行（本周はt1のみ）")
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
    parser.add_argument(
        "--yoy-end-gap-days", type=int, default=MAX_YOY_END_GAP_DAYS,
        help=f"決算期変更除外の許容幅(既定={MAX_YOY_END_GAP_DAYS}=凍結値)。判定は8で凍結・感度用にのみ変更",
    )
    parser.add_argument(
        "--no-sensitivity", action="store_true",
        help="±1/±7日の感度診断をスキップ(smokeテスト用・本実行では実施)",
    )
    args = parser.parse_args()

    if not kpi_pead_signals.MONTH_RE.match(args.start) or not kpi_pead_signals.MONTH_RE.match(args.end):
        raise SystemExit("FATAL: --start/--end は YYYY-MM 形式で指定してください")
    if args.end > kpi_pead_signals.IN_SAMPLE_END:
        raise SystemExit(
            f"FATAL: --end={args.end} はholdout期間(2023年以降)に抵触します。in-sample評価は"
            f"{kpi_pead_signals.IN_SAMPLE_END}まで（第25周同様・holdout保護FATAL化）"
        )

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    start_bd, end_bd = _event_bd_bounds(args.start, args.end, all_bdays)

    # 単Q差分・前年同期の as-of 構築のため履歴はfinsキャッシュ最古から積む。
    hist_start_bd = kpi_uprev_signals.FINS_HISTORY_START_BD
    if hist_start_bd > start_bd:
        hist_start_bd = start_bd

    topix_close = measure_base_rate.load_topix_series()
    regime_by_day = measure_base_rate.build_regime_series(topix_close)
    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    universes_by_month = kpi_event_study.load_universe_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

    harness_ctx = {
        "all_bdays": all_bdays,
        "bday_index": bday_index,
        "regime_by_day": regime_by_day,
        "base_rate_by_month": base_rate_by_month,
        "universes_by_month": universes_by_month,
    }

    print(f"T1 {T1_KPI_NAME}: fins履歴構築中（履歴起点={hist_start_bd}）...", file=sys.stderr)
    prebuilt = build_fins_single_q_history(hist_start_bd, end_bd)  # 感度ループでも使い回す
    print(f"T1 {T1_KPI_NAME}: シグナル生成中（yoy_end_gap_days={args.yoy_end_gap_days}）...", file=sys.stderr)
    signals_df, gen_diag, _store, discdate_index = generate_margin_expand_signals(
        start_bd, end_bd, hist_start_bd,
        yoy_end_gap_days=args.yoy_end_gap_days, prebuilt=prebuilt,
    )
    print(
        f"T1 シグナル生成完了: {len(signals_df)}件 "
        f"(イベント種別別={gen_diag['events_by_qtype']}, "
        f"条件到達={gen_diag['condition_evaluated']}, "
        f"前年当該不在={gen_diag['yoy_current_not_found']}, "
        f"変則除外={gen_diag['irregular_period_excluded']}, "
        f"決算期変更除外={gen_diag['fiscal_change_excluded']}, "
        f"再発火許可={gen_diag['corr_refire_allowed']}/抑止={gen_diag['corr_refire_suppressed']}, "
        f"シグナル成立={gen_diag['signals_total']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T1シグナルが0件です")

    kpi_dir = OUTPUT_ROOT / T1_KPI_NAME
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_df.to_csv(kpi_dir / "signals_raw.csv", index=False)  # 手動照合用の全詳細列付き

    filtered_df, n_raw, n_filtered = kpi_round23_signals.prefilter_in_universe(
        signals_df, universes_by_month
    )
    print(
        f"T1 ユニバース事前フィルタ: 生{n_raw}件 → ハーネス投入{n_filtered}件"
        f"(除去{n_raw - n_filtered}件=判定に使われないユニバース外・統計結果不変)",
        file=sys.stderr,
    )
    if filtered_df.empty:
        raise SystemExit("FATAL: T1の事前フィルタ後シグナルが0件です")

    # 決算期変更除外幅(±8日)の感度診断（診断限定・判定は8で凍結）。同一 store を使い回す。
    yoy_gap_sensitivity = None
    if not args.no_sensitivity:
        yoy_gap_sensitivity = {}
        for gap in (1, 7, MAX_YOY_END_GAP_DAYS):
            if gap == args.yoy_end_gap_days:
                variant_df = signals_df
            else:
                variant_df, _gd, _s, _d = generate_margin_expand_signals(
                    start_bd, end_bd, hist_start_bd, yoy_end_gap_days=gap, prebuilt=prebuilt,
                )
            vs = _variant_stats(variant_df, harness_ctx)
            yoy_gap_sensitivity[f"gap_{gap}"] = vs
            print(
                f"T1 感度(±{gap}日): n={vs['n']} lift={vs['lift']} EV={vs['ev_point']} "
                f"一次結論={vs['conclusion']}",
                file=sys.stderr,
            )

    base_params = {
        "signal_definition": (
            f"(OPM_q − OPM_qYoY ≥ +{OPM_DELTA_MIN_PT}pt) かつ (S_q > S_qYoY)。"
            f"OPM_q=単Q営業利益率(連結系OP/Sales の単四半期差分・1Qは累計そのまま)。"
            f"前年同期=同一四半期種別・CurFYEn年-1・月日一致・累計期間長差≤{MAX_SPAN_DIFF_DAYS}日。"
        ),
        "opm_delta_min_pt": OPM_DELTA_MIN_PT,
        "max_span_diff_days": MAX_SPAN_DIFF_DAYS,
        "max_yoy_end_gap_days": args.yoy_end_gap_days,
        "fiscal_change_guard_rule": (
            f"前年同期の四半期末(CurPerEn)が当年の365±{args.yoy_end_gap_days}日前でなければ非シグナル"
            "（決算期変更/移行期変則決算の除外）。"
        ),
        "max_yoy_end_gap_days_rationale": (
            "±8日は実データ（code23050=12月末→2月末へ決算期変更した移行期14ヶ月決算FY2018）を観察し、"
            "移行でカレンダーが約2ヶ月ずれたケース（四半期末が約14ヶ月前=gap 426日）を確実に弾きつつ、"
            "うるう年の四半期末gap 366日・週末調整の数日を通すよう選定した。"
            "リターン（n/lift/EV）を見て決めていない（前年同期の同一性=時点整合性のみで決定）。"
            "感度は max_yoy_end_gap_days_sensitivity に記録（±1/±7/±8で一次結論が不変なことを確認）。"
        ),
        "max_yoy_end_gap_days_sensitivity": yoy_gap_sensitivity,
        "scope_key_definition": (
            "DocTypeの Scope(Consolidated/NonConsolidated)×Standard(JP/IFRS/US等) 双方一致を"
            "スコープ一致条件とする（連結/非連結混在禁止＋会計基準跨ぎも比較不能なため保守側）。"
            "数値はOP/Salesのみ使用しNC*は不使用。"
        ),
        "yoy_period_length_rule": (
            f"(1)CurPerSt/CurPerEn の暦日差（累計期間長）が当年と前年で ±{MAX_SPAN_DIFF_DAYS}日以内。"
            f"(2)前年同期の四半期末(CurPerEn)が当年の365±{args.yoy_end_gap_days}日前。"
            "いずれも近似日付フォールバックはせず、逸脱は変則決算/決算期変更として除外。"
        ),
        "same_day_latest_rule": (
            "同一(Code,CurFYEn,四半期種別)×同一開示日は判定前に(DiscDate,DiscTime,DiscNo)最新の"
            "1件へ集約（§7-O凍結）。日跨ぎ再開示（訂正）は集約せず as-of 逐次判定で扱う。"
        ),
        "asof_rule": (
            "直前四半期・前年同期の参照はイベント自身の(DiscDate,DiscTime,DiscNo)より前の開示のみ。"
            "訂正は同一(Code,CurFYEn,四半期種別)の後日再開示として現れ、訂正直前as-of不通過時のみ再発火。"
        ),
        "reaction": "シグナル確定=反応日G終値・エントリー=G+1寄付（kpi_pead_signals.reaction_day）",
        "hist_start_bd": hist_start_bd,
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "prefilter_note": (
            "ユニバース事前フィルタ適用(第20周§7-I / 第23周T2前例・統計結果不変)。in_universe_dfは"
            "不変のためn/lift/EVは無フィルタ時と一致する"
        ),
    }
    report_extra = [
        (
            f"生シグナル数={n_raw}件 / ユニバース事前フィルタ後のハーネス投入数={n_filtered}件"
            f"(除去{n_raw - n_filtered}件=判定に使われないユニバース外・統計結果不変)。"
        ),
        (
            f"as-of履歴起点={hist_start_bd}。前年同期比較は前年finsが必要なため、"
            f"前年データ不足期(finsキャッシュ最古付近)では yoy_current_not_found として非シグナル化される"
            f"（当該件数={gen_diag['yoy_current_not_found']}）。"
        ),
    ]

    result = run_trial(
        filtered_df, base_params, harness_ctx, gen_diag, report_extra,
        append_to_ledger=not args.no_trials_append,
        discdate_index=discdate_index, start_bd=start_bd, end_bd=end_bd,
    )

    print("\n=== 第26周 T1 margin_expand_yoy 完了サマリー ===")
    sd = result["sue_diag"]
    no = sd["non_overlap_subset"]
    print(
        f"{result['kpi_name']}: n={result['n']} 月平均n={result.get('avg_monthly_n')} "
        f"lift={result['lift']}[{result['ci_low']},{result['ci_high']}] "
        f"EV(なし)={result['ev_point']}[{result['ev_ci_low']},{result['ev_ci_high']}] "
        f"EV(stop8)={result['ev_stop8']} "
        f"verdict(§6)={result['verdict']} 一次結論={result['conclusion']}"
    )
    print(
        f"SUE突合: ジャッカード={sd['jaccard']} 共通={sd['intersection']}/和={sd['union']} "
        f"非重複群 n={no['n']} lift={no['point_lift']} EV={no['ev_point']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
