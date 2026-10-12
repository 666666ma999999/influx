"""news_shock 発火の固定窓評価（+5/+20営業日・冪等）。仕様の正本= tasks/news_shock_preregister.md §3.

規則（凍結・スクリプトは写像）:
- 基準日: 発火 run_at(UTC) を JST に直し、09:00 より前なら当日・以後なら翌日を起点に、
  営業日（data/jquants/bars のファイル実在日）へ繰り上げた日。エントリー相当値=その日の始値 AdjO
- AdjO が欠損（売買停止等）なら翌営業日へ1日だけ繰延。それも欠損なら evaluation_skipped 行
- 測定: 基準始値 → +5/+20営業日の終値 AdjC。TOPIX は基準日終値 → 評価日終値（始値系列が
  無いため・凍結）。超過pt = 銘柄% − TOPIX%
- 冪等キー=(link, code, window)。false_positive 行が付いた link は評価しない
- 台帳は append-only（evaluation / evaluation_skipped 行を追記するのみ）
- 株式分割（2026-10-04・tasks/split_fix_live_lanes.md）: 評価日の AdjC は評価日ファイルの取得時点までの
  分割しか反映しないため、jq_fetch.split_scale で始値の尺度（始値を読んだ時点の entry_day ファイルの取得日の
  前日と entry_day の遅い方＝jq_fetch.split_scale_base）に揃えてから比べる。補正なしで
  書かれた既存の evaluation 行は上書きせず、変わる行だけ evaluation_correction 行を追記する（読む側は
  同じ (link, code, window) の evaluation_correction を優先する）

実行:
    python3 scripts/news_shock_eval.py            # 期日到来分を評価して追記
    python3 scripts/news_shock_eval.py --selftest # 模擬データで規則の固定テスト
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import jq_fetch  # noqa: E402  標準ライブラリのみ（ホスト実行可）・分割の尺度合わせの正本 split_scale
LEDGER = APP / "data/news_shock/news_log.jsonl"
BARS_DIR = APP / "data/jquants/bars"
TOPIX_PATH = APP / "data/jquants/topix.json.gz"
JST = timezone(timedelta(hours=9))
WINDOWS_BD = {"w5": 5, "w20": 20}


def base_trading_day(run_at_utc: str, bdays: list[str]) -> str | None:
    """発火時刻→エントリー基準の営業日（8桁）。09:00 JST が境界（プレレジ§3）。"""
    dt = datetime.fromisoformat(run_at_utc.replace("Z", "+00:00")).astimezone(JST)
    d0 = dt.date() if (dt.hour, dt.minute) < (9, 0) else dt.date() + timedelta(days=1)
    d8 = d0.strftime("%Y%m%d")
    later = [d for d in bdays if d >= d8]
    return later[0] if later else None


def _bar(day8: str, code5: str, bars_dir: Path) -> dict | None:
    f = bars_dir / f"{day8}.json.gz"
    try:
        with gzip.open(f, "rt") as fh:
            for r in json.load(fh)["data"]:
                if r["Code"] == code5:
                    return r
    except OSError:
        return None
    return None


SPLIT_ADJUST = "jq_fetch.split_scale"
_FAC_CACHE: dict[tuple[str, str], dict[str, float]] = {}


def _split_factors(day8: str, bars_dir: Path) -> dict[str, float]:
    """day8 のファイルで AdjFactor が 1 でない銘柄 {Code: AdjFactor}（1回の実行内でキャッシュ）。"""
    key = (str(bars_dir), day8)
    if key not in _FAC_CACHE:
        out: dict[str, float] = {}
        try:
            with gzip.open(bars_dir / f"{day8}.json.gz", "rt") as fh:
                for r in json.load(fh)["data"]:
                    fac = r.get("AdjFactor")
                    if fac not in (None, 1, 1.0):
                        out[r["Code"]] = float(fac)
        except OSError:
            pass
        _FAC_CACHE[key] = out
    return _FAC_CACHE[key]


def _scale_base(entry_day: str, asof: str | None) -> str:
    """entry_open（entry_day の AdjO）が反映している分割の基準日。asof=その始値を読んだ時刻（None=今）。
    後日の取り直しで最新の取得日だけを見ると昔読んだ始値の尺度を取り違える（Codex 2026-10-04）。"""
    return jq_fetch.split_scale_base(entry_day, jq_fetch.bars_fetch_ts_asof(entry_day, asof))


_CAL: list = []


def _calendar() -> list[str] | None:
    """取引所カレンダーの全営業日（1回の実行内でキャッシュ・無ければ None）。"""
    if not _CAL:
        days = jq_fetch.offline_business_days()
        if days is None:
            return None
        _CAL.append(days)
    return _CAL[0]


def _split_scan(code5: str, entry_day: str, ev_day: str, scale_base: str,
                bars_dir: Path) -> list[tuple[str, float]] | None:
    """entry_day の翌営業日〜尺度合わせに効く上限日（jq_fetch.split_scan_upper）の bars から code5 の分割。

    評価窓の営業日はプレレジ§3どおり bars の実在日で数えるが、分割の走査は取引所カレンダーで欠けを検査する
    （欠けた日の分割は実在日だけ見ていると検出できない・Codex 2026-10-04 3回目）。上限までの営業日の bars が
    1日でも欠けている・カレンダーが無い／入口日〜上限を収録していない時は None＝評価も訂正も書かずに保留する。
    """
    cal = _calendar()
    upper = jq_fetch.split_scan_upper(ev_day, jq_fetch.bars_fetch_ts_asof(ev_day, None), scale_base)
    if cal is None or cal[-1] < upper or cal[0] > entry_day or entry_day not in cal:
        return None                                   # カレンダーが入口日〜上限を収録していない（Codex 4回目: 開始側）
    days = [d for d in cal if entry_day < d <= upper]
    if any(not (bars_dir / f"{d}.json.gz").exists() for d in days):
        return None
    return [(d, _split_factors(d, bars_dir)[code5]) for d in days if code5 in _split_factors(d, bars_dir)]


def _representative_hits(rows: list[dict], fp_links: set) -> list[dict]:
    """評価対象の代表行。分析単位=事象: 各 event_id の**最初の非 false_positive 行**だけを代表にする
    （プレレジ§6の代表行規則。先頭行が後日FP化されたら次の非FP行へ繰り上がる＝Codex再審の注文。
    v1行は event_id が無いので従来どおり行単位・FP除外のみ＝後方互換）。evaluate と report が共用。"""
    hits, seen_ev = [], set()
    for r in rows:
        if r.get("status") != "hit" or r.get("link") in fp_links:
            continue
        eid = r.get("event_id")
        if eid:
            if eid in seen_ev:
                continue
            seen_ev.add(eid)
        hits.append(r)
    return hits


def evaluate(ledger: Path = LEDGER, bars_dir: Path = BARS_DIR,
             topix_path: Path = TOPIX_PATH) -> int:
    if not ledger.exists():
        print("[eval] 台帳がまだありません")
        return 0
    # ロックは台帳読込より前に取る（読込→ロックの順だと、古い done を持ったプロセスが
    # 先行プロセスの追記後にロックを取り二重追記できる・Codex第3審指摘）
    import fcntl
    lock = (ledger.parent / ".eval.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[eval] 別の評価が実行中のためスキップ")
        return 0
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()
            if l.strip()]
    fp_links = {r.get("link") for r in rows if r.get("type") == "false_positive"}
    hits = _representative_hits(rows, fp_links)
    done = {(r.get("link"), r.get("code"), r.get("window")) for r in rows
            if r.get("type") in ("evaluation", "evaluation_skipped")}
    bdays = sorted(Path(f).name[:8] for f in glob.glob(str(bars_dir / "*.json.gz")))
    if not hits or not bdays:
        print(f"[eval] 評価対象なし（発火 {len(hits)}件）")
        return 0
    with gzip.open(topix_path, "rt") as fh:
        tp = {r["Date"].replace("-", ""): r["C"] for r in json.load(fh)["data"]}

    n_new = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _FAC_CACHE.clear()
    _CAL.clear()
    with ledger.open("a", encoding="utf-8") as fh:
        n_new += _correct_split_rows(rows, fh, bdays, bars_dir, now)
        for h in hits:
            base = base_trading_day(h["run_at"], bdays)
            if base is None:
                continue
            bidx = bdays.index(base)
            for b in h.get("beneficiaries", []):
                code = b["code"]
                # エントリー始値: 基準日 AdjO・欠損なら翌営業日へ1日だけ繰延（凍結）。
                # 繰延先の営業日データが**まだ存在しない**うちは結論を書かず保留する
                # （Codex指摘: 期日前に skipped を確定させると冪等キーで永久に再評価されない）
                entry_day, entry_idx, o = base, bidx, None
                for idx in (bidx, bidx + 1):
                    if idx >= len(bdays):
                        break
                    bar = _bar(bdays[idx], code + "0", bars_dir)
                    if bar and bar.get("AdjO"):
                        entry_day, entry_idx, o = bdays[idx], idx, bar["AdjO"]
                        break
                conclusive = bidx + 1 < len(bdays)     # 繰延先データが実在して初めて欠損が確定
                for win, n_bd in WINDOWS_BD.items():
                    key = (h["link"], code, win)
                    if key in done:
                        continue
                    if o is None:
                        if not conclusive:
                            continue                   # 保留（次回 run で再評価）
                        fh.write(json.dumps({"type": "evaluation_skipped", "link": h["link"],
                                             "code": code, "window": win,
                                             "reason": "始値欠損（基準日+繰延1日とも）",
                                             "run_at": now}, ensure_ascii=False) + "\n")
                        done.add(key)
                        n_new += 1
                        continue
                    if entry_idx + n_bd >= len(bdays):
                        continue                        # 期日未到来
                    ev_day = bdays[entry_idx + n_bd]
                    bar = _bar(ev_day, code + "0", bars_dir)
                    if not bar or not bar.get("AdjC") or entry_day not in tp or ev_day not in tp:
                        continue
                    sbase = _scale_base(entry_day, None)
                    splits = _split_scan(code + "0", entry_day, ev_day, sbase, bars_dir)
                    if splits is None:
                        print(f"[eval] 分割走査の範囲に bars の欠けがあるため保留: {code} {win}")
                        continue                        # 保留（次回 run で再評価）
                    m = jq_fetch.split_scale(ev_day, sbase, splits) if splits else 1.0
                    stock = (bar["AdjC"] * m / o - 1) * 100
                    topix = (tp[ev_day] / tp[entry_day] - 1) * 100
                    row = {"type": "evaluation", "link": h["link"],
                           "code": code, "tier": b.get("tier"),
                           "type_label": b.get("type_label"),
                           "window": win, "entry_day": entry_day,
                           "eval_day": ev_day, "entry_open": o,
                           "stock_pct": round(stock, 2),
                           "topix_pct": round(topix, 2),
                           "excess_pt": round(stock - topix, 2),
                           "split_adjust": SPLIT_ADJUST, "scale_base_date": sbase,
                           "run_at": now}
                    if m != 1.0:
                        row["split_scale"] = m
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    done.add(key)
                    n_new += 1
                    print(f"[eval] {code} {win}: 始値{o}→{stock:+.1f}% "
                          f"TOPIX{topix:+.1f}% 超過{stock - topix:+.1f}pt")
    print(f"[eval] 新規 {n_new} 行")
    return 0


def _correct_split_rows(rows: list[dict], fh, bdays: list[str], bars_dir: Path, now: str) -> int:
    """分割補正なしで書かれた既存の evaluation 行のうち、間に分割があって値が変わる行だけ
    evaluation_correction を追記する（冪等: split_adjust 付きの行・補正済みのキーは触らない・元の行は消さない）。"""
    corrected = {(r.get("link"), r.get("code"), r.get("window")) for r in rows
                 if r.get("type") == "evaluation_correction"}
    n = 0
    for r in rows:
        if r.get("type") != "evaluation" or r.get("split_adjust"):
            continue
        key = (r.get("link"), r.get("code"), r.get("window"))
        if key in corrected or not r.get("entry_open"):
            continue
        entry_day, ev_day = r.get("entry_day"), r.get("eval_day")
        sbase = _scale_base(entry_day, r.get("run_at"))       # 元の評価で始値を読んだ時点の尺度
        splits = _split_scan(r["code"] + "0", entry_day, ev_day, sbase, bars_dir)
        if splits is None:
            continue           # 走査範囲に欠け＝訂正を書かず保留（corrected に入れない＝復旧後に正しく訂正できる）
        if not splits:
            continue                                           # 基準日後に分割の無い銘柄は触らない
        m = jq_fetch.split_scale(ev_day, sbase, splits)
        bar = _bar(ev_day, r["code"] + "0", bars_dir)
        if not bar or not bar.get("AdjC"):
            continue
        stock = (bar["AdjC"] * m / r["entry_open"] - 1) * 100
        # 今の倍率が 1 でも（評価日の足を後で取り直した等）尺度を揃えた今の価格で計算し直し、
        # 記録値と 0.02pt を超えて違う時だけ訂正する（Codex 2026-10-04 2回目）
        if r.get("stock_pct") is not None and abs(round(stock, 2) - r["stock_pct"]) <= 0.02:
            continue
        topix = r.get("topix_pct")
        fh.write(json.dumps({
            "type": "evaluation_correction", "link": r["link"], "code": r["code"],
            "tier": r.get("tier"), "type_label": r.get("type_label"), "window": r["window"],
            "entry_day": entry_day, "eval_day": ev_day, "entry_open": r["entry_open"],
            "stock_pct": round(stock, 2), "topix_pct": topix,
            "excess_pt": round(stock - topix, 2) if topix is not None else None,
            "split_adjust": SPLIT_ADJUST, "split_scale": m, "scale_base_date": sbase,
            "before": {k: r.get(k) for k in ("stock_pct", "excess_pt")},
            "corrects": {"type": "evaluation", "run_at": r.get("run_at")},
            "reason": "株式分割: 評価日の足が基準日の尺度と違った（tasks/split_fix_live_lanes.md・2026-10-04）",
            "run_at": now}, ensure_ascii=False) + "\n")
        corrected.add(key)
        n += 1
        print(f"[eval] 分割補正 {r['code']} {r['window']} {entry_day}→{ev_day}: "
              f"{r.get('stock_pct')}% → {stock:+.2f}%")
    return n


def _effective_evals(rows: list[dict]) -> dict[tuple, dict]:
    """(link, code, window) → 読む側の評価行。同じ鍵の evaluation_correction があればそれを優先（冒頭の規約）。"""
    ev: dict[tuple, dict] = {}
    for r in rows:
        if r.get("type") == "evaluation":
            ev.setdefault((r.get("link"), r.get("code"), r.get("window")), r)
    for r in rows:
        if r.get("type") == "evaluation_correction":
            ev[(r.get("link"), r.get("code"), r.get("window"))] = r
    return ev


def _window_stats(evals: dict[tuple, dict], links: set) -> dict[str, dict]:
    """窓ごとの記述統計（プレレジ§4: 超過 pt の分布だけ・しきい値判定なし）。

    銘柄行= (事象, 受益銘柄) 1 組の超過 pt。事象= 同じ事象の受益銘柄の超過 pt の平均（受益カードを選別しない
    §3 の対象全件を 1 事象に畳む読み方）。勝ち= 超過 pt > 0。
    """
    import statistics
    out = {}
    for win in WINDOWS_BD:
        by_link: dict[str, list[float]] = {}
        for (link, _code, w), r in evals.items():
            if w == win and link in links and r.get("excess_pt") is not None:
                by_link.setdefault(link, []).append(r["excess_pt"])
        rows_x = [x for xs in by_link.values() for x in xs]
        ev_x = [statistics.fmean(xs) for xs in by_link.values()]
        out[win] = {"events": len(ev_x), "event_wins": sum(x > 0 for x in ev_x),
                    "event_median": round(statistics.median(ev_x), 2) if ev_x else None,
                    "rows": len(rows_x), "row_wins": sum(x > 0 for x in rows_x),
                    "row_median": round(statistics.median(rows_x), 2) if rows_x else None}
    return out


def report(ledger: Path = LEDGER) -> str:
    """成績表（読み取り専用・台帳に書かない）。v2= config_version 2 の代表行すべて（従来の評価対象）／
    v3= そのうち v3_target=true の代表行だけ（掲載 2 日以内・プレレジ§8・凍結 2026-10-12）。
    期間・指標は両方とも §3/§4 と同じ物を使い、v3 の事象が 0 件でも落とさず「0 件」と出す。"""
    rows = []
    if ledger.exists():
        rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    fp_links = {r.get("link") for r in rows if r.get("type") == "false_positive"}
    hits = _representative_hits(rows, fp_links)
    evals = _effective_evals(rows)
    v2 = [h for h in hits if h.get("config_version") == 2]
    v3 = [h for h in v2 if h.get("v3_target") is True]
    n_v1 = len(hits) - len(v2)
    lines = ["# news_shock 成績表（記述統計のみ・しきい値判定なし・正本= tasks/news_shock_preregister.md §3/§4/§8）",
             f"- 台帳: {ledger}（hit 行 {sum(1 for r in rows if r.get('status') == 'hit')}・"
             f"false_positive {len(fp_links)}・v1 行 {n_v1} は両表に入れない）",
             "- 事象= 代表行（各 event_id の最初の非 FP 行）・事象の超過 pt= 受益銘柄の平均・勝ち= 超過 pt > 0・"
             "訂正行（evaluation_correction）を優先"]
    for title, hs in (("v2（従来の発火条件・凍結 2026-08-16）", v2),
                      ("v3（v2 ＋ 掲載 2 日以内・凍結 2026-10-12・§8）", v3)):
        st = _window_stats(evals, {h["link"] for h in hs})
        lines += ["", f"## {title}", f"- 発火事象 {len(hs)} 件（評価は期日到来分のみ）"]
        if not hs:
            lines.append("- 対象 0 件（まだ発火していない）")
        lines += ["", "| 窓 | 評価済み事象 | 勝ち（事象） | 中央値 pt（事象） | 銘柄行 | 勝ち（行） | 中央値 pt（行） |",
                  "|---|---|---|---|---|---|---|"]
        for win, n_bd in WINDOWS_BD.items():
            s = st[win]
            fmt = lambda x: "—" if x is None else f"{x:+.2f}"
            lines.append(f"| +{n_bd} 営業日 | {s['events']} | {s['event_wins']} | {fmt(s['event_median'])} | "
                         f"{s['rows']} | {s['row_wins']} | {fmt(s['row_median'])} |")
    return "\n".join(lines)


def _selftest() -> int:
    import tempfile
    ng = 0
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bars = td / "bars"; bars.mkdir()
        days = [f"2026010{i}" for i in range(1, 10)]     # 1/1〜1/9 を営業日とする
        # 分割走査が実データの取引所カレンダー・取得記録を読まないよう、模擬の置き場へ差し替える（2026-10-04）
        saved_root, saved_log = jq_fetch.DATA_ROOT, jq_fetch.LOG_PATH
        jq_fetch.DATA_ROOT, jq_fetch.LOG_PATH = td, td / "fetch_log.jsonl"
        jq_fetch.bars_fetch_history.cache_clear()
        with gzip.open(td / "calendar.json.gz", "wt") as fh:
            json.dump({"data": [{"Date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "HolDiv": "1"} for d in days]}, fh)
        for i, d in enumerate(days):
            with gzip.open(bars / f"{d}.json.gz", "wt") as fh:
                json.dump({"data": [{"Code": "99990", "AdjO": 100 + i, "AdjC": 102 + i}]}, fh)
        with gzip.open(td / "topix.json.gz", "wt") as fh:
            json.dump({"data": [{"Date": f"2026-01-0{i+1}", "C": 100 + i} for i in range(9)]}, fh)
        # 繰延テスト用: 7777 は基準日(1/2)に存在せず 1/3 から出現（→ entry は 1/3 へ繰延）
        with gzip.open(bars / "20260103.json.gz", "wt") as fh:
            json.dump({"data": [{"Code": "99990", "AdjO": 102, "AdjC": 104},
                                {"Code": "77770", "AdjO": 200, "AdjC": 202}]}, fh)
        for i in range(3, 9):   # 1/4以降にも 7777 の終値を足す
            with gzip.open(bars / f"2026010{i+1}.json.gz", "wt") as fh:
                json.dump({"data": [{"Code": "99990", "AdjO": 100 + i, "AdjC": 102 + i},
                                    {"Code": "77770", "AdjO": 200 + i, "AdjC": 202 + i}]}, fh)
        led = td / "log.jsonl"
        mk = lambda run_at, link, code="9999": {"run_at": run_at, "link": link, "status": "hit",
                                   "config_version": 2,
                                   "beneficiaries": [{"code": code, "tier": "confirmed",
                                                      "type_label": "価格直結型"}]}
        rows = [{**mk("2026-01-01T21:20:00+00:00", "http://a"), "v3_target": True},   # JST 1/2 06:20 <9時 → 基準1/2・v3 対象
                {**mk("2026-01-02T10:00:00+00:00", "http://b"), "v3_target": False},  # JST 1/2 19:00 ≥9時 → 基準1/3
                mk("2026-01-01T21:20:00+00:00", "http://c", "7777"),  # 基準1/2でAdjO欠損→1/3へ繰延
                mk("2026-01-08T22:20:00+00:00", "http://d", "8888"),  # 基準1/9=最終日でAdjO欠損・繰延先データ未到来→保留
                {"run_at": "2026-01-01T21:20:00+00:00", "link": "http://fp", "status": "hit",
                 "beneficiaries": [{"code": "9999", "tier": "confirmed", "type_label": ""}]},
                {"type": "false_positive", "link": "http://fp"},
                # 代表行の繰り上げ: 同一 event_id の先頭行がFP → 2件目が代表として評価される
                {**mk("2026-01-01T21:20:00+00:00", "http://e1"), "event_id": "ev1",
                 "dup_event": False},
                {**mk("2026-01-01T21:20:00+00:00", "http://e2"), "event_id": "ev1",
                 "dup_event": True},
                {"type": "false_positive", "link": "http://e1"}]
        led.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                       encoding="utf-8")
        evaluate(led, bars, td / "topix.json.gz")
        out = [json.loads(l) for l in led.read_text(encoding="utf-8").splitlines()]
        evs = [r for r in out if r.get("type") == "evaluation"]
        a5 = [r for r in evs if r["link"] == "http://a" and r["window"] == "w5"]
        b5 = [r for r in evs if r["link"] == "http://b" and r["window"] == "w5"]
        c5 = [r for r in evs if r["link"] == "http://c" and r["window"] == "w5"]
        d_rows = [r for r in out if r.get("link") == "http://d"
                  and r.get("type") in ("evaluation", "evaluation_skipped")]
        checks = [
            (a5 and a5[0]["entry_day"] == "20260102", "09:00前発火→当日寄りが基準"),
            (b5 and b5[0]["entry_day"] == "20260103", "09:00以後発火→翌営業日寄りが基準"),
            (a5 and abs(a5[0]["stock_pct"] - ((108 / 101 - 1) * 100)) < 0.01,
             "式=基準始値→+5営業日終値"),
            (not any(r["link"] == "http://fp" for r in evs), "false_positive は評価しない"),
            (any(r["link"] == "http://e2" for r in evs)
             and not any(r["link"] == "http://e1" for r in evs),
             "代表行がFP化されたら同一event_idの次の非FP行へ繰り上げ"),
            (c5 and c5[0]["entry_day"] == "20260103", "基準日にAdjO欠損→翌営業日へ1日繰延"),
            (not d_rows, "繰延先の営業日データ未到来なら結論を書かず保留（skipped を確定させない）"),
        ]
        n_before = len(out)
        evaluate(led, bars, td / "topix.json.gz")       # 再実行
        n_after = len(led.read_text(encoding="utf-8").splitlines())
        checks.append((n_before == n_after, "再実行で増えない（冪等）"))
        # 成績表（§8）: v3 の表は v3_target=true の代表行だけを数える・v2 の表は従来の全代表行・読み取り専用
        rep = report(led)
        st_v2 = _window_stats(_effective_evals(out), {r["link"] for r in rows
                                                        if r.get("status") == "hit" and r.get("config_version") == 2})
        st_v3 = _window_stats(_effective_evals(out), {"http://a"})
        checks.append((st_v3["w5"]["events"] == 1 and st_v2["w5"]["events"] >= 3
                       and "v3（v2 ＋ 掲載 2 日以内" in rep and "| +5 営業日 | 1 | " in rep.split("## v3")[1]
                       and n_after == len(led.read_text(encoding="utf-8").splitlines()),
                       "成績表: v3 表は v3_target の事象だけ・v2 表は全事象・台帳に書かない"))
        checks.append(("対象 0 件" in report(td / "none.jsonl"), "成績表: 台帳なし/v3 0 件でも落ちず「0 件」と出す"))
        for ok, why in checks:
            print(f"  {'OK ' if ok else 'NG '} {why}")
            ng += not ok
        jq_fetch.DATA_ROOT, jq_fetch.LOG_PATH = saved_root, saved_log
        jq_fetch.bars_fetch_history.cache_clear()
    return ng


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--report", action="store_true",
                    help="成績表（v2 全発火と v3=掲載 2 日以内の 2 表・記述統計のみ）を表示する。台帳に書かない")
    args = ap.parse_args()
    if args.selftest:
        ng = _selftest()
        print("OK: 全通過" if not ng else f"NG: {ng}件失敗")
        return 1 if ng else 0
    if args.report:
        print(report())
        return 0
    return evaluate()


if __name__ == "__main__":
    sys.exit(main())
