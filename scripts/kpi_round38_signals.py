#!/usr/bin/env python3
"""第38周: 新規軸二本 — 分割実施・決算読み替え（カタログ§7-AA・T1〜T2）。

docs/stock-algo-kpi-catalog.md §7-AA の事前登録定義（凍結・Codex55+文言修正GO）を実装する。
2試行とも in-sample期間（2016-11〜2022-11・holdout 2023年以降は使わない）で実行し、共通の
探索的一次結論ルール（第18周基準）を適用する。実装の流儀は scripts/kpi_round37_signals.py /
scripts/kpi_round33_signals.py の run_trial 標準フロー・prefilter_in_universe・複数trialドライバを
踏襲する。参考観測自動格下げ（n<30）は kpi_round33_signals.run_trial と同一実装。

Canonical Module再利用（新規ロジックはT1/T2の生成関数のみ）:
- T1 `split_execution`: 営業日Dの bars で 0<AdjFactor(D)≤0.5（1:2以上の分割・実施日）のみを
  シグナル化する。シグナル確定=D終値・エントリー=D+1寄付。AdjFactor>1（併合）は対象外・診断
  カウント。分割前 raw C(D-1) の価格帯分布は診断限定。bars読込は measure_base_rate.load_bars_day
  を再利用。**n<30 なら参考観測へ自動格下げ**（第32周T1・§7-V T1と同一規則）。
- T2 `earnings_spillover`: leaderは kpi_round33_signals.generate_sales_beat_signals の出力
  （§7-V凍結値・sales_beat・反応日=signal_date=D）をそのまま再利用する。候補は同一S33業種
  （D時点master=PIT直近月末master≤D・普通株=ProdCat"011"・leader除外）で当該会計期を未発表の
  銘柄。未発表判定3条件（(i)候補as-of最新開示のCurFYEn==leaderのCurFYEn / (ii)leader 2Qなら
  候補最新開示段階1Q以前・FYなら3Q以前 / (iii)同一(Code,CurFYEn,period_type)開示がreaction_day
  基準でD以前に不存在）∩ AdjC(D)/AdjC(D-1)−1 ∈[-1%,+2%]（第29周凍結値再利用）。候補×Dは1回。
  S33業種マップは kpi_round29_signals._pit_sector_map、普通株集合は kpi_round23_signals.
  _pit_prodcat011_set を再利用。候補の開示段階履歴は kpi_pead_signals.load_fins_day/reaction_day/
  _parse_disc_time_minutes を用いて新規構築する（as-of最新開示・reaction_day基準のPIT）。
- ユニバース事前フィルタ: kpi_round23_signals.prefilter_in_universe（統計結果不変・第20周§7-I前例）。
- フォワードリターン・重複除去・集計・§6判定・レポート・台帳: kpi_event_study の
  compute_signal_returns/compute_stats/judge/bootstrap_ev_ci/write_report_md/append_trial を再利用。
- 探索的一次結論ルール: kpi_event_batch_signals.classify_exploratory を再利用。

2試行とも defer_entry=True（§6手順6の第5周以降既定方針＝S高で買えない日は翌日繰り延べ）。

家族の割引解釈（§7-AA凍結・Codex55裁定）: earnings_spillover は industry_spillover_family =
{sector_sympathy_volshock[第29周], earnings_spillover} の2本目として割引宣言（同一in-sample期間の
第3変種禁止）。sector_sympathy（volshockトリガー・lift0.91 fail）とは伝播する情報の質が異なる
（出来高ショック vs ファンダ実績）。fins_unused_family には非抵触（凍結済み sales_beat の他銘柄
イベント化・新規フィールド採掘ではない）。split_execution は AdjFactor を90試行で未使用の
イベントトリガーとして初採用（家族割引なし・多重試行割引のみ）。

Usage:
    python3 scripts/kpi_round38_signals.py --trial all
    python3 scripts/kpi_round38_signals.py --trial t1
    python3 scripts/kpi_round38_signals.py --trial all --start 2017-01 --end 2017-12 --no-trials-append
"""
from __future__ import annotations

import argparse
import bisect
import re
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import kpi_event_batch_signals  # noqa: E402  (Canonical Module: classify_exploratory を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: compute_signal_returns/compute_stats/judge/
# write_report_md/append_trial/bootstrap_ev_ci を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・MONTH_RE・load_fins_day・
# reaction_day・_parse_disc_time_minutes を再利用)
import kpi_round23_signals  # noqa: E402  (Canonical Module: prefilter_in_universe/_pit_prodcat011_set を再利用)
import kpi_round29_signals  # noqa: E402  (Canonical Module: _pit_sector_map=PIT S33業種マップを再利用)
import kpi_round33_signals  # noqa: E402  (Canonical Module: generate_sales_beat_signals=leader生成を再利用)
import kpi_uprev_signals  # noqa: E402  (Canonical Module: FINS_HISTORY_START_BD を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読込を再利用)

PERIOD = (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_ROOT = Path("output/kpi")

# --- 事前登録パラメータ（カタログ§7-AA・事後変更禁止） ----------------------------

SMALL_N_REFERENCE_THRESHOLD = 30  # T1: n<30 なら§7-C型参考観測へ自動格下げ（第32周T1・§7-V T1と同一）

# T1: split_execution（株式分割の実施日イベント）。
T1_KPI_NAME = "split_execution"
T1_ADJFACTOR_MAX = 0.5  # 0 < AdjFactor(D) ≤ 0.5（1:2以上の分割・実施日）

# T2: earnings_spillover（同業種先行決算の読み替え買い）。
T2_KPI_NAME = "earnings_spillover"
T2_DAY_RET_MIN = -0.01  # 候補の当日変動下限（第29周凍結値再利用）
T2_DAY_RET_MAX = 0.02   # 候補の当日変動上限（第29周凍結値再利用）
# 決算開示段階のランク（1Q<2Q<3Q<FY）。leaderの period_type は sales_beat 由来で 2Q/FY のみ。
STAGE_RE = re.compile(r"^(1Q|2Q|3Q|FY)FinancialStatements")
STAGE_RANK = {"1Q": 1, "2Q": 2, "3Q": 3, "FY": 4}
# leader段階ごとの「候補の最新開示段階」上限（条件(ii)）。2Q→1Q以前(≤1) / FY→3Q以前(≤3)。
T2_MAX_CANDIDATE_STAGE_RANK = {"2Q": 1, "FY": 3}

ROUND_TAG = "38_split_earnings_spillover"

MULTI_TRIAL_NOTE = (
    "本ラウンドはT1〜T2の2試行同時登録であり累積試行数割引の対象。"
    "この結果単独で運用変更しない。"
)

SPLIT_NEIGHBOR_NOTE = (
    "split_executionはAdjFactorを90試行で未使用のイベントトリガーとして初採用（家族割引なし・"
    "多重試行割引のみ）。発表日データ非保有のため§2で眠っていた仮説の『実施日から取れる後半部分』"
    "（投資単位低下→個人資金流入・実施後10営業日残存・大和総研）を検証。分割前raw C(D-1)価格帯分布は"
    "診断限定（価格水準ガードは撤廃＝恣意的新規閾値回避・2018年以前は100株単元が一律でないため）。"
)

INDUSTRY_SPILLOVER_FAMILY_NOTE = (
    "industry_spillover_family={sector_sympathy_volshock[第29周], earnings_spillover}で固定・同一"
    "in-sample期間の第3変種禁止（Codex55裁定）。sector_sympathy（volshockトリガー・lift0.91 fail）とは"
    "伝播する情報の質が異なる（出来高ショック vs ファンダ実績）。fins_unused_familyには非抵触"
    "（凍結済み sales_beat の他銘柄イベント化・新規フィールド採掘ではない）。本家族の結果は統計的確認"
    "ではなく前向き候補の優先順位付けに限定する（家族単位の割引解釈）。"
)

DEFER_RATIONALE = (
    "§7-AA各試行はエントリー=T+1寄付。§6手順6『S高で買えない日は翌日繰り延べ(第5周以降の既定方針)』"
    "に従いdefer_entry=True。分割実施日の需給・決算読み替えの先回り買いいずれもT+1寄付がS高張り付きで"
    "買えないことがあり得るため通常の繰延で扱う。"
)


# --- T1: split_execution シグナル生成（新規ロジック） ------------------------------


def _price_band(raw_close: Optional[float]) -> str:
    """raw終値の価格帯バケット（診断限定・分割前 raw C(D-1) の分布記録用）。"""
    if raw_close is None or not isinstance(raw_close, (int, float)) or raw_close != raw_close:
        return "missing"
    if raw_close < 500:
        return "0-500"
    if raw_close < 1000:
        return "500-1000"
    if raw_close < 2000:
        return "1000-2000"
    if raw_close < 3000:
        return "2000-3000"
    if raw_close < 5000:
        return "3000-5000"
    if raw_close < 10000:
        return "5000-10000"
    return ">=10000"


def generate_split_execution_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]内の全営業日・全銘柄から株式分割実施日シグナルを生成する（§7-AA T1）。

    営業日Dの bars で 0<AdjFactor(D)≤0.5（1:2以上の分割・実施日）をシグナルとする。
    シグナル確定=D終値・エントリー=D+1寄付。AdjFactor>1（併合）は対象外・診断カウント。
    分割前 raw C(D-1) の価格帯分布は診断限定で記録する（価格水準ガードは撤廃）。
    """
    event_days = [d for d in all_bdays if start_bd <= d <= end_bd]

    diag = {
        "business_days_scanned": 0,
        "code_day_observations": 0,
        "adjfactor_missing": 0,           # AdjFactor欠損/非数値
        "adjfactor_invalid_nonpositive": 0,  # AdjFactor≤0（不正）
        "merger_events": 0,               # AdjFactor>1（併合・対象外・診断）
        "small_split_nonsignal": 0,       # 0.5<AdjFactor<1（1:2未満の分割・非シグナル・診断）
        "signals_split_execution": 0,     # 0<AdjFactor≤0.5（1:2以上の分割・実施日）
        "prev_close_missing_on_signal": 0,
    }
    factor_dist: dict[float, int] = defaultdict(int)  # シグナルのAdjFactor値分布（round3）
    price_band_dist: dict[str, int] = defaultdict(int)  # 分割前raw C(D-1)価格帯分布（診断限定）
    rows: list[dict] = []

    for d in event_days:
        diag["business_days_scanned"] += 1
        idx = bday_index[d]
        bars_d = measure_base_rate.load_bars_day(d)
        prev = all_bdays[idx - 1] if idx >= 1 else None
        bars_prev = measure_base_rate.load_bars_day(prev) if prev is not None else {}

        for code, rec in bars_d.items():
            diag["code_day_observations"] += 1
            f = rec.get("AdjFactor")
            if f is None or not isinstance(f, (int, float)) or f != f:
                diag["adjfactor_missing"] += 1
                continue
            if f <= 0:
                diag["adjfactor_invalid_nonpositive"] += 1
                continue
            if f > 1:
                diag["merger_events"] += 1
                continue
            if f > T1_ADJFACTOR_MAX:  # 0.5 < f <= 1（1:2未満の分割・非シグナル）
                if f < 1:
                    diag["small_split_nonsignal"] += 1
                continue
            # 0 < f <= 0.5 = シグナル（1:2以上の分割・実施日）。
            diag["signals_split_execution"] += 1
            factor_dist[round(float(f), 3)] += 1
            prev_rec = bars_prev.get(code)
            raw_prev_close = prev_rec.get("C") if prev_rec else None
            if raw_prev_close is None:
                diag["prev_close_missing_on_signal"] += 1
            band = _price_band(raw_prev_close)
            price_band_dist[band] += 1
            rows.append(
                {
                    "signal_date": d,
                    "code": code,
                    "adj_factor": float(f),
                    "raw_prev_close": raw_prev_close,
                    "prev_close_band": band,
                }
            )

    diag["adjfactor_signal_distribution"] = dict(sorted(factor_dist.items()))
    diag["prev_close_price_band_distribution"] = dict(sorted(price_band_dist.items()))
    return pd.DataFrame(rows), diag


# --- T2: earnings_spillover シグナル生成（新規ロジック） ---------------------------


def build_disclosure_stage_history(
    hist_start_bd: str,
    hist_end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
) -> tuple[dict[str, list[tuple]], dict]:
    """[hist_start_bd, hist_end_bd]内の全決算短信(1Q/2Q/3Q/FY)からCode別の開示段階履歴を構築する。

    各開示につき reaction_day（§2-E/§7-J準拠・kpi_pead_signals.reaction_day）を計算し、
    Code別に (reaction_day, stage_rank, stage_str, cur_fy_en, disclosed_date) を
    reaction_day昇順（同reaction_dayは disclosed_date→stage_rank昇順）でソートして返す。
    条件(i)/(ii)/(iii)の「候補as-of最新開示」判定に使う。
    """
    history: dict[str, list[tuple]] = defaultdict(list)
    stats = {
        "scanned_days": 0,
        "statement_records": 0,
        "reaction_out_of_range": 0,  # reaction_dayがカレンダー範囲外（末端）
        "missing_cur_fy_en": 0,
    }
    scan_days = [d for d in all_bdays if hist_start_bd <= d <= hist_end_bd]
    stats["scanned_days"] = len(scan_days)
    for d in scan_days:
        records = kpi_pead_signals.load_fins_day(d)
        for rec in records:
            code = rec.get("Code")
            if not code:
                continue
            m = STAGE_RE.match(rec.get("DocType", "") or "")
            if not m:
                continue
            stats["statement_records"] += 1
            stage_str = m.group(1)
            stage_rank = STAGE_RANK[stage_str]
            cur_fy_en = rec.get("CurFYEn") or ""
            if not cur_fy_en:
                stats["missing_cur_fy_en"] += 1
            disc_time_minutes = kpi_pead_signals._parse_disc_time_minutes(rec.get("DiscTime"))
            g_date, _rule = kpi_pead_signals.reaction_day(d, disc_time_minutes, bday_index, all_bdays)
            if g_date is None:
                stats["reaction_out_of_range"] += 1
                continue
            history[code].append((g_date, stage_rank, stage_str, cur_fy_en, d))
    for code in history:
        history[code].sort(key=lambda t: (t[0], t[4], t[1]))
    return history, stats


def _asof_records(hist_list: list[tuple], day: str) -> list[tuple]:
    """Code開示履歴（reaction_day昇順ソート済み）から reaction_day ≤ day の部分列を返す。"""
    # reaction_day は各タプルの [0]。bisect で reaction_day <= day の右端を求める。
    keys = [t[0] for t in hist_list]
    right = bisect.bisect_right(keys, day)
    return hist_list[:right]


def _candidate_unpublished(
    asof: list[tuple], leader_cur_fy_en: str, leader_stage: str
) -> Optional[str]:
    """候補が leader の会計期を未発表かを3条件で判定する。適格なら None、不適格なら失敗理由キー。

    asof: 候補の reaction_day ≤ D の開示履歴（reaction_day昇順）。
    条件(i): 最新as-of開示のCurFYEn == leaderのCurFYEn。
    条件(ii): 最新as-of開示のstage_rank ≤ T2_MAX_CANDIDATE_STAGE_RANK[leader_stage]。
    条件(iii): 同一(CurFYEn==leaderのCurFYEn, stage_str==leader_stage)開示がas-of内に不存在。
    """
    if not asof:
        return "cond_i_no_asof_disclosure"
    latest = asof[-1]  # (reaction_day, stage_rank, stage_str, cur_fy_en, disclosed_date)
    if latest[3] != leader_cur_fy_en:
        return "cond_i_fy_mismatch"
    max_rank = T2_MAX_CANDIDATE_STAGE_RANK[leader_stage]
    if latest[1] > max_rank:
        return "cond_ii_stage_too_late"
    for t in asof:
        if t[3] == leader_cur_fy_en and t[2] == leader_stage:
            return "cond_iii_same_period_disclosed"
    return None


def generate_earnings_spillover_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
) -> tuple[pd.DataFrame, dict, pd.DataFrame]:
    """[start_bd, end_bd]内から同業種先行決算の読み替え買いシグナルを生成する（§7-AA T2）。

    leader = kpi_round33_signals.generate_sales_beat_signals の出力（sales_beat・反応日=signal_date=D）。
    候補 = 同一S33業種（D時点master≤D・普通株ProdCat"011"・leader除外）で当該会計期を未発表
    （3条件）∩ AdjC(D)/AdjC(D-1)−1 ∈[-1%,+2%]。候補×Dは1回。

    Returns:
        (signals_df, diag, leader_df)。leader_df は sales_beat 生シグナル（Jaccard診断に使う）。
    """
    # leader（sales_beat・§7-V凍結値）をそのまま再利用。
    # Codex56修正: 反応日境界ガード。開示走査を1営業日前倒し（start_bd前日開示の反応日=start_bd の
    # leaderを取りこぼさない）し、生成後の leader を start_bd ≤ signal_date ≤ end_bd に明示限定する
    # （end_bd開示の反応日が翌月へはみ出す凍結期間逸脱を除外）。
    idx_start = bday_index[start_bd]
    leader_scan_start = all_bdays[idx_start - 1] if idx_start >= 1 else start_bd
    leader_df, _sb_diag, _discidx = kpi_round33_signals.generate_sales_beat_signals(
        leader_scan_start, end_bd
    )
    leader_raw_count = int(len(leader_df))
    if not leader_df.empty:
        leader_df = leader_df[
            (leader_df["signal_date"] >= start_bd) & (leader_df["signal_date"] <= end_bd)
        ].reset_index(drop=True)
    leader_boundary_filtered = leader_raw_count - int(len(leader_df))

    # 候補の開示段階履歴（fins先頭日〜end_bd。reaction_day基準のPITで as-of 判定）。
    hist_start_bd = (
        kpi_uprev_signals.FINS_HISTORY_START_BD
        if kpi_uprev_signals.FINS_HISTORY_START_BD <= start_bd
        else start_bd
    )
    disc_history, hist_stats = build_disclosure_stage_history(
        hist_start_bd, end_bd, all_bdays, bday_index
    )

    diag = {
        "leader_signals_total": int(len(leader_df)),
        "leader_scan_start_bd": leader_scan_start,
        "leader_raw_count_prefilter": leader_raw_count,
        "leader_boundary_filtered": leader_boundary_filtered,
        "leader_days": 0,
        "leader_count_max": 0,
        "leader_count_mean": None,
        "leader_sector_missing": 0,       # leaderのS33業種がPIT masterで欠損（延べ）
        "candidates_evaluated": 0,        # (leader×候補) 評価延べ
        "cond_i_no_asof_disclosure": 0,
        "cond_i_fy_mismatch": 0,
        "cond_ii_stage_too_late": 0,
        "cond_iii_same_period_disclosed": 0,
        "price_band_fail": 0,             # 3条件通過だが当日変動が[-1%,+2%]外
        "price_data_missing": 0,          # AdjC(D)/AdjC(D-1)不能
        "history_scanned_days": hist_stats["scanned_days"],
        "history_statement_records": hist_stats["statement_records"],
        "history_missing_cur_fy_en": hist_stats["missing_cur_fy_en"],
        "signals_earnings_spillover": 0,
    }

    # leaderを反応日Dごとにまとめる。
    leaders_by_day: dict[str, list[dict]] = defaultdict(list)
    for _, r in leader_df.iterrows():
        leaders_by_day[str(r["signal_date"])].append(
            {"code": str(r["code"]), "cur_fy_en": r["cur_fy_en"], "period_type": r["period_type"]}
        )

    leader_counts: list[int] = []
    fired: dict[tuple, dict] = {}  # (code, D) -> シグナル行（候補×Dは1回）

    for d in sorted(leaders_by_day.keys()):
        leaders = leaders_by_day[d]
        n_leaders = len(leaders)
        leader_counts.append(n_leaders)
        diag["leader_days"] += 1
        diag["leader_count_max"] = max(diag["leader_count_max"], n_leaders)

        sector_map = kpi_round29_signals._pit_sector_map(d)   # Code -> S33（PIT ≤ D）
        prodcat011 = kpi_round23_signals._pit_prodcat011_set(d)  # 普通株集合（PIT ≤ D）
        idx = bday_index[d]
        prev = all_bdays[idx - 1] if idx >= 1 else None
        bars_d = measure_base_rate.load_bars_day(d)
        bars_prev = measure_base_rate.load_bars_day(prev) if prev is not None else {}

        # 業種→候補コード（普通株のみ）を1回だけ構築（当日のsector_map基準）。
        sector_members: dict[str, list[str]] = defaultdict(list)
        for code, sec in sector_map.items():
            if sec and code in prodcat011:
                sector_members[sec].append(code)

        for leader in leaders:
            leader_code = leader["code"]
            leader_sector = sector_map.get(leader_code)
            if not leader_sector:
                diag["leader_sector_missing"] += 1
                continue
            leader_fy = leader["cur_fy_en"]
            leader_stage = leader["period_type"]  # sales_beat 由来 = "2Q"/"FY"
            for code in sector_members.get(leader_sector, []):
                if code == leader_code:
                    continue
                if (code, d) in fired:
                    continue  # 既に別leaderで発火済み（候補×Dは1回）
                diag["candidates_evaluated"] += 1
                asof = _asof_records(disc_history.get(code, []), d)
                fail = _candidate_unpublished(asof, leader_fy, leader_stage)
                if fail is not None:
                    diag[fail] += 1
                    continue
                rec_d = bars_d.get(code)
                prev_rec = bars_prev.get(code)
                adjc_d = rec_d.get("AdjC") if rec_d else None
                adjc_prev = prev_rec.get("AdjC") if prev_rec else None
                if adjc_d is None or adjc_prev is None or adjc_prev <= 0:
                    diag["price_data_missing"] += 1
                    continue
                day_ret = adjc_d / adjc_prev - 1
                if not (T2_DAY_RET_MIN <= day_ret <= T2_DAY_RET_MAX):
                    diag["price_band_fail"] += 1
                    continue
                diag["signals_earnings_spillover"] += 1
                fired[(code, d)] = {
                    "signal_date": d,
                    "code": code,
                    "sector": leader_sector,
                    "leader_code": leader_code,
                    "leader_cur_fy_en": leader_fy,
                    "leader_period_type": leader_stage,
                    "n_leaders": n_leaders,
                    "candidate_latest_stage": asof[-1][2],
                    "day_ret": day_ret,
                }

    if leader_counts:
        diag["leader_count_mean"] = round(sum(leader_counts) / len(leader_counts), 2)

    signals_df = pd.DataFrame(list(fired.values()))
    if not signals_df.empty:
        cluster = signals_df.groupby("signal_date").size()
        diag["same_day_fire_max"] = int(cluster.max())
    else:
        diag["same_day_fire_max"] = 0
    return signals_df, diag, leader_df


# --- 共通: ハーネス実行 + 探索的一次結論 + 台帳記録（round33 run_trial 標準フローを踏襲） ---


def run_trial(
    signals_df: pd.DataFrame,
    kpi_name: str,
    base_params: dict,
    harness_ctx: dict,
    report_extra_lines: Optional[list[str]] = None,
    append_to_ledger: bool = True,
    small_n_reference_threshold: Optional[int] = None,
) -> dict:
    """低レベルCanonical関数を直接呼び出してハーネス実行〜探索的結論〜台帳記録まで行う
    （kpi_round33_signals.run_trial と同一の標準フロー。全試行 defer_entry=True）。

    small_n_reference_threshold が指定され n がそれ未満のとき、judge を呼ばず
    verdict='reference_observation'（§7-C型参考観測）として台帳記録する（§7-AA T1の自動格下げ）。
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
        "entry_missing": diag["entry_missing"],
        "raw_signal_count": diag["raw_signal_count"],
        "duplicate_discarded": diag["duplicate_discarded"],
        "out_of_universe": diag["out_of_universe"],
    }
    if is_reference:
        params["reference_observation"] = True
        params["reference_observation_reason"] = (
            f"n={stats['n']} < {small_n_reference_threshold}（§7-AA凍結の自動格下げ）"
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
        f.write("\n## 探索的一次結論（第38周・カタログ§7-AA事前登録の共通ルール）\n")
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
        if report_extra_lines:
            f.write("\n## 近傍事実・必須診断・注記（§7-AA）\n")
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


def _event_key_set(signals_df: pd.DataFrame) -> set:
    """(code, signal_date) のイベントキー集合。"""
    if signals_df.empty:
        return set()
    return set(zip(signals_df["code"], signals_df["signal_date"]))


def _jaccard(a: set, b: set) -> Optional[float]:
    u = a | b
    return (len(a & b) / len(u)) if u else None


def run_t1(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T1 {T1_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd = generate_split_execution_signals(
        shared["start_bd"], shared["end_bd"], hc["all_bdays"], hc["bday_index"]
    )
    print(
        f"T1 シグナル生成完了: {len(signals_df)}件 (営業日走査={gd['business_days_scanned']}, "
        f"銘柄日観測={gd['code_day_observations']}, 併合(>1)={gd['merger_events']}, "
        f"小分割(0.5<f<1)={gd['small_split_nonsignal']}, AdjFactor欠損={gd['adjfactor_missing']}, "
        f"AdjFactor値分布={gd['adjfactor_signal_distribution']}, "
        f"分割前価格帯={gd['prev_close_price_band_distribution']}, "
        f"シグナル成立={gd['signals_split_execution']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T1シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T1_KPI_NAME, hc)
    monthly_counts = _monthly_counts(signals_df)

    base_params = {
        "signal_definition": (
            "営業日Dの bars で 0<AdjFactor(D)≤0.5(1:2以上の分割・実施日)のみ。"
            "AdjFactor>1(併合)は対象外・診断。価格水準ガードは撤廃(分割前raw C(D-1)価格帯分布は診断限定)。"
            "シグナル確定=D終値・エントリー=D+1寄付"
        ),
        "adjfactor_max": T1_ADJFACTOR_MAX,
        "small_n_reference_threshold": SMALL_N_REFERENCE_THRESHOLD,
        "reaction": "シグナル確定=D終値・エントリー=D+1寄付",
        "diag_merger_events": gd["merger_events"],
        "diag_small_split_nonsignal": gd["small_split_nonsignal"],
        "diag_adjfactor_missing": gd["adjfactor_missing"],
        "diag_adjfactor_invalid_nonpositive": gd["adjfactor_invalid_nonpositive"],
        "diag_adjfactor_signal_distribution": gd["adjfactor_signal_distribution"],
        "diag_prev_close_price_band_distribution": gd["prev_close_price_band_distribution"],
        "diag_prev_close_missing_on_signal": gd["prev_close_missing_on_signal"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "monthly_cluster": monthly_counts,
        "neighbor_note": SPLIT_NEIGHBOR_NOTE,
    }
    report_extra = [
        f"必須診断(T1): シグナル(0<f≤0.5)={gd['signals_split_execution']} / 併合(f>1)={gd['merger_events']} "
        f"/ 小分割(0.5<f<1)={gd['small_split_nonsignal']} / AdjFactor欠損={gd['adjfactor_missing']} "
        f"/ AdjFactor不正(≤0)={gd['adjfactor_invalid_nonpositive']}。",
        f"AdjFactor値分布(シグナル・round3): {gd['adjfactor_signal_distribution']}。",
        f"分割前raw C(D-1)価格帯分布(診断限定): {gd['prev_close_price_band_distribution']} "
        f"(価格帯欠損onシグナル={gd['prev_close_missing_on_signal']})。",
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        f"月別クラスタ分布: {monthly_counts}。",
        SPLIT_NEIGHBOR_NOTE,
    ]
    res = run_trial(
        filtered_df, T1_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
        small_n_reference_threshold=SMALL_N_REFERENCE_THRESHOLD,
    )
    res["monthly_counts"] = monthly_counts
    return res


def run_t2(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T2 {T2_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd, leader_df = generate_earnings_spillover_signals(
        shared["start_bd"], shared["end_bd"], hc["all_bdays"], hc["bday_index"]
    )
    print(
        f"T2 シグナル生成完了: {len(signals_df)}件 (leader総数={gd['leader_signals_total']}"
        f"(境界ガード前={gd['leader_raw_count_prefilter']}/期間外除外={gd['leader_boundary_filtered']}), "
        f"leader日数={gd['leader_days']}, leader数[mean/max]=[{gd['leader_count_mean']}/{gd['leader_count_max']}], "
        f"候補評価={gd['candidates_evaluated']}, "
        f"cond_i欠損/年度不一致={gd['cond_i_no_asof_disclosure']}/{gd['cond_i_fy_mismatch']}, "
        f"cond_ii段階超過={gd['cond_ii_stage_too_late']}, cond_iii既発表={gd['cond_iii_same_period_disclosed']}, "
        f"価格帯外={gd['price_band_fail']}, 価格欠損={gd['price_data_missing']}, "
        f"同日発火最大={gd['same_day_fire_max']}, シグナル成立={gd['signals_earnings_spillover']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T2シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T2_KPI_NAME, hc)
    monthly_counts = _monthly_counts(signals_df)

    # Jaccard診断: sales_beat（leader自身）と sector_sympathy_volshock（第29周family同胞）。
    my_keys = _event_key_set(signals_df)
    sales_keys = _event_key_set(leader_df)
    print("T2 sector_sympathy_volshock(Jaccard診断用)を生成中...", file=sys.stderr)
    ss_df, _ss_diag = kpi_round29_signals.generate_sector_sympathy_signals(
        shared["start_bd"], shared["end_bd"], hc["all_bdays"], hc["bday_index"], hc["universes_by_month"]
    )
    ss_keys = _event_key_set(ss_df)
    jac_sales = {
        "my_keys": len(my_keys), "sales_beat_keys": len(sales_keys),
        "intersection": len(my_keys & sales_keys), "union": len(my_keys | sales_keys),
        "jaccard": _jaccard(my_keys, sales_keys),
    }
    jac_ss = {
        "my_keys": len(my_keys), "sector_sympathy_keys": len(ss_keys),
        "intersection": len(my_keys & ss_keys), "union": len(my_keys | ss_keys),
        "jaccard": _jaccard(my_keys, ss_keys),
    }

    base_params = {
        "signal_definition": (
            "leader=sales_beat(§7-V凍結値・反応日=signal_date=D)。候補=同一S33業種(D時点master≤D・"
            "普通株ProdCat'011'・leader除外)で当該会計期を未発表。未発表判定3条件: "
            "(i)候補as-of最新開示CurFYEn==leaderのCurFYEn / (ii)leader 2Qなら候補最新開示段階1Q以前・"
            "FYなら3Q以前 / (iii)同一(Code,CurFYEn,period_type)開示がreaction_day基準でD以前に不存在。"
            f"かつ候補当日変動AdjC(D)/AdjC(D-1)−1∈[{T2_DAY_RET_MIN:+.0%},{T2_DAY_RET_MAX:+.0%}]。候補×Dは1回"
        ),
        "leader_source": "kpi_round33_signals.generate_sales_beat_signals(§7-V凍結値・2Q=Sales対FSales2Q/FY=Sales対FSales・+5%)",
        "day_ret_min": T2_DAY_RET_MIN,
        "day_ret_max": T2_DAY_RET_MAX,
        "sector_pit": "S33はD以前の直近月末master(kpi_round29_signals._pit_sector_map)",
        "prodcat_pit": "普通株ProdCat'011'はD以前の直近月末master(kpi_round23_signals._pit_prodcat011_set)",
        "reaction": "シグナル確定=D終値・エントリー=D+1寄付",
        "diag_leader_signals_total": gd["leader_signals_total"],
        "diag_leader_scan_start_bd": gd["leader_scan_start_bd"],
        "diag_leader_raw_count_prefilter": gd["leader_raw_count_prefilter"],
        "diag_leader_boundary_filtered": gd["leader_boundary_filtered"],
        "diag_leader_days": gd["leader_days"],
        "diag_leader_count_mean": gd["leader_count_mean"],
        "diag_leader_count_max": gd["leader_count_max"],
        "diag_leader_sector_missing": gd["leader_sector_missing"],
        "diag_candidates_evaluated": gd["candidates_evaluated"],
        "diag_cond_i_no_asof_disclosure": gd["cond_i_no_asof_disclosure"],
        "diag_cond_i_fy_mismatch": gd["cond_i_fy_mismatch"],
        "diag_cond_ii_stage_too_late": gd["cond_ii_stage_too_late"],
        "diag_cond_iii_same_period_disclosed": gd["cond_iii_same_period_disclosed"],
        "diag_price_band_fail": gd["price_band_fail"],
        "diag_price_data_missing": gd["price_data_missing"],
        "diag_same_day_fire_max": gd["same_day_fire_max"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "monthly_cluster": monthly_counts,
        "sales_beat_jaccard": jac_sales,
        "sector_sympathy_jaccard": jac_ss,
        "family_note": INDUSTRY_SPILLOVER_FAMILY_NOTE,
    }
    report_extra = [
        f"必須診断(T2): leader総数={gd['leader_signals_total']}(反応日境界ガード前={gd['leader_raw_count_prefilter']}"
        f"・期間外除外={gd['leader_boundary_filtered']}・走査開始={gd['leader_scan_start_bd']}) "
        f"/ leader日数={gd['leader_days']} / leader数[mean/max]=[{gd['leader_count_mean']}/{gd['leader_count_max']}] "
        f"/ 同日発火最大={gd['same_day_fire_max']} / 候補評価={gd['candidates_evaluated']}。",
        f"未発表判定内訳: cond_i(as-of開示なし)={gd['cond_i_no_asof_disclosure']} "
        f"/ cond_i(CurFYEn不一致)={gd['cond_i_fy_mismatch']} / cond_ii(段階超過)={gd['cond_ii_stage_too_late']} "
        f"/ cond_iii(同一期既発表)={gd['cond_iii_same_period_disclosed']} / 価格帯外={gd['price_band_fail']} "
        f"/ 価格欠損={gd['price_data_missing']}。",
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        f"月別クラスタ分布: {monthly_counts}。",
        f"sales_beat(leader)とのJaccard={jac_sales['jaccard']}(積集合={jac_sales['intersection']}"
        f"/和集合={jac_sales['union']}/自試行={jac_sales['my_keys']}/sales_beat={jac_sales['sales_beat_keys']})。",
        f"sector_sympathy_volshockとのJaccard={jac_ss['jaccard']}(積集合={jac_ss['intersection']}"
        f"/和集合={jac_ss['union']}/自試行={jac_ss['my_keys']}/sector_sympathy={jac_ss['sector_sympathy_keys']})。",
        INDUSTRY_SPILLOVER_FAMILY_NOTE,
    ]
    res = run_trial(
        filtered_df, T2_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
    )
    res["monthly_counts"] = monthly_counts
    res["jaccard_sales"] = jac_sales
    res["jaccard_ss"] = jac_ss
    return res


TRIAL_RUNNERS = {"t1": run_t1, "t2": run_t2}


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
        description="第38周: 新規軸二本 — 分割実施・決算読み替え（カタログ§7-AA・T1〜T2）"
    )
    parser.add_argument("--trial", choices=["t1", "t2", "all"], default="all")
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
    # holdout(2023年以降)抵触は FATAL（in-sampleのみで評価する事前登録の凍結境界を機械で強制）。
    if args.end > kpi_pead_signals.IN_SAMPLE_END:
        raise SystemExit(
            f"FATAL: --end={args.end} はholdout期間(2023年以降)に抵触します。"
            f"§7-AAはin-sample({kpi_pead_signals.IN_SAMPLE_END}まで)で評価します。holdoutは使用しません。"
        )

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    start_bd, end_bd = _event_bd_bounds(args.start, args.end, all_bdays)

    topix_close = measure_base_rate.load_topix_series()
    regime_by_day = measure_base_rate.build_regime_series(topix_close)
    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    universes_by_month = kpi_event_study.load_universe_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

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

    trials_to_run = ["t1", "t2"] if args.trial == "all" else [args.trial]
    results = []
    for t in trials_to_run:
        results.append(TRIAL_RUNNERS[t](shared))

    print("\n=== 第38周バッチ完了サマリー ===")
    for r in results:
        extra = ""
        if r["kpi_name"] == T2_KPI_NAME and "jaccard_sales" in r:
            extra = (
                f" sales_beat_Jaccard={r['jaccard_sales']['jaccard']} "
                f"sector_sympathy_Jaccard={r['jaccard_ss']['jaccard']}"
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
