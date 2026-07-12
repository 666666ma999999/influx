#!/usr/bin/env python3
"""Xブックマーク→キーワード抽出パイプライン: 閲覧・コピー専用Webページレンダラー。

keywords_latest.json（現在世代のactive_clusters）と台帳（keywords_ledger.jsonl・
世代履歴と最終fetch_run(SUCCESS)時刻の再確認用）を読み、外部依存ゼロの自己完結
HTML（CSS/JSインライン）を決定的に生成する。評価入力はOpen Vaultノート側の役割
のため、このページにはボタンを置かない（閲覧・検索・コピー・X直リンクのみ）。

デザインはoutput/bookmarks_viewer.html（ダークX風テーマ・sticky header・検索
ボックス）を踏襲する。

使い方（influxディレクトリで実行）:
    python3 scripts/bookmarks_keyword_render_html.py
    python3 scripts/bookmarks_keyword_render_html.py \\
        --latest /tmp/latest.json --ledger /tmp/ledger.jsonl --out /tmp/out.html

latest.jsonが存在しない場合は「未生成」の最小ページを書き出してexit 0する。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bookmarks_keyword_common as common  # noqa: E402

DEFAULT_LATEST = "output/bookmarks/keywords_latest.json"
DEFAULT_LEDGER = "output/bookmarks/keywords_ledger.jsonl"
DEFAULT_OUT = "output/bookmarks/x_keywords.html"

OBSIDIAN_NOTE_URI = (
    "obsidian://open?vault=Obsidian%20Vault&file=02_Ai%2Finflux%2Finflux_x_search_keywords.md"
)

EVIDENCE_DISPLAY_LIMIT = 3

CSS = """
:root{--bg:#15202b;--card:#192734;--border:#38444d;--text:#e1e8ed;--text2:#8899a6;--accent:#1da1f2;--ok:#17bf63}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;line-height:1.5}
.header{position:sticky;top:0;z-index:100;background:rgba(21,32,43,.95);backdrop-filter:blur(12px);border-bottom:1px solid var(--border);padding:12px 20px}
.header-inner{max-width:760px;margin:0 auto}
.header h1{font-size:18px;font-weight:700;margin-bottom:4px}
.header h1 span{color:var(--accent)}
.meta{font-size:12px;color:var(--text2);margin-bottom:8px}
.search{background:var(--card);border:1px solid var(--border);border-radius:20px;color:var(--text);padding:6px 14px;font-size:13px;width:100%;outline:none;transition:border-color .2s}
.search:focus{border-color:var(--accent)}
.main{max-width:760px;margin:0 auto;padding:16px 12px 60px}
.cluster-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px 18px;margin-bottom:16px}
.cluster-card h2{font-size:16px;margin-bottom:4px}
.cluster-why{font-size:13px;color:var(--text2);margin-bottom:12px}
.query-row{border-top:1px solid var(--border);padding:10px 0}
.query-row:first-of-type{border-top:none}
.query-main{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.query-text{flex:1;min-width:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;background:#0f1a23;border:1px solid var(--border);border-radius:6px;padding:6px 8px;word-break:break-all}
.query-simple-row{display:flex;align-items:center;gap:8px;margin-top:6px;font-size:12px;color:var(--text2)}
.query-simple-text{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;background:#0f1a23;border:1px solid var(--border);border-radius:6px;padding:3px 6px;color:var(--text)}
.query-intent{font-size:12px;color:var(--text2);margin-top:6px}
.copy-btn{background:var(--accent);color:#fff;border:none;border-radius:14px;padding:6px 12px;font-size:12px;cursor:pointer;white-space:nowrap;transition:background .15s}
.copy-btn:hover{background:#1a91da}
.copy-btn.copied{background:var(--ok)}
.copy-btn-sm{padding:3px 8px;font-size:11px}
.x-link{color:var(--accent);font-size:12px;text-decoration:none;white-space:nowrap}
.x-link:hover{text-decoration:underline}
.evidence{margin-top:12px;font-size:12px;color:var(--text2)}
.evidence-count{margin-bottom:4px}
.evidence-list{list-style:none;padding-left:0}
.evidence-list li{margin-bottom:2px}
.evidence-list a{color:var(--accent);text-decoration:none;word-break:break-all}
.evidence-list a:hover{text-decoration:underline}
.empty-msg{text-align:center;color:var(--text2);padding:60px 20px}
.footer{max-width:760px;margin:0 auto;padding:20px 12px 40px;font-size:12px;color:var(--text2);text-align:center;border-top:1px solid var(--border)}
.footer a{color:var(--accent);text-decoration:none}
.footer a:hover{text-decoration:underline}
"""

JS = """
document.addEventListener('click', function(e){
  var btn = e.target.closest('.copy-btn');
  if(!btn) return;
  var text = btn.getAttribute('data-copy') || '';
  navigator.clipboard.writeText(text).then(function(){
    var original = btn.textContent;
    btn.textContent = '\\u2714\\u30b3\\u30d4\\u30fc\\u6e08\\u307f';
    btn.classList.add('copied');
    setTimeout(function(){
      btn.textContent = original;
      btn.classList.remove('copied');
    }, 1500);
  });
});

var searchBox = document.getElementById('search');
if (searchBox) {
  var cards = document.querySelectorAll('.cluster-card');
  searchBox.addEventListener('input', function(){
    var q = searchBox.value.trim().toLowerCase();
    cards.forEach(function(card){
      var haystack = card.getAttribute('data-search') || '';
      card.style.display = (!q || haystack.indexOf(q) !== -1) ? '' : 'none';
    });
  });
}
"""


def esc(value) -> str:
    """全てのユーザー/LLM由来テキストに適用する共通エスケープ。属性値としても安全。"""
    return html.escape(str(value) if value is not None else "", quote=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Xキーワード集の閲覧・コピー専用HTMLページを生成する"
    )
    parser.add_argument("--latest", default=DEFAULT_LATEST, help="keywords_latest.json のパス")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER, help="keywords_ledger.jsonl のパス")
    parser.add_argument("--out", default=DEFAULT_OUT, help="出力HTMLのパス")
    return parser.parse_args()


def load_latest(path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_meta(latest: dict, ledger_path) -> dict:
    """世代/最終fetch成功時刻を、可能なら台帳replayで最新化し、latest.json自身の値へフォールバックする。"""
    meta = {
        "generation": latest.get("generation"),
        "revision": latest.get("revision", 0),
        "reason": latest.get("generation_reason"),
        "generated_at": latest.get("generated_at"),
        "last_fetch_success_at": latest.get("last_fetch_success_at"),
    }

    try:
        events = common.read_ledger(ledger_path)
    except common.LedgerCorruption:
        events = []

    if events:
        state = common.replay(events)
        last_gen = state.get("last_generation")
        if last_gen:
            meta["generation"] = last_gen.get("generation", meta["generation"])
            meta["revision"] = last_gen.get("revision", meta["revision"])
            meta["reason"] = last_gen.get("reason", meta["reason"])
            meta["generated_at"] = last_gen.get("at", meta["generated_at"])
        if state.get("last_fetch_success_at"):
            meta["last_fetch_success_at"] = state["last_fetch_success_at"]

    return meta


def fmt_dt(iso: str | None) -> str:
    """ISO8601文字列を'YYYY-MM-DD HH:MM UTC'表示へ短縮する。パース不能/Noneはそのまま表示用文言。"""
    if not iso:
        return "未実行"
    text = str(iso)
    if len(text) >= 16:
        return f"{text[:10]} {text[11:16]} UTC"
    return text


def x_search_url(query: str) -> str:
    return f"https://x.com/search?q={quote(query, safe='')}&f=live"


def render_query_row(query: dict) -> str:
    q = query.get("q", "")
    q_simple = query.get("q_simple", "")
    intent = query.get("intent", "")
    search_url = x_search_url(q)
    return f"""
    <div class="query-row">
      <div class="query-main">
        <code class="query-text">{esc(q)}</code>
        <button type="button" class="copy-btn" data-copy="{esc(q)}">コピー</button>
        <a class="x-link" href="{esc(search_url)}" target="_blank" rel="noopener">Xで開く &#8599;</a>
      </div>
      <div class="query-simple-row">
        <span>簡易版:</span>
        <code class="query-simple-text">{esc(q_simple)}</code>
        <button type="button" class="copy-btn copy-btn-sm" data-copy="{esc(q_simple)}">コピー</button>
      </div>
      <div class="query-intent">{esc(intent)}</div>
    </div>"""


_SAFE_EVIDENCE_HREF_PREFIXES = ("https://x.com/", "https://twitter.com/")


def _is_safe_evidence_href(url) -> bool:
    """evidence_urlsはLLM生成のクラスタデータ由来であり、台帳破損や検証バグでも
    危険スキームがhrefに混入しないよう、render層でも許可プレフィックスを強制する(P1-4)。"""
    return isinstance(url, str) and url.startswith(_SAFE_EVIDENCE_HREF_PREFIXES)


def render_evidence(evidence_urls: list) -> str:
    count = len(evidence_urls)
    cells = []
    for u in evidence_urls[:EVIDENCE_DISPLAY_LIMIT]:
        if _is_safe_evidence_href(u):
            cells.append(f'<li><a href="{esc(u)}" target="_blank" rel="noopener">{esc(u)}</a></li>')
        else:
            cells.append(f"<li>{esc(u)}</li>")
    items = "".join(cells)
    return f"""
    <div class="evidence">
      <div class="evidence-count">根拠: {count}件</div>
      <ul class="evidence-list">{items}</ul>
    </div>"""


def build_search_blob(cluster: dict) -> str:
    parts = [cluster.get("name", ""), cluster.get("why", "")]
    for kw in cluster.get("keywords", []):
        parts.append(kw.get("keyword", ""))
    for q in cluster.get("queries", []):
        parts.append(q.get("q", ""))
        parts.append(q.get("q_simple", ""))
        parts.append(q.get("intent", ""))
    return " ".join(p for p in parts if p).lower()


def render_cluster_card(cluster: dict) -> str:
    name = cluster.get("name", "")
    why = cluster.get("why", "")
    queries = cluster.get("queries", [])
    evidence_urls = cluster.get("evidence_urls", [])
    search_blob = build_search_blob(cluster)
    query_rows = "".join(render_query_row(q) for q in queries)
    return f"""
  <div class="cluster-card" data-search="{esc(search_blob)}">
    <h2>{esc(name)}</h2>
    <div class="cluster-why">{esc(why)}</div>
    {query_rows}
    {render_evidence(evidence_urls)}
  </div>"""


def render_page(latest: dict, meta: dict) -> str:
    active_clusters = latest.get("active_clusters", [])
    total_queries = sum(len(c.get("queries", [])) for c in active_clusters)
    cards_html = "".join(render_cluster_card(c) for c in active_clusters)
    if not active_clusters:
        cards_html = '<div class="empty-msg">アクティブなクラスタがありません。</div>'

    gen = meta.get("generation")
    rev = meta.get("revision")
    reason = meta.get("reason") or ""
    generated_at = fmt_dt(meta.get("generated_at"))
    last_fetch = fmt_dt(meta.get("last_fetch_success_at"))

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>X検索キーワード集</title>
<style>{CSS}</style>
</head>
<body>
<div class="header">
  <div class="header-inner">
    <h1>X検索キーワード集 <span>{len(active_clusters)}クラスタ / {total_queries}クエリ</span></h1>
    <div class="meta">世代 gen{esc(gen)} rev{esc(rev)}（{esc(reason)}）｜生成: {esc(generated_at)}｜最終fetch成功: {esc(last_fetch)}</div>
    <input type="text" class="search" id="search" placeholder="クラスタ名・クエリ・キーワードで絞り込み">
  </div>
</div>
<div class="main">{cards_html}</div>
<div class="footer">
  <p>✅ 評価は Obsidian ノートで行います: <a href="{esc(OBSIDIAN_NOTE_URI)}">ノートを開く</a></p>
</div>
<script>{JS}</script>
</body>
</html>
"""


def render_missing_page() -> str:
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>X検索キーワード集</title>
<style>{CSS}</style>
</head>
<body>
<div class="header">
  <div class="header-inner">
    <h1>X検索キーワード集</h1>
  </div>
</div>
<div class="main">
  <div class="empty-msg">まだキーワード集が生成されていません。</div>
</div>
<div class="footer">
  <p>✅ 評価は Obsidian ノートで行います: <a href="{esc(OBSIDIAN_NOTE_URI)}">ノートを開く</a></p>
</div>
</body>
</html>
"""


def atomic_write_text(path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def main() -> int:
    args = parse_args()
    latest = load_latest(args.latest)
    if latest is None:
        atomic_write_text(args.out, render_missing_page())
        return 0

    meta = resolve_meta(latest, args.ledger)
    atomic_write_text(args.out, render_page(latest, meta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
