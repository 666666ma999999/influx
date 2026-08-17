#!/usr/bin/env python3
"""海外上場カードの前向き記録（§16w・2026-08-17 P-08c 裁定）。

日本株の前向き検定（`price_watch_forward.py`・対TOPIX超過・事前登録 n>=100）とは
**別台帳・別ベンチマーク**で記録する。基準指数も営業日も違うものを同じ分母に入れると
検定が壊れるため（ピークアウト §16j・浸透 §16v と同じレーン分離の裁定）。

- 記録単位: 発火した系列 × 海外カード（重複は fire_date+ticker で排除）
- 指標: 銘柄リターン − ベンチマーク（カードの `benchmark`）リターン
- 価格: yfinance（Docker イメージに同梱）。取得失敗は fail-soft で `entry_close=None` を残し、
  後続の実行で埋め直せるようにする（黙って捨てない）

実行:
    python3 scripts/foreign_forward.py --selftest   # 判定ロジックの固定テスト
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = ROOT / "data" / "price_watch" / "foreign_forward_log.jsonl"
SPEC_VERSION = "foreign-v1"
# 日本株レーンと同じ評価窓（8/15営業日）に揃える。海外の営業日で数える
WINDOWS_BD = {"w8": 40, "w15": 75}


def foreign_cards(series: Dict[str, Any]) -> List[Dict[str, Any]]:
    """§16w の要件を満たす海外カードだけを返す。

    要件: market が JP 以外 / sign='+' / tier が confirmed|provisional /
    ticker と benchmark の両方を持つ（欠けたものは価格も評価もできないので除外）。

    Args:
        series: configs/price_universe_sources.json の系列 dict。

    Returns:
        条件を満たすカードのリスト。
    """
    out: List[Dict[str, Any]] = []
    for card in series.get("beneficiaries") or []:
        if not isinstance(card, dict):
            continue
        if (card.get("market") or "JP") == "JP":
            continue
        if card.get("sign") != "+" or card.get("tier") not in ("confirmed", "provisional"):
            continue
        if not card.get("ticker") or not card.get("benchmark"):
            continue
        out.append(card)
    return out


def read_log(path: Path = LEDGER_PATH) -> List[Dict[str, Any]]:
    """台帳を読む（無ければ空）。"""
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def already_recorded(rows: List[Dict[str, Any]], fire_date: str, ticker: str) -> bool:
    """同じ発火日・同じ銘柄が既に記録済みか（二重計上の防止）。"""
    return any(r.get("fire_date") == fire_date and r.get("ticker") == ticker for r in rows)


def fetch_close(ticker: str, day: str) -> Optional[float]:
    """指定日の終値を取る。取得できなければ None（fail-soft）。

    Args:
        ticker: yfinance のティッカー（例 "GL9.IR"）。
        day: "YYYY-MM-DD"。

    Returns:
        終値。休場・取得失敗時は None。
    """
    try:
        import yfinance as yf
        import pandas as pd  # noqa: F401  yfinance の依存（存在確認）

        end = (datetime.strptime(day, "%Y-%m-%d")).strftime("%Y-%m-%d")
        hist = yf.Ticker(ticker).history(start=end, period="5d", auto_adjust=True)
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[0])
    except Exception:  # noqa: BLE001  価格取得の失敗で本線を止めない
        return None


def record_firings(alerts: List[Tuple[Dict, Dict, List[str]]], fire_date: str,
                   path: Path = LEDGER_PATH) -> int:
    """発火した系列の海外カードを別台帳へ記録する。

    Args:
        alerts: (series, row, triggers) のリスト（price_universe_check と同じ形）。
        fire_date: 発火日 "YYYY-MM-DD"。
        path: 台帳パス。

    Returns:
        記録した行数。
    """
    rows = read_log(path)
    n = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for series, row, triggers in alerts:
            for card in foreign_cards(series):
                ticker = str(card["ticker"])
                if already_recorded(rows, fire_date, ticker):
                    continue
                entry = fetch_close(ticker, fire_date)
                bench = fetch_close(str(card["benchmark"]), fire_date)
                rec = {
                    "type": "firing", "spec_version": SPEC_VERSION,
                    "fire_date": fire_date, "series_id": series.get("id"),
                    "series_jp": series.get("jp"),
                    "ticker": ticker, "market": card.get("market"),
                    "name": card.get("name"), "tier": card.get("tier"),
                    "benchmark": card.get("benchmark"),
                    "entry_close": entry, "benchmark_entry": bench,
                    "triggers": triggers,
                    "commodity": {"value": row.get("value"),
                                  "weekly_pct": row.get("weekly_pct")},
                    "windows_bd": WINDOWS_BD,
                    "note": ("§16w 別レーン。対TOPIX検定には含めない。"
                             "entry_close が null の行は価格未取得＝後続実行で埋める"),
                    "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                rows.append(rec)
                n += 1
                px = f"{entry}" if entry is not None else "価格未取得"
                print(f"[foreign] 記録: {series.get('jp')} → [{card.get('market')}]"
                      f"{card.get('name') or ticker} 基準={fire_date} 終値={px}")
    return n


def selftest() -> int:
    """カード選別と二重排除の固定テスト（ネットワーク不要）。"""
    cases: List[Tuple[str, bool]] = []
    s = {"id": "dry-whey", "beneficiaries": [
        {"code": "5713", "sign": "+", "tier": "confirmed"},                      # JP は対象外
        {"code": "A", "market": "IE", "ticker": "GL9.IR", "benchmark": "^ISEQ",
         "sign": "+", "tier": "provisional"},                                    # 対象
        {"code": "B", "market": "US", "ticker": "X", "sign": "+", "tier": "confirmed"},  # benchmark 欠落
        {"code": "C", "market": "US", "benchmark": "^GSPC", "sign": "+", "tier": "confirmed"},  # ticker欠落
        {"code": "D", "market": "US", "ticker": "Y", "benchmark": "^GSPC",
         "sign": "+", "tier": "rejected"},                                       # 却下
        {"code": "E", "market": "US", "ticker": "Z", "benchmark": "^GSPC",
         "sign": "-", "tier": "confirmed"},                                      # 逆風
    ]}
    picked = [c["code"] for c in foreign_cards(s)]
    cases.append(("JP は対象外", "5713" not in picked))
    cases.append(("要件を満たす海外カードのみ採用", picked == ["A"]))
    cases.append(("ticker/benchmark 欠落は除外", "B" not in picked and "C" not in picked))
    cases.append(("rejected/逆風は除外", "D" not in picked and "E" not in picked))
    cases.append(("カード無しは空", foreign_cards({"beneficiaries": []}) == []))
    cases.append(("beneficiaries 欠落でも落ちない", foreign_cards({}) == []))

    rows = [{"fire_date": "2026-08-17", "ticker": "GL9.IR"}]
    cases.append(("同日同銘柄は二重記録しない", already_recorded(rows, "2026-08-17", "GL9.IR")))
    cases.append(("別日は記録してよい", not already_recorded(rows, "2026-08-24", "GL9.IR")))
    cases.append(("別銘柄は記録してよい", not already_recorded(rows, "2026-08-17", "ABC")))

    ok = sum(1 for _, passed in cases if passed)
    for name, passed in cases:
        print(f"  {'PASS' if passed else 'FAIL'} {name}")
    print(f"[selftest] {ok}/{len(cases)} PASS")
    return 0 if ok == len(cases) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="海外カードの前向き記録（§16w）")
    parser.add_argument("--selftest", action="store_true", help="固定テストを実行")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    print("このモジュールは price_universe_check から呼ばれます（単体実行は --selftest のみ）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
