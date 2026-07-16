#!/usr/bin/env python3
"""第32周: 新規発明イベント三本バッチ（カタログ§7-U・T1〜T3）。

docs/stock-algo-kpi-catalog.md §7-U の事前登録定義（凍結・Codexレビュー㊶㊷GO）を実装する。
3試行とも in-sample期間（2016-11〜2022-11・holdout 2023年以降は使わない）で実行し、共通の
探索的一次結論ルール（第18周基準）を適用する。実装の流儀は scripts/kpi_round30_signals.py の
run_trial 標準フロー・prefilter_in_universe・複数trialドライバ・Jaccard診断を踏襲する。

Canonical Module再利用（新規ロジックはT1/T2/T3の生成関数のみ）:
- T1 `mkt_upgrade`: 月末master(区分コード Mkt)スナップショット対の rank遷移。連続月の対のみ・
  昇格(rank増加)をシグナル化。区分順位表は§7-U凍結値をコード定数化。2022-03末→04末対は一斉
  再編のため全除外・新規上場/前月欠損/対象外区分は非シグナル・降格は診断カウントのみ。
  シグナル日=新スナップショット月の翌月初営業日。**n<30 なら judge を呼ばず
  verdict=reference_observation（§7-C型参考観測）として台帳記録**。
- T2 `updown_vol_ratio_high`: 窓D-19〜D の20営業日。UDV=Σ上昇日Va÷Σ下落日Va（同値日は分子分母外・
  20観測には数える）。シグナル=UDV(D)≥3.0 ∩ UDV(D-1)<3.0（D-1も完全20観測∩分母>0が必須）。
  AdjC/Vaの欠損/非有限は非シグナル。20営業日クールダウン。volshock/quiet系とのJaccard診断併記。
- T3 `preearnings_runup`: kpi_volshock_v3_ul_earnings の earnings_remaining_bd Canonicalを再利用。
  発火=(0≤remaining(D)≤5)∩(remaining(D-1)>5)∩quiet_ratio(D)≥1.2（第13周凍結値・クロス当日のみ評価）。
  サイクル識別=推定の基点となった直近開示の有効日・新しい実開示まで再発火禁止。
- ユニバース事前フィルタ: kpi_round23_signals.prefilter_in_universe（統計結果不変・第20周§7-I前例）。
- フォワードリターン・重複除去・集計・§6判定・レポート・台帳: kpi_event_study の
  compute_signal_returns/compute_stats/judge/bootstrap_ev_ci/write_report_md/append_trial を再利用。
- 探索的一次結論ルール: kpi_event_batch_signals.classify_exploratory を再利用。

3試行とも defer_entry=True（§6手順6の第5周以降既定方針＝S高で買えない日は翌日繰り延べ）。

Usage:
    python3 scripts/kpi_round32_signals.py --trial all
    python3 scripts/kpi_round32_signals.py --trial t1
    python3 scripts/kpi_round32_signals.py --trial all --start 2017-01 --end 2017-12 --no-trials-append
"""
from __future__ import annotations

import argparse
import bisect
import statistics
import sys
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst/DATA_ROOT を再利用)
import kpi_event_batch_signals  # noqa: E402  (Canonical Module: classify_exploratory を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: compute_signal_returns/compute_stats/judge/
# write_report_md/append_trial/bootstrap_ev_ci を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・MONTH_RE を再利用)
import kpi_round23_signals  # noqa: E402  (Canonical Module: prefilter_in_universe/_earliest_bars_date を再利用)
import kpi_uprev_signals  # noqa: E402  (Canonical Module: FINS_HISTORY_START_BD を再利用)
import kpi_volshock_v2_amplifiers as v2_amp  # noqa: E402  (Canonical Module: compute_quiet_ratio/QUIET_RATIO_THRESHOLD を再利用)
import kpi_volshock_v3_ul_earnings as v3ule  # noqa: E402  (Canonical Module: build_earnings_disclosure_history を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars/master読込を再利用)

PERIOD = (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_ROOT = Path("output/kpi")

VA_HISTORY_WINDOW = 20
WARMUP_BDAYS = 60

# --- 事前登録パラメータ（カタログ§7-U・事後変更禁止） -----------------------------

# T1: 区分順位表（実データ全列挙で凍結・Codex㊶CRITICAL対応）。
# rank3=最上位。対象外区分（0105 TOKYO PRO MARKET・0109 その他）は本表に無い＝rank=None＝非イベント。
T1_KPI_NAME = "mkt_upgrade"
T1_MKT_RANK = {
    # 旧体系
    "0101": 3,  # 東証一部
    "0102": 2,  # 東証二部
    "0106": 2,  # JASDAQ スタンダード
    "0104": 1,  # マザーズ
    "0107": 1,  # JASDAQ グロース
    # 新体系
    "0111": 3,  # プライム
    "0112": 2,  # スタンダード
    "0113": 1,  # グロース
}
T1_REORG_PREV_MONTH = "202203"  # 2022-03末→2022-04末対は一斉再編のため全除外
T1_REORG_NEW_MONTH = "202204"
T1_SMALL_N_REFERENCE_THRESHOLD = 30  # n<30 なら§7-C型参考観測へ自動格下げ

T2_KPI_NAME = "updown_vol_ratio_high"
T2_WINDOW_BDAYS = 20  # 窓 D-19..D の20営業日
T2_UDV_THRESHOLD = 3.0  # UDV(D)≥3.0 ∩ UDV(D-1)<3.0
T2_COOLDOWN_BDAYS = 20  # 同一銘柄はシグナルDから20営業日クールダウン

T3_KPI_NAME = "preearnings_runup"
T3_REMAINING_MAX_BD = 5  # 0 ≤ remaining(D) ≤ 5
T3_QUIET_THRESHOLD = v2_amp.QUIET_RATIO_THRESHOLD  # 1.2（第13周凍結値・Canonical再利用）
FINS_HIST_START_BD = kpi_uprev_signals.FINS_HISTORY_START_BD  # "20160706"

MULTI_TRIAL_NOTE = (
    "本ラウンドはT1〜T3の3試行同時登録であり累積試行数割引の対象。"
    "この結果単独で運用変更しない。"
)
ROUND_TAG = "32_new_event_batch"

DEFER_RATIONALE = (
    "§7-U各試行はエントリー=T+1寄付。§6手順6『S高で買えない日は翌日繰り延べ(第5周以降の既定方針)』"
    "に従いdefer_entry=True。区分昇格・出来高方向性・決算前いずれもT+1寄付がS高張り付きで買えない"
    "ことがあり得るため通常の繰延で扱う。"
)


def _is_pos(v) -> bool:
    """有限・正の数値か（None/NaN/非数/非正を弾く）。"""
    return isinstance(v, (int, float)) and v == v and v > 0


# --- T1: mkt_upgrade シグナル生成（新規ロジック） ----------------------------------


def _next_month(ym: str) -> str:
    """'YYYYMM' の翌月 'YYYYMM' を返す。"""
    y, m = int(ym[:4]), int(ym[4:6])
    if m == 12:
        return f"{y + 1:04d}01"
    return f"{y:04d}{m + 1:02d}"


def _first_bday_of_month(ym: str, all_bdays: list[str]) -> Optional[str]:
    """'YYYYMM' 月の初営業日（all_bdays内で ym+'01' 以上の最小日）を返す。無ければ None。"""
    lower = ym + "01"
    upper = ym + "32"  # 文字列上限（同月内で打ち切り）
    d = next((x for x in all_bdays if x >= lower), None)
    if d is None or not (d < upper):
        return None
    return d


def _master_snapshot_dates() -> list[str]:
    """data/jquants/master/*.json.gz の月末スナップショット日(YYYYMMDD)を昇順で返す。"""
    master_dir = jq_fetch.DATA_ROOT / "master"
    if not master_dir.exists():
        raise SystemExit(f"FATAL: master ディレクトリが見つかりません: {master_dir}")
    dates = sorted(p.name.removesuffix(".json.gz") for p in master_dir.glob("*.json.gz"))
    if not dates:
        raise SystemExit(f"FATAL: master スナップショットが1件もありません: {master_dir}")
    return dates


def generate_mkt_upgrade_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
) -> tuple[pd.DataFrame, dict]:
    """月末masterの区分(Mkt)順位が連続月対で増加した銘柄を昇格イベントとして生成する（§7-U T1）。

    連続する月末スナップショット対 (prev, new) につき、両スナップショットに存在し両区分とも
    順位表に載る（対象外区分でない）銘柄で rank(new)>rank(prev) を昇格＝シグナルとする。
    シグナル日=新スナップショット月の翌月初営業日（観測遅延を明示したas-of設計）。
    """
    snap_dates = _master_snapshot_dates()

    diag = {
        "consecutive_pairs_total": 0,       # 連続月対の総数（走査対象・窓内外問わず）
        "pairs_in_window": 0,               # シグナル日が[start_bd,end_bd]に入る連続月対
        "reorg_pairs_excluded": 0,          # 2022-03末→04末の一斉再編対（全除外）
        "non_consecutive_pairs_skipped": 0, # 月が連続していない対（欠損含む）
        "upgrades": 0,                      # 昇格シグナル（延べ）
        "downgrades": 0,                    # 降格（診断のみ）
        "new_listings": 0,                  # 前月スナップショットに不在（新規上場等）
        "prev_or_new_unranked": 0,          # 前月または当月が対象外区分（0105/0109等）
    }
    rows: list[dict] = []

    for prev_snap, new_snap in zip(snap_dates, snap_dates[1:]):
        prev_ym, new_ym = prev_snap[:6], new_snap[:6]
        if _next_month(prev_ym) != new_ym:
            diag["non_consecutive_pairs_skipped"] += 1
            continue
        diag["consecutive_pairs_total"] += 1

        signal_month = _next_month(new_ym)
        signal_date = _first_bday_of_month(signal_month, all_bdays)
        if signal_date is None or not (start_bd <= signal_date <= end_bd):
            continue
        diag["pairs_in_window"] += 1

        if prev_ym == T1_REORG_PREV_MONTH and new_ym == T1_REORG_NEW_MONTH:
            diag["reorg_pairs_excluded"] += 1
            continue

        prev_master = measure_base_rate.load_master_day(prev_snap)
        new_master = measure_base_rate.load_master_day(new_snap)

        for code, new_rec in new_master.items():
            new_rank = T1_MKT_RANK.get(new_rec.get("Mkt"))
            if new_rank is None:
                continue  # 当月が対象外区分＝非イベント
            prev_rec = prev_master.get(code)
            if prev_rec is None:
                diag["new_listings"] += 1
                continue
            prev_rank = T1_MKT_RANK.get(prev_rec.get("Mkt"))
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
                        "prev_mkt": prev_rec.get("Mkt"),
                        "new_mkt": new_rec.get("Mkt"),
                        "prev_rank": prev_rank,
                        "new_rank": new_rank,
                    }
                )
            elif new_rank < prev_rank:
                diag["downgrades"] += 1

    return pd.DataFrame(rows), diag


# --- T2: updown_vol_ratio_high シグナル生成（新規ロジック） -------------------------


def _udv_over_window(
    hist: dict[int, tuple[float, float]], end_idx: int
) -> Optional[tuple[float, float, float]]:
    """窓 [end_idx-19, end_idx] の20営業日でUDV=Σ上昇日Va÷Σ下落日Va を計算する。

    各日 j の上昇/下落は AdjC(j) vs AdjC(j-1)（j-1=end_idx-20 まで必要）。同値日は分子分母に
    入れないが20観測には数える。窓の全営業日で AdjC(j)・AdjC(j-1) が有限正で存在し、かつ
    上昇日/下落日の Va(j) が有限正であることを要求する（欠損/非有限があれば None＝非シグナル）。
    分母（Σ下落日Va）>0 も必須（0なら None）。

    Returns:
        (udv, up_va_sum, down_va_sum) または不成立時 None。
    """
    up_sum = 0.0
    down_sum = 0.0
    for j in range(end_idx - T2_WINDOW_BDAYS + 1, end_idx + 1):
        cur = hist.get(j)
        prev = hist.get(j - 1)
        if cur is None or prev is None:
            return None
        adjc_cur, va_cur = cur
        adjc_prev = prev[0]
        if not (_is_pos(adjc_cur) and _is_pos(adjc_prev)):
            return None
        if adjc_cur > adjc_prev:
            if not _is_pos(va_cur):
                return None
            up_sum += va_cur
        elif adjc_cur < adjc_prev:
            if not _is_pos(va_cur):
                return None
            down_sum += va_cur
        # 同値日は分子分母外（20観測には数える＝ループの一員として通過）
    if not (down_sum > 0):
        return None
    return up_sum / down_sum, up_sum, down_sum


def generate_updown_vol_ratio_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]内から上昇日/下落日 出来高比の高水準クロスを生成する（§7-U T2）。

    UDV(D)≥3.0 ∩ UDV(D-1)<3.0（D・D-1とも完全20観測∩分母>0が必須）。D-1が定義不能なら
    非シグナル（「3未満」扱い禁止）。同一銘柄はシグナルDから20営業日クールダウン。
    """
    earliest_idx = bday_index[kpi_round23_signals._earliest_bars_date()]
    idx_start = bday_index[start_bd]
    # D-1のUDV評価に end_idx-21 の AdjC まで必要＝22営業日+余裕の助走。
    warmup_idx = max(earliest_idx, idx_start - (T2_WINDOW_BDAYS + WARMUP_BDAYS))
    scan_days = all_bdays[warmup_idx : bday_index[end_bd] + 1]

    hist: dict[str, dict[int, tuple[float, float]]] = defaultdict(dict)  # code -> {idx: (AdjC, Va)}
    last_signal_idx: dict[str, int] = {}

    diag = {
        "business_days_scanned": 0,
        "code_day_observations": 0,
        "complete_windows": 0,   # D窓が完全成立(∩分母>0)かつD-1窓も完全成立
        "udv_ge3_d": 0,          # UDV(D)≥3.0
        "crosses": 0,            # UDV(D)≥3.0 ∩ UDV(D-1)<3.0（クールダウン前）
        "cooldown_suppressed": 0,
        "signals_updown_vol_ratio_high": 0,
    }
    rows: list[dict] = []

    for d in scan_days:
        in_window = start_bd <= d <= end_bd
        i = bday_index[d]
        bars_d = measure_base_rate.load_bars_day(d)
        if in_window:
            diag["business_days_scanned"] += 1
        for code, rec in bars_d.items():
            if in_window:
                diag["code_day_observations"] += 1
            adjc = rec.get("AdjC")
            va = rec.get("Va")
            hist[code][i] = (adjc if _is_pos(adjc) else None, va if _is_pos(va) else None)

            if not in_window:
                continue
            res_d = _udv_over_window(hist[code], i)
            res_dm1 = _udv_over_window(hist[code], i - 1)
            if res_d is None or res_dm1 is None:
                continue
            diag["complete_windows"] += 1
            udv_d, up_d, down_d = res_d
            udv_dm1 = res_dm1[0]
            if udv_d >= T2_UDV_THRESHOLD:
                diag["udv_ge3_d"] += 1
            if not (udv_d >= T2_UDV_THRESHOLD and udv_dm1 < T2_UDV_THRESHOLD):
                continue
            diag["crosses"] += 1
            prev_sig = last_signal_idx.get(code)
            if prev_sig is not None and (i - prev_sig) < T2_COOLDOWN_BDAYS:
                diag["cooldown_suppressed"] += 1
                continue
            diag["signals_updown_vol_ratio_high"] += 1
            last_signal_idx[code] = i
            rows.append(
                {
                    "signal_date": d,
                    "code": code,
                    "udv_d": udv_d,
                    "udv_dm1": udv_dm1,
                    "up_va_sum": up_d,
                    "down_va_sum": down_d,
                }
            )

        # 古いhist掃除（直近25営業日分あれば22日窓に十分）。
        for code in list(hist.keys()):
            hmap = hist[code]
            if len(hmap) > 30:
                for old in [k for k in hmap if k < i - 25]:
                    del hmap[old]

    return pd.DataFrame(rows), diag


# --- T3: preearnings_runup シグナル生成（新規ロジック） ----------------------------


def generate_preearnings_runup_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
    earnings_history: dict[str, list[str]],
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]内から推定決算接近×静かな漸増の事前ポジションを生成する（§7-U T3）。

    発火 = (0≤remaining(D)≤5) ∩ (remaining(D-1)>5) ∩ quiet_ratio(D)≥1.2。remaining は
    kpi_volshock_v3_ul_earnings の推定式（過去開示有効日間隔の中央値 − 経過営業日）と同一定義を
    銘柄内逐次で再現する（PIT安全＝有効日≤D の開示のみ使用）。サイクル識別=推定基点の直近開示
    有効日。同一サイクルは1回のみ発火・新しい実開示まで再発火禁止。quiet_ratioはクロス当日のみ評価。
    """
    idx_start = bday_index[start_bd]
    idx_end = bday_index[end_bd]
    # 前日remaining(D-1)を確定させるため窓開始の1営業日前から助走。
    loop_lo = max(1, idx_start - 1)

    # quiet_ratio(D)は各クロスで Va[D-30..D-1] を要する。銘柄別ループ順では load_bars_day の
    # LRUキャッシュがスラッシングし全期間で桁違いに遅くなるため、日次順の1パスで Va系列を
    # メモリに先取りする（v2_amp.compute_quiet_ratio と同一の「truthyなVaのみ採用」を再現）。
    quiet_lookback = v2_amp.QUIET_RECENT_WINDOW + v2_amp.QUIET_PRIOR_WINDOW  # =30
    pre_lo = max(0, idx_start - quiet_lookback)
    va_series: dict[str, dict[int, float]] = defaultdict(dict)
    for pd_day in all_bdays[pre_lo : idx_end + 1]:
        j = bday_index[pd_day]
        for c, rec in measure_base_rate.load_bars_day(pd_day).items():
            va = rec.get("Va")
            if va:  # None/0 を除外（compute_quiet_ratio の truthy フィルタと同一）
                va_series[c][j] = va

    def quiet_ratio_local(code: str, i: int) -> Optional[float]:
        """v2_amp.compute_quiet_ratio と数式・欠損規約が完全一致する in-memory 版。"""
        if i - quiet_lookback < 0:
            return None
        series = va_series.get(code)
        if not series:
            return None
        recent = [v for j in range(i - v2_amp.QUIET_RECENT_WINDOW, i) if (v := series.get(j))]
        prior = [
            v
            for j in range(i - quiet_lookback, i - v2_amp.QUIET_RECENT_WINDOW)
            if (v := series.get(j))
        ]
        if not recent or not prior:
            return None
        mean_prior = sum(prior) / len(prior)
        if mean_prior == 0:
            return None
        return (sum(recent) / len(recent)) / mean_prior

    diag = {
        "codes_with_history": 0,
        "codes_with_estimate": 0,      # ≥2開示があり推定可能だった銘柄
        "crosses": 0,                  # remainingクロス（quiet評価前）
        "quiet_fail": 0,               # クロスしたがquiet_ratio<1.2 or 計算不能
        "cycle_dedup_suppressed": 0,   # 同一サイクル(anchor)での再発火抑止
        "signals_preearnings_runup": 0,
    }
    est_errors: list[int] = []  # 推定誤差(営業日) = 実次回開示idx − 推定決算idx（事後診断）
    rows: list[dict] = []

    for code, eff_dates in earnings_history.items():
        diag["codes_with_history"] += 1
        eff_idxs = sorted(bday_index[dd] for dd in eff_dates if dd in bday_index)
        if len(eff_idxs) < 2:
            continue
        diag["codes_with_estimate"] += 1

        median_cache: dict[int, float] = {}  # cnt(=過去開示数) -> interval中央値

        def remaining_at(i: int) -> tuple[Optional[float], Optional[int], Optional[float]]:
            """(remaining, anchor_idx, estimated_earn_idx) を返す。過去開示<2なら (None,None,None)。"""
            cnt = bisect.bisect_right(eff_idxs, i)  # 有効日≤i の開示数
            if cnt < 2:
                return None, None, None
            if cnt not in median_cache:
                pts = eff_idxs[:cnt]
                median_cache[cnt] = statistics.median(b - a for a, b in zip(pts, pts[1:]))
            median_interval = median_cache[cnt]
            anchor_idx = eff_idxs[cnt - 1]
            remaining = median_interval - (i - anchor_idx)
            return remaining, anchor_idx, anchor_idx + median_interval

        fired_anchor: Optional[int] = None
        prev_rem: Optional[float] = None
        for i in range(loop_lo, idx_end + 1):
            rem, anchor_idx, est_earn_idx = remaining_at(i)
            in_window = idx_start <= i <= idx_end
            if (
                in_window
                and rem is not None
                and prev_rem is not None
                and 0 <= rem <= T3_REMAINING_MAX_BD
                and prev_rem > T3_REMAINING_MAX_BD
            ):
                diag["crosses"] += 1
                if fired_anchor == anchor_idx:
                    diag["cycle_dedup_suppressed"] += 1
                else:
                    d = all_bdays[i]
                    q = quiet_ratio_local(code, i)
                    if q is None or q < T3_QUIET_THRESHOLD:
                        diag["quiet_fail"] += 1
                    else:
                        diag["signals_preearnings_runup"] += 1
                        fired_anchor = anchor_idx
                        # 事後診断: 実次回開示（anchorより後の最初の有効日）との差。
                        nxt = bisect.bisect_right(eff_idxs, anchor_idx)
                        actual_next_idx = eff_idxs[nxt] if nxt < len(eff_idxs) else None
                        est_err = (
                            actual_next_idx - int(round(est_earn_idx))
                            if actual_next_idx is not None
                            else None
                        )
                        if est_err is not None:
                            est_errors.append(est_err)
                        rows.append(
                            {
                                "signal_date": d,
                                "code": code,
                                "remaining_d": rem,
                                "remaining_dm1": prev_rem,
                                "quiet_ratio": q,
                                "anchor_idx": anchor_idx,
                                "est_earn_idx": est_earn_idx,
                                "actual_next_idx": actual_next_idx,
                                "est_error_bd": est_err,
                            }
                        )
            prev_rem = rem

    diag["est_error_summary"] = _summarize_est_errors(est_errors)
    return pd.DataFrame(rows), diag


def _summarize_est_errors(errors: list[int]) -> dict:
    """推定誤差(営業日)の分布サマリ（件数・平均・中央値・最小最大・|誤差|中央値）。"""
    if not errors:
        return {"n": 0}
    absmed = statistics.median(abs(e) for e in errors)
    return {
        "n": len(errors),
        "mean": sum(errors) / len(errors),
        "median": statistics.median(errors),
        "min": min(errors),
        "max": max(errors),
        "abs_median": absmed,
    }


# --- 共通: ハーネス実行 + 探索的一次結論 + 台帳記録（round30 run_trial 標準フローを踏襲） ---


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
    （kpi_round30_signals.run_trial と同一の標準フロー。全試行 defer_entry=True）。

    small_n_reference_threshold が指定され n がそれ未満のとき、judge を呼ばず
    verdict='reference_observation'（§7-C型参考観測）として台帳記録する（§7-U T1の自動格下げ）。
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
            f"n={stats['n']} < {small_n_reference_threshold}（§7-U凍結の自動格下げ）"
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
        f.write("\n## 探索的一次結論（第32周・カタログ§7-U事前登録の共通ルール）\n")
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
            f.write("\n## 近傍事実・必須診断・注記（§7-U）\n")
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


def run_t1(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T1 {T1_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd = generate_mkt_upgrade_signals(shared["start_bd"], shared["end_bd"], hc["all_bdays"])
    print(
        f"T1 シグナル生成完了: {len(signals_df)}件 (連続月対={gd['consecutive_pairs_total']}, "
        f"窓内対={gd['pairs_in_window']}, 昇格={gd['upgrades']}, 降格={gd['downgrades']}, "
        f"再編除外対={gd['reorg_pairs_excluded']}, 新規上場={gd['new_listings']}, "
        f"対象外区分={gd['prev_or_new_unranked']}, 非連続対skip={gd['non_consecutive_pairs_skipped']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T1シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T1_KPI_NAME, hc)

    base_params = {
        "signal_definition": (
            "連続する月末masterスナップショット対で区分順位rank(new)>rank(prev)＝昇格。"
            "順位表: rank3=0101/0111, rank2=0102/0106/0112, rank1=0104/0107/0113。"
            "対象外(0105/0109)・新規上場・前月欠損・非連続対・2022-03末→04末再編対は非シグナル。"
            "シグナル日=新スナップショット月の翌月初営業日"
        ),
        "mkt_rank_table": T1_MKT_RANK,
        "reorg_excluded_pair": f"{T1_REORG_PREV_MONTH}末→{T1_REORG_NEW_MONTH}末",
        "small_n_reference_threshold": T1_SMALL_N_REFERENCE_THRESHOLD,
        "reaction": "シグナル確定=T終値・エントリー=T+1寄付",
        "diag_upgrades": gd["upgrades"],
        "diag_downgrades": gd["downgrades"],
        "diag_reorg_pairs_excluded": gd["reorg_pairs_excluded"],
        "diag_new_listings": gd["new_listings"],
        "diag_prev_or_new_unranked": gd["prev_or_new_unranked"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "power_note": (
            "実施→観測に最大約1ヶ月の遅延があり効果希薄化の可能性(バイアスでなく検定力問題)。"
            "観測遅延日数は算出不能。月別件数を必須報告"
        ),
        "neighbor_note": (
            "区分昇格は機関の投資可能ユニバース入り・パッシブ資金・信用条件改善の実需イベント。"
            "既存の価格/出来高テクニカル系とは発火機構が異なる制度イベント"
        ),
    }
    report_extra = [
        f"必須診断(T1内訳): 昇格={gd['upgrades']} / 降格(診断のみ)={gd['downgrades']} "
        f"/ 一斉再編除外対={gd['reorg_pairs_excluded']} / 新規上場(前月不在)={gd['new_listings']} "
        f"/ 対象外区分(0105/0109等)={gd['prev_or_new_unranked']} / 連続月対={gd['consecutive_pairs_total']}。",
        base_params["power_note"],
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        base_params["neighbor_note"],
    ]
    res = run_trial(
        filtered_df, T1_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
        small_n_reference_threshold=T1_SMALL_N_REFERENCE_THRESHOLD,
    )
    res["signals_raw_df"] = signals_df
    res["monthly_counts"] = (
        signals_df.assign(month=signals_df["signal_date"].str[:6])["month"].value_counts().sort_index().to_dict()
    )
    return res


def run_t2(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T2 {T2_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd = generate_updown_vol_ratio_signals(
        shared["start_bd"], shared["end_bd"], hc["all_bdays"], hc["bday_index"]
    )
    print(
        f"T2 シグナル生成完了: {len(signals_df)}件 (営業日走査={gd['business_days_scanned']}, "
        f"完全窓(D∩D-1)={gd['complete_windows']}, UDV(D)≥3={gd['udv_ge3_d']}, "
        f"クロス={gd['crosses']}, クールダウン抑止={gd['cooldown_suppressed']}, "
        f"シグナル成立={gd['signals_updown_vol_ratio_high']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T2シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T2_KPI_NAME, hc)

    base_params = {
        "signal_definition": (
            f"窓D-19..D の20営業日。上昇日=AdjC(d)>AdjC(d-1)/下落日=AdjC(d)<AdjC(d-1)（同値日は分子分母外・"
            f"20観測に数える）。UDV(D)=Σ上昇日Va÷Σ下落日Va(分母>0必須)。"
            f"シグナル=UDV(D)≥{T2_UDV_THRESHOLD} ∩ UDV(D-1)<{T2_UDV_THRESHOLD}"
            f"（D-1も完全20観測∩分母>0必須・定義不能なら非シグナル）。{T2_COOLDOWN_BDAYS}営業日クールダウン"
        ),
        "window_bdays": T2_WINDOW_BDAYS,
        "udv_threshold": T2_UDV_THRESHOLD,
        "cooldown_bdays": T2_COOLDOWN_BDAYS,
        "reaction": "シグナル確定=T終値・エントリー=T+1寄付",
        "diag_complete_windows": gd["complete_windows"],
        "diag_udv_ge3_d": gd["udv_ge3_d"],
        "diag_crosses": gd["crosses"],
        "diag_cooldown_suppressed": gd["cooldown_suppressed"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "neighbor_note": (
            "絶対量(volshock)・期間比(quiet)と直交する第3の出来高軸(方向性偏在=蓄積の足跡)。"
            "volshock/quiet系とのJaccard診断を併記"
        ),
    }
    report_extra = [
        f"必須診断: 完全窓(D∩D-1)={gd['complete_windows']} / UDV(D)≥{T2_UDV_THRESHOLD}={gd['udv_ge3_d']} "
        f"/ クロス={gd['crosses']} / クールダウン抑止={gd['cooldown_suppressed']}。",
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        base_params["neighbor_note"],
    ]
    res = run_trial(
        filtered_df, T2_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
    )
    res["signals_raw_df"] = signals_df
    return res


def run_t3(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T3 {T3_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd = generate_preearnings_runup_signals(
        shared["start_bd"], shared["end_bd"], hc["all_bdays"], hc["bday_index"], shared["earnings_history"]
    )
    ees = gd["est_error_summary"]
    print(
        f"T3 シグナル生成完了: {len(signals_df)}件 (履歴保有銘柄={gd['codes_with_history']}, "
        f"推定可能銘柄={gd['codes_with_estimate']}, クロス={gd['crosses']}, "
        f"quiet不成立={gd['quiet_fail']}, サイクル重複抑止={gd['cycle_dedup_suppressed']}, "
        f"シグナル成立={gd['signals_preearnings_runup']}) 推定誤差={ees}",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T3シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T3_KPI_NAME, hc)

    base_params = {
        "signal_definition": (
            f"kpi_volshock_v3_ul_earnings の earnings_remaining_bd(過去開示有効日間隔中央値−経過営業日)を再利用。"
            f"発火=(0≤remaining(D)≤{T3_REMAINING_MAX_BD}) ∩ (remaining(D-1)>{T3_REMAINING_MAX_BD}) "
            f"∩ quiet_ratio(D)≥{T3_QUIET_THRESHOLD}(第13周凍結・クロス当日のみ評価)。"
            "サイクル識別=推定基点の直近開示有効日・同一サイクル1回のみ発火・新実開示まで再発火禁止"
        ),
        "remaining_max_bd": T3_REMAINING_MAX_BD,
        "quiet_threshold": T3_QUIET_THRESHOLD,
        "reaction": "シグナル確定=T終値・エントリー=T+1寄付(20bd保有は決算発表を跨ぐ＝意図的なイベント取り)",
        "diag_codes_with_estimate": gd["codes_with_estimate"],
        "diag_crosses": gd["crosses"],
        "diag_quiet_fail": gd["quiet_fail"],
        "diag_cycle_dedup_suppressed": gd["cycle_dedup_suppressed"],
        "diag_est_error_summary": ees,
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "estimate_caveat": (
            "remaining_bdはJ-Quants決算発表予定日ではなく過去開示間隔中央値からの推定値"
            "(真の予定日は本プロジェクトで未取得・kpi_volshock_v3_ul_earningsと同一の限定事項)"
        ),
        "neighbor_note": (
            "§7-E B1(champion∩接近・n=16・degrading)とは母集団が別の新イベント"
            "(champion部分集合でなく全銘柄の推定決算接近×静かな漸増)"
        ),
    }
    report_extra = [
        f"必須診断(T3): 推定可能銘柄={gd['codes_with_estimate']} / remainingクロス={gd['crosses']} "
        f"/ quiet不成立={gd['quiet_fail']} / サイクル重複除去={gd['cycle_dedup_suppressed']}。",
        f"推定誤差分布(実次回開示−推定決算・営業日・事後診断): {ees}。",
        base_params["estimate_caveat"],
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        base_params["neighbor_note"],
    ]
    res = run_trial(
        filtered_df, T3_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
    )
    res["signals_raw_df"] = signals_df
    return res


TRIAL_RUNNERS = {"t1": run_t1, "t2": run_t2, "t3": run_t3}


# --- Jaccard 診断（診断限定・(code, signal_date) イベントキー） ---------------------


def _event_keys(df: pd.DataFrame) -> set:
    """DataFrame から (code, signal_date) の集合を作る（型を str に正規化して突合）。"""
    if df is None or len(df) == 0 or "code" not in df.columns or "signal_date" not in df.columns:
        return set()
    return set(zip(df["code"].astype(str), df["signal_date"].astype(str)))


def _jaccard(a: set, b: set) -> tuple[float, int]:
    """Jaccard係数と積集合サイズを返す（両空なら 0.0）。"""
    if not a and not b:
        return 0.0, 0
    inter = len(a & b)
    union = len(a | b)
    return (inter / union if union else 0.0), inter


def _load_volshock_5x_keys() -> set:
    """output/kpi/volshock_5x/signals_raw.csv の (code, signal_date) を読む（診断限定）。"""
    p = OUTPUT_ROOT / "volshock_5x" / "signals_raw.csv"
    if not p.exists():
        print(f"[Jaccard診断] {p} が無いためスキップ", file=sys.stderr)
        return set()
    return _event_keys(pd.read_csv(p, usecols=["signal_date", "code"], dtype=str))


def _load_quiet_champion_keys() -> set:
    """volshock_x_above200_quiet（above200 かつ quiet_bucket=='quiet'）の (code, signal_date)。

    第13周チャンピオン構成の生イベント集合を signals_features_volshock_x_above200.csv から復元する
    （診断限定・quiet_bucket でフィルタ）。
    """
    p = OUTPUT_ROOT / "volshock_v2_amplifiers" / "signals_features_volshock_x_above200.csv"
    if not p.exists():
        print(f"[Jaccard診断] {p} が無いためスキップ", file=sys.stderr)
        return set()
    df = pd.read_csv(p, usecols=["signal_date", "code", "quiet_bucket"], dtype=str)
    return _event_keys(df[df["quiet_bucket"] == "quiet"])


def print_jaccard_diagnostics(new_keys: dict[str, set]) -> list[str]:
    """T2 × {volshock_5x, volshock_x_above200_quiet} と T1/T2/T3相互のJaccardを算出・表示する（§7-U）。"""
    lines: list[str] = []
    labels = {"t1": T1_KPI_NAME, "t2": T2_KPI_NAME, "t3": T3_KPI_NAME}

    # 相互（生成された試行のみ）。
    for a, b in [("t1", "t2"), ("t1", "t3"), ("t2", "t3")]:
        if a in new_keys and b in new_keys:
            j, inter = _jaccard(new_keys[a], new_keys[b])
            lines.append(
                f"Jaccard {labels[a]}×{labels[b]} = {j:.4f} "
                f"(積集合={inter} / |{labels[a]}|={len(new_keys[a])} / |{labels[b]}|={len(new_keys[b])})"
            )

    # T2 × 既存2集合（§7-U指定: volshock_5x・volshock_x_above200_quiet）。
    if "t2" in new_keys:
        existing = {
            "volshock_5x": _load_volshock_5x_keys(),
            "volshock_x_above200_quiet": _load_quiet_champion_keys(),
        }
        for name, ekeys in existing.items():
            j, inter = _jaccard(new_keys["t2"], ekeys)
            lines.append(
                f"Jaccard {labels['t2']}×{name}(既存) = {j:.4f} "
                f"(積集合={inter} / |{labels['t2']}|={len(new_keys['t2'])} / |{name}|={len(ekeys)})"
            )

    print("\n=== Jaccard診断（診断限定・(code,signal_date)イベントキー・§7-U） ===")
    for ln in lines:
        print(ln)
    return lines


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
    parser = argparse.ArgumentParser(description="第32周: 新規発明イベント三本バッチ（カタログ§7-U・T1〜T3）")
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
            f"§7-Uはin-sample({kpi_pead_signals.IN_SAMPLE_END}まで)で評価します。holdoutは使用しません。"
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

    earnings_history: dict[str, list[str]] = {}
    if "t3" in trials_to_run:
        hist_end_bd = measure_base_rate.month_ends_in_range(
            calendar_days, kpi_pead_signals.IN_SAMPLE_END, kpi_pead_signals.IN_SAMPLE_END
        )[0]
        print("T3用 決算開示PIT履歴構築中...", file=sys.stderr)
        earnings_history, earn_diag = v3ule.build_earnings_disclosure_history(
            FINS_HIST_START_BD, hist_end_bd, all_bdays, bday_index
        )
        print(
            f"決算開示履歴構築完了: 銘柄数={earn_diag['codes_with_history']} "
            f"(走査日数={earn_diag['scanned_days']}, 決算短信レコード数={earn_diag['earnings_doctype_records']})",
            file=sys.stderr,
        )

    shared = {
        "start_bd": start_bd,
        "end_bd": end_bd,
        "append_to_ledger": not args.no_trials_append,
        "earnings_history": earnings_history,
        "harness_ctx": {
            "all_bdays": all_bdays,
            "bday_index": bday_index,
            "regime_by_day": regime_by_day,
            "base_rate_by_month": base_rate_by_month,
            "universes_by_month": universes_by_month,
        },
    }

    results = []
    new_keys: dict[str, set] = {}
    for t in trials_to_run:
        r = TRIAL_RUNNERS[t](shared)
        results.append(r)
        new_keys[t] = _event_keys(r.get("signals_raw_df"))

    jaccard_lines = print_jaccard_diagnostics(new_keys)

    print("\n=== 第32周バッチ完了サマリー ===")
    for r in results:
        extra = ""
        if r["kpi_name"] == T1_KPI_NAME and "monthly_counts" in r:
            extra = f" 月別件数={r['monthly_counts']}"
        print(
            f"{r['kpi_name']}: n={r['n']} 月平均n={r.get('avg_monthly_n')} "
            f"lift={r['lift']}[{r['ci_low']},{r['ci_high']}] "
            f"EV(なし)={r['ev_point']}[{r['ev_ci_low']},{r['ev_ci_high']}] EV(stop8)={r['ev_stop8']} "
            f"verdict={r['verdict']} 一次結論={r['conclusion']} "
            f"entry_missing={r['entry_missing']}/raw={r['raw_signal_count']}{extra}"
        )
    for ln in jaccard_lines:
        print(f"  [Jaccard] {ln}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
