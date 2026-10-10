#!/usr/bin/env python3
"""presign_forward_eval: 品薄予兆の前向き 60 日採点（P-INF-43 b）.

事前登録（`tasks/shortage_presign_forward.md` §事前登録 C/D/E・`configs/presign_forward.json` v1）を
**読むだけ**で動く採点器。規則の正本は task と config の文言で、ここでは解釈を足さない
（曖昧だった箇所は本 docstring 末尾の「解釈メモ」に列挙）。

## 何をするか
1. ① の計器（flea_panel の固定クエリ／price_universe の系列）の台帳から、事象日ごとの状態
   〈成立(on)／不成立(off)／不明(unknown)〉の系列を作り、開始日（onset）を検出する（§C 状態モデル）
2. 発火台帳（Wikipedia 週次見張り `data/wiki_pv_watch/ledger.jsonl`・news_shock presign レーン
   `data/news_shock/presign_log.jsonl`）から母集団（known_date ∈ collect_interval の初発火エピソード）
   を取り、計器ごとの判定表 1〜5 → OR で束ねる（§D）
3. 確定した採点（pending 以外）だけを台帳 `data/shortage_presign/forward_eval.jsonl` へ追記する。
   冪等: 同じ (source_class, episode_id, spec_version) の final 行は 1 つ・事前宣言行は spec_version ごとに 1 回
4. 比較率（収集区間内の毎週火曜を疑似発火にした hit/(hit+miss)）と源 4 行の報告
   `output/shortage_presign/forward_report.md` を書く

## 使い方
    /usr/bin/python3 scripts/presign_forward_eval.py                   # 本番（台帳・報告を書く）
    /usr/bin/python3 scripts/presign_forward_eval.py --dry-run         # 何も書かず報告を標準出力へ
    /usr/bin/python3 scripts/presign_forward_eval.py --as-of 2026-12-20
標準ライブラリのみ・ホスト /usr/bin/python3（3.9）で動く。テストは Docker（tests/test_presign_forward_eval.py）。

## 解釈メモ（task/config の文言で決まらなかった箇所・実装の選び方）
- flea の `window` が無い行（2026-09-22 の初便 6 行）は事象日を決められないので読まない
- 状態系列の事象日は「その計器の行がある日」だけ（行が無い日は系列に現れない＝不明とも数えない）
- 規則 2/2b（v1 明確化 2026-10-10・初便前）: (K, K+60] の**最初の成立**の開始を見る。開始が onset（開始日あり）なら hit、
  開始が onset_unknown（lookback 全不明＝開始日を特定できない成立）なら confirm_after（instrument_verdicts.onset_unknown=true・
  ① が先の可能性を否定できないため hit/miss の分母外）。開始が K 以前の onset で規則 1 を飛ばして届いた角は confirm_after（rule=2-prior）。
  規則 1 後半の「onset ∈ [K−28, K]」は開始日ありの onset だけ（onset_unknown は 2b で拾う）。
  (K, K+60] に onset_unknown があった事実は instrument_verdicts.onset_unknown_in_window にも残す
- 判定 1 の lookback は [K−L, K]（K を含む）・onset ∈ [K−28, K] は両端を含む・(K, K+60] は K を含まず K+60 を含む
- lookback が全て不明（判定可能日ゼロ）の時は判定 1 を前半・後半とも飛ばし lookback_partial=true
- 「判定可能観測が十分」= (K, K+60] の事象日のうち状態が on/off の件数（daily ≥42・weekly ≥6・monthly ≥2）
- 延長判定の N は母集団から unscorable（計器なし）を除いた件数（pending は含む）
- 比較率の行（type=comparison）は主題ごと・確定数（hit/miss/confirm_after/unobserved）か確定した火曜の集合が
  前回行から変わった時だけ追記する（毎便の上書きはしない）
- 採点日 as_of より後に観測された行（flea の date・pu の date）と known_date > as_of の発火は見ない（point-in-time）
- 束ね（v1 明確化 2026-10-10・Codex 実装レビュー）: いずれかの計器が hit → hit（開始日が最も早い計器）／それ以外で
  pending が 1 つでもあれば pending（未確定・台帳に書かない）／全計器が確定した後は confirm_after > miss > unobserved
- 発火行の score_eligible は鮮度（遅着でないか）だけの印として読み、収集区間（延長を含む）に入るかは本採点器が
  known_date で判定する（wiki 側で区間を AND すると延長期間の発火が落ちるため・同レビュー ①）
- onset_rule.pu_weekly / pu_monthly は同じ台帳・同じ id 列を読む契約（source と id_field が食い違えば ValueError で止める）
- 本書きの時だけ `data/shortage_presign/.eval.lock` の排他ロック（別の採点が走っていれば「スキップ」を印字して exit 0・
  dry-run はロックしない）。runner（wiki_pv_watch_run.sh 末尾）の多重起動と手動実行の衝突を防ぐ
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))

DEFAULT_CONFIG = APP / "configs/presign_forward.json"
DEFAULT_WIKI_CONFIG = APP / "configs/wiki_pv_watch.json"
DEFAULT_PU_SOURCES = APP / "configs/price_universe_sources.json"

ON, OFF, UNK = "on", "off", "unknown"
STATE_JA = {ON: "成立", OFF: "不成立", UNK: "不明"}
# 束ね（v1 明確化 2026-10-10・Codex 統合レビュー質問 3 の裁定）: いずれかの計器が hit → hit（確定）／
# それ以外でいずれかが pending → pending（未確定・後日 hit に変わり得るため confirm_after でも確定しない）／
# 全計器が確定した後の優先= confirm_after > miss > unobserved
FINAL_PRIORITY = {"confirm_after": 0, "miss": 1, "unobserved": 2}
VERDICTS = ["hit", "miss", "confirm_after", "pending", "unobserved", "unscorable"]
# 判定表 4「判定可能観測が十分」= config scoring.verdict_table_per_instrument[3] の文言を数値化
SUFFICIENT_OBS = {"daily": 42, "weekly": 6, "monthly": 2}
SOURCE_ORDER = ["wiki", "feed-official", "feed-industry", "gnews-media"]
EXTENSION_END_FALLBACK = "2027-02-10"  # config scoring.extension.rule の文言（正規表現で拾えない時の既定）
EPS = 1e-9


# ---------------------------------------------------------------- 小道具
def to_date(s) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def iso(x: Optional[date]) -> Optional[str]:
    return x.isoformat() if x else None


def read_jsonl(path: Optional[Path]) -> List[dict]:
    if path is None or not Path(path).exists():
        return []
    out: List[dict] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def median(vals: List[float]) -> float:
    return float(statistics.median(vals))


def ge(a: float, b: float) -> bool:
    """a ≥ b（浮動小数の丸めで 1.2×100=120.00000000000001 が落ちないよう相対 EPS を許す）."""
    return a >= b - EPS * max(1.0, abs(b))


def is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and not (isinstance(x, float) and math.isnan(x))


def wilson(h: int, n: int, z: float = 1.96) -> Optional[Tuple[float, float]]:
    """Wilson 95% 区間（統計ライブラリ不要の自前実装）."""
    if n <= 0:
        return None
    p = h / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def sha12(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    except OSError:
        return None


def tuesdays_between(start: date, end_exclusive: date) -> List[date]:
    t = start + timedelta(days=(1 - start.weekday()) % 7)  # weekday(): 月=0・火=1
    out = []
    while t < end_exclusive:
        out.append(t)
        t += timedelta(days=7)
    return out


def now_jst_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 状態系列
class Series:
    """1 計器の事象日ごとの状態〈on/off/unknown〉と onset."""

    def __init__(self, key: str, cadence: str):
        self.key = key
        self.cadence = cadence
        self.dates: List[date] = []
        self.states: Dict[date, str] = {}
        self.observed: Dict[date, date] = {}
        self.onsets: List[date] = []
        self.onset_unknown: List[date] = []

    def set(self, t: date, state: str, observed: date) -> None:
        self.states[t] = state
        self.observed[t] = observed

    def finalize(self, lookback_days: int) -> "Series":
        """onset= 成立の日 t で、直前 lookback 内の観測に成立が無く不成立が 1 つ以上ある日。
        lookback が全て不明（観測なしを含む）なら onset_unknown。成立が続く間は同一の ①."""
        self.dates = sorted(self.states)
        self.onsets, self.onset_unknown = [], []
        for t in self.dates:
            if self.states[t] != ON:
                continue
            lo = t - timedelta(days=lookback_days)
            lb = [self.states[u] for u in self.dates if lo <= u < t]
            if ON in lb:
                continue
            (self.onsets if OFF in lb else self.onset_unknown).append(t)
        return self

    def between(self, lo: date, hi: date, include_lo: bool = True, include_hi: bool = True) -> List[date]:
        return [u for u in self.dates
                if (lo <= u if include_lo else lo < u) and (u <= hi if include_hi else u < hi)]

    def judgeable_between(self, lo: date, hi: date, include_lo: bool = True, include_hi: bool = True) -> List[date]:
        return [u for u in self.between(lo, hi, include_lo, include_hi) if self.states[u] != UNK]

    def counts(self) -> Dict[str, int]:
        c = Counter(self.states.values())
        return {ON: c.get(ON, 0), OFF: c.get(OFF, 0), UNK: c.get(UNK, 0)}

    def first_judgeable(self) -> Optional[date]:
        for u in self.dates:
            if self.states[u] != UNK:
                return u
        return None

    def summary(self) -> dict:
        c = self.counts()
        return {
            "instrument": self.key, "cadence": self.cadence,
            "first": iso(self.dates[0]) if self.dates else None,
            "last": iso(self.dates[-1]) if self.dates else None,
            "on": c[ON], "off": c[OFF], "unknown": c[UNK],
            "first_judgeable": iso(self.first_judgeable()),
            "onsets": [iso(t) for t in self.onsets],
            "onset_unknown": [iso(t) for t in self.onset_unknown],
        }


def build_flea_series(rows: List[dict], qid: str, as_of: date, rule: dict, ratio: float,
                      list_price: Optional[float] = None, list_ratio: float = 1.2) -> Series:
    """flea_panel: 事象日= window・観測可能日= date。x= sold_median（sold_n_top100 ≥ min_n の status=ok 行）。
    m_t= t−(smooth_days−1)〜t の x の中央値（最低 smooth_min_values）。
    b_t= t+baseline_window[0]〜t+baseline_window[1] の x の中央値（最低 baseline_min_values）。
    成立 iff m_t ≥ ratio×b_t（定価がある query は m_t ≥ list_ratio×定価）。m か b が無い日は不明."""
    s = Series(f"flea:{qid}", rule.get("cadence", "daily"))
    ev_f = rule.get("event_date_field", "window")
    ob_f = rule.get("observed_date_field", "date")
    val_f = rule.get("value_field", "sold_median")
    n_f = rule.get("min_n_field", "sold_n_top100")
    min_n = rule.get("min_n", 3)
    smooth = int(rule.get("smooth_days", 7))
    smooth_min = int(rule.get("smooth_min_values", 4))
    b_lo, b_hi = rule.get("baseline_window_days", [-63, -8])
    b_min = int(rule.get("baseline_min_values", 14))
    xs: Dict[date, Optional[float]] = {}
    obs: Dict[date, date] = {}
    for r in rows:
        t = to_date(r.get(ev_f))
        o = to_date(r.get(ob_f))
        if t is None or o is None or o > as_of:
            continue  # window の無い行（初便）・採点日より後に観測された行は読まない
        ok = r.get("status") == "ok" and is_num(r.get(n_f)) and r.get(n_f) >= min_n and is_num(r.get(val_f))
        xs[t] = float(r[val_f]) if ok else None  # 同一事象日が複数あれば後の行で上書き
        obs[t] = o
    days = sorted(xs)
    for t in days:
        m_lo = t - timedelta(days=smooth - 1)
        m_vals = [xs[u] for u in days if m_lo <= u <= t and xs[u] is not None]
        state = UNK
        if len(m_vals) >= smooth_min:
            m = median(m_vals)
            if list_price:
                state = ON if ge(m, list_ratio * float(list_price)) else OFF
            else:
                lo, hi = t + timedelta(days=int(b_lo)), t + timedelta(days=int(b_hi))
                b_vals = [xs[u] for u in days if lo <= u <= hi and xs[u] is not None]
                if len(b_vals) >= b_min:
                    state = ON if ge(m, ratio * median(b_vals)) else OFF
        s.set(t, state, obs[t])
    return s


def build_pu_weekly_series(rows: List[dict], sid: str, as_of: date, rule: dict, ratio: float) -> Series:
    """price_universe 週次: status=ok 行・同一 (id,date) は最後の行。x= value。
    b= date が t+baseline_window[0]〜t+baseline_window[1] の値の中央値（最低 baseline_min_values 観測）。
    成立 iff x ≥ ratio×b。b が無い日・value が数値でない日は不明."""
    s = Series(f"pu:{sid}", "weekly")
    want_status = rule.get("status", "ok")
    ev_f = rule.get("event_date_field", "date")
    val_f = rule.get("value_field", "value")
    b_lo, b_hi = rule.get("baseline_window_days", [-63, -8])
    b_min = int(rule.get("baseline_min_values", 4))
    last: Dict[date, dict] = {}
    for r in rows:
        if r.get("status") != want_status:
            continue
        t = to_date(r.get(ev_f))
        if t is None or t > as_of:
            continue
        last[t] = r
    days = sorted(last)
    xs = {t: (float(last[t][val_f]) if is_num(last[t].get(val_f)) else None) for t in days}
    for t in days:
        state = UNK
        if xs[t] is not None:
            lo, hi = t + timedelta(days=int(b_lo)), t + timedelta(days=int(b_hi))
            b_vals = [xs[u] for u in days if lo <= u <= hi and xs[u] is not None]
            if len(b_vals) >= b_min:
                state = ON if ge(xs[t], ratio * median(b_vals)) else OFF
        s.set(t, state, t)
    return s


def build_pu_monthly_series(rows: List[dict], sid: str, as_of: date, rule: dict) -> Series:
    """price_universe 月次: status=ok 行・同一 (id, src_date) は最初に観測した行（その date が事象日）。
    成立 iff yoy_pct ≥ threshold_pct。null は不明."""
    s = Series(f"pu:{sid}", "monthly")
    want_status = rule.get("status", "ok")
    ev_f = rule.get("event_date_field", "date")
    val_f = rule.get("value_field", "yoy_pct")
    thr = float(rule.get("threshold_pct", 20.0))
    ordered = []
    for r in rows:
        if r.get("status") != want_status:
            continue
        t = to_date(r.get(ev_f))
        if t is None or t > as_of or r.get("src_date") in (None, ""):
            continue
        ordered.append((t, str(r.get("run_at") or ""), r))
    ordered.sort(key=lambda x: (x[0], x[1]))
    first: Dict[str, Tuple[date, dict]] = {}
    for t, _, r in ordered:
        src = str(r["src_date"])
        if src not in first:
            first[src] = (t, r)
    for _src, (t, r) in first.items():
        y = r.get(val_f)
        state = UNK if not is_num(y) else (ON if ge(float(y), thr) else OFF)
        s.set(t, state, t)
    return s


# ---------------------------------------------------------------- 判定表と束ね
def judge_instrument(series: Series, K: date, as_of: date, p: dict) -> dict:
    """計器ごとの判定表（config scoring.verdict_table_per_instrument 1〜5 の順に評価）."""
    L = int(p["lookback"][series.cadence])
    H = int(p["horizon"])
    C = int(p["confirm_lookback"])
    G = int(p["grace"])
    need = int(p["sufficient"][series.cadence])
    win_lo, win_hi = K, K + timedelta(days=H)  # (K, K+H]
    out = {
        "instrument": series.key, "cadence": series.cadence, "verdict": None, "rule": None,
        "onset_date": None, "lead_days": None, "detect_lag_days": None, "onset_unknown": False,
        "lookback_partial": False, "judgeable_in_window": 0, "onset_unknown_in_window": [],
    }
    lb = series.judgeable_between(K - timedelta(days=L), K)  # [K−L, K]
    out["lookback_partial"] = not lb
    out["judgeable_in_window"] = len(series.judgeable_between(win_lo, win_hi, include_lo=False))
    out["onset_unknown_in_window"] = [iso(t) for t in series.onset_unknown if win_lo < t <= win_hi]
    if lb:  # 1. lookback が全て不明なら飛ばす（lookback_partial=true）
        last = lb[-1]
        if series.states[last] == ON:
            prior = [t for t in series.onsets + series.onset_unknown if t <= last]
            out.update(verdict="confirm_after", rule="1a", onset_date=iso(max(prior)) if prior else None)
            return out
        conf = [t for t in series.onsets if K - timedelta(days=C) <= t <= K]
        if conf:
            out.update(verdict="confirm_after", rule="1b", onset_date=iso(conf[-1]))
            return out
    # 2 / 2b: (K, K+60] の「最初の成立」の開始で分ける（成立が続く間は同一の ①＝開始は直近の onset/onset_unknown）
    first_on = next((t for t in series.between(win_lo, win_hi, include_lo=False) if series.states[t] == ON), None)
    if first_on is not None:
        starts = [t for t in series.onsets + series.onset_unknown if t <= first_on]
        start = max(starts) if starts else first_on
        if start not in series.onsets:  # 2b. 開始が onset_unknown（lookback 全不明＝開始日を特定できない成立）
            out.update(verdict="confirm_after", rule="2b", onset_date=iso(start), onset_unknown=True)
            return out
        if start > K:  # 2. 開始日ありの onset が窓内
            out.update(verdict="hit", rule="2", onset_date=iso(start), lead_days=(start - K).days,
                       detect_lag_days=(series.observed[start] - start).days)
            return out
        # 開始日が K 以前の成立が窓まで続いている（規則 1 を飛ばした時にだけ届く角）→ ① が先
        out.update(verdict="confirm_after", rule="2-prior", onset_date=iso(start))
        return out
    if as_of < K + timedelta(days=H):  # 3.
        out.update(verdict="pending", rule="3")
        return out
    if out["judgeable_in_window"] >= need:  # 4.
        out.update(verdict="miss", rule="4")
        return out
    if as_of < K + timedelta(days=H + G):  # 5.
        out.update(verdict="pending", rule="5")
        return out
    out.update(verdict="unobserved", rule="5")
    return out


def combine_verdicts(iv: Dict[str, dict]) -> dict:
    """OR 束ね: いずれかが hit → hit／それ以外でいずれかが pending → pending／全確定後は
    confirm_after > miss > unobserved。hit（confirm_after）の onset は最も早い計器のもの."""
    if not iv:
        return {"verdict": "unscorable", "final": True, "onset_date": None, "instrument": None,
                "lead_days": None, "detect_lag_days": None, "onset_unknown": False, "lookback_partial": False}
    hits = [v for v in iv.values() if v["verdict"] == "hit"]
    if hits:
        verdict, chosen = "hit", min(hits, key=lambda v: v["onset_date"])
    elif any(v["verdict"] == "pending" for v in iv.values()):
        verdict, chosen = "pending", next(v for v in iv.values() if v["verdict"] == "pending")
    else:
        verdict = min((v["verdict"] for v in iv.values()), key=lambda x: FINAL_PRIORITY[x])
        chosen = min((v for v in iv.values() if v["verdict"] == verdict),
                     key=lambda v: (v["onset_date"] is None, v["onset_date"] or ""))
    pick = verdict in ("hit", "confirm_after")
    return {
        "verdict": verdict, "final": verdict != "pending",
        "onset_date": chosen["onset_date"] if pick else None,
        "instrument": chosen["instrument"] if pick else None,
        "lead_days": chosen["lead_days"] if verdict == "hit" else None,
        "detect_lag_days": chosen["detect_lag_days"] if verdict == "hit" else None,
        "onset_unknown": bool(chosen.get("onset_unknown")) if verdict == "confirm_after" else False,
        "lookback_partial": any(v["lookback_partial"] for v in iv.values()),
    }


# ---------------------------------------------------------------- 採点器
class Scorer:
    def __init__(self, cfg: dict, wiki_cfg: dict, pu_sources: dict,
                 flea_rows: List[dict], pu_rows: List[dict], as_of: date):
        self.cfg = cfg
        self.as_of = as_of
        sm = cfg.get("state_model", {})
        self.ratio = float(sm.get("ratio", 1.2))
        self.lookback = dict(sm.get("onset_lookback_days", {"daily": 14, "weekly": 14, "monthly": 45}))
        sc = cfg["scoring"]
        self.params = {
            "lookback": self.lookback, "horizon": sc.get("horizon_days", 60),
            "confirm_lookback": sc.get("confirm_lookback_days", 28), "grace": sc.get("grace_days", 14),
            "sufficient": SUFFICIENT_OBS,
        }
        onset = cfg.get("onset_rule", {})
        self.flea_rule = onset.get("flea", {})
        self.pu_weekly_rule = onset.get("pu_weekly", {})
        self.pu_monthly_rule = onset.get("pu_monthly", {})
        self.list_prices = dict(cfg.get("list_prices") or {})
        self.list_ratio = float(onset.get("flea_list_price", {}).get("ratio", self.ratio))
        self.cadence: Dict[str, str] = dict(onset.get("pu_cadence", {}))
        for s in (pu_sources or {}).get("series", []):  # config に無い系列だけ sources の cadence で補う
            if s.get("id") and s["id"] not in self.cadence:
                self.cadence[s["id"]] = "monthly" if s.get("cadence") == "monthly" else "weekly"
        self.flea_by: Dict[str, List[dict]] = defaultdict(list)
        for r in flea_rows:
            if r.get("query_id"):
                self.flea_by[r["query_id"]].append(r)
        # 系列 id の列名は config の id_field を消費する（週次・月次は同じ台帳＝同じ列名の固定契約・違えば拒否）
        id_w = self.pu_weekly_rule.get("id_field", "id")
        id_m = self.pu_monthly_rule.get("id_field", id_w)
        if id_w != id_m:
            raise ValueError(f"onset_rule.pu_weekly.id_field={id_w!r} と pu_monthly.id_field={id_m!r} が違う（v1 は同一台帳の契約）")
        self.pu_id_field = id_w
        self.pu_by: Dict[str, List[dict]] = defaultdict(list)
        for r in pu_rows:
            if r.get(self.pu_id_field):
                self.pu_by[str(r[self.pu_id_field])].append(r)
        self.wiki_overrides: Dict[str, dict] = {
            t["title"]: t["instruments_override"]
            for t in (wiki_cfg or {}).get("titles", []) if t.get("title") and t.get("instruments_override")
        }
        self._cache: Dict[str, Series] = {}

    # --- 計器
    def series(self, key: str) -> Series:
        if key in self._cache:
            return self._cache[key]
        kind, _, sid = key.partition(":")
        if kind == "flea":
            s = build_flea_series(self.flea_by.get(sid, []), sid, self.as_of, self.flea_rule, self.ratio,
                                  self.list_prices.get(sid), self.list_ratio)
        elif self.cadence.get(sid, "weekly") == "monthly":
            s = build_pu_monthly_series(self.pu_by.get(sid, []), sid, self.as_of, self.pu_monthly_rule)
        else:
            s = build_pu_weekly_series(self.pu_by.get(sid, []), sid, self.as_of, self.pu_weekly_rule, self.ratio)
        s.finalize(int(self.lookback.get(s.cadence, 14)))
        self._cache[key] = s
        return s

    @staticmethod
    def _keys(spec: dict) -> List[str]:
        return [f"flea:{q}" for q in (spec.get("flea") or [])] + [f"pu:{p}" for p in (spec.get("pu") or [])]

    def subject_instruments(self, subject: Optional[str]) -> Optional[List[str]]:
        sub = self.cfg.get("subjects", {}).get(subject or "")
        return None if sub is None else self._keys(sub)

    def fire_instruments(self, fire: dict) -> Tuple[Optional[List[str]], str]:
        if fire.get("source_class") == "wiki" and fire.get("title") in self.wiki_overrides:
            return self._keys(self.wiki_overrides[fire["title"]]), "instruments_override"
        return self.subject_instruments(fire.get("subject")), "subject"

    # --- 採点
    def score(self, instruments: List[str], K: date) -> dict:
        iv = {k: judge_instrument(self.series(k), K, self.as_of, self.params) for k in instruments}
        out = combine_verdicts(iv)
        out["instrument_verdicts"] = iv
        return out


# ---------------------------------------------------------------- 発火の読み込み
def load_fires(wiki_rows: List[dict], rss_rows: List[dict]) -> Tuple[List[dict], Dict[str, dict]]:
    """wiki: type=fire ∧ first_in_episode（score_eligible= 収集器の鮮度条件 week_end ≥ known_date−8 をフラグで持ち回る）／
    rss: type=hit ∧ first_in_episode（score_eligible は常に真）。
    **収集区間（延長込み）の判定は採点器が行う**（Codex 統合レビュー Critical #1・v1 明確化 2026-10-10）＝
    収集器の score_eligible に区間を含めない。件数突合のため台帳の行数・絞り込み段階ごとの件数を tally に残す."""
    fires: List[dict] = []
    tally: Dict[str, dict] = defaultdict(lambda: {"rows": 0, "first_in_episode": 0, "score_eligible": 0,
                                                 "deduped": 0})
    seen = set()
    for r in wiki_rows:
        if r.get("type") != "fire":
            continue
        t = tally["wiki"]
        t["rows"] += 1
        if not r.get("first_in_episode"):
            continue
        t["first_in_episode"] += 1
        eligible = bool(r.get("score_eligible"))
        if eligible:
            t["score_eligible"] += 1
        key = ("wiki", r.get("episode_id"))
        if key in seen or not r.get("episode_id"):
            continue
        seen.add(key)
        if eligible:
            t["deduped"] += 1
        fires.append({"source_class": "wiki", "episode_id": r["episode_id"], "subject": r.get("subject"),
                      "title": r.get("title"), "known_date": r.get("known_date"), "score_eligible": eligible})
    for r in rss_rows:
        if r.get("type") != "hit":
            continue
        sc = r.get("source_class") or "unknown"
        t = tally[sc]
        t["rows"] += 1
        if not r.get("first_in_episode"):
            continue
        t["first_in_episode"] += 1
        t["score_eligible"] += 1  # rss には score_eligible の概念が無い＝first_in_episode と同数
        key = (sc, r.get("episode_id"))
        if key in seen or not r.get("episode_id"):
            continue
        seen.add(key)
        t["deduped"] += 1
        fires.append({"source_class": sc, "episode_id": r["episode_id"], "subject": r.get("subject"),
                      "title": None, "known_date": r.get("known_date"), "score_eligible": True})
    return fires, dict(tally)


def in_population(fire: dict, iv: dict, as_of: date) -> bool:
    """採点対象= 鮮度 OK（score_eligible）かつ known_date ∈ その源の収集区間 [start, end)（延長込み）かつ known_date ≤ as_of."""
    kd = to_date(fire.get("known_date"))
    return bool(fire.get("score_eligible", True)) and kd is not None and iv["start"] <= kd < iv["end"] and kd <= as_of


def extension_end(cfg: dict) -> date:
    """延長の終端（排他）。構造化キー scoring.extension.end_exclusive を正とし、無い時だけ rule 文の日付→定数（validator 質問 1・2026-10-10）."""
    ext = cfg["scoring"].get("extension", {})
    structured = to_date(ext.get("end_exclusive"))
    if structured is not None:
        return structured
    m = re.search(r"(\d{4}-\d{2}-\d{2})", str(ext.get("rule", "")))
    return to_date(m.group(1)) if m else to_date(EXTENSION_END_FALLBACK)


def source_intervals(cfg: dict, fires: List[dict], as_of: date, scorer: Scorer) -> Dict[str, dict]:
    """源ごとの収集区間。decide_on 以降は N（unscorable を除く・pending 含む）< min_scored の源だけ延長."""
    sc = cfg["scoring"]
    start = to_date(sc["collect_interval"]["start"])
    base_end = to_date(sc["collect_interval"]["end_exclusive"])
    decide_on = to_date(sc.get("extension", {}).get("decide_on")) or base_end
    ext_end = extension_end(cfg)
    min_scored = int(sc.get("pass_line", {}).get("min_scored", 5))
    out: Dict[str, dict] = {}
    classes = list(SOURCE_ORDER) + sorted({f["source_class"] for f in fires} - set(SOURCE_ORDER))
    base_iv = {"start": start, "end": base_end}
    for cls in classes:
        n_base = 0
        for f in fires:
            if f["source_class"] != cls or not in_population(f, base_iv, as_of):
                continue
            inst, _ = scorer.fire_instruments(f)
            if inst:
                n_base += 1
        decided = as_of >= decide_on
        extended = decided and n_base < min_scored
        out[cls] = {"start": start, "end": ext_end if extended else base_end, "base_end": base_end,
                    "decided": decided, "extended": extended, "n_base": n_base, "decide_on": decide_on,
                    "ext_end": ext_end}
    return out


# ---------------------------------------------------------------- 台帳
def load_ledger(path: Path, spec_version: int) -> dict:
    rows = read_jsonl(path)
    final: Dict[Tuple[str, str], dict] = {}
    comparison_latest: Dict[Tuple[str, str], dict] = {}
    prereg = False
    for r in rows:
        if r.get("spec_version") != spec_version:
            continue
        t = r.get("type")
        if t == "preregistration":
            prereg = True
        elif t == "score" and r.get("final"):
            final[(r.get("source_class"), r.get("episode_id"))] = r
        elif t == "comparison":
            comparison_latest[(r.get("subject"), r.get("interval_end"))] = r
    return {"rows": rows, "prereg": prereg, "final": final, "comparison_latest": comparison_latest}


def preregistration_row(cfg: dict, config_path: Path, spec_version: int) -> dict:
    sc = cfg["scoring"]
    return {
        "type": "preregistration", "spec_version": spec_version, "registered_at": now_jst_iso(),
        "config_path": str(config_path), "config_version": cfg.get("version"), "config_sha": sha12(config_path),
        "fixed_at": cfg.get("fixed_at"),
        "state_model": cfg.get("state_model"),
        "onset_rule": {k: v for k, v in cfg.get("onset_rule", {}).items() if k != "pu_cadence"},
        "pu_cadence": cfg.get("onset_rule", {}).get("pu_cadence"),
        "list_prices": cfg.get("list_prices"),
        "scoring": {
            "horizon_days": sc.get("horizon_days"), "confirm_lookback_days": sc.get("confirm_lookback_days"),
            "grace_days": sc.get("grace_days"), "collect_interval": sc.get("collect_interval"),
            "extension": sc.get("extension"), "verdict_table_per_instrument": sc.get("verdict_table_per_instrument"),
            "combine_instruments": sc.get("combine_instruments"), "state_transitions": sc.get("state_transitions"),
            "comparison_rate": sc.get("comparison_rate"), "pass_line": sc.get("pass_line"),
            "sufficient_obs": SUFFICIENT_OBS,
        },
        "subjects_n": len(cfg.get("subjects", {})),
        "note": "tasks/shortage_presign_forward.md §事前登録 C/D の写し。結果を見てから変えない（変更は version を上げる）",
    }


# ---------------------------------------------------------------- 集計
def aggregate(episodes: List[dict], comparisons: Dict[Tuple[Tuple[str, ...], str], dict],
              end_for: Dict[str, str]) -> dict:
    c = Counter(e["verdict"] for e in episodes)
    H, M = c.get("hit", 0), c.get("miss", 0)
    scored = H + M
    leads = sorted(e["lead_days"] for e in episodes if e["verdict"] == "hit" and e.get("lead_days") is not None)
    onsets = {(e.get("instrument"), e.get("onset_date")) for e in episodes if e["verdict"] == "hit"}
    comp_rates = []
    for e in episodes:
        if e["verdict"] == "unscorable" or not e.get("instruments"):
            continue
        cr = comparisons.get((tuple(e["instruments"]), end_for.get(e["source_class"])))
        if cr and cr.get("rate") is not None:
            comp_rates.append(cr["rate"])
    comp = sum(comp_rates) / len(comp_rates) if comp_rates else None
    return {
        "N": len(episodes), **{v: c.get(v, 0) for v in VERDICTS},
        "confirm_after_onset_unknown": sum(1 for e in episodes
                                           if e["verdict"] == "confirm_after" and e.get("onset_unknown")),
        "scored": scored,
        "hit_rate": (H / scored) if scored else None,
        "miss_rate": (M / scored) if scored else None,
        "lead_median": (median(leads) if leads else None),
        "comparison": comp, "comparison_n": len(comp_rates),
        "wilson": wilson(H, scored),
        "distinct_onsets": len(onsets),
        "subjects": len({e.get("subject") for e in episodes}),
        "subjects_scored": len({e.get("subject") for e in episodes if e["verdict"] in ("hit", "miss")}),
    }


def pass_line(stats: dict, pl: dict) -> str:
    min_scored = int(pl.get("min_scored", 5))
    if stats["scored"] < min_scored:
        return f"判定不能（H+M={stats['scored']} < {min_scored}）"
    if stats["comparison"] is None:
        return "判定不能（比較率なし）"
    # 浮動小数の境界（例 7/10 − 1/2 = 0.19999999999999996）を落とさないよう状態判定と同じ ge() を使う
    ok = (ge(stats["hit_rate"], float(pl.get("min_hit_rate", 0.5)))
          and ge(stats["hit_rate"] - stats["comparison"], float(pl.get("min_margin_over_comparison", 0.2))))
    return "合格（運用継続の便宜基準）" if ok else "不合格"


# ---------------------------------------------------------------- 報告
def fmt_pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def fmt_wilson(w) -> str:
    return "—" if not w else f"[{100 * w[0]:.0f}%, {100 * w[1]:.0f}%]"


def fmt(x) -> str:
    return "—" if x is None else str(x)


def stats_row(label: str, s: dict, pl: Optional[dict] = None) -> str:
    cells = [label, s["N"], s["hit"], s["miss"], s["confirm_after"], s["pending"], s["unobserved"], s["unscorable"],
             fmt_pct(s["hit_rate"]), fmt_pct(s["miss_rate"]), fmt(s["lead_median"]),
             fmt_pct(s["comparison"]), fmt_wilson(s["wilson"]), s["distinct_onsets"], s["subjects"]]
    if pl is not None:
        cells.append(pass_line(s, pl))
    return "| " + " | ".join(str(c) for c in cells) + " |"


STATS_HEADER = ("| {} | N | hit | miss | confirm_after | pending | unobserved | unscorable | 的中率 | 空振り率 "
                "| 先行日数中央値 | 比較率 | Wilson95 | distinct onset | 主題数 |{}\n"
                "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|--:|--:|{}")


def build_report(ctx: dict) -> str:
    cfg = ctx["cfg"]
    sc = cfg["scoring"]
    pl = sc.get("pass_line", {})
    lines: List[str] = []
    a = lines.append
    a("# 品薄予兆の前向き採点（P-INF-43 b）— 60 日採点レポート")
    a("")
    a(f"- 採点日 as_of: **{iso(ctx['as_of'])}**（JST）／生成: {ctx['scored_at']}／spec_version {ctx['spec_version']}"
      f"／config `{ctx['config_path']}` sha {ctx['config_sha']}")
    a(f"- 収集区間: known_date ∈ [{sc['collect_interval']['start']}, {sc['collect_interval']['end_exclusive']})"
      f"／採点= known_date+{sc.get('horizon_days')} 日・遅着猶予 {sc.get('grace_days')} 日")
    a(f"- 台帳: `{ctx['ledger_path']}`" + ("（**dry-run: 書いていない**）" if ctx["dry_run"] else
                                             f"（追記: 事前宣言 {int(ctx['written']['preregistration'])}・"
                                             f"確定 {ctx['written']['scores']}・比較率 {ctx['written']['comparisons']}）"))
    a("- 合格線（源ごと）: (H+M) ≥ {} かつ 的中率 ≥ {:.0%} かつ 的中率−比較率 ≥ {:.0f} pt → 合格＝"
      "**見張りを続ける運用上の便宜基準であって有効性の証明ではない**（N=5 では偶然でも 3/5 以上が ≈32%）".format(
          pl.get("min_scored", 5), float(pl.get("min_hit_rate", 0.5)),
          100 * float(pl.get("min_margin_over_comparison", 0.2))))
    a("")
    # 1. 鮮度
    a("## 1. 入力台帳の鮮度")
    a("")
    a("| 台帳 | 行数 | 最終日 | 備考 |")
    a("|---|--:|---|---|")
    for f in ctx["freshness"]:
        a(f"| {f['name']} | {f['rows']} | {fmt(f['last'])} | {f.get('note', '')} |")
    a("")
    for s in ctx.get("run_summaries", []):  # 収集便が回っているかの証拠（未実行なら明示）
        a(f"- {s}")
    a("")
    # 2. 件数突合
    a("## 2. 母集団（発火）の件数突合")
    a("")
    a("| 源 | 台帳の発火行 | first_in_episode | score_eligible | episode 重複除去 | 区間内・as_of 以前 (N) | 収集区間 |")
    a("|---|--:|--:|--:|--:|--:|---|")
    for cls in ctx["classes"]:
        t = ctx["tally"].get(cls, {"rows": 0, "first_in_episode": 0, "score_eligible": 0, "deduped": 0})
        iv = ctx["intervals"][cls]
        span = f"[{iso(iv['start'])}, {iso(iv['end'])})" + ("（延長）" if iv["extended"] else "")
        a(f"| {cls} | {t['rows']} | {t['first_in_episode']} | {t['score_eligible']} | {t['deduped']} "
          f"| {ctx['sources'][cls]['N']} | {span} |")
    a("")
    # 3. 源別サマリ
    a("## 3. 源別サマリ（4 行）")
    a("")
    a(STATS_HEADER.format("源", " 合格線 |", "---|"))
    for cls in ctx["classes"]:
        a(stats_row(cls, ctx["sources"][cls], pl))
    a("")
    a("- 的中率= hit/(hit+miss)・空振り率= miss/(hit+miss)。confirm_after／pending／unobserved／unscorable は分母外")
    ou = {cls: ctx["sources"][cls]["confirm_after_onset_unknown"] for cls in ctx["classes"]}
    a("- confirm_after のうち開始日不明（規則 2b・窓内の最初の成立が onset_unknown）: "
      + "・".join(f"{cls} {n}" for cls, n in ou.items()))
    a("- 比較率= 同じ主題・同じ計器・同じ判定表で、収集区間内の毎週火曜を疑似発火にした hit/(hit+miss) を、"
      "その源の発火数で重み付けした加重平均（比較率が定義できる主題のみ）")
    a("")
    # 4. 源×主題
    a("## 4. 源 × 主題")
    a("")
    for cls in ctx["classes"]:
        subs = sorted({k[1] for k in ctx["source_subject"] if k[0] == cls}, key=lambda x: str(x))
        a(f"### {cls}（{cfg.get('source_classes', {}).get(cls, '')}）")
        a("")
        if not subs:
            a("- 発火なし")
            a("")
            continue
        a(STATS_HEADER.format("主題", "", ""))
        for sub in subs:
            a(stats_row(str(sub), ctx["source_subject"][(cls, sub)]))
        a("")
    # 5. 比較率
    a("## 5. 比較率（主題別・疑似発火= 収集区間内の毎週火曜 T・K=T）")
    a("")
    tues = ctx["tuesdays_attempted"]
    if tues:
        a(f"- 試行した火曜 T（as_of 以前）: {len(tues)} 本（{tues[0]} 〜 {tues[-1]}）")
        if len(ctx["ends"]) > 1:
            a(f"- 区間末が源で異なる（{', '.join(ctx['ends'])}）。下表は基準区間 {ctx['ends'][0]} まで・"
              "延長した源の集計には延長区間の比較率を使う（台帳には両方を書く）")
    else:
        a(f"- 試行した火曜 T: **0 本**（as_of {iso(ctx['as_of'])} が収集区間の初回火曜 "
          f"{sc['collect_interval']['start']} より前＝比較率はまだ計算できない）")
    a("")
    a("| 主題 | 計器 | 試行 T | hit | miss | confirm_after | pending | unobserved | 比較率 |")
    a("|---|---|--:|--:|--:|--:|--:|--:|--:|")
    for sub, cr in ctx["subject_comparisons"]:
        a(f"| {sub} | {', '.join(cr['instruments']) or '（計器なし）'} | {cr['attempted']} | {cr['hit']} | {cr['miss']} "
          f"| {cr['confirm_after']} | {cr['pending']} | {cr['unobserved']} | {fmt_pct(cr['rate'])} |")
    a("")
    # 6. 状態系列
    a("## 6. 計器の状態系列（as_of までに観測できた行から）")
    a("")
    a("| 計器 | cadence | 期間 | 成立 | 不成立 | 不明 | 初判定可能日 | onset | onset_unknown |")
    a("|---|---|---|--:|--:|--:|---|---|---|")
    for s in ctx["series_summaries"]:
        span = f"{fmt(s['first'])}〜{fmt(s['last'])}" if s["first"] else "行なし"
        a(f"| {s['instrument']} | {s['cadence']} | {span} | {s['on']} | {s['off']} | {s['unknown']} "
          f"| {fmt(s['first_judgeable'])} | {', '.join(s['onsets']) or '—'} | {', '.join(s['onset_unknown']) or '—'} |")
    a("")
    # 7. 未確定
    a("## 7. 未確定 episode（pending・台帳には書かない）")
    a("")
    pend = [e for e in ctx["episodes"] if e["verdict"] == "pending"]
    if not pend:
        a("- なし")
    else:
        a("| 源 | episode_id | 主題 | known_date | 確定予定（K+60） | 計器ごとの現状 |")
        a("|---|---|---|---|---|---|")
        for e in pend:
            kd = to_date(e["known_date"])
            due = iso(kd + timedelta(days=int(sc.get("horizon_days", 60)))) if kd else "—"
            cur = "; ".join(f"{k}={v['verdict']}(規則{v['rule']}, 判定可能{v['judgeable_in_window']})"
                            for k, v in e.get("instrument_verdicts", {}).items())
            a(f"| {e['source_class']} | {e['episode_id']} | {e.get('subject')} | {e['known_date']} | {due} | {cur} |")
    a("")
    # 8. 確定一覧
    fin = [e for e in ctx["episodes"] if e["verdict"] != "pending"]
    a("## 8. 確定した episode")
    a("")
    if not fin:
        a("- なし")
    else:
        a("| 源 | episode_id | 主題 | 題名 | known_date | verdict | onset | onset_unknown | 計器 | lead | 検出遅延 "
          "| lookback_partial | 出所 |")
        a("|---|---|---|---|---|---|---|---|---|--:|--:|---|---|")
        for e in fin:
            a(f"| {e['source_class']} | {e['episode_id']} | {e.get('subject')} | {fmt(e.get('title'))} | {e['known_date']} "
              f"| {e['verdict']} | {fmt(e.get('onset_date'))} | {bool(e.get('onset_unknown'))} | {fmt(e.get('instrument'))} "
              f"| {fmt(e.get('lead_days'))} | {fmt(e.get('detect_lag_days'))} | {e.get('lookback_partial')} "
              f"| {'台帳（確定済）' if e.get('from_ledger') else '今回'} |")
    a("")
    # 9. 延長
    a("## 9. 延長判定（決定日 {}・源ごとに N < {} なら {} まで 1 回延長）".format(
        iso(ctx["intervals"][ctx["classes"][0]]["decide_on"]), pl.get("min_scored", 5),
        iso(ctx["intervals"][ctx["classes"][0]]["ext_end"])))
    a("")
    for cls in ctx["classes"]:
        iv = ctx["intervals"][cls]
        if not iv["decided"]:
            a(f"- {cls}: 未判定（現在の N= {iv['n_base']}・unscorable を除く・pending 含む）")
        else:
            a(f"- {cls}: N= {iv['n_base']} → " + (f"**延長**（{iso(iv['end'])} まで）" if iv["extended"] else "延長なし"))
    a("")
    a("---")
    a("- 規則の正本: `tasks/shortage_presign_forward.md` §事前登録 C/D/E・`configs/presign_forward.json`。"
      "実装の解釈メモは `scripts/presign_forward_eval.py` docstring")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 本体
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--as-of", default=None, help="採点日 YYYY-MM-DD（既定= 今日 JST）")
    p.add_argument("--dry-run", action="store_true", help="台帳・報告を書かず報告を標準出力へ")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--wiki-config", default=str(DEFAULT_WIKI_CONFIG))
    p.add_argument("--pu-sources", default=str(DEFAULT_PU_SOURCES))
    p.add_argument("--flea", default=None, help="flea_panel daily.jsonl（既定= config onset_rule.flea.source）")
    p.add_argument("--pu", default=None, help="universe_weekly.jsonl（既定= config onset_rule.pu_weekly.source）")
    p.add_argument("--wiki-ledger", default=None, help="既定= config sources.wiki")
    p.add_argument("--rss-ledger", default=None, help="既定= config sources.rss")
    p.add_argument("--ledger", default=None, help="採点台帳（既定= config ledger）")
    p.add_argument("--report", default=None, help="報告 md（既定= config report）")
    p.add_argument("--quiet", action="store_true")
    return p


def _resolve(path_str: Optional[str], fallback: Optional[str]) -> Optional[Path]:
    s = path_str or fallback
    if not s:
        return None
    p = Path(s)
    return p if p.is_absolute() else APP / p


def run(args: argparse.Namespace) -> dict:
    config_path = Path(args.config)
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    wiki_cfg = json.loads(Path(args.wiki_config).read_text(encoding="utf-8")) if Path(args.wiki_config).exists() else {}
    pu_sources = json.loads(Path(args.pu_sources).read_text(encoding="utf-8")) if Path(args.pu_sources).exists() else {}
    spec_version = int(cfg.get("version", 1))
    as_of = to_date(args.as_of) if args.as_of else datetime.now(JST).date()
    if as_of is None:
        raise SystemExit(f"--as-of が日付でない: {args.as_of}")
    sc = cfg["scoring"]
    onset = cfg.get("onset_rule", {})
    flea_path = _resolve(args.flea, onset.get("flea", {}).get("source"))
    pu_src_w = onset.get("pu_weekly", {}).get("source")
    pu_src_m = onset.get("pu_monthly", {}).get("source", pu_src_w)
    if pu_src_w and pu_src_m and pu_src_w != pu_src_m:  # 週次・月次は同じ台帳を読む固定契約（違えば拒否）
        raise ValueError(f"onset_rule.pu_weekly.source={pu_src_w!r} と pu_monthly.source={pu_src_m!r} が違う（v1 は同一台帳の契約）")
    pu_path = _resolve(args.pu, pu_src_w)
    wiki_ledger = _resolve(args.wiki_ledger, cfg.get("sources", {}).get("wiki"))
    rss_ledger = _resolve(args.rss_ledger, cfg.get("sources", {}).get("rss"))
    ledger_path = _resolve(args.ledger, cfg.get("ledger")) or (APP / "data/shortage_presign/forward_eval.jsonl")
    report_path = _resolve(args.report, cfg.get("report")) or (APP / "output/shortage_presign/forward_report.md")

    flea_rows = read_jsonl(flea_path)
    pu_rows = read_jsonl(pu_path)
    wiki_rows = read_jsonl(wiki_ledger)
    rss_rows = read_jsonl(rss_ledger)
    scorer = Scorer(cfg, wiki_cfg, pu_sources, flea_rows, pu_rows, as_of)
    fires, tally = load_fires(wiki_rows, rss_rows)
    intervals = source_intervals(cfg, fires, as_of, scorer)
    classes = list(intervals.keys())
    # 多重起動ガード（本書きの時だけ・news_shock_collect の .collect.lock と同じ作法・採点器専用 .eval.lock）。
    # 台帳の読み→採点→追記→報告の書き出しまでを 1 本のロックで覆う（読みと書きの間に別便が追記すると冪等が崩れる）
    lock = None
    if not args.dry_run:
        import fcntl
        lock_path = ledger_path.parent / ".eval.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = lock_path.open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            print("別の採点が実行中のためスキップ（多重起動ガード）")
            return {"skipped": True, "lock": str(lock_path), "as_of": iso(as_of)}
    try:
        ledger = load_ledger(ledger_path, spec_version)
        scored_at = now_jst_iso()

        # --- 母集団と採点
        population = [f for f in fires
                      if f["source_class"] in intervals and in_population(f, intervals[f["source_class"]], as_of)]
        episodes: List[dict] = []
        for f in population:
            key = (f["source_class"], f["episode_id"])
            instruments, how = scorer.fire_instruments(f)
            if key in ledger["final"]:
                row = dict(ledger["final"][key])
                row["from_ledger"] = True
                row.setdefault("instruments", instruments or [])
                episodes.append(row)
                continue
            K = to_date(f["known_date"])
            if not instruments:
                res = combine_verdicts({})
                res["instrument_verdicts"] = {}
                reason = "subject_unknown" if instruments is None else "no_instrument"
            else:
                res = scorer.score(instruments, K)
                reason = None
            row = {
                "type": "score", "scored_at": scored_at, "as_of": iso(as_of),
                "source_class": f["source_class"], "episode_id": f["episode_id"], "subject": f.get("subject"),
                "title": f.get("title"), "known_date": f["known_date"],
                "verdict": res["verdict"], "final": res["final"],
                "onset_date": res["onset_date"], "instrument": res["instrument"],
                "lead_days": res["lead_days"], "detect_lag_days": res["detect_lag_days"],
                "onset_unknown": res.get("onset_unknown", False), "lookback_partial": res["lookback_partial"],
                "instruments": instruments or [], "instruments_from": how,
                "instrument_verdicts": res["instrument_verdicts"], "spec_version": spec_version,
            }
            if reason:
                row["unscorable_reason"] = reason
            row["from_ledger"] = False
            episodes.append(row)

        # --- 比較率（計器の組 × 区間末ごと・疑似発火= 毎週火曜）
        start = to_date(sc["collect_interval"]["start"])
        ends = sorted({iso(iv["end"]) for iv in intervals.values()})
        comparisons: Dict[Tuple[Tuple[str, ...], str], dict] = {}

        def comparison_for(instruments: List[str], end_iso: str) -> dict:
            key = (tuple(instruments), end_iso)
            if key in comparisons:
                return comparisons[key]
            Ts = [T for T in tuesdays_between(start, to_date(end_iso)) if T <= as_of]
            verdicts = {}
            for T in Ts:
                verdicts[iso(T)] = scorer.score(instruments, T)["verdict"] if instruments else "unscorable"
            c = Counter(verdicts.values())
            hm = c.get("hit", 0) + c.get("miss", 0)
            comparisons[key] = {
                "instruments": list(instruments), "interval_end": end_iso, "attempted": len(Ts),
                "tuesdays": [iso(T) for T in Ts], "verdicts": verdicts,
                "hit": c.get("hit", 0), "miss": c.get("miss", 0), "confirm_after": c.get("confirm_after", 0),
                "pending": c.get("pending", 0), "unobserved": c.get("unobserved", 0),
                "rate": (c.get("hit", 0) / hm) if hm else None,
            }
            return comparisons[key]

        subject_comparisons: List[Tuple[str, dict]] = []       # 報告用（最初の区間末＝基準区間）
        subject_comparisons_all: List[Tuple[str, dict]] = []   # 台帳用（区間末ごと・延長があれば 2 本）
        for sub in cfg.get("subjects", {}):
            inst = scorer.subject_instruments(sub) or []
            for end_iso in ends:
                cr = comparison_for(inst, end_iso)
                subject_comparisons_all.append((sub, cr))
                if end_iso == ends[0]:
                    subject_comparisons.append((sub, cr))
        for e in episodes:  # 発火側の計器の組（override 含む）も揃えておく
            if e.get("instruments"):
                comparison_for(e["instruments"], iso(intervals[e["source_class"]]["end"]))
        end_for = {cls: iso(iv["end"]) for cls, iv in intervals.items()}

        # --- 集計
        sources = {cls: aggregate([e for e in episodes if e["source_class"] == cls], comparisons, end_for)
                   for cls in classes}
        source_subject = {}
        for cls in classes:
            for sub in sorted({str(e.get("subject")) for e in episodes if e["source_class"] == cls}):
                source_subject[(cls, sub)] = aggregate(
                    [e for e in episodes if e["source_class"] == cls and str(e.get("subject")) == sub], comparisons, end_for)

        # --- 状態系列の要約（主題に載っている全計器）
        all_keys: List[str] = []
        for sub in cfg.get("subjects", {}):
            for k in scorer.subject_instruments(sub) or []:
                if k not in all_keys:
                    all_keys.append(k)
        for e in episodes:
            for k in e.get("instruments") or []:
                if k not in all_keys:
                    all_keys.append(k)
        series_summaries = [scorer.series(k).summary() for k in all_keys]

        # --- 鮮度
        def _last(rows, field):
            vals = [str(r.get(field)) for r in rows if r.get(field)]
            return max(vals) if vals else None

        freshness = [
            {"name": f"flea_panel `{flea_path}`", "rows": len(flea_rows), "last": _last(flea_rows, "date"),
             "note": f"最終 window {fmt(_last(flea_rows, 'window'))}"},
            {"name": f"price_universe `{pu_path}`", "rows": len(pu_rows), "last": _last(pu_rows, "date"), "note": ""},
            {"name": f"wiki 発火台帳 `{wiki_ledger}`", "rows": len(wiki_rows),
             "last": _last([r for r in wiki_rows if r.get("type") == "fire"], "known_date"),
             "note": "" if wiki_rows else "台帳なし（初便 2026-10-13 09:30 前）"},
            {"name": f"RSS presign 台帳 `{rss_ledger}`", "rows": len(rss_rows),
             "last": _last([r for r in rss_rows if r.get("type") == "hit"], "known_date"),
             "note": "" if rss_rows else "台帳なし（presign 便の初回前）"},
        ]
        def _last_summary(rows, label, fields):
            sums = [r for r in rows if r.get("type") == "run_summary"]
            if not sums:
                return f"{label}: 未実行（run_summary 行なし）"
            s = max(sums, key=lambda r: str(r.get("run_at") or ""))
            return f"{label}: 最終便 run_at {s.get('run_at')}・" + "・".join(f"{k} {s.get(k)}" for k in fields)

        run_summaries = [
            _last_summary(wiki_rows, "wiki 週次見張り", ["titles_ok", "titles_error"]),
            _last_summary(rss_rows, "RSS presign 便", ["queries_ok", "feeds_ok"]),
        ]
        tuesdays_attempted = [iso(T) for T in tuesdays_between(start, to_date(ends[-1])) if T <= as_of]

        # --- 書き込み
        written = {"preregistration": False, "scores": 0, "comparisons": 0, "report": None}
        if not args.dry_run:
            if not ledger["prereg"]:
                append_jsonl(ledger_path, preregistration_row(cfg, config_path, spec_version))
                written["preregistration"] = True
            for e in episodes:
                if e.get("from_ledger") or not e.get("final"):
                    continue
                row = {k: v for k, v in e.items() if k != "from_ledger"}
                append_jsonl(ledger_path, row)
                written["scores"] += 1
            for sub, cr in subject_comparisons_all:
                if cr["attempted"] == 0:
                    continue
                resolved = sorted(T for T, v in cr["verdicts"].items() if v != "pending")
                sig = {"hit": cr["hit"], "miss": cr["miss"], "confirm_after": cr["confirm_after"],
                       "unobserved": cr["unobserved"], "resolved": resolved}
                prev = ledger["comparison_latest"].get((sub, cr["interval_end"]))
                if prev and {"hit": prev.get("hit"), "miss": prev.get("miss"), "confirm_after": prev.get("confirm_after"),
                             "unobserved": prev.get("unobserved"), "resolved": sorted(prev.get("resolved_tuesdays") or [])} == sig:
                    continue
                append_jsonl(ledger_path, {
                    "type": "comparison", "scored_at": scored_at, "as_of": iso(as_of), "subject": sub,
                    "interval_end": cr["interval_end"], "instruments": cr["instruments"],
                    "tuesdays": cr["tuesdays"], "resolved_tuesdays": resolved,
                    "hit": cr["hit"], "miss": cr["miss"], "confirm_after": cr["confirm_after"],
                    "pending": cr["pending"], "unobserved": cr["unobserved"], "rate": cr["rate"],
                    "spec_version": spec_version,
                })
                written["comparisons"] += 1

        ctx = {
            "cfg": cfg, "config_path": config_path, "config_sha": sha12(config_path), "spec_version": spec_version,
            "as_of": as_of, "scored_at": scored_at, "dry_run": args.dry_run, "ledger_path": ledger_path,
            "written": written, "freshness": freshness, "run_summaries": run_summaries, "tally": tally, "classes": classes, "intervals": intervals,
            "sources": sources, "source_subject": source_subject, "subject_comparisons": subject_comparisons,
            "ends": ends, "tuesdays_attempted": tuesdays_attempted, "series_summaries": series_summaries,
            "episodes": episodes,
        }
        report = build_report(ctx)
        if not args.dry_run:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report, encoding="utf-8")
            written["report"] = str(report_path)
        return {
            "as_of": iso(as_of), "spec_version": spec_version, "fires": fires, "tally": tally,
            "population": population, "episodes": episodes, "comparisons": comparisons,
            "subject_comparisons": subject_comparisons, "sources": sources, "source_subject": source_subject,
            "intervals": intervals, "series_summaries": series_summaries, "report": report, "written": written,
            "ledger_path": ledger_path, "report_path": report_path,
        }
    finally:
        if lock is not None:
            lock.close()  # close で flock も解放


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    res = run(args)
    if res.get("skipped"):  # 別の採点がロック中（メッセージは run() が印字済み）
        return 0
    if args.dry_run:
        if not args.quiet:
            sys.stdout.write(res["report"])
        return 0
    if not args.quiet:
        w = res["written"]
        print(f"[presign_forward_eval] as_of={res['as_of']} 母集団={len(res['population'])} "
              f"確定追記={w['scores']} 比較率追記={w['comparisons']} 事前宣言={'書いた' if w['preregistration'] else '既存'} "
              f"報告={w['report']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
