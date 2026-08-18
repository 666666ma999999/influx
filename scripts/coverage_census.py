#!/usr/bin/env python3
"""実効カバレッジ census — 「入力件数」でなく重複除外集合で監視の広さを数える。

第4R敵対クロス（2026-08-17・tasks/xprice_reform_review.md §8）の一致点1・5と
オーナー裁定 P-08a「重複除外で数え直す」への対応。

数える対象は次の3条件を全て満たす **独立ドライバー**（＝値動きの源）の集合:
  1. 独立ドライバー   — 同じ商品を別レーンで観測していても 1 と数える（news_shock の
                       21系列は全て B2B と重複＝入力合計は面積にならない）
  2. 稼働取得経路     — 直近の取得が成功している（死んだ経路は網でない）
  3. 関門通過カード   — sign='+' かつ tier='confirmed' の受益カードを 1 枚以上持つ
                       （provisional は決算実読の待ち行列＝まだ銘柄を出せない）

集計は fail-loud: 入力の破損・未知の tier・alias の指す系列の不在は握りつぶさず
`warnings` に出す（「0件」と「入力が壊れている」を出力上で区別するため）。

使い方:
    python3 scripts/coverage_census.py            # 集計して output/coverage_census.md を更新
    python3 scripts/coverage_census.py --selftest # 固定データで集計器そのものを検証
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from price_universe_check import pass_through_cards  # noqa: E402  浸透カード判定の正本（§16v）

SOURCES_PATH = ROOT / "configs" / "price_universe_sources.json"
SHORTAGE_PATH = ROOT / "configs" / "x_shortage_map.json"
NEWS_PATH = ROOT / "configs" / "news_shock.json"
WEEKLY_LEDGER = ROOT / "data" / "price_watch" / "universe_weekly.jsonl"
X_WATCH_LOG = ROOT / "data" / "x_price_watch" / "watch_log.jsonl"
X_COLLECT_LEDGER = ROOT / "data" / "x_price_watch" / "ledger.jsonl"
NEWS_LEDGER = ROOT / "data" / "news_shock" / "news_log.jsonl"
OUT_PATH = ROOT / "output" / "coverage_census.md"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

KNOWN_TIERS = {"confirmed", "provisional", "rejected"}
# signs の定義は configs/x_shortage_map.json の "signs"（+=受益 / -=損失 / 0=中立）
KNOWN_SIGNS = {"+", "-", "0"}

# 同一ドライバーの別名。値は (統合先, 根拠). strict=同一商品の別呼称・別ソース。
DRIVER_ALIAS: Dict[str, Tuple[str, str]] = {
    "gold-int": ("gold", "strict: 同じ金地金の国際建て"),
    "gold-tanaka": ("gold", "strict: 同じ金地金の国内小売建て"),
    "wti": ("crude-oil", "strict: 原油の産地別建て"),
    "brent": ("crude-oil", "strict: 原油の産地別建て"),
    "scrap-steel": ("scrap", "strict: 鉄スクラップの市場別建て"),
    "scrap-tokyosteel": ("scrap", "strict: 鉄スクラップの東京製鐵建て"),
}

# X品薄 subject → 価格系列ドライバーの対応。loose=市場全体を単一指標に縮約している
# （Codex 指摘#4）ため、出力で loose 件数を明示し、監査できる状態にする。
X_SUBJECT_ALIAS: Dict[str, Tuple[str, str]] = {
    "copper": ("copper", "strict: 同一商品"),
    "gold": ("gold", "strict: 同一商品"),
    "lumber": ("lumber", "strict: 同一商品"),
    "genyu": ("crude-oil", "strict: 同一商品（消費者可視の原油）"),
    "naphtha-x": ("naphtha", "strict: 同一商品"),
    "nand-ssd": ("nand_spot", "strict: NAND スポット価格"),
    "dram": ("memory-asp-estat", "loose: DRAM品薄 vs メモリ公式ASP（DRAM単独の保証なし）"),
    "toreka": ("toreca-sar", "loose: トレカ市場全体 vs ポケカSAR中央値"),
    "used-car": ("uss-used-car", "loose: 中古車市場全体 vs USS単一オークション"),
    "kaiun-unchin": ("scfi", "loose: 海運運賃全般 vs コンテナ運賃（BDI等を取りこぼす）"),
}

# 稼働判定の窓（日）。B2B は週次ジョブなので 14 日、news_shock は日2回なので 3 日、
# X は日次だが判定に前後28日窓が要るため台帳の鮮度も 3 日で見る。
FRESH_DAYS_B2B = 14
FRESH_DAYS_NEWS = 3
FRESH_DAYS_X = 3
MUTE_THRESHOLD = 999.0  # 上昇アラートを止めている印（§16j の食品ミュート等）
# X品薄レーンの「波」判定パラメータ。正本は price_watch_alert.wave_state（ここは見込み日の逆算用の写し）。
# 値を変える時は必ず両方を合わせる（片方だけ変えると census の見込み日が嘘になる）
WAVE_DAYS = 28
WAVE_BALANCE = 0.3


def resolve_driver(series_id: str) -> str:
    """系列 ID を独立ドライバーのキーへ正規化する。

    Args:
        series_id: configs 上の系列 ID。

    Returns:
        別名統合後のドライバーキー（対応が無ければ入力のまま）。
    """
    entry = DRIVER_ALIAS.get(series_id)
    return entry[0] if entry else series_id


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} の最上位が dict ではない")
    return data


def _iter_jsonl(path: Path, warnings: List[str]) -> Iterable[Dict[str, Any]]:
    """JSONL を読む。壊れた行・辞書でない行は警告に積んで飛ばす。"""
    if not path.exists():
        warnings.append(f"台帳が存在しない: {path.name}")
        return
    broken = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                broken += 1
                continue
            if isinstance(row, dict):
                yield row
            else:
                broken += 1
    if broken:
        warnings.append(f"{path.name}: 読めない行 {broken} 件（0件と破損を区別するため記載）")


def classify_cards(
    beneficiaries: Any, where: str, warnings: List[str]
) -> Tuple[Set[str], Set[str]]:
    """受益カードを〈関門通過 / 裏取り待ち〉のコード集合に分ける。

    未知の tier・sign は黙って捨てず警告に積む（設定不良がカバレッジ低下に偽装するのを防ぐ）。

    Args:
        beneficiaries: カード配列（型不正も受ける）。
        where: 警告に出す所在（系列 ID 等）。
        warnings: 警告の追記先。

    Returns:
        (関門通過コード集合, 裏取り待ちコード集合)
    """
    gate: Set[str] = set()
    pending: Set[str] = set()
    if beneficiaries in (None, []):
        return gate, pending
    if not isinstance(beneficiaries, list):
        warnings.append(f"{where}: beneficiaries が配列でない（型={type(beneficiaries).__name__}）")
        return gate, pending
    for card in beneficiaries:
        if not isinstance(card, dict):
            warnings.append(f"{where}: カードが辞書でない")
            continue
        # §16w: 海外カードは ticker/benchmark が揃って初めて「使えるカード」。
        # 欠けたものを有効カードに数えると census が実力を過大表示する（Codex 3審 C）
        market = card.get("market") or "JP"
        if market != "JP" and not (card.get("ticker") and card.get("benchmark")):
            warnings.append(f"{where}: {market} カードに ticker/benchmark が無い（無効扱い）")
            continue
        code, sign, tier = card.get("code"), card.get("sign"), card.get("tier")
        if tier is not None and tier not in KNOWN_TIERS:
            warnings.append(f"{where}: 未知の tier={tier!r}（コード{code}）")
            continue
        if sign is not None and sign not in KNOWN_SIGNS:
            warnings.append(f"{where}: 未知の sign={sign!r}（コード{code}）")
            continue
        if sign is None:
            warnings.append(f"__nosign__{where}")  # 集計時に件数へ畳む（sign 欠落=受益と主張できない）
            continue
        if not code or sign != "+":
            continue
        if tier == "confirmed":
            gate.add(str(code))
        elif tier == "provisional":
            pending.add(str(code))
    return gate, pending


def is_muted_on_upside(series: Dict[str, Any]) -> bool:
    """その系列の上昇アラートが無効化されているか（§16j 食品ミュート）。

    §16v の部分解除に合わせ、浸透カード（pass_through かつ confirmed）を持つ系列は
    ミュート扱いにしない。判定の正本は price_universe_check.pass_through_cards（二重実装を作らない）。
    """
    alert = series.get("alert") or {}
    if not isinstance(alert, dict):
        return False
    try:
        muted = float(alert.get("weekly_pct", 0)) >= MUTE_THRESHOLD
    except (TypeError, ValueError):
        return False
    if not muted:
        return False
    return not pass_through_cards(series)


def latest_series_status(
    rows: Iterable[Dict[str, Any]], warnings: List[str]
) -> Dict[str, Tuple[str, str]]:
    """系列ごとの最新行を (日付, status) で返す。

    同日に複数試行がある場合は run_at の大きい方（＝後の試行）を採用する。
    """
    latest: Dict[str, Tuple[str, str, str]] = {}  # id -> (date, run_at, status)
    bad_dates = 0
    for row in rows:
        sid, date = row.get("id"), row.get("date")
        if not sid or not isinstance(date, str) or not DATE_RE.match(date):
            if sid:
                bad_dates += 1
            continue
        run_at = str(row.get("run_at") or "")
        key = (date, run_at)
        prev = latest.get(str(sid))
        if prev is None or key > (prev[0], prev[1]):
            latest[str(sid)] = (date, run_at, str(row.get("status")))
    if bad_dates:
        warnings.append(f"universe_weekly: 日付が不正な行 {bad_dates} 件を除外")
    return {sid: (date, status) for sid, (date, _run, status) in latest.items()}


def live_series_ids(
    latest: Dict[str, Tuple[str, str]], now: datetime, fresh_days: int = FRESH_DAYS_B2B
) -> Set[str]:
    """直近 fresh_days 以内に status=ok で取得できた系列 ID を返す。未来日は除く。"""
    today = now.strftime("%Y-%m-%d")
    cutoff = (now - timedelta(days=fresh_days)).strftime("%Y-%m-%d")
    return {
        sid
        for sid, (date, status) in latest.items()
        if status == "ok" and cutoff <= date <= today
    }


def x_warmup_forecast(collect_rows: List[Dict[str, Any]], now: datetime,
                      win: int = WAVE_DAYS, bal: float = WAVE_BALANCE,
                      need_prev: int = 3, need_cur: int = 6,
                      horizon: int = 180) -> Tuple[Optional[str], str]:
    """X品薄レーンの「波」判定が成立し始める見込み日を返す（2026-08-19 追加）。

    判定式は price_watch_alert.wave_state と同じ:
    直近 win 日の標本が need_cur 以上、前 win 日の標本が need_prev 以上、かつ
    前窓の標本数が直近窓の bal 倍以上。**収集が今後も毎日続く前提**の「最短見込み日」を返す。

    数える単位も正本に合わせて **(query_id, query_sha) ごと**にする（2026-08-19 敵対レビュー NO-GO）。
    台帳全体の日付を1つの集合にすると、クエリAだけ成功した日とBだけ成功した日が交互でも
    「毎日収集できている」に見えてしまい、どのクエリも標本不足なのに開通日を早く出す。
    レーンとしての開通日は「**どれか1本のクエリが最初に条件を満たす日**」＝各クエリの最短値。

    なぜ要るか: 収集を始めた直後は前窓が空で必ず判定不能になる。これを「故障」と表示すると
    直す物が無いのに直しに行く（2026-08-19 に統括自身が『レーン復旧』を提案しかけた実害）。
    「壊れている」と「まだ貯まっていない」を出力で区別するための関数。

    Args:
        collect_rows: X収集台帳（data/x_price_watch/ledger.jsonl）の行。
        now: 現在時刻（UTC）。
        win/bal/need_prev/need_cur: wave_state と同じ判定パラメータ。
        horizon: 何日先まで探すか。

    Returns:
        (見込み開通日 "YYYY-MM-DD" or None, 説明文)
    """
    # clean の定義も正本 price_watch_alert.is_clean に合わせる（status=ok・censored でない・count あり）
    per_query: Dict[Tuple[str, str], Set[str]] = {}
    all_days: Set[str] = set()
    for r in collect_rows:
        if r.get("status") != "ok" or r.get("censored") or r.get("count") is None:
            continue
        day = str(r.get("date") or "")
        if not DATE_RE.match(day):
            continue
        key = (str(r.get("query_id") or ""), str(r.get("query_sha") or ""))
        per_query.setdefault(key, set()).add(day)
        all_days.add(day)
    if not all_days:
        return None, "収集台帳に clean な行が無い"
    last = max(all_days)
    stale_cut = (now - timedelta(days=FRESH_DAYS_X)).strftime("%Y-%m-%d")
    if last < stale_cut:
        return None, f"収集が止まっている（最終収集 {last}）"

    start = datetime.strptime(now.strftime("%Y-%m-%d"), "%Y-%m-%d")
    best: Optional[Tuple[str, str, int, int]] = None   # (日付, query_id, n_prev, n_cur)
    for (qid, _sha), days in per_query.items():
        # そのクエリが直近まで生きている場合だけ「今後も毎日集まる」と仮定する
        # （SHA が変わった旧クエリの履歴で開通日を前倒ししない）
        if max(days) < stale_cut:
            continue
        future = {(datetime.strptime(max(days), "%Y-%m-%d") + timedelta(days=i)).strftime("%Y-%m-%d")
                  for i in range(1, horizon + 1)}
        dayset = days | future
        for i in range(horizon):
            target = start + timedelta(days=i)
            if best and target.strftime("%Y-%m-%d") >= best[0]:
                break   # 既に見つかった最短日より後ろは見ない
            n_cur = n_prev = 0
            for k in range(win * 2):
                d = (target - timedelta(days=k)).strftime("%Y-%m-%d")
                if d not in dayset:
                    continue
                if k < win:
                    n_cur += 1
                else:
                    n_prev += 1
            if n_cur >= need_cur and n_prev >= need_prev and n_prev >= n_cur * bal:
                best = (target.strftime("%Y-%m-%d"), qid, n_prev, n_cur)
                break
    if best:
        return best[0], (f"最短は {best[1]}（前窓 {best[2]}/直近窓 {best[3]}）"
                         f"・クエリ単位で計算・毎日収集が続く前提の最短値")
    return None, f"{horizon}日先まで条件を満たすクエリが無い"


def x_lane_state(rows: List[Dict[str, Any]], now: datetime,
                 collect_rows: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
    """X品薄レーンが判定可能か（前28日窓の充填＋台帳の鮮度）を返す。

    `collect_rows`（X収集台帳）を渡すと、前窓が未充填のときに
    〈ウォームアップ中＝見込み開通日つき〉と〈収集が止まっている＝故障〉を区別する。
    """
    if not rows:
        return False, "watch_log が空"
    cutoff = (now - timedelta(days=FRESH_DAYS_X)).strftime("%Y-%m-%d")
    fresh = [r for r in rows if str(r.get("date") or "") >= cutoff]
    if not fresh:
        last = max((str(r.get("date") or "") for r in rows), default="?")
        return False, f"台帳が古い（最終 {last}）"
    with_prev = [r for r in fresh if (r.get("n_wave_prev") or 0) > 0]
    if not with_prev:
        base = f"直近{len(fresh)}件が n_wave_prev=0"
        if collect_rows is not None:
            opens, why = x_warmup_forecast(collect_rows, now)
            if opens:
                return False, (f"{base}＝**ウォームアップ中**（故障ではない）。"
                               f"見込み開通 **{opens}**（{why}）。"
                               "⚠️ configs/x_price_watch.json のクエリ文言か min_faves を編集すると "
                               "query_sha が変わり、そのクエリの前窓履歴が無効化されて28日以上巻き戻る")
            return False, f"{base}＝発火不能（{why}）"
        return False, f"{base}（前28日窓が未充填＝発火不能）"
    return True, f"直近{len(with_prev)}/{len(fresh)} 件で前窓あり"


def news_lane_state(rows: List[Dict[str, Any]], now: datetime) -> Tuple[bool, str]:
    """news_shock レーンが直近に成功して走っているかを返す。

    run_summary が在るだけでは稼働と見なさず、ok>0 を要求する（全クエリ失敗を稼働扱いしない）。
    """
    summaries = [r for r in rows if r.get("type") == "run_summary"]
    if not summaries:
        return False, "run_summary なし"
    last = max(summaries, key=lambda r: str(r.get("run_at") or r.get("ts") or ""))
    ts = str(last.get("run_at") or last.get("ts") or "")
    cutoff = (now - timedelta(days=FRESH_DAYS_NEWS)).isoformat()
    ok = last.get("ok")
    ok_n = ok if isinstance(ok, int) else None
    if ts < cutoff:
        return False, f"最終 run={ts[:16]}（{FRESH_DAYS_NEWS}日窓の外）"
    if ok_n is not None and ok_n <= 0:
        return False, f"最終 run={ts[:16]} だが成功クエリ 0"
    return True, f"最終 run={ts[:16]}・成功{ok_n if ok_n is not None else '?'}"


def build_census_from_data(
    sources: Dict[str, Any],
    shortage: Dict[str, Any],
    news: Dict[str, Any],
    live_ids: Set[str],
    x_live: bool,
    x_note: str,
    news_live: bool,
    news_note: str,
    now: datetime,
    warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """読み込み済みデータから実効カバレッジを算出する（純関数・selftest 対象）。"""
    warnings = warnings if warnings is not None else []
    drivers: Dict[str, Dict[str, Any]] = {}

    def touch(key: str) -> Dict[str, Any]:
        return drivers.setdefault(
            key,
            {
                "driver": key,
                "lanes": set(),
                "series_ids": set(),
                "live_paths": set(),   # 稼働している取得経路（b2b系列ID / 'news' / 'x'）
                "muted_ids": set(),    # 上昇ミュートの系列ID
                "b2b_ids": set(),
                "gate_codes": set(),
                "pending_codes": set(),
                "loose_alias": False,
            },
        )

    series_ids_all: Set[str] = set()

    # --- B2B 価格レーン ---
    series_list = sources.get("series")
    if not isinstance(series_list, list):
        raise ValueError("price_universe_sources.json の series が配列でない")
    for series in series_list:
        if not isinstance(series, dict):
            warnings.append("B2B: series 要素が辞書でない")
            continue
        sid = series.get("id")
        if not sid:
            warnings.append("B2B: id の無い系列を除外")
            continue
        sid = str(sid)
        series_ids_all.add(sid)
        entry = touch(resolve_driver(sid))
        entry["lanes"].add("b2b")
        entry["series_ids"].add(sid)
        entry["b2b_ids"].add(sid)
        if sid in live_ids:
            entry["live_paths"].add(sid)
        if is_muted_on_upside(series):
            entry["muted_ids"].add(sid)
        gate, pending = classify_cards(series.get("beneficiaries"), f"series:{sid}", warnings)
        entry["gate_codes"] |= gate
        entry["pending_codes"] |= pending

    # --- news_shock レーン（系列は B2B と共有＝新規ドライバーにならない設計） ---
    for query in news.get("queries", []) or []:
        if not isinstance(query, dict):
            warnings.append("news: query 要素が辞書でない")
            continue
        for sid in query.get("series_ids", []) or []:
            sid = str(sid)
            if sid not in series_ids_all:
                warnings.append(f"news: 未知の series_id={sid}（B2B 側に存在しない）")
            entry = touch(resolve_driver(sid))
            entry["lanes"].add("news")
            if news_live:
                entry["live_paths"].add("news")

    # --- X品薄レーン ---
    for subject in shortage.get("subjects", []) or []:
        if not isinstance(subject, dict):
            warnings.append("X: subject 要素が辞書でない")
            continue
        sub_id = str(subject.get("id") or "")
        if not sub_id:
            warnings.append("X: id の無い subject を除外")
            continue
        alias = X_SUBJECT_ALIAS.get(sub_id)
        if alias:
            key = resolve_driver(alias[0])
            if alias[0] not in series_ids_all and alias[0] not in drivers:
                warnings.append(f"X alias: {sub_id}→{alias[0]} の統合先系列が存在しない")
        else:
            if sub_id in series_ids_all:
                warnings.append(f"X alias 漏れの疑い: subject '{sub_id}' と同名の系列が存在する")
            key = f"x:{sub_id}"
        entry = touch(key)
        entry["lanes"].add("x")
        if alias and alias[1].startswith("loose"):
            entry["loose_alias"] = True
        if x_live:
            entry["live_paths"].add("x")
        gate, pending = classify_cards(
            subject.get("beneficiaries"), f"subject:{sub_id}", warnings
        )
        entry["gate_codes"] |= gate
        entry["pending_codes"] |= pending

    def upside_open(d: Dict[str, Any]) -> bool:
        """上昇を鳴らせる経路が1つでも残っているか（ミュートはドライバー全体に伝播させない）。"""
        non_muted_b2b = d["b2b_ids"] - d["muted_ids"]
        live_non_muted = (d["live_paths"] & non_muted_b2b) or (d["live_paths"] & {"news", "x"})
        return bool(live_non_muted)

    effective = [d for d in drivers.values() if d["gate_codes"] and upside_open(d)]
    muted_blocked = [
        d for d in drivers.values() if d["gate_codes"] and d["live_paths"] and not upside_open(d)
    ]
    live_no_card = [d for d in drivers.values() if d["live_paths"] and not d["gate_codes"]]
    pending_only = [d for d in drivers.values() if not d["gate_codes"] and d["pending_codes"]]

    companies: Set[str] = set()
    for d in effective:
        companies |= d["gate_codes"]
    pending_only_companies: Set[str] = set()
    for d in pending_only:
        pending_only_companies |= d["pending_codes"]
    pending_all_companies: Set[str] = set()
    for d in drivers.values():
        pending_all_companies |= d["pending_codes"]

    # sign 欠落の警告は件数へ畳む（1行ずつ並べても読めないため）
    nosign = [w for w in warnings if w.startswith("__nosign__")]
    if nosign:
        warnings[:] = [w for w in warnings if not w.startswith("__nosign__")]
        warnings.append(f"sign 未記入のカード {len(nosign)} 件（受益と主張できないため計上しない）")

    raw_inputs = len(series_ids_all) + sum(
        len(q.get("series_ids", []) or []) for q in news.get("queries", []) or []
        if isinstance(q, dict)
    ) + len(shortage.get("subjects", []) or [])
    merged_away = raw_inputs - len(drivers)

    return {
        "generated": now.isoformat(timespec="seconds"),
        "drivers_total": len(drivers),
        "drivers_live": sum(1 for d in drivers.values() if d["live_paths"]),
        "effective": sorted(d["driver"] for d in effective),
        "effective_n": len(effective),
        "effective_loose_n": sum(1 for d in effective if d["loose_alias"]),
        "companies_n": len(companies),
        "target_3x": len(effective) * 3,
        "raw_inputs": raw_inputs,
        "merged_away": merged_away,
        "blockers": {
            "muted_blocked": sorted(d["driver"] for d in muted_blocked),
            "live_no_card": sorted(d["driver"] for d in live_no_card),
            "pending_only": sorted(d["driver"] for d in pending_only),
            "pending_only_companies_n": len(pending_only_companies),
            "pending_all_companies_n": len(pending_all_companies),
        },
        "lanes": {
            "b2b": {"series": len(series_ids_all), "live_series": len(live_ids & series_ids_all)},
            "news": {"queries": len(news.get("queries", []) or []), "live": news_live, "note": news_note},
            "x": {"subjects": len(shortage.get("subjects", []) or []), "live": x_live, "note": x_note},
        },
        "warnings": warnings,
    }


def build_census(now: Optional[datetime] = None) -> Dict[str, Any]:
    """実ファイルを読んで実効カバレッジを算出する。"""
    now = now or datetime.now(timezone.utc)
    warnings: List[str] = []
    sources = _load_json(SOURCES_PATH)
    shortage = _load_json(SHORTAGE_PATH)
    news = _load_json(NEWS_PATH)

    latest = latest_series_status(_iter_jsonl(WEEKLY_LEDGER, warnings), warnings)
    live_ids = live_series_ids(latest, now)
    x_live, x_note = x_lane_state(list(_iter_jsonl(X_WATCH_LOG, warnings)), now,
                                  list(_iter_jsonl(X_COLLECT_LEDGER, warnings)))
    news_live, news_note = news_lane_state(list(_iter_jsonl(NEWS_LEDGER, warnings)), now)

    census = build_census_from_data(
        sources, shortage, news, live_ids, x_live, x_note, news_live, news_note, now, warnings
    )
    # 止まっている（またはウォームアップ中の）レーンが開いたら実効がいくつ増えるかを併記する。
    # 「復旧の価値」を数字で見せないと、増えないレーンの復旧に時間を使ってしまう
    # （2026-08-19 実測: news 復旧は +0 ドライバー＝既存B2Bと重複、X 開通は +4 ドライバー/+6社）
    if not x_live or not news_live:
        upside = build_census_from_data(
            sources, shortage, news, live_ids, True, "", True, "", now, []
        )
        census["lane_upside"] = {
            "effective_n": upside["effective_n"],
            "companies_n": upside["companies_n"],
            "added": sorted(set(upside["effective"]) - set(census["effective"])),
        }
    return census


def _lane_label(lane: Dict[str, Any]) -> str:
    """レーンの見出し語。ウォームアップ中を「発火不能」と書かない（直す物が無いのに直しに行かせない）。"""
    if lane.get("live"):
        return "稼働"
    note = str(lane.get("note") or "")
    if "ウォームアップ中" in note:
        return "ウォームアップ中"
    return "発火不能"


def render_markdown(c: Dict[str, Any]) -> str:
    """census を人が読む1枚に整形する。"""
    b = c["blockers"]
    lines = [
        "# 実効カバレッジ census（監視の広さ・重複除外）",
        "",
        f"- 生成: {c['generated']}（`python3 scripts/coverage_census.py`）",
        "- 数え方の正本: tasks/xprice_reform_review.md §8（P-08a 裁定 2026-08-17）。",
        "  **入力件数ではなく「独立ドライバー×稼働取得経路×関門通過カード」の重複除外集合**を数える。",
        "",
        "## いまの実効カバレッジ",
        "",
        f"- **実効ドライバー数 = {c['effective_n']}**（3倍の目標値 = **{c['target_3x']}**）",
        f"- 銘柄を出せる会社数 = {c['companies_n']} 社（関門通過カードのみ・重複除外）",
        f"- 参考: 全ドライバー {c['drivers_total']} / 稼働中 {c['drivers_live']}"
        f"（入力 {c['raw_inputs']} 件から重複 {c['merged_away']} 件を統合した後）",
        f"- ⚠️ 実効のうち {c['effective_loose_n']} 件は loose な対応表（市場全体を単一指標に縮約）に依存",
        "",
        "## 詰まりの内訳（ここを開けないと網を広げても増えない）",
        "",
        f"- 🔇 カードはあるが上昇を鳴らす経路が無い: {len(b['muted_blocked'])} 件"
        f" — {', '.join(b['muted_blocked']) or 'なし'}",
        f"- 🈳 取得は動くがカード無し: {len(b['live_no_card'])} 件",
        f"- ⏳ 裏取り待ちのみ（confirmed 0・provisional あり）: {len(b['pending_only'])} ドライバー /"
        f" {b['pending_only_companies_n']} 社",
        f"  （参考: 全ドライバーの provisional を合算すると {b['pending_all_companies_n']} 社）",
        "",
        *( [
            f"- 🔓 **止まっているレーンが開いた場合の実効**: {c['lane_upside']['effective_n']} ドライバー /"
            f" {c['lane_upside']['companies_n']} 社"
            f"（増える分: {', '.join(c['lane_upside']['added']) or 'なし'}）",
            "",
        ] if c.get("lane_upside") else [] ),
        "## レーン別の稼働",
        "",
        "| レーン | 登録数 | 稼働 |",
        "|---|---|---|",
        f"| B2B価格 | {c['lanes']['b2b']['series']} 系列 |"
        f" 直近取得OK {c['lanes']['b2b']['live_series']} 系列 |",
        f"| news_shock | {c['lanes']['news']['queries']} クエリ |"
        f" {'稼働' if c['lanes']['news']['live'] else '停止'}（{c['lanes']['news']['note']}） |",
        f"| X品薄 | {c['lanes']['x']['subjects']} subject |"
        f" {_lane_label(c['lanes']['x'])}（{c['lanes']['x']['note']}） |",
        "",
        "## 実効ドライバー一覧",
        "",
        "".join(f"- {d}\n" for d in c["effective"]) or "- なし\n",
    ]
    if c["warnings"]:
        lines += ["", "## ⚠️ 入力の警告（0件と破損の区別）", ""]
        lines += [f"- {w}" for w in c["warnings"][:40]]
        if len(c["warnings"]) > 40:
            lines.append(f"- ほか {len(c['warnings']) - 40} 件")
    return "\n".join(lines)


def _fixture_now() -> datetime:
    return datetime(2026, 8, 17, tzinfo=timezone.utc)


def selftest() -> int:
    """集計器そのものを固定データで検証する（build_census_from_data を実行）。"""
    now = _fixture_now()
    cases: List[Tuple[str, bool]] = []

    sources = {
        "series": [
            # 統合対象・関門通過あり・稼働
            {"id": "wti", "beneficiaries": [{"code": "1605", "sign": "+", "tier": "confirmed"}]},
            {"id": "brent", "beneficiaries": [{"code": "1662", "sign": "+", "tier": "provisional"}]},
            # ミュート系列だが同ドライバーに非ミュートの稼働経路なし
            {"id": "milk", "alert": {"weekly_pct": 999.0},
             "beneficiaries": [{"code": "2264", "sign": "+", "tier": "confirmed"}]},
            # ミュート＋非ミュートが同居（片方が生きていれば実効に残る）
            {"id": "gold-int", "alert": {"weekly_pct": 999.0},
             "beneficiaries": [{"code": "5713", "sign": "+", "tier": "confirmed"}]},
            {"id": "gold-tanaka", "beneficiaries": []},
            # 稼働だがカード無し
            {"id": "urea", "beneficiaries": []},
            # 裏取り待ちのみ
            {"id": "lumber", "beneficiaries": [{"code": "8020", "sign": "+", "tier": "provisional"}]},
            # 未知 tier（警告対象・カードに数えない）
            {"id": "zinc", "beneficiaries": [{"code": "5711", "sign": "+", "tier": "kakutei"}]},
        ]
    }
    news = {"queries": [{"series_ids": ["wti", "brent"]}, {"series_ids": ["nope"]}]}
    shortage = {
        "subjects": [
            {"id": "copper", "beneficiaries": [{"code": "5713", "sign": "+", "tier": "confirmed"}]},
            {"id": "kome", "beneficiaries": []},
            {"id": "lumber", "beneficiaries": []},  # alias で lumber 系列へ統合される
        ]
    }
    live = {"wti", "brent", "milk", "gold-tanaka", "urea", "lumber", "zinc"}

    c = build_census_from_data(
        sources, shortage, news, live, False, "X停止", True, "news稼働", now
    )

    cases.append(("wti/brent が crude-oil に統合", "crude-oil" in c["effective"]))
    cases.append(("統合で入力8+3+3=14 > ドライバー数", c["drivers_total"] < c["raw_inputs"]))
    cases.append(("ミュート単独の milk は実効外", "milk" not in c["effective"]))
    cases.append(("milk は muted_blocked に出る", "milk" in c["blockers"]["muted_blocked"]))
    cases.append(
        ("同ドライバーに非ミュート稼働があれば実効に残る（gold）", "gold" in c["effective"])
    )
    cases.append(("カード無しの urea は live_no_card", "urea" in c["blockers"]["live_no_card"]))
    cases.append(("provisional のみの lumber は pending_only", "lumber" in c["blockers"]["pending_only"]))
    cases.append(
        ("pending_only の会社数はその集合に限る（1社）", c["blockers"]["pending_only_companies_n"] == 1)
    )
    cases.append(
        ("全体の provisional 社数は別枠（2社）", c["blockers"]["pending_all_companies_n"] == 2)
    )
    cases.append(("未知 tier は警告に出る", any("未知の tier" in w for w in c["warnings"])))
    cases.append(("未知 series_id は警告に出る", any("未知の series_id" in w for w in c["warnings"])))
    cases.append(
        ("X停止中は X 固有 subject が実効に入らない", "x:kome" not in c["effective"])
    )
    cases.append(("会社数は重複除外（1605 と 5713 で2社）", c["companies_n"] == 2))
    cases.append(("3倍目標は実効×3", c["target_3x"] == c["effective_n"] * 3))

    # 稼働窓の境界
    latest = {"a": ("2026-08-16", "ok"), "b": ("2026-08-01", "ok"), "c": ("2026-08-16", "error"),
              "d": ("2026-12-31", "ok")}
    live_ids = live_series_ids(latest, now)
    cases.append(("14日窓内の ok は稼働", "a" in live_ids))
    cases.append(("窓外は非稼働", "b" not in live_ids))
    cases.append(("error は非稼働", "c" not in live_ids))
    cases.append(("未来日は非稼働", "d" not in live_ids))

    # 同日2試行は run_at の後勝ち
    rows = [
        {"id": "x", "date": "2026-08-16", "run_at": "2026-08-16T01:00:00", "status": "ok"},
        {"id": "x", "date": "2026-08-16", "run_at": "2026-08-16T09:00:00", "status": "error"},
        {"id": "y", "date": "not-a-date", "status": "ok"},
    ]
    w: List[str] = []
    latest2 = latest_series_status(rows, w)
    cases.append(("同日は後の run_at を採用", latest2["x"][1] == "error"))
    cases.append(("不正日付は除外して警告", "y" not in latest2 and any("日付が不正" in s for s in w)))

    # news レーンの稼働判定
    ok_rows = [{"type": "run_summary", "run_at": "2026-08-16T22:20:00+00:00", "ok": 20}]
    ng_rows = [{"type": "run_summary", "run_at": "2026-08-16T22:20:00+00:00", "ok": 0}]
    cases.append(("news: ok>0 は稼働", news_lane_state(ok_rows, now)[0]))
    cases.append(("news: 全失敗は非稼働", not news_lane_state(ng_rows, now)[0]))
    cases.append(("news: 空は非稼働", not news_lane_state([], now)[0]))

    # X レーンの稼働判定
    cases.append(("X: 前窓ゼロは発火不能",
                  not x_lane_state([{"date": "2026-08-16", "n_wave_prev": 0}], now)[0]))
    cases.append(("X: 前窓ありは稼働",
                  x_lane_state([{"date": "2026-08-16", "n_wave_prev": 5}], now)[0]))
    cases.append(("X: 古い台帳は非稼働",
                  not x_lane_state([{"date": "2026-01-01", "n_wave_prev": 5}], now)[0]))

    # 実配置の不変条件（2026-08-18 P-08e の再発防止）: 確証カードを持つ系列は必ず上昇を鳴らせる。
    # パーム油は §16j の食品ミュート（alert 999）が残ったまま SD Guthrie を確証カード化したため、
    # 「カードはあるが1度も発火しない」状態が生まれた。config 側の取り違えを機械で止める。
    # X品薄レーンの「ウォームアップ中」と「故障」の区別（2026-08-19 追加・敵対レビュー後に強化）
    def _day(n: int) -> str:
        return (_fixture_now() - timedelta(days=n)).strftime("%Y-%m-%d")

    def _rows(qid: str, ns: Iterable[int], sha: str = "s1", **extra) -> List[Dict[str, Any]]:
        return [{"date": _day(n), "status": "ok", "count": 10,
                 "query_id": qid, "query_sha": sha, **extra} for n in ns]

    warm = _rows("q1", range(0, 25))                      # 25日ぶんしか無い＝前窓が埋まらない
    opens, _ = x_warmup_forecast(warm, _fixture_now())
    cases.append(("収集が浅いだけなら見込み開通日が出る", bool(opens) and opens > _day(0)))
    live_w, note_w = x_lane_state([{"date": _day(0), "n_wave_prev": 0}], _fixture_now(), warm)
    cases.append(("ウォームアップは非稼働だが文言に『ウォームアップ中』が入る",
                  (not live_w) and "ウォームアップ中" in note_w and "見込み開通" in note_w))

    dead = _rows("q1", range(40, 70))                     # 40日前で収集が停止
    opens_d, why_d = x_warmup_forecast(dead, _fixture_now())
    cases.append(("収集が止まっていれば見込み日を出さない", opens_d is None and "止まっている" in why_d))
    live_d, note_d = x_lane_state([{"date": _day(0), "n_wave_prev": 0}], _fixture_now(), dead)
    cases.append(("収集停止は『ウォームアップ中』と言わない",
                  (not live_d) and "ウォームアップ中" not in note_d))

    full = _rows("q1", range(0, 70))                      # 70日ぶん＝当日で条件充足
    opens_f, _ = x_warmup_forecast(full, _fixture_now())
    cases.append(("履歴が十分なら見込み開通日は当日", opens_f == _day(0)))

    # ① クエリを跨いだ日付の混線で開通日を早めない（NO-GO 2026-08-19）。
    #    A は偶数日・B は奇数日だけ成功＝集合を畳むと「毎日」に見えるが、どちらも標本が半分
    mixed = _rows("qA", range(0, 70, 2)) + _rows("qB", range(1, 70, 2))
    mixed_open, _ = x_warmup_forecast(mixed, _fixture_now())
    solo_open, _ = x_warmup_forecast(_rows("qA", range(0, 70, 2)), _fixture_now())
    cases.append(("クエリを跨いで日付を畳まない（混線で前倒ししない）", mixed_open == solo_open))

    # ② SHA が変わった旧クエリの履歴で前倒ししない（新SHAは今日から数え直し）
    sha_changed = _rows("q1", range(30, 70), sha="old") + _rows("q1", range(0, 3), sha="new")
    sha_open, _ = x_warmup_forecast(sha_changed, _fixture_now())
    cases.append(("SHA変更後は旧履歴で前倒ししない", sha_open is None or sha_open > _day(0)))

    # ③ clean でない行は標本に数えない（正本 is_clean と同じ3条件）
    dirty = (_rows("q1", range(0, 70), status="blocked")
             + _rows("q2", range(0, 70), censored=True)
             + [{"date": _day(n), "status": "ok", "count": None,
                 "query_id": "q3", "query_sha": "s1"} for n in range(0, 70)])
    dirty_open, dirty_why = x_warmup_forecast(dirty, _fixture_now())
    cases.append(("status/censored/count 欠落の行は数えない",
                  dirty_open is None and "clean" in dirty_why))

    # ④ balance 条件の境界（前窓が直近窓の 0.3 倍に届くかどうかで結果が変わる）
    #    直近28日は毎日・前窓は n 日だけ、という履歴を作って境界を確かめる
    def _bal_case(prev_days: int) -> Optional[str]:
        ns = list(range(0, 28)) + list(range(28, 28 + prev_days))
        return x_warmup_forecast(_rows("q1", ns), _fixture_now())[0]
    cases.append(("前窓が 0.3 倍に足りなければ当日開通にしない（8日）", _bal_case(8) != _day(0)))
    cases.append(("前窓が 0.3 倍に届けば当日開通（9日）", _bal_case(9) == _day(0)))

    # ⑤ 複製したパラメータが正本 wave_state と一致しているか（値をコピーしている以上、
    #    ズレたら見込み日が嘘になる。正本の関数に同じ履歴を流して答え合わせする）
    try:
        import price_watch_alert as _pwa

        def _clean(row: Dict[str, Any]) -> bool:
            return row.get("status") == "ok" and not row.get("censored") and row.get("count") is not None

        by_date = {r["date"]: r for r in _rows("q1", list(range(0, 28)) + list(range(28, 37)))}
        w_ok = _pwa.wave_state(by_date, _day(0), _clean, {}, "s1")
        by_date8 = {r["date"]: r for r in _rows("q1", list(range(0, 28)) + list(range(28, 36)))}
        w_ng = _pwa.wave_state(by_date8, _day(0), _clean, {}, "s1")
        cases.append(("複製パラメータが正本 wave_state と同じ答えを出す",
                      w_ok["wave_ratio"] is not None and w_ng["wave_ratio"] is None))
    except Exception as exc:  # noqa: BLE001  正本が読めない環境でも他のテストは走らせる
        cases.append((f"正本 wave_state と突き合わせできた（{exc}）", False))

    # config が読めない時は「検査しなかった」を PASS にしない（fail-open 禁止・Codex NIT-2）
    try:
        cfg_real = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
        dead_cards = [
            s.get("id")
            for s in cfg_real.get("series", [])
            if is_muted_on_upside(s)
            and any(isinstance(b, dict) and b.get("sign") == "+" and b.get("tier") == "confirmed"
                    for b in (s.get("beneficiaries") or []))
        ]
        cases.append((f"確証カードを持つ系列が上昇ミュートで死んでいない（{dead_cards or 'なし'}）",
                      not dead_cards))
    except (OSError, json.JSONDecodeError) as exc:
        cases.append((f"実 config を読んで不変条件を検査できた（{SOURCES_PATH.name}: {exc}）", False))

    ok = sum(1 for _, passed in cases if passed)
    for name, passed in cases:
        print(f"  {'PASS' if passed else 'FAIL'} {name}")
    print(f"[selftest] {ok}/{len(cases)} PASS")
    return 0 if ok == len(cases) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="実効カバレッジ census")
    parser.add_argument("--selftest", action="store_true", help="固定データで集計器を検証")
    parser.add_argument("--quiet", action="store_true", help="標準出力の要約を省く")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    census = build_census()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render_markdown(census), encoding="utf-8")

    if not args.quiet:
        b = census["blockers"]
        print(
            f"[census] 実効ドライバー {census['effective_n']}（3倍目標 {census['target_3x']}）/"
            f" 会社 {census['companies_n']}社 /"
            f" 全{census['drivers_total']}・稼働{census['drivers_live']}"
            f"（入力{census['raw_inputs']}→統合{census['merged_away']}件）"
        )
        print(
            f"  詰まり: 鳴らせない{len(b['muted_blocked'])} /"
            f" カード無し{len(b['live_no_card'])} /"
            f" 裏取り待ち{len(b['pending_only'])}ドライバー({b['pending_only_companies_n']}社)"
        )
        if census["warnings"]:
            print(f"  ⚠️ 入力警告 {len(census['warnings'])} 件（先頭3件）")
            for line in census["warnings"][:3]:
                print(f"    - {line}")
        print(f"[census] {OUT_PATH} へ出力")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
