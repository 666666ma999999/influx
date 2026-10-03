#!/usr/bin/env python3
"""short_forward_v1 — 空売り版（sell_reg_trigger_rebound を空売りの合図に）の凍結仕様モジュール。

仕様正本: tasks/short_forward_sellreg_preregister.md（2026-10-03 登録・Codex R1〜R7 反映）。
ここには「凍結した定数・損益式・日の関門・判定」だけを置く（判定ロジックの正本＝ここ1か所）。
ランナー（scripts/short_forward_runner.py）はオーケストレーションのみで、式を持たない。
式・定数を変える時は v2 として別登録する（このファイルを書き換えない）。

v1 のスコアの意味（Codex R5）: 片脚の想定元本あたりの価格差スコア。配当の受払い・TOPIX側の費用・
逆日歩・貸株の在庫は入れない＝実売買への採用判定にはそのまま使わない。
stdlib のみ（テスト・Docker 外でも import 可）。
"""
from __future__ import annotations

import calendar
import datetime as dt
import random
import statistics

SPEC_VERSION = "short_forward_v1"

# --- 凍結定数（記録開始前に固定） -------------------------------------------------------
FROZEN_START = "20261005"      # 実装日 2026-10-03(土) の翌営業日。これより前の合図日 D は記録しない
ENTRY_OFFSET_BD = 2            # 入口 = D+2 営業日の寄り（D・D+1 は空売り価格規制の期間）
HOLD_BD = 20                   # 出口 = 入口から20営業日後の引け（= D+22）
EXIT_OFFSET_BD = ENTRY_OFFSET_BD + HOLD_BD
ROUND_TRIP_COST = 0.003        # 往復売買コスト（measure_base_rate.ROUND_TRIP_COST と同値）
BORROW_RATE_ANNUAL = 0.0115    # 制度信用の貸株料 年1.15%（実暦日数で按分・逆日歩は計算外）
DAY_GATE_MIN = 1               # 上位500社内の発火数がこの範囲の日だけ記録
DAY_GATE_MAX = 9               # 10件以上の日は skipped_day_count（2016-11〜2022-11 の遡りだけで決めて固定）
INCOMPLETE_RATIO = 0.90        # D の bars 銘柄数が通常（直近の中央値）の90%未満なら incomplete
INCOMPLETE_LOOKBACK = 20       # 「通常」の銘柄数＝直近20本の bars ファイルの銘柄数の中央値
UNIVERSE_WINDOW = 21           # 名簿＝合図月より厳密に前の直近月末の売買代金21日合計
UNIVERSE_TOP_N = 500           # 上位500社（daily_screen.UniverseCache と同じ規約）
OPEN_TIME_JST = (9, 0)         # 寄りの時刻。初検出がこれ以降なら late_detected

# --- 判定（Codex R1・R4・記録開始前に凍結） -----------------------------------------------
JUDGE_MIN_MATURED_DAYS = 60    # 成熟した日コホートが60日以上
JUDGE_MIN_MONTHS = 12          # かつ開始から12か月以上 → その最初の月末で判定
JUDGE_HARD_DEADLINE = "20271230"  # 遅くともこの日で打ち切り判定
BOOTSTRAP_N = 2000
BOOTSTRAP_SEED = 42
BOOTSTRAP_ALPHA = 0.05         # 片側95%下限
WIN_DAY_RATE_MIN = 0.55

JST = dt.timezone(dt.timedelta(hours=9))


def _date(s: str) -> dt.date:
    return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def calendar_days(entry_day: str, exit_day: str) -> int:
    """入口日〜出口日の実暦日数（貸株料の按分に使う・遡りの固定28日ではない）。"""
    return (_date(exit_day) - _date(entry_day)).days


def borrow_cost(caldays: int) -> float:
    return BORROW_RATE_ANNUAL * caldays / 365


def ret_short(entry_px: float, exit_px: float, caldays: int) -> float:
    """空売り単体: -(exit/entry - 1) - 往復コスト - 貸株料（実暦日数）。価格は入口日の尺度に揃えたもの。"""
    return -(exit_px / entry_px - 1) - ROUND_TRIP_COST - borrow_cost(caldays)


def ret_pair(r_short: float, topix_open_entry: float, topix_close_exit: float) -> float:
    """TOPIX 組: 空売り単体 + TOPIX を入口の寄りで買い出口の引けで売った騰落率。"""
    return r_short + (topix_close_exit / topix_open_entry - 1)


def day_gate(n_top500: int) -> str:
    """その日 D の上位500社内の発火数で、日の扱いを返す: 'zero' / 'recorded' / 'skipped_day_count'。"""
    if n_top500 < DAY_GATE_MIN:
        return "zero"
    if n_top500 <= DAY_GATE_MAX:
        return "recorded"
    return "skipped_day_count"


def is_incomplete(n_codes: int, recent_counts: list[int]) -> bool:
    """D の bars の銘柄数が直近の中央値の90%未満なら True（直近が無ければ判定しない＝False）。"""
    if not recent_counts:
        return False
    return n_codes < INCOMPLETE_RATIO * statistics.median(recent_counts)


def open_datetime(day: str) -> dt.datetime:
    d = _date(day)
    return dt.datetime(d.year, d.month, d.day, OPEN_TIME_JST[0], OPEN_TIME_JST[1], tzinfo=JST)


def is_late(detected_at_iso: str, entry_day: str) -> bool:
    """初検出時刻が入口日の寄り（09:00 JST）以降なら late_detected。"""
    t = dt.datetime.fromisoformat(detected_at_iso)
    if t.tzinfo is None:
        t = t.replace(tzinfo=JST)
    return t >= open_datetime(entry_day)


# --- 判定時点と判定（途中は表示だけ・合否は判定時点まで出さない） ---------------------------


def _add_months(d: dt.date, months: int) -> dt.date:
    y, m0 = divmod(d.month - 1 + months, 12)
    y += d.year
    m = m0 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def judgment_due(today: str, cohorts: dict, month_ends: list[str],
                 start: str = FROZEN_START) -> str | None:
    """判定時点に達していればその日（YYYYMMDD）を返す。未到達なら None。

    判定時点 = 開始から12か月以上 かつ その月末の時点で手に入っていた日コホート（cohort_available＝
    judge_as_of と同じ規則）が60以上 の最初の月末（today 以前の月末営業日）。遅くとも
    JUDGE_HARD_DEADLINE で打ち切り。出口日だけで数えない（3回目 (c)）。
    cohorts = {D: (最後の出口日, 全終端事象の recorded_at の最大, [ret_pair])}。
    month_ends は取引所カレンダーの月末営業日（昇順）。
    """
    min_date = _add_months(_date(start), JUDGE_MIN_MONTHS).strftime("%Y%m%d")
    for me in month_ends:
        if me > today or me >= JUDGE_HARD_DEADLINE:
            break
        if me < min_date:
            continue
        if sum(1 for c in cohorts.values() if cohort_available(c, me)) >= JUDGE_MIN_MATURED_DAYS:
            return me
    if today >= JUDGE_HARD_DEADLINE:
        return JUDGE_HARD_DEADLINE
    return None


def day_cohort_values(day_values: dict[str, float]) -> list[float]:
    return [day_values[d] for d in sorted(day_values)]


def month_block_bootstrap_lower(day_values: dict[str, float],
                                n: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED,
                                alpha: float = BOOTSTRAP_ALPHA) -> float | None:
    """月ブロック・ブートストラップ（月単位で復元抽出）の平均の片側(1-alpha)下限。"""
    by_month: dict[str, list[float]] = {}
    for d, v in day_values.items():
        by_month.setdefault(d[:6], []).append(v)
    blocks = list(by_month.values())
    if not blocks:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        vals: list[float] = []
        for _b in range(len(blocks)):
            vals.extend(blocks[rng.randrange(len(blocks))])
        means.append(sum(vals) / len(vals))
    means.sort()
    return means[int(alpha * n)]


def judge(day_values: dict[str, float], event_values: list[float]) -> dict:
    """合格の線（全部満たす）: ①1日1票平均>0 かつ 月ブロックBS片側95%下限>0 ②勝ち日率≥55%
    ③1発火1票平均>0。判定時点に達した時だけ呼ぶ（途中で呼んで停止判断に使わない）。"""
    dv = day_cohort_values(day_values)
    if not dv or not event_values:
        return {"pass": False, "reason": "no_data"}
    mean_day = sum(dv) / len(dv)
    lower = month_block_bootstrap_lower(day_values)
    win = sum(1 for v in dv if v > 0) / len(dv)
    mean_ev = sum(event_values) / len(event_values)
    c1 = mean_day > 0 and lower is not None and lower > 0
    c2 = win >= WIN_DAY_RATE_MIN
    c3 = mean_ev > 0
    return {"pass": bool(c1 and c2 and c3), "mean_day": mean_day, "bs_lower": lower,
            "win_day_rate": win, "mean_event": mean_ev, "n_days": len(dv),
            "n_events": len(event_values), "c1": c1, "c2": c2, "c3": c3}


def recorded_cutoff(due: str) -> dt.datetime:
    """判定に使える行の記録時刻の上限＝判定日 due の 23:59:59 JST（Codex 再レビュー #1）。"""
    d = _date(due)
    return dt.datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=JST)


def _parse_ts(ts: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(ts)
    return t if t.tzinfo else t.replace(tzinfo=JST)


def cohort_available(cohort: tuple, due: str) -> bool:
    """日コホートが判定時点 due で「手に入っていた」か（判定日の決定と判定計算で共通の規則・3回目 (c)）。

    cohort = (最後の出口日, その D の全終端事象〔matured・unfilled・censored〕の recorded_at の最大, [ret_pair])。
    ①最後の出口日 <= due ②全終端事象が due 当日 23:59:59 JST までに記録済み（記録時刻が1つでも無ければ不可）。
    """
    m, rec, vals = cohort
    return bool(vals) and m <= due and bool(rec) and _parse_ts(rec) <= recorded_cutoff(due)


def judge_as_of(due: str, cohorts: dict) -> dict:
    """判定時点 due で凍結する判定。cohorts = {D: (成熟日=出口日の最大, 全終端事象の記録時刻の最大, [ret_pair])}。

    使う日コホートは cohort_available を満たすものだけ＝①最後の出口日 <= due ②その D の全終端事象
    （matured・unfilled・censored）が due 当日 23:59:59 JST までに台帳へ記録済み（Codex 実装レビュー #1・
    再レビュー #1・3回目 (a)）。
    判定日より後に手に入ったデータ・後から記録された行は混ぜない。記録時刻の無い行は使わない。
    結果は一度だけ台帳へ `judgment` 事象として凍結し、以後は再計算しない（ランナー側の責務）。
    """
    cut = recorded_cutoff(due)
    use = {d: c[2] for d, c in cohorts.items() if cohort_available(c, due)}
    day_values = {d: sum(v) / len(v) for d, v in use.items()}
    event_values = [x for v in use.values() for x in v]
    res = judge(day_values, event_values)
    res["due"] = due
    res["recorded_cutoff"] = cut.isoformat()
    return res
