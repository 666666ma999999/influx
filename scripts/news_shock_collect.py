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
    # 品薄予兆レーン（P-INF-43 b・2026-10-10）: 別台帳 data/news_shock/presign_log.jsonl・既定経路は不変
    python3 scripts/news_shock_collect.py --lane presign                      # 収集→判定→台帳→新規エピソード時のみ通知
    python3 scripts/news_shock_collect.py --lane presign --dry-run [--date YYYY-MM-DD]  # 台帳に書かず要約だけ（疎通便）
    python3 scripts/news_shock_collect.py --lane presign --selftest           # presign 判定の固定テスト（固定反例 4 件）
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
from datetime import date, datetime, timedelta, timezone
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
            # §16w（2026-08-17 P-08c）: 海外上場カードはこのレーンに入れない。
            # news_shock の評価器は code+"0" を日本株コードとして対TOPIX評価するため、
            # 海外銘柄が混ざると日本株の検定が壊れる（fail-closed・混入経路を塞ぐ）
            if (b.get("market") or "JP") != "JP":
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


# 通知の鮮度関門（2026-10-11 P-INF-46 付帯・オーナー裁定）: Google News RSS は検索句の when:2d を守らず
# 数年前の記事を返す（実測 2026-08-16〜10-10: 通知した 360 事象のうち掲載から 180 日超が 159）。
# 掲載から NOTIFY_MAX_AGE_DAYS 日を超えた記事は **Mac 通知だけ**止める。台帳には従来どおり
# status=hit で残し pub_age_days / stale を足す＝事前登録（tasks/news_shock_preregister.md）の
# 発火母集団は変えない。configs の collect.notify_max_age_days があればそちらを使う。
NOTIFY_MAX_AGE_DAYS = 180


def pub_age_days(pubdate: str, run_at: str) -> float | None:
    """記事の掲載時刻（pubdate・RFC822）から便の時刻（run_at・ISO8601）までの日数。解釈不能は None。"""
    from email.utils import parsedate_to_datetime
    try:
        pub = parsedate_to_datetime((pubdate or "").strip())
        run = datetime.fromisoformat(run_at)
    except (TypeError, ValueError, IndexError):   # 3.9 は TypeError・3.10+ は ValueError
        return None
    if pub is None:
        return None
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    if run.tzinfo is None:
        run = run.replace(tzinfo=timezone.utc)
    return round((run - pub).total_seconds() / 86400, 1)


def is_stale(age_days: float | None, max_age_days: float | None = NOTIFY_MAX_AGE_DAYS) -> bool:
    """鮮度関門: 掲載から max_age_days 日を超えたら True。日数不明・関門なし（None）は False
    ＝鳴らす側に倒す（本物の事象を落とさない）。"""
    return age_days is not None and max_age_days is not None and age_days > float(max_age_days)


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
    # 通知の鮮度関門: 実害 2 件（2022・2021 年の記事が 2026-10 に発火）は stale・当日記事は通す・
    # 境界 180.0 日は通す／180.1 日は止める・日付が読めない記事は通す（鳴らす側に倒す）
    run = "2026-10-09T13:20:00+00:00"
    a_old = pub_age_days("Wed, 24 Aug 2022 07:00:00 GMT", run)      # Boliden 亜鉛（実害）
    a_old2 = pub_age_days("Mon, 17 May 2021 07:00:00 GMT", "2026-10-10T01:00:00+00:00")   # JM プラチナ
    a_new = pub_age_days("Fri, 09 Oct 2026 10:00:00 GMT", run)
    a_bad = pub_age_days("いつか", run)
    ok_s = (a_old is not None and a_old > 1500 and is_stale(a_old) and is_stale(a_old2)
            and a_new is not None and 0 <= a_new < 1 and not is_stale(a_new)
            and not is_stale(180.0) and is_stale(180.1)
            and a_bad is None and not is_stale(a_bad) and not is_stale(9999.0, None))
    print(f"  {'OK ' if ok_s else 'NG '} 鮮度関門: 古い記事（{a_old}日・{a_old2}日）は通知抑止・当日/日付不明は通す・境界180日")
    ng += not ok_s
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


# =============================================================================
# 品薄予兆レーン presign（P-INF-43 b・事前登録 2026-10-10・正本= tasks/shortage_presign_forward.md §B/§E
# と configs/news_shock.json の presign ブロック）。
# 上の供給ショックレーンの関数は 1 つも変えない。--lane presign の時だけ以下が動き、台帳も別
# （data/news_shock/presign_log.jsonl・news_log.jsonl には書かない）。
# =============================================================================
PRESIGN_LEDGER = APP / "data/news_shock/presign_log.jsonl"
PRESIGN_LANE = "presign"
# 収集区間（known_date ∈ [start, end)）の正本= 採点器の config（§C/§D）。便はここの start より前なら書かずに終える
PRESIGN_FORWARD_CONFIG = APP / "configs/presign_forward.json"
JST = timezone(timedelta(hours=9))


def presign_config(cfg: dict | None = None) -> dict:
    """configs/news_shock.json の presign ブロック（無ければ fail-closed）。"""
    cfg = load_config() if cfg is None else cfg
    pcfg = cfg.get("presign")
    if not pcfg:
        raise KeyError("configs/news_shock.json に presign ブロックが無い")
    return pcfg


def presign_collect_start(path: Path = PRESIGN_FORWARD_CONFIG) -> date:
    """configs/presign_forward.json の scoring.collect_interval.start（収集区間の開始日・JST 日付）。

    読めない・キーが無い・日付でない時は例外＝呼び手は非ゼロ終了する（黙って skip も黙って走るもしない・
    2026-10-10 team-lead 裁定）。
    """
    fwd = json.loads(path.read_text(encoding="utf-8"))
    return date.fromisoformat(fwd["scoring"]["collect_interval"]["start"])


def _presign_token_in(t: str, phrase: str) -> bool:
    """presign 用の語照合（t は小文字化済み）。

    ASCII 語= 英数字境界の完全語句一致・日本語= 部分一致（§B「ASCII= 単語境界・日本語= 部分一致」）。
    既存 `_contains_token` の `\\b` は CJK 文字を語構成文字と見なすため「DRAM契約価格」「PS5の」で
    境界が立たず正例（§B 固定正例）を落とす＝英数字以外の文字を境界に取る lookaround に置き換える。
    gold→golden・coal→coalition の誤爆防止は同じ。
    """
    p = phrase.lower()
    if p.isascii():
        return re.search(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", t) is not None
    return p in t


def _strip_excludes(t: str, excludes: list[str] | None) -> str:
    """query.exclude の語を本文から除く。exclude は**本文照合だけ**に使い、Google News の検索クエリには
    入れない（presign_gnews_query は terms と search_or_phrases だけで組む・§B ①）。語彙・方向語・否定語の
    判定は除去前の本文で行う。"""
    for ex in excludes or []:
        t = t.replace(ex.lower(), " ")
    return t


def normalize_link(link: str) -> str:
    """utm_* クエリと fragment を除いた URL（article_id と重複排除の鍵・識別規則 identity）。"""
    s = (link or "").strip()
    try:
        sp = urllib.parse.urlsplit(s)
    except ValueError:
        return s
    q = [(k, v) for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True)
         if not k.lower().startswith("utm_")]
    return urllib.parse.urlunsplit((sp.scheme, sp.netloc, sp.path,
                                    urllib.parse.urlencode(q, doseq=True), ""))


def presign_article_id(source_id: str, link_norm: str) -> str:
    import hashlib
    return hashlib.sha256(f"{source_id}|{link_norm}".encode()).hexdigest()[:16]


def presign_event_id(subject: str, family: str, pubdate_utc_date: str, source_class: str) -> str:
    import hashlib
    raw = f"{subject}|{family}|{pubdate_utc_date}|{source_class}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def parse_pubdate_utc(pubdate: str, run_at: str) -> tuple[str, bool]:
    """RSS の RFC822／Atom の ISO8601 を UTC 日付 'YYYY-MM-DD' に。解釈不能は便の UTC 日付＋False。"""
    from email.utils import parsedate_to_datetime
    s = (pubdate or "").strip()
    if s:
        dt = None
        try:
            dt = parsedate_to_datetime(s)
        except (TypeError, ValueError, IndexError):   # 3.9 は TypeError・3.10+ は ValueError
            dt = None
        if dt is None:
            iso = s[:-1] + "+00:00" if s.endswith("Z") else s
            try:
                dt = datetime.fromisoformat(iso)
            except ValueError:
                dt = None
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d"), True
    return run_at[:10], False


def parse_feed_items(data: bytes) -> list[dict]:
    """RSS 2.0（item/title/link/description/pubDate）と Atom（entry/title/link@href/summary|content/
    published|updated）を同じ形 {title, link, desc, pubdate} に揃える。RSS 1.0 (RDF)・content:encoded・
    dc:date も名前空間を剥がして拾う。"""
    # 先頭の空白・改行・BOM を剥がす（鶏鳴新聞 feed は 1 行目が空行で XML 宣言が 2 行目にあり、
    # そのままだと「XML declaration not at start of entity」で落ちる・2026-10-10 疎通便で実測）
    root = ET.fromstring(data.lstrip(b"\xef\xbb\xbf \t\r\n"))
    items = []
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        fields, link = {}, ""
        for ch in el:
            name = ch.tag.rsplit("}", 1)[-1]
            text = (ch.text or "").strip()
            if name == "link":
                href = (ch.get("href") or "").strip()
                if href:                                   # Atom: rel 省略= alternate
                    if (ch.get("rel") or "alternate") == "alternate" and not link:
                        link = href
                elif text and not link:                    # RSS: 要素テキスト
                    link = text
            elif name not in fields and text:
                fields[name] = text
        if not link and fields.get("guid", "").startswith("http"):
            link = fields["guid"]
        desc = (fields.get("description") or fields.get("encoded")
                or fields.get("summary") or fields.get("content") or "")
        pub = (fields.get("pubDate") or fields.get("published")
               or fields.get("updated") or fields.get("date") or "")
        items.append({"title": _TAG.sub(" ", fields.get("title", "")).strip(),
                      "link": link, "desc": _TAG.sub(" ", desc).strip(), "pubdate": pub})
    return items


def _fetch_bytes(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def presign_gnews_query(qe: dict, pcfg: dict) -> str:
    """§B ①: (主題語 OR) (検索句 OR) when:Nd。空白を含む主題語は引用符で囲む。query.exclude は検索クエリに
    入れない（本文照合でだけ除く・_strip_excludes）。"""
    terms = [f'"{x}"' if " " in x else x for x in qe["terms"]]
    or_block = " OR ".join(pcfg["search_or_phrases"])
    days = pcfg.get("collect", {}).get("when_days", 2)
    return f"({' OR '.join(terms)}) ({or_block}) when:{days}d"


def fetch_presign_gnews(qe: dict, pcfg: dict) -> list[dict]:
    """Google News RSS 検索（ja/JP）1 主題ぶん。"""
    collect = pcfg.get("collect", {})
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(presign_gnews_query(qe, pcfg))
           + f"&hl={collect.get('hl', 'ja')}&gl={collect.get('gl', 'JP')}"
           + f"&ceid={urllib.parse.quote(collect.get('ceid', 'JP:ja'))}")
    return parse_feed_items(_fetch_bytes(url, collect.get("timeout_sec", 20)))


def fetch_feed(feed: dict, pcfg: dict) -> list[dict]:
    """発行元 RSS/Atom 1 本ぶん。"""
    return parse_feed_items(_fetch_bytes(feed["url"], pcfg.get("collect", {}).get("timeout_sec", 20)))


def _assign_family(source_class: str, families_all: list[str]) -> str:
    """family_assignment: feed-industry は forecast 優先・それ以外（feed-official／gnews-media）は official 優先。"""
    prefer = "industry_forecast" if source_class == "feed-industry" else "official_notice"
    return prefer if prefer in families_all else families_all[0]


def presign_judge(text: str, qe: dict, pcfg: dict, source_class: str) -> dict | None:
    """§B の判定順。①主題語（exclude 除去後の本文に対し ASCII= 英数字境界・日本語= 部分一致）
    ②凍結語彙（全一致語を記録）③direction_required の語彙は direction_words を伴う時だけ有効
    ④negation_words を含めば type=negated（発火にしない）。

    返り値: None（主題語なし／語彙なし／方向語を欠く）または
    {type: hit|negated, matched_term, matched_vocab, matched_vocab_all, vocab_family, families_all}。
    **否定語の判定は方向語の判定より先に行う**＝type のラベル（negated 行を残すか・行なしか）を決めるだけで、
    hit になる条件〈否定語なし AND 有効語彙あり〉は順序に依らない（固定反例 4「DRAM contract price forecast
    to fall 10% QoQ」は方向語を欠くが negated として台帳に残す・方向語があっても否定語があれば negated）。
    方向語・否定語は部分一致（config の "up " 等が前提）。exclude 語は主題語の照合でだけ除く。
    """
    t = text.lower()
    hit_term = next((x for x in qe["terms"] if _presign_token_in(_strip_excludes(t, qe.get("exclude")), x)),
                    None)
    if hit_term is None:
        return None
    raw = [(fam, v) for fam, words in pcfg["vocab_families"].items() for v in words
           if _presign_token_in(t, v)]
    if not raw:
        return None
    negated = any(w.lower() in t for w in pcfg.get("negation_words", []))
    if negated:
        eff = raw
    else:
        has_dir = any(w.lower() in t for w in pcfg.get("direction_words", []))
        need = set(pcfg.get("direction_required", []))
        eff = [(fam, v) for fam, v in raw if v not in need or has_dir]
        if not eff:
            return None
    families_all: list[str] = []
    for fam, _ in eff:
        if fam not in families_all:
            families_all.append(fam)
    family = _assign_family(source_class, families_all)
    return {"type": "negated" if negated else "hit", "matched_term": hit_term,
            "matched_vocab": next(v for fam, v in eff if fam == family),
            "matched_vocab_all": [v for _, v in eff],
            "vocab_family": family, "families_all": families_all}


def load_presign_seen(path: Path) -> tuple[set[tuple[str, str]], set[str], dict]:
    """既存台帳から〈(source_id, subject, link_norm) の既出〉〈hit の event_id〉〈(source_class, subject, family)
    ごとの最新エピソード起点 known_date〉を復元する（hit/negated 行だけ・壊れ行は読み飛ばす）。

    鍵に subject を含める（2026-10-10 team-lead 裁定 b）: feed 1 記事が複数主題に当たる時は主題ごとに 1 行
    （article_id は同じ値）・便をまたぐ同一 (源, 主題, 記事) の再書き込みは防ぐ。"""
    seen, seen_events, episodes = set(), set(), {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("type") not in ("hit", "negated"):
                continue
            seen.add((r.get("source_id", ""), r.get("subject", ""), normalize_link(r.get("link", ""))))
            if r.get("type") != "hit":
                continue
            if r.get("event_id"):
                seen_events.add(r["event_id"])
            key = (r.get("source_class"), r.get("subject"), r.get("vocab_family"))
            start = (r.get("episode_id") or "")[-10:]
            if len(start) == 10 and start > episodes.get(key, ""):
                episodes[key] = start
    return seen, seen_events, episodes


def presign_episode(episodes: dict, source_class: str, subject: str, family: str,
                    known_date: str) -> tuple[str, bool]:
    """エピソード= (source_class, subject, family) ごと。境界= 初 hit の known_date から **7 日未満**
    （day0〜day6）は同一・**7 日目（day7）から新規**。起点は初 hit に固定し連鎖させない（day6 の hit で
    起点を動かさない）。Wikipedia 側（wiki_pv_watch）は「差 56 日を含む」別規則＝ここの規則とは一致させない。
    episodes は副作用で更新する。"""
    key = (source_class, subject, family)
    start = episodes.get(key)
    if start:
        delta = (date.fromisoformat(known_date) - date.fromisoformat(start)).days
        if 0 <= delta <= 6:
            return f"{source_class}:{subject}:{family}:{start}", False
    episodes[key] = known_date
    return f"{source_class}:{subject}:{family}:{known_date}", True


def run_presign(dry_run: bool = False, date_override: str | None = None) -> int:
    """presign 便: Google News ja 検索（queries）＋発行元 feed → 判定 → 台帳 presign_log.jsonl → 通知。

    dry_run= 台帳に書かず・通知もせず要約だけ出す（疎通便用）。date_override= known_date の上書き。
    収集開始日ゲート: known_date < configs/presign_forward.json の scoring.collect_interval.start なら
    「収集区間前のためスキップ」で rc=0・台帳に何も書かない（presign 便は既存 news-shock 便に同乗するため
    commit 直後の便から走る＝区間前の hit とエピソードを台帳に残さない・2026-10-10 team-lead 裁定）。
    dry-run は区間前でも走る（疎通便）。設定が読めない・キーが無い時は rc=1（runner の失敗通知に乗る）。
    終了日はゲートしない（延長判定は採点器の仕事）。
    部分障害ゲート: 失敗した source（query+feed）が全体の 1/3 超なら rc=1。
    """
    try:
        known_date = (date.fromisoformat(date_override).isoformat() if date_override
                      else datetime.now(JST).strftime("%Y-%m-%d"))
    except ValueError:
        print(f"ERROR: --date は YYYY-MM-DD で指定（受け取った値: {date_override}）", file=sys.stderr)
        return 2
    try:
        start = presign_collect_start(PRESIGN_FORWARD_CONFIG)   # 引数で渡す＝モジュール変数の差替（テスト）が効く
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"ERROR: 収集開始日が読めない（{PRESIGN_FORWARD_CONFIG} scoring.collect_interval.start）: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if date.fromisoformat(known_date) < start:
        if not dry_run:
            print(f"収集区間前のためスキップ（開始 {start.isoformat()}・known_date {known_date}）")
            return 0
        print(f"[presign dry-run] 収集区間前（開始 {start.isoformat()}）だが疎通便のため実行する")
    cfg = load_config()
    pcfg = presign_config(cfg)
    cfg_sha = __import__("hashlib").sha256(CONFIG.read_bytes()).hexdigest()[:12]
    version = pcfg.get("version")
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ledger = PRESIGN_LEDGER
    if not dry_run:
        # 多重起動ロック（shock 便の .collect.lock とは別ファイル）
        import fcntl
        ledger.parent.mkdir(parents=True, exist_ok=True)
        lock = (ledger.parent / ".presign.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("別の presign 便が実行中のためスキップ（多重起動ガード）")
            return 0
    seen, seen_events, episodes = load_presign_seen(ledger)
    queries = pcfg["queries"]
    feeds = pcfg.get("feeds", [])
    rows: list[dict] = []
    items_per_source: dict[str, int] = {}
    counts = {"q_err": 0, "f_err": 0, "hit": 0, "negated": 0, "new_ep": 0}
    first_hits: list[str] = []

    def judge_items(items: list[dict], source_class: str, source_id: str, qes: list[dict]) -> None:
        for it in items:
            text = f"{it['title']} {it['desc']}"
            link_norm = normalize_link(it["link"])
            for qe in qes:
                # identity.dedupe（裁定 b）: 同一 (source_id, subject, link_norm) は 2 度書かない。
                # 1 記事が複数主題に当たれば主題ごとに 1 行（article_id は同じ）
                key = (source_id, qe["subject"], link_norm)
                if key in seen:
                    continue
                j = presign_judge(text, qe, pcfg, source_class)
                if not j:
                    continue
                seen.add(key)
                pub_date, parsed = parse_pubdate_utc(it["pubdate"], run_at)
                eid = presign_event_id(qe["subject"], j["vocab_family"], pub_date, source_class)
                row = {"type": j["type"], "lane": PRESIGN_LANE, "run_at": run_at,
                       "known_date": known_date, "source_class": source_class,
                       "source_id": source_id, "subject": qe["subject"],
                       "matched_term": j["matched_term"], "matched_vocab": j["matched_vocab"],
                       "matched_vocab_all": j["matched_vocab_all"],
                       "vocab_family": j["vocab_family"], "families_all": j["families_all"],
                       "article_id": presign_article_id(source_id, link_norm),
                       "event_id": eid, "dup_event": eid in seen_events,
                       "episode_id": None, "first_in_episode": False,
                       "title": it["title"], "desc_snippet": text[:300], "link": it["link"],
                       "pubdate": it["pubdate"], "pubdate_utc_date": pub_date,
                       "pubdate_parsed": parsed,
                       "presign_version": version, "config_sha": cfg_sha}
                if j["type"] == "hit":
                    seen_events.add(eid)
                    ep, first = presign_episode(episodes, source_class, qe["subject"],
                                                j["vocab_family"], known_date)
                    row["episode_id"], row["first_in_episode"] = ep, first
                    counts["hit"] += 1
                    counts["new_ep"] += int(first)
                    if first:
                        first_hits.append(f"{qe['subject']}/{j['vocab_family']}: {it['title'][:40]}")
                    print(f"🔔 [{source_id}] {qe['subject']}/{j['vocab_family']}"
                          f"{'' if first else '（同一エピソード）'} | {it['title'][:60]}")
                else:
                    counts["negated"] += 1
                    print(f"   [{source_id}] negated {qe['subject']}/{j['vocab_family']} | {it['title'][:60]}")
                rows.append(row)

    for qe in queries:
        source_id = f"gnews:{qe['subject']}"
        try:
            items = fetch_presign_gnews(qe, pcfg)
        except Exception as e:                           # fail-closed: エラーも台帳に残す
            counts["q_err"] += 1
            rows.append({"type": "error", "lane": PRESIGN_LANE, "run_at": run_at,
                         "known_date": known_date, "source_class": "gnews-media",
                         "source_id": source_id, "error": str(e)[:200]})
            print(f"[{source_id}] ERROR {e}", file=sys.stderr)
            continue
        items_per_source[source_id] = len(items)
        judge_items(items, "gnews-media", source_id, [qe])
    for fd in feeds:
        source_id = fd["id"]
        subjects = fd.get("subjects") or []
        qes = queries if "*" in subjects else [q for q in queries if q["subject"] in subjects]
        try:
            items = fetch_feed(fd, pcfg)
        except Exception as e:
            counts["f_err"] += 1
            rows.append({"type": "error", "lane": PRESIGN_LANE, "run_at": run_at,
                         "known_date": known_date, "source_class": fd.get("source_class"),
                         "source_id": source_id, "error": str(e)[:200]})
            print(f"[{source_id}] ERROR {e}", file=sys.stderr)
            continue
        items_per_source[source_id] = len(items)
        judge_items(items, fd.get("source_class", "feed-official"), source_id, qes)

    summary = {"type": "run_summary", "lane": PRESIGN_LANE, "run_at": run_at, "known_date": known_date,
               "queries_planned": len(queries), "queries_ok": len(queries) - counts["q_err"],
               "feeds_planned": len(feeds), "feeds_ok": len(feeds) - counts["f_err"],
               "items": items_per_source, "hits": counts["hit"], "negated": counts["negated"],
               "new_episodes": counts["new_ep"], "presign_version": version, "config_sha": cfg_sha}
    if dry_run:
        print("[presign dry-run] 台帳・通知なし")
    else:
        with ledger.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            fh.write(json.dumps(summary, ensure_ascii=False) + "\n")
        if first_hits:        # 新規エピソードがある便だけ 1 回通知（同一エピソードの 2 件目以降は台帳のみ）
            notify(f"🔔 品薄予兆 presign: 新規エピソード {len(first_hits)}件",
                   " / ".join(first_hits[:3])[:200])
    print(json.dumps(summary, ensure_ascii=False))
    print(f"[presign done] hits={counts['hit']} negated={counts['negated']} new_episodes={counts['new_ep']} "
          f"queries_ok={summary['queries_ok']}/{len(queries)} feeds_ok={summary['feeds_ok']}/{len(feeds)}")
    n_fail, total = counts["q_err"] + counts["f_err"], len(queries) + len(feeds)
    if n_fail * 3 > total:
        print(f"ERROR: 失敗 source {n_fail}/{total}（許容= 1/3 以下）", file=sys.stderr)
        return 1
    return 0


def _selftest_presign() -> int:
    """presign 判定の固定テスト（§B 固定反例 4 件＋正例 2 件＋カバレッジ規則＋同一性・エピソード）。"""
    pcfg = presign_config()
    by = {q["subject"]: q for q in pcfg["queries"]}
    cases = [
        ("ジャパン航空が運賃の価格改定を発表", "pan", "gnews-media", None, None,
         "固定反例1: ジャパン→パン非該当（exclude 除去後に照合）"),
        ("たまごっちの抽選販売", "keiran", "gnews-media", None, None,
         "固定反例2: たまごっち→鶏卵非該当"),
        ("パンの価格改定を見送り", "pan", "gnews-media", "negated", None,
         "固定反例3: 見送り→negated（発火しない）"),
        ("DRAM contract price forecast to fall 10% QoQ", "dram", "gnews-media", "negated", None,
         "固定反例4: to fall→negated"),
        ("ナンヤテクノロジー、DRAM契約価格を最大20%引き上げへ 供給不足深刻化", "dram", "gnews-media",
         "hit", ["official_notice", "industry_forecast"],
         "正例1: 供給不足（official）＋契約価格×上げ（forecast）の両族"),
        ("HBM Supply Constraints Persist, 2027 Price Outlook Revised Upward", "dram", "feed-industry",
         "hit", ["industry_forecast"], "正例2: feed-industry は industry_forecast"),
        ("たまごっちの抽選販売", "tamagotchi", "gnews-media", "hit", ["official_notice"],
         "exclude は query ごと（たまごっち主題では発火）"),
        ("DRAM contract price forecast flat QoQ", "dram", "gnews-media", None, None,
         "方向語なしの direction_required 語彙だけでは発火しない（否定語もなし→行なし）"),
    ]
    ng = 0
    for text, subject, sc, want_type, want_fams, why in cases:
        got = presign_judge(text, by[subject], pcfg, sc)
        ok = (got is None and want_type is None) or (
            got is not None and got["type"] == want_type
            and (want_fams is None or got["families_all"] == want_fams))
        ng += not ok
        print(f"  {'OK ' if ok else 'NG '} {why}（期待={want_type}/{want_fams} 実際="
              f"{None if got is None else (got['type'], got['families_all'])}）")
    g1 = presign_judge(cases[4][0], by["dram"], pcfg, "gnews-media")
    g2 = presign_judge(cases[5][0], by["dram"], pcfg, "feed-industry")
    ok_f = g1["vocab_family"] == "official_notice" and g2["vocab_family"] == "industry_forecast"
    print(f"  {'OK ' if ok_f else 'NG '} 族の付与: gnews=official 優先／feed-industry=forecast 優先")
    ng += not ok_f
    # カバレッジ規則: presign の日本語語彙は全て検索句に含まれる（Google 検索の母集団に届く）
    jp_vocab = [v for words in pcfg["vocab_families"].values() for v in words if not v.isascii()]
    uncovered = [v for v in jp_vocab if v not in pcfg["search_or_phrases"]]
    print(f"  {'OK ' if not uncovered else 'NG '} 語彙カバレッジ: 検索句に無い日本語語彙 {uncovered or 'なし'}")
    ng += bool(uncovered)
    # 同一性: utm と fragment を除いた URL が鍵
    a = presign_article_id("gnews:dram", normalize_link("https://x/a?utm_source=t&id=1#top"))
    b = presign_article_id("gnews:dram", normalize_link("https://x/a?id=1"))
    c = presign_article_id("trendforce-semiconductors", normalize_link("https://x/a?id=1"))
    ok_i = a == b and a != c
    print(f"  {'OK ' if ok_i else 'NG '} article_id: utm/fragment を無視・別源は別 id")
    ng += not ok_i
    # エピソード: day0 起点・day6 同一・day7 は新
    eps: dict = {}
    e0 = presign_episode(eps, "gnews-media", "dram", "official_notice", "2026-10-13")
    e6 = presign_episode(eps, "gnews-media", "dram", "official_notice", "2026-10-19")
    e7 = presign_episode(eps, "gnews-media", "dram", "official_notice", "2026-10-20")
    ok_e = (e0 == ("gnews-media:dram:official_notice:2026-10-13", True)
            and e6 == ("gnews-media:dram:official_notice:2026-10-13", False)
            and e7 == ("gnews-media:dram:official_notice:2026-10-20", True))
    print(f"  {'OK ' if ok_e else 'NG '} エピソード: 7 日以内は同一・連鎖しない")
    ng += not ok_e
    # pubdate: RFC822/ISO8601 → UTC 日付・解釈不能は便の日付＋False
    run_at = "2026-10-13T00:30:00+00:00"
    p1 = parse_pubdate_utc("Tue, 13 Oct 2026 08:30:00 +0900", run_at)
    p2 = parse_pubdate_utc("2026-10-13T08:30:00+09:00", run_at)
    p3 = parse_pubdate_utc("いつか", run_at)
    ok_p = p1 == ("2026-10-12", True) and p2 == ("2026-10-12", True) and p3 == ("2026-10-13", False)
    print(f"  {'OK ' if ok_p else 'NG '} pubdate: RFC822/ISO→UTC 日付・解釈不能= 便の日付+parsed=false")
    ng += not ok_p
    # クエリ合成: 空白を含む主題語は引用符・検索句ブロック・when
    q = presign_gnews_query(by["switch2"], pcfg)
    ok_q = '"Nintendo Switch 2"' in q and "値上げ OR 価格改定" in q and q.endswith("when:2d")
    print(f"  {'OK ' if ok_q else 'NG '} クエリ合成: 引用符・検索句 OR・when（長さ{len(urllib.parse.quote(q))}）")
    ng += not ok_q
    return ng


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="通知を出さない")
    ap.add_argument("--first-seen-probe", action="store_true",
                    help="R2時計: first_seen 記録だけの対照モード（通知・受益引き当て・本線台帳なし）")
    ap.add_argument("--lane", choices=("shock", "presign"), default="shock",
                    help="shock= 供給ショック（既定・従来どおり）／presign= 品薄予兆（P-INF-43 b・別台帳 "
                         "presign_log.jsonl・--dry-run は台帳にも書かない）")
    ap.add_argument("--date", default=None,
                    help="presign 専用: known_date の上書き（YYYY-MM-DD・既定= 今日 JST）")
    args = ap.parse_args()
    if args.lane == "presign":
        # 品薄予兆レーン: ここで分岐して戻る＝以下の shock 経路には一切入らない
        if args.selftest:
            ng = _selftest_presign()
            print("OK: presign 全通過" if not ng else f"NG: presign {ng}件失敗")
            return 1 if ng else 0
        return run_presign(dry_run=args.dry_run, date_override=args.date)
    if args.date:
        print("WARN: --date は --lane presign 専用（shock 経路では無視）", file=sys.stderr)
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
    n_hit, n_err, n_stale = 0, 0, 0
    max_age = cfg.get("collect", {}).get("notify_max_age_days", NOTIFY_MAX_AGE_DAYS)
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
                age = pub_age_days(it["pubdate"], run_at)
                stale = is_stale(age, max_age)     # 古い記事は通知だけ止める（台帳は hit のまま）
                n_stale += int(stale)
                # 版とconfig指紋を発火行へ焼き込む（語彙変更後も母集団を分離再現できる・Codex指摘）
                rec = {"run_at": run_at, "term": term, "matched_term": hit_term,
                       "series_ids": qe["series_ids"],
                       "matched_vocab": v, "vocab_family": fam,
                       "event_id": eid, "dup_event": dup,
                       "title": it["title"], "link": it["link"],
                       "pubdate": it["pubdate"], "beneficiaries": bens, "status": "hit",
                       "pub_age_days": age, "stale": stale,
                       "config_version": cfg.get("version"), "config_sha": cfg_sha}
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n_hit += 1
                labels = "/".join(f"{b['code']}{'(仮)' if b['tier']=='provisional' else ''}"
                                  + (f"[{b['type_label']}]" if b["type_label"] else "")
                                  for b in bens[:4]) or "受益カードなし"
                if not dup and not stale:    # 同一事象の2件目以降・古い記事は台帳のみ・通知しない
                    hits_for_notify.append(f"{hit_term}:{v} → {labels}")
                note = ("（同一事象・通知抑止）" if dup
                        else f"（掲載から {age:.0f} 日＝古い記事・通知抑止）" if stale else "")
                print(f"🛑 [{term}] {v}{note} | {it['title'][:60]}")
                print(f"    受益: {labels}")
        # 実行サマリ行（欠測の見える化・空振りの日もこの1行は必ず残る）。
        # items= クエリ別の RSS 取得件数: 全クエリ 0件が続けば「事象が無い」でなく
        # 「Google 側の recall 崩壊」を疑う（敵対R3 A10）
        fh.write(json.dumps({"type": "run_summary", "run_at": run_at, "hit": n_hit,
                             "stale": n_stale,
                             "ok": len(cfg["queries"]) - n_err, "error": n_err,
                             "items": items_per_term,
                             "config_version": cfg.get("version"), "config_sha": cfg_sha},
                            ensure_ascii=False) + "\n")
    # 通知件数=ユニーク事象数（発火行数 n_hit ではない・Codex v2審 W2）。0件なら通知しない
    if hits_for_notify and not args.dry_run:
        notify(f"🛑 供給ショック検知: {len(hits_for_notify)}事象",
               " / ".join(hits_for_notify)[:200])
    print(f"[done] hit={n_hit}（うち古い記事で通知抑止 {n_stale}）error={n_err}/{len(cfg['queries'])}クエリ")
    # 部分障害ゲート: 4クエリ以上失敗（ok<15/18）で異常終了→runner が失敗通知（Codex指摘）
    if n_err > 3:
        print(f"ERROR: 失敗クエリ {n_err}件（許容3）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
