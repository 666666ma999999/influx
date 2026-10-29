"""銘柄言及の数えの手前で、判定器 Jev（はい確率を返す小さな判定器）が「関係ない」とした投稿だけ外す.

位置づけ（2026-10-29 新設・P-ENV-242 案 1 の③・オーナー「推奨で」＝明らかに関係ない投稿だけ外す・
オーナーの確認は 0 件で使う）。正本の説明= docs/price-watch-universe.md §16c 追記・tasks/x_mention_jev_filter.md。

決め（変えない）:
- 問い= 試しで合格した v2 の英語 2 問（~/.claude/state/wip/p-env-242/influx/trial_influx.py の QS_V2 と同文）。
  点= 2 問の高い方。線 0.15 未満だけ外す（configs/x_mention_jev.json の threshold）
- 送るのは伏せた本文だけ（URL・@名前・メール・電話）。伏せきれない物は送らずに数えに残す
- 判定器が失敗・鍵なし・時間切れ・429 → 外さない（今までどおり数える）。理由は記録に残す
- 外した投稿は消さない。日ごとに data/x_price_watch/jev_dropped/<日>.jsonl へ id・点・理由
- 判定済みの点は status_id ごとに data/x_price_watch/jev_scores.jsonl に貯める（同じ投稿を毎晩送り直さない）

Jev の呼び出しは ~/.claude/hooks/lib/jev.py（正本・標準ライブラリのみ）を読むだけで使う。本線（x_mention_extract.py）は
毎晩ホストの /usr/bin/python3 で動くため、コンテナへ渡す必要がない。コンテナ内（試験）では読めず、外さずに数える側へ倒れる。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
CONFIG = APP / "configs/x_mention_jev.json"
SCORES = APP / "data/x_price_watch/jev_scores.jsonl"
DROPPED_DIR = APP / "data/x_price_watch/jev_dropped"
FILTER_LOG = APP / "data/x_price_watch/jev_filter_log.jsonl"
MENTIONS_JEV = APP / "data/x_price_watch/mentions_jev.jsonl"

DEFAULTS = {"enabled": False, "threshold": 0.15, "timeout_sec": 10, "budget_sec": 600,
            "retry_429_wait_sec": 30, "max_consecutive_fail": 5, "jev_lib": "~/.claude/hooks/lib"}

# --- 問い（v2・試しで合格した文のまま。1 字でも変えたら QV を上げ、貯めた点を使わない） ---
QV = "v2"
NO_CLAUSE = ("Answer no for: stock price or stock index moves, market capitalisation or trading commentary "
             "(these are not prices of goods); metaphorical 'tightness' or shortage (hospitals overwhelmed, "
             "tight budgets, labour or staff shortage at a workplace); taxes or social insurance burdens; and store "
             "announcements that only say sold out, restocked, preorder open or a campaign, without any price or "
             "supply detail.")
NO_CLAUSE_V2 = NO_CLAUSE + (" Also answer no when: a price change is only proposed, hoped for or hypothetical (someone "
                            "says a company should raise prices); the post only talks about demand, orders or earnings "
                            "growth or stock picks without a supply or price fact; the writer only lists what they paid "
                            "for their own items; an individual resells a one-off item at a high price; or the writer "
                            "already has the item and only mentions in passing that it is rare or hard to get (a gift, "
                            "bought used).")
QUESTIONS = {
    "supply_price": "Does this post contain a concrete statement about the supply of goods or services "
                    "(production, shipments, inventory, procurement, investment in new factories or production "
                    "capacity, orders, delivery times, or a shortage with its cause or scale) or about an actual "
                    "change in their price (a price increase or decrease, price revision, market or spot price) for a "
                    "specific company, product, industry or market? " + NO_CLAUSE_V2,
    "shortage_voice": "Does the writer say that something people want to buy is now hard to get, sold out, out of "
                      "stock or in short supply, including old or discontinued items and equipment (for example "
                      "'I couldn't buy it', 'it was already sold out', 'hard to get overseas', 'the shelves were "
                      "empty', 'readers for old media are hard to obtain')? " + NO_CLAUSE_V2,
}

# --- 伏せ方（試しと同じ 2 段: ①URL・メール・@名前・電話を伏せる → ②残っていないか確かめ、残れば送らない） ---
_URL = re.compile(r"https?://\s?\S+|www\.\S+|\bt\.co/\S+|pic\.(?:twitter|x)\.com/\S+")
_MAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_AT = re.compile(r"[@＠][A-Za-z0-9_]{1,15}")
_TEL = re.compile(r"(?<!\d)(?:\+81[-\s]?|0)\d{1,4}[-ー－\s]?\d{1,4}[-ー－\s]?\d{3,4}(?!\d)")
_MASK2 = [
    (re.compile(r"https?://\S+", re.I), "[URL]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[MAIL]"),
    (re.compile(r"@[A-Za-z0-9_]{1,15}"), "[NAME]"),
    (re.compile(r"0\d{1,4}-\d{1,4}-\d{3,4}|\b0[789]0\d{8}\b"), "[TEL]"),
    # 試しの伏せ方の取りこぼし（2026-10-29 Codex 指摘）: 国番号つき・かっこ書きの電話
    (re.compile(r"\+\d[\d\-\s()]{8,}\d"), "[TEL]"),
    (re.compile(r"(?<!\d)0\d{1,4}\(\d{1,4}\)\d{3,4}(?!\d)"), "[TEL]"),
]
_LEFT = re.compile(r"https?://|@[A-Za-z0-9_]|[\w.+-]+@[\w-]+\.|0[789]0-?\d{4}-?\d{4}", re.I)
MAX_CHARS = 2500


def _tel(m: re.Match) -> str:
    s = m.group(0)
    d = re.sub(r"\D", "", s)
    is_tel = (10 <= len(d) <= 12 and re.search(r"[-ー－\s]", s)) or (len(d) in (10, 11) and s.startswith("0"))
    return "[TEL]" if is_tel else s


def mask_text(text: str) -> tuple[str, bool]:
    """伏せた本文と「送ってよいか」を返す。False なら送らない（伏せきれない物が残っている）。"""
    t = _URL.sub("[URL]", text or "")
    t = _MAIL.sub("[MAIL]", t)
    t = _AT.sub("[@]", t)
    t = _TEL.sub(_tel, t)[:MAX_CHARS]
    for rx, rep in _MASK2:
        t = rx.sub(rep, t)
    return t, not _LEFT.search(t)


def load_config(path: Path | None = None) -> dict:
    """設定を読む。無い・壊れている時は enabled=False（＝今までと同じ動き）。"""
    cfg = dict(DEFAULTS)
    p = path or CONFIG
    try:
        cfg.update(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        cfg["enabled"] = False
    return cfg


def load_scores(path: Path | None = None) -> dict[str, dict]:
    """貯めた点を status_id → 行 で返す（問いの版が違う行は使わない・後の行が勝つ）。"""
    out: dict[str, dict] = {}
    p = path or SCORES
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("qv") == QV and r.get("status_id") and isinstance(r.get("score"), (int, float)):
            out[str(r["status_id"])] = r
    return out


def _default_client(cfg: dict) -> Optional[Callable]:
    """~/.claude/hooks/lib/jev.py の nouls を返す。読めなければ None（＝外さずに数える）。"""
    lib = os.path.expanduser(str(cfg.get("jev_lib") or DEFAULTS["jev_lib"]))
    if not os.path.isfile(os.path.join(lib, "jev.py")):
        return None
    if lib not in sys.path:
        sys.path.insert(0, lib)
    try:
        import jev  # noqa: E402
    except Exception:  # 読めない・壊れている
        return None
    return lambda state, qs, timeout: jev.nouls(state, qs, timeout=timeout)


# 1 回で止める失敗（続けても通らない）
_STOP_AT_ONCE = {"no_key", "no_client", "http_401", "http_403", "http_429"}


class JevFilter:
    """1 回の実行の間、投稿ごとに「数えるか」を決める。点は貯め、外した物と理由を日ごとに覚える。"""

    def __init__(self, cfg: dict, client: Optional[Callable] = None, scores: Optional[dict] = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.th = float(cfg.get("threshold", DEFAULTS["threshold"]))
        self.client = client if client is not None else _default_client(cfg)
        self.cache = load_scores() if scores is None else scores
        self.sleep, self.clock = sleep, clock
        self.stop_reason: Optional[str] = None if self.client else "no_client"
        self.t0: Optional[float] = None
        self.consec_fail = 0
        self.sent = 0
        self.new_scores: list[dict] = []
        self.stats: dict[str, Counter] = defaultdict(Counter)
        self.unjudged: dict[str, Counter] = defaultdict(Counter)
        self.dropped: dict[str, list[dict]] = defaultdict(list)
        self._memo: dict[tuple[str, str], bool] = {}

    # --- 点を得る ---
    def _ask(self, masked: str) -> tuple[Optional[dict], str | dict]:
        timeout = float(self.cfg.get("timeout_sec", DEFAULTS["timeout_sec"]))
        probs, meta = self.client(masked, QUESTIONS, timeout)
        if probs is None and meta == "http_429":
            self.sleep(float(self.cfg.get("retry_429_wait_sec", DEFAULTS["retry_429_wait_sec"])))
            probs, meta = self.client(masked, QUESTIONS, timeout)
        return probs, meta

    def score(self, status_id: str, text: str) -> tuple[Optional[float], Optional[str], Optional[dict], str]:
        """(点, 点が無い理由, 2 問の確率, 出どころ) を返す。点が無い時は数えに残す。"""
        sid = str(status_id)
        if sid and sid in self.cache:
            r = self.cache[sid]
            return float(r["score"]), None, r.get("p"), "cache"
        masked, ok = mask_text(text)
        if not ok:
            return None, "mask_left", None, "-"
        if self.stop_reason:
            return None, self.stop_reason, None, "-"
        if self.t0 is None:
            self.t0 = self.clock()
        if self.clock() - self.t0 > float(self.cfg.get("budget_sec", DEFAULTS["budget_sec"])):
            self.stop_reason = "budget"
            return None, "budget", None, "-"
        try:
            probs, meta = self._ask(masked)
        except Exception as e:  # 呼び出し側の想定外でも本線を止めない
            probs, meta = None, type(e).__name__
        if probs is None or not isinstance(probs, dict) or any(
                not isinstance(probs.get(k), (int, float)) for k in QUESTIONS):
            reason = str(meta) if probs is None else "none_prob"
            self.consec_fail += 1
            if reason in _STOP_AT_ONCE:
                self.stop_reason = reason
            elif self.consec_fail >= int(self.cfg.get("max_consecutive_fail", DEFAULTS["max_consecutive_fail"])):
                self.stop_reason = f"consecutive_fail:{reason}"
            return None, reason, None, "-"
        self.consec_fail = 0
        self.sent += 1
        s = max(float(probs[k]) for k in QUESTIONS)
        rec = {"status_id": sid, "qv": QV, "p": {k: probs[k] for k in QUESTIONS}, "score": s,
               "model": meta.get("model") if isinstance(meta, dict) else None,
               "ms": meta.get("ms") if isinstance(meta, dict) else None,
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "src": "live"}
        if sid:
            self.cache[sid] = rec
            self.new_scores.append(rec)
        return s, None, rec["p"], "live"

    # --- 数えるか ---
    def keep(self, day: str, post: dict, codes: list[str]) -> bool:
        """その投稿を数えるなら True。点が線未満の時だけ False（＝外す）。1 投稿 1 回だけ決める。"""
        key = (day, str(post.get("status_id", "")) or post.get("text", "")[:60])
        if key in self._memo:
            return self._memo[key]
        s, why, p, src = self.score(post.get("status_id", ""), post.get("text", ""))
        st = self.stats[day]
        st["判定にかけた"] += 1
        if s is None:
            st["判定できず残した"] += 1
            self.unjudged[day][why or "?"] += 1
            res = True
        elif s < self.th:
            st["点が返った"] += 1
            st["外した"] += 1
            self.dropped[day].append({"date": day, "status_id": str(post.get("status_id", "")),
                                      "query_id": post.get("query_id", ""), "codes": sorted(set(codes)),
                                      "score": s, "p": p, "src": src,
                                      "reason": f"score<{self.th}（{QV} の 2 問の高い方が線未満）"})
            res = False
        else:
            st["点が返った"] += 1
            res = True
        self._memo[key] = res
        return res

    def keeper(self, day: str) -> Callable[[dict, list[str]], bool]:
        return lambda post, codes: self.keep(day, post, codes)

    # --- 書き出し ---
    def summary(self) -> dict:
        return {d: {**dict(self.stats[d]), "理由別（残した）": dict(self.unjudged[d])} for d in sorted(self.stats)}

    def flush(self, target: str, days: list[str], dry_run: bool = False) -> None:
        """点の貯めは dry-run でも書く（同じ投稿を二度送らないため）。外した記録と実行の記録は本番だけ。"""
        if self.new_scores:
            SCORES.parent.mkdir(parents=True, exist_ok=True)
            with SCORES.open("a", encoding="utf-8") as fh:
                for r in self.new_scores:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            self.new_scores = []
        if dry_run:
            return
        DROPPED_DIR.mkdir(parents=True, exist_ok=True)
        for d in days:   # その日を数え直した結果で毎回書き直す（冪等）
            rows = self.dropped.get(d, [])
            (DROPPED_DIR / f"{d}.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        FILTER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FILTER_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                 "target": target, "threshold": self.th, "qv": QV, "days": days,
                                 "sent_live": self.sent, "stop_reason": self.stop_reason,
                                 "per_day": self.summary()}, ensure_ascii=False) + "\n")


def write_mentions_jev(rows: list[dict], days: list[str]) -> None:
    """判定器を通した後の言及の台帳（数え直した日を差し替え）。規則だけの mentions.jsonl とは別に持つ。"""
    keep = []
    if MENTIONS_JEV.exists():
        for line in MENTIONS_JEV.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("date") not in days:
                    keep.append(r)
    MENTIONS_JEV.parent.mkdir(parents=True, exist_ok=True)
    with MENTIONS_JEV.open("w", encoding="utf-8") as fh:
        for r in keep + rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
