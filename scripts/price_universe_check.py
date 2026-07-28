"""price_universe_check: B2B商品価格の週次チェッカー（値上がり検出レーンの非X部品）.

configs/price_universe_sources.json の約20系列（TradingEconomics + 田中貴金属）を
requests で取得し、data/price_watch/universe_weekly.jsonl へ append。閾値
（weekly% または 4週累積%）を超えた系列だけを表出力する。

設計規約:
- 取得失敗(status=error)と値ゼロを混ぜない。全滅時のみ exit 1
- TE は存在しないスラッグでも 200 を返すため、行スコープ + ラベル一致で実在判定
- 前回値から±50%超の跳びは suspect（サイト構造変化の疑い）としてアラート対象外
- 週次実行（手動）: docker compose run --rm xstock python scripts/price_universe_check.py

出典設計: docs/price-watch-universe.md「実装注意」節・2026-07-28 実測（copper 6.35 等）。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
CONFIG_PATH = APP / "configs/price_universe_sources.json"
LEDGER_PATH = APP / "data/price_watch/universe_weekly.jsonl"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TAG_RE = re.compile(r"<[^>]+>")
NUM_RE = re.compile(r"-?[0-9][0-9,]*\.?[0-9]*")


def strip_tags(html: str) -> str:
    return TAG_RE.sub(" ", html)


def fetch(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    if resp.encoding in (None, "ISO-8859-1"):
        resp.encoding = resp.apparent_encoding
    return resp.text


def parse_te(html: str, slug: str, label: str) -> dict | None:
    """TE商品ページから該当行を行スコープで抽出（重複行は先頭固定・ラベル一致必須）。"""
    for row_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        row = row_match.group(1)
        if not re.search(rf"/commodity/{re.escape(slug)}[\"'/?#]", row):
            continue
        cells = [strip_tags(c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        # 空セルを除去しない（除去すると列が左詰めされ値を取り違える・Codex CONFIRMED-1）。
        # 値=name以降で「%を含まない最初の数値」、%列=「%を含むセル」を出現順に[Day,Weekly,Monthly]
        name_idx = next((i for i, c in enumerate(cells) if c), None)
        if name_idx is None or label.lower() not in cells[name_idx].lower():
            continue
        value = None
        pcts: list[float | None] = []
        src_date = ""
        for c in cells[name_idx + 1:]:
            if not c:
                pcts.append(None) if False else None
                continue
            m = NUM_RE.search(c.replace("%", ""))
            if "%" in c:
                pcts.append(float(m.group().replace(",", "")) if m else None)
            elif value is None and m and not re.search(r"[A-Za-z]", c):
                value = float(m.group().replace(",", ""))
            elif re.search(r"[A-Za-z]", c):
                src_date = c  # 日付セル（Jul/28等）は数値扱いしない（Codex SUSPECT-1）
        return {
            "value": value,
            "day_pct": pcts[0] if len(pcts) > 0 else None,
            "weekly_pct": pcts[1] if len(pcts) > 1 else None,
            "monthly_pct": pcts[2] if len(pcts) > 2 else None,
            "src_date": src_date,
        }
    return None


def parse_tanaka(html: str) -> dict | None:
    """田中貴金属の店頭金小売価格（円/g）。「金」ラベル近傍の最初の5桁数値を採る。"""
    text = strip_tags(html)
    m = re.search(r"金[^銀白]{0,120}?([0-9]{2},[0-9]{3})\s*円", text)
    if not m:
        return None
    return {"value": float(m.group(1).replace(",", "")), "day_pct": None,
            "weekly_pct": None, "monthly_pct": None, "src_date": ""}


def load_history() -> dict[str, list[dict]]:
    hist: dict[str, list[dict]] = {}
    if LEDGER_PATH.exists():
        for line in LEDGER_PATH.read_text().splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            hist.setdefault(r.get("id", ""), []).append(r)
    return hist


def main() -> int:
    cfg = json.loads(CONFIG_PATH.read_text())
    alert_cfg = cfg["alert"]
    history = load_history()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # TE は一覧ページ1回で全系列の行を取得（リクエスト削減・自ページに行が無い系列対策）。
    # 一覧に無い系列のみ個別ページへフォールバック
    try:
        te_index_html = fetch("https://tradingeconomics.com/commodities")
    except Exception as exc:  # noqa: BLE001
        te_index_html = ""
        print(f"[warn] TE一覧ページ取得失敗: {str(exc)[:80]}")

    rows, alerts = [], []
    for s in cfg["series"]:
        base = {"date": today, "id": s["id"], "jp": s["jp"], "run_at": run_at}
        try:
            if s["type"] == "te":
                parsed = parse_te(te_index_html, s["slug"], s["label"]) if te_index_html else None
                if parsed is None:
                    parsed = parse_te(fetch(f"https://tradingeconomics.com/commodity/{s['slug']}"),
                                      s["slug"], s["label"])
            else:
                parsed = parse_tanaka(fetch("https://gold.tanaka.co.jp/commodity/souba/"))
            if parsed is None or parsed["value"] is None:
                rows.append({**base, "status": "parse_fail"})
                print(f"[parse_fail] {s['id']}")
                continue
            prev = [r for r in history.get(s["id"], []) if r.get("status") == "ok"]
            suspect = bool(prev and prev[-1].get("value") and
                           abs(parsed["value"] / prev[-1]["value"] - 1) > 0.5)
            row = {**base, **parsed, "status": "suspect_jump" if suspect else "ok"}
            rows.append(row)
            # 4週累積: 日付基準で25〜35日前の最新レコードと比較（同日再実行・実行間隔の
            # 乱れに頑健・Codex CONFIRMED-2）。該当なしなら判定しない
            four_w = None
            by_date: dict[str, dict] = {}
            for r in sorted(prev, key=lambda x: (x.get("date", ""), x.get("run_at", ""))):
                by_date[r.get("date", "")] = r
            target_dt = datetime.strptime(today, "%Y-%m-%d")
            cands = [r for d, r in by_date.items()
                     if d and 25 <= (target_dt - datetime.strptime(d, "%Y-%m-%d")).days <= 35
                     and r.get("value")]
            if cands:
                four_w = (parsed["value"] / cands[-1]["value"] - 1) * 100
            trigger = []
            if row["status"] == "ok":
                if parsed.get("weekly_pct") is not None and parsed["weekly_pct"] >= alert_cfg["weekly_pct"]:
                    trigger.append(f"weekly {parsed['weekly_pct']:+.1f}%")
                if four_w is not None and four_w >= alert_cfg["four_week_pct"]:
                    trigger.append(f"4週累積 {four_w:+.1f}%")
            if trigger:
                alerts.append((s, row, trigger))
            wk = f"{parsed['weekly_pct']:+.1f}%" if parsed.get("weekly_pct") is not None else "-"
            print(f"[ok] {s['id']:<12} {parsed['value']:>10} weekly={wk} {row['status'] if row['status']!='ok' else ''}")
        except Exception as exc:  # noqa: BLE001  取得失敗は系列単位fail-soft
            rows.append({**base, "status": "error", "error": str(exc)[:100]})
            print(f"[error] {s['id']}: {str(exc)[:80]}")

    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    ok = sum(1 for r in rows if r["status"] == "ok")
    print(f"\n[done] {ok}/{len(rows)} ok → {LEDGER_PATH}")
    if alerts:
        print(f"\n🚨 閾値超え {len(alerts)} 系列:")
        for s, row, trigger in alerts:
            print(f"  {s['jp']}（{'/'.join(trigger)}）→ 受益: {s['stocks']}")
    else:
        print("閾値超えなし（4週累積は履歴4本蓄積後から判定）")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
