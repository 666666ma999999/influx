#!/usr/bin/env python3
"""ペーパートレード観察リスト 日次スクリーニング（毎営業日の自動シグナル判定・記録）。

config/paper_watchlist.json の観察対象KPI（volshock_5x/shortcover_x_bear/pead_gap8_vol3）を
「前営業日(T)終値までのデータ」で判定し、新規シグナルを data/paper_trades/ledger.jsonl に
2段階記録する: (1)シグナル検出時=entry_price未定・status=pending_entry (2)T+1（+defer_entry猶予）
のAdjOが確定した時点でentry_priceを確定=status=open。実際の状態遷移は scripts/paper_eval.py に
委譲する（Canonical Module原則・本スクリプトは重複実装しない）。

シグナル判定ロジックは全て既存KPIスクリプトの生成関数をそのまま import 再利用する
（再実装しない。config/paper_watchlist.jsonの_comment参照）:
    - volshock_5x       -> kpi_volshock_signals.generate_volshock_signals()
    - shortcover_x_bear -> kpi_shortcover_signals.generate_shortcover_signals()
                           + measure_base_rate.build_regime_series() でbear日のみ抽出
    - pead_gap8_vol3    -> kpi_pead_signals.generate_pead_signals()
シグナルは §0確定の対象ユニバース（月次売買代金TOP500・window=21・measure_base_rate.build_universe
を再利用）に属するものだけをledgerに記録する（in-sample/holdoutの検証スコープと整合させるため。
TOP500外は観察対象から除外し件数のみ報告する）。

=== J-Quants当日データ可用性プローブ（2026-07-07実施・実データ） ===
既存キャッシュを調査した結果、2026-07-06分(bars/fins/shortsale)を「同日05:57 JST」「同日10:41 JST」
「同日17:44 JST」の3時点で取得試行した過去ログが残っており、いずれも count=0（未公表）だった
（jq_fetch.py の fetch_snapshot_for_date は既存ファイルの中身を見ずスキップするため、この空
レスポンスが恒久キャッシュとして固着していた＝本スクリプトの ensure_recent_data() が起動時に
空キャッシュを検知して強制再取得するのはこのバグの回避策）。翌営業日 2026-07-07 10:05 JST に
再取得したところ bars=4437件/fins=15件/shortsale=1115件と正常取得できた。
結論: 同日夜間（17時台）でもshortsale公表が間に合わない実績を確認したため、本スクリプトは
「前営業日(T)終値までのデータで判定し、想定エントリーはT+1寄付」という設計（=T+1寄付ルールと
整合する「翌朝実行」構成）を採用する。実行時刻は当面 平日07:30 JST とするが、07:30時点でT
（前日）のデータが確実に揃っているかは本プローブでは未検証（検証できたのは翌営業日10:05時点の
みで、9:00の市場寄り付き後）。エントリー価格自体はT+1寄付が確定した後の“次回実行”で埋める
2段階記録のため、当日朝の実行タイミングの正確性は本質的な問題にならない（=間に合わなくても
翌日以降のリトライで自然に追いつく設計。ensure_recent_data() のキャッチアップ機構を参照）。

Usage:
    python3 scripts/daily_screen.py                 # 通常実行（データ更新+シグナル判定+ledger追記+レポート生成）
    python3 scripts/daily_screen.py --dry-run        # 今あるキャッシュのみでシグナル判定→件数表示。データ取得・ledger書込みなし
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: DATA_ROOT / read_json_gz / now_jst / get_api_key / fetch_topix を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: generate_pead_signals を再利用)
import kpi_shortcover_signals  # noqa: E402  (Canonical Module: generate_shortcover_signals を再利用)
import kpi_volshock_signals  # noqa: E402  (Canonical Module: generate_volshock_signals を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・regime・universe構築を再利用)
import paper_eval  # noqa: E402  (Canonical Module: ledger I/O・状態更新・scoreboard集計を再利用)

PROJECT_ROOT = Path(__file__).parent.parent
WATCHLIST_PATH = PROJECT_ROOT / "config" / "paper_watchlist.json"
STATE_PATH = PROJECT_ROOT / "data" / "paper_trades" / "state.json"
TODAY_REPORT_PATH = PROJECT_ROOT / "output" / "paper_today.md"

UNIVERSE_WINDOW = 21  # scripts/measure_base_rate.py 既定・v0系KPI検証と同一
UNIVERSE_TOP_N = 500  # カタログ§0「月次売買代金TOP500」

PEAD_LOOKBACK_BDAYS = 2  # 反応日Gが対象日に来る開示は前営業日にも起こりうるための走査開始オフセット


# --- watchlist設定読み込み -----------------------------------------------------------


def load_watchlist() -> list[dict]:
    obj = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    return obj["watchlist"]


# --- データ鮮度確保（jq_fetch.py 呼び出し + 既知の空キャッシュ固着バグの回避） -----------------


def clear_empty_cache_files(kind: str, dates: list[str]) -> int:
    """指定日付群のうち「ファイルは存在するがdata配列が空」のキャッシュを削除し強制再取得対象にする。

    jq_fetch.fetch_snapshot_for_date はファイル存在のみでスキップ判定するため、市場閉場前後の
    際どいタイミングで空レスポンスがキャッシュされると恒久的に再取得されない
    （本ファイルdocstring「J-Quants当日データ可用性プローブ」参照・2026-07-07実データで確認）。
    fins は開示が無い日に正当に空となるため対象外（jq_fetch.py print_status と同じ扱い）。
    """
    if kind == "fins":
        return 0
    removed = 0
    kind_dir = jq_fetch.DATA_ROOT / kind
    for d in dates:
        path = kind_dir / f"{d}.json.gz"
        if not path.exists():
            continue
        try:
            obj = jq_fetch.read_json_gz(path)
        except (OSError, EOFError):
            path.unlink(missing_ok=True)
            removed += 1
            continue
        if not obj.get("data"):
            path.unlink()
            removed += 1
    return removed


def refresh_recent_data(start_bd: str, end_bd: str, all_bdays: list[str]) -> None:
    """[start_bd, end_bd] 区間の bars/fins/shortsale を jq_fetch.py 経由で最新化する（冪等）。"""
    target_days = [d for d in all_bdays if start_bd <= d <= end_bd]
    for kind in ("bars", "fins", "shortsale"):
        removed = clear_empty_cache_files(kind, target_days)
        if removed:
            print(f"[refresh] {kind}: 空キャッシュ{removed}件を削除し再取得対象にしました", file=sys.stderr)
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / "jq_fetch.py"),
             "--only", kind, "--start", start_bd, "--end", end_bd],
            check=True,
        )


def refresh_topix_if_stale(target_bd: str) -> None:
    """topix.json.gz は jq_fetch.fetch_topix() が「存在すれば全期間スキップ」する設計のため
    日次では自動更新されない（2026-07-07実データで確認: bars/fins/shortsaleは20260706まで
    取得済みだったのにtopixは20260703止まりだった）。target_bd時点のレジーム判定
    （TOPIX SMA200）に必要な分だけ、古ければ削除して jq_fetch.fetch_topix() を再実行する
    （jq_fetch.py 本体は変更せず、呼び出し側で鮮度を確保するだけの最小介入）。
    """
    path = jq_fetch.DATA_ROOT / "topix.json.gz"
    if path.exists():
        obj = jq_fetch.read_json_gz(path)
        dates = [r["Date"].replace("-", "") for r in obj.get("data", [])]
        if dates and max(dates) >= target_bd:
            return
        print(
            f"[refresh] topix.json.gz が古い(最新={max(dates) if dates else 'なし'} < 必要={target_bd})。再取得します",
            file=sys.stderr,
        )
        path.unlink()
    api_key = jq_fetch.get_api_key()
    run_id = uuid.uuid4().hex
    jq_fetch.fetch_topix(api_key, run_id)


# --- 走査対象レンジの決定（キャッチアップ込み） --------------------------------------------


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def compute_scan_range(state: dict, all_bdays: list[str]) -> tuple[Optional[str], Optional[str]]:
    """未スクリーニングの営業日範囲 [start, end] を返す（無ければ (None, None)）。

    T = 「今日より前」の最新営業日（本スクリプトの設計上、前営業日終値までしか使わない・
    docstring「J-Quants当日データ可用性プローブ」参照）。state.last_screened_date が無ければ
    初回実行としてT一日のみを対象にする（過去に遡った一括生成はしない）。
    """
    today_str = jq_fetch.now_jst().strftime("%Y%m%d")
    completed = [d for d in all_bdays if d < today_str]
    if not completed:
        return None, None
    t_max = completed[-1]
    last_screened = state.get("last_screened_date")
    if last_screened is None:
        return t_max, t_max
    candidates = [d for d in completed if d > last_screened]
    if not candidates:
        return None, None
    return candidates[0], t_max


def _bars_available(d: str) -> bool:
    path = jq_fetch.DATA_ROOT / "bars" / f"{d}.json.gz"
    if not path.exists():
        return False
    obj = jq_fetch.read_json_gz(path)
    return bool(obj.get("data"))


def _bars_shortsale_available(d: str) -> bool:
    if not _bars_available(d):
        return False
    path = jq_fetch.DATA_ROOT / "shortsale" / f"{d}.json.gz"
    if not path.exists():
        return False
    obj = jq_fetch.read_json_gz(path)
    return bool(obj.get("data"))


def pead_effective_end(end_bd: str, bday_index: dict[str, int], all_bdays: list[str]) -> str:
    """PEADは開示時刻>=15:00の反応日がend_bdの翌営業日に押し出されうる
    （kpi_pead_signals.reaction_day）。その翌営業日のbarsがまだ存在しない場合
    （=まだ発生していない営業日）、end_bd当日の開示分は今回の走査から除外し
    end_bdを1営業日前倒しする（該当開示は翌営業日のbarsが揃った次回実行で
    自然に拾われる。measure_base_rate.load_bars_dayのFATAL停止を避けるための
    呼び出し側ガード。kpi_pead_signals.py 自体は変更しない）。
    """
    idx = bday_index[end_bd]
    if idx + 1 < len(all_bdays) and not _bars_available(all_bdays[idx + 1]):
        return all_bdays[idx - 1] if idx > 0 else end_bd
    return end_bd


def latest_available_bday_overall(all_bdays: list[str], max_lookback: int = 40) -> Optional[str]:
    """カレンダー全体の末尾から遡り、bars/shortsaleが揃っている直近の営業日を返す（--dry-run用）。

    catch-up走査用の latest_fully_available_bday とは異なり「連続性」は要求しない
    （どこか1日でも最新の有効キャッシュが見つかればそれで良い・過去に遡って履歴全体を
    先頭から舐めると2016年より前の日付でbarsが存在せず即座にNoneになってしまうため分離した）。
    """
    today_str = jq_fetch.now_jst().strftime("%Y%m%d")
    past_or_today = [d for d in all_bdays if d <= today_str]
    for d in reversed(past_or_today[-max_lookback:]):
        if _bars_shortsale_available(d):
            return d
    return None


def latest_fully_available_bday(candidate_days: list[str]) -> Optional[str]:
    """candidate_days（昇順・通常は数日〜十数日程度の狭いキャッチアップ区間）のうち
    bars/shortsale が両方非空で揃っている最後の日を返す（fins は開示が無い日に正当に空に
    なりうるため判定対象に含めない）。未公表で連続性が途切れた時点で打ち切る（間の空白日を
    残したまま先の日だけ処理すると§0のPIT前提が壊れるため）。
    """
    result: Optional[str] = None
    for d in candidate_days:
        if not _bars_shortsale_available(d):
            break
        result = d
    return result


# --- ユニバース判定（TOP500・§0確定スコープとの整合） ----------------------------------------


class UniverseCache:
    """signal_dateが属する月(YYYY-MM)ごとの直近確定済み月末TOP500ユニバースをメモ化する。

    measure_base_rate.build_universe() をそのまま再利用する（Canonical Module）。判定規約は
    kpi_event_study._universe_membership と同じ「signal_monthより厳密に前の直近月末」。
    """

    def __init__(self, calendar_days: list[tuple[str, str]], bday_index: dict[str, int], all_bdays: list[str]):
        self._calendar_days = calendar_days
        self._bday_index = bday_index
        self._all_bdays = all_bdays
        self._cache: dict[str, Optional[set]] = {}

    def universe_for_month(self, month: str) -> Optional[set]:
        if month in self._cache:
            return self._cache[month]
        month_ends = measure_base_rate.month_ends_in_range(self._calendar_days, "2016-08", month)
        prior_ends = [d for d in month_ends if d[:6] < month.replace("-", "")]
        if not prior_ends:
            self._cache[month] = None
            return None
        t_date = prior_ends[-1]
        try:
            selected, _stats = measure_base_rate.build_universe(
                t_date, self._bday_index, self._all_bdays, UNIVERSE_WINDOW, UNIVERSE_TOP_N
            )
        except SystemExit as e:
            print(f"WARN: {month}のユニバース構築に失敗（{e}）。この月のシグナルはユニバース判定不能扱い", file=sys.stderr)
            self._cache[month] = None
            return None
        codes = {code for code, _ in selected}
        self._cache[month] = codes
        return codes

    def in_universe(self, code: str, signal_date: str) -> bool:
        month = f"{signal_date[:4]}-{signal_date[4:6]}"
        universe = self.universe_for_month(month)
        if universe is None:
            return False
        return code in universe


# --- KPI別シグナル生成ディスパッチ ------------------------------------------------------------


def generate_kpi_signals(
    entry: dict, start_bd: str, end_bd: str, regime_by_day: dict[str, str],
    bday_index: dict[str, int], all_bdays: list[str],
) -> pd.DataFrame:
    kpi_name = entry["kpi_name"]
    params = entry["params"]

    if kpi_name in ("volshock_5x", "volshock_x_above200"):
        # 第11周: volshock_x_above200 は kpi_volshock_signals.py 実装済みの filter_ma200
        # (dev200符号フィルタ) をそのまま再利用する (Canonical Module原則・判定ロジックの
        # 再実装はしない)。日次判定でも generate_volshock_signals 内部の SMA200 は
        # 「D自身を含む直近200営業日終値平均」= PIT安全（同関数のdocstring参照）。
        df, _diag = kpi_volshock_signals.generate_volshock_signals(
            start_bd, end_bd,
            vol_multiplier=params["vol_multiplier"],
            day_ret_min=params["day_ret_min"],
            day_ret_max=params["day_ret_max"],
            filter_ma200=params.get("filter_ma200"),
        )
        return df[["signal_date", "code"]] if not df.empty else df

    if kpi_name == "shortcover_x_bear":
        raw_df, _diag = kpi_shortcover_signals.generate_shortcover_signals(
            start_bd, end_bd,
            aggregate_threshold=params["aggregate_threshold"],
            exit_threshold=params["exit_threshold"],
        )
        if raw_df.empty:
            return raw_df[["signal_date", "code"]] if "signal_date" in raw_df.columns else raw_df
        filtered = raw_df[raw_df["signal_date"].map(regime_by_day).eq(entry["regime_filter"])]
        return filtered[["signal_date", "code"]].reset_index(drop=True)

    if kpi_name == "pead_gap8_vol3":
        idx_start = bday_index[start_bd]
        scan_start_idx = max(0, idx_start - PEAD_LOOKBACK_BDAYS)
        scan_start = all_bdays[scan_start_idx]
        # 開示走査の終端は「反応日(=開示日+1)のbarsが存在する範囲」に縮めておく（pead_effective_end）。
        # 出力側のsignal_date上限は縮めない元のend_bd（=既にbars確定済み）のままでよい。
        scan_end = pead_effective_end(end_bd, bday_index, all_bdays)
        df, _diag = kpi_pead_signals.generate_pead_signals(
            scan_start, scan_end,
            gap_threshold=params["gap_threshold"],
            vol_multiplier=params["vol_multiplier"],
            vol_window=params["vol_window"],
        )
        if df.empty:
            return df[["signal_date", "code"]] if "signal_date" in df.columns else df
        filtered = df[(df["signal_date"] >= start_bd) & (df["signal_date"] <= end_bd)]
        return filtered[["signal_date", "code"]].reset_index(drop=True)

    raise SystemExit(f"FATAL: 未知のkpi_name={kpi_name}（config/paper_watchlist.json確認）")


# --- ledgerへの新規シグナル追記 --------------------------------------------------------------


def append_new_signals(
    records: list[dict], entry: dict, signals_df: pd.DataFrame, bday_index: dict[str, int],
    all_bdays: list[str], run_id: str,
) -> int:
    kpi_name = entry["kpi_name"]
    existing_keys = paper_eval.ledger_keys(records)
    added = 0
    for row in signals_df.itertuples(index=False):
        key = (kpi_name, row.code, row.signal_date)
        if key in existing_keys:
            continue
        idx = bday_index[row.signal_date]
        if idx + 1 >= len(all_bdays):
            continue  # カレンダー終端（実運用では起こらない想定）
        planned_entry_date = all_bdays[idx + 1]
        records.append(
            {
                "run_id": run_id,
                "kpi_name": kpi_name,
                "code": row.code,
                "signal_date": row.signal_date,
                "detected_at": jq_fetch.now_jst().isoformat(),
                "status": "pending_entry",
                "planned_entry_date": planned_entry_date,
                "defer_entry": entry["defer_entry"],
                "max_defer_bdays": entry["max_defer_bdays"] if entry["defer_entry"] else 0,
                "entry_date": None,
                "entry_price": None,
                "defer_bdays_used": None,
                "exit_date": None,
                "exit_price": None,
                "exit_reason": None,
                "ret_gross": None,
                "ret_net": None,
                "mfe": None,
                "mae": None,
                "updated_at": jq_fetch.now_jst().isoformat(),
            }
        )
        existing_keys.add(key)
        added += 1
    return added


# --- レポート生成 --------------------------------------------------------------------------


def write_today_report(
    new_signal_records: list[dict], records: list[dict], scoreboard: dict[str, dict],
    scan_start: Optional[str], scan_end: Optional[str], out_of_universe_counts: dict[str, int],
) -> None:
    lines = [
        "# ペーパートレード 当日スクリーニングレポート（scripts/daily_screen.py 自動生成）",
        "",
        f"実行時刻: {jq_fetch.now_jst().isoformat()}",
        f"走査区間: {scan_start or '-'} 〜 {scan_end or '-'}（前営業日終値までのデータで判定・想定エントリーはT+1寄付）",
        "",
        "## 新規シグナル",
        "",
    ]
    if new_signal_records:
        lines.append("| KPI | 銘柄コード | signal_date | 想定エントリー日 |")
        lines.append("|---|---|---|---|")
        for r in new_signal_records:
            lines.append(f"| {r['kpi_name']} | {r['code']} | {r['signal_date']} | {r['planned_entry_date']} |")
    else:
        lines.append("（本走査区間で新規シグナルなし）")
    if out_of_universe_counts:
        lines.append("")
        lines.append(
            "TOP500ユニバース外のため除外した候補件数: "
            + " / ".join(f"{k}={v}" for k, v in sorted(out_of_universe_counts.items()))
        )

    lines += ["", "## 保有中ポジション（pending_entry / open）", ""]
    active = [r for r in records if r["status"] in ("pending_entry", "open")]
    if active:
        lines.append("| KPI | 銘柄コード | signal_date | status | entry_date | entry_price |")
        lines.append("|---|---|---|---|---|---|")
        for r in sorted(active, key=lambda r: (r["kpi_name"], r["signal_date"])):
            lines.append(
                f"| {r['kpi_name']} | {r['code']} | {r['signal_date']} | {r['status']} | "
                f"{r['entry_date'] or '-'} | {r['entry_price'] or '-'} |"
            )
    else:
        lines.append("（保有中ポジションなし）")

    lines += ["", "## 累計成績（確定済みのみ）", ""]
    lines.append("| KPI | 稼働中(pending/open) | entry_missing | 確定n | P(+20%) | EV(net) |")
    lines.append("|---|---|---|---|---|---|")
    for kpi in sorted(scoreboard):
        s = scoreboard[kpi]
        p20 = f"{s['p_reach20']:.1%}" if s["p_reach20"] is not None else "-"
        ev = f"{s['ev_net']:+.2%}" if s["ev_net"] is not None else "-"
        lines.append(
            f"| {kpi} | {s['n_pending_entry']}/{s['n_open']} | {s['n_entry_missing']} | "
            f"{s['n_closed']} | {p20} | {ev} |"
        )
    lines.append("")
    lines.append("詳細は `output/paper_scoreboard.md`（scripts/paper_eval.py）を参照。")

    TODAY_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    TODAY_REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- メイン処理 -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="ペーパートレード観察リスト 日次スクリーニング")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="データ取得・ledger書込みを行わず、今あるキャッシュのみでシグナル件数を表示する",
    )
    args = parser.parse_args()

    watchlist = [e for e in load_watchlist() if e["status"] in ("observation", "reference")]
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    if args.dry_run:
        end_bd = latest_available_bday_overall(all_bdays)
        if end_bd is None:
            print("FATAL: bars/shortsaleの有効キャッシュが1件も見つかりません", file=sys.stderr)
            return 1
        start_bd = end_bd
        print(f"[dry-run] 今あるキャッシュの最新営業日 {end_bd} のみで判定します（データ取得は行いません）")
    else:
        start_bd, end_bd = compute_scan_range(load_state(), all_bdays)
        if start_bd is None:
            print("走査すべき新規営業日がありません（既に最新まで処理済み）")
            return 0
        refresh_recent_data(start_bd, end_bd, all_bdays)
        refresh_topix_if_stale(end_bd)
        available_end = latest_fully_available_bday([d for d in all_bdays if start_bd <= d <= end_bd])
        if available_end is None:
            print(f"WARN: {start_bd}〜{end_bd} のデータがまだ公表されていません。次回実行で再試行します", file=sys.stderr)
            return 0
        end_bd = available_end
        print(f"走査区間: {start_bd} 〜 {end_bd}")

    topix_close = measure_base_rate.load_topix_series()
    regime_by_day = measure_base_rate.build_regime_series(topix_close)
    universe_cache = UniverseCache(calendar_days, bday_index, all_bdays)

    run_id = uuid.uuid4().hex
    records = paper_eval.read_ledger()
    new_signal_records: list[dict] = []
    out_of_universe_counts: dict[str, int] = {}

    for entry in watchlist:
        signals_df = generate_kpi_signals(entry, start_bd, end_bd, regime_by_day, bday_index, all_bdays)
        n_raw = len(signals_df)
        if not signals_df.empty:
            in_univ_mask = signals_df.apply(
                lambda r: universe_cache.in_universe(r["code"], r["signal_date"]), axis=1
            )
            out_count = int((~in_univ_mask).sum())
            if out_count:
                out_of_universe_counts[entry["kpi_name"]] = out_count
            signals_df = signals_df[in_univ_mask].reset_index(drop=True)
        print(f"[{entry['kpi_name']}] 候補{n_raw}件 -> TOP500内{len(signals_df)}件")

        if args.dry_run:
            continue

        before = len(records)
        added = append_new_signals(records, entry, signals_df, bday_index, all_bdays, run_id)
        new_signal_records.extend(records[before : before + added])

    if args.dry_run:
        print("[dry-run] ledger書込み・レポート生成は行いません")
        return 0

    records, fill_diag, open_diag = paper_eval.run_update(records)
    paper_eval.write_ledger_atomic(records)
    print(
        f"新規シグナル追加: {len(new_signal_records)}件 / entry確定: {fill_diag['filled']}件 / "
        f"ポジションクローズ: {open_diag['closed_stop_loss'] + open_diag['closed_time_exit'] + open_diag['closed_delisted']}件"
    )

    scoreboard = paper_eval.compute_scoreboard(records)
    paper_eval.write_scoreboard_md(scoreboard, paper_eval.SCOREBOARD_PATH)
    write_today_report(new_signal_records, records, scoreboard, start_bd, end_bd, out_of_universe_counts)
    print(f"レポート出力: {TODAY_REPORT_PATH}")

    save_state({"last_screened_date": end_bd, "last_run_at": jq_fetch.now_jst().isoformat(), "last_run_id": run_id})
    return 0


if __name__ == "__main__":
    sys.exit(main())
