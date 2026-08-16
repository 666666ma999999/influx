#!/usr/bin/env python3
"""仕込み候補の行データ（価格系列レーンの発火→受益銘柄・対TOPIX超過つき）を作る。

**2026-08-16 一本化**: ユーザー指摘「同じ役割なら1ファイルにまとめて」により、
閲覧面は `output/daily_reco.md` の「🌱 仕込み型」節**1枚だけ**にした。
本モジュールの役割は `build_rows()` の Canonical 実装で、**永続ファイルは書かない**
（単体実行は標準出力への確認用プレビューのみ＝第二の閲覧面を作らない）。

daily_reco.md（+20%/20営業日の急騰狙い）とは**別レーン**の表示層。こちらは
「商品価格が上がった → その恩恵が決算に出るまで8〜15週かかる」型の候補を一覧にする。

- 入力はすべて既存の生成物・凍結値の引用（新規推定なし・α非消費・台帳不算入）
- 受益銘柄は configs/price_universe_sources.json の受益カード（決算実読の tier つき）
- 発火は data/price_watch/forward_log.jsonl（type=firing）
- 株価は data/jquants/bars（AdjC 優先）・市場比較は data/jquants/topix.json.gz
- **金額・株数・配分は書かない**（catalog §0付記II 一方向ルール）
"""
from __future__ import annotations

import datetime as dt
import glob
import gzip
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FORWARD_LOG = REPO / "data/price_watch/forward_log.jsonl"
CONFIG = REPO / "configs/price_universe_sources.json"
BARS_DIR = REPO / "data/jquants/bars"
TOPIX = REPO / "data/jquants/topix.json.gz"
MASTER_DIR = REPO / "data/jquants/master"

LOOKBACK_DAYS = 90      # 何日前までの発火を「仕込み窓」として載せるか（表示上の既定値）
# 評価窓（8週/15週）は docs/price-watch-universe.md が正本。表示文言は daily_reco 側に持つ


def load_topix() -> dict[str, float]:
    obj = json.load(gzip.open(TOPIX))
    rows = obj if isinstance(obj, list) else (obj.get("topix") or obj.get("data") or [])
    out = {}
    for r in rows:
        d = (r.get("Date") or "").replace("-", "")
        c = r.get("Close") or r.get("C")
        if d and c:
            out[d] = float(c)
    return out


def load_prices(codes: set[str], since: str) -> dict[str, dict[str, float]]:
    """{code: {YYYYMMDD: 調整済み終値}}。**AdjC のみ**（C へのフォールバックはしない）。

    起点と終点で調整済み/未調整が混在すると分割銘柄の騰落が壊れるため（Codex R1-2）。
    AdjC が無い日はその銘柄のその日を持たない＝両端が揃わなければ超過は None になる。
    """
    series: dict[str, dict[str, float]] = {c: {} for c in codes}
    for f in sorted(glob.glob(str(BARS_DIR / "*.json.gz"))):
        day = Path(f).name[:8]
        if day < since:
            continue
        for r in json.load(gzip.open(f)).get("data", []):
            c = str(r.get("Code", ""))
            if c in series:
                # 調整済み終値(AdjC)のみを使う。C へのフォールバックは禁止（起点=AdjC・終点=C の
                # 混在で分割銘柄の騰落が壊れるため・Codex R1-2）。AdjC 欠測日はその日を持たない
                v = r.get("AdjC")
                if v is not None:
                    series[c][day] = float(v)
    return series


def code_names() -> dict[str, str]:
    files = sorted(glob.glob(str(MASTER_DIR / "*.json.gz")))
    if not files:
        return {}
    obj = json.load(gzip.open(files[-1]))
    rows = obj if isinstance(obj, list) else (obj.get("info") or obj.get("data") or [])
    return {str(r.get("Code")): (r.get("CoName") or "") for r in rows if r.get("Code")}


def next_bd(days: list[str], after: str) -> str | None:
    """発火日より後の最初の営業日（＝仕込み起点。発火当日には買えない）。"""
    for d in days:
        if d > after:
            return d
    return None


def build_rows() -> tuple[list[tuple], list[tuple], str | None]:
    """(候補行, 掲載しなかった行, データ全体の最新日) を返す。

    daily_reco 側からも import して使う（同じ計算を2箇所に書かない・Canonical Module原則）。
    """
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    # 受益カード: {(series_id, code): (tier, evidence)}
    cards: dict[tuple[str, str], tuple[str, str, str | None]] = {}  # -> (tier, evidence, verified)
    series_jp = {}
    for s in cfg["series"]:
        series_jp[s["id"]] = s["jp"]
        for b in s.get("beneficiaries", []):
            if b.get("tier") in ("confirmed", "provisional") and b.get("sign") == "+":
                cards[(s["id"], str(b["code"]))] = (b["tier"], b.get("evidence", ""), b.get("verified"))

    today = dt.date.today()
    since = (today - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    firings = []
    for line in FORWARD_LOG.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("type") != "firing":
            continue
        fd = (r.get("fire_date") or "").replace("-", "")
        if fd >= since:
            firings.append((fd, r))
    firings.sort(key=lambda x: x[0], reverse=True)

    # 対象銘柄を集める（forward_log の stocks は5桁・カードは4桁）
    wanted: set[str] = set()
    for _fd, r in firings:
        for st in r.get("stocks", []) or []:
            wanted.add(str(st.get("code")))
    prices = load_prices(wanted, since)
    tx = load_topix()
    names = code_names()
    all_days = sorted({d for p in prices.values() for d in p})
    tx_days = sorted(tx)
    latest = all_days[-1] if all_days else None

    rows_out, skipped = [], []
    for fd, r in firings:
        sid = r.get("series_id", "")
        for st in r.get("stocks", []) or []:
            code5 = str(st.get("code"))
            code4 = code5[:4]
            card = cards.get((sid, code4))
            if card is None:
                # fail-closed: 受益カードに confirmed/provisional で載っていない銘柄は**出さない**。
                # forward_log 側の tier をフォールバックに使うと、決算実読で **rejected** にした銘柄
                # （例: 5714 DOWA×銀＝価格上昇年に製錬営業利益−34.1%／3186 ネクステージ×中古車）が
                # 「仮」として候補に紛れ込む（2026-08-16 実測で検出・却下銘柄の提示は最も危険な誤り）。
                skipped.append((code4, names.get(code5, ""), r.get("series_jp", sid)))
                continue
            tier, evidence, verified = card
            # カードの鮮度: verified から365日超 or 未記載は STALE（既存 price_universe_check.py の
            # beneficiaries_display と同一規約を踏襲・期限切れカードを「確証」の顔で出さない・Codex R1-5）
            stale = True
            if verified:
                try:
                    age = (today - dt.datetime.strptime(verified, "%Y-%m-%d").date()).days
                    stale = age > 365
                except ValueError:
                    stale = True
            p = prices.get(code5, {})
            days = sorted(p)
            entry_day = next_bd(days, fd)
            # 最終日は**銘柄ごと**に持つ（売買停止・上場廃止・欠測で共通 latest を持たない銘柄が
            # 1つでもあると生成全体が KeyError で落ちるため・Codex R1-1）
            code_last = days[-1] if days else None
            if not entry_day or not code_last or entry_day == code_last:
                rows_out.append((fd, r.get("series_jp", sid), code4, names.get(code5, ""), tier,
                                 None, None, None, entry_day, evidence, stale))
                continue
            base, last = p[entry_day], p[code_last]
            chg = (last / base - 1) * 100
            # TOPIX は**銘柄と同じ両端日が存在する時だけ**比較する（近傍で補完すると
            # 銘柄と異なる期間を比べてしまう・Codex R1-3）
            tb, tl = tx.get(entry_day), tx.get(code_last)
            mkt = (tl / tb - 1) * 100 if tb and tl else None
            exc = chg - mkt if mkt is not None else None
            rows_out.append((fd, r.get("series_jp", sid), code4, names.get(code5, ""), tier,
                             chg, mkt, exc, entry_day, evidence, stale))

    def sort_key(row):
        tier_rank = 0 if row[4] == "confirmed" else 1
        exc = row[7]
        # 証拠の強い順 → 超過が小さい（＝まだ動いていない）順。未算出(None)は必ず末尾
        # （固定値置換だと実測が閾値を超えた時に順序が壊れる・Codex R1-4）
        return (tier_rank, exc is None, exc if exc is not None else 0.0)
    rows_out.sort(key=sort_key)

    return rows_out, skipped, latest


def main() -> None:
    """単体実行は**標準出力へのプレビューのみ**（ファイルは書かない・一本化のため）。"""
    rows_out, skipped, latest = build_rows()
    print(f"# 仕込み候補プレビュー（永続化しない・正本は output/daily_reco.md の「🌱 仕込み型」節）")
    print(f"データ全体の最新日: {latest} / 候補 {len(rows_out)}件 / 非掲載 {len(skipped)}件")
    print(f"{'証拠':10s} {'銘柄':6s} {'社名':16s} {'発火':10s} {'超過':>8s}")
    for fd, sjp, code4, name, tier, chg, mkt, exc, _e, _ev, stale in rows_out:
        mark = ("◎確証" if tier == "confirmed" else "△仮") + ("(要再確認)" if stale else "")
        exc_s = f"{exc:+.2f}%" if exc is not None else "—"
        print(f"{mark:10s} {code4:6s} {name[:16]:16s} {sjp[:10]:10s} {exc_s:>8s}")
    for c, n, s in skipped:
        print(f"[非掲載] {c} {n}（{s}）＝決算実読で却下 or カード未登録")


if __name__ == "__main__":
    main()
