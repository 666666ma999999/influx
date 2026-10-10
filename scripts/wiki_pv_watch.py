#!/usr/bin/env python3
"""Wikipedia 閲覧数の週次見張り（P-INF-43 b・品薄予兆の前向き測定・源 S-C の昇格）.

位置づけ: 事前登録 `tasks/shortage_presign_forward.md` §A と `configs/wiki_pv_watch.json` v1 が規則の正本。
P-INF-42 遡り測定（`_tmp_presign_backfill.py` の wiki_daily / weekly_sums / s_c_wiki）を前向きの定期便に移植した。
結果を見てから規則を変えない（変更は config の version を上げ、変更日以降の発火にだけ適用）。

規則（§A の写し・実装はこの文言に従う）:
- 週= 月曜〜日曜（UTC 日付）。7/7 日そろった週だけ判定。
- 発火: 週合計 S ≥ 2.0 × B かつ B ≥ 200。B= 直前 8 週のうち 7/7 日そろった週（最低 6 週）の合計の中央値。
- 判定の一意キー= (title, week_start)。便ごとに「取得範囲内（直近 fetch_weeks 週）で week_end ≤ 便の UTC 前日」の
  未判定週を全て判定し、初回判定が最終（judged=true の週は再判定しない）。
- 7/7 日そろわない週= judged=false/incomplete・基準 6 週未満= judged=false/base_insufficient →
  取得範囲内なら次便で再判定・範囲を出たら reason=unjudgeable で judged=true として確定。
- known_date= 便の実行日（JST）。score_eligible= week_end ≥ known_date−8 日（鮮度条件のみ＝初便が過去 10 週を
  まとめて判定する分は基準線づくり）。収集区間（延長込み）の判定は採点器 presign_forward_eval.py が known_date で行う。
- エピソード= 題名ごと・初発火の week_start から 56 日以内は同一（連鎖させない）。

規約:
- 台帳 data/wiki_pv_watch/ledger.jsonl は追記専用（スキーマ= task §E・全行に config_version / config_sha）
- 依存は標準ライブラリのみ（ホスト /usr/bin/python3 3.9 で動く・Docker 不要）
- 1 題名の失敗で便を落とさない（type=error 行・便は続行）。題名の過半が error なら rc=1（runner が失敗通知）
- 429/5xx は Retry-After 優先（無ければ 5×試行回数 秒）で最大 retry 回

実行:
    python3 scripts/wiki_pv_watch.py                      # 取得→判定→台帳→発火時のみ Mac 通知
    python3 scripts/wiki_pv_watch.py --dry-run            # 台帳に書かず要約だけ（通知もしない）
    python3 scripts/wiki_pv_watch.py --date 2026-10-13    # 便の実行日（JST）を上書き
    python3 scripts/wiki_pv_watch.py --selftest           # 固定 PV 列の自己テスト（ネット不要）
"""
from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "scripts"))

CONFIG = APP / "configs/wiki_pv_watch.json"
LEDGER = APP / "data/wiki_pv_watch/ledger.jsonl"

JST = timezone(timedelta(hours=9))
MAX_RETRY_AFTER_SEC = 300          # Retry-After の上限（launchd 便を無限に止めないための安全弁）

# テストから差し替えられるように module 変数にしておく
_sleep: Callable[[float], None] = time.sleep


# ---------------------------------------------------------------- 設定・日付
def load_config(path: Path = CONFIG) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def config_sha(path: Path = CONFIG) -> str:
    """config ファイルの sha256 先頭 12 桁（fire/week_obs 行に焼き込む provenance）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def jst_today() -> date:
    return datetime.now(JST).date()


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def plan_weeks(run_utc_date: date, cfg: dict) -> dict:
    """便の判定対象週と取得範囲を決める。

    cutoff= 便の UTC 前日。判定対象= week_end ≤ cutoff の直近 fetch_weeks 週（week_start ≥ baseline_from）。
    取得範囲= 最古の判定週の base_weeks 週前（baseline_from で下限）〜 最新の判定週の week_end。
    """
    api, rule = cfg["api"], cfg["rule"]
    cutoff = run_utc_date - timedelta(days=1)
    last_week_end = cutoff - timedelta(days=(cutoff.weekday() + 1) % 7)     # 最新の日曜 ≤ cutoff
    last_week_start = last_week_end - timedelta(days=6)
    baseline_from = date.fromisoformat(cfg["baseline_from"])
    judge_starts = sorted(
        ws for ws in (last_week_start - timedelta(days=7 * k) for k in range(int(api["fetch_weeks"])))
        if ws >= baseline_from)
    if judge_starts:
        fetch_start = max(baseline_from, judge_starts[0] - timedelta(days=7 * int(rule["base_weeks"])))
    else:
        fetch_start = baseline_from
    return {"cutoff": cutoff, "judge_week_starts": judge_starts,
            "fetch_start": fetch_start, "fetch_end": last_week_end}


# ---------------------------------------------------------------- 取得
def article_url(cfg: dict, title: str, start: date, end: date) -> str:
    """per-article daily の URL。題名は空白→アンダースコアの上で URI エンコード（API 仕様）。"""
    t = urllib.parse.quote(title.replace(" ", "_"), safe="")
    return f"{cfg['api']['base'].rstrip('/')}/{t}/daily/{start:%Y%m%d}/{end:%Y%m%d}"


def _retry_after_seconds(headers, attempt: int) -> float:
    """Retry-After（秒 or HTTP 日付）を優先・無ければ 5×試行回数 秒。上限 MAX_RETRY_AFTER_SEC。"""
    raw = headers.get("Retry-After") if headers is not None else None
    wait = None
    if raw:
        raw = raw.strip()
        if raw.isdigit():
            wait = float(raw)
        else:
            try:
                dt_ = email.utils.parsedate_to_datetime(raw)
                if dt_.tzinfo is None:
                    dt_ = dt_.replace(tzinfo=timezone.utc)
                wait = max(0.0, (dt_ - datetime.now(timezone.utc)).total_seconds())
            except Exception:                           # noqa: BLE001
                wait = None
    if wait is None:
        wait = 5.0 * attempt
    return min(wait, MAX_RETRY_AFTER_SEC)


def fetch_daily(title: str, start: date, end: date, cfg: dict) -> Dict[date, int]:
    """日次 PV を 1 要求で取る（{date: views}）。429/5xx/通信失敗は Retry-After で最大 retry 回。

    404（記事なし・データなし）は再試行せず例外＝呼び手が type=error 行にする（0 件と混同しない）。
    """
    api = cfg["api"]
    retry = int(api.get("retry", 6))
    timeout = float(api.get("timeout_sec", 60))
    url = article_url(cfg, title, start, end)
    req = urllib.request.Request(url, headers={"User-Agent": api["user_agent"], "Accept": "application/json"})
    last_err: Optional[Exception] = None
    for attempt in range(1, retry + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            out: Dict[date, int] = {}
            for it in data.get("items", []):
                ts = str(it["timestamp"])[:8]
                out[date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))] = int(it["views"])
            return out
        except urllib.error.HTTPError as ex:
            last_err = ex
            if ex.code == 429 or ex.code >= 500:
                if attempt < retry:
                    _sleep(_retry_after_seconds(ex.headers, attempt))
                    continue
            raise RuntimeError(f"http{ex.code} after {attempt} attempt(s): {url}") from ex
        except (urllib.error.URLError, OSError, ValueError) as ex:   # 通信・timeout・JSON 不正
            last_err = ex
            if attempt < retry:
                _sleep(5.0 * attempt)
                continue
    raise RuntimeError(f"{type(last_err).__name__}: {str(last_err)[:120]} after {retry} attempt(s)")


# ---------------------------------------------------------------- 週集計・判定
def weekly_table(daily: Dict[date, int], first_ws: date, last_ws: date) -> Dict[date, dict]:
    """月曜始まりの週ごとに {week_start: {"week_end","days","sum"}}（first_ws〜last_ws・いずれも月曜）。"""
    out: Dict[date, dict] = {}
    ws = first_ws
    while ws <= last_ws:
        vals = [daily.get(ws + timedelta(days=i)) for i in range(7)]
        have = [v for v in vals if v is not None]
        out[ws] = {"week_end": ws + timedelta(days=6), "days": len(have), "sum": int(sum(have))}
        ws += timedelta(days=7)
    return out


def judge_week(weeks: Dict[date, dict], ws: date, rule: dict) -> dict:
    """1 週を規則どおり判定する（純関数）。

    返り値: days, sum, base_median8, base_n, ratio, judged, reason, fired
    - days < require_days → judged=false, reason=incomplete
    - 直前 base_weeks 週のうち 7/7 日の週が base_min_weeks 未満 → judged=false, reason=base_insufficient
    - それ以外 judged=true・fired= (S ≥ ratio×B かつ B ≥ base_floor_per_week)
    """
    req_days = int(rule["require_days"])
    base_weeks = int(rule["base_weeks"])
    base_min = int(rule["base_min_weeks"])
    ratio_th = float(rule["ratio"])
    floor_ = float(rule["base_floor_per_week"])
    cur = weeks.get(ws) or {"days": 0, "sum": 0}
    base_sums = []
    for k in range(1, base_weeks + 1):
        w = weeks.get(ws - timedelta(days=7 * k))
        if w and w["days"] >= req_days:
            base_sums.append(w["sum"])
    base_n = len(base_sums)
    base_med = float(statistics.median(base_sums)) if base_sums else None
    res = {"days": cur["days"], "sum": cur["sum"], "base_median8": base_med, "base_n": base_n,
           "ratio": None, "judged": False, "reason": None, "fired": False}
    if cur["days"] < req_days:
        res["reason"] = "incomplete"
        return res
    if base_n < base_min:
        res["reason"] = "base_insufficient"
        return res
    res["judged"] = True
    if base_med and base_med > 0:
        res["ratio"] = round(cur["sum"] / base_med, 3)
    res["fired"] = bool(base_med is not None and base_med >= floor_ and cur["sum"] >= ratio_th * base_med)
    return res


def is_score_eligible(week_end: date, known_date: date, lag_days: int = 8) -> bool:
    """鮮度条件のみ: week_end ≥ known_date−8 日。

    収集区間（[start, end_exclusive)・源別の延長込み）に入るかは採点器 presign_forward_eval.py が
    fire 行の known_date で判定する（ここで AND すると延長期間の発火が全て採点対象外になる・Codex 統合レビュー #1/#4）。
    """
    return week_end >= known_date - timedelta(days=lag_days)


def assign_episode(title: str, ws: date, episodes: Dict[str, List[date]], episode_days: int) -> Tuple[str, bool]:
    """エピソード付与（連鎖させない）: 既存エピソードの初発火 week_start E で (ws−E) ∈ [0, episode_days] なら同一。

    episodes は {title: [初発火 week_start, ...]} を更新する（新エピソードを足す）。
    返り値: (episode_id, first_in_episode)
    """
    anchors = episodes.setdefault(title, [])
    for e in sorted(anchors, reverse=True):
        d = (ws - e).days
        if 0 <= d <= episode_days:
            return episode_id_of(title, e), False
    anchors.append(ws)
    return episode_id_of(title, ws), True


def episode_id_of(title: str, first_ws: date) -> str:
    return f"wiki|{title}|{first_ws.isoformat()}"


# ---------------------------------------------------------------- 台帳
def read_ledger(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def ledger_state(rows: List[dict]) -> dict:
    """台帳から〈judged 済みキー／未確定（judged=false）の最新行／エピソード anchor〉を組み立てる。"""
    judged = set()
    pending: Dict[Tuple[str, str], dict] = {}
    episodes: Dict[str, List[date]] = {}
    for r in rows:
        t = r.get("type")
        if t == "week_obs":
            key = (r.get("title"), r.get("week_start"))
            if r.get("judged"):
                judged.add(key)
            else:
                pending[key] = r
        elif t == "fire" and r.get("first_in_episode"):
            try:
                episodes.setdefault(r["title"], []).append(date.fromisoformat(r["week_start"]))
            except (KeyError, ValueError):
                continue
    for k in judged:
        pending.pop(k, None)
    return {"judged": judged, "pending": pending, "episodes": episodes}


def append_rows(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 便
def run(cfg: dict, cfg_sha: str, known_date: date, run_utc_date: date, ledger_path: Path,
        dry_run: bool, fetcher: Callable[[str, date, date, dict], Dict[date, int]] = fetch_daily,
        run_at: Optional[str] = None, verbose: bool = True) -> dict:
    """1 便を実行する。返り値= {"rows": 書いた（or 書くはずの）行, "summary": run_summary 行, "fires": fire 行}。"""
    rule = cfg["rule"]
    plan = plan_weeks(run_utc_date, cfg)
    judge_ws: List[date] = plan["judge_week_starts"]
    run_at = run_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = ledger_state(read_ledger(ledger_path))
    titles = cfg["titles"]
    meta = {"config_version": cfg.get("version"), "config_sha": cfg_sha}
    kd = known_date.isoformat()

    rows: List[dict] = []
    fires: List[dict] = []
    n_ok = n_err = n_judged = n_pending = n_unjudgeable = n_new_ep = 0
    sleep_sec = float(cfg["api"].get("sleep_sec", 0.5))

    # 範囲を出た未確定週を unjudgeable で確定（データ不要・取得の成否と独立）
    oldest = judge_ws[0] if judge_ws else None
    for (title, ws_s), prev in sorted(state["pending"].items()):
        if oldest is None:
            break
        try:
            ws = date.fromisoformat(ws_s)
        except ValueError:
            continue
        if ws < oldest and any(te["title"] == title for te in titles):
            rows.append({"type": "week_obs", "run_at": run_at, "known_date": kd, "title": title,
                         "subject": prev.get("subject"), "week_start": ws_s, "week_end": prev.get("week_end"),
                         "days": prev.get("days"), "sum": prev.get("sum"),
                         "base_median8": prev.get("base_median8"), "base_n": prev.get("base_n"),
                         "ratio": None, "judged": True, "reason": "unjudgeable", "fired": False,
                         "score_eligible": False, "episode_id": None, **meta})
            state["judged"].add((title, ws_s))
            n_unjudgeable += 1

    for i, te in enumerate(titles):
        title, subject = te["title"], te.get("subject")
        if i and sleep_sec > 0:
            _sleep(sleep_sec)
        if not judge_ws:
            break
        try:
            daily = fetcher(title, plan["fetch_start"], plan["fetch_end"], cfg)
        except Exception as e:                           # noqa: BLE001  1 題名の失敗で便を落とさない
            n_err += 1
            rows.append({"type": "error", "run_at": run_at, "title": title, "error": str(e)[:200], **meta})
            if verbose:
                print(f"[{title}] ERROR {str(e)[:120]}", file=sys.stderr)
            continue
        n_ok += 1
        weeks = weekly_table(daily, plan["fetch_start"] - timedelta(days=plan["fetch_start"].weekday()),
                             judge_ws[-1])
        for ws in judge_ws:                              # 古い週から（エピソード anchor を先に確定）
            ws_s = ws.isoformat()
            if (title, ws_s) in state["judged"]:
                continue                                 # 初回判定が最終
            j = judge_week(weeks, ws, rule)
            we = weeks[ws]["week_end"]
            elig = is_score_eligible(we, known_date)
            episode_id, first = None, False
            if j["fired"]:
                episode_id, first = assign_episode(title, ws, state["episodes"], int(rule["episode_days"]))
            rows.append({"type": "week_obs", "run_at": run_at, "known_date": kd, "title": title,
                         "subject": subject, "week_start": ws_s, "week_end": we.isoformat(),
                         "days": j["days"], "sum": j["sum"], "base_median8": j["base_median8"],
                         "base_n": j["base_n"], "ratio": j["ratio"], "judged": j["judged"],
                         "reason": j["reason"], "fired": j["fired"], "score_eligible": elig,
                         "episode_id": episode_id, **meta})
            if j["judged"]:
                n_judged += 1
                state["judged"].add((title, ws_s))
            else:
                n_pending += 1
            if j["fired"]:
                fire = {"type": "fire", "run_at": run_at, "known_date": kd, "title": title, "subject": subject,
                        "week_start": ws_s, "week_end": we.isoformat(), "sum": j["sum"],
                        "base_median8": j["base_median8"], "ratio": j["ratio"], "episode_id": episode_id,
                        "first_in_episode": first, "score_eligible": elig, **meta}
                rows.append(fire)
                fires.append(fire)
                n_new_ep += int(first)
                if verbose:
                    print(f"🔥 [{title}] {ws_s}..{we.isoformat()} S={j['sum']} B={j['base_median8']} "
                          f"×{j['ratio']} ep={episode_id}{' (new)' if first else ''}"
                          f"{' score' if elig else ''}")

    summary = {"type": "run_summary", "run_at": run_at, "known_date": kd,
               "titles_planned": len(titles), "titles_ok": n_ok, "titles_error": n_err,
               "weeks_judged": n_judged, "weeks_pending": n_pending, "weeks_unjudgeable": n_unjudgeable,
               "fires": len(fires), "new_episodes": n_new_ep,
               "fetch_start": plan["fetch_start"].isoformat(), "fetch_end": plan["fetch_end"].isoformat(),
               "judge_weeks": [d.isoformat() for d in judge_ws], **meta}
    rows.append(summary)
    if not dry_run:
        append_rows(ledger_path, rows)
    return {"rows": rows, "summary": summary, "fires": fires, "plan": plan}


def notify(title: str, body: str) -> None:
    """macOS 通知。失敗は握りつぶさず WARN（launchd ログに残る）。引用符・バックスラッシュは除去。"""
    clean = body.replace('"', "").replace("\\", "")[:120]
    try:
        r = subprocess.run(["osascript", "-e", f'display notification "{clean}" with title "{title}"'],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"WARN: 通知失敗 rc={r.returncode} {r.stderr.strip()[:100]}", file=sys.stderr)
    except OSError as e:
        print(f"WARN: 通知失敗 {e}", file=sys.stderr)


# ---------------------------------------------------------------- selftest
def _selftest() -> int:
    """固定 PV 列で規則の要点を検査する（ネット不要）。失敗数を返す。"""
    rule = {"week_start": "monday", "require_days": 7, "base_weeks": 8, "base_min_weeks": 6,
            "ratio": 2.0, "base_floor_per_week": 200, "episode_days": 56}
    ng = 0

    def check(name: str, cond: bool) -> None:
        nonlocal ng
        print(f"  {'OK' if cond else 'NG'}: {name}")
        ng += 0 if cond else 1

    def daily_const(ws0: date, per_day_by_week: List[Optional[int]]) -> Dict[date, int]:
        d: Dict[date, int] = {}
        for k, v in enumerate(per_day_by_week):
            if v is None:
                continue
            for i in range(7):
                d[ws0 + timedelta(days=7 * k + i)] = v
        return d

    ws0 = date(2026, 8, 3)
    # ① 8 週 300/日（2100/週）→ 当週 600/日（4200）= 2.0 倍ちょうど・B=2100 ≥ 200 → 発火
    wk = weekly_table(daily_const(ws0, [300] * 8 + [600]), ws0, ws0 + timedelta(days=56))
    j = judge_week(wk, ws0 + timedelta(days=56), rule)
    check("S=2.0×B かつ B≥200 で発火", j["judged"] and j["fired"] and j["base_n"] == 8 and j["ratio"] == 2.0)
    # ①' 1.9 倍は非発火
    wk = weekly_table(daily_const(ws0, [300] * 8 + [570]), ws0, ws0 + timedelta(days=56))
    check("S<2.0×B は非発火", judge_week(wk, ws0 + timedelta(days=56), rule)["fired"] is False)
    # ② B<200: 20/日（140/週）→ 当週 100/日（700・5 倍）でも非発火
    wk = weekly_table(daily_const(ws0, [20] * 8 + [100]), ws0, ws0 + timedelta(days=56))
    j = judge_week(wk, ws0 + timedelta(days=56), rule)
    check("B<200 なら 2 倍超でも非発火（judged=true）", j["judged"] and not j["fired"] and j["base_median8"] == 140.0)
    # ③ 基準 5 週のみ → base_insufficient
    wk = weekly_table(daily_const(ws0, [None, None, None, 300, 300, 300, 300, 300, 600]), ws0,
                      ws0 + timedelta(days=56))
    j = judge_week(wk, ws0 + timedelta(days=56), rule)
    check("基準 6 週未満は judged=false/base_insufficient", not j["judged"] and j["reason"] == "base_insufficient"
          and j["base_n"] == 5)
    # ④ 当週 6/7 日 → incomplete
    d = daily_const(ws0, [300] * 8 + [600])
    del d[ws0 + timedelta(days=56 + 3)]
    wk = weekly_table(d, ws0, ws0 + timedelta(days=56))
    j = judge_week(wk, ws0 + timedelta(days=56), rule)
    check("days<7 は judged=false/incomplete", not j["judged"] and j["reason"] == "incomplete" and j["days"] == 6)
    # ⑤ エピソード: 56 日以内は同一・57 日目は新
    eps: Dict[str, List[date]] = {}
    e1, f1 = assign_episode("T", date(2026, 10, 5), eps, 56)
    e2, f2 = assign_episode("T", date(2026, 10, 5) + timedelta(days=56), eps, 56)
    e3, f3 = assign_episode("T", date(2026, 10, 5) + timedelta(days=57), eps, 56)
    check("56 日以内は同一エピソード", f1 and not f2 and e1 == e2)
    check("57 日目は新エピソード（初発火起点・連鎖させない）", f3 and e3 != e1)
    # ⑦ score_eligible 境界（鮮度条件のみ・収集区間は採点器が判定）
    kd = date(2026, 10, 13)
    check("week_end = known_date−8 は対象", is_score_eligible(kd - timedelta(days=8), kd))
    check("week_end = known_date−9 は対象外", not is_score_eligible(kd - timedelta(days=9), kd))
    check("収集区間外の便（10/10）でも鮮度条件を満たせば対象", is_score_eligible(date(2026, 10, 4), date(2026, 10, 10)))
    check("延長期間の便（12/15）でも鮮度条件を満たせば対象", is_score_eligible(date(2026, 12, 13), date(2026, 12, 15)))
    # plan: 初便 2026-10-13（火）→ cutoff 10-12・判定週 08-03..10-05 の 10 週・取得 08-03..10-11
    cfg = {"api": {"fetch_weeks": 10}, "rule": rule, "baseline_from": "2026-08-03"}
    p = plan_weeks(date(2026, 10, 13), cfg)
    check("初便の判定週= 08-03..10-05 の 10 週", p["judge_week_starts"][0] == date(2026, 8, 3)
          and p["judge_week_starts"][-1] == date(2026, 10, 5) and len(p["judge_week_starts"]) == 10)
    check("初便の取得範囲= 08-03..10-11", p["fetch_start"] == date(2026, 8, 3) and p["fetch_end"] == date(2026, 10, 11))
    # Retry-After 優先・無ければ 5×試行
    check("Retry-After=7 を優先", _retry_after_seconds({"Retry-After": "7"}, 3) == 7.0)
    check("Retry-After 無しは 5×試行回数", _retry_after_seconds({}, 3) == 15.0)
    return ng


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", help="便の実行日 YYYY-MM-DD（JST・known_date を上書き・UTC 前日の基準も同日扱い）")
    ap.add_argument("--dry-run", action="store_true", help="台帳に書かず要約だけ（通知もしない）")
    ap.add_argument("--selftest", action="store_true", help="固定 PV 列の自己テスト（ネット不要）")
    ap.add_argument("--config", type=Path, default=CONFIG)
    ap.add_argument("--ledger", type=Path, default=LEDGER)
    args = ap.parse_args()
    if args.selftest:
        ng = _selftest()
        print("OK: 全通過" if not ng else f"NG: {ng}件失敗")
        return 1 if ng else 0

    if args.date:
        known_date = date.fromisoformat(args.date)
        run_utc_date = known_date          # 09:30 JST の便では JST 日付 = UTC 日付
    else:
        known_date, run_utc_date = jst_today(), utc_today()

    lock = None
    if not args.dry_run:
        # 多重起動ロック（launchd と手動実行の重複で同じ週を二重判定しないため・news_shock と同型）
        import fcntl
        args.ledger.parent.mkdir(parents=True, exist_ok=True)
        lock = (args.ledger.parent / ".watch.lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("別の便が実行中のためスキップ（多重起動ガード）")
            return 0

    cfg = load_config(args.config)
    sha = config_sha(args.config)
    res = run(cfg, sha, known_date, run_utc_date, args.ledger, dry_run=args.dry_run)
    s = res["summary"]
    mode = "DRY-RUN（台帳に書かない）" if args.dry_run else "台帳へ追記"
    print(f"[{mode}] known_date={s['known_date']} 判定週={s['judge_weeks'][0] if s['judge_weeks'] else '-'}"
          f"..{s['judge_weeks'][-1] if s['judge_weeks'] else '-'}（{len(s['judge_weeks'])}週）"
          f" 取得={s['fetch_start']}..{s['fetch_end']}")
    print(f"[done] titles planned={s['titles_planned']} ok={s['titles_ok']} error={s['titles_error']} | "
          f"weeks judged={s['weeks_judged']} pending={s['weeks_pending']} unjudgeable={s['weeks_unjudgeable']} | "
          f"fires={s['fires']} new_episodes={s['new_episodes']}")
    if args.dry_run:
        # 要約: 題名ごとの判定状況（報告用）
        per: Dict[str, dict] = {}
        for r in res["rows"]:
            if r["type"] == "week_obs":
                p = per.setdefault(r["title"], {"judged": 0, "pending": 0, "fired": 0})
                p["judged" if r["judged"] else "pending"] += 1
                p["fired"] += int(r["fired"])
        for t, p in per.items():
            print(f"  {t}: judged={p['judged']} pending={p['pending']} fired={p['fired']}")
        judged_rows = [r for r in res["rows"] if r["type"] == "week_obs" and r["judged"] and r["ratio"] is not None]
        if judged_rows:
            print("  比率上位 5（S/B・発火線 2.0）:")
            for r in sorted(judged_rows, key=lambda x: -x["ratio"])[:5]:
                print(f"    {r['title']} {r['week_start']}: S={r['sum']} B={r['base_median8']} ×{r['ratio']}")
    if res["fires"] and not args.dry_run:
        body = " / ".join(f"{f['title']} {f['week_start']} ×{f['ratio']}" for f in res["fires"][:4])
        notify(f"🔥 Wikipedia 閲覧数 発火: {len(res['fires'])}週（新エピソード {s['new_episodes']}）", body)
    if lock is not None:
        lock.close()
    if s["titles_planned"] and s["titles_error"] * 2 > s["titles_planned"]:
        print(f"ERROR: 題名の過半が取得失敗 {s['titles_error']}/{s['titles_planned']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
