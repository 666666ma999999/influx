#!/usr/bin/env python3
"""第23周: 注目度・規制イベント系2本バッチ（カタログ§7-L・T1〜T2）。

docs/stock-algo-kpi-catalog.md §7-L の事前登録定義を実装する。2試行とも in-sample期間
（2016-11〜2022-11・従来と同一。holdout 2023年以降は使わない）で実行し、共通の探索的
一次結論ルール（第18周基準）を適用する。実装の流儀は scripts/kpi_event_batch_signals.py
（第18周）の run_trial 呼び出し契約と、scripts/kpi_high52_signals.py（bars全走査系生成器）を
踏襲する。

Canonical Module再利用（新規ロジックはT1/T2の生成関数のみ）:
- T1 `turnover_rank_surge`: 順位母集団の普通株判定は measure_base_rate の
  ProdCat=="011"（PROD_CAT_STOCK）をそのまま再利用（build_universe と同一判定）。master は
  月末営業日のみ格納されているため、各営業日Dには「D以前の直近月末master」をPIT適用する。
- T2 `sell_reg_trigger_rebound`: bars の AdjL(D) <= AdjC(D-1)×0.90 の単純判定。
- フォワードリターン・重複除去・ユニバース判定: kpi_event_study.compute_signal_returns
  （T1は defer_entry=True＝§6手順6の第5周以降既定方針・breakout系と整合、
   T2は defer_entry=False＝§7-L明示）。
- 集計・§6正式判定・レポート・台帳記録: kpi_event_study.compute_stats/judge/write_report_md/
  append_trial/bootstrap_ev_ci。
- 探索的一次結論ルール: kpi_event_batch_signals.classify_exploratory を再利用。
- T2参考感度（判定不使用）: compute_forward_return_deferred（defer_entry=True再実行）の
  defer_bdays==1（T+1不能→T+2約定）を「T+2寄付強制約定」、defer_bdays>=2 と全期間約定不能を
  「T+2でも欠損＝感度計算不能」として集計する（ハーネス無改変・canonical関数再利用のみ）。

Usage:
    python3 scripts/kpi_round23_signals.py --trial both
    python3 scripts/kpi_round23_signals.py --trial t1
    python3 scripts/kpi_round23_signals.py --trial both --start 2017-01 --end 2017-12 --no-trials-append
"""
from __future__ import annotations

import argparse
import functools
import sys
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst / DATA_ROOT を再利用)
import kpi_event_batch_signals  # noqa: E402  (Canonical Module: classify_exploratory を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: compute_signal_returns/compute_stats/judge/
# write_report_md/append_trial/bootstrap_ev_ci/compute_forward_return_deferred を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・MONTH_RE を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars/master読込・ProdCat判定を再利用)

PERIOD = (kpi_pead_signals.IN_SAMPLE_START, kpi_pead_signals.IN_SAMPLE_END)
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_ROOT = Path("output/kpi")

# --- 事前登録パラメータ（カタログ§7-L・事後変更禁止） -----------------------------

T1_KPI_NAME = "turnover_rank_surge"
T1_RANK_LOOKBACK_BDAYS = 20  # rank(D-20) の遡り営業日数
T1_RANK_SURGE_FROM = 301  # rank(D-20) >= 301
T1_RANK_SURGE_TO = 100  # rank(D) <= 100

T2_KPI_NAME = "sell_reg_trigger_rebound"
T2_DROP_TRIGGER = 0.90  # AdjL(D) <= AdjC(D-1) × 0.90（=前日比-10%到達の機械近似）

MULTI_TRIAL_NOTE = (
    "本ラウンドはT1〜T2の2試行同時登録であり累積試行数割引の対象。"
    "この結果単独で運用変更しない。"
)
ROUND_TAG = "23_attention_regulation_batch"

# T2近傍性注記（§7-L・strevリバーサル家族の割引解釈基準）
T2_NEIGHBOR_NOTE = (
    "近傍リバーサル家族の割引解釈基準: strev_20d(lift1.83・EV(なし)-0.65%＝濃縮は本物だがEV負)。"
    "既存strev_20d(20日累積下位)・sh_dip_reentry(S高後押し目)とは判定軸が異なる"
    "（単日ショック+規制メカニズム）が、リバーサル家族のEV負傾向を割引の基準として持ち込む。"
)


# --- master(月末) → PIT ProdCat=="011" 集合の解決 --------------------------------


@functools.lru_cache(maxsize=1)
def _master_dates() -> tuple[str, ...]:
    """data/jquants/master/ に実在する月末営業日(YYYYMMDD)を昇順で返す(ハードコードしない)。"""
    master_dir = jq_fetch.DATA_ROOT / "master"
    dates = sorted(p.name[:8] for p in master_dir.glob("*.json.gz"))
    if not dates:
        raise SystemExit(f"FATAL: master キャッシュが1件も見つかりません: {master_dir}")
    return tuple(dates)


@functools.lru_cache(maxsize=200)
def _prodcat011_set_for_master(master_date: str) -> frozenset:
    """指定月末masterのうち ProdCat=="011"(内国株券)のCode集合。
    判定は measure_base_rate.build_universe と同一(PROD_CAT_STOCK)を再利用する。"""
    master = measure_base_rate.load_master_day(master_date)
    return frozenset(
        code for code, rec in master.items() if rec.get("ProdCat") == measure_base_rate.PROD_CAT_STOCK
    )


def _pit_prodcat011_set(day: str) -> frozenset:
    """営業日 day に対し「day 以前の直近月末master」の普通株集合をPIT適用する。"""
    md = _master_dates()
    eligible = [m for m in md if m <= day]
    if not eligible:
        raise SystemExit(
            f"FATAL: {day} 以前の月末master が存在しません(最古master={md[0]})。"
            f"順位母集団の普通株判定ができません。"
        )
    return _prodcat011_set_for_master(eligible[-1])


# --- T1: turnover_rank_surge シグナル生成（新規ロジック） --------------------------


def generate_turnover_rank_surge_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]内の全営業日・全銘柄から売買代金ランク急上昇シグナルを生成する
    （カタログ§7-L T1）。

    順位母集団(各営業日D・凍結): PIT月末masterで ProdCat=="011" ∩ Va(D)>0。
    順位 = Va降順・同額はCode昇順で一意順位(1始まり)。
    シグナル: rank(D-20)>=301 かつ rank(D)<=100 かつ AdjC(D)>AdjC(D-1)(当日陽線)。
    D-20時点で順位不能(bars欠損・上場20営業日未満)は非シグナル。

    順位は日単位で1回計算しキャッシュする(日→{code:rank})。
    """
    earliest_bars = _earliest_bars_date()
    earliest_idx = bday_index[earliest_bars]

    rank_cache: dict[str, dict[str, int]] = {}

    def rank_for_day(d: str) -> dict[str, int]:
        cached = rank_cache.get(d)
        if cached is not None:
            return cached
        bars = measure_base_rate.load_bars_day(d)
        prodcat011 = _pit_prodcat011_set(d)
        pop = [
            (code, rec["Va"])
            for code, rec in bars.items()
            if code in prodcat011 and rec.get("Va") and rec["Va"] > 0
        ]
        pop.sort(key=lambda x: (-x[1], x[0]))  # Va降順・同額Code昇順
        ranks = {code: i + 1 for i, (code, _) in enumerate(pop)}
        rank_cache[d] = ranks
        return ranks

    event_days = [d for d in all_bdays if start_bd <= d <= end_bd]

    diag = {
        "business_days_scanned": 0,
        "rank_pop_min": None,
        "rank_pop_max": None,
        "rank_pop_sum": 0,
        "d20_out_of_range_days": 0,
        "code_rank_le100": 0,
        "pass_rank20_ge301": 0,
        "pass_up_day": 0,
        "signals_turnover_rank_surge": 0,
    }
    pop_sizes: list[int] = []
    rows: list[dict] = []

    for d in event_days:
        idx = bday_index[d]
        idx20 = idx - T1_RANK_LOOKBACK_BDAYS
        # D-20 の順位計算に必要な bars 収録範囲を割り込む場合、当日は全銘柄非シグナル
        if idx20 < earliest_idx:
            diag["d20_out_of_range_days"] += 1
            continue
        diag["business_days_scanned"] += 1

        rank_d = rank_for_day(d)
        pop_n = len(rank_d)
        pop_sizes.append(pop_n)
        diag["rank_pop_sum"] += pop_n

        d20 = all_bdays[idx20]
        rank_d20 = rank_for_day(d20)

        prev = all_bdays[idx - 1]
        bars_d = measure_base_rate.load_bars_day(d)
        bars_prev = measure_base_rate.load_bars_day(prev)

        for code, r in rank_d.items():
            if r > T1_RANK_SURGE_TO:
                continue
            diag["code_rank_le100"] += 1
            r20 = rank_d20.get(code)
            if r20 is None or r20 < T1_RANK_SURGE_FROM:
                continue  # D-20順位不能 or 急上昇未達 → 非シグナル
            diag["pass_rank20_ge301"] += 1

            rec_d = bars_d.get(code)
            prev_rec = bars_prev.get(code)
            adjc_d = rec_d.get("AdjC") if rec_d else None
            adjc_prev = prev_rec.get("AdjC") if prev_rec else None
            if adjc_d is None or adjc_prev is None or not (adjc_d > adjc_prev):
                continue  # 当日陽線でない or 前日終値不能 → 非シグナル
            diag["pass_up_day"] += 1

            diag["signals_turnover_rank_surge"] += 1
            rows.append(
                {
                    "signal_date": d,
                    "code": code,
                    "rank_d": r,
                    "rank_d20": r20,
                    "va_d": rec_d.get("Va"),
                    "adjc_d": adjc_d,
                    "adjc_prev": adjc_prev,
                }
            )

    if pop_sizes:
        diag["rank_pop_min"] = min(pop_sizes)
        diag["rank_pop_max"] = max(pop_sizes)
        diag["rank_pop_mean"] = round(sum(pop_sizes) / len(pop_sizes), 1)
    return pd.DataFrame(rows), diag


@functools.lru_cache(maxsize=1)
def _earliest_bars_date() -> str:
    """data/jquants/bars/ に実在する最古営業日(YYYYMMDD)。"""
    bars_dir = jq_fetch.DATA_ROOT / "bars"
    dates = sorted(p.name[:8] for p in bars_dir.glob("*.json.gz"))
    if not dates:
        raise SystemExit(f"FATAL: bars キャッシュが1件も見つかりません: {bars_dir}")
    return dates[0]


# --- T2: sell_reg_trigger_rebound シグナル生成（新規ロジック） --------------------


def generate_sell_reg_trigger_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]内の全営業日・全銘柄から空売り価格規制トリガー反発シグナルを生成する
    （カタログ§7-L T2）。

    定義(凍結): AdjL(D) <= AdjC(D-1) × 0.90(前日比-10%到達の機械近似)。
    シグナル確定=T終値・エントリー=T+1寄付。
    """
    earliest_bars = _earliest_bars_date()
    earliest_idx = bday_index[earliest_bars]
    event_days = [d for d in all_bdays if start_bd <= d <= end_bd]

    diag = {
        "business_days_scanned": 0,
        "prev_day_out_of_range": 0,
        "code_day_observations": 0,
        "prev_close_missing": 0,
        "low_missing": 0,
        "signals_sell_reg_trigger": 0,
    }
    rows: list[dict] = []

    for d in event_days:
        idx = bday_index[d]
        if idx - 1 < earliest_idx:
            diag["prev_day_out_of_range"] += 1
            continue
        diag["business_days_scanned"] += 1
        prev = all_bdays[idx - 1]
        bars_d = measure_base_rate.load_bars_day(d)
        bars_prev = measure_base_rate.load_bars_day(prev)

        for code, rec in bars_d.items():
            diag["code_day_observations"] += 1
            adjl = rec.get("AdjL")
            if adjl is None:
                diag["low_missing"] += 1
                continue
            prev_rec = bars_prev.get(code)
            adjc_prev = prev_rec.get("AdjC") if prev_rec else None
            if adjc_prev is None or adjc_prev <= 0:
                diag["prev_close_missing"] += 1
                continue
            if adjl <= adjc_prev * T2_DROP_TRIGGER:
                diag["signals_sell_reg_trigger"] += 1
                rows.append(
                    {
                        "signal_date": d,
                        "code": code,
                        "adjl_d": adjl,
                        "adjc_prev": adjc_prev,
                        "drop_pct": adjl / adjc_prev - 1,
                    }
                )

    return pd.DataFrame(rows), diag


# --- ユニバース事前フィルタ（統計結果不変の性能最適化・第20周§7-I前例） -------------


def prefilter_in_universe(
    signals_df: pd.DataFrame, universes_by_month: dict[str, set]
) -> tuple[pd.DataFrame, int, int]:
    """順リターン計算より前に、判定に絶対使われない(ユニバース外)シグナルを除去する。

    kpi_event_study.compute_signal_returns は全シグナルにフォワードリターンを計算するが、
    判定は in_universe 行のみ(in_universe_df)を使う。ユニバース外シグナルは
    (a) 統計(n/lift/EV)に一切寄与しない、(b) 重複除去では in_universe シグナルの
    exit_date でしかブロックされず、ユニバース外シグナル自身は last_exit_date を更新しないため
    後続 in_universe シグナルの採否に影響しない。したがって事前に除去しても in_universe_df は
    完全に不変(統計結果不変)。判定はハーネス内部の _universe_membership と同一のCanonical関数で
    行うため二重路にならない(§7-I filter_never_in_universe と同一設計)。

    急落母集団(T2)は小型・低流動の銘柄が大量に含まれ生シグナルが数万件に達するため、この事前
    フィルタなしでは全件順リターン計算がランタイム爆発する(第20周I1で実証済み・186738→937件圧縮)。

    Returns:
        (filtered_df, n_raw, n_filtered)。
    """
    n_raw = int(len(signals_df))
    keep_mask = []
    for _, sig in signals_df.iterrows():
        signal_month = str(sig["signal_date"])[:6]
        in_universe, _ = kpi_event_study._universe_membership(
            str(sig["code"]), signal_month, universes_by_month
        )
        keep_mask.append(in_universe)
    filtered = signals_df[pd.Series(keep_mask, index=signals_df.index)].reset_index(drop=True)
    return filtered, n_raw, int(len(filtered))


# --- T2参考感度: T+1約定不能→T+2寄付強制約定EV（判定不使用・reportのみ） -----------


def compute_t2_forced_fill_sensitivity(
    signals_df: pd.DataFrame,
    harness_ctx: dict,
) -> dict:
    """T2のentry_missing(T+1約定不能)シグナルを「T+2寄付で強制約定」した場合のEVを算出する
    （カタログ§7-L T2参考感度・判定不使用）。

    実装はハーネスを無改変で再利用する: compute_signal_returns を defer_entry=True で再実行し、
    出力の defer_bdays を canonical な繰り延べ探索(compute_forward_return_deferred)の結果として
    そのまま用いる。
      - defer_bdays==1: T+1約定不能→T+2で約定できた ＝ 「T+2寄付強制約定」成功。この ret で EV を計算。
      - defer_bdays in {2,3}: T+2も欠損しT+3/T+4で約定 ＝ 「T+2時点では感度計算不能」。件数のみ。
      - 全期間(T+1〜T+4)約定不能: diag_defer["entry_missing"](universe内外合算)として件数のみ報告。
    EVは判定対象(in_universe)に限定して集計する(実際にトレードされる母集団に合わせる)。
    往復コストは primary と同一(ROUND_TRIP_COST)を控除する。

    注意: defer実行のdedupはexit_dateのずれにより defer_entry=false実行と厳密一致しない
    (診断目的のため許容。primaryの entry_missing 件数は defer=false 実行の diag が正)。
    """
    harness_signals = signals_df[["signal_date", "code"]].copy()
    returns_df_defer, diag_defer = kpi_event_study.compute_signal_returns(
        harness_signals,
        harness_ctx["bday_index"],
        harness_ctx["all_bdays"],
        harness_ctx["regime_by_day"],
        harness_ctx["universes_by_month"],
        defer_entry=True,
    )
    if len(returns_df_defer) == 0 or "defer_bdays" not in returns_df_defer.columns:
        return {
            "t2_forced_fill_n": 0,
            "t2_forced_fill_ev": None,
            "t2_infeasible_at_t2_n": 0,
            "t2_unfillable_all_defer_n": diag_defer.get("entry_missing", 0),
        }
    in_univ = returns_df_defer[returns_df_defer["in_universe"]].reset_index(drop=True)
    filled_t2 = in_univ[in_univ["defer_bdays"] == 1]
    infeasible_t2 = in_univ[in_univ["defer_bdays"] >= 2]

    if len(filled_t2) > 0:
        ev = float(filled_t2["ret"].mean()) - measure_base_rate.ROUND_TRIP_COST
    else:
        ev = None
    return {
        "t2_forced_fill_n": int(len(filled_t2)),
        "t2_forced_fill_ev": ev,
        "t2_infeasible_at_t2_n": int(len(infeasible_t2)),
        "t2_unfillable_all_defer_n": int(diag_defer.get("entry_missing", 0)),
    }


# --- 共通: ハーネス実行 + 探索的一次結論 + 台帳記録 --------------------------------


def run_trial(
    signals_df: pd.DataFrame,
    kpi_name: str,
    base_params: dict,
    defer_entry: bool,
    harness_ctx: dict,
    exploratory_conclusion_extra: Optional[dict] = None,
    report_extra_lines: Optional[list[str]] = None,
    append_to_ledger: bool = True,
) -> dict:
    """低レベルCanonical関数を直接呼び出してハーネス実行〜探索的結論〜台帳記録までを行う
    （実装の流儀は kpi_event_batch_signals.run_trial を踏襲。台帳appendは append_to_ledger で
    制御し、smokeテスト時は False にして台帳を汚さない）。"""
    all_bdays = harness_ctx["all_bdays"]
    bday_index = harness_ctx["bday_index"]
    regime_by_day = harness_ctx["regime_by_day"]
    base_rate_by_month = harness_ctx["base_rate_by_month"]
    universes_by_month = harness_ctx["universes_by_month"]

    harness_signals_df = signals_df[["signal_date", "code"]].copy()
    returns_df, diag = kpi_event_study.compute_signal_returns(
        harness_signals_df, bday_index, all_bdays, regime_by_day, universes_by_month, defer_entry=defer_entry
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
    defer_stats = kpi_event_study._compute_defer_stats(in_universe_df) if defer_entry else None

    ev_ci = kpi_event_study.bootstrap_ev_ci(in_universe_df, ev_column="ret")
    conclusion = kpi_event_batch_signals.classify_exploratory(
        stats.get("point_lift"), ev_ci["point_ev"], ev_ci["ci_low"]
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
        "multi_trial_discount_note": MULTI_TRIAL_NOTE,
        "entry_missing": diag["entry_missing"],
        "raw_signal_count": diag["raw_signal_count"],
        "duplicate_discarded": diag["duplicate_discarded"],
        "out_of_universe": diag["out_of_universe"],
    }
    if exploratory_conclusion_extra:
        params.update(exploratory_conclusion_extra)

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
        f.write("\n## 探索的一次結論（第23周・カタログ§7-L事前登録の共通ルール）\n")
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
            f.write("\n## 近傍事実・参考感度・注記（§7-L）\n")
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


def run_t1(shared: dict) -> dict:
    start_bd, end_bd = shared["start_bd"], shared["end_bd"]
    hc = shared["harness_ctx"]
    print(f"T1 {T1_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gen_diag = generate_turnover_rank_surge_signals(
        start_bd, end_bd, hc["all_bdays"], hc["bday_index"]
    )
    print(
        f"T1 シグナル生成完了: {len(signals_df)}件 "
        f"(営業日走査={gen_diag['business_days_scanned']}, "
        f"D-20範囲外日={gen_diag['d20_out_of_range_days']}, "
        f"順位母集団[min/mean/max]=[{gen_diag['rank_pop_min']}/{gen_diag.get('rank_pop_mean')}/{gen_diag['rank_pop_max']}], "
        f"rank<=100={gen_diag['code_rank_le100']}, "
        f"rank20>=301通過={gen_diag['pass_rank20_ge301']}, "
        f"当日陽線通過={gen_diag['pass_up_day']}, "
        f"シグナル成立={gen_diag['signals_turnover_rank_surge']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T1シグナルが0件です")
    kpi_dir = OUTPUT_ROOT / T1_KPI_NAME
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_df.to_csv(kpi_dir / "signals_raw.csv", index=False)

    base_params = {
        "signal_definition": (
            f"rank(D-20)>={T1_RANK_SURGE_FROM} かつ rank(D)<={T1_RANK_SURGE_TO} かつ AdjC(D)>AdjC(D-1)。"
            f"順位=当日Va降順・同額Code昇順の一意順位(1始まり)。母集団=PIT月末masterのProdCat=='011' ∩ Va>0"
        ),
        "rank_lookback_bdays": T1_RANK_LOOKBACK_BDAYS,
        "rank_surge_from": T1_RANK_SURGE_FROM,
        "rank_surge_to": T1_RANK_SURGE_TO,
        "prodcat_stock": measure_base_rate.PROD_CAT_STOCK,
        "master_pit": "各営業日Dに『D以前の直近月末master』をPIT適用(masterは月末のみ格納)",
        "rank_pop_min": gen_diag["rank_pop_min"],
        "rank_pop_mean": gen_diag.get("rank_pop_mean"),
        "rank_pop_max": gen_diag["rank_pop_max"],
        "defer_rationale": (
            "§7-L T1は繰り延べを明示せず。§6手順6『S高で買えない日は翌日繰り延べ(第5周以降の既定方針)』"
            "に従いdefer_entry=True。breakout/momentum系(high52/volshock)と整合"
        ),
        "reaction": "シグナル確定=T終値・エントリー=T+1寄付",
        "neighbor_note": (
            "volshock系は出来高の絶対倍率、本試行は全市場内の相対順位ジャンプ(§7-D非該当を確認済み)"
        ),
    }
    report_extra = [
        "近傍性: volshock系=出来高の絶対倍率 / 本試行=全市場内の相対順位ジャンプ(§7-D非該当)。",
        f"順位母集団規模(診断): min={gen_diag['rank_pop_min']} / mean={gen_diag.get('rank_pop_mean')} "
        f"/ max={gen_diag['rank_pop_max']}(日別・ProdCat011 ∩ Va>0)。",
    ]
    return run_trial(
        signals_df, T1_KPI_NAME, base_params, defer_entry=True, harness_ctx=hc,
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
    )


def run_t2(shared: dict) -> dict:
    start_bd, end_bd = shared["start_bd"], shared["end_bd"]
    hc = shared["harness_ctx"]
    print(f"T2 {T2_KPI_NAME}: シグナル生成中...", file=sys.stderr)
    signals_df, gen_diag = generate_sell_reg_trigger_signals(
        start_bd, end_bd, hc["all_bdays"], hc["bday_index"]
    )
    print(
        f"T2 シグナル生成完了: {len(signals_df)}件 "
        f"(営業日走査={gen_diag['business_days_scanned']}, "
        f"銘柄日観測={gen_diag['code_day_observations']}, "
        f"前日終値不能で除外={gen_diag['prev_close_missing']}, "
        f"安値欠損で除外={gen_diag['low_missing']}, "
        f"シグナル成立={gen_diag['signals_sell_reg_trigger']})",
        file=sys.stderr,
    )
    if signals_df.empty:
        raise SystemExit("FATAL: T2シグナルが0件です")
    kpi_dir = OUTPUT_ROOT / T2_KPI_NAME
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_df.to_csv(kpi_dir / "signals_raw.csv", index=False)  # 生シグナル全件を保存

    # ユニバース事前フィルタ（統計結果不変の性能最適化・第20周§7-I前例）。
    # 急落母集団は数万件に達しランタイム爆発するため、判定に絶対使われないユニバース外を除去。
    filtered_df, n_raw, n_filtered = prefilter_in_universe(signals_df, hc["universes_by_month"])
    print(
        f"T2 ユニバース事前フィルタ: 生{n_raw}件 → ハーネス投入{n_filtered}件"
        f"(除去{n_raw - n_filtered}件=判定に使われないユニバース外・統計結果不変)",
        file=sys.stderr,
    )
    if filtered_df.empty:
        raise SystemExit("FATAL: T2の事前フィルタ後シグナルが0件です")

    # 参考感度: T+1約定不能→T+2寄付強制約定EV（判定不使用・reportのみ）。
    # 事前フィルタ後の判定対象母集団に対し defer 実行し、defer_bdays==1(=T+1不能→T+2約定)を
    # entry_missing の T+2 強制約定として分離する（全件対象は不要・§7-L凍結仕様に整合）。
    print("T2 参考感度(T+2強制約定・entry_missing対象)を算出中...", file=sys.stderr)
    sens = compute_t2_forced_fill_sensitivity(filtered_df, hc)

    base_params = {
        "signal_definition": f"AdjL(D) <= AdjC(D-1) × {T2_DROP_TRIGGER}(前日比-10%到達の機械近似)",
        "drop_trigger": T2_DROP_TRIGGER,
        "reaction": "シグナル確定=T終値・エントリー=T+1寄付",
        "defer_rationale": "§7-L明示: defer_entry=false(繰り延べなし・T+1約定不能はentry_missing)",
        "neighbor_note": T2_NEIGHBOR_NOTE,
        "raw_signal_count_full": n_raw,
        "harness_input_count": n_filtered,
        "prefilter_note": (
            "ユニバース事前フィルタ適用(第20周§7-I前例・統計結果不変)。"
            "kpi_event_study._universe_membershipと同一判定でユニバース外を順リターン計算前に除去。"
            "in_universe_dfは不変のためn/lift/EVは無フィルタ時と一致する"
        ),
        "t2_forced_fill_sensitivity": sens,
    }
    ev_str = f"{sens['t2_forced_fill_ev']:.4%}" if sens["t2_forced_fill_ev"] is not None else "計算不能(該当0件)"
    report_extra = [
        (
            f"生シグナル数={n_raw}件 / ユニバース事前フィルタ後のハーネス投入数={n_filtered}件"
            f"(除去{n_raw - n_filtered}件=判定に使われないユニバース外・統計結果不変の性能最適化・第20周§7-I前例)。"
        ),
        T2_NEIGHBOR_NOTE,
        (
            f"参考感度(判定不使用・T+2寄付強制約定): T+1約定不能→T+2で約定できた判定対象シグナル "
            f"n={sens['t2_forced_fill_n']} のEV(往復コスト控除込)={ev_str}。"
            f"T+2でも欠損(T+3/T+4で約定)=感度計算不能 {sens['t2_infeasible_at_t2_n']}件。"
            f"T+1〜T+4全期間約定不能={sens['t2_unfillable_all_defer_n']}件。"
        ),
        (
            "急落母集団はストップ安張り付き・売買停止でT+1約定不能率が構造的に高く、欠損の暗黙除外は"
            "最悪ケースを落とす選択バイアスになるため、entry_missing率と参考感度を必ず併記する(§7-L)。"
        ),
    ]
    result = run_trial(
        filtered_df, T2_KPI_NAME, base_params, defer_entry=False, harness_ctx=hc,
        exploratory_conclusion_extra={"t2_forced_fill_sensitivity": sens},
        report_extra_lines=report_extra, append_to_ledger=shared["append_to_ledger"],
    )
    result["t2_sensitivity"] = sens
    result["raw_signal_count_full"] = n_raw
    result["harness_input_count"] = n_filtered
    return result


TRIAL_RUNNERS = {"t1": run_t1, "t2": run_t2}


# --- メイン処理 ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="第23周: 注目度・規制イベント系2本バッチ（カタログ§7-L・T1〜T2）")
    parser.add_argument("--trial", choices=["t1", "t2", "both"], default="both")
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
        print(
            f"WARN: --end={args.end} はholdout期間(2023年以降)に抵触します。in-sample評価は"
            f"{kpi_pead_signals.IN_SAMPLE_END}までに限定してください。",
            file=sys.stderr,
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

    trials_to_run = ["t1", "t2"] if args.trial == "both" else [args.trial]
    results = []
    for t in trials_to_run:
        results.append(TRIAL_RUNNERS[t](shared))

    print("\n=== 第23周バッチ完了サマリー ===")
    for r in results:
        line = (
            f"{r['kpi_name']}: n={r['n']} 月平均n={r.get('avg_monthly_n')} "
            f"lift={r['lift']}[{r['ci_low']},{r['ci_high']}] "
            f"EV(なし)={r['ev_point']}[{r['ev_ci_low']},{r['ev_ci_high']}] EV(stop8)={r['ev_stop8']} "
            f"verdict(§6)={r['verdict']} 一次結論={r['conclusion']} "
            f"entry_missing={r['entry_missing']}/raw={r['raw_signal_count']}"
        )
        if "t2_sensitivity" in r:
            s = r["t2_sensitivity"]
            line += (
                f" | 参考感度: T+2強制約定n={s['t2_forced_fill_n']} EV={s['t2_forced_fill_ev']} "
                f"T+2欠損={s['t2_infeasible_at_t2_n']} 全期間不能={s['t2_unfillable_all_defer_n']}"
            )
        print(line)
    return 0


def _event_bd_bounds(start_month: str, end_month: str, all_bdays: list[str]) -> tuple[str, str]:
    start_bound = start_month.replace("-", "") + "01"
    end_bound = end_month.replace("-", "") + "31"
    start_bd = next((d for d in all_bdays if d >= start_bound), None)
    end_bd = next((d for d in reversed(all_bdays) if d <= end_bound), None)
    if start_bd is None or end_bd is None:
        raise SystemExit("FATAL: 指定期間に営業日が見つかりません")
    return start_bd, end_bd


if __name__ == "__main__":
    sys.exit(main())
