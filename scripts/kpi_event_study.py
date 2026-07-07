#!/usr/bin/env python3
"""KPI検証イベントスタディ共通ハーネス（複合KPI検証の汎用基盤）。

docs/stock-algo-kpi-catalog.md の §3（複合KPIの文法とシグナル頻度予算）・
§6（評価プロトコル凍結）を実装する。個別KPIのシグナル生成器（例: scripts/kpi_pead_signals.py）
から signal_date/code の DataFrame を受け取り、フォワードリターン計算・重複シグナル除去・
ユニバース所属チェック・月次ブロック・ブートストラップCI・合格判定・レポート出力・
試行台帳追記までを一括で行う。

フォワードリターン計算（entry/exit/MFE/MAE/損切りシム）と集計ヘルパーは
scripts/measure_base_rate.py の関数をそのまま再利用する（Canonical Module原則・再実装しない）。

Usage（他KPIスクリプトから run_event_study() を直接呼ぶのが主経路。CLIは補助/デバッグ用）:
    python3 scripts/kpi_event_study.py --signals-csv output/kpi/foo/signals_raw.csv \
        --kpi-name foo --start 2016-11 --end 2022-11 --params-json '{"threshold": 0.08}'
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: compute_forward_return_for_code/summarize等を再利用)

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

N_BOOTSTRAP = 1000
BOOTSTRAP_SEED = 42  # 固定シード（試行台帳の再現性のため。再実行しても同じCIが出る）

# §6 評価プロトコル凍結（合格基準・以後変更しない）
MIN_N = 100
MIN_MONTHS_SPANNED = 24
MIN_LIFT_CI_LOW = 1.5
MIN_EV_MONTHLY = 0.03  # EV >= +3%/月（判定にはstop8=-8%損切り版を用いる。§0-4「損切り-5〜-8%を前提」に整合）
MIN_AVG_MONTHLY_SIGNALS = 5  # §3「最低シグナル頻度」

# 第4周: エントリー繰り延べ（§6手順6「S高等で買えない日は翌日に繰り延べ」実装）
MAX_ENTRY_DEFER_BDAYS = 3  # S高/売買停止等でT+1に約定不能な場合、最大何営業日まで繰り延べるか

DEFAULT_BASE_RATE_DIR = Path("output/base_rate")
DEFAULT_OUTPUT_DIR = Path("output/kpi")
DEFAULT_TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")

VERDICT_LABELS_JA = {
    "in_sample_pass_candidate": "合格候補（in-sample。holdout 2023以降は未実施のため最終合格ではない）",
    "fail": "不合格（point推定リフト<=1倍で濃縮効果なし）",
    "pending": "判定保留（一部基準未達）",
}


# --- ベースレート・ユニバース読み込み -------------------------------------------


def load_base_rate_by_month(base_rate_dir: Path, universe_window: int) -> dict[str, float]:
    """scripts/measure_base_rate.py が出力した月次ベースレート(P20)を month -> p20 の dict で返す。"""
    path = base_rate_dir / f"base_rate_monthly_w{universe_window}.csv"
    if not path.exists():
        raise SystemExit(
            f"FATAL: ベースレートCSVが見つかりません: {path}\n"
            f"先に `scripts/measure_base_rate.py` を実行してください。"
        )
    df = pd.read_csv(path, dtype={"month": str})
    return dict(zip(df["month"], df["p20"]))


def load_universe_by_month(base_rate_dir: Path, universe_window: int) -> dict[str, set]:
    """scripts/measure_base_rate.py が出力した月次ユニバースを month -> set(code) の dict で返す。"""
    path = base_rate_dir / f"universes_w{universe_window}.csv.gz"
    if not path.exists():
        raise SystemExit(f"FATAL: ユニバースCSVが見つかりません: {path}")
    df = pd.read_csv(path, dtype={"month": str, "code": str})
    universes: dict[str, set] = {}
    for month, g in df.groupby("month"):
        universes[month] = set(g["code"])
    return universes


def _universe_membership(
    code: str, signal_month: str, universes_by_month: dict[str, set]
) -> tuple[bool, str]:
    """シグナル発生時点で「既に確定していた直近月末」のユニバース所属を判定する。

    universes_by_month[M] は暦月Mの月末営業日Tまでの売買代金データで構築されている
    （scripts/measure_base_rate.py build_universe）。シグナルが月Mの月末より前（=ほぼ全てのケース）
    に発生した場合、universes_by_month[M] をそのまま使うと月末までの未来の出来高データで
    所属判定することになりlook-ahead違反になる。そのため必ず signal_month より**厳密に前**の
    月（=直近の確定済み月末ユニバース）を使う。

    Returns:
        (in_universe, universe_month_used)。確定済みユニバースが1件も見つからない場合は (False, "")。
    """
    earlier_months = [m for m in universes_by_month if m < signal_month]
    if not earlier_months:
        return (False, "")
    fallback_month = max(earlier_months)
    return (code in universes_by_month[fallback_month], fallback_month)


# --- シグナルごとのフォワードリターン計算・重複除去・ユニバース所属チェック -----------


def compute_forward_return_deferred(
    code: str,
    t_date: str,
    bday_index: dict[str, int],
    all_bdays: list[str],
) -> Optional[dict]:
    """S高・売買停止等でT+1にAdjOが約定不能な場合、最大MAX_ENTRY_DEFER_BDAYS営業日まで
    エントリーを繰り延べる版のフォワードリターン計算（第4周・§6手順6「S高で買えない日は
    エントリー翌日に繰り延べ」の実装）。

    scripts/measure_base_rate.py は凍結（変更禁止）のため、`compute_forward_return_for_code()`
    のエントリー確定後の計算ロジック（MFE/MAE・損切りシミュレーション部分）をここに複製している。
    これは Canonical Module 原則に対する意図的な例外である（frozen モジュールを一切変更せずに
    エントリー日決定ロジックだけを差し替える必要があるため）。呼び出し元は run_event_study の
    defer_entry フラグで measure_base_rate.compute_forward_return_for_code とこの関数を
    排他的に切り替える（両方が同時に呼ばれることはない）。

    探索はT+1, T+2, ...と各日を実際に到達して初めてAdjOの有無が分かる前提のみを使う
    （T+1到達時点でT+2以降のデータは一切参照しない=look-aheadなし）。
    exit は「シグナル日T+1から数えて20営業日後」固定ではなく「実際に約定できたエントリー日から
    20営業日後」に後ろへずれる（繰り延べた分だけウィンドウ全体がスライドする）。

    Returns:
        measure_base_rate.compute_forward_return_for_code と同じ列 + defer_bdays
        （0=繰り延べなし・T+1で約定, 1-3=繰り延べ発生）。
        MAX_ENTRY_DEFER_BDAYS営業日以内に約定可能日(AdjO)が無ければ None（entry_missing集計・従来通り）。
    """
    idx = bday_index[t_date]
    entry_idx: Optional[int] = None
    entry_day: Optional[str] = None
    defer_bdays: Optional[int] = None
    for defer in range(0, MAX_ENTRY_DEFER_BDAYS + 1):
        candidate_idx = idx + 1 + defer
        if candidate_idx >= len(all_bdays):
            raise SystemExit(f"FATAL: T={t_date} の繰り延べ探索(+{defer}bd)がカレンダー範囲外です。")
        candidate_day = all_bdays[candidate_idx]
        candidate_rec = measure_base_rate.load_bars_day(candidate_day).get(code)
        if candidate_rec is not None and candidate_rec.get("AdjO"):
            entry_idx = candidate_idx
            entry_day = candidate_day
            defer_bdays = defer
            break
    if entry_day is None or entry_idx is None:
        return None

    exit_idx = entry_idx + measure_base_rate.FORWARD_WINDOW_BD
    if exit_idx >= len(all_bdays):
        raise SystemExit(
            f"FATAL: T={t_date}（繰り延べ後entry={entry_day}）の"
            f"{measure_base_rate.FORWARD_WINDOW_BD}営業日後がカレンダー範囲外です。"
        )
    fwd_days = all_bdays[entry_idx : exit_idx + 1]
    fwd_bars = {d: measure_base_rate.load_bars_day(d) for d in fwd_days}

    entry_rec = fwd_bars[entry_day][code]
    entry_price = entry_rec["AdjO"]

    max_h = entry_rec.get("AdjH") or entry_price
    min_l = entry_rec.get("AdjL") or entry_price
    last_seen_adjc = entry_rec.get("AdjC") or entry_price
    last_seen_day = entry_day

    for d in fwd_days[1:]:
        rec = fwd_bars[d].get(code)
        if rec is None:
            continue  # 売買停止等の欠損日はスキップ（MFE/MAE・last_seenの更新をしない）
        if rec.get("AdjH") is not None:
            max_h = max(max_h, rec["AdjH"])
        if rec.get("AdjL") is not None:
            min_l = min(min_l, rec["AdjL"])
        if rec.get("AdjC") is not None:
            last_seen_adjc = rec["AdjC"]
            last_seen_day = d

    exit_target_day = fwd_days[-1]
    exit_target_rec = fwd_bars[exit_target_day].get(code)
    if exit_target_rec is not None and exit_target_rec.get("AdjC"):
        exit_price = exit_target_rec["AdjC"]
        exit_date = exit_target_day
        delisted_flag = 0
    else:
        exit_price = last_seen_adjc
        exit_date = last_seen_day
        delisted_flag = 1

    ret = exit_price / entry_price - 1
    mfe = max_h / entry_price - 1
    mae = min_l / entry_price - 1

    stop_rets = {}
    for name, s in measure_base_rate.STOP_LEVELS.items():
        threshold = entry_price * (1 - s)
        stop_price = None
        for d in fwd_days:
            if d > last_seen_day:
                break  # 上場廃止後は約定しえない
            rec = fwd_bars[d].get(code)
            if rec is None:
                continue
            ao = rec.get("AdjO")
            al = rec.get("AdjL")
            if ao is not None and ao <= threshold:
                stop_price = ao  # ギャップダウン: 始値で決済
                break
            if al is not None and al <= threshold:
                stop_price = threshold  # 指値相当: 損切り水準そのもので決済
                break
        if stop_price is None:
            stop_price = exit_price  # 発動なし: 通常のイグジット価格
        stop_rets[name] = (stop_price / entry_price - 1) - measure_base_rate.ROUND_TRIP_COST

    row = {
        "entry_date": entry_day,
        "exit_date": exit_date,
        "entry_price": entry_price,
        "ret": ret,
        "mfe": mfe,
        "mae": mae,
        "delisted_flag": delisted_flag,
        "ret_stop8": stop_rets["stop8"],
        "ret_stop10": stop_rets["stop10"],
        "defer_bdays": defer_bdays,
    }
    for name, level in measure_base_rate.MAE_TOUCH_LEVELS:
        row[name] = int(mae <= level)
    for name, level in measure_base_rate.MFE_TOUCH_LEVELS:
        row[name] = int(mfe >= level)
    return row


def compute_signal_returns(
    signals_df: pd.DataFrame,
    bday_index: dict[str, int],
    all_bdays: list[str],
    regime_by_day: dict[str, str],
    universes_by_month: dict[str, set],
    defer_entry: bool = False,
) -> tuple[pd.DataFrame, dict]:
    """シグナルごとにフォワードリターンを計算し、重複除去・ユニバース所属チェックを行う。

    重複除去（§3「同時点火の扱い」＝1銘柄1エントリー原則の時系列版）:
    同一銘柄の複数シグナルは、直前に「実際にエントリーしたはずのシグナル」(=in_universeだったもの)の
    exit_date以前に発生したものを破棄する（ユニバース外で実際にはトレードされないシグナルは
    ポジションを占有しないため、後続シグナルをブロックしない。破棄されたシグナルはフォワード
    リターンを計算しない＝無駄な計算をしない）。

    Returns:
        (result_df, diag)。result_df は採用シグナル全件（in_universe列で所属フラグ付き。
        除外はせず、レポート側でフィルタする＝生データはCSVに全件残す）。
        diag は件数の内訳（raw/重複破棄/entry_missing/ユニバース外。defer_entry=True時は
        繰り延べで約定できた件数の内訳 defer_1bd/defer_2bd/defer_3bd も含む＝ユニバース内外
        問わず全採用シグナルが対象。判定対象=ユニバース内に限定した繰り延べ統計は
        run_event_study 側の _compute_defer_stats で別途算出する）。
    """
    diag = {
        "raw_signal_count": int(len(signals_df)),
        "duplicate_discarded": 0,
        "entry_missing": 0,
        "out_of_universe": 0,
    }
    if defer_entry:
        diag["defer_1bd"] = 0
        diag["defer_2bd"] = 0
        diag["defer_3bd"] = 0
    rows: list[dict] = []
    for code, grp in signals_df.groupby("code"):
        grp_sorted = grp.sort_values("signal_date")
        last_exit_date: Optional[str] = None
        for _, sig in grp_sorted.iterrows():
            signal_date = str(sig["signal_date"])
            if last_exit_date is not None and signal_date <= last_exit_date:
                diag["duplicate_discarded"] += 1
                continue
            if signal_date not in bday_index:
                raise SystemExit(
                    f"FATAL: signal_date={signal_date}（code={code}）がカレンダーの営業日と一致しません。"
                )
            if defer_entry:
                result = compute_forward_return_deferred(code, signal_date, bday_index, all_bdays)
            else:
                result = measure_base_rate.compute_forward_return_for_code(
                    code, signal_date, bday_index, all_bdays
                )
            if result is None:
                diag["entry_missing"] += 1
                continue
            if defer_entry:
                defer_bdays = result.get("defer_bdays") or 0
                if defer_bdays == 1:
                    diag["defer_1bd"] += 1
                elif defer_bdays == 2:
                    diag["defer_2bd"] += 1
                elif defer_bdays == 3:
                    diag["defer_3bd"] += 1

            signal_month = signal_date[:6]
            in_universe, universe_month_used = _universe_membership(
                code, signal_month, universes_by_month
            )
            if not in_universe:
                diag["out_of_universe"] += 1
            else:
                # ユニバース内=実際にエントリーしたはずのシグナルのみポジションを占有する
                last_exit_date = result["exit_date"]

            row = {
                "signal_date": signal_date,
                "code": code,
                "month": signal_month,
                "regime": regime_by_day.get(signal_date, "unknown"),
                "in_universe": in_universe,
                "universe_month_used": universe_month_used,
                **result,
            }
            rows.append(row)

    result_df = pd.DataFrame(rows)
    return result_df, diag


# --- 月次ブロック・ブートストラップCI（§6手順5準拠） -----------------------------


def _is_missing_base_rate(br: Optional[float]) -> bool:
    """ベースレートが欠測（None）またはNaN（例: 取引所全面システム障害等でその月の
    ベースレート計測自体がn=0だった月）かどうかを判定する。"""
    return br is None or (isinstance(br, float) and pd.isna(br))


def _pooled_p20_and_base(
    month_list: list[str], grouped: dict[str, pd.DataFrame], base_rate_by_month: dict[str, float]
) -> tuple[Optional[float], Optional[float]]:
    """複数月（重複可＝ブートストラップ再標本用）のシグナルをプールしたP(+20%)と、
    シグナル数加重の月次ベースレートを返す。month_list に同じ月が複数回含まれる場合、
    その月の寄与もその回数分だけ重複してカウントする（ブロック・ブートストラップの定義通り）。

    呼び出し側（bootstrap_lift_ci）が既にベースレートNaN/欠測月をmonth_listから除外している
    前提だが、本関数内でも念のため二重ガードする（NaNは算術演算で伝播し合計を汚染するため
    `br is None` だけでは防げない。§6手順5準拠）。
    """
    wins = 0
    total = 0
    weighted_base_num = 0.0
    weighted_base_den = 0
    for m in month_list:
        g = grouped.get(m)
        if g is None or len(g) == 0:
            continue
        wins += int((g["ret"] >= 0.20).sum())
        n_g = len(g)
        total += n_g
        br = base_rate_by_month.get(m)
        if _is_missing_base_rate(br):
            continue
        weighted_base_num += br * n_g
        weighted_base_den += n_g
    if total == 0 or weighted_base_den == 0:
        return None, None
    return wins / total, weighted_base_num / weighted_base_den


def bootstrap_lift_ci(
    in_universe_df: pd.DataFrame,
    base_rate_by_month: dict[str, float],
    n_boot: int = N_BOOTSTRAP,
    seed: int = BOOTSTRAP_SEED,
) -> dict:
    """月次ブロック・ブートストラップでリフト倍率の点推定と95%CIを算出する（§6手順5準拠）。

    月次コホートは横断相関・系列相関を持つため、独立試行を仮定した二項CIは使わず、
    月を単位として復元抽出する（月内の相関構造を保持したまま再標本化する）。

    ベースレートがNaN/欠測の月（例: 取引所全面システム障害でその月のベースレート計測が
    n=0になった月）はリフト計算（点推定・ブートストラップの復元抽出母集団とも）から
    完全に除外する。NaNは加算で伝播するため、1ヶ月でも混入すると point_lift 全体がNaNに
    汚染される（除外は本関数のみに閉じる。全体のP(+20%)・EV等の集計(measure_base_rate.summarize)
    は in_universe_df 全件を対象に別途行われており、この除外の影響を受けない）。
    """
    all_months = sorted(in_universe_df["month"].unique())
    grouped = {m: g for m, g in in_universe_df.groupby("month")}

    valid_months = [m for m in all_months if not _is_missing_base_rate(base_rate_by_month.get(m))]
    excluded_months = [m for m in all_months if m not in valid_months]
    excluded_signal_count = int(sum(len(grouped[m]) for m in excluded_months))

    if not valid_months:
        return {
            "point_lift": None,
            "ci_low": None,
            "ci_high": None,
            "n_boot_valid": 0,
            "excluded_months": excluded_months,
            "excluded_signal_count": excluded_signal_count,
        }

    p20, base = _pooled_p20_and_base(valid_months, grouped, base_rate_by_month)
    point_lift = (p20 / base) if (p20 is not None and base) else None

    rng = np.random.default_rng(seed)
    boot_lifts = []
    for _ in range(n_boot):
        sample_months = rng.choice(valid_months, size=len(valid_months), replace=True).tolist()
        p20_b, base_b = _pooled_p20_and_base(sample_months, grouped, base_rate_by_month)
        if p20_b is None or not base_b:
            continue
        boot_lifts.append(p20_b / base_b)

    if not boot_lifts:
        return {
            "point_lift": point_lift,
            "ci_low": None,
            "ci_high": None,
            "n_boot_valid": 0,
            "excluded_months": excluded_months,
            "excluded_signal_count": excluded_signal_count,
        }

    ci_low, ci_high = np.percentile(boot_lifts, [2.5, 97.5])
    return {
        "point_lift": point_lift,
        "ci_low": float(ci_low),
        "ci_high": float(ci_high),
        "n_boot_valid": len(boot_lifts),
        "excluded_months": excluded_months,
        "excluded_signal_count": excluded_signal_count,
    }


# --- 集計・合格判定 ------------------------------------------------------------


def _month_range_count(start_month: str, end_month: str) -> int:
    """[start_month, end_month]（YYYY-MM）に含まれる暦月数を返す（両端含む）。"""
    sy, sm = (int(x) for x in start_month.split("-"))
    ey, em = (int(x) for x in end_month.split("-"))
    return (ey - sy) * 12 + (em - sm) + 1


def _compute_defer_stats(in_universe_df: pd.DataFrame) -> dict:
    """エントリー繰り延べ統計（判定対象=ユニバース内のシグナルに限定。第4周・§6手順6）。

    「救済されたシグナル」= defer_bdays > 0（=T+1では約定できず、繰り延べで初めて約定できた）
    かつユニバース内（=判定に使われる）シグナル。ベースレート（分母）側は凍結定義のまま繰り延べ
    なしのため、ここで数える「救済」は分子（in_universe_df）側だけに存在する非対称である。
    """
    if len(in_universe_df) == 0 or "defer_bdays" not in in_universe_df.columns:
        counts = {1: 0, 2: 0, 3: 0}
        rescued_n = 0
        rescued_p20 = None
    else:
        vc = in_universe_df["defer_bdays"].value_counts()
        counts = {k: int(vc.get(k, 0)) for k in (1, 2, 3)}
        rescued_df = in_universe_df[in_universe_df["defer_bdays"] > 0]
        rescued_n = len(rescued_df)
        rescued_p20 = float((rescued_df["ret"] >= 0.20).mean()) if rescued_n else None
    return {
        "defer_1bd_in_universe": counts[1],
        "defer_2bd_in_universe": counts[2],
        "defer_3bd_in_universe": counts[3],
        "rescued_n_in_universe": rescued_n,
        "rescued_p20_in_universe": rescued_p20,
    }


def compute_stats(
    in_universe_df: pd.DataFrame,
    base_rate_by_month: dict[str, float],
    period: tuple[str, str],
) -> dict:
    """判定・レポートに使う集計一式を計算する。

    avg_monthly_n は「シグナルが発生した月だけの平均」ではなく、検証期間全体の暦月数を
    分母にする（一部の月に偏って発生するKPIの実運用頻度を過大評価しないため。
    §3「年3シグナルでは運用不能」の趣旨は期間全体での可用性を指すと解釈した）。
    """
    overall = measure_base_rate.summarize(in_universe_df)
    n = overall.get("n", 0)
    months_spanned = int(in_universe_df["month"].nunique()) if n else 0
    total_period_months = _month_range_count(period[0], period[1])
    avg_monthly_n = (n / total_period_months) if total_period_months else 0.0
    has_bull = bool((in_universe_df["regime"] == "bull").any()) if n else False
    has_bear = bool((in_universe_df["regime"] == "bear").any()) if n else False

    boot = (
        bootstrap_lift_ci(in_universe_df, base_rate_by_month)
        if n
        else {
            "point_lift": None,
            "ci_low": None,
            "ci_high": None,
            "n_boot_valid": 0,
            "excluded_months": [],
            "excluded_signal_count": 0,
        }
    )

    return {
        "n": n,
        "months_spanned": months_spanned,
        "total_period_months": total_period_months,
        "avg_monthly_n": avg_monthly_n,
        "has_bull": has_bull,
        "has_bear": has_bear,
        "p20": overall.get("p20"),
        "ev_none": overall.get("ev_none"),
        "ev_stop8": overall.get("ev_stop8"),
        "ev_stop10": overall.get("ev_stop10"),
        **boot,
    }


def judge(stats: dict) -> tuple[str, list[str]]:
    """§6 評価プロトコル凍結の合格基準（プロトコル3）に照らして判定する。

    holdout（2023年以降・ロック中）はこのハーネス単体では確認できないため、
    ここでの「合格候補」は in-sample 側の基準を満たした状態を指す
    （最終合格は holdout 確認後にのみ確定する＝§6プロトコル3の⑤）。
    """
    checks = {
        f"n={stats['n']} >= {MIN_N}": stats["n"] >= MIN_N,
        f"months_spanned={stats['months_spanned']} >= {MIN_MONTHS_SPANNED}": stats["months_spanned"]
        >= MIN_MONTHS_SPANNED,
        "両レジーム(bull/bear)を跨いでいる": stats["has_bull"] and stats["has_bear"],
        f"lift_ci_low={stats['ci_low']} > {MIN_LIFT_CI_LOW}": (
            stats["ci_low"] is not None and stats["ci_low"] > MIN_LIFT_CI_LOW
        ),
        f"ev_stop8={stats['ev_stop8']} >= {MIN_EV_MONTHLY:.0%}": (
            stats["ev_stop8"] is not None and stats["ev_stop8"] >= MIN_EV_MONTHLY
        ),
        f"avg_monthly_n(期間全体平均)={stats['avg_monthly_n']:.2f} >= {MIN_AVG_MONTHLY_SIGNALS}": stats[
            "avg_monthly_n"
        ]
        >= MIN_AVG_MONTHLY_SIGNALS,
    }
    reasons = [("OK " if ok else "NG ") + label for label, ok in checks.items()]

    if all(checks.values()):
        return "in_sample_pass_candidate", reasons
    point_lift = stats.get("point_lift")
    if point_lift is not None and point_lift <= 1.0:
        return "fail", reasons
    return "pending", reasons


# --- レポート出力・試行台帳 -----------------------------------------------------


def write_report_md(
    path: Path,
    kpi_name: str,
    params: dict,
    period: tuple[str, str],
    diag: dict,
    stats: dict,
    verdict: str,
    reasons: list[str],
    in_universe_df: pd.DataFrame,
    defer_stats: Optional[dict] = None,
) -> None:
    def fmt_pct(x: Optional[float]) -> str:
        return f"{x:.2%}" if x is not None else "-"

    def fmt_ratio(x: Optional[float]) -> str:
        return f"{x:.2f}" if x is not None else "-"

    lines = [
        f"# KPI検証レポート: {kpi_name}",
        "",
        f"生成日時: {jq_fetch.now_jst().isoformat()}",
        f"検証期間: {period[0]} 〜 {period[1]}"
        "（in-sample。holdout 2023年以降はロック中・本レポートの対象外）",
        f"パラメータ: `{json.dumps(params, ensure_ascii=False)}`",
        "",
        "## シグナル内訳",
    ]
    if diag.get("regime_filter"):
        lines.append(
            f"- レジームフィルタ: {diag['regime_filter']}（フィルタ前 {diag['pre_regime_filter_count']}件"
            f" → フィルタ後 {diag['raw_signal_count']}件。TOPIX Close vs SMA200の既存レジーム判定を再利用）"
        )
    lines += [
        f"- 生シグナル数: {diag['raw_signal_count']}",
        f"- 重複除去（前シグナルのexit以前に発生）: {diag['duplicate_discarded']}",
        f"- エントリー不能（entry_missing）: {diag['entry_missing']}",
        f"- ユニバース外（除外・レポート集計対象外）: {diag['out_of_universe']}",
        f"- 採用シグナル数（in_universe, 判定対象）: {stats['n']}",
    ]
    if "defer_1bd" in diag:
        lines.append(
            f"- うち繰り延べエントリーで約定（ユニバース内外問わず・全採用シグナル基準）: "
            f"+1bd={diag['defer_1bd']} / +2bd={diag['defer_2bd']} / +3bd={diag['defer_3bd']}"
        )
    lines += [
        f"- 分散月数: {stats['months_spanned']} / "
        f"月平均シグナル数（検証期間全体={stats['total_period_months']}ヶ月が分母）: "
        f"{stats['avg_monthly_n']:.2f}",
        "",
        "## リフト・EV",
        f"- P(+20%終値) = {fmt_pct(stats['p20'])}",
        f"- リフト倍率（点推定, シグナル月加重ベースレート比） = {fmt_ratio(stats['point_lift'])}",
        f"- リフト95%CI（月次ブロック・ブートストラップ x{N_BOOTSTRAP}） = "
        f"[{fmt_ratio(stats['ci_low'])}, {fmt_ratio(stats['ci_high'])}]",
        f"- EV（損切りなし・往復コスト0.3%込） = {fmt_pct(stats['ev_none'])}/トレード",
        f"- EV（-8%損切り・判定に使用） = {fmt_pct(stats['ev_stop8'])}/トレード",
        f"- EV（-10%損切り） = {fmt_pct(stats['ev_stop10'])}/トレード",
    ]
    excluded_months = stats.get("excluded_months") or []
    if excluded_months:
        lines.append(
            f"- ベースレート欠測月({', '.join(excluded_months)})のシグナル"
            f"{stats.get('excluded_signal_count', 0)}件はリフト計算（点推定・ブートストラップとも）"
            f"から除外（P(+20%)・EV等の上記集計には含めたまま）"
        )
    lines += [
        "",
        "## 全体・レジーム別 詳細",
        measure_base_rate.STATS_HEADER,
    ]
    if len(in_universe_df) > 0:
        lines.append(
            measure_base_rate.format_stats_row("全体(in_universe)", measure_base_rate.summarize(in_universe_df))
        )
        for regime, g in in_universe_df.groupby("regime"):
            lines.append(measure_base_rate.format_stats_row(regime, measure_base_rate.summarize(g)))
    else:
        lines.append("| (シグナルなし) | 0 | - | - | - | - | - | - | - | - | - |")

    if defer_stats is not None:
        lines += [
            "",
            "## エントリー繰り延べ統計（第4周・§6手順6実装）",
            f"- 繰り延べ発生（ユニバース内・判定対象のみ）: "
            f"+1bd={defer_stats['defer_1bd_in_universe']} / "
            f"+2bd={defer_stats['defer_2bd_in_universe']} / "
            f"+3bd={defer_stats['defer_3bd_in_universe']} / "
            f"計{defer_stats['rescued_n_in_universe']}件（採用{stats['n']}件中）",
            f"- 救済分（繰り延べで初めて約定できたシグナル）のP(+20%終値) = "
            f"{fmt_pct(defer_stats['rescued_p20_in_universe'])}"
            f"（n={defer_stats['rescued_n_in_universe']}） / 全体P(+20%) = {fmt_pct(stats['p20'])}",
            "- 注記: ベースレート(分母)側は凍結定義のまま繰り延べを適用していない"
            "（measure_base_rate.py凍結のため）。したがって分子（本KPIのP20/リフト等）は"
            "繰り延べあり・分母（ベースレート）は繰り延べなしという非対称が生じる"
            "（判定への影響は軽微と考えられるが、認識した上で判定すること）",
        ]

    lines += [
        "",
        "## 判定",
        f"**{VERDICT_LABELS_JA.get(verdict, verdict)}**",
        "",
        "判定根拠（§6評価プロトコル凍結・プロトコル3の基準）:",
    ]
    lines += [f"- {r}" for r in reasons]
    lines += [
        "",
        "## 実装ノート",
        "- フォワードリターン(entry/exit/MFE/MAE/損切りシム)は `scripts/measure_base_rate.py` の"
        " `compute_forward_return_for_code` を再利用（再実装なし）",
        "- 重複除去は「同一銘柄の後続シグナルが前シグナルのexit_date以前なら破棄」のルール（§3同時点火の扱い準拠）",
        "- ユニバース所属は「シグナル発生月より前で直近確定していた月末ユニバース」で判定"
        "（同月の月末ユニバースは未来データのためlook-ahead回避で使わない）",
        "- リフトの分母は「シグナル発生月のベースレートをシグナル数で加重平均」した値（単純な全期間平均ではない）",
        "- CIは月次ブロック・ブートストラップ（独立試行を仮定した二項CIは不使用、§6手順5準拠）",
        "- ベースレートがNaN/欠測の月（例: 202009=2020-10-01のTSE全面システムトラブルで当該コホートが"
        "全銘柄エントリー不能・n=0）はリフト計算（点推定・ブートストラップの復元抽出母集団とも）から"
        "除外する。NaNは加算で伝播するため除外しないとpoint_lift全体が汚染される",
    ]
    if defer_stats is not None:
        lines.append(
            "- エントリーはT+1のAdjOが無い場合、最大+3営業日までS高・売買停止等での約定不能を"
            "繰り延べる（`compute_forward_return_deferred`。measure_base_rate.py凍結のため"
            "このハーネス内に独立実装。exitは実際の約定日+20営業日後にスライドする）"
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def append_trial(record: dict, trials_path: Path) -> None:
    """試行台帳に1行追記する（tracked正本・上書き禁止のためappendのみ）。"""
    trials_path.parent.mkdir(parents=True, exist_ok=True)
    with open(trials_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# --- メイン処理（他スクリプトから直接呼ばれる主経路） -----------------------------


def run_event_study(
    signals_df: pd.DataFrame,
    kpi_name: str,
    params: dict,
    period: tuple[str, str],
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    base_rate_dir: Path = DEFAULT_BASE_RATE_DIR,
    universe_window: int = 21,
    trials_path: Path = DEFAULT_TRIALS_PATH,
    defer_entry: bool = False,
    regime_filter: Optional[str] = None,
) -> dict:
    """KPIシグナルDataFrameを受け取り、検証〜レポート出力〜試行台帳追記までを一括実行する。

    Args:
        signals_df: 列 signal_date（YYYYMMDD文字列）・code（銘柄コード文字列）を持つDataFrame。
        kpi_name: 出力先ディレクトリ名・試行台帳のKPI名（例: "pead_initial_gap8_vol3"）。
        params: KPIのパラメータ（レポート・試行台帳に記録するのみ。計算には使わない）。
        period: (start_month, end_month) の YYYY-MM タプル（レポート表示・頻度分母に使用）。
        defer_entry: True の場合、T+1にAdjOが無い（S高・売買停止等）シグナルを最大
            MAX_ENTRY_DEFER_BDAYS営業日まで繰り延べてエントリーする（第4周・§6手順6実装）。
            False（既定）は従来通りT+1固定・約定不能ならentry_missingでスキップ。
        regime_filter: "bear"/"bull"/None（既定）。指定時は生シグナルを
            signal_date時点のレジーム（measure_base_rate.build_regime_series・
            TOPIX Close vs SMA200の既存判定をそのまま再利用）でこのレジームの日付のみに
            絞り込んでから、通常のハーネス処理（重複除去・ユニバース判定・月次加重リフト・
            ブートストラップCI）を行う（カタログ§0確認事項14「TOPIX 200日線レジーム=
            全KPIのANDゲート」の実装。リフト分母のベースレートも同じ月次加重方式のため、
            フィルタ後は自動的にbear/bull月のベースレート比になる＝別実装不要）。

    Returns:
        n/lift/ci_low/ci_high/ev_stop8/verdict/report_path/returns_path/diag/stats/defer_stats を含む dict。
    """
    if signals_df.empty:
        raise SystemExit("FATAL: signals_df が空です（シグナル生成器側の出力を確認してください）")
    missing_cols = {"signal_date", "code"} - set(signals_df.columns)
    if missing_cols:
        raise SystemExit(f"FATAL: signals_df に必須列が不足しています: {missing_cols}")
    if regime_filter is not None and regime_filter not in ("bear", "bull"):
        raise SystemExit(f"FATAL: regime_filter は 'bear'/'bull' のみ対応です（指定値: {regime_filter}）")

    signals_df = signals_df[["signal_date", "code"]].copy()
    signals_df["signal_date"] = signals_df["signal_date"].astype(str)
    signals_df["code"] = signals_df["code"].astype(str)

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    topix_close = measure_base_rate.load_topix_series()
    regime_by_day = measure_base_rate.build_regime_series(topix_close)

    pre_regime_filter_count = int(len(signals_df))
    if regime_filter is not None:
        signals_df = signals_df[
            signals_df["signal_date"].map(regime_by_day).eq(regime_filter)
        ].reset_index(drop=True)
        if signals_df.empty:
            raise SystemExit(
                f"FATAL: regime_filter={regime_filter} 適用後にシグナルが0件になりました"
                f"（フィルタ前{pre_regime_filter_count}件）"
            )

    base_rate_by_month = load_base_rate_by_month(base_rate_dir, universe_window)
    universes_by_month = load_universe_by_month(base_rate_dir, universe_window)

    returns_df, diag = compute_signal_returns(
        signals_df, bday_index, all_bdays, regime_by_day, universes_by_month, defer_entry=defer_entry
    )
    diag["regime_filter"] = regime_filter
    diag["pre_regime_filter_count"] = pre_regime_filter_count
    in_universe_df = (
        returns_df[returns_df["in_universe"]].reset_index(drop=True) if len(returns_df) else returns_df
    )

    stats = compute_stats(in_universe_df, base_rate_by_month, period)
    verdict, reasons = judge(stats)
    defer_stats = _compute_defer_stats(in_universe_df) if defer_entry else None

    kpi_dir = output_dir / kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    returns_path = kpi_dir / "returns.csv"
    report_path = kpi_dir / "report.md"
    returns_df.to_csv(returns_path, index=False)
    write_report_md(
        report_path, kpi_name, params, period, diag, stats, verdict, reasons, in_universe_df,
        defer_stats=defer_stats,
    )

    trial_record = {
        "run_id": uuid.uuid4().hex,
        "ts": jq_fetch.now_jst().isoformat(),
        "kpi_name": kpi_name,
        "params": params,
        "period": {"start": period[0], "end": period[1]},
        "n": stats["n"],
        "lift": stats["point_lift"],
        "ci_low": stats["ci_low"],
        "ci_high": stats["ci_high"],
        "ev": stats["ev_stop8"],
        "verdict": verdict,
        "entry_mode": "defer_max3bd" if defer_entry else "fixed_t1",
        "regime_filter": regime_filter,
    }
    append_trial(trial_record, trials_path)

    return {
        "n": stats["n"],
        "lift": stats["point_lift"],
        "ci_low": stats["ci_low"],
        "ci_high": stats["ci_high"],
        "ev_stop8": stats["ev_stop8"],
        "verdict": verdict,
        "report_path": report_path,
        "returns_path": returns_path,
        "diag": diag,
        "stats": stats,
        "defer_stats": defer_stats,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="KPI検証イベントスタディ共通ハーネス")
    parser.add_argument("--signals-csv", required=True, help="signal_date,code 列を持つCSV")
    parser.add_argument("--kpi-name", required=True)
    parser.add_argument("--start", required=True, help="検証期間開始 YYYY-MM（レポート表示・頻度分母に使用）")
    parser.add_argument("--end", required=True, help="検証期間終了 YYYY-MM（同上）")
    parser.add_argument("--params-json", default="{}", help="KPIパラメータをJSON文字列で（記録用）")
    parser.add_argument("--universe-window", type=int, default=21, choices=[21, 126])
    parser.add_argument("--base-rate-dir", default=str(DEFAULT_BASE_RATE_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--trials-path", default=str(DEFAULT_TRIALS_PATH))
    parser.add_argument(
        "--defer-entry", action="store_true",
        help="T+1にAdjOが無い場合、最大3営業日までエントリーを繰り延べる（第4周・§6手順6実装）",
    )
    parser.add_argument(
        "--regime-filter", choices=["bear", "bull"], default=None,
        help="signal_date時点のTOPIX 200日線レジームでシグナルを絞り込んでから集計する（第8周実装）",
    )
    args = parser.parse_args()

    if not MONTH_RE.match(args.start) or not MONTH_RE.match(args.end):
        raise SystemExit("FATAL: --start/--end は YYYY-MM 形式で指定してください")

    signals_path = Path(args.signals_csv)
    if not signals_path.exists():
        raise SystemExit(f"FATAL: signals-csv が見つかりません: {signals_path}")
    signals_df = pd.read_csv(signals_path, dtype={"signal_date": str, "code": str})

    try:
        params = json.loads(args.params_json)
    except json.JSONDecodeError as e:
        raise SystemExit(f"FATAL: --params-json のパースに失敗: {e}")

    result = run_event_study(
        signals_df=signals_df,
        kpi_name=args.kpi_name,
        params=params,
        period=(args.start, args.end),
        output_dir=Path(args.output_dir),
        base_rate_dir=Path(args.base_rate_dir),
        universe_window=args.universe_window,
        trials_path=Path(args.trials_path),
        defer_entry=args.defer_entry,
        regime_filter=args.regime_filter,
    )
    lift_str = f"{result['lift']:.2f}" if result["lift"] is not None else "-"
    ci_str = (
        f"[{result['ci_low']:.2f}, {result['ci_high']:.2f}]" if result["ci_low"] is not None else "[-, -]"
    )
    print(f"完了: n={result['n']} lift={lift_str} ci95={ci_str} verdict={result['verdict']}")
    print(f"report: {result['report_path']}")
    print(f"returns: {result['returns_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
