"""R2「時計」レポート: 本線(07:20/19:00) vs 高頻度probe の first_seen を突き合わせる.

指標の正本= tasks/news_shock_preregister.md §7（凍結）。読み取り専用・何度でも再実行可。

測るもの（link 単位で結合し、event_id 単位に代表集計）:
- pub→probe: 媒体掲載(pubdate) → probe が最初に見た時刻（収録遅延+最大2hのポーリング待ち）
- pub→main : 媒体掲載 → 本線が最初に見た時刻（同+最大12.3hの定時待ち）
- polling_gain_hours = main − probe（正= probe が早い）
- boundary_cross = probe の時刻なら本線より**1営業日早い寄り付きで買えたか**
  （エントリー規則はプレレジ§3と同一: JST 09:00 境界→営業日繰り上げ）

分析対象の凍結規則: pubdate >= 分析開始時刻（= 本線v2凍結 2026-08-16T12:33:03Z と
probe 初回実行時刻の遅い方）。両レーンの初回バックフィル行を除外するため。

実行:
    python3 scripts/news_shock_clock_report.py            # 集計を表示+md出力
    python3 scripts/news_shock_clock_report.py --selftest
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "scripts"))
from news_shock_eval import base_trading_day  # noqa: E402  # エントリー規則の正規実装を共有

MAIN = APP / "data/news_shock/news_log.jsonl"
PROBE = APP / "data/news_shock/first_seen_probe.jsonl"
BARS_DIR = APP / "data/jquants/bars"
OUT_MD = APP / "output/news_shock_clock.md"
V2_START = "2026-08-16T12:33:03+00:00"    # 本線v2凍結時刻（プレレジ§6・不変）


def _parse_pub(pubdate: str) -> datetime | None:
    try:
        return parsedate_to_datetime(pubdate).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _load(path: Path, ts_key: str) -> tuple[dict[tuple[str, str], dict], datetime | None]:
    """(term, link) -> 最初に見えた行。あわせて最初の run_summary.run_at（稼働開始アンカー）を返す。

    アンカーに hit でなく run_summary を使うのは、初回実行が0件でも稼働開始時刻が
    動かないようにするため（Codex R2審 C1）。
    """
    out: dict[tuple[str, str], dict] = {}
    anchor: datetime | None = None
    if not path.exists():
        return out, anchor
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("type") == "run_summary" and anchor is None:
            anchor = datetime.fromisoformat(r["run_at"].replace("Z", "+00:00"))
            continue
        if r.get("status") != "hit" or not r.get("link"):
            continue
        key = (r.get("term", ""), r["link"].strip())
        if key not in out:
            r["_ts"] = datetime.fromisoformat(r[ts_key].replace("Z", "+00:00"))
            out[key] = r
    return out, anchor


def build_report(main_rows: dict[tuple[str, str], dict],
                 probe_rows: dict[tuple[str, str], dict],
                 bdays: list[str], analysis_start: datetime) -> dict:
    joined = []
    n_main_only = n_bad_pub = n_excluded = n_event_mismatch = 0
    for key, m in main_rows.items():
        p = probe_rows.get(key)
        if p is None:
            n_main_only += 1
            continue
        pub = _parse_pub(m.get("pubdate", ""))
        if pub is None:
            n_bad_pub += 1
            continue
        if pub < analysis_start:
            n_excluded += 1                             # バックフィル除外（凍結規則）
            continue
        if m.get("event_id") != p.get("event_id"):
            n_event_mismatch += 1                       # 両側の event 整合検査（W1）
        gain_h = (m["_ts"] - p["_ts"]).total_seconds() / 3600
        m_day = base_trading_day(m["_ts"].isoformat(), bdays)
        p_day = base_trading_day(p["_ts"].isoformat(), bdays)
        joined.append({
            "link": key[1], "event_id": m.get("event_id"),
            "pub_to_probe_h": round((p["_ts"] - pub).total_seconds() / 3600, 2),
            "pub_to_main_h": round((m["_ts"] - pub).total_seconds() / 3600, 2),
            "polling_gain_h": round(gain_h, 2),
            "boundary_cross": bool(p_day and m_day and p_day < m_day),
            "title": m.get("title", "")[:70]})
    n_probe_only = sum(1 for k in probe_rows if k not in main_rows)
    # event 単位の代表 = 各 event_id で最初に probe が見た link
    by_event: dict[str, dict] = {}
    for j in sorted(joined, key=lambda x: x["pub_to_probe_h"]):
        by_event.setdefault(j["event_id"] or j["link"], j)
    events = list(by_event.values())
    rep = {"n_links": len(joined), "n_events": len(events),
           "n_main_only": n_main_only, "n_probe_only": n_probe_only,
           "n_bad_pubdate": n_bad_pub, "n_excluded_backfill": n_excluded,
           "n_event_mismatch": n_event_mismatch}
    if events:
        rep["median_pub_to_probe_h"] = round(statistics.median(
            e["pub_to_probe_h"] for e in events), 2)
        rep["median_pub_to_main_h"] = round(statistics.median(
            e["pub_to_main_h"] for e in events), 2)
        rep["median_polling_gain_h"] = round(statistics.median(
            e["polling_gain_h"] for e in events), 2)
        rep["boundary_cross_rate"] = round(
            sum(e["boundary_cross"] for e in events) / len(events), 3)
    rep["events"] = events
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return 1 if _selftest() else 0

    main_rows, _ = _load(MAIN, "run_at")
    probe_rows, probe_anchor = _load(PROBE, "first_seen_at")
    bdays = sorted(Path(f).name[:8] for f in glob.glob(str(BARS_DIR / "*.json.gz")))
    v2 = datetime.fromisoformat(V2_START)
    # 分析開始アンカー= probe の最初の run_summary.run_at（0件初回でも固定・Codex C1）
    analysis_start = max(v2, probe_anchor) if probe_anchor else v2
    rep = build_report(main_rows, probe_rows, bdays, analysis_start)
    print(f"[clock] 結合 {rep['n_links']} link / {rep['n_events']} 事象"
          f"（分析開始 {analysis_start.isoformat()} 以後の掲載のみ）")
    print(f"  欠測: 本線のみ{rep['n_main_only']} / probeのみ{rep['n_probe_only']}"
          f" / pubdate不正{rep['n_bad_pubdate']} / バックフィル除外{rep['n_excluded_backfill']}"
          f" / event不整合{rep['n_event_mismatch']}")
    if rep["n_events"]:
        print(f"  掲載→probe 中央値: {rep['median_pub_to_probe_h']}h"
              f" / 掲載→本線 中央値: {rep['median_pub_to_main_h']}h")
        print(f"  ポーリング短縮 中央値: {rep['median_polling_gain_h']}h"
              f" / 営業日境界を跨いだ率: {rep['boundary_cross_rate']:.1%}")
        n_cross = sum(e["boundary_cross"] for e in rep["events"])
        print(f"  R3判定（凍結条件: n>=30 かつ 跨ぎ率>=10% かつ 跨ぎ>=3件）: "
              f"n={rep['n_events']} 率={rep['boundary_cross_rate']:.1%} 件数={n_cross}")
    lines = ["# news_shock 時計レポート（R2・機械生成）", "",
             f"> 生成: {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
             f"／再生成: `python3 scripts/news_shock_clock_report.py`"
             f"／指標の正本: tasks/news_shock_preregister.md §7", "",
             f"- 結合 {rep['n_links']} link / **{rep['n_events']} 事象**"
             f"（欠測: 本線のみ{rep['n_main_only']}・probeのみ{rep['n_probe_only']}"
             f"・pubdate不正{rep['n_bad_pubdate']}・バックフィル除外{rep['n_excluded_backfill']}"
             f"・event不整合{rep['n_event_mismatch']}）",
             f"- 掲載→probe 中央値: **{rep.get('median_pub_to_probe_h', '—')}h**"
             f" ／ 掲載→本線 中央値: **{rep.get('median_pub_to_main_h', '—')}h**",
             f"- ポーリング短縮 中央値: **{rep.get('median_polling_gain_h', '—')}h**"
             f" ／ **営業日境界を跨いだ率: "
             f"{rep.get('boundary_cross_rate', 0):.1%}**（R3判定に使う数字）",
             f"- **R3凍結条件**（プレレジ§7）: n>=30 かつ 跨ぎ率>=10% かつ 跨ぎ>=3件 ／ "
             f"現在値: n={rep['n_events']}・率={rep.get('boundary_cross_rate', 0):.1%}"
             f"・件数={sum(e['boundary_cross'] for e in rep['events'])}", "",
             "| 事象 | 掲載→probe(h) | 掲載→本線(h) | 短縮(h) | 境界跨ぎ |", "|---|---|---|---|---|"]
    for e in rep["events"][:40]:
        lines.append(f"| {e['title']} | {e['pub_to_probe_h']} | {e['pub_to_main_h']}"
                     f" | {e['polling_gain_h']} | {'✅' if e['boundary_cross'] else '—'} |")
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[clock] {OUT_MD} へ出力")
    return 0


def _selftest() -> int:
    bdays = ["20260114", "20260115", "20260116", "20260119"]  # 金曜1/16→月曜1/19
    mk = lambda link, ts, pub, eid="e1": {"link": link, "event_id": eid, "status": "hit",
                                          "pubdate": pub, "title": link,
                                          "_ts": datetime.fromisoformat(ts)}
    start = datetime.fromisoformat("2026-01-14T00:00:00+00:00")
    # 事象1: 掲載 1/15 20:00 UTC → probe 1/15 21:00 UTC(JST 1/16 06:00 寄り前=1/16エントリー)
    #        本線 1/16 10:00 UTC(JST 19:00 ≥9時 → 翌営業日 1/19 エントリー) ＝境界跨ぎ✅
    m = {("q", "a"): mk("a", "2026-01-16T10:00:00+00:00", "Thu, 15 Jan 2026 20:00:00 GMT")}
    p = {("q", "a"): mk("a", "2026-01-15T21:00:00+00:00", "Thu, 15 Jan 2026 20:00:00 GMT")}
    # 事象2: 両者同じ営業日に落ちる（跨がない）
    m[("q", "b")] = mk("b", "2026-01-15T05:00:00+00:00", "Thu, 15 Jan 2026 01:00:00 GMT", "e2")
    p[("q", "b")] = mk("b", "2026-01-15T03:00:00+00:00", "Thu, 15 Jan 2026 01:00:00 GMT", "e2")
    # 除外: 分析開始前の掲載
    m[("q", "c")] = mk("c", "2026-01-15T05:00:00+00:00", "Mon, 12 Jan 2026 01:00:00 GMT", "e3")
    p[("q", "c")] = mk("c", "2026-01-15T03:00:00+00:00", "Mon, 12 Jan 2026 01:00:00 GMT", "e3")
    # 片側欠落: 本線のみ / probe のみ
    m[("q", "d")] = mk("d", "2026-01-15T05:00:00+00:00", "Thu, 15 Jan 2026 01:00:00 GMT", "e4")
    p[("q", "e")] = mk("e", "2026-01-15T03:00:00+00:00", "Thu, 15 Jan 2026 01:00:00 GMT", "e5")
    rep = build_report(m, p, bdays, start)
    checks = [
        (rep["n_events"] == 2, "分析開始前の掲載は除外（2事象）"),
        (rep["n_main_only"] == 1 and rep["n_probe_only"] == 1
         and rep["n_excluded_backfill"] == 1, "欠測集計（本線のみ1・probeのみ1・除外1）"),
        (any(e["link"] == "a" and e["boundary_cross"] for e in rep["events"]),
         "probe が寄り前・本線が引け後→営業日境界跨ぎ✅"),
        (any(e["link"] == "b" and not e["boundary_cross"] for e in rep["events"]),
         "同じ営業日に落ちる場合は跨ぎ扱いしない"),
        (any(e["link"] == "a" and abs(e["polling_gain_h"] - 13.0) < 0.01
             for e in rep["events"]), "短縮時間= 本線−probe（13h）"),
    ]
    ng = 0
    for ok, why in checks:
        print(f"  {'OK ' if ok else 'NG '} {why}")
        ng += not ok
    print("OK: 全通過" if not ng else f"NG: {ng}件失敗")
    return ng


if __name__ == "__main__":
    sys.exit(main())
