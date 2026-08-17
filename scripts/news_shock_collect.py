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
# R2「時計」: 高頻度ポーリング対照レーンの first_seen 台帳（本線と完全分離・
# tasks/news_shock_preregister.md §7 が指標の正本）
PROBE_LEDGER = APP / "data/news_shock/first_seen_probe.jsonl"

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


# 攻撃・爆発系語彙が有効になる供給設備語（§16u「供給ショック限定」の機械ガード・凍結）
FACILITY_WORDS = ("terminal", "refinery", "pipeline", "mine", "smelter",
                  "port", "facility", "platform")
# 攻撃系ファミリー（v2: drone strike 1語→ファミリー全体へ一般化・第3R B2対応）
ATTACK_VOCAB = ("drone strike", "missile attack", "missile strike",
                "air strike", "airstrike", "precision strike",
                "sabotage", "blast", "explosion")


def _contains_token(t: str, phrase: str) -> bool:
    """ASCII語句は単語境界つき完全語句一致（gold→golden 誤爆防止）・日本語は部分一致。"""
    p = phrase.lower()
    if p.isascii():
        return re.search(r"\b" + re.escape(p) + r"\b", t) is not None
    return p in t


def judge(text: str, terms: list[str], vocab: list[str],
          terms_are_facilities: bool = False) -> tuple[str, str] | None:
    """§16u 規則1の機械判定: 〈商品名 or 施設名〉AND 凍結語彙 の両方を含む時だけ
    (一致した対象語, 一致した語彙) を返す。

    Google News の検索は曖昧一致（類義語展開）があるため、返ってきた記事を
    ここでもう一度厳密に照合する＝凍結語彙に無い言い回しでは発火しない。
    語彙側も単語境界つき照合（v2実疎通で「precisiON STRIKE」が "on strike" に
    部分文字列一致した誤マッチを検出したための凍結ガード）。
    """
    t = text.lower()
    hit_term = next((x for x in terms if _contains_token(t, x)), None)
    if hit_term is None:
        return None
    # 英語慣用句ガード（実疎通で検出した誤マッチの凍結リスト・2026-08-16）:
    # strike gold/strike it rich=大当たり・blast from the past=懐かしの・silver lining=不幸中の幸い
    if hit_term.lower() == "silver" and "silver lining" in t:
        return None
    for v in vocab:
        if _contains_token(t, v):
            if "strike" in v.lower() and any(x in t for x in (
                    "strike gold", "struck gold", "strike it rich", "struck it rich")):
                continue
            if v.lower() == "blast" and "blast from the past" in t:
                continue
            # 攻撃・爆発系語彙は「供給設備の語」を伴う時だけ有効。
            # ただし施設名クエリでは対象語そのものが供給設備なのでガード充足とみなす
            if (v.lower() in ATTACK_VOCAB and not terms_are_facilities
                    and not any(k in t for k in FACILITY_WORDS)):
                continue
            return hit_term, v
    return None


def vocab_family(v: str, families: dict[str, list[str]]) -> str:
    for fam, words in families.items():
        if v in words:
            return fam
    return "other"


def event_id_of(series_ids: list[str], family: str, pubdate: str, run_at: str,
                subject: str = "") -> str:
    """事象バケットキー（凍結規則）: sorted(series)|語彙ファミリー|対象施設|UTC日付の sha 先頭12桁。

    日付は pubdate（媒体掲載時刻）優先・解釈不能なら run_at の日付。subject は施設クエリで
    一致した施設名（小文字）・商品クエリでは空＝商品レベルのバケット。
    ⚠️ これは**真の事象IDでなく日次バケットの代理**（Codex v2審 C2）: 同日・同商品・同ファミリーの
    別事象は施設名が無い限り合流し、日跨ぎの同一事象は分裂する。既知の限界としてプレレジ§6に明記。
    """
    import hashlib
    from email.utils import parsedate_to_datetime
    try:
        d = parsedate_to_datetime(pubdate).astimezone(timezone.utc).strftime("%Y%m%d")
    except (TypeError, ValueError):
        d = run_at[:10].replace("-", "")
    raw = "|".join(sorted(series_ids)) + "|" + family + "|" + subject.lower() + "|" + d
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def fetch_rss(qe: dict, cfg: dict) -> list[dict]:
    """1クエリ分の RSS を取得して item の (title, link, desc, pubdate) を返す。

    v2: 検索の OR 句は cfg['search_or_phrases'] から合成（v1 のコード直書きは
    「凍結した語彙が取得母集団に効かない」バグだった・第3R A3対応）。
    施設クエリ（or_terms あり）は施設名群そのものを検索語にする。
    """
    collect = cfg.get("collect", {})
    or_block = " OR ".join(cfg["search_or_phrases"])
    if qe.get("or_terms"):
        subject = "(" + " OR ".join(qe["or_terms"]) + ")"
    else:
        subject = qe["term"]
    q = f"{subject} ({or_block}) when:{collect.get('when_days', 2)}d"
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


def load_seen(path: Path = LEDGER) -> tuple[set[tuple[str, str]], set[str]]:
    """既出の (term, link) と event_id を台帳から読む（後者は通知の重複抑止用）。"""
    seen, seen_events = set(), set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    seen.add((r.get("term", ""), r.get("link", "")))
                    if r.get("event_id"):
                        seen_events.add(r["event_id"])
                except json.JSONDecodeError:
                    continue
    return seen, seen_events


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
             "drone strike", "missile attack", "sabotage", "port closure",
             "explosion", "blast", "禁輸"]
    cases = [
        ("Congo announces copper export ban", ["copper"], "export ban",
         "商品名+語彙の AND で発火"),
        ("Copper price hits record high on demand", ["copper"], None,
         "商品名だけ（語彙なし）は発火しない＝価格実況を通さない"),
        ("Miners strike in Chile halts operations", ["copper"], None,
         "語彙だけ（商品名なし）は発火しない＝§16u 規則1"),
        ("コンゴが銅の禁輸を発表", ["銅"], "禁輸", "日本語の商品名+語彙も発火"),
        ("Gold miners strike enters second week", ["gold"], "miners strike", "別商品の AND"),
        ("Treasure hunters strike gold after years", ["gold"], None,
         "慣用句 strike gold に誤発火しない（2026-08-16 実疎通の教訓）"),
        ("Drone strike sparks blaze at Russian oil terminal", ["oil"], "drone strike",
         "攻撃による供給支障は拾う"),
        ("Construction workers strike gold at building site", ["gold"], None,
         "workers strike gold（慣用句）にも誤発火しない"),
        ("Gold mine workers strike over pay dispute", ["gold"], "workers strike",
         "本物の労働ストは strike gold を含まないので発火する"),
        ("Golden Week travel demand hits record", ["gold"], None,
         "gold は golden に部分一致しない（単語境界・Codex指摘）"),
        ("Coalition announces new export ban on wheat", ["coal"], None,
         "coal は coalition に部分一致しない"),
        ("Drone strike hits residential area in city", ["oil"], None,
         "攻撃語のみ（供給設備の語なし）では発火しない"),
        # --- v2 追加（第3R対応）---
        ("Escondida workers go on strike over wages",
         ["Escondida", "Collahuasi", "El Teniente"], "on strike",
         "施設名クエリ: 商品名が無くても施設名+語彙で発火（Escondida型）"),
        ("Missile attack damages oil export terminal", ["oil"], "missile attack",
         "攻撃ファミリー一般化: missile attack+設備語で発火"),
        ("Missile attack on residential district", ["oil"], None,
         "missile attack も設備語なしでは発火しない"),
        ("Port closure halts copper concentrate shipments", ["copper"], "port closure",
         "閉鎖系語彙（port closure）で発火"),
        ("Copper wins precision strike appeal in court", ["copper"], None,
         "precision strike は設備語なし（商品クエリ）では発火しない"),
        ("Qld miners strike it rich amid silver boom", ["silver"], None,
         "慣用句 strike it rich に誤発火しない（実疎通の教訓）"),
        ("Blast from the Past: When Chile nationalized copper mines", ["copper"], None,
         "慣用句 blast from the past に誤発火しない"),
        ("China's export ban has a silver lining for traders", ["silver"], None,
         "慣用句 silver lining では silver 判定を無効化"),
        ("Explosion at silver mine halts production", ["silver"], "explosion",
         "本物の銀鉱山爆発は発火する（silver lining ガードの過剰適用なし）"),
    ]
    ng = 0
    for text, terms, want, why in cases:
        got = judge(text, terms, vocab)
        got_v = got[1] if got else None
        ok = got_v == want
        ng += not ok
        print(f"  {'OK ' if ok else 'NG '} {why}（期待={want} 実際={got_v}）")
    # 重複排除: 同じ (term, link) は2度数えない
    seen = {("copper", "http://x/1")}
    ok_d = ("copper", "http://x/1") in seen and ("gold", "http://x/1") not in seen
    print(f"  {'OK ' if ok_d else 'NG '} 重複キーは (term, link)")
    ng += not ok_d
    # event_id: 別リンク・別クエリでも 同series×同ファミリー×同日 なら同一事象に畳まれる
    fam = {"strike": ["workers strike", "miners strike"]}
    e1 = event_id_of(["copper"], vocab_family("workers strike", fam),
                     "Fri, 14 Aug 2026 11:30:00 GMT", "2026-08-16T10:00:00+00:00")
    e2 = event_id_of(["copper"], vocab_family("miners strike", fam),
                     "Fri, 14 Aug 2026 23:00:00 GMT", "2026-08-16T11:00:00+00:00")
    e3 = event_id_of(["copper"], "strike", "Sat, 15 Aug 2026 01:00:00 GMT",
                     "2026-08-16T11:00:00+00:00")
    ok_e = e1 == e2 and e1 != e3
    print(f"  {'OK ' if ok_e else 'NG '} event_id: 同series×同ファミリー×同UTC日は同一・日跨ぎは別")
    ng += not ok_e
    # 同日の別施設は別事象（Codex v2審 C2: Escondida と Collahuasi の合流を防ぐ）
    ea = event_id_of(["copper"], "strike", "Fri, 14 Aug 2026 11:00:00 GMT",
                     "2026-08-16T10:00:00+00:00", subject="Escondida")
    eb = event_id_of(["copper"], "strike", "Fri, 14 Aug 2026 12:00:00 GMT",
                     "2026-08-16T10:00:00+00:00", subject="Collahuasi")
    ok_s = ea != eb
    print(f"  {'OK ' if ok_s else 'NG '} event_id: 同日でも別施設（subject）なら別事象")
    ng += not ok_s

    # --- 統合契約テスト（Codex v2審 C1/W1/W3: 実configとの整合を機械検証）---
    cfg = load_config()
    # C1: 全ASCII語彙がいずれかの検索句と部分文字列関係を持つ（取得母集団に届く）
    phrases = [p.strip('"').lower() for p in cfg["search_or_phrases"]]
    uncovered = [v for v in cfg["vocab"] if v.isascii()
                 and not any(p in v.lower() or v.lower() in p for p in phrases)]
    print(f"  {'OK ' if not uncovered else 'NG '} 語彙カバレッジ: 検索句に届かないASCII語彙 "
          f"{uncovered or 'なし'}")
    ng += bool(uncovered)
    # W1: 施設クエリの or_terms は facilities に全て存在し series が整合する
    bad = []
    fac = {k.lower(): v for k, v in cfg["facilities"].items()}
    q_terms = set()
    for qe in cfg["queries"]:
        for x in qe.get("or_terms", []):
            name = x.strip('"').lower()
            q_terms.add(name)
            if name not in fac or not set(qe["series_ids"]) <= set(fac[name]):
                bad.append(name)
    unqueried = sorted(set(fac) - q_terms)
    ok_f = not bad and not unqueried
    print(f"  {'OK ' if ok_f else 'NG '} 施設整合: 不整合={bad or 'なし'} 未クエリ施設={unqueried or 'なし'}")
    ng += not ok_f
    # クエリ合成: 引用符つき or_terms が括弧OR結合され検索句ブロックを含む
    import urllib.parse as up
    fq = next(qe for qe in cfg["queries"] if qe.get("or_terms"))
    # fetch_rss は URL を作る直前まで同じ式を組む（式の合成だけを検証・HTTPは打たない）
    or_block = " OR ".join(cfg["search_or_phrases"])
    subj = "(" + " OR ".join(fq["or_terms"]) + ")"
    q = f"{subj} ({or_block}) when:2d"
    ok_q = '"El Teniente"' in q and " OR " in subj and "export ban" in q
    print(f"  {'OK ' if ok_q else 'NG '} クエリ合成: 施設OR括弧+検索句ブロック（長さ{len(up.quote(q))}）")
    ng += not ok_q
    # load_seen: v1形式行（event_id なし）と v2 行の混在を読める
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"term": "copper", "link": "http://v1", "status": "hit"}) + "\n")
        f.write(json.dumps({"term": "gold", "link": "http://v2", "event_id": "abc"}) + "\n")
        f.write("壊れた行\n")
        pth = f.name
    s, se = load_seen(Path(pth))
    Path(pth).unlink()
    ok_l = ("copper", "http://v1") in s and ("gold", "http://v2") in s and se == {"abc"}
    print(f"  {'OK ' if ok_l else 'NG '} load_seen: v1/v2混在+壊れ行で落ちない")
    ng += not ok_l
    return ng


def run_probe() -> int:
    """R2時計の対照レーン: 同一クエリ・同一判定で first_seen だけを高頻度記録する。

    本線との違い（凍結・プレレジ§7）: 通知なし・受益引き当てなし・本線台帳に書かない。
    重複キーは (term, link)＝この台帳内で最初に見えた時刻だけが残る。
    """
    import fcntl
    PROBE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    lock = (PROBE_LEDGER.parent / ".probe.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("別のprobeが実行中のためスキップ")
        return 0
    cfg = load_config()
    vocab = cfg["vocab"]
    families = cfg.get("vocab_families", {})
    cfg_sha = __import__("hashlib").sha256(CONFIG.read_bytes()).hexdigest()[:12]
    seen, _ = load_seen(PROBE_LEDGER)
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n_new, n_err = 0, 0
    items_per_term = {}
    with PROBE_LEDGER.open("a", encoding="utf-8") as fh:
        for qe in cfg["queries"]:
            term = qe.get("term") or qe["facility_group"]
            terms = ([qe["term"]] if qe.get("term")
                     else [x.strip('"') for x in qe["or_terms"]])
            try:
                items = fetch_rss(qe, cfg)
                items_per_term[term] = len(items)
            except Exception as e:
                n_err += 1
                fh.write(json.dumps({"first_seen_at": run_at, "term": term,
                                     "status": "error", "error": str(e)[:200]},
                                    ensure_ascii=False) + "\n")
                continue
            for it in items:
                j = judge(f"{it['title']} {it['desc']}", terms, vocab,
                          terms_are_facilities=bool(qe.get("or_terms")))
                if not j or (term, it["link"]) in seen:
                    continue
                hit_term, v = j
                seen.add((term, it["link"]))
                fam = vocab_family(v, families)
                subject = hit_term if qe.get("or_terms") else ""
                eid = event_id_of(qe["series_ids"], fam, it["pubdate"], run_at, subject)
                fh.write(json.dumps({"first_seen_at": run_at, "term": term,
                                     "matched_term": hit_term, "matched_vocab": v,
                                     "series_ids": qe["series_ids"], "event_id": eid,
                                     "title": it["title"], "link": it["link"],
                                     "pubdate": it["pubdate"], "status": "hit",
                                     "config_version": cfg.get("version"),
                                     "config_sha": cfg_sha}, ensure_ascii=False) + "\n")
                n_new += 1
        fh.write(json.dumps({"type": "run_summary", "run_at": run_at, "hit": n_new,
                             "ok": len(cfg["queries"]) - n_err, "error": n_err,
                             "items": items_per_term, "config_version": cfg.get("version"),
                             "config_sha": cfg_sha}, ensure_ascii=False) + "\n")
    print(f"[probe] first_seen 新規 {n_new} 件 / error {n_err}/{len(cfg['queries'])}")
    return 1 if n_err > 3 else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="通知を出さない")
    ap.add_argument("--first-seen-probe", action="store_true",
                    help="R2時計: first_seen 記録だけの対照モード（通知・受益引き当て・本線台帳なし）")
    args = ap.parse_args()
    if args.selftest:
        ng = _selftest()
        print("OK: 全通過" if not ng else f"NG: {ng}件失敗")
        return 1 if ng else 0
    if args.first_seen_probe:
        return run_probe()

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
    families = cfg.get("vocab_families", {})
    cfg_sha = __import__("hashlib").sha256(CONFIG.read_bytes()).hexdigest()[:12]
    seen, seen_events = load_seen()
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n_hit, n_err = 0, 0
    items_per_term = {}    # recall のサイレント崩壊検知用（敵対R3 A10: 0件返答と事象なしを区別）
    hits_for_notify = []
    with LEDGER.open("a", encoding="utf-8") as fh:
        for qe in cfg["queries"]:
            term = qe.get("term") or qe["facility_group"]
            # 判定対象語: 商品クエリ=商品名／施設クエリ=施設名群（引用符を剥がす）
            terms = ([qe["term"]] if qe.get("term")
                     else [x.strip('"') for x in qe["or_terms"]])
            try:
                items = fetch_rss(qe, cfg)
                items_per_term[term] = len(items)
            except Exception as e:                     # fail-closed: エラーは台帳に残す
                n_err += 1
                fh.write(json.dumps({"run_at": run_at, "term": term, "status": "error",
                                     "error": str(e)[:200]}, ensure_ascii=False) + "\n")
                print(f"[{term}] ERROR {e}", file=sys.stderr)
                continue
            for it in items:
                j = judge(f"{it['title']} {it['desc']}", terms, vocab,
                          terms_are_facilities=bool(qe.get("or_terms")))
                if not j or (term, it["link"]) in seen:
                    continue
                hit_term, v = j
                seen.add((term, it["link"]))
                fam = vocab_family(v, families)
                subject = hit_term if qe.get("or_terms") else ""
                eid = event_id_of(qe["series_ids"], fam, it["pubdate"], run_at, subject)
                dup = eid in seen_events
                seen_events.add(eid)
                bens = beneficiaries_of(qe["series_ids"])
                # 版とconfig指紋を発火行へ焼き込む（語彙変更後も母集団を分離再現できる・Codex指摘）
                rec = {"run_at": run_at, "term": term, "matched_term": hit_term,
                       "series_ids": qe["series_ids"],
                       "matched_vocab": v, "vocab_family": fam,
                       "event_id": eid, "dup_event": dup,
                       "title": it["title"], "link": it["link"],
                       "pubdate": it["pubdate"], "beneficiaries": bens, "status": "hit",
                       "config_version": cfg.get("version"), "config_sha": cfg_sha}
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_hit += 1
                labels = "/".join(f"{b['code']}{'(仮)' if b['tier']=='provisional' else ''}"
                                  + (f"[{b['type_label']}]" if b["type_label"] else "")
                                  for b in bens[:4]) or "受益カードなし"
                if not dup:    # 同一事象（event_id）の2件目以降は台帳のみ・通知しない
                    hits_for_notify.append(f"{hit_term}:{v} → {labels}")
                print(f"🛑 [{term}] {v}{'（同一事象・通知抑止）' if dup else ''} | {it['title'][:60]}")
                print(f"    受益: {labels}")
        # 実行サマリ行（欠測の見える化・空振りの日もこの1行は必ず残る）。
        # items= クエリ別の RSS 取得件数: 全クエリ 0件が続けば「事象が無い」でなく
        # 「Google 側の recall 崩壊」を疑う（敵対R3 A10）
        fh.write(json.dumps({"type": "run_summary", "run_at": run_at, "hit": n_hit,
                             "ok": len(cfg["queries"]) - n_err, "error": n_err,
                             "items": items_per_term,
                             "config_version": cfg.get("version"), "config_sha": cfg_sha},
                            ensure_ascii=False) + "\n")
    # 通知件数=ユニーク事象数（発火行数 n_hit ではない・Codex v2審 W2）。0件なら通知しない
    if hits_for_notify and not args.dry_run:
        notify(f"🛑 供給ショック検知: {len(hits_for_notify)}事象",
               " / ".join(hits_for_notify)[:200])
    print(f"[done] hit={n_hit} error={n_err}/{len(cfg['queries'])}クエリ")
    # 部分障害ゲート: 4クエリ以上失敗（ok<15/18）で異常終了→runner が失敗通知（Codex指摘）
    if n_err > 3:
        print(f"ERROR: 失敗クエリ {n_err}件（許容3）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
