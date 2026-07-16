#!/usr/bin/env python3
"""第29周: 新規発明イベント三本バッチ（カタログ§7-R・T1〜T3）。

docs/stock-algo-kpi-catalog.md §7-R の事前登録定義（凍結・Codexレビュー㉚㉛GO）を実装する。
3試行とも in-sample期間（2016-11〜2022-11・holdout 2023年以降は使わない）で実行し、共通の
探索的一次結論ルール（第18周基準）を適用する。実装の流儀は scripts/kpi_round23_signals.py の
run_trial 標準フロー・prefilter_in_universe・複数trialドライバを踏襲する。

Canonical Module再利用（新規ロジックはT1/T2/T3の生成関数のみ）:
- T1 `sector_sympathy_volshock`:
  - leader検出は kpi_volshock_signals.generate_volshock_signals（第5周凍結値: 倍率5・陽線・
    前日比+2〜8%・直近20回有効Va平均）をそのまま再利用し、その (signal_date, code) 集合を
    「その営業日の volshock 発火銘柄」として使う（volshock条件の再実装はしない）。
  - S33業種は「D以前の直近月末master」をPIT適用（measure_base_rate.load_master_day 経由）。
  - TOP500は §0確定の月次売買代金ユニバース（kpi_event_study が読む universes_by_month）。
    「D時点TOP500」は _universe_membership と同一のPIT規約（signal_month より厳密に前の直近
    確定月末ユニバース）で解決する。leader/候補ともこのTOP500に限定する。
  - 20日平均Va（D含まず・過去20有効観測）は逐次スキャンの deque(maxlen=20) で保持する
    （kpi_volshock_signals と同じ「直近20回の有効Va観測値」規約・当日は判定後に追加＝D含まず）。
- T2 `ll_release_rebound`: bars の LL フラグ（"1"=ストップ安）で連続LLエピソードを検出し、
  最終LL日Eの翌営業日から10営業日内で初の (LL=="0" ∩ 陽線 ∩ Va≥20日平均×1.5) を拾う。
- T3 `gap_hold_close_strong`: bars の四本値のみで gap≥+3% ∩ 引け≥寄付 ∩ close位置≥0.8 ∩
  窓埋めなし(AdjL≥前日AdjC) ∩ Va≥20日平均×2 ∩ OHLCV品質ガードを判定する。
- ユニバース事前フィルタ: kpi_round23_signals.prefilter_in_universe（統計結果不変・第20周§7-I前例）。
- フォワードリターン・重複除去・集計・§6判定・レポート・台帳: kpi_event_study の
  compute_signal_returns/compute_stats/judge/bootstrap_ev_ci/write_report_md/append_trial を再利用。
- 探索的一次結論ルール: kpi_event_batch_signals.classify_exploratory を再利用。

3試行とも defer_entry=True（§6手順6の第5周以降既定方針＝S高で買えない日は翌日繰り延べ。
T2/T3のストップ安解放・ギャップ持続はS高張り付きが起きうるため通常の繰延で扱う）。

Usage:
    python3 scripts/kpi_round29_signals.py --trial all
    python3 scripts/kpi_round29_signals.py --trial t1
    python3 scripts/kpi_round29_signals.py --trial all --start 2017-01 --end 2017-12 --no-trials-append
"""
from __future__ import annotations

import argparse
import functools
import statistics
import sys
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import kpi_event_batch_signals  # noqa: E402  (Canonical Module: classify_exploratory を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: compute_signal_returns/compute_stats/judge/
# write_report_md/append_trial/bootstrap_ev_ci/_universe_membership を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・MONTH_RE を再利用)
import kpi_round23_signals  # noqa: E402  (Canonical Module: prefilter_in_universe/_master_dates/
# _earliest_bars_date を再利用)
import kpi_volshock_signals  # noqa: E402  (Canonical Module: generate_volshock_signals を leader検出に再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars/master読込を再利用)

PERIOD = (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_ROOT = Path("output/kpi")

VA_HISTORY_WINDOW = 20  # 「直近20回の有効Va観測値」（当日は判定後に追加＝D含まず。volshock規約と同一）
WARMUP_BDAYS = 60  # deque(maxlen=20) を確実に満たすための助走（欠測日を挟んでも余裕を持たせる）

# --- 事前登録パラメータ（カタログ§7-R・事後変更禁止） -----------------------------

T1_KPI_NAME = "sector_sympathy_volshock"
T1_DAY_RET_MIN = -0.01  # 候補の当日リターン下限（まだ動いていない）
T1_DAY_RET_MAX = 0.02  # 候補の当日リターン上限

T2_KPI_NAME = "ll_release_rebound"
T2_REBOUND_WINDOW_BDAYS = 10  # E+1〜E+10営業日
T2_VOL_MULTIPLIER = 1.5  # Va ≥ 20日平均 × 1.5
T2_COOLDOWN_BDAYS = 10  # 同一銘柄の再発火はシグナルDから10営業日クールダウン

T3_KPI_NAME = "gap_hold_close_strong"
T3_GAP_RATIO = 1.03  # AdjO ≥ 前日AdjC × 1.03
T3_CLOSE_LOC_MIN = 0.8  # (AdjC-AdjL)/(AdjH-AdjL) ≥ 0.8
T3_VOL_MULTIPLIER = 2.0  # Va ≥ 20日平均 × 2

MULTI_TRIAL_NOTE = (
    "本ラウンドはT1〜T3の3試行同時登録であり累積試行数割引の対象。"
    "この結果単独で運用変更しない。"
)
ROUND_TAG = "29_new_event_batch"

DEFER_RATIONALE = (
    "§7-R各試行はエントリー=T+1寄付。§6手順6『S高で買えない日は翌日繰り延べ(第5周以降の既定方針)』"
    "に従いdefer_entry=True。T2ストップ安解放反発・T3ギャップ持続はS高張り付きが起きうるため通常の繰延で扱う。"
)


# --- D時点マスタ由来のS33業種マップ（PIT・月末masterのみ格納） -----------------------


@functools.lru_cache(maxsize=200)
def _sector_map_for_master(master_date: str) -> dict:
    """指定月末masterの Code -> S33業種コード dict（measure_base_rate.load_master_day を再利用）。
    S33が空/欠損の銘柄は値 None/'' のまま返す（呼び出し側で業種欠損として除外・診断する）。"""
    master = measure_base_rate.load_master_day(master_date)
    return {code: rec.get("S33") for code, rec in master.items()}


def _pit_sector_map(day: str) -> dict:
    """営業日 day に対し「day 以前の直近月末master」のS33業種マップをPIT適用する
    （kpi_round23_signals._master_dates と同一の月末master実在集合を再利用）。"""
    md = kpi_round23_signals._master_dates()
    eligible = [m for m in md if m <= day]
    if not eligible:
        raise SystemExit(
            f"FATAL: {day} 以前の月末master が存在しません(最古master={md[0]})。S33業種判定ができません。"
        )
    return _sector_map_for_master(eligible[-1])


def _top500_for_day(day: str, universes_by_month: dict[str, set]) -> tuple[Optional[set], str]:
    """営業日 day の「D時点TOP500」ユニバース集合をPIT解決する。

    kpi_event_study._universe_membership と同一のPIT規約（signal_month より厳密に前の直近確定月末
    ユニバース）でユニバース集合そのものを返す。同関数は per-code の所属真偽を返すが、T1は業種内
    中央値の母集団列挙に集合自体が必要なため、同じ規約で集合を読む（判定路の二重化ではなく、
    ハーネスが読むのと同一の canonical universe を読むだけ）。

    Returns:
        (top500_set, universe_month_used)。確定済みユニバースが無ければ (None, "")。
    """
    signal_month = day[:6]
    earlier_months = [m for m in universes_by_month if m < signal_month]
    if not earlier_months:
        return None, ""
    mu = max(earlier_months)
    return universes_by_month[mu], mu


# --- T1: sector_sympathy_volshock シグナル生成（新規ロジック） ---------------------


def generate_sector_sympathy_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
    universes_by_month: dict[str, set],
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]内の全営業日から業種内波及シグナルを生成する（カタログ§7-R T1）。

    営業日Dに同一S33業種内の他銘柄（TOP500内）が volshock_5x を満たしたとき、同業種TOP500内で
    D当日に volshock を満たさず、当日リターンが -1%〜+2%、20日平均Va（D含まず）が業種内中央値
    （同業種TOP500∩計算可能銘柄）以上の銘柄を候補とする。候補はD日1回発火（leader数は診断値）。

    leaderの発火銘柄集合は kpi_volshock_signals.generate_volshock_signals（第5周凍結値）の出力
    （全市場・TOP500非限定）を再利用し、TOP500との積集合を leader とする。候補の除外に使う
    「D当日にvolshock」は同集合（全市場）を用いる。
    """
    # leader（=当日volshock発火）は全市場の volshock を第5周凍結値でそのまま再利用する。
    volshock_df, _vs_diag = kpi_volshock_signals.generate_volshock_signals(start_bd, end_bd)
    volshock_by_day: dict[str, set] = defaultdict(set)
    for row in volshock_df.itertuples():
        volshock_by_day[str(row.signal_date)].add(str(row.code))

    earliest_idx = bday_index[kpi_round23_signals._earliest_bars_date()]
    idx_start = bday_index[start_bd]
    warmup_idx = max(earliest_idx, idx_start - WARMUP_BDAYS)
    scan_days = all_bdays[warmup_idx : bday_index[end_bd] + 1]

    va_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=VA_HISTORY_WINDOW))

    diag = {
        "business_days_scanned": 0,  # イベント期間内の営業日数
        "days_no_universe": 0,  # D時点TOP500が未確定の日
        "leader_days": 0,  # TOP500 leader が1件以上存在した日
        "leader_count_sum": 0,
        "leader_count_max": 0,
        "sector_missing_codes": 0,  # TOP500だがS33業種欠損（延べ・日×銘柄）
        "sector_computable_sum": 0,  # leader業種の「同業種TOP500∩avg20計算可能」延べ数
        "signals_sector_sympathy": 0,
    }
    leader_counts: list[int] = []
    rows: list[dict] = []

    for d in scan_days:
        in_window = start_bd <= d <= end_bd
        bars_d = measure_base_rate.load_bars_day(d)

        if in_window:
            diag["business_days_scanned"] += 1
            top500, _mu = _top500_for_day(d, universes_by_month)
            if top500 is None:
                diag["days_no_universe"] += 1
            else:
                sector_map = _pit_sector_map(d)
                idx = bday_index[d]
                prev = all_bdays[idx - 1]
                bars_prev = measure_base_rate.load_bars_day(prev)
                volshock_today = volshock_by_day.get(d, set())

                # TOP500銘柄を業種ごとにグルーピング（avg20はD含まず＝当日更新前のdeque）。
                sector_members: dict[str, list[tuple[str, Optional[float]]]] = defaultdict(list)
                for code in top500:
                    sec = sector_map.get(code)
                    if not sec:
                        diag["sector_missing_codes"] += 1
                        continue
                    hist = va_hist.get(code)
                    avg20 = (sum(hist) / len(hist)) if (hist is not None and len(hist) == VA_HISTORY_WINDOW) else None
                    sector_members[sec].append((code, avg20))

                # TOP500 leader（=全市場volshock ∩ TOP500）とその業種。
                top500_leaders = volshock_today & top500
                leader_sectors = {sector_map.get(c) for c in top500_leaders if sector_map.get(c)}
                n_leaders = len(top500_leaders)
                leader_counts.append(n_leaders)
                diag["leader_count_sum"] += n_leaders
                diag["leader_count_max"] = max(diag["leader_count_max"], n_leaders)
                if n_leaders > 0:
                    diag["leader_days"] += 1

                fired_codes: set[str] = set()
                for sec in leader_sectors:
                    pool = sector_members.get(sec, [])
                    computable = [avg for (_c, avg) in pool if avg is not None]
                    diag["sector_computable_sum"] += len(computable)
                    if not computable:
                        continue
                    median_va = statistics.median(computable)
                    for code, avg20 in pool:
                        if code in fired_codes:
                            continue
                        if code in volshock_today:
                            continue  # D当日にvolshock（=他銘柄でなく主導株）→候補でない
                        if avg20 is None or avg20 < median_va:
                            continue  # 換金性（業種内中央値以上）未達
                        rec_d = bars_d.get(code)
                        prev_rec = bars_prev.get(code)
                        adjc_d = rec_d.get("AdjC") if rec_d else None
                        adjc_prev = prev_rec.get("AdjC") if prev_rec else None
                        if adjc_d is None or adjc_prev is None or adjc_prev <= 0:
                            continue
                        day_ret = adjc_d / adjc_prev - 1
                        if not (T1_DAY_RET_MIN <= day_ret <= T1_DAY_RET_MAX):
                            continue
                        fired_codes.add(code)
                        diag["signals_sector_sympathy"] += 1
                        rows.append(
                            {
                                "signal_date": d,
                                "code": code,
                                "sector": sec,
                                "n_leaders": n_leaders,
                                "avg20_va": avg20,
                                "sector_median_va": median_va,
                                "day_ret": day_ret,
                            }
                        )

        # 履歴更新は当日の判定より後（当日Vaは翌日以降の「直近20回」の一員としてのみ蓄積）。
        for code, rec in bars_d.items():
            va = rec.get("Va")
            if va and va > 0:
                va_hist[code].append(va)

    if leader_counts:
        diag["leader_count_mean"] = round(sum(leader_counts) / len(leader_counts), 2)
    return pd.DataFrame(rows), diag


# --- T2: ll_release_rebound シグナル生成（新規ロジック） --------------------------


def generate_ll_release_rebound_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]内から ストップ安解放反発シグナルを生成する（カタログ§7-R T2）。

    連続LL（L="1"）を1エピソードとし最終LL日をEとする。E+1〜E+10営業日内で初の
    (LL=="0" ∩ AdjC>AdjO ∩ Va≥20日平均(D含まず)×1.5) の日DでシグナルをDに確定する。
    先行エピソード優先で同一Dの重複発火なし。同一銘柄の再発火はDから10営業日クールダウン。
    """
    earliest_idx = bday_index[kpi_round23_signals._earliest_bars_date()]
    idx_start = bday_index[start_bd]
    warmup_idx = max(earliest_idx, idx_start - WARMUP_BDAYS)
    scan_days = all_bdays[warmup_idx : bday_index[end_bd] + 1]

    va_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=VA_HISTORY_WINDOW))
    active_run_last: dict[str, str] = {}  # 現在LL連続中の code -> 最後にLL=="1"を観測した日
    pending: dict[str, list[dict]] = defaultdict(list)  # code -> [{E, scanned}] （E昇順=先行優先）
    cooldown_until_idx: dict[str, int] = {}  # code -> この営業日indexまで新規発火を抑制

    diag = {
        "business_days_scanned": 0,
        "ll_episodes_total": 0,  # スキャン中に検出したLLエピソード数（助走含む）
        "signals_ll_release_rebound": 0,
        "episodes_expired_no_rebound": 0,  # E+10までに条件不成立
        "dedup_cooldown_suppressed": 0,  # 反発条件は満たすがクールダウン/同一Dで抑制
    }
    rows: list[dict] = []

    for d in scan_days:
        i = bday_index[d]
        in_window = start_bd <= d <= end_bd
        bars_d = measure_base_rate.load_bars_day(d)
        if in_window:
            diag["business_days_scanned"] += 1

        # (1) 連続LLの終了検出: active な code が今日 LL=="1" でなければ（LL=="0"/欠測）ランは前日で終了。
        for code in list(active_run_last):
            rec = bars_d.get(code)
            ll = rec.get("LL") if rec else None
            if ll != "1":
                e_day = active_run_last.pop(code)
                pending[code].append({"E": e_day, "scanned": 0})
                diag["ll_episodes_total"] += 1

        # (2) 反発スキャン: pending エピソードについて今日Dを E+1..E+10 の1日として評価する。
        #     凍結定義は「E+1〜E+10で最初の適格日D」。適格日(LL=='0'∩陽線∩Va≥20日平均×1.5)に
        #     到達したエピソードは、発火可否(クールダウン中・同一D重複)に関わらずその場で必ず消費する
        #     （Codexレビュー㉜: 最初の適格日がクールダウン中でも、後日の別適格日で発火させない）。
        for code in list(pending.keys()):
            rec = bars_d.get(code)
            eps = pending[code]
            still: list[dict] = []
            fired_today = False
            for ep in eps:  # E昇順（append順=時系列）＝先行エピソード優先
                ep["scanned"] += 1
                eligible = False
                avg20_fire: Optional[float] = None
                if rec is not None and in_window:
                    ll = rec.get("LL")
                    adjc = rec.get("AdjC")
                    adjo = rec.get("AdjO")
                    va = rec.get("Va")
                    hist = va_hist.get(code)
                    if (
                        ll == "0"
                        and adjc is not None
                        and adjo is not None
                        and adjc > adjo
                        and va
                        and hist is not None
                        and len(hist) == VA_HISTORY_WINDOW
                    ):
                        avg20 = sum(hist) / len(hist)
                        if avg20 > 0 and va >= avg20 * T2_VOL_MULTIPLIER:
                            eligible = True
                            avg20_fire = avg20
                if eligible:
                    # 最初の適格日Dに到達＝このエピソードはここで必ず消費する（still に残さない）。
                    cu = cooldown_until_idx.get(code)
                    suppressed = fired_today or (cu is not None and i <= cu)
                    if suppressed:
                        diag["dedup_cooldown_suppressed"] += 1  # クールダウン中/同一Dで抑止（発火なし・消費）
                    else:
                        diag["signals_ll_release_rebound"] += 1
                        cooldown_until_idx[code] = i + T2_COOLDOWN_BDAYS
                        fired_today = True
                        rows.append(
                            {
                                "signal_date": d,
                                "code": code,
                                "ll_episode_end": ep["E"],
                                "days_since_e": ep["scanned"],
                                "va": rec.get("Va"),
                                "avg20_va": avg20_fire,
                                "adjc": rec.get("AdjC"),
                                "adjo": rec.get("AdjO"),
                            }
                        )
                    continue  # 適格日到達＝消費（発火有無に関わらず still に残さない）
                if ep["scanned"] >= T2_REBOUND_WINDOW_BDAYS:
                    diag["episodes_expired_no_rebound"] += 1
                    continue  # E+10到達・不発
                still.append(ep)
            if still:
                pending[code] = still
            else:
                del pending[code]

        # (3) 今日のLL=="1"を反映（ラン開始/継続）。
        for code, rec in bars_d.items():
            if rec.get("LL") == "1":
                active_run_last[code] = d

        # (4) 履歴更新（D含まず: 判定より後に当日Vaを追加）。
        for code, rec in bars_d.items():
            va = rec.get("Va")
            if va and va > 0:
                va_hist[code].append(va)

    return pd.DataFrame(rows), diag


# --- T3: gap_hold_close_strong シグナル生成（新規ロジック） -----------------------


def generate_gap_hold_close_strong_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]内から ギャップ持続・引け強シグナルを生成する（カタログ§7-R T3）。

    (AdjO≥前日AdjC×1.03) ∩ (AdjC≥AdjO) ∩ ((AdjC-AdjL)/(AdjH-AdjL)≥0.8・AdjH>AdjL) ∩
    (AdjL≥前日AdjC＝窓埋めなし) ∩ (Va≥20日平均(D含まず)×2)。OHLCV全て有限・正・AdjH>AdjL・Va>0。
    """
    earliest_idx = bday_index[kpi_round23_signals._earliest_bars_date()]
    idx_start = bday_index[start_bd]
    warmup_idx = max(earliest_idx, idx_start - WARMUP_BDAYS)
    scan_days = all_bdays[warmup_idx : bday_index[end_bd] + 1]

    va_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=VA_HISTORY_WINDOW))

    diag = {
        "business_days_scanned": 0,
        "code_day_observations": 0,
        "ohlcv_quality_fail": 0,
        "prev_close_missing": 0,
        "insufficient_va_history": 0,
        "pass_gap": 0,
        "pass_close_ge_open": 0,
        "pass_close_location": 0,
        "pass_no_gap_fill": 0,
        "signals_gap_hold_close_strong": 0,
    }
    rows: list[dict] = []

    prev_bars: Optional[dict] = None
    for d in scan_days:
        in_window = start_bd <= d <= end_bd
        bars_d = measure_base_rate.load_bars_day(d)
        if in_window:
            diag["business_days_scanned"] += 1
            for code, rec in bars_d.items():
                diag["code_day_observations"] += 1
                adjo = rec.get("AdjO")
                adjh = rec.get("AdjH")
                adjl = rec.get("AdjL")
                adjc = rec.get("AdjC")
                va = rec.get("Va")
                # OHLCV品質ガード（全て有限・正・AdjH>AdjL・Va>0）。
                vals = [adjo, adjh, adjl, adjc, va]
                if any(v is None for v in vals) or any(
                    (not isinstance(v, (int, float)) or v != v or v <= 0) for v in vals
                ):
                    diag["ohlcv_quality_fail"] += 1
                    continue
                if not (adjh > adjl):
                    diag["ohlcv_quality_fail"] += 1
                    continue
                prev_rec = (prev_bars or {}).get(code)
                adjc_prev = prev_rec.get("AdjC") if prev_rec else None
                if adjc_prev is None or adjc_prev <= 0:
                    diag["prev_close_missing"] += 1
                    continue
                if not (adjo >= adjc_prev * T3_GAP_RATIO):
                    continue
                diag["pass_gap"] += 1
                if not (adjc >= adjo):
                    continue
                diag["pass_close_ge_open"] += 1
                close_loc = (adjc - adjl) / (adjh - adjl)
                if not (close_loc >= T3_CLOSE_LOC_MIN):
                    continue
                diag["pass_close_location"] += 1
                if not (adjl >= adjc_prev):
                    continue  # 窓埋めなし
                diag["pass_no_gap_fill"] += 1
                hist = va_hist.get(code)
                if hist is None or len(hist) < VA_HISTORY_WINDOW:
                    diag["insufficient_va_history"] += 1
                    continue
                avg20 = sum(hist) / len(hist)
                if not (avg20 > 0 and va >= avg20 * T3_VOL_MULTIPLIER):
                    continue
                diag["signals_gap_hold_close_strong"] += 1
                rows.append(
                    {
                        "signal_date": d,
                        "code": code,
                        "adjo": adjo,
                        "adjc_prev": adjc_prev,
                        "gap_ratio": adjo / adjc_prev,
                        "close_location": close_loc,
                        "adjl": adjl,
                        "adjc": adjc,
                        "va": va,
                        "avg20_va": avg20,
                    }
                )

        # 履歴更新（D含まず）。
        for code, rec in bars_d.items():
            va = rec.get("Va")
            if va and va > 0:
                va_hist[code].append(va)
        prev_bars = bars_d

    return pd.DataFrame(rows), diag


# --- 共通: ハーネス実行 + 探索的一次結論 + 台帳記録（round23 run_trial 標準フローを踏襲） ---


def run_trial(
    signals_df: pd.DataFrame,
    kpi_name: str,
    base_params: dict,
    harness_ctx: dict,
    report_extra_lines: Optional[list[str]] = None,
    append_to_ledger: bool = True,
) -> dict:
    """低レベルCanonical関数を直接呼び出してハーネス実行〜探索的結論〜台帳記録までを行う
    （kpi_round23_signals.run_trial と同一の標準フロー。全試行 defer_entry=True）。"""
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
        f.write("\n## 探索的一次結論（第29周・カタログ§7-R事前登録の共通ルール）\n")
        f.write(
            f"- EV(なし・往復コスト0.3%控除込)点推定 = {ev_ci['point_ev']:.4%}"
            f"（月次ブロック・ブートストラップ95%CI=[{ev_ci['ci_low']:.4%}, {ev_ci['ci_high']:.4%}]"
            f"・有効ブートストラップ数={ev_ci['n_boot_valid']}）\n"
            if ev_ci["point_ev"] is not None
            else "- EV(なし)点推定 = 計算不能\n"
        )
        f.write(f"- リフト点推定 = {stats.get('point_lift')}\n")
        f.write(f"- **探索的一次結論: {conclusion}**\n")
        f.write(f"- {MULTI_TRIAL_NOTE}\n")
        if report_extra_lines:
            f.write("\n## 近傍事実・必須診断・注記（§7-R）\n")
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
        f"{kpi_name} 完了: n={stats['n']} lift={stats.get('point_lift')} verdict(§6)={verdict} "
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


def run_t1(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T1 {T1_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd = generate_sector_sympathy_signals(
        shared["start_bd"], shared["end_bd"], hc["all_bdays"], hc["bday_index"], hc["universes_by_month"]
    )
    print(
        f"T1 シグナル生成完了: {len(signals_df)}件 (営業日走査={gd['business_days_scanned']}, "
        f"ユニバース未確定日={gd['days_no_universe']}, leader日数={gd['leader_days']}, "
        f"leader数[mean/max]=[{gd.get('leader_count_mean')}/{gd['leader_count_max']}], "
        f"業種欠損(延べ)={gd['sector_missing_codes']}, 計算可能(延べ)={gd['sector_computable_sum']}, "
        f"シグナル成立={gd['signals_sector_sympathy']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T1シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T1_KPI_NAME, hc)

    base_params = {
        "signal_definition": (
            "同一S33業種内の他銘柄(TOP500)がD当日にvolshock_5x(第5周凍結値)を満たしたとき、"
            f"同業種TOP500内でvolshock未成立・当日リターン{T1_DAY_RET_MIN:+.0%}〜{T1_DAY_RET_MAX:+.0%}・"
            "20日平均Va(D含まず)≥業種内中央値(同業種TOP500∩計算可能)の銘柄。D日1回発火"
        ),
        "day_ret_min": T1_DAY_RET_MIN,
        "day_ret_max": T1_DAY_RET_MAX,
        "va_history_window": VA_HISTORY_WINDOW,
        "leader_source": "kpi_volshock_signals.generate_volshock_signals(第5周凍結値・倍率5/陽線/前日比+2〜8%)",
        "top500_pit": "_universe_membershipと同一PIT規約(signal_monthより厳密に前の直近確定月末ユニバース)",
        "sector_pit": "S33はD以前の直近月末master(measure_base_rate.load_master_day)",
        "reaction": "シグナル確定=T終値・エントリー=T+1寄付",
        "diag_leader_days": gd["leader_days"],
        "diag_leader_count_mean": gd.get("leader_count_mean"),
        "diag_leader_count_max": gd["leader_count_max"],
        "diag_sector_missing_codes": gd["sector_missing_codes"],
        "diag_sector_computable_sum": gd["sector_computable_sum"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "neighbor_note": (
            "sector_momentum_laggard(業種モメンタム×出遅れ・fail)とは判定軸が異なる(本試行はイベント駆動="
            "当日の主導株volshock)。volshock_5x自体の再検証ではない(発火主体が他銘柄)"
        ),
    }
    report_extra = [
        f"必須診断: leader日数={gd['leader_days']} / leader数[mean/max]=[{gd.get('leader_count_mean')}/{gd['leader_count_max']}] "
        f"/ 業種欠損(延べ日×銘柄)={gd['sector_missing_codes']} / 同業種TOP500∩計算可能(延べ)={gd['sector_computable_sum']}。",
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        base_params["neighbor_note"],
    ]
    return run_trial(
        filtered_df, T1_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
    )


def run_t2(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T2 {T2_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd = generate_ll_release_rebound_signals(
        shared["start_bd"], shared["end_bd"], hc["all_bdays"], hc["bday_index"]
    )
    print(
        f"T2 シグナル生成完了: {len(signals_df)}件 (営業日走査={gd['business_days_scanned']}, "
        f"LLエピソード数={gd['ll_episodes_total']}, 不発(E+10到達)={gd['episodes_expired_no_rebound']}, "
        f"重複除去(クールダウン/同一D抑制)={gd['dedup_cooldown_suppressed']}, "
        f"シグナル成立={gd['signals_ll_release_rebound']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T2シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T2_KPI_NAME, hc)

    base_params = {
        "signal_definition": (
            "連続LL(=ストップ安)を1エピソード・最終LL日E。E+1〜E+10営業日内で初の"
            f"(LL=='0' ∩ AdjC>AdjO ∩ Va≥20日平均(D含まず)×{T2_VOL_MULTIPLIER})の日D。"
            f"先行エピソード優先・同一D重複なし・同一銘柄はDから{T2_COOLDOWN_BDAYS}営業日クールダウン"
        ),
        "rebound_window_bdays": T2_REBOUND_WINDOW_BDAYS,
        "vol_multiplier": T2_VOL_MULTIPLIER,
        "cooldown_bdays": T2_COOLDOWN_BDAYS,
        "va_history_window": VA_HISTORY_WINDOW,
        "reaction": "シグナル確定=T終値・エントリー=T+1寄付",
        "diag_ll_episodes_total": gd["ll_episodes_total"],
        "diag_episodes_expired_no_rebound": gd["episodes_expired_no_rebound"],
        "diag_dedup_cooldown_suppressed": gd["dedup_cooldown_suppressed"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "neighbor_note": (
            "sell_reg_trigger_rebound(-10%到達・lift2.76)と機構は同族だがトリガーが"
            "『LL付着→解放確認』の2段階。sh_dip_reentry(S高後押し目=逆側)とも別。"
            "リバーサル家族のEV負傾向を割引の基準として持ち込む"
        ),
    }
    report_extra = [
        f"必須診断: LLエピソード数={gd['ll_episodes_total']} / 不発(E+10到達)={gd['episodes_expired_no_rebound']} "
        f"/ 重複除去(クールダウン・同一D抑制)={gd['dedup_cooldown_suppressed']}。",
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        base_params["neighbor_note"],
    ]
    return run_trial(
        filtered_df, T2_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
    )


def run_t3(shared: dict) -> dict:
    hc = shared["harness_ctx"]
    print(f"T3 {T3_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gd = generate_gap_hold_close_strong_signals(
        shared["start_bd"], shared["end_bd"], hc["all_bdays"], hc["bday_index"]
    )
    print(
        f"T3 シグナル生成完了: {len(signals_df)}件 (営業日走査={gd['business_days_scanned']}, "
        f"銘柄日観測={gd['code_day_observations']}, OHLCV品質除外={gd['ohlcv_quality_fail']}, "
        f"前日終値なし={gd['prev_close_missing']}, Va履歴不足={gd['insufficient_va_history']}, "
        f"gap通過={gd['pass_gap']}, 引け≥寄付通過={gd['pass_close_ge_open']}, "
        f"close位置通過={gd['pass_close_location']}, 窓埋めなし通過={gd['pass_no_gap_fill']}, "
        f"シグナル成立={gd['signals_gap_hold_close_strong']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T3シグナルが0件です")
    filtered_df, n_raw, n_filtered = _prefilter_and_save(signals_df, T3_KPI_NAME, hc)

    base_params = {
        "signal_definition": (
            f"(AdjO≥前日AdjC×{T3_GAP_RATIO}) ∩ (AdjC≥AdjO) ∩ "
            f"((AdjC-AdjL)/(AdjH-AdjL)≥{T3_CLOSE_LOC_MIN}・AdjH>AdjL) ∩ (AdjL≥前日AdjC=窓埋めなし) ∩ "
            f"(Va≥20日平均(D含まず)×{T3_VOL_MULTIPLIER})。OHLCV全て有限・正・AdjH>AdjL・Va>0"
        ),
        "gap_ratio": T3_GAP_RATIO,
        "close_location_min": T3_CLOSE_LOC_MIN,
        "vol_multiplier": T3_VOL_MULTIPLIER,
        "va_history_window": VA_HISTORY_WINDOW,
        "reaction": "シグナル確定=T終値・エントリー=T+1寄付",
        "diag_ohlcv_quality_fail": gd["ohlcv_quality_fail"],
        "diag_pass_gap": gd["pass_gap"],
        "diag_pass_close_ge_open": gd["pass_close_ge_open"],
        "diag_pass_close_location": gd["pass_close_location"],
        "diag_pass_no_gap_fill": gd["pass_no_gap_fill"],
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "neighbor_note": (
            "volshock(出来高倍率主導)とは判定軸が異なる(価格構造主導・出来高は2倍の緩い条件)。"
            "high52_breakout(水準ブレイク)とも別(本試行は当日の値動きの質)"
        ),
    }
    report_extra = [
        f"必須診断(段階別通過): gap={gd['pass_gap']} / 引け≥寄付={gd['pass_close_ge_open']} "
        f"/ close位置≥{T3_CLOSE_LOC_MIN}={gd['pass_close_location']} / 窓埋めなし={gd['pass_no_gap_fill']} "
        f"/ OHLCV品質除外={gd['ohlcv_quality_fail']}。",
        f"生シグナル={n_raw}件 → ユニバース事前フィルタ後ハーネス投入={n_filtered}件(統計結果不変・第20周§7-I前例)。",
        base_params["neighbor_note"],
    ]
    return run_trial(
        filtered_df, T3_KPI_NAME, base_params, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
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
    parser = argparse.ArgumentParser(description="第29周: 新規発明イベント三本バッチ（カタログ§7-R・T1〜T3）")
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
    # holdout(2023年以降)抵触は FATAL（in-sampleのみで評価する事前登録の凍結境界を機械で強制）。
    if args.end > kpi_pead_signals.IN_SAMPLE_END:
        raise SystemExit(
            f"FATAL: --end={args.end} はholdout期間(2023年以降)に抵触します。"
            f"§7-Rはin-sample({kpi_pead_signals.IN_SAMPLE_END}まで)で評価します。holdoutは使用しません。"
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

    trials_to_run = ["t1", "t2", "t3"] if args.trial == "all" else [args.trial]
    results = []
    for t in trials_to_run:
        results.append(TRIAL_RUNNERS[t](shared))

    print("\n=== 第29周バッチ完了サマリー ===")
    for r in results:
        print(
            f"{r['kpi_name']}: n={r['n']} 月平均n={r.get('avg_monthly_n')} "
            f"lift={r['lift']}[{r['ci_low']},{r['ci_high']}] "
            f"EV(なし)={r['ev_point']}[{r['ev_ci_low']},{r['ev_ci_high']}] EV(stop8)={r['ev_stop8']} "
            f"verdict(§6)={r['verdict']} 一次結論={r['conclusion']} "
            f"entry_missing={r['entry_missing']}/raw={r['raw_signal_count']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
