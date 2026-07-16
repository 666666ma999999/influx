#!/usr/bin/env python3
"""第33周: master属性変化家族の残り二本+売上ビート（カタログ§7-V・T1〜T3）。

docs/stock-algo-kpi-catalog.md §7-V の事前登録定義（凍結・Codexレビュー㊹㊺GO）を実装する。
3試行とも in-sample期間（2016-11〜2022-11・holdout 2023年以降は使わない）で実行し、共通の
探索的一次結論ルール（第18周基準）を適用する。実装の流儀は scripts/kpi_round32_signals.py の
run_trial 標準フロー・月末master差分・参考観測自動格下げ・prefilter_in_universe を踏襲する。

Canonical Module再利用（新規ロジックはT1/T2/T3の生成関数のみ）:
- 月末master差分の純ユーティリティ（_next_month/_first_bday_of_month/_master_snapshot_dates）は
  kpi_round32_signals から再利用（第32周 mkt_upgrade と同一インフラ）。
- T1 `mrgn_upgrade`: 連続月末master対で Mrgn が 1(信用)→2(貸借) に遷移した銘柄を昇格イベント化。
  3絡みの遷移は対象外・診断カウント。新規上場/欠損/非連続月は非シグナル。シグナル日=翌月初営業日。
  **n<30 なら judge を呼ばず verdict=reference_observation（§7-C型参考観測）**（第32周T1と同一実装）。
- T2 `scalecat_upgrade`: 連続月末master対で ScaleCat の rank(§7-V凍結表)が増加した銘柄を昇格化。
  2018-08末→2018-09末対は「TOPIX Small 1」初出の一斉再編対として全除外。漏斗診断
  （raw昇格→前月末TOP500適格→最終n）・月別クラスタ分布を必須出力。**n<30 なら参考観測へ格下げ**。
- T3 `sales_beat`: kpi_event_batch_signals.generate_sue_beat_signals と完全同型のas-of機構
  （build_fop_history 流儀・同一CurFYEn・2Q/FYのみ・15:00/15:30境界・遡り365暦日・直前予想≤0除外）で、
  2Q=Sales対FSales2Q / FY=Sales対FSales・閾値+5%。SUE(sue_beat)とのJaccard（キー=(Code,CurFYEn,
  period_type)）とSUE非重複群のn/EV/liftを必須診断（§7-O margin_expandと同じ診断設計）。
- ユニバース事前フィルタ: kpi_round23_signals.prefilter_in_universe（統計結果不変・第20周§7-I前例）。
- フォワードリターン・重複除去・集計・§6判定・レポート・台帳: kpi_event_study の
  compute_signal_returns/compute_stats/judge/bootstrap_ev_ci/write_report_md/append_trial を再利用。
- 探索的一次結論ルール: kpi_event_batch_signals.classify_exploratory を再利用。

3試行とも defer_entry=True（§6手順6の第5周以降既定方針＝S高で買えない日は翌日繰り延べ）。

家族の割引解釈（§7-V凍結・Codex㊹M対応）: master_diff_family = {mkt_upgrade[第32周],
mrgn_upgrade, scalecat_upgrade} の3本で固定。mkt_upgrade の正EV(EV+2.75%/n=33/inconclusive)を
見た後の家族展開であり、同一in-sample期間ではmaster属性の追加試行を以後禁止。本家族の結果は
統計的確認ではなく前向き候補の優先順位付けに限定する。

Usage:
    python3 scripts/kpi_round33_signals.py --trial all
    python3 scripts/kpi_round33_signals.py --trial t1
    python3 scripts/kpi_round33_signals.py --trial all --start 2017-01 --end 2017-12 --no-trials-append
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
import kpi_round32_signals as r32  # noqa: E402  (Canonical Module: _next_month/_first_bday_of_month/
# _master_snapshot_dates=月末master差分の純ユーティリティを再利用)
import kpi_uprev_signals  # noqa: E402  (Canonical Module: build_fop_history/_parse_numeric/_days_between を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・master読込を再利用)

PERIOD = (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_ROOT = Path("output/kpi")

# --- 事前登録パラメータ（カタログ§7-V・事後変更禁止） -----------------------------

SMALL_N_REFERENCE_THRESHOLD = 30  # T1/T2: n<30 なら§7-C型参考観測へ自動格下げ（第32周T1と同一ルール）

# T1: Mrgn 信用区分。実データ列挙で凍結: 1=信用/2=貸借/3=その他。昇格＝1→2 の遷移のみ。
T1_KPI_NAME = "mrgn_upgrade"
T1_MRGN_MARGIN = "1"    # 信用
T1_MRGN_LOAN = "2"      # 貸借（昇格先）
T1_MRGN_OTHER = "3"     # その他（対象外・診断カウント）

# T2: TOPIX規模区分ランク表（実データ列挙で凍結・§7-V）。rank5=最上位・"-"=TOPIX外=0。
T2_KPI_NAME = "scalecat_upgrade"
T2_SCALECAT_RANK = {
    "TOPIX Core30": 5,
    "TOPIX Large70": 4,
    "TOPIX Mid400": 3,
    "TOPIX Small 1": 2,
    "TOPIX Small 2": 1,
    "-": 0,  # TOPIX外
}
# 「TOPIX Small 1」初出対＝一斉再編対として全除外（2026-07-14実データ走査で特定・§7-V凍結）。
T2_REORG_PREV_MONTH = "201808"
T2_REORG_NEW_MONTH = "201809"

# T3: 売上ビート（sue_beatと完全同型のas-of）。
T3_KPI_NAME = "sales_beat"
T3_THRESHOLD = 0.05  # Sales実績/直前Sales予想-1 >= +5%（単一探索値・凍結）
T3_LOOKBACK_DAYS = kpi_event_batch_signals.T2_LOOKBACK_DAYS  # 365暦日（sue_beatと同一）
T3_DOCTYPE_RE = re.compile(r"^(2Q|FY)FinancialStatements")   # sue_beatと同一規約

MULTI_TRIAL_NOTE = (
    "本ラウンドはT1〜T3の3試行同時登録であり累積試行数割引の対象。"
    "この結果単独で運用変更しない。"
)
ROUND_TAG = "33_master_diff_family"

MASTER_DIFF_FAMILY_NOTE = (
    "master_diff_family={mkt_upgrade[第32周],mrgn_upgrade,scalecat_upgrade}の3本で固定。"
    "mkt_upgradeの正EV(EV+2.75%/n=33/inconclusive)を見た後の家族展開であり、同一in-sample期間では"
    "master属性の追加試行を以後禁止。本家族の結果は統計的確認ではなく前向き候補の優先順位付けに限定"
    "（家族単位の割引解釈）。"
)

DEFER_RATIONALE = (
    "§7-V各試行はエントリー=T+1寄付。§6手順6『S高で買えない日は翌日繰り延べ(第5周以降の既定方針)』"
    "に従いdefer_entry=True。信用区分昇格・規模区分昇格・売上ビートいずれもT+1寄付がS高張り付きで"
    "買えないことがあり得るため通常の繰延で扱う。"
)


# --- T1: mrgn_upgrade シグナル生成（新規ロジック） ---------------------------------


def generate_mrgn_upgrade_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
) -> tuple[pd.DataFrame, dict]:
    """月末masterの信用区分(Mrgn)が連続月対で 1(信用)→2(貸借) に遷移した銘柄を昇格イベント化する
    （§7-V T1）。

    連続する月末スナップショット対 (prev, new) につき、両スナップショットに存在する銘柄で
    prev Mrgn=='1' ∩ new Mrgn=='2' を昇格＝シグナルとする。3絡みの遷移は対象外（診断カウント）。
    新規上場（前月不在）・欠損・非連続月は非シグナル。
    シグナル日=新スナップショット月の翌月初営業日（観測遅延を明示したas-of設計）。
    """
    snap_dates = r32._master_snapshot_dates()

    diag = {
        "consecutive_pairs_total": 0,        # 連続月対の総数
        "pairs_in_window": 0,                # シグナル日が[start_bd,end_bd]に入る連続月対
        "non_consecutive_pairs_skipped": 0,  # 月が連続していない対
        "upgrades": 0,                       # 昇格(1→2)シグナル（延べ）
        "downgrades": 0,                     # 降格(2→1・診断のみ)
        "mrgn3_involved": 0,                 # 3絡みの遷移(対象外・診断のみ)
        "new_listings": 0,                   # 前月スナップショットに不在
        "prev_or_new_mrgn_missing": 0,       # 前月または当月のMrgn欠損/非有効
    }
    rows: list[dict] = []
    valid = {T1_MRGN_MARGIN, T1_MRGN_LOAN, T1_MRGN_OTHER}

    for prev_snap, new_snap in zip(snap_dates, snap_dates[1:]):
        prev_ym, new_ym = prev_snap[:6], new_snap[:6]
        if r32._next_month(prev_ym) != new_ym:
            diag["non_consecutive_pairs_skipped"] += 1
            continue
        diag["consecutive_pairs_total"] += 1

        signal_month = r32._next_month(new_ym)
        signal_date = r32._first_bday_of_month(signal_month, all_bdays)
        if signal_date is None or not (start_bd <= signal_date <= end_bd):
            continue
        diag["pairs_in_window"] += 1

        prev_master = measure_base_rate.load_master_day(prev_snap)
        new_master = measure_base_rate.load_master_day(new_snap)

        for code, new_rec in new_master.items():
            new_mrgn = new_rec.get("Mrgn")
            prev_rec = prev_master.get(code)
            if prev_rec is None:
                diag["new_listings"] += 1
                continue
            prev_mrgn = prev_rec.get("Mrgn")
            if prev_mrgn not in valid or new_mrgn not in valid:
                diag["prev_or_new_mrgn_missing"] += 1
                continue
            if prev_mrgn == T1_MRGN_MARGIN and new_mrgn == T1_MRGN_LOAN:
                diag["upgrades"] += 1
                rows.append(
                    {
                        "signal_date": signal_date,
                        "code": code,
                        "prev_snap": prev_snap,
                        "new_snap": new_snap,
                        "prev_mrgn": prev_mrgn,
                        "new_mrgn": new_mrgn,
                    }
                )
            elif T1_MRGN_OTHER in (prev_mrgn, new_mrgn) and prev_mrgn != new_mrgn:
                diag["mrgn3_involved"] += 1
            elif prev_mrgn == T1_MRGN_LOAN and new_mrgn == T1_MRGN_MARGIN:
                diag["downgrades"] += 1

    return pd.DataFrame(rows), diag


# --- T2: scalecat_upgrade シグナル生成（新規ロジック） -----------------------------


def generate_scalecat_upgrade_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
) -> tuple[pd.DataFrame, dict]:
    """月末masterの規模区分(ScaleCat)順位が連続月対で増加した銘柄を昇格イベント化する（§7-V T2）。

    順位表(§7-V凍結): Core30=5/Large70=4/Mid400=3/Small1=2/Small2=1/"-"(TOPIX外)=0。
    rank(new)>rank(prev) を昇格＝シグナル。2018-08末→2018-09末対は「TOPIX Small 1」初出の
    一斉再編対として全除外。新規上場（前月不在）・順位表にないラベル・非連続月は非シグナル。
    シグナル日=新スナップショット月の翌月初営業日。
    """
    snap_dates = r32._master_snapshot_dates()

    diag = {
        "consecutive_pairs_total": 0,
        "pairs_in_window": 0,
        "reorg_pairs_excluded": 0,           # 2018-08末→09末の一斉再編対（全除外）
        "non_consecutive_pairs_skipped": 0,
        "upgrades": 0,
        "downgrades": 0,
        "new_listings": 0,
        "prev_or_new_unranked": 0,           # 前月または当月が順位表にないラベル
    }
    rows: list[dict] = []

    for prev_snap, new_snap in zip(snap_dates, snap_dates[1:]):
        prev_ym, new_ym = prev_snap[:6], new_snap[:6]
        if r32._next_month(prev_ym) != new_ym:
            diag["non_consecutive_pairs_skipped"] += 1
            continue
        diag["consecutive_pairs_total"] += 1

        signal_month = r32._next_month(new_ym)
        signal_date = r32._first_bday_of_month(signal_month, all_bdays)
        if signal_date is None or not (start_bd <= signal_date <= end_bd):
            continue
        diag["pairs_in_window"] += 1

        if prev_ym == T2_REORG_PREV_MONTH and new_ym == T2_REORG_NEW_MONTH:
            diag["reorg_pairs_excluded"] += 1
            continue

        prev_master = measure_base_rate.load_master_day(prev_snap)
        new_master = measure_base_rate.load_master_day(new_snap)

        for code, new_rec in new_master.items():
            new_rank = T2_SCALECAT_RANK.get(new_rec.get("ScaleCat"))
            if new_rank is None:
                continue  # 当月が順位表にないラベル＝非イベント
            prev_rec = prev_master.get(code)
            if prev_rec is None:
                diag["new_listings"] += 1
                continue
            prev_rank = T2_SCALECAT_RANK.get(prev_rec.get("ScaleCat"))
            if prev_rank is None:
                diag["prev_or_new_unranked"] += 1
                continue
            if new_rank > prev_rank:
                diag["upgrades"] += 1
                rows.append(
                    {
                        "signal_date": signal_date,
                        "code": code,
                        "prev_snap": prev_snap,
                        "new_snap": new_snap,
                        "prev_scalecat": prev_rec.get("ScaleCat"),
                        "new_scalecat": new_rec.get("ScaleCat"),
                        "prev_rank": prev_rank,
                        "new_rank": new_rank,
                    }
                )
            elif new_rank < prev_rank:
                diag["downgrades"] += 1

    return pd.DataFrame(rows), diag


# --- T3: sales_beat シグナル生成（新規ロジック・sue_beatと完全同型） ----------------


def generate_sales_beat_signals(
    start_bd: str,
    end_bd: str,
    threshold: float = T3_THRESHOLD,
    lookback_days: int = T3_LOOKBACK_DAYS,
) -> tuple[pd.DataFrame, dict, dict]:
    """[start_bd, end_bd]内の決算短信(2Q/FY限定)から売上高が直前会社予想を threshold以上
    上回った開示を抽出する（§7-V T3）。generate_sue_beat_signals と完全同型のas-of機構。

    フィールド対応(凍結): 2Q開示= `Sales`(中間累計実績) 対 `FSales2Q`(直前開示の中間期予想) /
    FY開示= `Sales`(通期実績) 対 `FSales`(直前開示の通期予想)。直前予想≤0は除外・遡り365暦日上限・
    同一CurFYEnのみ「直前予想」候補（すべてsue_beatと同一規約）。

    Returns:
        (signals_df, diag, discdate_index)。discdate_index は (code, period_type, disclosed_date)
        -> cur_fy_en（走査した全2Q/FY開示・SUE突合診断で sue_beat 開示のCurFYEn逆引きに使う）。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    hist_start_bd = (
        kpi_uprev_signals.FINS_HISTORY_START_BD
        if kpi_uprev_signals.FINS_HISTORY_START_BD <= start_bd
        else start_bd
    )
    history_fsales2q, hist_stats_2q = kpi_uprev_signals.build_fop_history(
        hist_start_bd, end_bd, all_bdays, field="FSales2Q", fiscal_year_key=True
    )
    history_fsales, hist_stats_fy = kpi_uprev_signals.build_fop_history(
        hist_start_bd, end_bd, all_bdays, field="FSales", fiscal_year_key=True
    )

    event_days = [d for d in all_bdays if start_bd <= d <= end_bd]

    diag = {
        "history_scanned_days": hist_stats_2q["scanned_days"],
        "history_fsales2q_numeric_records": hist_stats_2q["fop_numeric_records"],
        "history_fsales_numeric_records": hist_stats_fy["fop_numeric_records"],
        "event_days_scanned": len(event_days),
        "total_disclosure_records": 0,
        "statement_2q_or_fy_events": 0,
        "actual_sales_missing_or_nonnumeric": 0,
        "no_prior_forecast_found": 0,
        "current_cur_fy_en_missing": 0,
        "prior_fy_mismatch_excluded": 0,
        "prior_forecast_beyond_lookback": 0,
        "forecast_pair_established": 0,
        "deficit_forecast_excluded": 0,  # 直前予想≤0（売上では稀だが規約統一）
        "signals_sales_beat": 0,
        "signals_2q": 0,
        "signals_fy": 0,
    }
    discdate_index: dict[tuple, str] = {}  # (code, period_type, disclosed_date) -> cur_fy_en
    rows: list[dict] = []

    for d in event_days:
        records = kpi_pead_signals.load_fins_day(d)
        for rec in records:
            diag["total_disclosure_records"] += 1
            code = rec.get("Code")
            doc_type = rec.get("DocType", "") or ""
            if not code:
                continue
            m = T3_DOCTYPE_RE.match(doc_type)
            if not m:
                continue
            diag["statement_2q_or_fy_events"] += 1
            period_prefix = m.group(1)  # "2Q" or "FY"

            cur_fy_en = rec.get("CurFYEn") or ""
            if cur_fy_en:
                # 走査した全2Q/FY開示を discdate_index に登録（同キー複数は最新開示で上書き）。
                discdate_index[(code, period_prefix, d)] = cur_fy_en

            actual_val = kpi_uprev_signals._parse_numeric(rec.get("Sales"))
            if actual_val is None:
                diag["actual_sales_missing_or_nonnumeric"] += 1
                continue

            if period_prefix == "2Q":
                history = history_fsales2q
                forecast_field_name = "FSales2Q"
            else:
                history = history_fsales
                forecast_field_name = "FSales"

            disc_time_raw = rec.get("DiscTime")
            disc_time_minutes = kpi_pead_signals._parse_disc_time_minutes(disc_time_raw)
            cur_key = (d, disc_time_minutes if disc_time_minutes is not None else 0)

            prior_records = [t for t in history.get(code, []) if (t[0], t[1]) < cur_key]
            if not prior_records:
                diag["no_prior_forecast_found"] += 1
                continue
            if not cur_fy_en:
                diag["current_cur_fy_en_missing"] += 1
                continue
            same_fy_prior = [t for t in prior_records if len(t) > 3 and t[3] == cur_fy_en]
            if not same_fy_prior:
                diag["prior_fy_mismatch_excluded"] += 1
                continue
            prior_records = same_fy_prior
            prev_date, prev_time, forecast_before = max(prior_records, key=lambda t: (t[0], t[1]))[:3]
            assert (prev_date, prev_time) < cur_key, (
                f"FATAL: look-ahead検出 code={code} event={cur_key} prev=({prev_date},{prev_time})"
            )

            if kpi_uprev_signals._days_between(prev_date, d) > lookback_days:
                diag["prior_forecast_beyond_lookback"] += 1
                continue

            diag["forecast_pair_established"] += 1

            if forecast_before <= 0:
                diag["deficit_forecast_excluded"] += 1
                continue

            beat_pct = actual_val / forecast_before - 1
            if beat_pct < threshold:
                continue

            g_date, rule = kpi_pead_signals.reaction_day(d, disc_time_minutes, bday_index, all_bdays)
            if g_date is None:
                continue  # 検証期間の終端でカレンダー範囲外

            diag["signals_sales_beat"] += 1
            diag["signals_2q" if period_prefix == "2Q" else "signals_fy"] += 1
            rows.append(
                {
                    "signal_date": g_date,
                    "code": code,
                    "disclosed_date": d,
                    "disc_time": disc_time_raw,
                    "reaction_rule": rule,
                    "doc_type": doc_type,
                    "period_type": period_prefix,
                    "cur_fy_en": cur_fy_en,
                    "forecast_field": forecast_field_name,
                    "sales_actual": actual_val,
                    "sales_forecast_before": forecast_before,
                    "sales_forecast_before_date": prev_date,
                    "beat_pct": beat_pct,
                }
            )

    return pd.DataFrame(rows), diag, discdate_index


def compute_sales_sue_overlap_diagnostic(
    sales_df: pd.DataFrame,
    in_universe_df: pd.DataFrame,
    event_key_map: dict,
    discdate_index: dict,
    start_bd: str,
    end_bd: str,
    base_rate_by_month: dict,
) -> dict:
    """sales_beat 母集団と sue_beat のイベントキー(Code,CurFYEn,period_type)突合診断（§7-V T3）。

    Jaccard係数・period_type別重複率・SUE非重複群の n/EV/lift を返す（診断限定・§7-O同設計）。
    sue_beat の各シグナルは discdate_index[(code, period_type, disclosed_date)] で CurFYEn を逆引き
    してキー化する（sue_beat 出力行は CurFYEn を持たないため）。
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

    sales_keys: set = set()
    sales_keys_by_qtype: dict[str, set] = defaultdict(set)
    for _, r in sales_df.iterrows():
        key = (r["code"], r["cur_fy_en"], r["period_type"])
        sales_keys.add(key)
        sales_keys_by_qtype[r["period_type"]].add(key)

    inter = sales_keys & sue_keys
    union = sales_keys | sue_keys
    jaccard = (len(inter) / len(union)) if union else None
    overlap_by_qtype = {}
    for q in ("2Q", "FY"):
        sak = sales_keys_by_qtype.get(q, set())
        sk = sue_keys_by_qtype.get(q, set())
        u = sak | sk
        overlap_by_qtype[q] = {
            "sales_n": len(sak), "sue_n": len(sk), "intersection": len(sak & sk),
            "union": len(u), "jaccard": (len(sak & sk) / len(u)) if u else None,
        }

    # SUE非重複群（in_universe のうち sue キーに含まれないもの）の成績（診断限定）。
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
        "sales_unique_event_keys": len(sales_keys),
        "intersection": len(inter),
        "union": len(union),
        "jaccard": jaccard,
        "overlap_by_qtype": overlap_by_qtype,
        "non_overlap_subset": non_overlap_stats,
        "sue_gen_signals_total": sue_diag.get("signals_sue_beat"),
    }


# --- 共通: ハーネス実行 + 探索的一次結論 + 台帳記録（round32 run_trial 標準フローを踏襲） ---


def run_trial(
    signals_df: pd.DataFrame,
    kpi_name: str,
    base_params: dict,
    harness_ctx: dict,
    report_extra_lines: Optional[list[str]] = None,
    append_to_ledger: bool = True,
    small_n_reference_threshold: Optional[int] = None,
    event_key_map: Optional[dict] = None,
) -> dict:
    """低レベルCanonical関数を直接呼び出してハーネス実行〜探索的結論〜台帳記録まで行う
    （kpi_round32_signals.run_trial と同一の標準フロー。全試行 defer_entry=True）。

    small_n_reference_threshold が指定され n がそれ未満のとき、judge を呼ばず
    verdict='reference_observation'（§7-C型参考観測）として台帳記録する（§7-V T1/T2の自動格下げ）。
    event_key_map が渡されたとき、返り値に in_universe_df を含める（T3のSUE非重複診断で使う）。
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

    is_reference = small_n_reference_threshold is not None and stats["n"] < small_n_reference_threshold
    if is_reference:
        verdict = "reference_observation"
        reasons = [
            f"n={stats['n']} < {small_n_reference_threshold} のため§7-C型の参考観測へ自動格下げ"
            "（§6 judge未実行・正式判定の対象外・記録のみ）"
        ]
    else:
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
        "master_diff_family_note": MASTER_DIFF_FAMILY_NOTE,
        "entry_missing": diag["entry_missing"],
        "raw_signal_count": diag["raw_signal_count"],
        "duplicate_discarded": diag["duplicate_discarded"],
        "out_of_universe": diag["out_of_universe"],
    }
    if is_reference:
        params["reference_observation"] = True
        params["reference_observation_reason"] = (
            f"n={stats['n']} < {small_n_reference_threshold}（§7-V凍結の自動格下げ）"
        )

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
        f.write("\n## 探索的一次結論（第33周・カタログ§7-V事前登録の共通ルール）\n")
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
        f.write(f"- {MASTER_DIFF_FAMILY_NOTE}\n")
        if report_extra_lines:
            f.write("\n## 近傍事実・必須診断・注記（§7-V）\n")
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
        "in_universe_df": in_universe_df, "is_reference": is_reference,
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


def run_t1(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T1 {T1_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd = generate_mrgn_upgrade_signals(shared["start_bd"], shared["end_bd"], hc["all_bdays"])
    print(
        f"T1 シグナル生成完了: {len(signals_df)}件 (連続月対={gd['consecutive_pairs_total']}, "
        f"窓内対={gd['pairs_in_window']}, 昇格(1→2)={gd['upgrades']}, 降格(2→1)={gd['downgrades']}, "
        f"3絡み={gd['mrgn3_involved']}, 新規上場={gd['new_listings']}, "
        f"Mrgn欠損={gd['prev_or_new_mrgn_missing']}, 非連続対skip={gd['non_consecutive_pairs_skipped']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T1シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T1_KPI_NAME, hc)

    base_params = {
        "signal_definition": (
            "連続する月末masterスナップショット対で Mrgn が 1(信用)→2(貸借) に遷移＝昇格。"
            "3絡みの遷移は対象外(診断カウント)・新規上場/欠損/非連続対は非シグナル。"
            "シグナル日=新スナップショット月の翌月初営業日"
        ),
        "mrgn_codes": "1=信用/2=貸借/3=その他",
        "upgrade_transition": "1->2",
        "small_n_reference_threshold": SMALL_N_REFERENCE_THRESHOLD,
        "reaction": "シグナル確定=T終値・エントリー=T+1寄付",
        "diag_upgrades": gd["upgrades"],
        "diag_downgrades": gd["downgrades"],
        "diag_mrgn3_involved": gd["mrgn3_involved"],
        "diag_new_listings": gd["new_listings"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "power_note": (
            "実施→観測に最大約1ヶ月の遅延があり効果希薄化の可能性(バイアスでなく検定力問題)。"
            "月別件数を必須報告"
        ),
        "neighbor_note": (
            "貸借銘柄入り=空売り可能化・機関の売買自由度向上・信用取引の資金流入の実需イベント。"
            "近傍事実=mkt_upgrade(同型・EV+2.75%・n=33・inconclusive)"
        ),
    }
    report_extra = [
        f"必須診断(T1内訳): 昇格(1→2)={gd['upgrades']} / 降格(2→1・診断のみ)={gd['downgrades']} "
        f"/ 3絡み(対象外)={gd['mrgn3_involved']} / 新規上場(前月不在)={gd['new_listings']} "
        f"/ Mrgn欠損={gd['prev_or_new_mrgn_missing']} / 連続月対={gd['consecutive_pairs_total']}。",
        base_params["power_note"],
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        base_params["neighbor_note"],
        MASTER_DIFF_FAMILY_NOTE,
    ]
    res = run_trial(
        filtered_df, T1_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
        small_n_reference_threshold=SMALL_N_REFERENCE_THRESHOLD,
    )
    res["signals_raw_df"] = signals_df
    res["monthly_counts"] = _monthly_counts(signals_df)
    return res


def run_t2(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T2 {T2_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd = generate_scalecat_upgrade_signals(shared["start_bd"], shared["end_bd"], hc["all_bdays"])
    print(
        f"T2 シグナル生成完了: {len(signals_df)}件 (連続月対={gd['consecutive_pairs_total']}, "
        f"窓内対={gd['pairs_in_window']}, 昇格={gd['upgrades']}, 降格={gd['downgrades']}, "
        f"再編除外対={gd['reorg_pairs_excluded']}, 新規上場={gd['new_listings']}, "
        f"順位表外={gd['prev_or_new_unranked']}, 非連続対skip={gd['non_consecutive_pairs_skipped']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T2シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T2_KPI_NAME, hc)
    monthly_counts = _monthly_counts(signals_df)

    base_params = {
        "signal_definition": (
            "連続する月末masterスナップショット対で ScaleCat の rank(new)>rank(prev)＝昇格。"
            "順位表: Core30=5/Large70=4/Mid400=3/Small1=2/Small2=1/'-'=0。"
            "2018-08末→09末対(Small1初出の一斉再編)・新規上場・順位表外・非連続対は非シグナル。"
            "シグナル日=新スナップショット月の翌月初営業日"
        ),
        "scalecat_rank_table": T2_SCALECAT_RANK,
        "reorg_excluded_pair": f"{T2_REORG_PREV_MONTH}末→{T2_REORG_NEW_MONTH}末(TOPIX Small 1初出)",
        "small_n_reference_threshold": SMALL_N_REFERENCE_THRESHOLD,
        "reaction": "シグナル確定=T終値・エントリー=T+1寄付",
        "diag_upgrades": gd["upgrades"],
        "diag_downgrades": gd["downgrades"],
        "diag_reorg_pairs_excluded": gd["reorg_pairs_excluded"],
        "diag_new_listings": gd["new_listings"],
        "diag_prev_or_new_unranked": gd["prev_or_new_unranked"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "funnel": {
            "raw_upgrades": int(gd["upgrades"]),
            "top500_eligible_prefilter": int(n_filtered),
            "note": "最終nはrun_trial内のin_universe行数(entry_missing差引後)",
        },
        "monthly_cluster": monthly_counts,
        "power_note": (
            "実施→観測に最大約1ヶ月の遅延があり効果希薄化の可能性(バイアスでなく検定力問題)。"
            "月別件数を必須報告"
        ),
        "neighbor_note": (
            "規模区分の上位入り=TOPIX連動パッシブのウェイト増・年金/ETFの機械的買い需要の実需イベント。"
            "近傍事実=mkt_upgrade(同型・EV+2.75%・n=33・inconclusive)・§2指数組入れの実装可能形"
        ),
    }
    report_extra = [
        f"必須診断(T2内訳): 昇格={gd['upgrades']} / 降格(診断のみ)={gd['downgrades']} "
        f"/ 一斉再編除外対(Small1初出2018-08→09)={gd['reorg_pairs_excluded']} "
        f"/ 新規上場(前月不在)={gd['new_listings']} / 順位表外ラベル={gd['prev_or_new_unranked']} "
        f"/ 連続月対={gd['consecutive_pairs_total']}。",
        f"漏斗診断: raw昇格={gd['upgrades']}件 → 前月末TOP500適格(prefilter通過)={n_filtered}件 "
        f"→ 最終n(in_universe)はレポート先頭のn。'-'→Small2以上は小型株中心でユニバース判定の"
        f"大量脱落が予想されるがバイアスではない(Codex㊹注記)。",
        f"月別クラスタ分布(TOPIX定期見直しの年次クラスタは除外しない・それ自体が機構): {monthly_counts}。",
        base_params["power_note"],
        base_params["neighbor_note"],
        MASTER_DIFF_FAMILY_NOTE,
    ]
    res = run_trial(
        filtered_df, T2_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
        small_n_reference_threshold=SMALL_N_REFERENCE_THRESHOLD,
    )
    res["signals_raw_df"] = signals_df
    res["monthly_counts"] = monthly_counts
    return res


def run_t3(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T3 {T3_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd, discdate_index = generate_sales_beat_signals(shared["start_bd"], shared["end_bd"])
    print(
        f"T3 シグナル生成完了: {len(signals_df)}件 (2Q/FY開示={gd['statement_2q_or_fy_events']}, "
        f"年度不一致除外={gd['prior_fy_mismatch_excluded']}, 予想ペア成立={gd['forecast_pair_established']}, "
        f"直前予想≤0除外={gd['deficit_forecast_excluded']}, "
        f"シグナル成立={gd['signals_sales_beat']}(2Q={gd['signals_2q']}/FY={gd['signals_fy']}))",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T3シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T3_KPI_NAME, hc)

    # (signal_date, code) -> イベントキー(SUE非重複群の分割に使う)。prefilter後の行に対応。
    event_key_map = {
        (r["signal_date"], r["code"]): (r["code"], r["cur_fy_en"], r["period_type"])
        for _, r in signals_df.iterrows()
    }

    base_params = {
        "signal_definition": (
            "sue_beatと完全同型のas-of。2Q開示=Sales対FSales2Q / FY開示=Sales対FSales。"
            "beat_pct=Sales実績/直前Sales予想-1 >= +5%。CurPerType in {2Q,FY}限定・同一CurFYEnのみ比較・"
            "直前予想≤0除外・遡り365暦日上限・15:00/15:30境界"
        ),
        "excluded_periods": "1Q/3Q(J-QuantsにFSales1Q/FSales3Qが存在しないため・sue_beatと同一)",
        "beat_threshold": T3_THRESHOLD,
        "lookback_days": T3_LOOKBACK_DAYS,
        "reaction_cutoff": "15:00",
        "fiscal_year_key": True,
        "diag_forecast_pair_established": gd["forecast_pair_established"],
        "diag_prior_fy_mismatch_excluded": gd["prior_fy_mismatch_excluded"],
        "diag_deficit_forecast_excluded": gd["deficit_forecast_excluded"],
        "diag_signals_2q": gd["signals_2q"],
        "diag_signals_fy": gd["signals_fy"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "family_note": (
            "「実績対予想ビート」家族の2本目(1本目=sue_beat: lift1.16・EV+1.46%[+0.17,+2.84]・枠F配線済み)。"
            "SUEとのJaccard・SUE非重複群成績を必須診断(§7-O margin_expandと同じ診断設計)"
        ),
    }
    report_extra = [
        f"必須診断(T3): 2Q/FY開示={gd['statement_2q_or_fy_events']} / 予想ペア成立={gd['forecast_pair_established']} "
        f"/ 年度不一致除外={gd['prior_fy_mismatch_excluded']} / 直前予想≤0除外={gd['deficit_forecast_excluded']} "
        f"/ シグナル成立={gd['signals_sales_beat']}(2Q={gd['signals_2q']}/FY={gd['signals_fy']})。",
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        base_params["family_note"],
    ]
    res = run_trial(
        filtered_df, T3_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
        event_key_map=event_key_map,
    )
    # SUE突合診断（診断限定・台帳のverdictには影響しない）。
    diag_overlap = compute_sales_sue_overlap_diagnostic(
        signals_df, res["in_universe_df"], event_key_map, discdate_index,
        shared["start_bd"], shared["end_bd"], hc["base_rate_by_month"],
    )
    res["sue_overlap"] = diag_overlap
    res["signals_raw_df"] = signals_df

    # SUE突合診断をレポートに追記（run_trial 完了後）。
    report_path = OUTPUT_ROOT / T3_KPI_NAME / "report.md"
    nos = diag_overlap["non_overlap_subset"]
    with open(report_path, "a", encoding="utf-8") as f:
        f.write("\n## SUE突合診断（診断限定・§7-V T3・イベントキー=(Code,CurFYEn,period_type)）\n")
        f.write(
            f"- Jaccard(sales_beat×sue_beat) = {diag_overlap['jaccard']} "
            f"(積集合={diag_overlap['intersection']} / 和集合={diag_overlap['union']} / "
            f"sales固有キー={diag_overlap['sales_unique_event_keys']} / "
            f"sueキー={diag_overlap['sue_unique_event_keys']} / sue生シグナル={diag_overlap['sue_signal_count']} / "
            f"sueキー逆引き不能={diag_overlap['sue_unmapped_fy_en']})\n"
        )
        for q, ov in diag_overlap["overlap_by_qtype"].items():
            f.write(
                f"- period_type={q}: sales_n={ov['sales_n']} sue_n={ov['sue_n']} "
                f"積集合={ov['intersection']} Jaccard={ov['jaccard']}\n"
            )
        f.write(
            f"- **SUE非重複群(in_universeのうちsueキーに含まれない)の成績: n={nos['n']} "
            f"lift={nos['point_lift']} EV(なし)点推定={nos['ev_point']} "
            f"CI95=[{nos['ev_ci_low']}, {nos['ev_ci_high']}]**（sue_beatと独立な売上ビート固有の効果）\n"
        )
    print(
        f"T3 SUE突合診断: Jaccard={diag_overlap['jaccard']} 積集合={diag_overlap['intersection']} "
        f"SUE非重複群 n={nos['n']} lift={nos['point_lift']} EV={nos['ev_point']}",
        file=sys.stderr,
    )
    return res


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
        description="第33周: master属性変化家族の残り二本+売上ビート（カタログ§7-V・T1〜T3）"
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
            f"§7-Vはin-sample({kpi_pead_signals.IN_SAMPLE_END}まで)で評価します。holdoutは使用しません。"
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

    results = []
    for t in trials_to_run:
        results.append(TRIAL_RUNNERS[t](shared))

    print("\n=== 第33周バッチ完了サマリー ===")
    for r in results:
        extra = ""
        if r["kpi_name"] in (T1_KPI_NAME, T2_KPI_NAME) and "monthly_counts" in r:
            extra = f" 月別件数={r['monthly_counts']}"
        if r["kpi_name"] == T3_KPI_NAME and "sue_overlap" in r:
            ov = r["sue_overlap"]
            nos = ov["non_overlap_subset"]
            extra = (
                f" Jaccard={ov['jaccard']} 積集合={ov['intersection']} "
                f"SUE非重複群(n={nos['n']}/lift={nos['point_lift']}/EV={nos['ev_point']})"
            )
        print(
            f"{r['kpi_name']}: n={r['n']} 月平均n={r.get('avg_monthly_n')} "
            f"lift={r['lift']}[{r['ci_low']},{r['ci_high']}] "
            f"EV(なし)={r['ev_point']}[{r['ev_ci_low']},{r['ev_ci_high']}] EV(stop8)={r['ev_stop8']} "
            f"verdict={r['verdict']} 一次結論={r['conclusion']} "
            f"entry_missing={r['entry_missing']}/raw={r['raw_signal_count']}{extra}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
