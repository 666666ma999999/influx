#!/usr/bin/env python3
"""ペーパートレード台帳の評価器（エントリー確定・-8%損切りシム・20営業日クローズ・成績集計）。

`data/paper_trades/ledger.jsonl`（1トレード1行のJSONLスナップショット。新規シグナルは
scripts/daily_screen.py が追記し、本スクリプトが状態遷移を進める）を読み込み、以下を行う:

    1. pending_entry -> open: 想定エントリー日（+ KPI別 defer_entry 設定の範囲内）に
       AdjO が確定していれば entry_price を確定する。
    2. open -> closed: entry_date 以降を1営業日ずつ前進し、-8%損切り水準タッチ
       （AdjO<=閾値ならギャップダウン決済・AdjL<=閾値なら閾値そのものの指値決済）を
       最優先で判定、未タッチのまま20営業日後に達したら終値で決済する
       （上場廃止等で当日に価格が無ければ窓内最後のAdjCで決済）。
    3. 並走exit追跡（第12周・カタログ§7-A Exit設計ラウンド由来）: 上記2の-8%固定損切り/
       20bd満期（status/exit_date/exit_price/exit_reason/ret_gross/ret_net）を「正」として
       一切変更せず、同じエントリーに対しE1（シナリオ崩壊: 前日終値>=SMA200(前日)かつ
       当日終値<SMA200(当日)のクロスで翌営業日寄付手仕舞い）とnostop（損切りなし・20bd満期）
       を並走評価し、ret_e1/ret_nostop（および対応するexit_date_e1等）に記録する。
       どれが優れているかは前向きデータの蓄積で後から判定する方針のため、本スクリプトは
       3系統のいずれも「正解」として扱わない（team-lead方針）。

損切り水準・往復コスト・フォワード窓日数の「定義」は scripts/measure_base_rate.py の
STOP_LEVELS["stop8"]/ROUND_TRIP_COST/FORWARD_WINDOW_BD をそのまま参照する（Canonical Module
原則・数値を再定義しない）。ただし「日ごとに前進しながら判定する」計算アルゴリズム自体は
measure_base_rate.compute_forward_return_for_code() を再利用できない（あちらは20営業日分の
バーズが既に全て存在する前提のバックテスト専用関数で、未到来の営業日を待てない）。
これは kpi_event_study.compute_forward_return_deferred() が「frozen モジュールは変更しない」
という理由で意図的にロジックを複製しているのと同種の、明示的な例外である。

ledger.jsonl は「1トレード1行」の現在状態スナップショットとして扱う（新規シグナル追加は
末尾追記、既存行の状態遷移は全件読み込み→更新→アトミック全体書き戻し。件数が高々数百件の
運用規模のため全件書き戻しで十分）。

Usage:
    python3 scripts/paper_eval.py                 # ポジション更新 + scoreboard 再生成
    python3 scripts/paper_eval.py --scoreboard-only  # 状態更新をスキップしscoreboardのみ再生成
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import sys
from collections import deque
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst / read_json_gz / DATA_ROOT を再利用)
import kpi_exit_study  # noqa: E402  (Canonical Module: MA200_WINDOW/SCAN_BUFFER_BDAYS定数を再利用。
                        # バックテスト専用関数(全future既知が前提でFATAL停止)はライブ日次前進判定には
                        # 使えないため、E1(シナリオ崩壊)の判定ロジック自体は個別実装する)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・STOP_LEVELS等の定義を再利用)

PROJECT_ROOT = Path(__file__).parent.parent
PAPER_TRADES_DIR = PROJECT_ROOT / "data" / "paper_trades"
LEDGER_PATH = PAPER_TRADES_DIR / "ledger.jsonl"
SCOREBOARD_PATH = PROJECT_ROOT / "output" / "paper_scoreboard.md"

STOP_LEVEL_NAME = "stop8"  # カタログ§0-4「損切り-5〜-8%を前提」に整合。measure_base_rate.STOP_LEVELSキー


@functools.lru_cache(maxsize=600)
def load_bars_day_if_cached(date_str: str) -> Optional[dict]:
    """measure_base_rate.load_bars_day() は未取得ファイルをFATALで止める設計だが、本スクリプトは
    「まだ到来していない将来営業日」を日次で前進判定する必要があるため、存在しなければNoneを返す
    バリアントをここに用意する。measure_base_rate.py は§6の凍結モジュール（変更禁止）のため
    追加できず、kpi_event_study.compute_forward_return_deferred と同種の意図的な複製である。"""
    path = jq_fetch.DATA_ROOT / "bars" / f"{date_str}.json.gz"
    if not path.exists():
        return None
    obj = jq_fetch.read_json_gz(path)
    return {rec["Code"]: rec for rec in obj["data"]}


# --- ledger I/O ------------------------------------------------------------------


def read_ledger() -> list[dict]:
    """ledger.jsonl を全件読み込む（存在しなければ空リスト）。"""
    if not LEDGER_PATH.exists():
        return []
    records: list[dict] = []
    with open(LEDGER_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def write_ledger_atomic(records: list[dict]) -> None:
    """ledger.jsonl を全件アトミック書き戻し（同一ディレクトリの一時ファイル経由でos.replace）。"""
    PAPER_TRADES_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = LEDGER_PATH.with_name(LEDGER_PATH.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp_path, LEDGER_PATH)


def ledger_keys(records: list[dict]) -> set[tuple[str, str, str]]:
    """(kpi_name, code, signal_date) の重複検知キー集合を返す（daily_screen.py の冪等追記用）。"""
    return {(r["kpi_name"], r["code"], r["signal_date"]) for r in records}


# --- pending_entry -> open ---------------------------------------------------------


def fill_pending_entries(records: list[dict], bday_index: dict[str, int], all_bdays: list[str]) -> dict:
    """pending_entry 状態のレコードについて、エントリー価格が確定できるものを埋める。

    KPI別の defer_entry/max_defer_bdays 設定に従い、planned_entry_date から最大
    max_defer_bdays 営業日先まで AdjO の有無を探索する（defer_entry=false のKPIは
    max_defer_bdays=0 相当・T+1固定でAdjOが無ければ entry_missing 確定）。
    候補日のbarsキャッシュがまだ存在しない（未来の営業日でまだ発生していない）場合は
    判定を保留し pending_entry のまま次回実行を待つ。
    """
    diag = {"filled": 0, "still_pending": 0, "entry_missing": 0}
    for rec in records:
        if rec["status"] != "pending_entry":
            continue
        planned_idx = bday_index.get(rec["planned_entry_date"])
        if planned_idx is None:
            diag["still_pending"] += 1
            continue

        max_defer = int(rec.get("max_defer_bdays", 0) or 0)
        resolved = False
        all_candidates_available = True
        for defer in range(0, max_defer + 1):
            idx = planned_idx + defer
            if idx >= len(all_bdays):
                all_candidates_available = False
                break
            candidate_day = all_bdays[idx]
            bars = load_bars_day_if_cached(candidate_day)
            if bars is None:
                all_candidates_available = False
                break  # その営業日のbarsがまだ取得できていない=判定保留
            rec_bar = bars.get(rec["code"])
            if rec_bar is not None and rec_bar.get("AdjO"):
                rec["entry_date"] = candidate_day
                rec["entry_price"] = rec_bar["AdjO"]
                rec["defer_bdays_used"] = defer
                rec["status"] = "open"
                rec["updated_at"] = jq_fetch.now_jst().isoformat()
                diag["filled"] += 1
                resolved = True
                break
        if resolved:
            continue
        if all_candidates_available:
            # 探索範囲内の全営業日のbarsが揃っているのにAdjOが一度も無かった
            # （S高固着・売買停止長期化等） -> エントリー不能確定
            rec["status"] = "entry_missing"
            rec["updated_at"] = jq_fetch.now_jst().isoformat()
            diag["entry_missing"] += 1
        else:
            diag["still_pending"] += 1
    return diag


# --- open -> closed（-8%損切り + 20営業日クローズの日次前進判定） ------------------------


def _stop_threshold(entry_price: float) -> float:
    stop_pct = measure_base_rate.STOP_LEVELS[STOP_LEVEL_NAME]
    return entry_price * (1 - stop_pct)


def update_open_positions(records: list[dict], bday_index: dict[str, int], all_bdays: list[str]) -> dict:
    """open 状態のレコードを entry_date 以降で日次前進判定し、-8%損切りタッチまたは
    FORWARD_WINDOW_BD(20)営業日後クローズで確定させる。まだどちらの条件も満たさない
    （かつ将来日のbarsがまだ存在しない）場合は open のまま mfe/mae のみ更新する。
    """
    diag = {"closed_stop_loss": 0, "closed_time_exit": 0, "closed_delisted": 0, "still_open": 0}
    forward_window = measure_base_rate.FORWARD_WINDOW_BD
    cost = measure_base_rate.ROUND_TRIP_COST

    for rec in records:
        if rec["status"] != "open":
            continue
        code = rec["code"]
        entry_idx = bday_index[rec["entry_date"]]
        entry_price = rec["entry_price"]
        threshold = _stop_threshold(entry_price)
        exit_target_idx = entry_idx + forward_window

        max_h = entry_price
        min_l = entry_price
        last_seen_adjc = entry_price
        last_seen_day = rec["entry_date"]
        stop_price: Optional[float] = None
        stop_day: Optional[str] = None
        reached_exit_target = False
        exit_target_day: Optional[str] = None

        idx = entry_idx
        while idx <= exit_target_idx:
            day = all_bdays[idx]
            bars = load_bars_day_if_cached(day)
            if bars is None:
                break  # その営業日がまだ来ていない（未来）-> ここでいったん打ち切り、openのまま次回へ
            bar = bars.get(code)
            if bar is not None:
                if bar.get("AdjH") is not None:
                    max_h = max(max_h, bar["AdjH"])
                if bar.get("AdjL") is not None:
                    min_l = min(min_l, bar["AdjL"])
                if bar.get("AdjC") is not None:
                    last_seen_adjc = bar["AdjC"]
                    last_seen_day = day
                ao = bar.get("AdjO")
                al = bar.get("AdjL")
                if ao is not None and ao <= threshold:
                    stop_price = ao
                    stop_day = day
                    break
                if al is not None and al <= threshold:
                    stop_price = threshold
                    stop_day = day
                    break
            if idx == exit_target_idx:
                reached_exit_target = True
                exit_target_day = day
                break
            idx += 1

        rec["mfe"] = max_h / entry_price - 1
        rec["mae"] = min_l / entry_price - 1

        if stop_price is not None:
            ret_gross = stop_price / entry_price - 1
            rec["exit_date"] = stop_day
            rec["exit_price"] = stop_price
            rec["exit_reason"] = "stop_loss"
            rec["ret_gross"] = ret_gross
            rec["ret_net"] = ret_gross - cost
            rec["ret_stop8"] = rec["ret_net"]  # 第12周並走記録用エイリアス（既存ret_netの意味は不変）
            rec["status"] = "closed"
            rec["updated_at"] = jq_fetch.now_jst().isoformat()
            diag["closed_stop_loss"] += 1
            continue

        if reached_exit_target:
            exit_bars = load_bars_day_if_cached(exit_target_day)
            exit_bar = (exit_bars or {}).get(code)
            if exit_bar is not None and exit_bar.get("AdjC"):
                exit_price = exit_bar["AdjC"]
                exit_date = exit_target_day
                exit_reason = "time_exit"
            else:
                exit_price = last_seen_adjc
                exit_date = last_seen_day
                exit_reason = "delisted"
            ret_gross = exit_price / entry_price - 1
            rec["exit_date"] = exit_date
            rec["exit_price"] = exit_price
            rec["exit_reason"] = exit_reason
            rec["ret_gross"] = ret_gross
            rec["ret_net"] = ret_gross - cost
            rec["ret_stop8"] = rec["ret_net"]  # 第12周並走記録用エイリアス（既存ret_netの意味は不変）
            rec["status"] = "closed"
            rec["updated_at"] = jq_fetch.now_jst().isoformat()
            if exit_reason == "delisted":
                diag["closed_delisted"] += 1
            else:
                diag["closed_time_exit"] += 1
            continue

        rec["updated_at"] = jq_fetch.now_jst().isoformat()
        diag["still_open"] += 1

    return diag


# --- 並走exit評価（E1シナリオ崩壊・nostop。既存stop8/20bd満期は一切変更しない） ------------
#
# 第12周カタログ§7-A で確定した exit 設計ラウンドの知見を受け、team-lead指示によりE1
# （シナリオ崩壊: 保有中に前日終値>=SMA200(前日)かつ当日終値<SMA200(当日)で翌営業日寄付手仕舞い）
# をペーパートレードの前向き検証に並走配線する。既存の-8%固定損切り・20bd満期ルール（status/
# exit_date/exit_price/exit_reason/ret_gross/ret_net）は一切変更せず、これを「正」として引き続き
# scoreboard等の既存経路が参照する。ret_stop8/ret_e1/ret_nostop の3系統をレコードに並記し、
# どれが優れているかは前向きデータの蓄積で後から判定する（team-lead方針: 「どれか1つを正としない」）。


def _build_live_close_sma200(
    code: str, start_idx: int, end_idx: int, all_bdays: list[str]
) -> tuple[dict[str, float], dict[str, Optional[float]], bool]:
    """[start_idx, end_idx]の範囲でcodeの日次AdjCとSMA200(D自身を含む直近200回の有効AdjC平均。
    scripts/kpi_exit_study.py の MA200_WINDOW と同一規約)をライブ判定用に逐次構築する。

    まだキャッシュされていない営業日(未来)に到達したら打ち切る。measure_base_rate.load_bars_day
    は未取得ファイルをFATALで止める設計（全future dataが既知のバックテスト専用）のため、
    ライブの日次前進判定にはload_bars_day_if_cached(未取得ならNone)を使い個別実装する
    （本モジュール冒頭docstring記載の既存方針と同種の意図的な複製）。

    Returns:
        (close_by_day, sma_by_day, reached_end)。reached_endはend_idxまでの全営業日のbarsが
        揃っていたか(False=まだ未来日が残っており判定保留すべき)。
    """
    close_by_day: dict[str, float] = {}
    sma_by_day: dict[str, Optional[float]] = {}
    hist: deque = deque(maxlen=kpi_exit_study.MA200_WINDOW)
    idx = start_idx
    while idx <= end_idx:
        day = all_bdays[idx]
        bars = load_bars_day_if_cached(day)
        if bars is None:
            return close_by_day, sma_by_day, False
        rec_bar = bars.get(code)
        if rec_bar is not None and rec_bar.get("AdjC") is not None:
            close_by_day[day] = rec_bar["AdjC"]
            hist.append(rec_bar["AdjC"])
        sma_by_day[day] = (sum(hist) / len(hist)) if len(hist) == kpi_exit_study.MA200_WINDOW else None
        idx += 1
    return close_by_day, sma_by_day, True


def evaluate_parallel_exits(rec: dict, bday_index: dict[str, int], all_bdays: list[str]) -> dict:
    """open/closed(stop8)問わず、E1(シナリオ崩壊)とnostop(損切りなし・20bd満期)を並走評価する。

    既存のstatus/exit_date/exit_price/exit_reason/ret_gross/ret_net(=stop8相当)は一切変更しない。
    まだ判定できない(将来日未到達)場合は戻り値のdictに該当キーを含めない
    （呼び出し側は「キーが無ければ前回値のまま据え置く」）。ロジック自体は
    scripts/kpi_exit_study.py の C3(nostop)/E1(_walk_e1_scenario_break) と同一定義
    （カタログ§7-A事前登録仕様）だが、バックテスト専用(全future既知)の同関数はそのまま
    使えないため、ライブ判定用に個別実装している。

    Returns:
        {"nostop": {...}, "e1": {...}} の一部または全部(解決済みの分のみ)。各値は
        exit_date/exit_price/exit_reason/ret_gross/ret_net を持つdict。
    """
    code = rec["code"]
    entry_idx = bday_index[rec["entry_date"]]
    entry_price = rec["entry_price"]
    exit_target_idx = entry_idx + measure_base_rate.FORWARD_WINDOW_BD
    cost = measure_base_rate.ROUND_TRIP_COST

    warmup_start_idx = max(0, entry_idx - 1 - kpi_exit_study.SCAN_BUFFER_BDAYS)
    close_by_day, sma_by_day, reached_end = _build_live_close_sma200(
        code, warmup_start_idx, exit_target_idx, all_bdays
    )

    result: dict[str, dict] = {}

    # --- nostop（損切りなし・20bd満期。reached_endの時のみ確定） ---
    if reached_end:
        target_day = all_bdays[exit_target_idx]
        target_close = close_by_day.get(target_day)
        if target_close is not None:
            exit_date, exit_price, exit_reason = target_day, target_close, "time_exit"
        else:
            # 上場廃止等でtarget_dayに終値が無い -> 窓内最後の有効AdjCで決済
            window_closes = {
                d: c for d, c in close_by_day.items() if entry_idx <= bday_index[d] <= exit_target_idx
            }
            if window_closes:
                exit_date = max(window_closes, key=lambda d: bday_index[d])
                exit_price = window_closes[exit_date]
            else:
                exit_date, exit_price = rec["entry_date"], entry_price
            exit_reason = "delisted"
        ret_gross = exit_price / entry_price - 1
        result["nostop"] = {
            "exit_date": exit_date, "exit_price": exit_price, "exit_reason": exit_reason,
            "ret_gross": ret_gross, "ret_net": ret_gross - cost,
        }

    # --- E1（シナリオ崩壊: 前日終値>=SMA200(前日)かつ当日終値<SMA200(当日)で翌営業日寄付手仕舞い） ---
    prev_day = all_bdays[entry_idx - 1]
    prev_close, prev_sma = close_by_day.get(prev_day), sma_by_day.get(prev_day)
    idx = entry_idx
    while idx <= exit_target_idx:
        day = all_bdays[idx]
        if day not in sma_by_day:
            break  # まだ当日のbarsが来ていない(未来) -> e1は保留のまま
        cur_close, cur_sma = close_by_day.get(day), sma_by_day.get(day)
        crossed = (
            prev_close is not None and prev_sma is not None
            and cur_close is not None and cur_sma is not None
            and prev_close >= prev_sma and cur_close < cur_sma
        )
        if crossed:
            if idx == exit_target_idx:
                if "nostop" in result:
                    result["e1"] = {**result["nostop"], "exit_reason": "time_cross_on_last_day"}
                break
            for j in range(idx + 1, exit_target_idx + 1):
                day2 = all_bdays[j]
                bars2 = load_bars_day_if_cached(day2)
                if bars2 is None:
                    break  # 未来日 -> e1は保留のまま(for-elseへは進まない)
                bar2 = bars2.get(code)
                if bar2 is not None and bar2.get("AdjO") is not None:
                    exit_price = bar2["AdjO"]
                    ret_gross = exit_price / entry_price - 1
                    result["e1"] = {
                        "exit_date": day2, "exit_price": exit_price, "exit_reason": "cross_exit",
                        "ret_gross": ret_gross, "ret_net": ret_gross - cost,
                    }
                    break
            else:
                # 残り窓の全営業日のbarsが揃っているのに約定可能な寄付が一度も無かった
                # （売買停止長期化等）-> 時間切れexit(nostop相当)に合流
                if "nostop" in result:
                    result["e1"] = {**result["nostop"], "exit_reason": "time_cross_no_open"}
            break
        prev_close, prev_sma = cur_close, cur_sma
        idx += 1
    else:
        # クロスが一度も起きないまま窓の最後(exit_target_idx)まで到達 -> nostopと同じ終値
        if "nostop" in result:
            result["e1"] = {**result["nostop"], "exit_reason": "time"}

    return result


def update_parallel_exit_tracking(records: list[dict], bday_index: dict[str, int], all_bdays: list[str]) -> dict:
    """既存の canonical exit(stop8/20bd満期)を一切変更せず、E1とnostopを並走記録する
    （第12周・team-lead指示。カタログ§7-A E1のペーパートレード前向き確認）。

    open/closedいずれの状態でも、まだret_e1/ret_nostopの一方でも未解決なレコードを対象に
    評価し直す（stop8が先に閉じても、e1/nostopは20bd分の将来データが揃うまで解決しないため、
    closed後も引き続き評価を続ける必要がある）。両方解決済みのレコードは再評価をスキップする。
    """
    diag = {"resolved_e1": 0, "resolved_nostop": 0, "evaluated": 0}
    for rec in records:
        if rec["status"] not in ("open", "closed"):
            continue  # entry未確定(pending_entry/entry_missing)は評価対象外
        if rec.get("ret_e1") is not None and rec.get("ret_nostop") is not None:
            continue  # 既に両方解決済み(再評価不要)

        diag["evaluated"] += 1
        parallel = evaluate_parallel_exits(rec, bday_index, all_bdays)

        if "nostop" in parallel and rec.get("ret_nostop") is None:
            p = parallel["nostop"]
            rec["exit_date_nostop"] = p["exit_date"]
            rec["exit_price_nostop"] = p["exit_price"]
            rec["exit_reason_nostop"] = p["exit_reason"]
            rec["ret_nostop"] = p["ret_net"]
            diag["resolved_nostop"] += 1
        if "e1" in parallel and rec.get("ret_e1") is None:
            p = parallel["e1"]
            rec["exit_date_e1"] = p["exit_date"]
            rec["exit_price_e1"] = p["exit_price"]
            rec["exit_reason_e1"] = p["exit_reason"]
            rec["ret_e1"] = p["ret_net"]
            diag["resolved_e1"] += 1
        rec["updated_at"] = jq_fetch.now_jst().isoformat()
    return diag


# --- 成績集計 -----------------------------------------------------------------


def compute_scoreboard(records: list[dict]) -> dict[str, dict]:
    """kpi_name別の累計成績（closed分のn/P(+20%)/EV・稼働中件数）を返す。"""
    by_kpi: dict[str, dict] = {}
    for rec in records:
        kpi = rec["kpi_name"]
        s = by_kpi.setdefault(
            kpi,
            {
                "n_closed": 0,
                "n_reach20": 0,
                "sum_ret_net": 0.0,
                "n_pending_entry": 0,
                "n_open": 0,
                "n_entry_missing": 0,
                "n_stop_loss": 0,
                "n_time_exit": 0,
                # primary endpoint（§7-J凍結: 判定対象は nostop の ret・stop8は参考）。
                # ledger の ret_net は stop8 エイリアスのため別集計する（2026-07-30 第27R A10）。
                "n_nostop": 0,
                "n_reach20_nostop": 0,
                "sum_ret_nostop": 0.0,
            },
        )
        status = rec["status"]
        if status == "closed":
            s["n_closed"] += 1
            s["sum_ret_net"] += rec["ret_net"]
            if rec["ret_gross"] >= 0.20:
                s["n_reach20"] += 1
            if rec["exit_reason"] == "stop_loss":
                s["n_stop_loss"] += 1
            else:
                s["n_time_exit"] += 1
        elif status == "pending_entry":
            s["n_pending_entry"] += 1
        elif status == "open":
            s["n_open"] += 1
        elif status == "entry_missing":
            s["n_entry_missing"] += 1
        # primary(nostop) は status に依らず ret_nostop が確定した行だけを集計する
        # （stop8で早期closedでも nostop は20営業日満期まで並走追跡されるため）。
        rn = rec.get("ret_nostop")
        if isinstance(rn, (int, float)):
            s["n_nostop"] += 1
            s["sum_ret_nostop"] += rn
            if rn + measure_base_rate.ROUND_TRIP_COST >= 0.20:  # コスト後→grossへ戻して判定
                s["n_reach20_nostop"] += 1

    for s in by_kpi.values():
        s["p_reach20"] = (s["n_reach20"] / s["n_closed"]) if s["n_closed"] else None
        s["ev_net"] = (s["sum_ret_net"] / s["n_closed"]) if s["n_closed"] else None
        s["p_reach20_nostop"] = (s["n_reach20_nostop"] / s["n_nostop"]) if s["n_nostop"] else None
        s["ev_nostop"] = (s["sum_ret_nostop"] / s["n_nostop"]) if s["n_nostop"] else None
        # 早期決済バイアス（2026-07-29 追加）: 時間切れ決済が1件も無い段階では、
        # closed に入れるのは -8% 損切りに掛かった負けだけ（勝ちは20営業日まで open のまま）。
        # ＝ P(+20%)/EV は構造的に下振れし、読むと必ず「全KPIが大負け」に見える。
        s["early_exit_biased"] = bool(s["n_closed"]) and s["n_time_exit"] == 0
    return by_kpi


def write_scoreboard_md(scoreboard: dict[str, dict], path: Path) -> None:
    biased = sorted(k for k, s in scoreboard.items() if s.get("early_exit_biased"))
    lines = [
        "# ペーパートレード 累計成績（scripts/paper_eval.py 自動生成）",
        "",
        f"最終更新: {jq_fetch.now_jst().isoformat()}",
        "",
    ]
    if biased:
        lines += [
            "> ## ⚠️ この表の P(+20%)/EV はまだ読んではいけない（早期決済バイアス）",
            ">",
            f"> **時間切れ決済が0件のKPIが {len(biased)}本**あります"
            f"（{', '.join(biased[:6])}{' ほか' if len(biased) > 6 else ''}）。",
            "> この段階で `closed` に入るのは **-8%損切りに掛かった負けだけ**で、勝っている建玉は"
            "20営業日の満期まで `open` のまま残ります。",
            "> ＝ P(+20%) と EV は**構造的に下振れ**し、必ず「全KPIが大負け」に見えます。",
            "> **最初のまともな読み取りは、各KPIで時間切れ決済が出てから**（＝最古シグナルの"
            "エントリーから20営業日後）。それまでは『稼働中』件数と損切り件数だけを見ること。",
            "",
        ]
    lines += [
        "**判定対象＝primary exit(nostop・20営業日満期)**。stop8 は §7-J 凍結どおり参考の secondary。",
        "",
        "| KPI | 稼働中(pending/open) | **確定n(nostop)** | **P(+20%)** | **EV(nostop)** | 参考:確定n(stop8) | 参考:EV(stop8) | 損切り | 時間切れ |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for kpi in sorted(scoreboard):
        s = scoreboard[kpi]
        active = f"{s['n_pending_entry']}/{s['n_open']}"
        mark = " ⚠️未成熟" if s.get("early_exit_biased") else ""
        p20n = f"{s['p_reach20_nostop']:.1%}" if s.get("p_reach20_nostop") is not None else "-"
        evn = f"{s['ev_nostop']:+.2%}" if s.get("ev_nostop") is not None else "-"
        ev8 = f"{s['ev_net']:+.2%}{mark}" if s["ev_net"] is not None else "-"
        lines.append(
            f"| {kpi} | {active} | {s.get('n_nostop', 0)} | {p20n} | {evn} "
            f"| {s['n_closed']} | {ev8} | {s['n_stop_loss']} | {s['n_time_exit']} |"
        )
    lines.append("")
    lines.append(
        "注記: 確定n・P(+20%)・EVは `status=closed` のみで集計（in-sample/holdoutの評価基準と同じ"
        "ret_gross>=+20%判定・ROUND_TRIP_COST控除後のret_net平均）。稼働中件数はまだ結果が出ていない"
        "ため参考情報。config/paper_watchlist.json の in_sample/holdout_observation と比較する際は"
        "サンプルサイズが極小である前向き記録の性質上、統計的な合否判定にはまだ使えない点に注意。"
        " **⚠️未成熟** が付いた行は時間切れ決済0件＝上記の早期決済バイアスが掛かっている（負けだけが"
        "先に確定する期間）。"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- メイン処理 -----------------------------------------------------------------


def run_update(records: list[dict]) -> tuple[list[dict], dict, dict, dict]:
    """pending_entry埋め + open進行判定 + 並走exit(nostop/e1)追跡を1パス実行する共通処理
    （daily_screen.pyからも呼ばれる）。"""
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    fill_diag = fill_pending_entries(records, bday_index, all_bdays)
    open_diag = update_open_positions(records, bday_index, all_bdays)
    parallel_diag = update_parallel_exit_tracking(records, bday_index, all_bdays)
    return records, fill_diag, open_diag, parallel_diag


def main() -> int:
    parser = argparse.ArgumentParser(description="ペーパートレード台帳の評価器（エントリー確定・損切りシム・成績集計）")
    parser.add_argument(
        "--scoreboard-only", action="store_true",
        help="状態更新(fill_pending_entries/update_open_positions)をスキップしscoreboard再生成のみ行う",
    )
    args = parser.parse_args()

    records = read_ledger()
    if not records:
        print("WARN: ledger.jsonl が空か未作成です（先に scripts/daily_screen.py を実行してください）", file=sys.stderr)

    if not args.scoreboard_only and records:
        records, fill_diag, open_diag, parallel_diag = run_update(records)
        write_ledger_atomic(records)
        print(
            f"pending_entry更新: filled={fill_diag['filled']} entry_missing={fill_diag['entry_missing']} "
            f"still_pending={fill_diag['still_pending']}"
        )
        print(
            f"openポジション更新: closed_stop_loss={open_diag['closed_stop_loss']} "
            f"closed_time_exit={open_diag['closed_time_exit']} closed_delisted={open_diag['closed_delisted']} "
            f"still_open={open_diag['still_open']}"
        )
        print(
            f"並走exit追跡(E1シナリオ崩壊/nostop・第12周): evaluated={parallel_diag['evaluated']} "
            f"resolved_nostop={parallel_diag['resolved_nostop']} resolved_e1={parallel_diag['resolved_e1']}"
        )

    scoreboard = compute_scoreboard(records)
    write_scoreboard_md(scoreboard, SCOREBOARD_PATH)
    print(f"scoreboard出力: {SCOREBOARD_PATH}")
    for kpi in sorted(scoreboard):
        s = scoreboard[kpi]
        p20 = f"{s['p_reach20']:.1%}" if s["p_reach20"] is not None else "-"
        ev = f"{s['ev_net']:+.2%}" if s["ev_net"] is not None else "-"
        print(
            f"  [{kpi}] 稼働中={s['n_pending_entry']}/{s['n_open']} entry_missing={s['n_entry_missing']} "
            f"確定n={s['n_closed']} P(+20%)={p20} EV={ev}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
