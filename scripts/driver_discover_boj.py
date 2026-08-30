#!/usr/bin/env python3
"""ドライバー発見器（日銀 CGPI/SPPI 全品目 ＋ TDnet 開示タイトル）。

無料の一次統計だけから「まだ監視していない素材・商品」を列挙し、オーナーが次に監視する
価格系列を選ぶための材料を `output/driver_discover.md` に出す（tasks/shortage_goods_expansion.md A-1..A-3）。

やること:
  1. 日銀の一括 zip（cgpi_m_jp.zip / sppi_m_jp.zip）の**全品目**を読み、最新値・前月比・前年同月比・
     3ヶ月比を出す。`configs/price_universe_sources.json` の type=boj_bulk で既に監視中の data_code は除外。
  2. `data/tdnet/index/<year>/*.json.gz` の直近 N 日のタイトルを〈価格改定|値上げ|増産|受注停止|出荷停止|
     供給|生産能力|設備投資〉で検索し、会社コードを `data/center_pin/center_pin.jsonl`（TOP1000）と照合。
  3. 上位品目の名前トークンを center_pin の pin/note/name に部分一致させて候補会社を付ける（最大5社／品目）。

やらないこと: 受益 tier の判定（帰属は別工程 beneficiary-attribution）・configs/*.json の編集。

取得・パースは scripts/monthly_sources.py の `_boj_bulk_rows` / `parse_boj_bulk` を再利用する
（fail-closed の検査＝レンジ外・欠測・stale はそちらの規約に従う）。

使い方:
    python3 scripts/driver_discover_boj.py [--days 90] [--top 20] [--no-tdnet]
    python3 scripts/driver_discover_boj.py --selftest   # ネットワーク不要の固定データで検査
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import gzip
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from monthly_sources import _boj_bulk_rows, _ym_shift, parse_boj_bulk  # noqa: E402

SOURCES_PATH = os.path.join(ROOT, "configs", "price_universe_sources.json")
CENTER_PIN_PATH = os.path.join(ROOT, "data", "center_pin", "center_pin.jsonl")
TDNET_INDEX_DIR = os.path.join(ROOT, "data", "tdnet", "index")
OUTPUT_PATH = os.path.join(ROOT, "output", "driver_discover.md")

DATASETS = ("cgpi", "sppi")
TDNET_PATTERN = re.compile(r"価格改定|値上げ|増産|受注停止|出荷停止|供給|生産能力|設備投資")
# 品目名から会社を引く時に捨てる語（総称・接尾語。残すと全社にマッチして意味が無い）
STOP_TOKENS = {"品目", "類別", "総平均", "その他", "製品", "うち", "除く", "含む", "国内", "輸入", "輸出",
               "指数", "サービス", "業務", "関連", "および", "及び", "もの", "部品", "用品", "機器", "装置",
               "材料", "加工", "一般", "工業", "産業", "小類別", "中類別", "大類別", "基本分類"}
TOKEN_MIN_LEN = 2
MAX_COMPANIES_PER_ITEM = 5
# 同じ品目が別の指数族（契約通貨ベース・消費税除き・参考系列・戦前基準）で重複収録されているため、
# 円ベース／基本分類だけを採る（上位20が同一品目の写しで埋まるのを防ぐ・2026-08-30 実測）
DUPLICATE_FAMILY = re.compile(r"契約通貨ベース|消費税を除く|戦前基準|〔参考系列〕|（参考）")
FAMILY_LABELS = (("輸出", "輸出"), ("輸入", "輸入"), ("知的財産", "知財"), ("国内", "国内"))


# ---------------------------------------------------------------- 日銀 全品目

def monitored_codes(sources_path: str = SOURCES_PATH) -> set:
    """既に監視中の日銀 data_code の集合（type=boj_bulk のみ）。

    Args:
        sources_path: price_universe_sources.json のパス。

    Returns:
        data_code の集合。ファイルが無ければ空集合。
    """
    if not os.path.exists(sources_path):
        return set()
    with open(sources_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    return {s["data_code"] for s in cfg.get("series", [])
            if s.get("type") == "boj_bulk" and s.get("data_code")}


def three_month_pct(header: List[str], row: List[str]) -> Optional[float]:
    """行から「最新月 vs 3ヶ月前」の変化率(%)を返す。どちらか欠測なら None。

    Args:
        header: 先頭行（"YYYYMM" の月ヘッダを含む）。
        row: 系列の行（コード, 統計名, 系列名, 値...）。
    """
    by_month: Dict[str, float] = {}
    for i, h in enumerate(header):
        h = h.strip()
        if not re.fullmatch(r"\d{6}", h) or i >= len(row):
            continue
        cell = row[i].strip()
        if not cell:
            continue
        try:
            by_month[h] = float(cell)
        except ValueError:
            continue
    if not by_month:
        return None
    latest = max(by_month)
    base = by_month.get(_ym_shift(latest, -3))
    if not base:
        return None
    return round((by_month[latest] / base - 1) * 100, 2)


def family_label(stat: str) -> str:
    """統計名から短い指数族ラベル（国内/輸出/輸入/知財/基本）を作る。"""
    for key, label in FAMILY_LABELS:
        if key in stat:
            return label
    return "基本"


def enumerate_boj_items(rows: List[List[str]], dataset: str, exclude: set,
                        today: Optional[str] = None) -> Dict[str, Any]:
    """一括CSVの全行を品目として集計する（重複指数族は dup として数え除外）。

    Args:
        rows: `_boj_bulk_rows` が返す行列（先頭行が月ヘッダ）。
        dataset: "cgpi" | "sppi"。
        exclude: 監視中の data_code（除外）。
        today: "YYYY-MM"（stale 判定に渡す。None なら判定しない）。

    Returns:
        {"total": 品目数（重複族を除いた後）, "dup": 重複族として除いた行数, "monitored": 監視中,
         "unmonitored": 未監視で値が取れた品目のリスト, "skipped": 値が取れなかった件数}
        各品目は {dataset, data_code, stat, family, name, value, monthly_pct, yoy_pct, m3_pct, src_date}。
    """
    if not rows:
        return {"total": 0, "unmonitored": [], "skipped": 0, "monitored": 0, "dup": 0}
    header = rows[0]
    items: List[Dict[str, Any]] = []
    total = skipped = monitored = dup = 0
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        code = row[0].strip()
        stat = row[1].strip() if len(row) > 1 else ""
        if code in exclude:
            total += 1
            monitored += 1
            continue
        if DUPLICATE_FAMILY.search(stat):
            dup += 1
            continue
        total += 1
        parsed = parse_boj_bulk([header, row], code, today)
        if parsed is None:
            skipped += 1
            continue
        items.append({
            "dataset": dataset,
            "data_code": code,
            "stat": stat,
            "family": family_label(stat),
            "name": row[2].strip() if len(row) > 2 else "",
            "value": parsed["value"],
            "monthly_pct": parsed["monthly_pct"],
            "yoy_pct": parsed["yoy_pct"],
            "m3_pct": three_month_pct(header, row),
            "src_date": parsed["src_date"],
        })
    return {"total": total, "unmonitored": items, "skipped": skipped, "monitored": monitored, "dup": dup}


def rank_items(items: List[Dict[str, Any]], top: int) -> Dict[str, List[Dict[str, Any]]]:
    """前年同月比で上位 top 件と下位 top//2 件（負のみ）に分ける。yoy_pct が None は対象外。"""
    with_yoy = [it for it in items if it["yoy_pct"] is not None]
    up = sorted(with_yoy, key=lambda x: x["yoy_pct"], reverse=True)[:top]
    down = sorted([it for it in with_yoy if it["yoy_pct"] < 0],
                  key=lambda x: x["yoy_pct"])[:max(1, top // 2)]
    return {"up": up, "down": down}


# ---------------------------------------------------------------- TDnet

def normalize_code(code: str) -> str:
    """TDnet の会社コード（5桁・末尾0）を center_pin の4桁に揃える。"""
    code = (code or "").strip()
    if len(code) == 5 and code.endswith("0"):
        return code[:-1]
    return code


def load_center_pin(path: str = CENTER_PIN_PATH) -> List[Dict[str, Any]]:
    """center_pin.jsonl を読む（壊れた行は飛ばす）。"""
    rows: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _index_files_since(index_dir: str, since: dt.date) -> List[str]:
    """`<year>/<YYYYMMDD>_<YYYYMMDD>.json.gz` のうち終了日が since 以降のファイル。"""
    out: List[str] = []
    for path in glob.glob(os.path.join(index_dir, "*", "*.json.gz")):
        m = re.search(r"(\d{8})_(\d{8})\.json\.gz$", path)
        if not m:
            continue
        end = dt.datetime.strptime(m.group(2), "%Y%m%d").date()
        if end >= since:
            out.append(path)
    return sorted(out)


def scan_tdnet_items(items: List[Dict[str, Any]], since: dt.date,
                     pin_by_code: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """TDnet index の items からキーワード該当を抜き、center_pin と照合する。

    Args:
        items: index ファイルの "items"（各要素は {"Tdnet": {...}}）。
        since: この日付（含む）以降の pubdate だけ採る。
        pin_by_code: 4桁コード → center_pin 行。

    Returns:
        {date, code, company, title, keyword, in_top1000, pin, url} のリスト。
    """
    hits: List[Dict[str, Any]] = []
    for wrapper in items:
        rec = wrapper.get("Tdnet") if isinstance(wrapper, dict) else None
        if not rec:
            continue
        title = rec.get("title") or ""
        m = TDNET_PATTERN.search(title)
        if not m:
            continue
        pub = (rec.get("pubdate") or "")[:10]
        try:
            if dt.datetime.strptime(pub, "%Y-%m-%d").date() < since:
                continue
        except ValueError:
            continue
        code = normalize_code(rec.get("company_code") or "")
        pin = pin_by_code.get(code)
        hits.append({
            "date": pub,
            "code": code,
            "company": rec.get("company_name") or "",
            "title": title,
            "keyword": m.group(0),
            "in_top1000": pin is not None,
            "pin": pin.get("pin", "") if pin else "",
            "url": rec.get("document_url") or "",
        })
    return hits


def scan_tdnet(days: int, pin_by_code: Dict[str, Dict[str, Any]],
               index_dir: str = TDNET_INDEX_DIR,
               today: Optional[dt.date] = None) -> List[Dict[str, Any]]:
    """直近 days 日の index ファイルを走査する（ネットワーク不要・ローカルの鏡のみ）。"""
    today = today or dt.date.today()
    since = today - dt.timedelta(days=days)
    hits: List[Dict[str, Any]] = []
    seen: set = set()
    for path in _index_files_since(index_dir, since):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        for h in scan_tdnet_items(data.get("items", []), since, pin_by_code):
            key = (h["date"], h["code"], h["title"])
            if key in seen:
                continue
            seen.add(key)
            hits.append(h)
    hits.sort(key=lambda x: (x["date"], x["code"]), reverse=True)
    return hits


# ---------------------------------------------------------------- 候補会社

def item_tokens(name: str) -> List[str]:
    """品目名を 2文字以上のトークンに分ける（透明な部分一致のため）。

    "品目/___酪農品（除バター）" → ["酪農品", "バター"] のように、区切り記号・括弧・停止語を落とす。
    """
    body = name.split("/")[-1]
    parts = re.split(r"[／/、,，・（）()「」\s_＿\-－〜~:：；;]+", body)
    toks: List[str] = []
    for p in parts:
        p = p.strip()
        for stop in ("除", "うち"):
            if p.startswith(stop) and len(p) > len(stop):
                p = p[len(stop):]
        # 漢字の連なり／カタカナの連なり／英数の連なりを別トークンに分ける（"モス型メモリ集積回路" →
        # モス・メモリ・集積回路）。"型" は接尾語なので落とす
        for sub in re.findall(r"[\u4e00-\u9fff々]+|[\u30a0-\u30ffー]+|[A-Za-z0-9Ａ-Ｚａ-ｚ０-９]+", p):
            sub = sub.rstrip("型")
            if len(sub) >= TOKEN_MIN_LEN and sub not in STOP_TOKENS and sub not in toks:
                toks.append(sub)
    return toks


def match_companies(name: str, pins: List[Dict[str, Any]],
                    limit: int = MAX_COMPANIES_PER_ITEM) -> List[Dict[str, str]]:
    """品目名トークンを center_pin の pin/note/name に部分一致させる。

    Returns:
        [{code, name, token, field}] 最大 limit 件（一致トークン数の多い順）。
    """
    toks = item_tokens(name)
    if not toks:
        return []
    scored: List[tuple] = []
    for p in pins:
        hit_tok = hit_field = None
        n_hit = 0
        for field in ("pin", "note", "name"):
            text = p.get(field) or ""
            for t in toks:
                if t in text:
                    n_hit += 1
                    if hit_tok is None:
                        hit_tok, hit_field = t, field
        if n_hit:
            scored.append((-n_hit, p.get("code", ""), hit_tok, hit_field, p.get("name", "")))
    scored.sort()
    return [{"code": c, "name": nm, "token": t, "field": f}
            for _, c, t, f, nm in scored[:limit]]


# ---------------------------------------------------------------- 出力

def _fmt(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:+.1f}"


def render_markdown(run_date: str, cmd: str, summary: Dict[str, Any],
                    ranked: Dict[str, List[Dict[str, Any]]],
                    tdnet_hits: Optional[List[Dict[str, Any]]],
                    candidates: List[Dict[str, Any]], days: int) -> str:
    """3表の Markdown を組み立てる。"""
    lines: List[str] = [
        "# ドライバー発見器（日銀 CGPI/SPPI 全品目 ＋ TDnet 開示）",
        "",
        f"- 実行日: {run_date}",
        f"- 再現コマンド: `{cmd}`",
        f"- 品目数 {summary['total']}／監視中 {summary['monitored']}／未監視 {summary['unmonitored']}"
        f"（値が取れず除外 {summary['skipped']}・重複指数族〈契約通貨/消費税除き/参考〉として除外 {summary['dup']}）"
        f"／上位 {len(ranked['up'])} 件",
        "- 出所: 日銀一括ダウンロード cgpi_m_jp.zip / sppi_m_jp.zip（2020年=100・月次）、"
        "TDnet index（`data/tdnet/index/`）、`data/center_pin/center_pin.jsonl`",
        "- ⚠️ 候補会社はキーワード部分一致のみ＝受益 tier ではない（帰属は beneficiary-attribution で別途）",
        "",
        "## 1. 未監視 CGPI/SPPI 品目 前年同月比 上位",
        "",
        "| # | 系列 | 族 | 品目 | data_code | 最新値 | 前月比% | 3ヶ月比% | 前年比% | 出所月 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, it in enumerate(ranked["up"], 1):
        lines.append(f"| {i} | {it['dataset'].upper()} | {it['family']} | {it['name']} | `{it['data_code']}` | {it['value']:.1f} | "
                     f"{_fmt(it['monthly_pct'])} | {_fmt(it['m3_pct'])} | {_fmt(it['yoy_pct'])} | {it['src_date']} |")
    lines += ["", "### 1b. 前年同月比 下位（下落）", "",
              "| # | 系列 | 族 | 品目 | data_code | 最新値 | 前月比% | 3ヶ月比% | 前年比% | 出所月 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for i, it in enumerate(ranked["down"], 1):
        lines.append(f"| {i} | {it['dataset'].upper()} | {it['family']} | {it['name']} | `{it['data_code']}` | {it['value']:.1f} | "
                     f"{_fmt(it['monthly_pct'])} | {_fmt(it['m3_pct'])} | {_fmt(it['yoy_pct'])} | {it['src_date']} |")

    lines += ["", f"## 2. TDnet 開示イベント（直近 {days} 日・価格改定|値上げ|増産|受注停止|出荷停止|供給|生産能力|設備投資）", ""]
    if tdnet_hits is None:
        lines.append("（--no-tdnet で省略）")
    else:
        n_in = sum(1 for h in tdnet_hits if h["in_top1000"])
        lines += [f"- 該当 {len(tdnet_hits)} 件（TOP1000 内 {n_in}）", "",
                  "| 開示日 | コード | 会社 | 語 | TOP1000 | センターピン | タイトル |",
                  "|---|---|---|---|---|---|---|"]
        for h in tdnet_hits:
            title = h["title"].replace("|", "｜")
            link = f"[{title}]({h['url']})" if h["url"] else title
            lines.append(f"| {h['date']} | {h['code']} | {h['company']} | {h['keyword']} | "
                         f"{'in' if h['in_top1000'] else 'out'} | {h['pin']} | {link} |")

    lines += ["", "## 3. 上位品目の候補会社（center_pin pin/note/name への部分一致・最大5社）", "",
              "| 品目 | 族 | 前年比% | 出所月 | 照合トークン | 候補会社（コード 社名 ←一致語@欄） |",
              "|---|---|---|---|---|---|"]
    for c in candidates:
        comps = "、".join(f"{m['code']} {m['name']} ←{m['token']}@{m['field']}" for m in c["matches"]) or "（一致なし）"
        lines.append(f"| {c['name']} | {c['family']} | {_fmt(c['yoy_pct'])} | {c['src_date']} | "
                     f"{' '.join(item_tokens(c['name']))} | {comps} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def run(days: int, top: int, no_tdnet: bool, out_path: str = OUTPUT_PATH) -> int:
    """本番実行。日銀 zip の取得失敗は例外のまま止める（偽データを出さない）。"""
    today = dt.date.today()
    today_ym = today.strftime("%Y-%m")
    exclude = monitored_codes()
    all_items: List[Dict[str, Any]] = []
    summary = {"total": 0, "monitored": 0, "unmonitored": 0, "skipped": 0, "dup": 0}
    for ds in DATASETS:
        rows = _boj_bulk_rows(ds)   # 失敗は requests/zipfile の例外がそのまま上がる
        res = enumerate_boj_items(rows, ds, exclude, today_ym)
        summary["total"] += res["total"]
        summary["monitored"] += res["monitored"]
        summary["skipped"] += res["skipped"]
        summary["dup"] += res["dup"]
        summary["unmonitored"] += len(res["unmonitored"])
        all_items.extend(res["unmonitored"])
        print(f"[boj] {ds}: 品目数 {res['total']}／監視中 {res['monitored']}／"
              f"未監視 {len(res['unmonitored'])}／除外 {res['skipped']}／重複族 {res['dup']}")
    ranked = rank_items(all_items, top)
    print(f"品目数 {summary['total']}／未監視 {summary['unmonitored']}／上位 {len(ranked['up'])} 件")

    pins = load_center_pin()
    pin_by_code = {p.get("code"): p for p in pins}
    tdnet_hits = None
    if not no_tdnet:
        tdnet_hits = scan_tdnet(days, pin_by_code)
        n_in = sum(1 for h in tdnet_hits if h["in_top1000"])
        print(f"[tdnet] 直近{days}日 該当 {len(tdnet_hits)} 件（TOP1000 内 {n_in}）")

    candidates = []
    for it in ranked["up"]:
        candidates.append({"name": it["name"], "family": it["family"], "yoy_pct": it["yoy_pct"],
                           "src_date": it["src_date"],
                           "matches": match_companies(it["name"], pins)})
    n_cand = sum(1 for c in candidates if c["matches"])
    print(f"[cand] 上位 {len(candidates)} 品目のうち候補会社あり {n_cand}")

    cmd = f"python3 scripts/driver_discover_boj.py --days {days} --top {top}" + (" --no-tdnet" if no_tdnet else "")
    md = render_markdown(today.isoformat(), cmd, summary, ranked, tdnet_hits, candidates, days)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(md)
    print(f"書き出し: {out_path}")
    return 0


def _selftest() -> int:
    """ネットワーク不要の固定データで各パーサを検査する。"""
    fails: List[str] = []

    def chk(name: str, cond: bool) -> None:
        print(("  ok " if cond else "  NG ") + name)
        if not cond:
            fails.append(name)

    hdr = ["", "", "", "202503", "202504", "202505", "202506", "202603", "202604", "202605", "202606"]
    rows = [
        hdr,
        ["C_A", "統計", "品目/___酪農品（除バター）", "100", "100", "100", "100", "105", "108", "110", "112"],
        ["C_B", "統計", "品目/___鋼板", "100", "100", "100", "100", "99", "98", "96", "95"],
        ["C_MON", "統計", "品目/___受託開発ソフトウェア", "100", "100", "100", "100", "101", "101", "101", "101"],
        ["C_EMPTY", "統計", "品目/___空", "", "", "", "", "", "", "", ""],
        ["C_DUP", "統計/契約通貨ベース", "品目/___酪農品（除バター）", "100", "100", "100", "100", "105", "108", "110", "112"],
    ]
    res = enumerate_boj_items(rows, "cgpi", {"C_MON"}, "2026-08")
    chk("品目数=4・監視中=1・未監視=2・除外=1・重複族=1",
        res["total"] == 4 and res["monitored"] == 1 and len(res["unmonitored"]) == 2
        and res["skipped"] == 1 and res["dup"] == 1)
    chk("指数族ラベル", family_label("企業物価指数 2020年基準/輸入物価指数/円ベース") == "輸入"
        and family_label("企業向けサービス価格指数 2020年基準") == "基本")
    a = next(it for it in res["unmonitored"] if it["data_code"] == "C_A")
    chk("最新値/前月比/前年比/出所月", a["value"] == 112 and a["monthly_pct"] == 1.82
        and a["yoy_pct"] == 12.0 and a["src_date"] == "2026-06")
    chk("3ヶ月比（202603=105 → 202606=112）", a["m3_pct"] == 6.67)
    chk("3ヶ月前が欠測なら None", three_month_pct(["", "202605", "202606"], ["C", "1", "2"]) is None)
    rk = rank_items(res["unmonitored"], 20)
    chk("上位は前年比降順・下位は負のみ", rk["up"][0]["data_code"] == "C_A"
        and [d["data_code"] for d in rk["down"]] == ["C_B"])

    chk("TDnet コード 5桁→4桁", normalize_code("48200") == "4820" and normalize_code("482A0") == "482A"
        and normalize_code("1301") == "1301")
    pins = [{"code": "1301", "name": "極洋", "pin": "原料魚価（マグロ）", "note": "魚価上昇は仕入原価"},
            {"code": "2264", "name": "森永乳業", "pin": "生乳・バター相場", "note": "酪農品の仕入"},
            {"code": "5401", "name": "日本製鉄", "pin": "鋼板スプレッド", "note": "鋼材市況"}]
    pin_by = {p["code"]: p for p in pins}
    items = [
        {"Tdnet": {"pubdate": "2026-08-20 15:00:00", "company_code": "13010", "company_name": "極洋",
                   "title": "製品価格改定のお知らせ", "document_url": "https://x/1.pdf"}},
        {"Tdnet": {"pubdate": "2026-08-21 15:00:00", "company_code": "99990", "company_name": "無名",
                   "title": "出荷停止に関するお知らせ", "document_url": ""}},
        {"Tdnet": {"pubdate": "2026-08-22 15:00:00", "company_code": "13010", "company_name": "極洋",
                   "title": "配当予想の修正", "document_url": ""}},
        {"Tdnet": {"pubdate": "2026-01-01 15:00:00", "company_code": "13010", "company_name": "極洋",
                   "title": "値上げのお知らせ", "document_url": ""}},
    ]
    hits = scan_tdnet_items(items, dt.date(2026, 6, 1), pin_by)
    chk("TDnet 語一致2件・期間外は落とす", len(hits) == 2)
    chk("TDnet in/out と pin", hits[0]["in_top1000"] and hits[0]["pin"] == "原料魚価（マグロ）"
        and hits[0]["keyword"] == "価格改定" and not hits[1]["in_top1000"] and hits[1]["code"] == "9999")

    chk("品目トークン（区切り・括弧・除）", item_tokens("品目/___酪農品（除バター）") == ["酪農品", "バター"])
    chk("停止語だけなら空", item_tokens("品目/___その他") == [])
    chk("漢字/カタカナ境界で分割・型を落とす",
        item_tokens("品目/____モス型メモリ集積回路") == ["モス", "メモリ", "集積回路"])
    m = match_companies("品目/___酪農品（除バター）", pins)
    chk("候補会社（森永のみ・一致語を明示）", [x["code"] for x in m] == ["2264"] and m[0]["token"] in ("酪農品", "バター"))
    chk("候補は最大5社", len(match_companies("品目/___魚価", pins * 10, 5)) == 5)

    md = render_markdown("2026-08-30", "cmd", {"total": 4, "monitored": 1, "unmonitored": 2, "skipped": 1, "dup": 1},
                         rk, hits, [{"name": "酪農品", "family": "国内", "yoy_pct": 12.0,
                                     "src_date": "2026-06", "matches": m}], 90)
    chk("Markdown に3表と再現コマンド", "## 1." in md and "## 2." in md and "## 3." in md and "`cmd`" in md)

    print(f"selftest: {len(fails)} NG")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=90, help="TDnet の遡り日数（既定90）")
    ap.add_argument("--top", type=int, default=20, help="前年比上位の件数（既定20・下位はその半分）")
    ap.add_argument("--no-tdnet", action="store_true", help="TDnet 走査を省略")
    ap.add_argument("--selftest", action="store_true", help="ネットワーク不要の自己検査")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    return run(args.days, args.top, args.no_tdnet)


if __name__ == "__main__":
    sys.exit(main())
