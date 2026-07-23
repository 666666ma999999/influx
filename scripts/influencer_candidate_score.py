#!/usr/bin/env python3
"""インフルエンサー候補を記述的に採点する（売買判断には使用しない）。

入力は posts[].{account,date,text} の JSON。価格評価には measure_base_rate の
Canonical calendar/bars loader を import して使い、外部通信は行わない。
"""
from __future__ import annotations

import argparse
import bisect
import csv
import gzip
import hashlib
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import measure_base_rate as mbr  # noqa: E402  Canonical loaders


DEFAULT_INPUT = ROOT / "data/influencer_candidates/observed_posts.json"
DEFAULT_SURGES = ROOT / "output/reverse_lookup/surge_episodes_1y.csv"
DEFAULT_MASTER_DIR = ROOT / "data/jquants/master"
DEFAULT_OUTPUT_DIR = ROOT / "output/influencer_candidates"
ROUND_TRIP_COST = 0.003
HORIZON_BD = 20
DIRECT_THRESHOLD = 0.60
PROSPECTIVE_THRESHOLD = 0.60
EPISODE_THRESHOLD = 2

RESULT_WORDS = ("利確", "爆益", "的中", "取れた", "だった", "してた")
FUTURE_WORDS = ("そう", "期待", "仕込", "狙", "注目")
DIRECT_RE = re.compile(
    r"(?:[\(（【\[]\s*([0-9]{4}|[0-9]{3}[A-Z])\s*[\)）】\]]|\$\s*([0-9]{4}|[0-9]{3}[A-Z]))",
    re.IGNORECASE,
)
VALID_DATE_RE = re.compile(r"^[0-9]{8}$")


def normalize_code(raw: str) -> str:
    """表示用4文字コードをJ-Quantsの5文字コードへ正規化する。"""
    code = unicodedata.normalize("NFKC", raw).upper()
    if not re.fullmatch(r"(?:[0-9]{4}|[0-9]{3}[A-Z])", code):
        raise ValueError(f"invalid security code: {raw!r}")
    return code + "0"


def normalize_name(value: str) -> str:
    """NFKC後、空白・句読点・記号を除去して比較キーを作る。"""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(ch for ch in normalized if unicodedata.category(ch)[0] not in {"P", "S", "Z", "C"})


def read_json_gz(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def load_company_index(master_dir: Path) -> tuple[Path, dict[str, set[str]]]:
    masters = sorted(master_dir.glob("*.json.gz"))
    if not masters:
        raise SystemExit(f"FATAL: master not found: {master_dir}")
    latest = masters[-1]
    index: dict[str, set[str]] = defaultdict(set)
    for rec in read_json_gz(latest).get("data", []):
        name = normalize_name(str(rec.get("CoName", "")))
        code = str(rec.get("Code", "")).upper()
        # 2〜3文字の日本語社名は一般語への包含（例: リード、エン）を「言及」と
        # 誤認しやすく、完全一致を担保できないため明示的なコード無しでは採らない。
        is_ascii_name = bool(re.fullmatch(r"[a-z0-9]+", name))
        safely_matchable = (is_ascii_name and len(name) >= 3) or len(name) >= 4
        if safely_matchable and re.fullmatch(r"(?:[0-9]{5}|[0-9]{3}[A-Z]0)", code):
            index[name].add(code)
    return latest, dict(index)


def direct_codes(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text)
    return list(dict.fromkeys(normalize_code(a or b) for a, b in DIRECT_RE.findall(normalized)))


def company_codes(text: str, company_index: dict[str, set[str]]) -> tuple[list[str], list[str]]:
    """正規化済みCoNameの完全な文字列が本文にあるものだけを解決する。

    同一CoNameが複数コードへ対応する場合は曖昧なので解決しない。
    長い社名を先に採用し、包含される短い社名の二重計上を避ける。
    """
    haystack = normalize_name(text)
    candidates = sorted((name for name in company_index if name in haystack), key=len, reverse=True)
    occupied: list[tuple[int, int]] = []
    resolved: list[str] = []
    ambiguous: list[str] = []
    for name in candidates:
        start = haystack.find(name)
        end = start + len(name)
        if any(not (end <= lo or start >= hi) for lo, hi in occupied):
            continue
        codes = company_index[name]
        if len(codes) != 1:
            ambiguous.append(name)
            continue
        resolved.append(next(iter(codes)))
        occupied.append((start, end))
    return list(dict.fromkeys(resolved)), ambiguous


def prospective_class(text: str) -> tuple[bool, str]:
    if any(word in text for word in RESULT_WORDS):
        return False, "retrospective_result_word"
    if any(word in text for word in FUTURE_WORDS):
        return True, "prospective_future_word"
    return True, "prospective_plain_mention"


def load_surge_rows(path: Path) -> dict[str, list[dict[str, str]]]:
    by_code: dict[str, list[dict[str, str]]] = defaultdict(list)
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            by_code[row["code"]].append(row)
    return dict(by_code)


def match_episodes(code: str, post_date: str, surges: dict[str, list[dict[str, str]]]) -> list[str]:
    return sorted({
        row["episode_start"]
        for row in surges.get(code, [])
        if row["search_window_from"] <= post_date <= row["search_window_to"]
    })


def to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def evaluate_forward(code: str, post_date: str, bdays: list[str], last_bar_day: str) -> dict[str, Any]:
    """投稿翌営業日をsignal、その翌営業日をentryとして§0を評価する。"""
    signal_idx = bisect.bisect_right(bdays, post_date)
    if signal_idx >= len(bdays):
        return {"status": "censored", "reason": "signal_not_arrived"}
    entry_idx = signal_idx + 1
    exit_idx = entry_idx + HORIZON_BD
    base = {"signal_date": bdays[signal_idx]}
    if entry_idx >= len(bdays) or bdays[entry_idx] > last_bar_day:
        return {**base, "status": "censored", "reason": "entry_not_arrived"}
    if exit_idx >= len(bdays) or bdays[exit_idx] > last_bar_day:
        return {
            **base,
            "entry_date": bdays[entry_idx],
            "status": "censored",
            "reason": "20bd_not_matured",
        }

    entry_day = bdays[entry_idx]
    exit_day = bdays[exit_idx]
    entry = to_float(mbr.load_bars_day(entry_day).get(code, {}).get("AdjO"))
    if entry is None or entry <= 0:
        return {
            **base,
            "entry_date": entry_day,
            "exit_date": exit_day,
            "status": "excluded",
            "reason": "entry_adj_open_missing",
        }
    exit_close = to_float(mbr.load_bars_day(exit_day).get(code, {}).get("AdjC"))
    highs = []
    for day in bdays[entry_idx : exit_idx + 1]:
        high = to_float(mbr.load_bars_day(day).get(code, {}).get("AdjH"))
        if high is not None:
            highs.append(high)
    if exit_close is None or not highs:
        return {
            **base,
            "entry_date": entry_day,
            "exit_date": exit_day,
            "status": "excluded",
            "reason": "forward_price_missing",
        }
    gross_return = exit_close / entry - 1.0
    net_return = gross_return - ROUND_TRIP_COST
    mfe = max(highs) / entry - 1.0
    return {
        **base,
        "entry_date": entry_day,
        "exit_date": exit_day,
        "entry_adj_open": entry,
        "exit_adj_close": exit_close,
        "gross_return": gross_return,
        "net_return": net_return,
        "mfe": mfe,
        "touch_20pct": mfe >= 0.20,
        "close_20pct": gross_return >= 0.20,
        "status": "scored",
        "reason": "",
    }


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def yesno(value: bool) -> str:
    return "○" if value else "×"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def render_csv(rows: list[dict[str, Any]]) -> str:
    import io

    fields = [
        "account", "post_date", "code", "source", "prospective", "classification",
        "surge_hit", "episode_starts", "status", "reason", "signal_date", "entry_date",
        "exit_date", "entry_adj_open", "exit_adj_close", "mfe", "touch_20pct",
        "gross_return", "net_return", "close_20pct", "text",
    ]
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fields})
    return buf.getvalue()


def build_report(
    summaries: list[dict[str, Any]], details: list[dict[str, Any]], extraction: dict[str, Any],
    input_path: Path, master_path: Path, surge_path: Path,
) -> str:
    lines = [
        "# インフルエンサー候補 scorecard",
        "",
        "> **記述的採点のみ。売買判断には使用しない。台帳不算入。**",
        "",
        "> **偏り:** 観測サンプルは手動収集で当方が目にした分のみ=偏りあり。full収集(Playwright)前の暫定。数字は上限寄り。",
        "",
        "## アカウント別 scorecard",
        "",
        "| account | n（採点済み言及） | コード直接率 | 社名解決率 | prospective率 | P(+20%タッチ) | P(20bd終値+20%) | EV（コスト後） | 急騰当て数 | コード≥60% | episode≥2 | prospective≥60% | 第15R |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|:---:|:---:|:---:|:---:|",
    ]
    for s in summaries:
        lines.append(
            f"| @{s['account']} | {s['n_scored']} | {pct(s['direct_rate'])} | {pct(s['company_resolution_rate'])} | "
            f"{pct(s['prospective_rate'])} | {pct(s['touch_rate'])} | {pct(s['close20_rate'])} | {pct(s['ev_net'])} | "
            f"{s['independent_episode_hits']} | {yesno(s['pass_direct'])} | {yesno(s['pass_episodes'])} | "
            f"{yesno(s['pass_prospective'])} | **{yesno(s['round15_pass'])}** |"
        )
    lines += [
        "",
        "率の分母: コード直接率=直接コードを含む投稿/全投稿、社名解決率=直接コードなし投稿のうちCoName完全一致で解決できた投稿/直接コードなし投稿、prospective率=抽出済み言及。価格系は採点済み言及のみ。EVは20bd終値リターンから往復コスト0.3%を控除。",
        "",
        "## 抽出・除外内訳",
        "",
        f"- 入力投稿: {extraction['posts']}件",
        f"- 抽出済み言及: {extraction['mentions']}件（直接コード {extraction['direct_mentions']}、社名解決 {extraction['company_mentions']}）",
        f"- 銘柄未解決投稿: {extraction['unresolved_posts']}件",
        f"- 曖昧CoName（複数コード）: {extraction['ambiguous_names']}件",
        f"- 採点済み: {extraction['scored']}件",
        f"- censored（20bd未確定等）: {extraction['censored']}件",
        f"- 価格欠損等の除外: {extraction['excluded']}件",
        "",
        "### 除外理由",
        "",
    ]
    if extraction["reason_counts"]:
        for reason, count in sorted(extraction["reason_counts"].items()):
            lines.append(f"- {reason}: {count}件")
    else:
        lines.append("- なし")
    lines += [
        "",
        "## 言及明細",
        "",
        "| account | 投稿日 | code | 抽出元 | prospective | 急騰事前的中 | 状態 | 20bd net | MFE |",
        "|---|---|---|---|:---:|---|---|---:|---:|",
    ]
    for d in details:
        episode = ",".join(d["episode_starts"]) or "—"
        net = pct(d.get("net_return"))
        mfe = pct(d.get("mfe"))
        lines.append(
            f"| @{d['account']} | {d['post_date']} | {d['code']} | {d['source']} | "
            f"{yesno(d['prospective'])} | {episode} | {d['status']} | {net} | {mfe} |"
        )
    lines += [
        "",
        "## 定義・provenance",
        "",
        "- 日程: 投稿日の翌営業日=signal_date、その翌営業日始値=entry、entryから20営業日後終値=exit（entry日を0日目）。MFEはentry〜exitの調整後高値。",
        "- prospective heuristic: 結果報告語があれば事後。それ以外は、将来語あり又は単なる銘柄提示として事前扱い。",
        "- 急騰事前的中: code一致かつ投稿日がepisodeのsearch_window_from〜search_window_to内。独立数はepisode_startのユニーク数。",
        f"- input: `{input_path.relative_to(ROOT)}`",
        f"- master: `{master_path.relative_to(ROOT)}`（最新）",
        f"- surge episodes: `{surge_path.relative_to(ROOT)}`",
        "- price/calendar: `scripts/measure_base_rate.py` の `load_bars_day` / `load_calendar_days` / `all_business_days` をCanonical import。",
        "",
    ]
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--master-dir", type=Path, default=DEFAULT_MASTER_DIR)
    parser.add_argument("--surges", type=Path, default=DEFAULT_SURGES)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    posts = payload.get("posts")
    if not isinstance(posts, list):
        raise SystemExit("FATAL: input must contain posts[]")
    master_path, companies = load_company_index(args.master_dir)
    surges = load_surge_rows(args.surges)
    bdays = mbr.all_business_days(mbr.load_calendar_days())
    bar_files = sorted((ROOT / "data/jquants/bars").glob("*.json.gz"))
    if not bar_files:
        raise SystemExit("FATAL: bars cache is empty")
    last_bar_day = bar_files[-1].name.split(".", 1)[0]

    details: list[dict[str, Any]] = []
    post_stats: dict[str, dict[str, int]] = defaultdict(lambda: Counter())
    unresolved_posts = 0
    ambiguous_count = 0
    for index, post in enumerate(posts):
        if not isinstance(post, dict):
            raise SystemExit(f"FATAL: posts[{index}] must be an object")
        account = str(post.get("account", "")).lstrip("@")
        date = str(post.get("date", ""))
        text = str(post.get("text", ""))
        if not account or not VALID_DATE_RE.fullmatch(date) or not text:
            raise SystemExit(f"FATAL: invalid posts[{index}]")
        post_stats[account]["posts"] += 1
        direct = direct_codes(text)
        if direct:
            post_stats[account]["direct_posts"] += 1
        company, ambiguous = company_codes(text, companies)
        ambiguous_count += len(ambiguous)
        company = [code for code in company if code not in direct]
        if not direct:
            post_stats[account]["non_direct_posts"] += 1
            if company:
                post_stats[account]["company_resolved_posts"] += 1
        mentions = [(code, "direct_code") for code in direct] + [(code, "company_name") for code in company]
        if not mentions:
            unresolved_posts += 1
            post_stats[account]["unresolved_posts"] += 1
            continue
        prospective, classification = prospective_class(text)
        for code, source in mentions:
            episode_starts = match_episodes(code, date, surges)
            forward = evaluate_forward(code, date, bdays, last_bar_day)
            details.append({
                "account": account,
                "post_date": date,
                "code": code,
                "source": source,
                "prospective": prospective,
                "classification": classification,
                "surge_hit": bool(episode_starts),
                "episode_starts": episode_starts,
                "text": text,
                **forward,
            })

    summaries: list[dict[str, Any]] = []
    for account in sorted(post_stats):
        stats = post_stats[account]
        account_rows = [row for row in details if row["account"] == account]
        scored = [row for row in account_rows if row["status"] == "scored"]
        episodes = {(row["code"], ep) for row in account_rows for ep in row["episode_starts"]}
        direct_rate = stats["direct_posts"] / stats["posts"]
        company_rate = (
            stats["company_resolved_posts"] / stats["non_direct_posts"]
            if stats["non_direct_posts"] else None
        )
        prospective_rate = mean([float(row["prospective"]) for row in account_rows])
        pass_direct = direct_rate >= DIRECT_THRESHOLD
        pass_episodes = len(episodes) >= EPISODE_THRESHOLD
        pass_prospective = prospective_rate is not None and prospective_rate >= PROSPECTIVE_THRESHOLD
        summaries.append({
            "account": account,
            "posts": stats["posts"],
            "n_scored": len(scored),
            "direct_rate": direct_rate,
            "company_resolution_rate": company_rate,
            "prospective_rate": prospective_rate,
            "touch_rate": mean([float(row["touch_20pct"]) for row in scored]),
            "close20_rate": mean([float(row["close_20pct"]) for row in scored]),
            "ev_net": mean([row["net_return"] for row in scored]),
            "independent_episode_hits": len(episodes),
            "pass_direct": pass_direct,
            "pass_episodes": pass_episodes,
            "pass_prospective": pass_prospective,
            "round15_pass": pass_direct and pass_episodes and pass_prospective,
        })

    statuses = Counter(row["status"] for row in details)
    reasons = Counter(row["reason"] for row in details if row["status"] != "scored")
    extraction = {
        "posts": len(posts),
        "mentions": len(details),
        "direct_mentions": sum(row["source"] == "direct_code" for row in details),
        "company_mentions": sum(row["source"] == "company_name" for row in details),
        "unresolved_posts": unresolved_posts,
        "ambiguous_names": ambiguous_count,
        "scored": statuses["scored"],
        "censored": statuses["censored"],
        "excluded": statuses["excluded"],
        "reason_counts": dict(reasons),
    }
    report = build_report(summaries, details, extraction, args.input, master_path, args.surges)
    json_payload = {
        "disclaimer": "記述的採点のみ。売買判断には使用しない。台帳不算入。",
        "bias": "観測サンプルは手動収集で当方が目にした分のみ=偏りあり。full収集(Playwright)前の暫定。数字は上限寄り。",
        "parameters": {
            "round_trip_cost": ROUND_TRIP_COST,
            "horizon_bd": HORIZON_BD,
            "last_bar_day": last_bar_day,
            "master": str(master_path.relative_to(ROOT)),
        },
        "extraction": extraction,
        "scorecards": summaries,
        "mentions": details,
    }
    atomic_write(args.output_dir / "scorecard.md", report)
    atomic_write(args.output_dir / "scorecard.json", json.dumps(json_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    atomic_write(args.output_dir / "mentions.csv", render_csv(details))
    for name in ("scorecard.md", "scorecard.json", "mentions.csv"):
        path = args.output_dir / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        print(f"{path.relative_to(ROOT)} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
