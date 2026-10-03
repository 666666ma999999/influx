"""X 品薄系の候補語の「遡り取得＋当たり判定」計器（便 3・P-INF-22 裁定 2026-10-03 Q3=a）.

何をするか:
  1. collect: 候補語ごとに過去 N 日を **1 日窓（JST 日）** で遡って本文を取り、語×日のファイルへ保存する。
     窓は `since:D until:D`（2026-10-03 実測: X の since/until は JST 基準・until の日を含む。
     本番の収集器が使う `since:D until:D+1` は実質 2 JST 日ぶん＝連日が重なる。本計器は重ねない）。
     取れた日は飛ばす（中断・再開可）。保存形は本番 texts と同じキー＋likes/author。
  2. eval: 保存した本文を語ごとに判定して `output/x_vocab_eval.md` を書く。
     判定の定義（叩き台・この 1 箇所が正本）:
       受益語あり … 受益表 configs/x_shortage_map.json の **actionable=true の主題** の label 語（「/」「・」区切り）
                   のどれかを本文に含む＝株への出口がある主題に紐づけ得る投稿
       銘柄名あり … 同じ主題の beneficiaries の社名を本文に含む
       新規候補   … 既知語彙（ユニバース MD・固定クエリ）とストップリストに無い候補トークンを 1 つ以上含む
                   （発見器 price_watch_discover.extract_candidates と同じ切り方）
       相場実況   … price_watch_discover.MARKET_POST_RE に当たる（株式市場の実況＝実物の品薄でない）
       告知文     … ANNOUNCE_RE（完売・再入荷・販売スタート 等＝店の宣伝）
       転売語     … 「転売」を含む（除外には使わない・件数を見るだけ＝一致 5）
     「当たり率」= 受益語あり ÷ 取れた投稿。語の採否はこの率と新規候補率の 2 軸で見る（1 軸にしない＝B1・C4）。

実行（VNC コンテナ・既存の検索実績経路）:
    docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/x_vocab_backfill_eval.py collect --days 14
    docker exec xstock-vnc python3 /app/scripts/x_vocab_backfill_eval.py eval
    docker exec xstock-vnc python3 /app/scripts/x_vocab_backfill_eval.py eval --selftest   # 判定部の自己テスト（判定は発見器の語彙を借りるため playwright 入りのコンテナで）
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))

JST = timezone(timedelta(hours=9))
DEFAULT_WORDS = ["品薄", "品不足", "入手困難", "供給不足", "逼迫", "調達難"]  # 先頭= 基準語
MIN_FAVES = 20   # 発見器と同じ下限
PER_QUERY = 60
MAX_SCROLLS = 4
OUT_DIR = APP / "data/x_price_watch/texts_vocab_eval"
REPORT = APP / "output/x_vocab_eval.md"
SHORTAGE_MAP = APP / "configs/x_shortage_map.json"
PROFILE = APP / "x_profiles/maaaki"

ANNOUNCE_RE = re.compile(
    r"完売|再入荷|販売スタート|販売開始|発売中|予約受付|POP ?UP|ポップアップ|キャンペーン|プレゼント|フォロー&|フォロー＆|RT&|抽選で"
)
RESALE_RE = re.compile(r"転売")


# ---------------------------------------------------------------- 判定（純関数・ホストで試験可）
def load_subject_terms(path: Path = SHORTAGE_MAP) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """actionable=true の主題 → label 語リスト／社名リスト。"""
    d = json.loads(path.read_text(encoding="utf-8"))
    terms: dict[str, list[str]] = {}
    names: dict[str, list[str]] = {}
    for s in d["subjects"]:
        if not s.get("actionable"):
            continue
        parts = [p.strip() for p in re.split(r"[/／・,、]", s.get("label", "")) if len(p.strip()) >= 2]
        terms[s["id"]] = [unicodedata.normalize("NFKC", p) for p in parts]
        names[s["id"]] = [unicodedata.normalize("NFKC", b["name"]) for b in s.get("beneficiaries", []) if b.get("name")]
    return terms, names


def judge(text: str, terms: dict[str, list[str]], names: dict[str, list[str]],
          known: set[str], token_re: re.Pattern, stoplist: set[str], market_re: re.Pattern) -> dict:
    t = unicodedata.normalize("NFKC", text or "")
    hit_subjects = [sid for sid, ws in terms.items() if any(w in t for w in ws)]
    hit_names = [sid for sid, ns in names.items() if any(n in t for n in ns)]
    new_tokens = sorted({tok for tok in token_re.findall(t)
                         if tok not in known and tok not in stoplist and len(tok) >= 3
                         and not re.fullmatch(r"[0-9,.\-]+", tok)})  # 数字だけの断片は除外（発見器 :152 と同条件）
    return {
        "subjects": hit_subjects,
        "names": hit_names,
        "new_tokens": new_tokens,
        "market": bool(market_re.search(t)),
        "announce": bool(ANNOUNCE_RE.search(t)),
        "resale": bool(RESALE_RE.search(t)),
    }


# ---------------------------------------------------------------- collect
def jst_days(end_day: str, days: int) -> list[str]:
    end = datetime.strptime(end_day, "%Y-%m-%d")
    return [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]


def out_path(word: str, day: str) -> Path:
    return OUT_DIR / f"{day}__{word}.jsonl"


def collect(words: list[str], days: list[str]) -> int:
    import fetch_bookmarks  # noqa: E402
    import x_search_collect_twittora as xs  # noqa: E402
    from bookmarks_keyword_digest_collect_browser import LoginWallError, build_context_kwargs  # noqa: E402
    from playwright.sync_api import sync_playwright

    xs.MAX_SCROLLS = MAX_SCROLLS
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    todo = [(day, w) for day in days for w in words if not out_path(w, day).exists()]
    print(f"[collect] words={len(words)} days={len(days)} todo={len(todo)} (skip={len(words) * len(days) - len(todo)})")
    if not todo:
        return 0
    cookies = fetch_bookmarks.load_cookies(str(PROFILE))
    rc = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(**build_context_kwargs())
        context.add_cookies(cookies)
        page = context.new_page()
        for i, (day, w) in enumerate(todo):
            try:
                rows = xs.collect_query(page, w, day, day, MIN_FAVES, PER_QUERY, lang="ja", tab="live")
            except LoginWallError as exc:
                print(f"✗ ログイン壁で中断: {exc}", file=sys.stderr)
                return 1
            except Exception as exc:  # noqa: BLE001  語×日の単位で fail-soft（ファイルを作らない＝次回再試行）
                print(f"[warn] {day} {w}: {type(exc).__name__}", file=sys.stderr)
                rc = 1
                time.sleep(random.uniform(5.0, 10.0))
                continue
            with out_path(w, day).open("w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps({
                        "date": day, "word": w, "status_id": r.get("id"), "text": r.get("content") or "",
                        "likes": r.get("likes"), "author": r.get("author"), "posted_at": r.get("posted_at"),
                        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }, ensure_ascii=False) + "\n")
            print(f"[ok] {day} {w}: {len(rows)} 件 ({i + 1}/{len(todo)})")
            if i < len(todo) - 1:
                time.sleep(random.uniform(20.0, 40.0))
        browser.close()
    return rc


# ---------------------------------------------------------------- eval
def load_rows() -> tuple[list[dict], set[tuple[str, str]]]:
    """保存行と、収集済みの (日, 語) の集合（0 件の日もファイルは存在する＝日数と語の欠落を防ぐ）。"""
    rows: list[dict] = []
    collected: set[tuple[str, str]] = set()
    for p in sorted(OUT_DIR.glob("*.jsonl")):
        day, _, word = p.stem.partition("__")
        collected.add((day, word))
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows, collected


def evaluate(rows: list[dict], terms, names, known, token_re, stoplist, market_re,
             collected: set[tuple[str, str]] | None = None) -> tuple[list[dict], dict]:
    per_word: dict[str, Counter] = defaultdict(Counter)
    examples: dict[str, list[str]] = defaultdict(list)
    new_tok: dict[str, Counter] = defaultdict(Counter)
    days: dict[str, set] = defaultdict(set)
    seen: set[str] = set()
    for day, w in (collected or set()):   # 0 件で終わった語・日も表と日数に残す
        per_word[w]["n"] += 0
        days[w].add(day)
    for r in rows:
        key = f"{r.get('word')}:{r.get('status_id')}"
        if key in seen:
            continue
        seen.add(key)
        w = r.get("word", "?")
        j = judge(r.get("text", ""), terms, names, known, token_re, stoplist, market_re)
        c = per_word[w]
        c["n"] += 1
        days[w].add(r.get("date"))
        if j["subjects"]:
            c["subject"] += 1
            if len(examples[w]) < 3:
                examples[w].append(f"[{'/'.join(j['subjects'])}] {r.get('text', '')[:70]}")
        if j["names"]:
            c["name"] += 1
        if j["new_tokens"]:
            c["new"] += 1
            for tok in j["new_tokens"]:
                new_tok[w][tok] += 1
        c["market"] += j["market"]
        c["announce"] += j["announce"]
        c["resale"] += j["resale"]
    table = []
    for w in per_word:
        c = per_word[w]
        n = c["n"] or 1
        table.append({
            "word": w, "days": len(days[w]), "n": c["n"],
            "subject": c["subject"], "subject_pct": round(100 * c["subject"] / n, 1),
            "name": c["name"], "new": c["new"], "new_pct": round(100 * c["new"] / n, 1),
            "market": c["market"], "announce": c["announce"], "resale": c["resale"],
            "top_new": [t for t, _ in new_tok[w].most_common(8)],
            "examples": examples[w],
        })
    table.sort(key=lambda x: (-x["subject_pct"], -x["n"]))
    all_days = sorted(d for ds in days.values() for d in ds if d)
    meta = {"rows": len(rows), "unique": len(seen), "words": len(per_word),
            "day_min": all_days[0] if all_days else None, "day_max": all_days[-1] if all_days else None}
    return table, meta


def write_report(table: list[dict], meta: dict) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    L = [f"# X 品薄系 候補語の当たり判定（便 3・P-INF-22）", "",
         f"生成: {datetime.now(JST).strftime('%Y-%m-%d %H:%M JST')}　母集団: `data/x_price_watch/texts_vocab_eval/` の全行 {meta['rows']} 行・語×投稿で重複除去後 {meta['unique']} 件・語 {meta['words']}"
         + (f"・窓= {meta['day_min']}〜{meta['day_max']}（保存データの実期間・JST 日・since:D until:D・いいね下限 {MIN_FAVES}）" if meta.get("day_min") else ""),
         "", "判定の定義は `scripts/x_vocab_backfill_eval.py` 冒頭（叩き台）。**当たり率= 受益語あり ÷ 取れた投稿**・新規候補率と 2 軸で読む。並べ替え= 当たり率の降順→件数。", "",
         "| 語 | 日数 | 投稿 | 受益語あり | 当たり率% | 銘柄名あり | 新規候補 | 新規率% | 相場実況 | 告知文 | 転売語 |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in table:
        L.append(f"| {t['word']} | {t['days']} | {t['n']} | {t['subject']} | {t['subject_pct']} | {t['name']} | {t['new']} | {t['new_pct']} | {t['market']} | {t['announce']} | {t['resale']} |")
    L.append("")
    for t in table:
        L.append(f"## {t['word']}")
        L.append(f"- 新規候補トークン上位: {', '.join(t['top_new']) or 'なし'}")
        for e in t["examples"]:
            L.append(f"- 受益語あり例: {e}")
        L.append("")
    REPORT.write_text("\n".join(L), encoding="utf-8")


def _judge_deps():
    import price_watch_discover as d  # noqa: E402  playwright 依存は import 時に出ない（関数内）
    return d.load_known_vocab(), d.TOKEN_RE, d.STOPLIST, d.MARKET_POST_RE


def selftest() -> int:
    terms, names = load_subject_terms()
    known, token_re, stoplist, market_re = _judge_deps()
    a = judge("DDR5 メモリが品薄で DRAM スポットが上がっている。ディスコの装置も好調", terms, names, known, token_re, stoplist, market_re)
    assert "dram" in a["subjects"], a
    assert "dram" in a["names"], a
    b = judge("日経平均 大引け 全面高。半導体関連株が強い", terms, names, known, token_re, stoplist, market_re)
    assert b["market"], b
    c = judge("ちいかわのシール 完売いたしました。再入荷は未定です", terms, names, known, token_re, stoplist, market_re)
    assert c["announce"] and not c["subjects"], c
    d_ = judge("メモリ不足を転売屋のせいにしてる人", terms, names, known, token_re, stoplist, market_re)
    assert d_["resale"], d_
    rows = [{"word": "品薄", "status_id": "1", "text": "DDR5 が品薄", "date": "2026-10-01"},
            {"word": "品薄", "status_id": "1", "text": "DDR5 が品薄", "date": "2026-10-01"},
            {"word": "品薄", "status_id": "2", "text": "シール 完売", "date": "2026-10-02"}]
    collected = {("2026-10-01", "品薄"), ("2026-10-02", "品薄"), ("2026-10-01", "調達難")}  # 調達難は 0 件の日だけ
    table, meta = evaluate(rows, terms, names, known, token_re, stoplist, market_re, collected)
    assert meta["unique"] == 2 and table[0]["n"] == 2 and table[0]["subject"] == 1 and table[0]["announce"] == 1, (table, meta)
    assert {t["word"] for t in table} == {"品薄", "調達難"} and [t for t in table if t["word"] == "調達難"][0]["n"] == 0, table
    assert meta["day_min"] == "2026-10-01" and meta["day_max"] == "2026-10-02", meta
    e = judge("999987円で品薄", terms, names, known, token_re, stoplist, market_re)
    assert e["new_tokens"] == [], e  # 数字だけの断片は新規候補にしない（Codex P2）
    print("selftest ok:", {k: v for k, v in a.items() if k != "new_tokens"})
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["collect", "eval"])
    ap.add_argument("--words", nargs="*", default=DEFAULT_WORDS)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--end", help="最新の JST 日 YYYY-MM-DD（省略時= 昨日 JST）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    end = args.end or (datetime.now(JST) - timedelta(days=1)).strftime("%Y-%m-%d")
    days = jst_days(end, args.days)
    if args.mode == "collect":
        return collect(args.words, days)
    if args.selftest:
        return selftest()
    terms, names = load_subject_terms()
    known, token_re, stoplist, market_re = _judge_deps()
    rows, collected = load_rows()
    table, meta = evaluate(rows, terms, names, known, token_re, stoplist, market_re, collected)
    write_report(table, meta)
    print(f"[eval] rows={meta['rows']} unique={meta['unique']} → {REPORT}")
    for t in table:
        print(f"  {t['word']:<6} n={t['n']:<4} 当たり={t['subject_pct']}% 新規={t['new_pct']}% 相場={t['market']} 告知={t['announce']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
