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
        # 列構成を%セル数で判定する（位置固定は禁止・A-1）。
        #   一覧(/commodities): [%Chg, Weekly, Monthly, YTD, YoY] = 5個 → weekly は pcts[1]
        #   個別(/commodity/x): [Day, Month, Year]                = 3個 → **weekly 列は存在しない**
        # 未知の列数は誤ラベルを避けるため値を採らない（parse_fail にする）。
        n_pct = len(pcts)
        if n_pct >= 5:
            return {"value": value, "day_pct": pcts[0], "weekly_pct": pcts[1],
                    "monthly_pct": pcts[2], "src_date": src_date, "layout": "index5"}
        if n_pct == 3:
            return {"value": value, "day_pct": pcts[0], "weekly_pct": None,
                    "monthly_pct": pcts[1], "src_date": src_date, "layout": "detail3"}
        return None
    return None


def parse_tanaka(html: str) -> dict | None:
    """田中貴金属の金・店頭小売価格（円/g）と公表日を採る。

    A-2対策: (1)数値に左境界を付け6桁以上にも対応（10万円/g超で下5桁を拾う事故の防止）
    (2)「店頭小売価格」にアンカー（買取価格へ静かにズレるのを防ぐ）(3)公表日を src_date に格納
    （実行が公表時刻09:30 JST より前だと前日値を当日として記録する事故の検知用）。
    """
    text = re.sub(r"\s+", " ", strip_tags(html))
    # 金ブロック = 「金」〜「プラチナ」までにスライスしてから小売価格を探す
    gold_start = text.find("公表")
    block = text[gold_start:text.find("プラチナ", gold_start)] if gold_start >= 0 else text
    m = re.search(r"店頭小売価格[^0-9]{0,40}?(?<![0-9,])([0-9]{1,3}(?:,[0-9]{3})+)\s*円", block)
    if not m:
        return None
    d = re.search(r"(\d{4})年(\d{2})月(\d{2})日", text)
    src_date = f"{d.group(1)}-{d.group(2)}-{d.group(3)}" if d else ""
    return {"value": float(m.group(1).replace(",", "")), "day_pct": None,
            "weekly_pct": None, "monthly_pct": None, "src_date": src_date, "layout": "tanaka"}


def parse_scfi(payload: dict) -> dict | None:
    """SCFI総合指数（en.sse.net.cn の週次JSON API・2026-07-28 実測スキーマ）。

    dataItemTypeName=="SCFI_T" が総合指数行。前週比%（percentage）が API 側で直接提供される
    ため weekly_pct にそのまま渡せる（TE と違い自前履歴に依存しない）。src_date=指数の公表日
    （週次金曜のため当日一致は要求しない）。
    """
    for line in (payload.get("data") or {}).get("lineDataList", []):
        if line.get("dataItemTypeName") == "SCFI_T" and line.get("currentContent") is not None:
            pct = line.get("percentage")
            return {"value": float(line["currentContent"]), "day_pct": None,
                    "weekly_pct": float(pct) if pct is not None else None,
                    "monthly_pct": None,
                    "src_date": (payload.get("data") or {}).get("currentDate", ""),
                    "layout": "scfi_json"}
    return None


def parse_jepx(today: str) -> dict | None:
    """JEPXスポット（システムプライス）の日平均から週次変化率を自前計算する。

    公開CSV https://www.jepx.jp/market/excel/spot_YYYY.csv（Shift-JIS・1日48コマ）。
    value=48コマ揃った直近日の平均（円/kWh）、weekly_pct=直近7日平均÷その前7日平均。
    列は名前でアンカーする（位置固定は禁止・TE列ズレ事故と同じ轍を踏まないため）。

    **YYYY は暦年でなく年度（4月始まり）**（2026-07-28 実測: spot_2025.csv = 2025/04/01〜2026/03/31・
    spot_2027.csv は404）。暦年で組むと1〜3月に存在しないファイルを取りにいって系列ごと落ちる。
    年度替わり直後（4月上旬）は当年度分が14日に満たないため前年度CSVも連結する。
    """
    import csv as _csv
    import io as _io
    from statistics import mean as _mean

    fy = int(today[:4]) if int(today[5:7]) >= 4 else int(today[:4]) - 1  # 年度（4月始まり）
    daily: dict[str, list[float]] = {}
    for y in (fy, fy - 1):
        try:
            raw = requests.get(f"https://www.jepx.jp/market/excel/spot_{y}.csv",
                               headers={"User-Agent": UA}, timeout=60)
            raw.raise_for_status()
        except Exception:  # noqa: BLE001  前年度分は存在しない/不要なこともある
            if y == fy:
                raise
            continue
        rows = list(_csv.reader(_io.StringIO(raw.content.decode("shift_jis", errors="replace"))))
        if not rows:
            continue
        try:
            si = rows[0].index("システムプライス(円/kWh)")
        except ValueError:
            continue  # 列名が変わったら黙って別列を採らない
        for r in rows[1:]:
            if len(r) <= si or not r[0]:
                continue
            try:
                daily.setdefault(r[0], []).append(float(r[si]))
            except ValueError:
                continue
        if len(daily) >= 14:
            break
    avg = {d: _mean(v) for d, v in daily.items() if len(v) == 48}
    days = sorted(avg)
    if len(days) < 14:
        return None
    cur = _mean(avg[d] for d in days[-7:])
    prev = _mean(avg[d] for d in days[-14:-7])
    return {"value": round(avg[days[-1]], 2), "day_pct": None,
            "weekly_pct": round((cur / prev - 1) * 100, 2) if prev else None,
            "monthly_pct": None, "src_date": days[-1].replace("/", "-"), "layout": "jepx_csv"}


def beneficiaries_display(s: dict, today: str) -> str:
    """受益カード（帰属プロトコルv2）の発火時表示。

    sign=+ かつ confirmed/provisional のみ表示（rejected は買いシグナル禁止）。
    provisional は (仮)、verified から12ヶ月超は (STALE要再確認) を付ける。
    正カードゼロの系列は「受益者なし」＝記録のみで銘柄を後付けしない（関門B逆流防止）。
    """
    cards = [b for b in s.get("beneficiaries", [])
             if b.get("sign") == "+" and b.get("tier") in ("confirmed", "provisional")]
    if not cards:
        return "受益者なし(TOP1000内・発火記録のみ)"
    parts = []
    t = datetime.strptime(today, "%Y-%m-%d")
    for b in cards:
        tag = "" if b["tier"] == "confirmed" else "(仮)"
        stale = "(STALE要再確認)"  # verified 無しは無期限に新鮮扱いしない（Codex軽微指摘）
        if b.get("verified"):
            age = (t - datetime.strptime(b["verified"], "%Y-%m-%d")).days
            if age <= 365:
                stale = ""
        parts.append(f"{b['code']}{tag}{stale}")
    return "/".join(parts)


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
        # 一覧が取れないと全系列が列構成の違う個別ページへ落ちるため即停止（A-1）
        print(f"[FATAL] TE一覧ページ取得失敗のため中断（誤列での記録を防ぐ）: {str(exc)[:100]}")
        return 1

    rows, alerts = [], []
    for s in cfg["series"]:
        base = {"date": today, "id": s["id"], "jp": s["jp"], "run_at": run_at}
        try:
            if s["type"] == "te":
                parsed = parse_te(te_index_html, s["slug"], s["label"]) if te_index_html else None
                if parsed is None:
                    parsed = parse_te(fetch(f"https://tradingeconomics.com/commodity/{s['slug']}"),
                                      s["slug"], s["label"])
            elif s["type"] == "scfi":
                parsed = parse_scfi(json.loads(fetch("https://en.sse.net.cn/currentIndex?indexName=scfi")))
            elif s["type"] == "jepx":
                parsed = parse_jepx(today)
            else:
                parsed = parse_tanaka(fetch("https://gold.tanaka.co.jp/commodity/souba/"))
            if parsed is None or parsed["value"] is None:
                rows.append({**base, "status": "parse_fail"})
                print(f"[parse_fail] {s['id']}")
                continue
            prev = [r for r in history.get(s["id"], []) if r.get("status") == "ok"]
            suspect = bool(prev and prev[-1].get("value") and
                           abs(parsed["value"] / prev[-1]["value"] - 1) > 0.5)
            status = "suspect_jump" if suspect else "ok"
            # 公表日が当日でない系列は stale（前日値を当日として記録する事故の検知・A-2）
            if parsed.get("layout") == "tanaka" and parsed.get("src_date") and \
                    parsed["src_date"] != datetime.now().strftime("%Y-%m-%d"):
                status = "stale"
            # JEPXは日次公表（翌日受渡分まで出る）。3日以上古い＝公開停止/取得ズレの検知
            if parsed.get("layout") == "jepx_csv" and parsed.get("src_date") and \
                    (datetime.strptime(today, "%Y-%m-%d")
                     - datetime.strptime(parsed["src_date"], "%Y-%m-%d")).days > 3:
                status = "stale"
            row = {**base, **parsed, "status": status}
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
            # サイト側が週次%を出さない系列（田中貴金属・BDI個別ページ等）は自前履歴から算出する
            # （P0-②・2026-07-28。これが無いと weekly が永久に None で発火経路が4週累積だけになる）。
            # 自前履歴依存なので four_week と同じく status==ok の時のみ判定に使う
            if parsed.get("weekly_pct") is None:
                wk_cands = [r for d, r in by_date.items()
                            if d and 5 <= (target_dt - datetime.strptime(d, "%Y-%m-%d")).days <= 9
                            and r.get("value")]
                if wk_cands:
                    row["weekly_pct"] = round((parsed["value"] / wk_cands[-1]["value"] - 1) * 100, 2)
                    row["weekly_src"] = "self"  # サイト提供値と自前計算値を混同しないための出所印
            # 閾値は系列側で上書き可（既定は全系列共通）。電力のように平常時の変動が大きい
            # 系列に一律+5%を当てると常時発火して使い物にならないため（JEPX実測: 週次変化の
            # 平均絶対値13.9%・+5%だと41%の日で発火）。上書き値は series.alert に根拠つきで置く
            th = {**alert_cfg, **s.get("alert", {})}
            trigger = []
            # weekly はサイト側の値なら自前履歴と独立なので suspect_jump でも判定する（A-3）。
            # 自前算出（weekly_src=self）は履歴依存なので four_week と同じく ok の時のみ
            wk, wk_self = row.get("weekly_pct"), row.get("weekly_src") == "self"
            if wk is not None and wk >= th["weekly_pct"] and (not wk_self or row["status"] == "ok"):
                trigger.append(f"weekly {wk:+.1f}%" + ("(自前)" if wk_self else ""))
            # 4週累積は自前履歴に依存するため ok の時のみ
            if row["status"] == "ok" and four_w is not None and four_w >= th["four_week_pct"]:
                trigger.append(f"4週累積 {four_w:+.1f}%")
            row["four_week_pct"] = round(four_w, 2) if four_w is not None else None
            row["four_week_base_date"] = cands[-1].get("date") if cands else None
            rows.append(row)
            if trigger:
                alerts.append((s, row, trigger))
            wk = f"{parsed['weekly_pct']:+.1f}%" if parsed.get("weekly_pct") is not None else "-"
            print(f"[{row['status']}] {s['id']:<12} {parsed['value']:>10} weekly={wk}")
        except Exception as exc:  # noqa: BLE001  取得失敗は系列単位fail-soft
            rows.append({**base, "status": "error", "error": str(exc)[:100]})
            print(f"[error] {s['id']}: {str(exc)[:80]}")

    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    ok = sum(1 for r in rows if r["status"] == "ok")
    bad = [r for r in rows if r["status"] not in ("ok",)]
    print(f"\n[done] {ok}/{len(rows)} ok → {LEDGER_PATH}")
    if bad:
        print(f"⚠️ 要確認 {len(bad)} 件: " + ", ".join(f"{r['id']}({r['status']})" for r in bad))
    if alerts:
        print(f"\n🚨 閾値超え {len(alerts)} 系列:")
        for s, row, trigger in alerts:
            print(f"  {s['jp']}（{'/'.join(trigger)}）→ 受益: {beneficiaries_display(s, today)}")
        # 前向き記録（レビューC-1対応: 発火を将来検定できる形で残す）
        try:
            import price_watch_forward as fwd
            fwd.record_firings(alerts, today)
        except Exception as exc:  # noqa: BLE001  記録失敗で本処理を落とさない
            print(f"[forward] WARN: 前向き記録に失敗: {str(exc)[:100]}")
    else:
        print("閾値超えなし（4週累積は履歴4本蓄積後から判定）")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
