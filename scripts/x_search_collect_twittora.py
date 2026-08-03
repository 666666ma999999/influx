#!/usr/bin/env python3
"""@twittora_ 向けバズ投稿収集 — X ログイン検索スクレイプ版。

2026-07-19 新設 → 同日 v2（精度改善 P1-P3・Fable5+Codex 敵対レビュー収束案）:
  P1 本文カラ対策: スクロール毎に取り込み（仮想化対策）＋空本文は隔離し上位のみ個別ページ再取得
     ＋完全性セルフチェック（空率/ja率/views被覆率を出力に記録・空率20%超で警告）
  P2 指標の完全取得: カードの [role=group] aria-label から replies/reposts/likes/bookmarks/views を
     **正確な生数値**で抽出（従来の「impressions は取れない」は誤った前提だった＝実機診断で確認）
  P3 lang:ja 別窓: 日本語比重の高い5クエリを lang:ja + 低め閾値で追加収集（英語大手の top 独占対策）
  既知課題→解消: X が日本語原文を英訳して表示する件は、収集アカウントの表示言語を ja へ変更して解消
  （2026-07-19 夜・検索3クエリ11/11件が日本語原文と実測。ja 判定のクエリ由来は保険として維持）
  → ja 判定はクエリ由来で行う。原文取得（Show original 相当）は次回改善

背景: Grok x_search 版が xAI クレジット枯渇で停止（2026-07-01 確定判断: 第一選択は Cookie 自動収集）。
出力契約: output/grok_twittora/grok-twittora-YYYY-MM-DD.jsonl + .md（grok 版と同系・collector で区別）。

2026-07-26 T11/Critical2 item10: 本スクリプトの metrics_missing は値そのものを 0 初期化のまま残す
（ソート・self-check の安全のため null にしない）。教師データ（training_data/schema.md）へ手動転記する
際は metrics_missing に載ったフィールドを null に変換すること（0 のまま持ち込むと反応ゼロと誤認される）。

2026-07-26 Fix1-4（実データ調査で確定した空本文37/95の実体=X Articles投稿への対応・syndication救済）:
  Fix1 空本文の救済フォールバック: DOM個別再取得(cap8)後もなお空の行に syndication API
    （normalize_master_posts.fetch_syndication 再利用・無認証）を試行。payload.article が
    あれば title+preview_text を content にし content_source="x_article" を必ず立てる
    （通常ツイート本文と区別・下流の教師データ汚染防止）。article でなく text が返れば
    content_source="syndication" で通常本文を復元。cap50・リクエスト間隔1秒（アカウントリスクなし）
  Fix2 完全性セルフチェック: empty_rate の分母を「content_source が x_article/syndication
    でない=説明のつかない空」に変更（X Articles 普及で20%閾値が恒久アラーム化し、本物の DOM
    回帰が埋もれる問題を解消）
  Fix3 inner_text() は未描画時に例外を投げず空文字を返すことがある → 空文字のときも
    text_content() へフォールバックするよう対応
  Fix4 DOM由来の本文（翻訳・タイムライン切断済みの可能性）に normalize_master_posts.
    build_norm_fields の判定を適用し、原文と異なれば syndication 原文で置換
    （元文は content_preview に退避・content_normalized=True）

実行（xstock-vnc コンテナ内・DISPLAY 必須）:
  docker exec -e DISPLAY=:99 xstock-vnc python3 /app/scripts/x_search_collect_twittora.py --days 7
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fetch_bookmarks  # Cookie loader（canonical）
from bookmarks_keyword_digest_collect_browser import (  # 基盤部品を再利用
    LoginWallError,
    _wait_for_new_cards,
    build_context_kwargs,
    is_login_wall_url,
)
from grok_collect_twittora import DEFAULT_MIN_LIKES, DEFAULT_QUERIES  # クエリ正本を共有
import normalize_master_posts  # syndication救済・翻訳/切断正規化を再利用（2026-07-26 Fix1/4）

JST = timezone(timedelta(hours=9))
TWEET_CARD_SELECTOR = '[data-testid="tweet"]'
# 2026-08-03: 空本文率37%（7/26 実測・警告閾値20%超）の主因を DOM の遅延レンダリングと判断し
# 5→8 に引き上げ（敵対レビュー一致指摘）。ループは per_query 到達か新規カード枯渇で早期break
# するため、当たりが薄いクエリでの追加コストは限定的。待機の主体は QUERY_PACING 側のまま。
MAX_SCROLLS = 8
QUERY_PACING = (20.0, 40.0)  # クエリ間の待機（アカウント保護・秒）

# P3: 日本語比重の高いクエリ（lang:ja 別窓で追加収集する）
# 2026-07-26: gen2追加6本（grok_collect_twittora.py DEFAULT_QUERIES）を実読し、日本語主体の3本を追加。
#   採用: "Claude Code 新機能 アップデート"（既存の"Claude Code 使い方/tips"と同型）、
#         "Obsidian 第二の脳 Claude"（"第二の脳"は完全な和製慣用句）、
#         "Fable オーケストレーション"（"AI コーディング ワークフロー"と同型＝英語ツール名+和語カタカナ概念）
#   非採用: "Claude Skills Routines"（日本語ゼロ）、
#          "NotebookLM Codex 文字起こし" / "Opus Fable Codex 使い分け"
#          （英語固有名詞が主体で末尾に和語1語＝非採用だった旧"MCP server 自作"等と同型）
JA_SPLIT_QUERIES = [
    "Claude Code 使い方",
    "Claude Code tips",
    "Claude hooks 自動化",
    "AI コーディング ワークフロー",
    "subagent 並列",
    "Claude Code 新機能 アップデート",
    "Obsidian 第二の脳 Claude",
    "Fable オーケストレーション",
    # gen3 日本語6本（2026-08-03 敵対レビューA指摘: JA別窓に未登録だと旧クエリだけが
    # min_faves:20 の低floor窓を持ち、新クエリは50の1窓のみ＝floor不公平の測定バイアス）
    "launchd 止まってた",
    "CLAUDE.md 守らない",
    "Claude hooks 誤検知",
    "Claude 出力 テンプレ",
    "AIエージェント 承認 フロー",
    "Claude Code 引き継ぎ",
]

_STATUS_ID_RE = re.compile(r"/status/(\d+)")
# aria-label 例(EN): "1222 replies, 1855 reposts, 15717 likes, 1387 bookmarks, 1648921 views"
# aria-label 例(JA): "110 件の返信、169 件のリポスト、2714 件のいいね、1081 件のブックマーク、221859 件の表示"
# 2026-07-20 収集アカウントの表示言語を ja 化（原文取得対策）→ aria も日本語になるため両対応
_ARIA_METRIC_RE = re.compile(r"([\d,]+)\s+(replies|reply|reposts|repost|likes|like|bookmarks|bookmark|views|view)\b")
_ARIA_METRIC_JA_RE = re.compile(r"([\d,]+)\s*件の(返信|リポスト|いいね|ブックマーク|表示)")
_JA_KIND = {"返信": "replies", "リポスト": "retweets", "いいね": "likes", "ブックマーク": "bookmarks", "表示": "impressions"}
_JA_RE = re.compile(r"[ぁ-んァ-ン一-龥]")


def build_buzz_search_url(q: str, since: str, until: str, min_likes: int, lang: str | None = None) -> str:
    """バズ検索 URL（f=top 明示・min_faves・期間窓・任意 lang）。"""
    parts = [q, f"min_faves:{min_likes}", f"since:{since}", f"until:{until}", "-filter:nativeretweets"]
    if lang:
        parts.append(f"lang:{lang}")
    return f"https://x.com/search?q={quote(' '.join(parts))}&f=top"


def parse_aria_metrics(aria: str | None) -> dict:
    """[role=group] の aria-label から正確な指標を抽出（部分欠けにも耐える）。

    2026-07-26 T11: aria から検出できなかった指標は従来どおり0のまま返しつつ、
    `metrics_missing` に欠損フィールド名を積む（実測ゼロと計測不能の事後区別）。
    0値そのものの意味・ソート・self-checkのロジックは変更しない（実測で欠損は稀）。
    """
    out = {"replies": 0, "retweets": 0, "likes": 0, "bookmarks": 0, "impressions": 0}
    all_keys = ("replies", "retweets", "likes", "bookmarks", "impressions")
    found: set[str] = set()
    if not aria:
        out["metrics_missing"] = list(all_keys)
        return out
    for num, kind in _ARIA_METRIC_RE.findall(aria):
        v = int(num.replace(",", ""))
        if kind.startswith("repl"):
            out["replies"] = v
            found.add("replies")
        elif kind.startswith("repost"):
            out["retweets"] = v
            found.add("retweets")
        elif kind.startswith("like"):
            out["likes"] = v
            found.add("likes")
        elif kind.startswith("bookmark"):
            out["bookmarks"] = v
            found.add("bookmarks")
        elif kind.startswith("view"):
            out["impressions"] = v
            found.add("impressions")
    for num, kind in _ARIA_METRIC_JA_RE.findall(aria):
        key = _JA_KIND[kind]
        out[key] = int(num.replace(",", ""))
        found.add(key)
    out["metrics_missing"] = [k for k in all_keys if k not in found]
    return out


def scrape_cards_rich(page) -> list[dict]:
    """表示中カードから url/author/本文/正確指標を抽出（buzz 収集専用のリッチ版）。

    digest 系の scrape_search_results_from_dom は likes 表示文字列のみの軽量契約のため
    共有関数は変更せず、本スクリプト専用に aria ベースの正確抽出を持つ。
    """
    results = []
    cards = page.locator(TWEET_CARD_SELECTOR)
    for i in range(cards.count()):
        try:
            card = cards.nth(i)
            url = ""
            links = card.locator('a[href*="/status/"]')
            if links.count():
                href = links.first.get_attribute("href") or ""
                if href.startswith("/"):
                    href = f"https://x.com{href}"
                url = href.split("?")[0]
            m = _STATUS_ID_RE.search(url)
            if not m:
                continue
            content = ""
            tt = card.locator('[data-testid="tweetText"]')
            if tt.count():
                try:
                    content = tt.first.inner_text(timeout=1500) or ""
                except Exception:
                    content = ""
                if not content:
                    # Fix3(2026-07-26): inner_text() は未描画時（仮想化スクロール中）に
                    # 例外を投げず "" を返すことがある → 空文字でも text_content() へ再フォールバック
                    try:
                        content = tt.first.text_content(timeout=1500) or ""
                    except Exception:
                        # Critical(2026-07-26 validator指摘): ここが無保護だとカード単位
                        # fail-soft(:195)に落ちてカード全体が消える（Fix3以前より後退）。
                        # 例外時は空文字のまま＝従来どおり隔離扱いに戻す。
                        content = ""
            dt_attr = ""
            te = card.locator("time")
            if te.count():
                dt_attr = te.first.get_attribute("datetime") or ""
            grp = card.locator('[role="group"]')
            aria = grp.first.get_attribute("aria-label") if grp.count() else None
            metrics = parse_aria_metrics(aria)
            path = url.split("x.com/")[-1]
            author = path.split("/")[0] if "/" in path else ""
            results.append({
                "id": m.group(1),
                "url": url,
                "author": author,
                "display_name": "",
                # U+2028/U+2029 は json.dumps 素通し + splitlines() を割る → 無害化 (2026-07-21)
                "content": content.replace("\u2028", " ").replace("\u2029", " ").strip(),
                "posted_at": dt_attr[:10] if dt_attr else "",
                **metrics,
            })
        except Exception:
            continue  # カード単位 fail-soft
    return results


def collect_query(page, q: str, since: str, until: str, min_likes: int, per_query: int,
                  lang: str | None = None) -> list[dict]:
    """1クエリ収集。スクロール毎に取り込み・id マージ（仮想化による本文喪失の対策）。"""
    url = build_buzz_search_url(q, since, until, min_likes, lang)
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    time.sleep(random.uniform(3.0, 5.0))
    if is_login_wall_url(page.url):
        raise LoginWallError(f"ログイン壁: {page.url}")
    try:
        page.wait_for_selector(TWEET_CARD_SELECTOR, timeout=10_000)
    except Exception:
        return []  # 0件（正常系）
    by_id: dict[str, dict] = {}

    def ingest():
        for c in scrape_cards_rich(page):
            prev = by_id.get(c["id"])
            # 既取得の本文を空で上書きしない（仮想化で後から空になるケース）
            if prev and prev.get("content") and not c.get("content"):
                continue
            by_id[c["id"]] = c

    ingest()
    prev_count = 0
    for _ in range(MAX_SCROLLS):
        if len(by_id) >= per_query:
            break
        cur = page.locator(TWEET_CARD_SELECTOR).count()
        page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        new = _wait_for_new_cards(page, cur)
        ingest()
        if new <= prev_count and new <= cur:
            break
        prev_count = cur
    rows = [r for r in by_id.values() if r["likes"] >= min_likes]
    for r in rows:
        r["query"] = q + (f" lang:{lang}" if lang else "")
    rows.sort(key=lambda r: r["likes"], reverse=True)
    return rows[:per_query]


def append_runlog(out_dir: Path, run_at: str, queries: int, rows: int, quarantined: int,
                   wall_errors: int, outcome: str) -> None:
    """毎実行の結果を1行追記（0件 early-return を含む全経路）。

    「0件実行」と「未実行」を事後区別するための実行証跡（2026-07-26 T10・原観測の不変化）。
    outcome: "ok"（正常書き込み） / "empty"（0件・窓内に閾値超えなし） / "login_wall"（0件で壁停止）
             / "partial_wall"（壁で停止したが途中まで収集できた行がある・"ok"と区別）
             / "crash"（cookieロード失敗・playwright起動失敗等の未捕捉クラッシュ）
    """
    runlog_path = out_dir / "collect_runlog.jsonl"
    entry = {
        "run_at": run_at,
        "queries": queries,
        "rows": rows,
        "quarantined": quarantined,
        "wall_errors": wall_errors,
        "outcome": outcome,
    }
    with open(runlog_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def refetch_content(page, url: str) -> str:
    """空本文の個別ページ再取得（隔離行の救済・少数のみ）。"""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        time.sleep(random.uniform(2.0, 4.0))
        el = page.locator('[data-testid="tweetText"]')
        if el.count():
            return (el.first.inner_text(timeout=3000) or "").strip()
    except Exception:
        pass
    return ""


def apply_syndication_pass(rows: list[dict], empty_cap: int) -> dict:
    """syndication API のみで空本文の救済と DOM本文の翻訳/切断正規化を行う（ブラウザ不要）。

    normalize_master_posts.fetch_syndication / build_norm_fields を再利用する
    （ロジック複製を避ける・ライブ収集の後段にも遡及適用にも同じ関数を流用できる設計）。

    - 空本文行（likes降順・empty_cap 件まで）: payload.article があれば title+preview_text
      を content にし content_source="x_article"（X Articles・本文が物理的に無いツイート）。
      article でなく text が返れば content_source="syndication" で通常本文を復元。
    - 非空行のうち content_source 未設定（＝DOM由来）のもの: build_norm_fields で
      翻訳/切断を判定し、該当すれば syndication 原文を採用（元文は content_preview に退避）。
    """
    stats = {"rescued_article": 0, "rescued_text": 0, "normalized": 0, "unchanged": 0, "failed": 0}
    empties = sorted((r for r in rows if not r.get("content")), key=lambda r: r.get("likes", 0), reverse=True)
    dom_valid = [r for r in rows if r.get("content") and not r.get("content_source")]
    targets = empties[:empty_cap] + dom_valid

    for r in targets:
        status, payload = normalize_master_posts.fetch_syndication(r["id"])
        time.sleep(1.0)  # syndication は無認証だが節度を保つ
        # Critical(2026-07-26 validator指摘): fetch_syndication は JSON妥当な非dict
        # （配列等）を "ok" で素通しすることを実証済み。dict保証をここで強制する
        # （payload.get 呼び出しで例外化 → 呼び出し元 :420 まで伝播し収集全損する不具合の根治）。
        if status != "ok" or not isinstance(payload, dict):
            if not r.get("content"):
                r["rescue_attempted"] = True  # 試みたが失敗＝説明のつく空（cap超過とは区別）
            stats["failed"] += 1
            continue
        if not r.get("content"):
            article = payload.get("article")
            if isinstance(article, dict) and (article.get("title") or article.get("preview_text")):
                title = article.get("title") or ""
                preview = article.get("preview_text") or ""
                r["content"] = f"{title}\n\n{preview}".strip()
                r["content_source"] = "x_article"
                stats["rescued_article"] += 1
            else:
                text = (payload.get("text") or "").strip()
                if text:
                    r["content"] = text
                    r["content_source"] = "syndication"
                    stats["rescued_text"] += 1
                else:
                    r["rescue_attempted"] = True  # 試みたが article/text とも空＝説明のつく空
                    stats["unchanged"] += 1
            continue
        fields = normalize_master_posts.build_norm_fields({"text": r["content"]}, status, payload)
        if fields["text_was_translated"] or fields["text_was_truncated"]:
            r["content_preview"] = r["content"]
            r["content"] = fields["norm_text"]
            r["content_normalized"] = True
            r["norm_lang"] = fields["norm_lang"]
            stats["normalized"] += 1
        else:
            stats["unchanged"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", nargs="+", default=DEFAULT_QUERIES)
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-likes", type=int, default=DEFAULT_MIN_LIKES)
    ap.add_argument("--min-likes-ja", type=int, default=20, help="lang:ja 窓の閾値（日本語相場は低め）")
    ap.add_argument("--per-query", type=int, default=8)
    ap.add_argument("--no-ja-split", action="store_true", help="lang:ja 別窓を無効化")
    ap.add_argument("--refetch-cap", type=int, default=8, help="空本文の個別再取得の上限件数")
    # 2026-08-03: 50→120（7/26 実測は空本文29件に対し救済1件＝3%。cap が律速でない可能性も
    # あるが、上げ幅のコストは syndication 個別取得の回数のみで安全側）。
    ap.add_argument("--syndication-cap", type=int, default=120,
                    help="syndication API での空本文救済の上限件数（無認証・refetch-capとは別枠）")
    ap.add_argument("--profile", default="x_profiles/maaaki")
    ap.add_argument("--output-dir", default="/app/output/grok_twittora")
    args = ap.parse_args()

    now = datetime.now(JST)
    until = args.until or now.strftime("%Y-%m-%d")
    since = args.since or (now - timedelta(days=args.days)).strftime("%Y-%m-%d")

    # タスク表: (query, min_likes, lang)
    tasks: list[tuple[str, int, str | None]] = [(q, args.min_likes, None) for q in args.queries]
    if not args.no_ja_split:
        tasks += [(q, args.min_likes_ja, "ja") for q in JA_SPLIT_QUERIES if q in args.queries]

    # out_dir/run_at はcookieロード・playwright起動より前に確定させる（Critical2 item9・
    # そこで未捕捉クラッシュしても runlog を書けるようにするため）。
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_at = now.isoformat()

    all_rows: list[dict] = []
    wall_errors = 0
    deduped: list[dict] = []
    pw = browser = context = None
    try:
        cookies = fetch_bookmarks.load_cookies(args.profile)
        print("=== X Search Collect (@twittora_・login DOM v2) ===")
        print(f"tasks: {len(tasks)} (global {len(args.queries)} + ja {len(tasks)-len(args.queries)}), "
              f"window: {since}..{until}, min_likes: {args.min_likes}/{args.min_likes_ja}(ja)")

        from playwright.sync_api import sync_playwright

        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(**build_context_kwargs())
            context.add_cookies(cookies)
            page = context.new_page()
            for i, (q, ml, lang) in enumerate(tasks):
                label = f"{q!r}" + (f" [lang:{lang} min:{ml}]" if lang else "")
                print(f"  → searching: {label}")
                try:
                    rows = collect_query(page, q, since, until, ml, args.per_query, lang)
                except LoginWallError as exc:
                    print(f"  ✗ {exc}", file=sys.stderr)
                    wall_errors += 1
                    break  # 壁が出たら即停止（アカウント保護）
                except Exception as exc:
                    print(f"  ✗ {label} 失敗（スキップ）: {type(exc).__name__}: {str(exc)[:80]}", file=sys.stderr)
                    time.sleep(random.uniform(5.0, 10.0))
                    continue
                print(f"    got {len(rows)}")
                all_rows.extend(rows)
                if i < len(tasks) - 1:
                    time.sleep(random.uniform(*QUERY_PACING))

            # id 横断 dedupe（likes の大きい記録を残す）
            by_id: dict[str, dict] = {}
            for r in all_rows:
                prev = by_id.get(r["id"])
                if prev is None or r["likes"] > prev["likes"] or (not prev.get("content") and r.get("content")):
                    if prev and prev.get("content") and not r.get("content"):
                        r["content"] = prev["content"]
                    by_id[r["id"]] = r
            deduped = list(by_id.values())

            # P1: 空本文の隔離と救済（likes 上位のみ・cap つき）
            empties = sorted((r for r in deduped if not r["content"]), key=lambda r: r["likes"], reverse=True)
            refetched = 0
            for r in empties[: args.refetch_cap]:
                text = refetch_content(page, r["url"])
                if text:
                    r["content"] = text
                    r["content_refetched"] = True
                    refetched += 1
            if empties:
                print(f"  空本文: {len(empties)} 件（個別再取得で {refetched} 件救済）")

            # Fix1/Fix4(2026-07-26): syndication API（無認証・cap は refetch-cap と別枠）で
            # DOM再取得後もなお空の行を救済（X Articles対応）し、DOM由来の本文の翻訳/切断も正規化
            try:
                syn_stats = apply_syndication_pass(deduped, args.syndication_cap)
                print(f"  syndication: article救済={syn_stats['rescued_article']} "
                      f"本文救済={syn_stats['rescued_text']} 正規化={syn_stats['normalized']} "
                      f"変化なし={syn_stats['unchanged']} 失敗={syn_stats['failed']}")
            except Exception as exc:
                # Critical(2026-07-26 validator指摘): ここで捕捉しないと外側:434のcrash経路に
                # 落ち、この回の収集行(deduped)が1行も書き出されず全損する。fail-soft継続。
                print(f"  ⚠ syndication救済ステップで例外（収集結果はそのまま書き出す）: "
                      f"{type(exc).__name__}: {str(exc)[:120]}", file=sys.stderr)
        finally:
            for closer in (context, browser):
                try:
                    closer and closer.close()
                except Exception:
                    pass
            try:
                pw and pw.stop()
            except Exception:
                pass
    except Exception as exc:
        # Critical2 item9: cookieロード失敗・playwright起動失敗等の未捕捉クラッシュも
        # runlog に残す（「未実行」と区別する。個別クエリの失敗は上のtry/exceptで既に処理済み）。
        print(f"ERROR: 予期しないクラッシュ: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        append_runlog(out_dir, run_at, len(tasks), 0, 0, wall_errors, "crash")
        return 1

    print(f"\nTotal: {len(all_rows)} fetched, {len(deduped)} unique")
    if not deduped:
        if wall_errors:
            print("ERROR: ログイン壁で停止。Cookie 更新（refresh-x-cookies）を確認", file=sys.stderr)
            append_runlog(out_dir, run_at, len(tasks), 0, 0, wall_errors, "login_wall")
            return 1
        print("0件（窓内に閾値超えなし）。空ファイルは書かない")
        append_runlog(out_dir, run_at, len(tasks), 0, 0, wall_errors, "empty")
        return 0

    # P1: 完全性セルフチェック
    valid = [r for r in deduped if r["content"]]
    quarantined = [r for r in deduped if not r["content"]]
    # ja 判定はクエリ由来を優先（英訳表示問題はアカウント表示言語 ja 化で解消済み 2026-07-19・本判定は保険で維持）
    ja_count = sum(1 for r in valid if "lang:ja" in r.get("query", "") or _JA_RE.search(r["content"]))
    views_cov = sum(1 for r in deduped if r["impressions"] > 0)
    # Fix2改(2026-07-26 validator指摘対応): quarantined行はcontent_sourceを持ち得ない
    # （非空になった時点でquarantinedから外れるため）ので旧ロジックは unexplained==quarantined
    # の恒等式でdeadだった（区別が効かない）。rescue_attempted（apply_syndication_pass が
    # 救済を試みたが syndication 側も空/失敗＝個別ツイートの削除・非公開等で説明がつく）で
    # 区別し、「試みてすらいない」行（cap超過等）だけを本物の DOM 抽出劣化の疑いとして扱う。
    unexplained = [r for r in quarantined if not r.get("rescue_attempted")]
    rescue_failed_count = sum(1 for r in quarantined if r.get("rescue_attempted"))
    x_article_count = sum(1 for r in deduped if r.get("content_source") == "x_article")
    empty_rate = len(unexplained) / len(deduped)
    print(f"  self-check: valid={len(valid)} quarantined={len(quarantined)} unexplained={len(unexplained)} "
          f"rescue_failed={rescue_failed_count} x_article={x_article_count} ja={ja_count} "
          f"views_coverage={views_cov}/{len(deduped)}")
    if empty_rate > 0.2:
        print(f"WARNING: 説明のつかない空本文率 {empty_rate:.0%} が閾値20%超（DOM 抽出の劣化を疑う）", file=sys.stderr)

    captured = now.isoformat()
    for r in deduped:
        r["captured_at"] = captured
        r["collector"] = "x_search_dom_v2"
        if not r["content"]:
            r["content_missing"] = True  # 隔離マーク（分析からは除外・URL 実在）

    today = now.date().isoformat()
    jsonl_path = out_dir / f"grok-twittora-{today}.jsonl"
    if jsonl_path.exists():
        # 同日再実行での原観測消失を防ぐ（T10）。拡張子 .jsonl.bak は
        # vault 側 glob `grok-twittora-*.jsonl` にヒットしないため下流の二重読みなし。
        stamp = now.strftime("%H%M")
        backup_path = out_dir / f"grok-twittora-{today}.pre-{stamp}.jsonl.bak"
        jsonl_path.rename(backup_path)
        print(f"  既存ファイルを退避: {backup_path.name}（同日再実行で原観測を保持）")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for r in sorted(deduped, key=lambda x: x["likes"], reverse=True):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    md_path = out_dir / f"grok-twittora-{today}.md"
    if md_path.exists():
        # jsonl と同じ退避規則（T10フォロー・Critical2 item7）。拡張子 .md.bak は
        # `grok-twittora-*.md` 系 glob（現状は下流に実在しないが将来分の予防的措置）にヒットしない。
        md_backup_path = out_dir / f"grok-twittora-{today}.pre-{now.strftime('%H%M')}.md.bak"
        md_path.rename(md_backup_path)
        print(f"  既存ファイルを退避: {md_backup_path.name}（同日再実行で原観測を保持）")
    lines = [
        "---",
        f"collected_at: {today}",
        f"entries: {len(deduped)}",
        f"valid: {len(valid)}",
        f"quarantined_empty: {len(quarantined)}",
        f"ja_count: {ja_count}",
        f"views_coverage: {views_cov}",
        f"window: {since}..{until}",
        'source: "x_search_collect_twittora.py v2 (login DOM・aria正確指標)"',
        "---",
        "",
        f"# バズ収集 {today}（v2・{len(deduped)}件 / 有効{len(valid)}・ja{ja_count}）",
        "",
    ]
    for r in sorted(valid, key=lambda x: x["likes"], reverse=True):
        er = f" ER{r['likes']/r['impressions']*100:.1f}%" if r["impressions"] else ""
        lines.append(f"- **{r['likes']:,}L** {r['impressions']:,}v{er} @{r['author']} ({r['posted_at']}): {r['content'][:70]} … {r['url']}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✓ {jsonl_path}\n✓ {md_path}")
    # Critical2 item9: 壁が出た後でも一部行を収集できたケースは"ok"と区別する（途中まで正常・途中から壁）
    final_outcome = "partial_wall" if wall_errors else "ok"
    append_runlog(out_dir, run_at, len(tasks), len(deduped), len(quarantined), wall_errors, final_outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
