#!/usr/bin/env python3
"""インフルエンサー勝率アルゴ P1: J-Quants bars による採点 + 週次スコアボード生成。

docs/influencer-winrate-spec.md §5 F3/F4 を実装する。signals.jsonl の全シグナルを
J-Quants ローカルキャッシュ(bars)で評価する。価格ソースを旧 yfinance(PriceFetcher)
から bars に差し替える以外は、既存 evaluations.jsonl と互換のスキーマ
(horizon_5bd/horizon_20bd/is_win)を保つ（signal_date 翌営業日寄付エントリー →
5bd/20bd終値で判定 + +20%到達フラグ）。

営業日カレンダー・bars読み込みの低レベル部分は scripts/measure_base_rate.py の
Canonical な関数（load_calendar_days/all_business_days/load_bars_day）をそのまま
再利用する。ただし measure_base_rate.compute_forward_return_for_code は「エントリー
〜20営業日後の全日程のbarsファイルが揃っている」ことを前提に FATAL で停止する契約
（scripts/kpi_event_study.py 等の in-sample 検証専用ハーネス向け）のため、prospective
（今週投稿・ホライズン未到来）シグナルを評価するとクラッシュしてしまい、本スクリプトの
要件「ホライズン未到来は pending」と両立しない。そのためエントリー/5bd/20bd の算出は
本ファイル内に独立実装する（低レベル関数は再利用しつつ、上位の集計関数は意図的に
再利用しない。kpi_event_study.py の compute_forward_return_deferred と同種の判断）。

出力は output/research/evaluations_bars.jsonl（旧 evaluations.jsonl とは別ファイル）。
理由:
  (1) 週次再実行のたびに pending だったホライズンを埋め直す必要があるが、
      ResearchStore.add_evaluation は signal_id で重複排除するのみで更新機能がない。
      本ファイルは signals.jsonl から毎回全件再計算する「導出データ」として扱い、
      実行ごとに全体を書き直す（追記ではなく上書き。決定論的なので安全に再生成できる）
  (2) 受入テストで旧yfinance版evaluations.jsonlとの突合が必要なため、物理的に
      別ファイルに分離しないと比較ができない

Usage:
    python3 scripts/winrate_score.py
    python3 scripts/winrate_score.py --limit 50   # 動作確認用に評価件数を絞る
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import jq_fetch  # noqa: E402  (Canonical Module: now_jst / DATA_ROOT)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読み込み)
from extensions.tier1_collection.grok_discoverer.research_store import ResearchStore  # noqa: E402

RESEARCH_DIR = "output/research"
FROZEN_DATE = "2026-07-05"  # docs/influencer-winrate-spec.md §6 PIT規律: この日以降がprospective
HORIZON_5BD = 5
HORIZON_20BD = 20
REACH_THRESHOLD = 0.20  # カタログ§2-G「+20%到達」の関心指標
N_REFERENCE_MIN = 10  # n<10のアカウントは「参考」表示（spec §5 F4）
LIST_TWEET_TICKER_THRESHOLD = 5  # F4: 同一tweet_urlの抽出ticker数がこれ以上なら「リスト型」投稿
# CLI引数化しない（固定定数）。カタログ§6「評価プロトコル凍結・事前登録必須」の精神に倣い、
# 閾値を実行のたびに変えられる状態にすると事後的な閾値探索（データスヌーピング）の温床になる。
# 変更する場合は設計書と spec §F4 の該当箇所を明示的に改版してから行う。

# シグナルのticker(例 "7203.T" "285A.T")のうち日本株4桁(英数字混在可)+.T形式のみ
# J-Quants bars で評価できる（米国株/ETF/指数/暗号資産等はbars対象外）
TICKER_JP_RE = re.compile(r'^(\d[0-9A-Z]{3})\.T$')


def ticker_to_jquants_code(ticker: str) -> "str | None":
    """シグナルのticker(例 '7203.T')をJ-Quants bars の5桁Code(例 '72030')へ変換する。

    J-Quants は2024年の証券コード改革で4桁コードの末尾に'0'を付与した5桁形式に
    移行済み（scripts/kpi_activist_signals.py の normalize_seccode と同じ慣行だが、
    入力が数字専用ではなく英数字混在(例 '285A')の '.T' 付きticker文字列である点が
    異なるため、ここでは専用の軽量変換にとどめる。日本株以外(米国株/ETF/指数/暗号資産)
    は bars で評価できないため None を返す）。
    """
    m = TICKER_JP_RE.match(ticker.strip().upper())
    if not m:
        return None
    return m.group(1) + "0"


def _bars_exist(date_str: str) -> bool:
    """指定営業日(YYYYMMDD形式)のbarsキャッシュファイルが既に存在するか（未到来日はFalse=pending扱い）。"""
    path = jq_fetch.DATA_ROOT / "bars" / f"{date_str}.json.gz"
    return path.exists()


def _to_compact_date(iso_date: str) -> str:
    """"YYYY-MM-DD" を all_bdays / bars ファイル名と同じ "YYYYMMDD" 形式へ変換する。

    signals.jsonl の signal_date は posted_at[:10] 由来の "YYYY-MM-DD"（ハイフン区切り）だが、
    measure_base_rate.all_business_days() / bars キャッシュファイル名はハイフン無しの
    "YYYYMMDD" 形式（例: "20260701.json.gz"）。この変換を怠って直接 bisect すると、
    文字列の辞書式比較がハイフンの位置でずれて全く無関係な日付にマッチしてしまう
    （実装時に発覚した既知のバグ、必ずこの関数を経由すること）。
    """
    return iso_date.replace("-", "")


def _to_iso_date(compact_date: str) -> str:
    """"YYYYMMDD" を "YYYY-MM-DD" 形式へ変換する（evaluations.jsonl 互換の表示形式）。"""
    return f"{compact_date[:4]}-{compact_date[4:6]}-{compact_date[6:8]}"


def evaluate_signal(ticker: str, direction: str, signal_date: str, all_bdays: list) -> tuple:
    """1シグナルを T+1 寄付エントリーで評価する（5bd/20bd終値 + 20%到達フラグ）。

    signal_date が非営業日（週末等）の場合、signal_date 以降で最初に到来する営業日を
    実質的な T として扱い、その翌営業日をエントリー日とする（投稿タイミングは
    曜日を問わないため）。

    Returns:
        (evaluation_body, None): 評価成功（ホライズンが未到来の場合は該当フィールドが
            pending=Noneのまま返る。entry自体が未到来の場合はhorizon類が全てpending）。
        (None, skip_reason): 評価不能（"unresolvable_ticker" / "signal_date_out_of_calendar_range"
            / "entry_missing"）。
    """
    code = ticker_to_jquants_code(ticker)
    if code is None:
        return None, "unresolvable_ticker"

    t_idx = bisect.bisect_left(all_bdays, _to_compact_date(signal_date))
    if t_idx >= len(all_bdays):
        return None, "signal_date_out_of_calendar_range"

    entry_idx = t_idx + 1
    if entry_idx >= len(all_bdays):
        return None, "signal_date_out_of_calendar_range"
    entry_day = all_bdays[entry_idx]  # YYYYMMDD形式（bars参照・all_bdaysインデックス用）

    pending_body = {
        "entry_date": _to_iso_date(entry_day),
        "entry_price": None,
        "horizon_5bd": {"target_date": None, "price": None, "return_pct": None, "is_win": None},
        "horizon_20bd": {"target_date": None, "price": None, "return_pct": None, "is_win": None},
        "reach_20pct": None,
    }

    if not _bars_exist(entry_day):
        return pending_body, None  # エントリー日がまだ到来していない

    entry_rec = measure_base_rate.load_bars_day(entry_day).get(code)
    if entry_rec is None or not entry_rec.get("AdjO"):
        return None, "entry_missing"  # S高/売買停止等で約定不能（P1では繰り延べ未対応）
    entry_price = entry_rec["AdjO"]

    window_end_idx = entry_idx + HORIZON_20BD
    max_h = entry_price
    min_l = entry_price
    horizon_close = {}  # n_bd -> AdjC

    for offset in range(0, window_end_idx - entry_idx + 1):
        day = all_bdays[entry_idx + offset]
        if not _bars_exist(day):
            break  # これ以降は未到来（pending）
        rec = measure_base_rate.load_bars_day(day).get(code)
        if rec is None:
            continue  # 売買停止等の欠損日（P1では単純にスキップ）
        if rec.get("AdjH") is not None:
            max_h = max(max_h, rec["AdjH"])
        if rec.get("AdjL") is not None:
            min_l = min(min_l, rec["AdjL"])
        if offset in (HORIZON_5BD, HORIZON_20BD) and rec.get("AdjC") is not None:
            horizon_close[offset] = rec["AdjC"]

    def _build_horizon(n_bd: int) -> dict:
        target_idx = entry_idx + n_bd
        if target_idx >= len(all_bdays):
            return {"target_date": None, "price": None, "return_pct": None, "is_win": None}
        target_day_iso = _to_iso_date(all_bdays[target_idx])
        price = horizon_close.get(n_bd)
        if price is None:
            return {"target_date": target_day_iso, "price": None, "return_pct": None, "is_win": None}
        return_pct = round((price / entry_price - 1) * 100, 2)
        is_win = (return_pct > 0) if direction == "LONG" else (return_pct < 0)
        return {"target_date": target_day_iso, "price": price, "return_pct": return_pct, "is_win": is_win}

    horizon_5bd = _build_horizon(HORIZON_5BD)
    horizon_20bd = _build_horizon(HORIZON_20BD)

    # 方向別の+20%到達判定（LONG=高値が+20%到達、SHORT=安値が-20%到達=空売り益+20%相当）
    if direction == "LONG":
        reached = (max_h / entry_price - 1) >= REACH_THRESHOLD
    else:
        reached = (1 - min_l / entry_price) >= REACH_THRESHOLD

    window_fully_elapsed = _bars_exist(all_bdays[window_end_idx]) if window_end_idx < len(all_bdays) else True
    if reached:
        reach_20pct = True
    elif window_fully_elapsed:
        reach_20pct = False
    else:
        reach_20pct = None  # まだ到達していないが、ウィンドウが未確定のため判定保留

    return {
        "entry_date": _to_iso_date(entry_day),
        "entry_price": entry_price,
        "horizon_5bd": horizon_5bd,
        "horizon_20bd": horizon_20bd,
        "reach_20pct": reach_20pct,
    }, None


def run_scoring(signals: list, all_bdays: list) -> tuple:
    """全シグナルを評価し、(evaluations, diag) を返す。"""
    evaluations = []
    diag = {"total_signals": len(signals), "evaluated": 0, "skipped": {}}

    for sig in signals:
        direction = sig.get("direction", "")
        posted_at = sig.get("posted_at", "")
        signal_date = posted_at[:10] if posted_at else ""
        signal_id = sig.get("signal_id")

        if direction not in ("LONG", "SHORT") or not signal_date or not signal_id:
            diag["skipped"]["invalid_signal_fields"] = diag["skipped"].get("invalid_signal_fields", 0) + 1
            continue

        result, skip_reason = evaluate_signal(sig.get("ticker", ""), direction, signal_date, all_bdays)
        if result is None:
            diag["skipped"][skip_reason] = diag["skipped"].get(skip_reason, 0) + 1
            continue

        evaluations.append({
            "signal_id": signal_id,
            "username": sig.get("username", ""),
            "display_name": sig.get("display_name", ""),
            "ticker": sig.get("ticker", ""),
            "direction": direction,
            "signal_date": signal_date,
            "price_at_signal": result["entry_price"],
            "entry_date": result["entry_date"],
            "evaluated_at": jq_fetch.now_jst().isoformat(),
            "horizon_5bd": result["horizon_5bd"],
            "horizon_20bd": result["horizon_20bd"],
            "reach_20pct": result["reach_20pct"],
            "price_source": "jquants_bars",
        })
        diag["evaluated"] += 1

    return evaluations, diag


# --- スコアボード生成 -----------------------------------------------------------


def compute_tickers_per_tweet(signals: list) -> dict:
    """tweet_url単位で抽出されたticker(=signal)件数を数える（リスト型判定の入力）。

    signals.jsonl のみから計算する構造的指標（本文のtweet_url・件数のみに依存し、
    価格・評価結果を一切参照しない）。ticker抽出結果自体は投稿時点で確定しているため
    look-ahead は発生しない。

    判定は「抽出済み signal 数」ベースであり「ツイート本文の銘柄数」そのものではない
    （抽出漏れがあれば境界がブレうる。spec §F4 参照）。

    引数は必ず --limit 適用前の全 signals を渡すこと。--limit で絞った後の signals を
    渡すと、本来リスト型だったツイートの一部シグナルだけが集計対象に残り、
    tickers_per_tweet が過小算出されて非リスト型に誤分類されるため。

    tweet_url が空/欠落のシグナルは同一の空文字キーへ集約しない（1ツイート内の
    複数シグナルという前提が崩れるため、グルーピング対象外として個別に扱う）。
    """
    counts: dict = {}
    for sig in signals:
        url = sig.get("tweet_url") or f"__no_url__:{id(sig)}"
        counts[url] = counts.get(url, 0) + 1
    return counts


def is_list_tweet(tweet_url: str, tickers_per_tweet: dict) -> bool:
    """1ツイートからの抽出ticker数がLIST_TWEET_TICKER_THRESHOLD以上の「リスト型」投稿か判定する（spec §F4）。

    tweet_url が空文字の場合、compute_tickers_per_tweet 側で個別キーに退避済みのため
    ここでの空文字ルックアップは常に0件(=リスト型でない)になる（グルーピング対象外）。
    """
    return tickers_per_tweet.get(tweet_url, 0) >= LIST_TWEET_TICKER_THRESHOLD


def _is_prospective(posted_at: str) -> bool:
    return bool(posted_at) and posted_at[:10] >= FROZEN_DATE


def _fmt_pct(x) -> str:
    return f"{x:.1f}%" if x is not None else "-"


def _fmt_ret(x) -> str:
    return f"{x:+.2f}%" if x is not None else "-"


def group_by_username(signals: list, evaluations: list, tickers_per_tweet: dict) -> dict:
    """シグナルを評価結果と突合し、アカウント別・prospective/遡及別・リスト型/非リスト型別に振り分ける。"""
    eval_by_id = {e["signal_id"]: e for e in evaluations}
    by_username: dict = {}
    for sig in signals:
        username = sig.get("username", "unknown")
        entry = by_username.setdefault(username, {
            "prospective": [], "retroactive": [],
            "prospective_list": [], "retroactive_list": [],
        })
        joined = dict(sig)
        joined["evaluation"] = eval_by_id.get(sig.get("signal_id"))
        time_bucket = "prospective" if _is_prospective(sig.get("posted_at", "")) else "retroactive"
        bucket = f"{time_bucket}_list" if is_list_tweet(sig.get("tweet_url", ""), tickers_per_tweet) else time_bucket
        entry[bucket].append(joined)
    return by_username


def _account_stats(records: list) -> dict:
    n = len(records)
    ev_5bd = [r for r in records if r["evaluation"] and r["evaluation"]["horizon_5bd"]["is_win"] is not None]
    ev_20bd = [r for r in records if r["evaluation"] and r["evaluation"]["horizon_20bd"]["is_win"] is not None]
    ev_reach = [r for r in records if r["evaluation"] and r["evaluation"]["reach_20pct"] is not None]

    win_5bd = sum(1 for r in ev_5bd if r["evaluation"]["horizon_5bd"]["is_win"])
    win_20bd = sum(1 for r in ev_20bd if r["evaluation"]["horizon_20bd"]["is_win"])
    reach_hits = sum(1 for r in ev_reach if r["evaluation"]["reach_20pct"])

    returns_20bd = [
        r["evaluation"]["horizon_20bd"]["return_pct"] for r in ev_20bd
        if r["evaluation"]["horizon_20bd"]["return_pct"] is not None
    ]
    avg_return = sum(returns_20bd) / len(returns_20bd) if returns_20bd else None

    latest = max(records, key=lambda r: r.get("posted_at", "")) if records else None
    latest_label = f"{latest['posted_at'][:10]} {latest['ticker']}({latest['direction']})" if latest else "-"

    return {
        "n": n,
        "win_rate_5bd": (win_5bd / len(ev_5bd) * 100) if ev_5bd else None,
        "n_5bd": len(ev_5bd),
        "win_rate_20bd": (win_20bd / len(ev_20bd) * 100) if ev_20bd else None,
        "n_20bd": len(ev_20bd),
        "reach_20pct_rate": (reach_hits / len(ev_reach) * 100) if ev_reach else None,
        "n_reach": len(ev_reach),
        "avg_return_20bd": avg_return,
        "latest_signal": latest_label,
    }


def _render_table(rows: list) -> list:
    lines = ["| アカウント | シグナル数 | 5bd勝率 | 20bd勝率 | +20%到達率 | 平均リターン(20bd) | 直近シグナル |",
             "|---|---|---|---|---|---|---|"]
    if not rows:
        lines.append("| (シグナルなし) | 0 | - | - | - | - | - |")
        return lines
    for username, stats in rows:
        ref_marker = "（参考）" if stats["n"] < N_REFERENCE_MIN else ""
        lines.append(
            f"| @{username}{ref_marker} | {stats['n']} | "
            f"{_fmt_pct(stats['win_rate_5bd'])}({stats['n_5bd']}) | "
            f"{_fmt_pct(stats['win_rate_20bd'])}({stats['n_20bd']}) | "
            f"{_fmt_pct(stats['reach_20pct_rate'])}({stats['n_reach']}) | "
            f"{_fmt_ret(stats['avg_return_20bd'])} | {stats['latest_signal']} |"
        )
    return lines


def render_scoreboard_md(by_username: dict, generated_at: str) -> str:
    def rows_for(bucket_key: str) -> list:
        rows = []
        for username, buckets in by_username.items():
            if buckets[bucket_key]:
                rows.append((username, _account_stats(buckets[bucket_key])))
        rows.sort(key=lambda x: (-(x[1]["win_rate_20bd"] if x[1]["win_rate_20bd"] is not None else -1), -x[1]["n"]))
        return rows

    prospective_rows = rows_for("prospective")
    retro_rows = rows_for("retroactive")
    prospective_list_rows = rows_for("prospective_list")
    retro_list_rows = rows_for("retroactive_list")

    n_main = sum(s["n"] for _, s in prospective_rows) + sum(s["n"] for _, s in retro_rows)
    n_list = sum(s["n"] for _, s in prospective_list_rows) + sum(s["n"] for _, s in retro_list_rows)

    list_tweet_urls = set()
    for _, buckets in by_username.items():
        for rec in buckets["prospective_list"] + buckets["retroactive_list"]:
            list_tweet_urls.add(rec.get("tweet_url", ""))
    n_list_tweets = len(list_tweet_urls)

    lines = [
        "# インフルエンサー週次スコアボード",
        "",
        f"生成日時: {generated_at}",
        "",
        f"PIT基準: {FROZEN_DATE}以降の投稿=prospective（前向き・本評価対象）。"
        "それ以前は参考枠（仮説出し専用。遡及収集バイアスの影響を受ける。カタログ§2-G準拠）。",
        f"n<{N_REFERENCE_MIN} のアカウントは「（参考）」表示（統計的信頼性が低いため）。",
        f"1ツイートからの抽出ticker数が{LIST_TWEET_TICKER_THRESHOLD}件以上の投稿は「リスト型」（株探ランキング転載・"
        "固定リスト等）と判定し、下記アカウント別的中率表から除外して別枠に表示する（spec §F4・サイレント除外なし）。"
        f"リスト型として分離: {n_list_tweets}tweets / {n_list}signals（評価対象=非リスト型: {n_main}件）。",
        "**注意**: 下記メイン表は非リスト型投稿のみで集計。リスト型多用アカウント（@kazzn_blog等）は"
        "残存する非リスト型シグナルの件数が少なく、本人の通常運用全体の代表サンプルではない可能性がある。",
        "",
        "## Prospective（前向き・本評価対象）",
        "",
    ]
    lines += _render_table(prospective_rows)
    lines += [
        "",
        "## 参考: 遡及シグナル（〜2026-07-05以前収集・仮説出し専用・後知恵バイアスあり）",
        "",
    ]
    lines += _render_table(retro_rows)
    lines += [
        "",
        "## リスト型投稿（的中率評価対象外・セクター横断スクリーニング情報の参考表示）",
        "",
        "以下は1ツイートに複数銘柄が同時掲載される投稿（株探ランキング転載・固定ウォッチリスト等）の集計。"
        "1ツイート内の複数ティッカーは独立した個別判断ではなく相関した一括情報のため、"
        "アカウントの個別的中率スキルの指標として解釈しない。",
        "",
        "### Prospective",
        "",
    ]
    lines += _render_table(prospective_list_rows)
    lines += ["", "### 遡及", ""]
    lines += _render_table(retro_list_rows)

    total_prospective = sum(s["n"] for _, s in prospective_rows)
    lines += ["", "## サマリ", ""]
    lines.append(f"- prospectiveシグナル累計（非リスト型のみ）: {total_prospective}件（アカウント{len(prospective_rows)}件）")
    if prospective_rows:
        top_username, top_stats = max(
            prospective_rows, key=lambda x: (x[1]["win_rate_20bd"] if x[1]["win_rate_20bd"] is not None else -1)
        )
        lines.append(f"- 勝率(20bd)首位（非リスト型）: @{top_username}（{_fmt_pct(top_stats['win_rate_20bd'])}, n={top_stats['n_20bd']}）")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="J-Quants bars による採点 + 週次スコアボード生成（インフルエンサー勝率アルゴ P1）")
    parser.add_argument("--research-dir", default=RESEARCH_DIR)
    parser.add_argument("--limit", type=int, default=None, help="動作確認用に評価対象シグナル数を絞る")
    parser.add_argument("--evaluations-filename", default="evaluations_bars.jsonl")
    parser.add_argument("--scoreboard-path", default=None, help="既定: <research-dir>/weekly_scoreboard.md")
    args = parser.parse_args()

    scoreboard_path = args.scoreboard_path or os.path.join(args.research_dir, "weekly_scoreboard.md")

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)

    store = ResearchStore(base_dir=args.research_dir)
    signals_all = store.load_signals()
    # リスト型判定(tickers_per_tweet)は必ず --limit 適用前の全件で確定させる。
    # limit後に数えるとリスト型の一部シグナルだけが対象に残り、非リスト型に誤分類されるため。
    tickers_per_tweet = compute_tickers_per_tweet(signals_all)
    signals = signals_all[: args.limit] if args.limit else signals_all
    if args.limit:
        print(f"注: リスト型判定は全件基準（--limit適用前の{len(signals_all)}件）で行っています。")

    evaluations, diag = run_scoring(signals, all_bdays)

    eval_path = os.path.join(args.research_dir, args.evaluations_filename)
    os.makedirs(os.path.dirname(eval_path) or ".", exist_ok=True)
    with open(eval_path, "w", encoding="utf-8") as f:
        for e in evaluations:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    print("=== J-Quants bars による採点 ===")
    print(f"対象シグナル数: {diag['total_signals']}")
    print(f"評価成功: {diag['evaluated']}")
    for reason, count in diag["skipped"].items():
        print(f"  スキップ({reason}): {count}")
    print(f"評価結果: {eval_path}")

    n_list_signals = sum(1 for s in signals if is_list_tweet(s.get("tweet_url", ""), tickers_per_tweet))
    print(f"リスト型投稿(tickers_per_tweet>={LIST_TWEET_TICKER_THRESHOLD})除外: {n_list_signals}件 / "
          f"評価対象(非リスト型): {diag['total_signals'] - n_list_signals}件")

    by_username = group_by_username(signals, evaluations, tickers_per_tweet)
    md = render_scoreboard_md(by_username, jq_fetch.now_jst().isoformat())
    os.makedirs(os.path.dirname(scoreboard_path) or ".", exist_ok=True)
    with open(scoreboard_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"スコアボード生成: {scoreboard_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
