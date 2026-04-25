"""Gold Set 人手ラベル付け用 HTML UI を生成する（plan.md M2 着手ゲート T2.0）。

候補 (`data/gold_set/candidates.jsonl`) をブラウザで 1 件ずつ読み、7 カテゴリを
checkbox / キーボードショートカット (1-7) で選択する。完了時に
`gold_set.jsonl` を Blob ダウンロードでローカル保存する。

中立性ルール:
    - LLM 推測 (`answer_key.jsonl`) は HTML に埋め込まない。
    - `candidates.jsonl` から `llm_categories` / `categories` / `category_count` 等
      LLM 由来フィールドを念のため除去してから埋め込む。

Usage:
    python3 scripts/build_gold_set_labeler.py
    # → output/label_gold_set.html
    # ブラウザで開いてラベル付け → ダウンロードした gold_set.jsonl を
    # data/gold_set/ に配置
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = PROJECT_ROOT / "data" / "gold_set" / "candidates.jsonl"
OUTPUT = PROJECT_ROOT / "output" / "label_gold_set.html"

# README の labels ルールと一致させる
CATEGORIES = [
    ("recommended_assets", "オススメしている資産・セクター"),
    ("purchased_assets", "個人で売買している資産"),
    ("ipo", "申し込んだ IPO"),
    ("market_trend", "市況トレンドに関する見解"),
    ("bullish_assets", "高騰している資産"),
    ("bearish_assets", "下落している資産"),
    ("warning_signals", "警戒すべき動き・逆指標シグナル"),
]

# 中立性: LLM 由来フィールドを取り除く（万が一 candidates に残っていた場合の防衛）
_LLM_LEAK_FIELDS = {
    "llm_categories", "llm_reasoning", "llm_confidence",
    "categories", "category_details", "category_count",
    "sampled_from_category",
}


def _sanitize(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in rec.items() if k not in _LLM_LEAK_FIELDS}


def _load_candidates() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with CANDIDATES.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(_sanitize(json.loads(line)))
    return rows


def _embed_safe(obj: Any) -> str:
    # `<script>` 直下に JSON リテラルを書くと、テキストに `</script>` が含まれた場合にタグが閉じる。
    # HTML パーサが `</` で反応するため、JSON のバックスラッシュでエスケープしておく。
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def main() -> int:
    if not CANDIDATES.exists():
        print(f"候補ファイルなし: {CANDIDATES}")
        return 1
    candidates = _load_candidates()
    data_json = _embed_safe(candidates)
    categories_json = _embed_safe(CATEGORIES)
    # 候補セットの fingerprint: news_id 集合のハッシュ。差し替え時に localStorage の旧 state を破棄するキー。
    news_ids = [c.get("news_id", "") for c in candidates]
    fingerprint = hashlib.sha256(
        "\n".join(sorted(str(n) for n in news_ids)).encode("utf-8")
    ).hexdigest()[:12]

    html = (
        HTML_TEMPLATE
        .replace("__DATA__", data_json)
        .replace("__CATEGORIES__", categories_json)
        .replace("__FINGERPRINT__", fingerprint)
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html, encoding="utf-8")

    print(f"書き出し: {OUTPUT}")
    print(f"候補数:  {len(candidates)}")
    print(f"ブラウザで開く: open {OUTPUT}")
    print("完了時にダウンロードされる gold_set.jsonl を data/gold_set/ に配置してください")
    return 0


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<title>Gold Set Labeler</title>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Yu Gothic", sans-serif;
    background: #0f172a; color: #e2e8f0;
    margin: 0; padding: 0;
  }
  header {
    background: #1e293b; padding: 12px 24px;
    display: flex; align-items: center; justify-content: space-between;
    border-bottom: 1px solid #334155;
    position: sticky; top: 0; z-index: 10;
  }
  h1 { font-size: 16px; margin: 0; font-weight: 600; }
  .progress-wrap { flex: 1; margin: 0 24px; max-width: 400px; }
  .progress-bar {
    background: #334155; border-radius: 4px; height: 10px; overflow: hidden;
  }
  .progress-fill {
    background: linear-gradient(90deg, #3b82f6, #06b6d4);
    height: 100%; transition: width 0.2s ease;
  }
  .progress-text { font-size: 12px; color: #94a3b8; margin-top: 4px; text-align: center; }
  .labeler-input {
    background: #0f172a; color: #e2e8f0; border: 1px solid #475569;
    border-radius: 4px; padding: 4px 8px; font-size: 13px;
  }
  main { padding: 24px 24px 96px; max-width: 800px; margin: 0 auto; }
  .card {
    background: #1e293b; border: 1px solid #334155; border-radius: 8px;
    padding: 24px; margin-bottom: 16px;
  }
  .meta { font-size: 12px; color: #94a3b8; margin-bottom: 12px; }
  .meta a { color: #60a5fa; text-decoration: none; }
  .meta a:hover { text-decoration: underline; }
  .tweet-text {
    font-size: 16px; line-height: 1.7; white-space: pre-wrap; word-break: break-word;
    background: #0f172a; border: 1px solid #334155; border-radius: 6px;
    padding: 16px; margin-bottom: 16px; color: #f1f5f9;
  }
  .categories { display: flex; flex-direction: column; gap: 8px; }
  .cat-label {
    display: flex; align-items: center; gap: 12px; cursor: pointer;
    padding: 10px 14px; border: 1px solid #334155; border-radius: 6px;
    transition: all 0.1s ease; user-select: none;
  }
  .cat-label:hover { border-color: #64748b; background: #263244; }
  .cat-label.checked { border-color: #3b82f6; background: #1e3a5f; }
  .cat-key {
    display: inline-block; width: 24px; height: 24px; line-height: 24px;
    text-align: center; background: #334155; border-radius: 4px;
    font-size: 12px; font-weight: 700; color: #cbd5e1;
  }
  .cat-label.checked .cat-key { background: #3b82f6; color: #fff; }
  .cat-label input { margin: 0; }
  .cat-key-en { font-family: monospace; font-size: 11px; color: #64748b; margin-left: auto; }
  /* button#none-btn: cat-label を継承するため background/font/text-align を上書きしブラウザ既定スタイルを潰す。 */
  button.cat-none {
    width: 100%; background: transparent; color: #cbd5e1;
    border-color: #475569; font: inherit; text-align: left;
  }
  button.cat-none:hover { border-color: #64748b; background: #263244; }
  button.cat-none.active { border-color: #f59e0b; background: #422006; color: #fed7aa; }
  button.cat-none.active .cat-key { background: #f59e0b; color: #fff; }
  .notes {
    width: 100%; background: #0f172a; color: #e2e8f0;
    border: 1px solid #334155; border-radius: 6px;
    padding: 10px; font-size: 14px; margin-top: 12px; resize: vertical;
    font-family: inherit;
  }
  .nav {
    display: flex; gap: 12px; justify-content: space-between; margin-top: 16px;
  }
  button {
    background: #3b82f6; color: white; border: none;
    border-radius: 6px; padding: 10px 20px; font-size: 14px; font-weight: 600;
    cursor: pointer; transition: background 0.1s;
  }
  button:hover { background: #2563eb; }
  button.secondary { background: #475569; }
  button.secondary:hover { background: #64748b; }
  button.danger { background: #dc2626; }
  button.danger:hover { background: #b91c1c; }
  button:disabled { background: #334155; cursor: not-allowed; color: #64748b; }
  /* A11y: キーボードフォーカス可視性 */
  button:focus-visible, input:focus-visible, textarea:focus-visible,
  a:focus-visible, label:focus-visible {
    outline: 2px solid #3b82f6; outline-offset: 2px;
  }
  .footer {
    background: #1e293b; border-top: 1px solid #334155;
    padding: 12px 24px; font-size: 12px; color: #94a3b8;
    position: sticky; bottom: 0; display: flex; gap: 24px; flex-wrap: wrap;
  }
  .shortcut { font-family: monospace; background: #334155; padding: 2px 6px; border-radius: 3px; }
  .completed-banner {
    background: #065f46; border: 1px solid #10b981;
    padding: 16px; border-radius: 8px; margin-bottom: 16px;
    display: none;
  }
  .completed-banner.visible { display: block; }
</style>
</head>
<body>

<header>
  <h1>Gold Set Labeler</h1>
  <div class="progress-wrap">
    <!-- A11y: 進捗バーは role=progressbar + aria-valuenow で読み上げ対応 -->
    <div class="progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" id="progress-bar">
      <div class="progress-fill" id="progress-fill"></div>
    </div>
    <div class="progress-text" id="progress-text" aria-live="polite">0 / 0</div>
  </div>
  <div>
    <label for="labeler-input" style="font-size: 12px; color: #94a3b8; margin-right: 8px;">labeler:</label>
    <input type="text" class="labeler-input" id="labeler-input" placeholder="masaaki">
  </div>
</header>

<main>
  <div class="completed-banner" id="completed-banner" role="status" aria-live="polite">
    全件ラベル付け完了。<code>gold_set.jsonl</code> をダウンロードして <code>data/gold_set/</code> に配置してください。
  </div>
  <div class="card" id="card">
    <div class="meta" id="meta"></div>
    <div class="tweet-text" id="text"></div>
    <div class="categories" id="categories"></div>
    <button type="button" class="cat-label cat-none" id="none-btn" style="margin-top: 8px;">
      <span class="cat-key">0</span>
      <span>どれにも該当しない（notes に理由必須）</span>
      <span class="cat-key-en">none</span>
    </button>
    <label for="notes" class="sr-only" style="position:absolute;left:-9999px;">メモ</label>
    <textarea class="notes" id="notes" placeholder="メモ（「どれにも該当しない」選択時は理由必須）" rows="2"></textarea>
    <div class="nav">
      <button class="secondary" id="prev-btn">← 戻る (Shift+Enter)</button>
      <span style="align-self: center; color: #64748b; font-size: 12px;" id="status-hint" aria-live="polite"></span>
      <button id="next-btn">次へ → (Enter)</button>
    </div>
  </div>
  <div class="nav">
    <button class="secondary" id="download-btn">⬇ gold_set.jsonl ダウンロード</button>
    <button class="danger" id="reset-btn" style="margin-left: auto;">進捗リセット</button>
  </div>
</main>

<footer class="footer">
  <div><span class="shortcut">1-7</span> カテゴリトグル</div>
  <div><span class="shortcut">0</span> どれにも該当しない（notes 必須）</div>
  <div><span class="shortcut">Enter</span> 次へ（ラベル+Notes 空なら警告）</div>
  <div><span class="shortcut">Shift+Enter</span> 戻る</div>
  <div>進捗は自動で localStorage に保存されます</div>
</footer>

<script>
const CANDIDATES = __DATA__;
const CATEGORIES = __CATEGORIES__;
// 候補セット fingerprint を key に含めることで、candidates.jsonl 差し替え時に旧 state を自動無効化。
const FINGERPRINT = "__FINGERPRINT__";
const STORAGE_KEY = "gold_set_labels_" + FINGERPRINT;
const LABELER_KEY = "gold_set_labeler";

let state = loadState();
let idx = 0;

function loadState() {
  // 現在の CANDIDATES と突合せ、一致した news_id の進捗だけ引き継ぐ。
  // 壊れた JSON は自動削除して fresh に戻す（次回以降の警告無限ループを避ける）。
  let stored = {};
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) stored = JSON.parse(raw);
    if (!stored || typeof stored !== "object") stored = {};
  } catch (e) {
    console.warn("corrupt localStorage, resetting", e);
    try { localStorage.removeItem(STORAGE_KEY); } catch (_) {}
    stored = {};
  }
  const fresh = {};
  for (const c of CANDIDATES) {
    const prev = stored[c.news_id];
    if (prev && typeof prev === "object") {
      fresh[c.news_id] = {
        labels: Array.isArray(prev.labels) ? prev.labels.slice() : [],
        notes: typeof prev.notes === "string" ? prev.notes : "",
        labeled_at: typeof prev.labeled_at === "string" ? prev.labeled_at : null,
        labeler: typeof prev.labeler === "string" ? prev.labeler : undefined,
      };
    } else {
      fresh[c.news_id] = { labels: [], notes: "", labeled_at: null };
    }
  }
  return fresh;
}

function saveState() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); }
  catch (e) { console.warn("save failed", e); }
}

function getLabeler() {
  return document.getElementById("labeler-input").value.trim() || "anonymous";
}

function setLabeler(v) {
  document.getElementById("labeler-input").value = v;
  localStorage.setItem(LABELER_KEY, v);
}

function render() {
  // 候補 0 件: UI を空状態に固定して早期 return（null 参照クラッシュ防止）。
  if (CANDIDATES.length === 0) {
    document.getElementById("meta").textContent = "候補 0 件";
    document.getElementById("text").textContent = "data/gold_set/candidates.jsonl を先に生成してください。";
    document.getElementById("categories").replaceChildren();
    document.getElementById("prev-btn").disabled = true;
    document.getElementById("next-btn").disabled = true;
    document.getElementById("download-btn").disabled = true;
    document.getElementById("progress-text").textContent = "0 / 0";
    document.getElementById("progress-fill").style.width = "0%";
    return;
  }
  const cand = CANDIDATES[idx];
  const rec = state[cand.news_id];

  // meta + text
  const url = cand.tweet_url || "";
  // URL スキーム検証: javascript: 等を弾き、http(s) のみリンクとして描画（HTML エスケープだけでは防げない）。
  let safeUrl = "";
  if (url) {
    try {
      const u = new URL(url, location.href);
      if (u.protocol === "http:" || u.protocol === "https:") safeUrl = url;
    } catch (e) { /* invalid URL: drop */ }
  }
  const postedAt = cand.posted_at ? cand.posted_at.substring(0, 10) : "";
  // XSS 防御: news_id / postedAt もユーザー生成由来ではないが、将来の入力差し替えに備え一貫してエスケープ。
  document.getElementById("meta").innerHTML = `
    <strong>[${idx + 1} / ${CANDIDATES.length}]</strong>
    @${escapeHtml(cand.username || "")}
    ${postedAt ? ` · ${escapeHtml(postedAt)}` : ""}
    ${safeUrl ? ` · <a href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener">X で見る ↗</a>` : ""}
    · <code style="color:#64748b;">news_id=${escapeHtml(cand.news_id)}</code>
  `;
  document.getElementById("text").textContent = cand.text || "";

  // categories
  const catDiv = document.getElementById("categories");
  catDiv.innerHTML = "";
  CATEGORIES.forEach(([key, ja], i) => {
    const checked = rec.labels.includes(key);
    const num = i + 1;
    const wrap = document.createElement("label");
    wrap.className = "cat-label" + (checked ? " checked" : "");
    wrap.innerHTML = `
      <span class="cat-key">${num}</span>
      <input type="checkbox" ${checked ? "checked" : ""} data-key="${key}">
      <span>${escapeHtml(ja)}</span>
      <span class="cat-key-en">${key}</span>
    `;
    wrap.querySelector("input").addEventListener("change", (e) => {
      toggleLabel(key, e.target.checked);
      render();
    });
    catDiv.appendChild(wrap);
  });

  // "none" ボタン: ラベル空 + notes ありで「該当なし確定済み」、ラベル空 + notes 空で「未操作」を区別。
  const noneBtn = document.getElementById("none-btn");
  noneBtn.classList.toggle("active", rec.labels.length === 0 && !!(rec.notes && rec.notes.trim()));

  // notes
  document.getElementById("notes").value = rec.notes || "";

  // progress
  const done = CANDIDATES.filter(c => {
    const r = state[c.news_id];
    return r.labeled_at !== null;
  }).length;
  const pct = Math.round(done / CANDIDATES.length * 100);
  document.getElementById("progress-fill").style.width = pct + "%";
  document.getElementById("progress-text").textContent = `${done} / ${CANDIDATES.length}`;
  document.getElementById("progress-bar").setAttribute("aria-valuenow", String(pct));

  // prev/next button state
  document.getElementById("prev-btn").disabled = idx === 0;
  const isLast = idx === CANDIDATES.length - 1;
  document.getElementById("next-btn").textContent = isLast ? "確定 (Enter)" : "次へ → (Enter)";

  // status hint
  const hint = document.getElementById("status-hint");
  hint.textContent = rec.labeled_at ? "✓ ラベル済" : (rec.labels.length > 0 || rec.notes ? "編集中" : "未ラベル");
  hint.style.color = rec.labeled_at ? "#10b981" : "#64748b";

  // completed banner
  document.getElementById("completed-banner").classList.toggle("visible", done === CANDIDATES.length);
}

function toggleLabel(key, checked) {
  const cand = CANDIDATES[idx];
  const rec = state[cand.news_id];
  if (checked && !rec.labels.includes(key)) rec.labels.push(key);
  if (!checked) rec.labels = rec.labels.filter(x => x !== key);
  saveState();
}

// 既存のラベルを全部外し、notes 入力にフォーカスする。notes 必須は confirmCurrent() で強制される。
function selectNone() {
  const cand = CANDIDATES[idx];
  const rec = state[cand.news_id];
  if (rec.labels.length > 0) {
    rec.labels = [];
    saveState();
    render();
  }
  document.getElementById("notes").focus();
}

function confirmCurrent() {
  const cand = CANDIDATES[idx];
  const rec = state[cand.news_id];
  rec.notes = document.getElementById("notes").value.trim();
  // README 中立性: 空配列ラベル時は「該当なし」理由の notes 必須（F1 計測時の判断根拠担保）。
  if (rec.labels.length === 0 && !rec.notes) {
    alert("「どれにも該当しない」を選んだ場合は notes に理由を記載してください（例: 「雑談のみ」「告知のみ」）。");
    document.getElementById("notes").focus();
    return false;
  }
  rec.labeled_at = nowJstIso();
  rec.labeler = getLabeler();
  saveState();
  return true;
}

function next() {
  if (!confirmCurrent()) return;
  if (idx < CANDIDATES.length - 1) {
    idx++;
    render();
    document.getElementById("text").scrollIntoView({ behavior: "smooth", block: "start" });
  } else {
    render();
  }
}

function prev() {
  // 前に戻るときは notes のみ保存（確定はしない）
  const cand = CANDIDATES[idx];
  state[cand.news_id].notes = document.getElementById("notes").value.trim();
  saveState();
  if (idx > 0) {
    idx--;
    render();
  }
}

// README: posted_at / labeled_at は JST +09:00 形式必須。candidates の UTC (Z) や任意 offset を JST に正規化。
function toJstIso(s) {
  if (!s) return "";
  // 既に +09:00 ならそのまま返す。+00:00 / -05:00 等は再計算して JST に寄せる。
  if (/\+09:?00$/.test(s)) return s;
  const d = new Date(s);
  if (isNaN(d.getTime())) return s;
  const jst = new Date(d.getTime() + 9 * 60 * 60 * 1000);
  return jst.toISOString().slice(0, 19) + "+09:00";
}

const VALID_LABELS = new Set(CATEGORIES.map(([key]) => key));

function download() {
  // README 仕様: 35 件揃ってから提出。未完了時は確認ダイアログで誤提出を防ぐ（部分バックアップ用途は許可）。
  const done = CANDIDATES.filter(c => state[c.news_id].labeled_at !== null).length;
  if (done < CANDIDATES.length) {
    const msg = `未完了 (${done}/${CANDIDATES.length} 件ラベル済)。中間バックアップとしてダウンロードしますか？`;
    if (!confirm(msg)) return;
  }
  const lines = CANDIDATES.map(c => {
    const rec = state[c.news_id];
    // localStorage 改変時の防衛: 7 カテゴリ以外の値は除去
    const labels = (rec.labels || []).filter(l => VALID_LABELS.has(l));
    // 未確定行は labeler / labeled_at を空のまま出力（中間バックアップ時に確定済みと誤認されないため）
    const confirmed = typeof rec.labeled_at === "string" && rec.labeled_at !== null;
    return JSON.stringify({
      news_id: c.news_id,
      tweet_url: c.tweet_url || "",
      username: c.username || "",
      posted_at: toJstIso(c.posted_at || ""),
      text: c.text || "",
      labels: labels,
      labeler: confirmed ? (rec.labeler || getLabeler()) : "",
      labeled_at: confirmed ? rec.labeled_at : "",
      notes: rec.notes || "",
    });
  });
  const blob = new Blob([lines.join("\n") + "\n"], { type: "application/x-ndjson" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "gold_set.jsonl";
  a.click();
  URL.revokeObjectURL(a.href);
}

function resetAll() {
  if (!confirm("全進捗を削除します。本当によろしいですか？")) return;
  localStorage.removeItem(STORAGE_KEY);
  state = loadState();
  idx = 0;
  render();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

// README の posted_at / labeled_at は JST (+09:00) 必須。UTC ではなく JST で発行する。
function nowJstIso() {
  const d = new Date();
  const jst = new Date(d.getTime() + 9 * 60 * 60 * 1000);
  const s = jst.toISOString();
  return s.slice(0, 19) + "+09:00";
}

// Keyboard shortcuts
document.addEventListener("keydown", (e) => {
  // Don't hijack when typing in inputs/textarea (except Enter/Shift+Enter on textarea which we handle specially)
  const tag = (e.target.tagName || "").toLowerCase();
  const isTextarea = tag === "textarea";
  const isInput = tag === "input";

  // labeler 入力 (input[type=text]) で Enter を奪うと名前入力中に誤って次に進む。
  // textarea (notes) と input (labeler) の両方で Enter は通し、ボタン/body フォーカス時のみ next()/prev() を発火。
  const isFormField = isTextarea || isInput;
  // none-btn にフォーカスがある時の Enter はボタン既定の click を発火させる（next() に奪われないようガード）。
  const isNoneBtnFocused = e.target && e.target.id === "none-btn";

  if (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
    if (isFormField || isNoneBtnFocused) return;
    e.preventDefault();
    next();
  } else if (e.key === "Enter" && e.shiftKey) {
    if (isFormField || isNoneBtnFocused) return;
    e.preventDefault();
    prev();
  } else if (/^[1-7]$/.test(e.key) && !isFormField) {
    e.preventDefault();
    const key = CATEGORIES[parseInt(e.key, 10) - 1][0];
    const cand = CANDIDATES[idx];
    const rec = state[cand.news_id];
    const has = rec.labels.includes(key);
    toggleLabel(key, !has);
    render();
  } else if (e.key === "0" && !isFormField) {
    e.preventDefault();
    selectNone();
  }
});

// Button wiring
document.getElementById("prev-btn").addEventListener("click", prev);
document.getElementById("next-btn").addEventListener("click", next);
document.getElementById("download-btn").addEventListener("click", download);
document.getElementById("reset-btn").addEventListener("click", resetAll);
document.getElementById("none-btn").addEventListener("click", selectNone);
document.getElementById("labeler-input").addEventListener("input", (e) => {
  localStorage.setItem(LABELER_KEY, e.target.value.trim());
});
document.getElementById("notes").addEventListener("input", () => {
  const cand = CANDIDATES[idx];
  state[cand.news_id].notes = document.getElementById("notes").value;
  saveState();
});

// Init
const savedLabeler = localStorage.getItem(LABELER_KEY);
if (savedLabeler) setLabeler(savedLabeler);

// Jump to first unlabeled
const firstUnlabeled = CANDIDATES.findIndex(c => state[c.news_id].labeled_at === null);
if (firstUnlabeled >= 0) idx = firstUnlabeled;
render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
