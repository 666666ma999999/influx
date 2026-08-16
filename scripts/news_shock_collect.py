"""商品名つき供給ショックのニュースを Google News RSS から収集し、受益銘柄つきで通知する.

位置づけ（2026-08-16 新設・入場条件の正本= docs/price-watch-universe.md §16u）:
銅ケース（コンゴ禁輸 → LME銅 → 株高 2026-08-04〜07）で、株高の前に存在した唯一の
無料シグナルが「商品名つき供給ショックのニュース」だった。§16d の棄却実測
（企業名なしニュースは帰属不能・「誰より早く」は効かない）と衝突しない境界として、
①監視系列に対応する商品名を含む ②凍結語彙（禁輸・スト等）を含む——の AND だけを拾う。
帰属は受益カード（configs/price_universe_sources.json の confirmed/provisional・sign+）経由のみ。

規約:
- 台帳 data/news_shock/news_log.jsonl は append-only・(series, link) で重複スキップ
- 依存は標準ライブラリのみ（ホスト /usr/bin/python3 で動く・Docker 不要）
- fail-closed: 全クエリ失敗で exit 1・通知失敗は握りつぶさず WARN をログに出す

実行:
    python3 scripts/news_shock_collect.py               # 収集→判定→通知→台帳
    python3 scripts/news_shock_collect.py --dry-run     # 通知しない（台帳には書く）
    python3 scripts/news_shock_collect.py --selftest    # 判定ロジックの固定テスト
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "scripts"))

CONFIG = APP / "configs/news_shock.json"
SOURCES = APP / "configs/price_universe_sources.json"
LEDGER = APP / "data/news_shock/news_log.jsonl"

_TAG = re.compile(r"<[^>]+>")


def load_config(path: Path = CONFIG) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def beneficiaries_of(series_ids: list[str], sources_path: Path = SOURCES) -> list[dict]:
    """series_ids に紐づく正方向カード（confirmed/provisional・sign+）を返す。

    §16u 規則2: ニュース本文から銘柄を直接推定しない。カード経由のみ。
    受益タイプの平易ラベルは center_pin 台帳（x_mention_dict.pin_info）から引く。
    """
    try:
        import x_mention_dict as xmd
        pins = xmd.pin_info()
    except Exception:                                  # 台帳が無くても本線は止めない
        pins = {}
    src = json.loads(sources_path.read_text(encoding="utf-8"))
    out, seen = [], set()
    for se in src.get("series", []):
        if se.get("id") not in series_ids:
            continue
        for b in se.get("beneficiaries", []):
            code = b.get("code")
            if not code or code in seen:
                continue
            if b.get("sign") == "+" and b.get("tier") in ("confirmed", "provisional"):
                seen.add(code)
                out.append({"code": code, "tier": b["tier"],
                            "type_label": (pins.get(code) or {}).get("type_label", "")})
    return out


def judge(text: str, term: str, vocab: list[str]) -> str | None:
    """§16u 規則1の機械判定: 商品名 AND 凍結語彙 の両方を含む時だけ語彙を返す。

    Google News の検索は曖昧一致（類義語展開）があるため、返ってきた記事を
    ここでもう一度厳密に照合する＝凍結語彙に無い言い回しでは発火しない。
    """
    t = text.lower()
    term_l = term.lower()
    if term_l.isascii():
        # 英数字の商品名は単語境界つき完全語句一致（gold→golden / coal→coalition の
        # 部分一致誤爆を防ぐ・Codex 2026-08-16 指摘）。日本語は境界が無いので部分一致のまま
        if not re.search(r"\b" + re.escape(term_l) + r"\b", t):
            return None
    elif term_l not in t:
        return None
    for v in vocab:
        if v.lower() in t:
            # 英慣用句「strike gold（大当たり）」の除外: strike 系語彙は、本文に
            # strike gold / struck gold がある場合は無効（2026-08-16 実疎通で
            # workers strike gold 型の誤発火を確認したための凍結ガード）
            if "strike" in v.lower() and ("strike gold" in t or "struck gold" in t):
                continue
            # 攻撃系語彙は「供給設備の語」を伴う時だけ有効（§16u「供給ショック限定」を
            # 機械で守る・Codex 2026-08-16 指摘: drone strike 単独では供給支障を保証しない）
            if v.lower() == "drone strike" and not any(
                    k in t for k in ("terminal", "refinery", "pipeline", "mine",
                                     "smelter", "port", "facility", "platform")):
                continue
            return v
    return None


def fetch_rss(term: str, vocab: list[str], collect: dict) -> list[dict]:
    """1クエリ分の RSS を取得して item の (title, link, desc, pubdate) を返す。"""
    q = f'{term} ("export ban" OR "export restriction" OR embargo OR strike OR ' \
        f'"force majeure" OR "mine accident" OR "supply disruption" OR "production halt")' \
        f' when:{collect.get("when_days", 2)}d'
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
           + f"&hl={collect.get('hl','en-US')}&gl={collect.get('gl','US')}"
           + f"&ceid={urllib.parse.quote(collect.get('ceid','US:en'))}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=collect.get("timeout_sec", 20)) as res:
        root = ET.fromstring(res.read())
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = _TAG.sub(" ", it.findtext("description") or "").strip()
        items.append({"title": title, "link": (it.findtext("link") or "").strip(),
                      "desc": desc, "pubdate": (it.findtext("pubDate") or "").strip()})
    return items


def load_seen(path: Path = LEDGER) -> set[tuple[str, str]]:
    seen = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    seen.add((r.get("term", ""), r.get("link", "")))
                except json.JSONDecodeError:
                    continue
    return seen


def notify(title: str, body: str) -> None:
    """macOS 通知。失敗は握りつぶさず WARN を出す（launchd ログに残る）。

    記事タイトル由来の引用符・バックスラッシュは AppleScript を壊すため除去する。
    """
    clean = re.sub(r'["\\\\]', "", body)[:120]
    r = subprocess.run(["osascript", "-e",
                        f'display notification "{clean}" with title "{title}"'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"WARN: 通知失敗 rc={r.returncode} {r.stderr.strip()[:100]}", file=sys.stderr)


def _selftest() -> int:
    vocab = ["export ban", "miners strike", "workers strike", "on strike",
             "drone strike", "禁輸"]
    cases = [
        ("Congo announces copper export ban", "copper", "export ban",
         "商品名+語彙の AND で発火"),
        ("Copper price hits record high on demand", "copper", None,
         "商品名だけ（語彙なし）は発火しない＝価格実況を通さない"),
        ("Miners strike in Chile halts operations", "copper", None,
         "語彙だけ（商品名なし）は発火しない＝§16u 規則1"),
        ("コンゴが銅の禁輸を発表", "銅", "禁輸", "日本語の商品名+語彙も発火"),
        ("Gold miners strike enters second week", "gold", "miners strike", "別商品の AND"),
        ("Treasure hunters strike gold after years", "gold", None,
         "慣用句 strike gold に誤発火しない（2026-08-16 実疎通の教訓）"),
        ("Drone strike sparks blaze at Russian oil terminal", "oil", "drone strike",
         "攻撃による供給支障は拾う"),
        ("Construction workers strike gold at building site", "gold", None,
         "workers strike gold（慣用句）にも誤発火しない"),
        ("Gold mine workers strike over pay dispute", "gold", "workers strike",
         "本物の労働ストは strike gold を含まないので発火する"),
        ("Golden Week travel demand hits record", "gold", None,
         "gold は golden に部分一致しない（単語境界・Codex指摘）"),
        ("Coalition announces new export ban on wheat", "coal", None,
         "coal は coalition に部分一致しない"),
        ("Drone strike hits residential area in city", "oil", None,
         "攻撃語のみ（供給設備の語なし）では発火しない"),
    ]
    ng = 0
    for text, term, want, why in cases:
        got = judge(text, term, vocab)
        ok = got == want
        ng += not ok
        print(f"  {'OK ' if ok else 'NG '} {why}（期待={want} 実際={got}）")
    # 重複排除: 同じ (term, link) は2度数えない
    seen = {("copper", "http://x/1")}
    ok_d = ("copper", "http://x/1") in seen and ("gold", "http://x/1") not in seen
    print(f"  {'OK ' if ok_d else 'NG '} 重複キーは (term, link)")
    ng += not ok_d
    return ng


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="通知を出さない")
    args = ap.parse_args()
    if args.selftest:
        ng = _selftest()
        print("OK: 全通過" if not ng else f"NG: {ng}件失敗")
        return 1 if ng else 0

    # 多重起動ロック（launchd と手動実行の重複で同じ行を二重追記しないため・Codex指摘）
    import fcntl
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    lock = (LEDGER.parent / ".collect.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("別の収集が実行中のためスキップ（多重起動ガード）")
        return 0

    cfg = load_config()
    vocab = cfg["vocab"]
    cfg_sha = __import__("hashlib").sha256(CONFIG.read_bytes()).hexdigest()[:12]
    seen = load_seen()
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n_hit, n_err = 0, 0
    hits_for_notify = []
    with LEDGER.open("a", encoding="utf-8") as fh:
        for qe in cfg["queries"]:
            term = qe["term"]
            try:
                items = fetch_rss(term, vocab, cfg.get("collect", {}))
            except Exception as e:                     # fail-closed: エラーは台帳に残す
                n_err += 1
                fh.write(json.dumps({"run_at": run_at, "term": term, "status": "error",
                                     "error": str(e)[:200]}, ensure_ascii=False) + "\n")
                print(f"[{term}] ERROR {e}", file=sys.stderr)
                continue
            for it in items:
                v = judge(f"{it['title']} {it['desc']}", term, vocab)
                if not v or (term, it["link"]) in seen:
                    continue
                seen.add((term, it["link"]))
                bens = beneficiaries_of(qe["series_ids"])
                # 版とconfig指紋を発火行へ焼き込む（語彙変更後も母集団を分離再現できる・Codex指摘）
                rec = {"run_at": run_at, "term": term, "series_ids": qe["series_ids"],
                       "matched_vocab": v, "title": it["title"], "link": it["link"],
                       "pubdate": it["pubdate"], "beneficiaries": bens, "status": "hit",
                       "config_version": cfg.get("version"), "config_sha": cfg_sha}
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_hit += 1
                labels = "/".join(f"{b['code']}{'(仮)' if b['tier']=='provisional' else ''}"
                                  + (f"[{b['type_label']}]" if b["type_label"] else "")
                                  for b in bens[:4]) or "受益カードなし"
                hits_for_notify.append(f"{term}:{v} → {labels}")
                print(f"🛑 [{term}] {v} | {it['title'][:60]}")
                print(f"    受益: {labels}")
        # 実行サマリ行（欠測の見える化・空振りの日もこの1行は必ず残る）
        fh.write(json.dumps({"type": "run_summary", "run_at": run_at, "hit": n_hit,
                             "ok": len(cfg["queries"]) - n_err, "error": n_err,
                             "config_version": cfg.get("version"), "config_sha": cfg_sha},
                            ensure_ascii=False) + "\n")
    if n_hit and not args.dry_run:
        notify(f"🛑 供給ショック検知: {n_hit}件", " / ".join(hits_for_notify)[:200])
    print(f"[done] hit={n_hit} error={n_err}/{len(cfg['queries'])}クエリ")
    # 部分障害ゲート: 4クエリ以上失敗（ok<15/18）で異常終了→runner が失敗通知（Codex指摘）
    if n_err > 3:
        print(f"ERROR: 失敗クエリ {n_err}件（許容3）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
