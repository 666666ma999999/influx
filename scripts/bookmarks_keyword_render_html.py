#!/usr/bin/env python3
"""Xブックマーク→キーワード抽出パイプライン: 評価入力もこのページで完結するWebビューア。

keywords_latest.json（現在世代のactive_clusters/proposals）と台帳（keywords_ledger.jsonl・
世代履歴と最終fetch_run(SUCCESS)時刻の再確認用）を読み、外部依存ゼロの自己完結
HTML（CSS/JSインライン）を決定的に生成する。評価（✅🔁🔍❌ + 提案の✅採用）は
ページ内のボタンでlocalStorageへ蓄積し、「評価を保存」ボタンでJSON（schema:
x-keywords-evals/1）を~/Downloadsへダウンロードする。週次worklist（--downloads-dir）
がそのJSONを回収して台帳のevaluation_batchイベントへ反映する（設計正本:
~/.claude/docs/x-keywords-plan.md §B・§F）。Obsidianノートの評価列は引き続き有効
（併用可）。

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
.eval-header-row{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.badge{font-size:12px;color:var(--text2);background:var(--card);border:1px solid var(--border);border-radius:12px;padding:4px 10px}
.badge.xkw-badge-saved{color:var(--ok);border-color:var(--ok)}
.save-btn{background:var(--ok);color:#fff;border:none;border-radius:14px;padding:6px 14px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap}
.save-btn:hover{opacity:.9}
.prev-eval{font-size:11px;color:var(--text2);margin-top:4px}
.eval-row{display:flex;gap:6px;margin-top:8px}
.eval-btn{background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:4px 10px;font-size:14px;cursor:pointer;line-height:1}
.eval-btn.active{background:var(--accent);border-color:var(--accent)}
.chip-row{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
.chip-btn{background:transparent;color:var(--text2);border:1px solid var(--border);border-radius:10px;padding:3px 8px;font-size:11px;cursor:pointer}
.chip-btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.proposals-section{margin-top:24px}
.proposals-section h2{font-size:16px;margin-bottom:12px}
.proposal-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px 18px;margin-bottom:12px}
.proposal-card h3{font-size:14px;margin-bottom:4px}
.proposal-queries{list-style:none;padding-left:0;margin:8px 0;font-size:12px}
.proposal-queries li{margin-bottom:4px}
.adopt-btn{margin-top:8px;background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:14px;padding:6px 12px;font-size:12px;cursor:pointer}
.adopt-btn.active{background:var(--ok);border-color:var(--ok);color:#fff}
.digest-section{margin-bottom:20px}
.digest-section h2{font-size:16px;margin-bottom:4px}
.digest-intro{font-size:12px;color:var(--text2);margin-bottom:12px}
.digest-item{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px 16px;margin-bottom:10px}
.digest-excerpt{font-size:13px;margin-bottom:6px}
.digest-meta{font-size:12px;color:var(--text2);display:flex;gap:8px;flex-wrap:wrap;align-items:center}
"""

JS = """
document.addEventListener('click', function(e){
  var copyBtn = e.target.closest('.copy-btn');
  if (copyBtn) {
    var text = copyBtn.getAttribute('data-copy') || '';
    navigator.clipboard.writeText(text).then(function(){
      var original = copyBtn.textContent;
      copyBtn.textContent = '\\u2714\\u30b3\\u30d4\\u30fc\\u6e08\\u307f';
      copyBtn.classList.add('copied');
      setTimeout(function(){
        copyBtn.textContent = original;
        copyBtn.classList.remove('copied');
      }, 1500);
    });
    return;
  }

  var evalBtn = e.target.closest('.eval-btn');
  if (evalBtn) {
    var row = evalBtn.closest('.id-row');
    var idType = row.getAttribute('data-id-type');
    var id = row.getAttribute('data-id');
    var mark = evalBtn.getAttribute('data-mark');
    var current = xkwReadState(idType, id);
    if (current && current.mark === mark) {
      xkwWriteState(idType, id, null);
    } else {
      xkwWriteState(idType, id, {mark: mark, note: ''});
    }
    xkwRestoreRowState(row);
    xkwUpdateBadge();
    return;
  }

  var chipBtn = e.target.closest('.chip-btn');
  if (chipBtn) {
    var chipRowEl = chipBtn.closest('.id-row');
    var chipIdType = chipRowEl.getAttribute('data-id-type');
    var chipId = chipRowEl.getAttribute('data-id');
    var chipCurrent = xkwReadState(chipIdType, chipId);
    if (!chipCurrent || !chipCurrent.mark) return;
    var note = chipBtn.getAttribute('data-note');
    var newNote = (chipCurrent.note === note) ? '' : note;
    xkwWriteState(chipIdType, chipId, {mark: chipCurrent.mark, note: newNote});
    xkwRestoreRowState(chipRowEl);
    xkwUpdateBadge();
    return;
  }

  var adoptBtn = e.target.closest('.adopt-btn');
  if (adoptBtn) {
    var pRow = adoptBtn.closest('.id-row');
    var pIdType = pRow.getAttribute('data-id-type');
    var pId = pRow.getAttribute('data-id');
    var pCurrent = xkwReadState(pIdType, pId);
    if (pCurrent && pCurrent.mark === '\\u2705') {
      xkwWriteState(pIdType, pId, null);
    } else {
      xkwWriteState(pIdType, pId, {mark: '\\u2705', note: ''});
    }
    xkwRestoreRowState(pRow);
    xkwUpdateBadge();
    return;
  }

  if (e.target.closest('#xkw-save-btn')) {
    xkwSaveEvals();
    return;
  }
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

// --- 評価UI: localStorageへの蓄積 + JSON書き出し ---------------------------
var XKW_KEY_PREFIX = 'xkw-eval:gen' + XKW_GEN + '-rev' + XKW_REV + ':';
var XKW_CHIP_FOR_MARK = {'\\ud83d\\udd01': true, '\\ud83d\\udd0d': true};

function xkwStorageKey(idType, id) {
  return XKW_KEY_PREFIX + idType + ':' + id;
}

function xkwReadState(idType, id) {
  try {
    var raw = localStorage.getItem(xkwStorageKey(idType, id));
    return raw ? JSON.parse(raw) : null;
  } catch (err) {
    return null;
  }
}

function xkwWriteState(idType, id, state) {
  try {
    var key = xkwStorageKey(idType, id);
    if (!state || !state.mark) {
      localStorage.removeItem(key);
    } else {
      localStorage.setItem(key, JSON.stringify(state));
    }
  } catch (err) { /* localStorage不可時は無音で無視 */ }
}

function xkwEachStoredEntry(fn) {
  try {
    for (var i = 0; i < localStorage.length; i++) {
      var k = localStorage.key(i);
      if (!k || k.indexOf(XKW_KEY_PREFIX) !== 0) continue;
      var rest = k.slice(XKW_KEY_PREFIX.length);
      var sep = rest.indexOf(':');
      if (sep === -1) continue;
      var idType = rest.slice(0, sep);
      var id = rest.slice(sep + 1);
      var state = null;
      try { state = JSON.parse(localStorage.getItem(k)); } catch (err) { state = null; }
      if (!state || !state.mark) continue;
      fn(idType, id, state);
    }
  } catch (err) { /* localStorage列挙不可時は0件扱い */ }
}

function xkwCountPending() {
  var n = 0;
  xkwEachStoredEntry(function(){ n++; });
  return n;
}

function xkwUpdateBadge() {
  var badge = document.getElementById('xkw-badge');
  if (!badge) return;
  badge.textContent = '\\u672a\\u4fdd\\u5b58\\u306e\\u8a55\\u4fa1 ' + xkwCountPending() + ' \\u4ef6';
  badge.classList.remove('xkw-badge-saved');
}

function xkwUpdateChipVisibility(row, mark, note) {
  var chipRow = row.querySelector('.chip-row');
  if (!chipRow) return;
  if (XKW_CHIP_FOR_MARK[mark]) {
    chipRow.style.display = '';
    chipRow.querySelectorAll('.chip-btn').forEach(function(cb){
      var isForMark = cb.getAttribute('data-for-mark') === mark;
      cb.style.display = isForMark ? '' : 'none';
      cb.classList.toggle('active', isForMark && note && cb.getAttribute('data-note') === note);
    });
  } else {
    chipRow.style.display = 'none';
  }
}

function xkwRestoreRowState(row) {
  var idType = row.getAttribute('data-id-type');
  var id = row.getAttribute('data-id');
  var state = xkwReadState(idType, id);
  var mark = state ? state.mark : null;
  var note = state ? (state.note || '') : '';
  row.querySelectorAll('.eval-btn').forEach(function(btn){
    btn.classList.toggle('active', btn.getAttribute('data-mark') === mark);
  });
  var adoptBtn = row.querySelector('.adopt-btn');
  if (adoptBtn) {
    adoptBtn.classList.toggle('active', mark === '\\u2705');
  }
  xkwUpdateChipVisibility(row, mark, note);
}

function xkwSaveEvals() {
  var evals = [];
  xkwEachStoredEntry(function(idType, id, state){
    evals.push({id: id, id_type: idType, mark: state.mark, note: state.note || ''});
  });
  var payload = {
    schema: 'x-keywords-evals/1',
    exported_at: new Date().toISOString(),
    source: {generation: XKW_GEN, revision: XKW_REV},
    evals: evals
  };
  var blob = new Blob([JSON.stringify(payload, null, 2)], {type: 'application/json'});
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = 'x-keywords-evals-' + XKW_GEN + '-' + Math.floor(Date.now() / 1000) + '.json';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);

  var badge = document.getElementById('xkw-badge');
  if (badge) {
    badge.textContent = '\\u66f8\\u304d\\u51fa\\u3057\\u6e08\\u307f';
    badge.classList.add('xkw-badge-saved');
  }
}

document.querySelectorAll('.id-row').forEach(xkwRestoreRowState);
xkwUpdateBadge();
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


_EVAL_MARKS = ("✅", "🔁", "🔍", "❌")
# 🔁/🔍選択時のみ表示する理由チップ（固定語彙・自由入力なし）。
_EVAL_CHIPS = (
    ("🔁", "min_favesを上げる"),
    ("🔁", "絞り込み語を足す"),
    ("🔍", "min_favesを下げる"),
    ("🔍", "語を減らす"),
)


def render_eval_buttons() -> str:
    """評価ボタン4つ + 🔁/🔍用の理由チップ行（初期は非表示・JSがdata-idの状態に応じて出し分ける）。"""
    btns = "".join(
        f'<button type="button" class="eval-btn" data-mark="{esc(m)}">{esc(m)}</button>'
        for m in _EVAL_MARKS
    )
    chips = "".join(
        f'<button type="button" class="chip-btn" data-for-mark="{esc(m)}" data-note="{esc(n)}">{esc(n)}</button>'
        for m, n in _EVAL_CHIPS
    )
    return f"""
      <div class="eval-row">{btns}</div>
      <div class="chip-row" style="display:none">{chips}</div>"""


def render_prev_eval(eval_history: list) -> str:
    """台帳に記録済みの前回評価（eval_history最新）を表示する。"""
    if not eval_history:
        return ""
    last = eval_history[-1]
    mark = last.get("mark", "")
    at = (last.get("at") or "")[:10]
    at_suffix = f"（{esc(at)}）" if at else ""
    return f'<div class="prev-eval">前回評価: {esc(mark)}{at_suffix}</div>'


def render_query_row(query: dict) -> str:
    q = query.get("q", "")
    q_simple = query.get("q_simple", "")
    intent = query.get("intent", "")
    qid = query.get("query_id", "")
    search_url = x_search_url(q)
    return f"""
    <div class="query-row id-row" data-id-type="qid" data-id="{esc(qid)}">
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
      {render_prev_eval(query.get("eval_history") or [])}
      {render_eval_buttons()}
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


def render_proposal_card(proposal: dict) -> str:
    """提案（pid）1件のカード。評価ボタンではなく「✅採用」単独トグルのみ持つ
    （設計正本の提案承認セマンティクスは✅/❌の2値だが、Webページでは採用チェックのみ
    露出する。却下は現状Obsidianノート側の役割のまま）。"""
    pid = proposal.get("proposal_id", "")
    name = proposal.get("name_ja", "")
    why = proposal.get("why", "")
    queries = proposal.get("queries", [])
    evidence_urls = proposal.get("evidence_urls", [])
    query_items = "".join(
        f'<li><code class="query-text">{esc(q.get("q", ""))}</code></li>' for q in queries
    )
    return f"""
  <div class="proposal-card id-row" data-id-type="pid" data-id="{esc(pid)}">
    <h3>{esc(name)}</h3>
    <div class="cluster-why">{esc(why)}</div>
    <ul class="proposal-queries">{query_items}</ul>
    <div class="evidence-count">根拠: {len(evidence_urls)}件</div>
    <button type="button" class="adopt-btn">✅ 採用</button>
  </div>"""


def render_proposals_section(proposals: list) -> str:
    if not proposals:
        return ""
    cards = "".join(render_proposal_card(p) for p in proposals)
    return f"""
  <div class="proposals-section">
    <h2>提案（✅採用で次回世代に反映）</h2>
    {cards}
  </div>"""


def load_latest_digest(ledger_path) -> dict | None:
    """台帳replayから最新のdigestイベント（latest_digest）を返す。無ければNone。"""
    try:
        events = common.read_ledger(ledger_path)
    except common.LedgerCorruption:
        events = []
    if not events:
        return None
    return common.replay(events).get("latest_digest")


def render_digest_item(item: dict, cluster_names: dict) -> str:
    excerpt = esc(item.get("excerpt", ""))
    author = esc(item.get("author", ""))
    likes = esc(item.get("likes", 0))
    url = item.get("url", "")
    cluster_id = item.get("cluster_id")
    cluster_name = cluster_names.get(cluster_id) or cluster_id or ""
    if _is_safe_evidence_href(url):
        link = f'<a class="x-link" href="{esc(url)}" target="_blank" rel="noopener">Xで見る &#8599;</a>'
    else:
        link = esc(url)
    return f"""
    <div class="digest-item">
      <div class="digest-excerpt">{excerpt}</div>
      <div class="digest-meta"><span>@{author}</span><span>&#10084;&#65039;{likes}</span><span>{esc(cluster_name)}</span>{link}</div>
    </div>"""


def render_digest_section(latest_digest: dict | None, active_clusters: list) -> str:
    """latest_digestが無い、またはitemsが空ならセクション自体を出さない。"""
    if not latest_digest:
        return ""
    items = latest_digest.get("items", [])
    if not items:
        return ""
    cluster_names = {c.get("cluster_id"): c.get("name", "") for c in active_clusters}
    published_at = fmt_dt(latest_digest.get("ts"))
    item_rows = "".join(render_digest_item(item, cluster_names) for item in items)
    return f"""
  <div class="digest-section">
    <h2>&#128235; 今週の候補記事（{esc(published_at)} 掲載）</h2>
    <div class="digest-intro">気になったらそのままブックマーク &#8594; 次回から的中として学習されます</div>
    {item_rows}
  </div>"""


def render_page(latest: dict, meta: dict, latest_digest: dict | None = None) -> str:
    active_clusters = latest.get("active_clusters", [])
    proposals = latest.get("proposals", [])
    total_queries = sum(len(c.get("queries", [])) for c in active_clusters)
    cards_html = "".join(render_cluster_card(c) for c in active_clusters)
    if not active_clusters:
        cards_html = '<div class="empty-msg">アクティブなクラスタがありません。</div>'
    proposals_html = render_proposals_section(proposals)
    digest_html = render_digest_section(latest_digest, active_clusters)

    gen = meta.get("generation")
    rev = meta.get("revision") or 0
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
    <div class="eval-header-row">
      <span id="xkw-badge" class="badge">未保存の評価 0 件</span>
      <button type="button" id="xkw-save-btn" class="save-btn">評価を保存</button>
    </div>
    <input type="text" class="search" id="search" placeholder="クラスタ名・クエリ・キーワードで絞り込み">
  </div>
</div>
<div class="main">{digest_html}{cards_html}{proposals_html}</div>
<div class="footer">
  <p>このページのボタンで評価 → 保存 → 週次で自動反映（Obsidian の評価列も併用可）</p>
</div>
<script>var XKW_GEN={json.dumps(gen)};var XKW_REV={json.dumps(rev)};</script>
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


def render_to_file(latest_path=DEFAULT_LATEST, ledger_path=DEFAULT_LEDGER, out_path=DEFAULT_OUT) -> int:
    """latest.json/台帳からHTMLを再生成する（CLI以外＝digest_apply.py等からのimport呼び出し用）。"""
    latest = load_latest(latest_path)
    if latest is None:
        atomic_write_text(out_path, render_missing_page())
        return 0

    meta = resolve_meta(latest, ledger_path)
    latest_digest = load_latest_digest(ledger_path)
    atomic_write_text(out_path, render_page(latest, meta, latest_digest))
    return 0


def main() -> int:
    args = parse_args()
    return render_to_file(args.latest, args.ledger, args.out)


if __name__ == "__main__":
    sys.exit(main())
