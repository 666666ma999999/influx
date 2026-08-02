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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
CONFIG_PATH = APP / "configs/price_universe_sources.json"
LEDGER_PATH = APP / "data/price_watch/universe_weekly.jsonl"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TAG_RE = re.compile(r"<[^>]+>")
NUM_RE = re.compile(r"-?[0-9][0-9,]*\.?[0-9]*")
JST = timezone(timedelta(hours=9))  # 台帳の日付は日本時間基準（監視対象が日本株のため）


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


def parse_uss(html: str) -> dict | None:
    """USS公式IRの月次データ表（HTML直）から最新完全月の成約車両単価（千円/台）を採る。

    表は1行=1ヶ月・14セル固定（月／開催回数 当期・前期／出品 実績・前年・前年比／
    成約 実績・前年・前年比／成約率 実績・前年／単価 実績・前年・前年比）を実測（2026-08-02）。
    未発表月は空セル14個の行として存在する。
    罠: 月名ヘッダ行が全角数字「５月」で、正規表現 \\d は全角にもマッチするため
    月名は ASCII 数字に限定し、さらに単価実績セルが数値の行だけを完全月として扱う。
    前月比は開催構成（GW・決算期）の季節性で±7%動くため判定に使わず（config 側で無効化）、
    判定は表が直接持つ前年同月比（yoy_pct）を使う。会計年度は「YYYY年3月期」表記から
    4〜12月=前年・1〜3月=当年に展開して src_date（YYYY-MM）にする。
    """
    fy = re.search(r"(\d{4})年.?3月期", html)
    if not fy:
        return None
    fy_end = int(fy.group(1))
    rows: dict[str, dict] = {}  # ym → {tanka, yoy_raw}（重複年月の衝突検知のため辞書）
    for tr in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [" ".join(strip_tags(c).split()) for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr.group(1), re.S)]
        if len(cells) != 14 or not re.match(r"^[0-9]{1,2}月$", cells[0]):
            continue
        nums = []
        for c in cells[1:]:
            m = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)", c.replace("*", ""))
            nums.append(float(m.group(1).replace(",", "")) if m else None)
        # nums: [開催当期,開催前期,出品実績,出品前年,出品比,成約実績,成約前年,成約比,
        #        成約率実績,成約率前年,単価実績,単価前年,単価前年比]
        if nums[10] is not None:
            month = int(cells[0].rstrip("月"))
            year = fy_end - 1 if month >= 4 else fy_end
            ym = f"{year}-{month:02d}"
            # 同一年月が異なる値で2回現れる＝ページに過去年度表が併載された等の構造変化
            # （会計年度regexは先頭1件しか見ないため過去表の月に誤った年を当ててしまう）。
            # 静かに誤値を採らず parse_fail に倒す（Codex S-3・安全側）
            if ym in rows and rows[ym]["tanka"] != nums[10]:
                return None
            rows[ym] = {"tanka": nums[10], "yoy_raw": nums[12]}
    if not rows:
        return None
    cur_ym = max(rows)  # DOM順でなく年月の最大で「最新」を決める（表の並び順に依存しない）
    cur = rows[cur_ym]
    y, mth = map(int, cur_ym.split("-"))
    prev_ym = f"{y - 1}-12" if mth == 1 else f"{y}-{mth - 1:02d}"
    mom = (cur["tanka"] / rows[prev_ym]["tanka"] - 1) * 100 if prev_ym in rows else None
    yoy = cur["yoy_raw"] - 100 if cur["yoy_raw"] is not None else None
    return {"value": cur["tanka"], "day_pct": None, "weekly_pct": None,
            "monthly_pct": round(mom, 2) if mom is not None else None,
            "yoy_pct": round(yoy, 2) if yoy is not None else None,
            "src_date": cur_ym, "layout": "uss_monthly_v1"}


def parse_yuyutei(html: str) -> dict | None:
    """遊々亭のポケカSAR販売検索（静的HTML）から販売価格の中央値（円）を採る。

    card-product ブロック1件につき <strong>N,NNN 円</strong> が1つ（2026-08-02 実測 261/261件・
    1ページ完結）。個票でなく中央値を系列にするのは、新弾追加・売り切れで母集団が動いても
    代表値が壊れないようにするため。件数はレイアウト変更・検索仕様変更の検知用に n_items で
    毎回記録し、50件未満は市場縮小とパース破損を区別できないため値を返さない（parse_fail）。
    """
    prices = []
    for block in html.split("card-product")[1:]:
        # 商品詳細への href が無いブロックは、CSS/JS等が偶然 card-product を含んだだけの
        # 可能性があるため数えない（Codex S-5・非商品ブロックの価格混入防止）
        if "/sell/poc/card/" not in block:
            continue
        m = re.search(r"<strong[^>]*>\s*([0-9][0-9,]*)\s*円", block)
        if m:
            prices.append(int(m.group(1).replace(",", "")))
    if len(prices) < 50:
        return None
    prices.sort()
    n = len(prices)
    med = float(prices[n // 2]) if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2
    return {"value": med, "day_pct": None, "weekly_pct": None, "monthly_pct": None,
            "n_items": n, "src_date": "", "layout": "yuyutei_sar_v1"}


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


def parse_spread(index_html: str, s: dict) -> dict | None:
    """石化スプレッド（製品 − ナフサ）を TE 一覧ページの2脚から合成する（2026-07-28 新設）。

    分子はDCE先物のCNY建て（polyethylene/polypropylene/styrene）、分母はナフサのUSD/T。
    通貨違いは ECB 参照レート（frankfurter.dev・無料/キー不要）で解決する。
        value[USD/T] = 分子[CNY/T] / USDCNY / vat_divisor - 分母[USD/T] * coef

    **%判定は禁止**（実測で確認した事故）: スプレッドは負値を取りうるため、PVCの
    週次 -7.23 USD/T の「縮小」が%表示では +9.6% になり買い側で誤発火する。
    そこで weekly_pct は None を返し、代わりに weekly_abs（USD/Tの実変化）で判定する。
    各脚の週次%から1週前の値を解析的に復元するので、自前履歴ゼロの初回から判定が効く。

    coef=1.0 は業界慣行の1:1差引き（ナフサ1tからエチレンは約3割だが、残りの併産品を
    ナフサ等価で評価する前提。質量収支の3.0を入れると併産品クレジット無しの無意味な値になる）。
    vat_divisor は中国国内価格の増値税13%を落とす任意パラメータ（既定1.0）。
    一覧ページ限定（個別ページは weekly 列が無く週次変化が消える）。
    """
    num = parse_te(index_html, s["num"]["slug"], s["num"]["label"])
    den = parse_te(index_html, s["den"]["slug"], s["den"]["label"])
    if not num or not den or num.get("value") is None or den.get("value") is None:
        return None
    try:
        fx = requests.get("https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY",
                          headers={"User-Agent": UA}, timeout=30)
        fx.raise_for_status()
        usdcny = float(fx.json()["rates"]["CNY"])
    except Exception:  # noqa: BLE001  FXが取れなければ通貨換算できない＝値を捏造しない
        return None
    coef, vat = float(s.get("coef", 1.0)), float(s.get("vat_divisor", 1.0))

    def spread(n_val: float, d_val: float) -> float:
        return n_val / usdcny / vat - d_val * coef

    value = spread(num["value"], den["value"])
    # 1週前の各脚を週次%から解析的に復元（片脚でも週次%が無ければ週次判定はしない）
    weekly_abs = None
    if num.get("weekly_pct") is not None and den.get("weekly_pct") is not None:
        n_prev = num["value"] / (1 + num["weekly_pct"] / 100)
        d_prev = den["value"] / (1 + den["weekly_pct"] / 100)
        weekly_abs = round(value - spread(n_prev, d_prev), 2)
    # 脚の出所日は最大1日ズレる。src_date は古い方に合わせる（新しく見せない）
    dates = [x.get("src_date") or "" for x in (num, den)]
    return {"value": round(value, 2), "day_pct": None, "weekly_pct": None,
            "monthly_pct": None, "src_date": min(d for d in dates) if all(dates) else "",
            "layout": "spread", "weekly_abs": weekly_abs, "usdcny": round(usdcny, 4),
            "legs": f"{s['num']['slug']}:{num.get('src_date')} / {s['den']['slug']}:{den.get('src_date')}"}


FMBI_URL = "https://www.furuyametals.co.jp/products/cms-data/datedata.json"
FMBI_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# メタル→JSONキー。**キー名でアンカーする**（位置・添字で採らない）。
# キー改名時は別メタルへ静かにズレるより parse_fail を選ぶ。
FMBI_KEYS = {
    "iridium": {"jpy": "price_yen_iridium", "usd": "price_iridium"},
    "ruthenium": {"jpy": "price_yen_ruthenium", "usd": "price_ruthenium"},
}


def _fmbi_num(v) -> float | None:
    """数値セルを厳密に float 化（想定外の型・書式は None にして黙って採らない）。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = re.sub(r"[¥￥$,\s]", "", v)
        if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", s):
            return float(s)
    return None


def parse_fmbi(payload: list, metal: str, currency: str = "JPY") -> dict | None:
    """フルヤ金属の自社公表価格 FMBI（イリジウム/ルテニウム）を採る（2026-07-28 新設）。

    出所は一覧表ページ(/products/fmbi-table/)が読む JSON = FMBI_URL。
    2022-01-05 以降の全公表日を持つ日次配列（実測1080件）で、1レコード=
    {date, time, price_yen_iridium, price_yen_ruthenium, price_iridium, price_ruthenium}
    （円建て=円/g・ドル建て=$/toz・time は全件 "10:30" 東京）。

    **HTML を読んではいけない**（2026-07-28 実測・田中貴金属A-2と同種の事故）: ページの
    価格ブロックはサーバ側の静的プレースホルダで「2026/03/02」のまま5ヶ月固まっており、
    実表示は上記 JSON を取得した JS が実行時に上書きしている。HTML から採ると
    **公表日フィールドごと古いため stale 判定もすり抜けて** 静かに旧価格を記録する。
    JSON が取れなければ HTML へフォールバックせず parse_fail にすること。

    設計（parse_tanaka / parse_te の教訓の踏襲）:
    - メタルは FMBI_KEYS のキー名でアンカー（添字・位置で採らない）
    - 数値は _fmbi_num で厳密変換（桁数を仮定しない）
    - **JSON は日付順に並んでいない**（実測: 生の先頭が最新日）ので必ず日付ソートしてから最終行
    - day/weekly/monthly は「暦日でN日前**以前**の直近公表日」と比較（添字で-1/-5/-20 と
      数えると祝日・年末年始で基準日が静かにズレる）
    - 円建ては FX を含む。金属自体の騰勢かは value_alt（USD建て）で確認する
    """
    keys = FMBI_KEYS.get(metal)
    if keys is None or not isinstance(payload, list) or not payload:
        return None
    key = keys["jpy"] if currency == "JPY" else keys["usd"]

    rows: list[tuple[str, float, dict]] = []
    for r in payload:
        if not isinstance(r, dict):
            continue
        d = r.get("date")
        if not isinstance(d, str) or not FMBI_DATE_RE.match(d):
            continue
        v = _fmbi_num(r.get(key))
        if v is None:
            continue
        rows.append((d, v, r))
    if not rows:
        return None  # キー改名・全欠損は別メタルを採らず parse_fail にする
    rows.sort(key=lambda x: x[0])

    src_date, value, latest = rows[-1]
    latest_dt = datetime.strptime(src_date, "%Y-%m-%d")

    def pct_back(days: int) -> float | None:
        target = latest_dt - timedelta(days=days)
        prior = [v for d, v, _ in rows if datetime.strptime(d, "%Y-%m-%d") <= target]
        if not prior or not prior[-1]:
            return None
        return round((value / prior[-1] - 1) * 100, 2)

    other = keys["usd"] if currency == "JPY" else keys["jpy"]
    return {"value": value, "day_pct": pct_back(1), "weekly_pct": pct_back(7),
            "monthly_pct": pct_back(30), "src_date": src_date, "layout": "fmbi_json",
            "unit": "JPY/g" if currency == "JPY" else "USD/toz", "metal": metal,
            "value_alt": _fmbi_num(latest.get(other)), "src_time": latest.get("time")}


def parse_boj(page: str, data_code: str) -> dict | None:
    """日銀「主要時系列統計データ表」から月次系列を1本取る（月次レーン用・2026-07-28）。

    https://www.stat-search.boj.or.jp/ssi/mtshtml/<page>.html（Shift-JIS）。
    列は**データコードでアンカーする**（列位置や系列名は表記変更で動くが、データコードは
    統計の恒久IDなので最も堅い。位置固定禁止の規約に沿う）。
    value=最新月の指数、monthly_pct=前月比%、src_date="YYYY-MM"、weekly_pct=None（月次のため）。
    """
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S)

    def cells(r: str) -> list[str]:
        return [re.sub(r"\s+", " ", strip_tags(c)).strip()
                for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]

    col = None
    for r in rows[:10]:
        c = cells(r)
        if c and c[0].startswith("データコード"):
            for i, v in enumerate(c):
                if v == data_code:
                    col = i
                    break
            break
    if col is None:
        return None  # コードが見つからない＝列を推測しない（黙って別系列を採らない）

    series: list[tuple[str, float]] = []
    for r in rows:
        c = cells(r)
        if not c or not re.match(r"^\d{4}/\d{2}$", c[0]) or len(c) <= col:
            continue
        try:
            series.append((c[0], float(c[col].replace(",", ""))))
        except ValueError:
            continue  # 空欄・「-」は欠測として飛ばす
    if len(series) < 2:
        return None
    (m1, v1), (m0, v0) = series[-1], series[-2]
    return {"value": v1, "day_pct": None, "weekly_pct": None,
            "monthly_pct": round((v1 / v0 - 1) * 100, 2) if v0 else None,
            "src_date": m1.replace("/", "-"), "layout": "boj_mts"}


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
    # 日付は **JST基準**（run_at はUTCのまま＝実行時刻の絶対記録）。
    # UTC日付だと launchd の月曜08:30 JST 実行が UTC では日曜になり、同じ日の手動再実行
    # （昼＝UTCでも月曜）が別日として記録される。前向き記録の同日重複排除もすり抜ける
    # （2026-07-28 Codex CONFIRMED・実害を日付計算で確認済み）。
    # 監視対象は日本株なので日本の営業日で数えるのが本来の定義でもある。
    today = datetime.now(JST).strftime("%Y-%m-%d")
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # TE は一覧ページ1回で全系列の行を取得（リクエスト削減・自ページに行が無い系列対策）。
    # 一覧に無い系列のみ個別ページへフォールバック
    try:
        te_index_html = fetch("https://tradingeconomics.com/commodities")
    except Exception as exc:  # noqa: BLE001
        # 一覧が取れないと全系列が列構成の違う個別ページへ落ちるため即停止（A-1）
        print(f"[FATAL] TE一覧ページ取得失敗のため中断（誤列での記録を防ぐ）: {str(exc)[:100]}")
        return 1

    fmbi_payload: list | None = None  # Ir/Ru は同一エンドポイント＝1回だけ取って使い回す

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
            elif s["type"] == "spread":
                parsed = parse_spread(te_index_html, s)
            elif s["type"] == "fmbi":
                if fmbi_payload is None:
                    fmbi_payload = json.loads(fetch(FMBI_URL))
                parsed = parse_fmbi(fmbi_payload, s["metal"], s.get("currency", "JPY"))
            elif s["type"] == "boj":
                resp = requests.get(f"https://www.stat-search.boj.or.jp/ssi/mtshtml/{s['page']}.html",
                                    headers={"User-Agent": UA}, timeout=60)
                resp.raise_for_status()
                parsed = parse_boj(resp.content.decode("shift_jis", errors="replace"), s["data_code"])
            elif s["type"] == "tokyosteel":
                # 国内実勢（東京製鐵の自社公表建値）。PDFを解析するため別モジュールに分離。
                # 標準ライブラリのみで動くので requests 依存とは独立
                import tokyosteel_scrap
                parsed = tokyosteel_scrap.fetch_tokyosteel_scrap(today)
            elif s["type"] == "tanaka":
                parsed = parse_tanaka(fetch("https://gold.tanaka.co.jp/commodity/souba/"))
            elif s["type"] == "uss":
                parsed = parse_uss(fetch("https://www.ussnet.co.jp/ir/library/monthly/index.html"))
            elif s["type"] == "yuyutei":
                parsed = parse_yuyutei(
                    fetch("https://yuyu-tei.jp/sell/poc/s/search?search_word=&rare=SAR"))
            elif s["type"] in ("jmtba", "seaj", "jama", "miki", "tamago", "rice", "jnto"):
                # 波2の月次データ源（30カテゴリ拡張・2026-08-02）。取得実装と fixtures は
                # monthly_sources.py に分離。type名 → fetch_<type>() の明示対応（allowlist方式）
                import monthly_sources
                parsed = getattr(monthly_sources, "fetch_" + s["type"])()
            else:
                # 未知の type を既存パーサへ流すと別サイトの値を静かに記録する。
                # 型を増やしたら分岐も足す（fail-fast・Codex軽微指摘）
                raise ValueError(f"未知の series type: {s['type']}")
            if parsed is None or parsed["value"] is None:
                rows.append({**base, "status": "parse_fail"})
                print(f"[parse_fail] {s['id']}")
                continue
            prev = [r for r in history.get(s["id"], []) if r.get("status") == "ok"]
            # 台帳の**追記順**でなく日付順で比較する。種まき（seed-official）は過去日付を
            # 後から追記するため、追記順のままだと prev[-1] が古い種になり、50%超の差で
            # 現在値が suspect_jump → 以後 ok 行が増えず永久 suspect のループに入る（Codex S-11）
            prev.sort(key=lambda r: (r.get("date", ""), r.get("run_at", "")))
            # 比率ベースの跳び検知はゼロ近傍を跨ぐスプレッドでは常時発動するため絶対値で見る
            if s["type"] == "spread":
                suspect = bool(prev and prev[-1].get("value") is not None and
                               abs(parsed["value"] - prev[-1]["value"]) > 200)
            else:
                suspect = bool(prev and prev[-1].get("value") and
                               abs(parsed["value"] / prev[-1]["value"] - 1) > 0.5)
            status = "suspect_jump" if suspect else "ok"
            # 公表日が当日でない系列は stale（前日値を当日として記録する事故の検知・A-2）
            if parsed.get("layout") == "tanaka" and parsed.get("src_date") and \
                    parsed["src_date"] != today:  # 田中貴金属は09:30JST公表・当日一致が正
                status = "stale"
            # JEPXは日次公表（翌日受渡分まで出る）。3日以上古い＝公開停止/取得ズレの検知
            if parsed.get("layout") == "jepx_csv" and parsed.get("src_date") and \
                    (datetime.strptime(today, "%Y-%m-%d")
                     - datetime.strptime(parsed["src_date"], "%Y-%m-%d")).days > 3:
                status = "stale"
            # FMBIは営業日日次10:30JST公表。通常3日・三連休4日空き、年末年始/GWは最大11日
            # （実測）なので閾値は5日。年2回程度の誤 stale は許容する
            if parsed.get("layout") == "fmbi_json" and parsed.get("src_date") and \
                    (datetime.strptime(today, "%Y-%m-%d")
                     - datetime.strptime(parsed["src_date"], "%Y-%m-%d")).days > 5:
                status = "stale"
            # 東京製鐵は改定のたびの公表（2026年は平均7.8日間隔・最長27日を実測）。
            # 日付一致は要求せず45日以上の据え置きを異常とみなす
            if parsed.get("layout") == "tokyosteel_pdf_v1" and parsed.get("src_date") and \
                    (datetime.strptime(today, "%Y-%m-%d")
                     - datetime.strptime(parsed["src_date"], "%Y-%m-%d")).days > 45:
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
            four_w_abs = None
            if cands:
                # スプレッドは負値を取りうるので比率にすると符号が壊れる（両方負の区間で
                # 比率が+方向に出て誤発火する・レビュー指摘）。絶対差(USD/T)で持つ
                if s["type"] == "spread":
                    four_w_abs = round(parsed["value"] - cands[-1]["value"], 2)
                else:
                    four_w = (parsed["value"] / cands[-1]["value"] - 1) * 100
                # 組成ジャンプガード（Codex C-1・遊々亭SAR）: 中央値は「同一カード集合の価格」
                # ではなく「その時点の検索結果の中央値」なので、比較基準と件数が2%超違う時は
                # 新弾追加・売り切れ等の母集団変化と価格変動を区別できない。判定から外す
                # （値と n_items の記録は続ける＝台帳で組成変化そのものは追える）
                if parsed.get("layout") == "yuyutei_sar_v1" and four_w is not None:
                    bn = cands[-1].get("n_items")
                    if not bn or abs(parsed["n_items"] - bn) / bn > 0.02:
                        four_w = None
                        row["four_week_note"] = "mix_change"
            # サイト側が週次%を出さない系列（田中貴金属・BDI個別ページ等）は自前履歴から算出する
            # （P0-②・2026-07-28。これが無いと weekly が永久に None で発火経路が4週累積だけになる）。
            # 自前履歴依存なので four_week と同じく status==ok の時のみ判定に使う。
            # 月次系列は元データが月1回しか動かず、5〜9日前と比べても常に0%になるので対象外
            if parsed.get("weekly_pct") is None and s.get("cadence") != "monthly" \
                    and s["type"] != "spread":
                wk_cands = [r for d, r in by_date.items()
                            if d and 5 <= (target_dt - datetime.strptime(d, "%Y-%m-%d")).days <= 9
                            and r.get("value")]
                if wk_cands:
                    row["weekly_pct"] = round((parsed["value"] / wk_cands[-1]["value"] - 1) * 100, 2)
                    row["weekly_src"] = "self"  # サイト提供値と自前計算値を混同しないための出所印
                    # 組成ジャンプガード（Codex C-1）: 週次も基準週と件数2%超の差なら判定無効
                    if parsed.get("layout") == "yuyutei_sar_v1":
                        bn = wk_cands[-1].get("n_items")
                        if not bn or abs(parsed["n_items"] - bn) / bn > 0.02:
                            row["weekly_pct"], row["weekly_src"] = None, "mix_change"
            # 閾値は系列側で上書き可（既定は全系列共通）。電力のように平常時の変動が大きい
            # 系列に一律+5%を当てると常時発火して使い物にならないため（JEPX実測: 週次変化の
            # 平均絶対値13.9%・+5%だと41%の日で発火）。上書き値は series.alert に根拠つきで置く
            th = {**alert_cfg, **s.get("alert", {})}
            trigger = []
            if s["type"] == "spread":
                # スプレッドは%でなく絶対値(USD/T)で判定する（負値を取りうるため・上記コメント）
                wa, fa = parsed.get("weekly_abs"), four_w_abs
                row["weekly_abs"], row["four_week_abs"] = wa, fa
                if wa is not None and wa >= th.get("weekly_abs_usd", 15.0):
                    trigger.append(f"週次 {wa:+.1f}USD/T")
                if row["status"] == "ok" and fa is not None and fa >= th.get("four_week_abs_usd", 30.0):
                    trigger.append(f"4週 {fa:+.1f}USD/T")
            elif s.get("cadence") == "monthly":
                # 月次レーン: 週次の閾値体系（週+5%等）は月次統計の刻み（0.1〜0.5%/月）に
                # 対して大きすぎて永久に鳴らない。前月比を専用閾値で見る。
                # かつ**同じ月の値で毎週鳴らない**よう、前回記録と同じ公表月なら判定しない
                # 月次判定は status==ok の時のみ（suspect_jump＝50%跳びの取得値で発火して
                # forward台帳を汚染しない。weekly_src=self/4週累積と同じ安全規約・Codex C-2）
                prev_months = {r.get("src_date") for r in prev if r.get("src_date")}
                mo = parsed.get("monthly_pct")
                if row["status"] == "ok" and parsed.get("src_date") not in prev_months \
                        and mo is not None and mo >= th.get("monthly_pct", 1.0):
                    trigger.append(f"前月比 {mo:+.2f}%({parsed['src_date']}公表)")
                # USS等、前月比が季節性で使えない月次系列は表側の前年同月比で判定する
                # （系列側 alert.yoy_pct を明示したときだけ働く＝キー存在で判定・Codex NIT-6。
                # 同じ公表月で毎週鳴らない条件は前月比と同一）
                yy = parsed.get("yoy_pct")
                if row["status"] == "ok" and "yoy_pct" in th and yy is not None \
                        and parsed.get("src_date") not in prev_months and yy >= th["yoy_pct"]:
                    trigger.append(f"前年同月比 {yy:+.2f}%({parsed['src_date']}分)")
            else:
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

            # 原材料ピークアウト検知（2026-08-02 新設・食品の「原価圧力の反転」レーン）。
            # 食品株は値上げでは上がらず、原材料が落ち着いた後に値上げ効果が残った期に上がる
            # （実測: 森永乳業 単価+185億 vs 原料△92億=正味+93億の期が増益・§16f）。
            # そこでコスト側系列（peakout=true）は上昇でなく「山を越えた」ことを検知する:
            #   条件 = 履歴高値から peakout_drop_pct(既定15%)以上下 かつ 直近4記録が単調下落。
            # 履歴が8本（週次≒2か月）貯まるまでは判定しない（高値の基準が浅すぎるため）。
            if s.get("peakout") and row["status"] == "ok":
                past = sorted([r for r in prev if r.get("value")],
                              key=lambda r: (r.get("date", ""), r.get("run_at", "")))
                hist_ok = past + [row]
                # 月次系列（米・卵など）は週次実行で同じ公表月の行が繰り返し貯まるため、
                # src_date で 1公表月=1点 に間引いてから判定する。間引かないと
                # 「直近4記録単調下落」が同値の並びで永久に成立せず、レーンが沈黙する
                # （2026-08-02 波2で月次ピークアウト系列を新設した際の適合）。
                # さらに判定は「新しい公表月を初めて見た週」だけ行う（Codex C-3/C-4/C-5）:
                #   ①既出の公表月＝成立状態が続くと毎週再発火する ②サイトが旧月へ巻き戻った
                #   場合＝旧月値と未来側履歴が混ざった判定になる ③src_date無し＝契約崩れ、
                #   のいずれも判定しない（fail-closed。値の記録は続く）
                judge = True
                if s.get("cadence") == "monthly":
                    sd = row.get("src_date")
                    seen_months_po = {r.get("src_date") for r in past if r.get("src_date")}
                    by_src: dict[str, dict] = {}
                    for r in hist_ok:
                        if r.get("src_date"):
                            by_src[r["src_date"]] = r
                    hist_ok = sorted(by_src.values(), key=lambda r: r["src_date"])
                    past = hist_ok[:-1]
                    if not sd or sd in seen_months_po or sd != max(by_src):
                        judge = False
                if judge and len(past) >= 8:   # 過去8本（週次≒2か月/月次=8公表月）必要。当日分は数えない
                    peak = max(r["value"] for r in hist_ok)
                    drop = (row["value"] / peak - 1) * 100
                    last5 = [r["value"] for r in hist_ok[-5:]]
                    falling4 = all(last5[i] > last5[i + 1] for i in range(len(last5) - 1))
                    row["peak_value"], row["drop_from_peak_pct"] = peak, round(drop, 1)
                    if drop <= -th.get("peakout_drop_pct", 15.0) and falling4:
                        relief = "/".join(f"{c['code']}{c['name'][:6]}"
                                          for c in s.get("cost_relief_for", [])[:8])
                        trigger.append(f"PEAKOUT 高値比{drop:+.1f}%・4週連続下落")
                        row["peakout_fired"] = True
                        print(f"  📉→📈 原材料ピークアウト: {s['jp']} 高値比{drop:+.1f}% "
                              f"→ 原価圧力の反転候補: {relief}")

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
    # ピークアウト発火は「値上がり受益」の前向き検定（n>=100・事前登録）に混ぜない。
    # レーンが違う発火を同じ台帳に入れると検定の分母が汚れる（言及レーンと同じ裁定 2026-07-31）
    rise_alerts = [(s, r, t) for s, r, t in alerts if not r.get("peakout_fired")]
    peak_alerts = [(s, r, t) for s, r, t in alerts if r.get("peakout_fired")]
    if peak_alerts:
        print(f"\n📉→📈 原材料ピークアウト {len(peak_alerts)} 系列（原価圧力の反転・食品レーン）:")
        for s, row, trigger in peak_alerts:
            relief = "/".join(f"{c['code']}{c['name'][:6]}" for c in s.get("cost_relief_for", []))
            print(f"  {s['jp']}（{'/'.join(trigger)}）→ 反転候補: {relief or 'なし'}")
            print("    ※次の四半期ブリッジ（scripts/food_bridge_fetch.py）で正味プラス転換を確認してから判断")
    if rise_alerts:
        print(f"\n🚨 閾値超え {len(rise_alerts)} 系列:")
        for s, row, trigger in rise_alerts:
            print(f"  {s['jp']}（{'/'.join(trigger)}）→ 受益: {beneficiaries_display(s, today)}")
        # 前向き記録（レビューC-1対応: 発火を将来検定できる形で残す）
        try:
            import price_watch_forward as fwd
            fwd.record_firings(rise_alerts, today)
        except Exception as exc:  # noqa: BLE001  記録失敗で本処理を落とさない
            print(f"[forward] WARN: 前向き記録に失敗: {str(exc)[:100]}")
    else:
        print("閾値超えなし（4週累積は履歴4本蓄積後から判定）")
    return 0 if ok > 0 else 1


def _selftest() -> int:
    """USS/遊々亭パーサの境界回帰テスト（Codex NIT-7）。ネットワーク不要・fixtures内蔵。

    実行: python scripts/price_universe_check.py --selftest
    """
    fails = []

    def chk(name: str, cond: bool) -> None:
        print(("  ok " if cond else "  NG ") + name)
        if not cond:
            fails.append(name)

    def uss_html(month_rows: list, fy: str = "2027") -> str:
        trs = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
                      for r in month_rows)
        return f"{fy}年3月期 <table>{trs}</table>"

    def mrow(name: str, tanka: str = "", yoy: str = "") -> list:
        base = [name] + [""] * 13
        if tanka:
            base[1:14] = ["79", "80", "348,991", "327,914", "106.4%", "224,073",
                          "200,476", "111.8%", "64.2%", "61.1%", tanka, "1,065", yoy]
        return base

    # 1) 通常: 完全月2つ＋未発表空行＋全角月名の14セル数値行（＝除外されるべき）
    u = parse_uss(uss_html([mrow("4月", "1,221", "114.6%"), mrow("5月", "1,310", "110.6%"),
                            mrow("6月"), mrow("５月", "9,999", "999.9%")]))
    chk("uss 最新完全月=5月・全角行除外", bool(u) and u["src_date"] == "2026-05"
        and u["value"] == 1310.0)
    chk("uss 前月比自前計算", bool(u) and abs(u["monthly_pct"] - 7.29) < 0.02)
    chk("uss 前年比=表の値-100", bool(u) and u["yoy_pct"] == 10.6)
    # 2) 年度境界: 「2027年3月期」の1月は2027年
    u = parse_uss(uss_html([mrow("1月", "1,300", "105.0%")]))
    chk("uss 1月→2027-01", bool(u) and u["src_date"] == "2027-01")
    # 3) 同一年月が別値で重複（過去年度表の併載など）→ 静かに誤値を採らず parse_fail
    u = parse_uss(uss_html([mrow("4月", "1,221", "114.6%"), mrow("4月", "1,999", "100.0%")]))
    chk("uss 年月衝突→None", u is None)

    def card(price: int) -> str:
        return ('card-product<a href="https://yuyu-tei.jp/sell/poc/card/m06/1"></a>'
                f"<strong>{price:,} 円</strong>")

    # 4) 中央値の偶奇
    y = parse_yuyutei("".join(card(p) for p in [100] * 30 + [200] * 30))
    chk("yuyu 偶数中央値=150", bool(y) and y["value"] == 150.0 and y["n_items"] == 60)
    y = parse_yuyutei("".join(card(p) for p in [100] * 30 + [200] * 31))
    chk("yuyu 奇数中央値=200", bool(y) and y["value"] == 200.0)
    # 5) 49件は市場縮小とパース破損を区別できないため None・50件は ok
    chk("yuyu 49件→None", parse_yuyutei("".join(card(100) for _ in range(49))) is None)
    chk("yuyu 50件→ok", parse_yuyutei("".join(card(100) for _ in range(50))) is not None)
    # 6) 商品hrefの無いブロック（CSS等の偶然の card-product）は数えない
    y = parse_yuyutei("".join(card(100) for _ in range(50))
                      + "card-product<strong>9,999 円</strong>")
    chk("yuyu 非商品ブロック除外", bool(y) and y["n_items"] == 50)

    print(f"[selftest] {'FAIL: ' + ', '.join(fails) if fails else 'all ok'}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(main())
