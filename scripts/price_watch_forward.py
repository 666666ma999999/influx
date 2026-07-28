"""price_watch_forward: 値上がり発火の前向き記録と評価.

2026-07-28 の2モデル敵対レビュー C-1（最重要指摘）への対応。それまで発火は print される
だけで記録が残らず、「いつ何が鳴ったか」すら復元できないため、このシステムの土台仮説
（実物価格の変曲 → 受益株の上昇が8〜15週遅行・現在 n=2）を将来検定する手段が
構造的に存在しなかった。

## 凍結する事前宣言（v1・2026-07-28。変更は version を上げて別系列として扱う）
- entry      : 発火日時点で入手できる直近営業日の調整後終値（AdjC）＝ look-ahead なし
- windows    : 40営業日（≒8週）と 75営業日（≒15週）
- metric     : 銘柄リターン − TOPIXリターン（＝超過リターン。市場全体の上下を除く）
- hit        : 超過リターン > 0
- 対象       : configs/price_universe_sources.json の受益銘柄（発火系列のもの全件・選別なし）
- 除外       : 「(受益なし…)」表記の系列は銘柄ゼロで記録のみ（後知恵の銘柄選びを禁止）

## 位置づけ
これは**観察記録**であり、正式レシピのα台帳（data/kpi_trials/trials.jsonl）には登録しない。
監視トリガーを正式レシピと同列に数えると多重比較の粒度が壊れるため（レビュアー懸念への配慮）。

## 使い方
    # 記録は price_universe_check.py から自動（発火時のみ）
    docker compose run --rm xstock python scripts/price_watch_forward.py --eval    # 期日到来分を評価
    docker compose run --rm xstock python scripts/price_watch_forward.py --status  # 蓄積状況
"""
from __future__ import annotations

import argparse
import bisect
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "scripts"))

import jq_fetch  # noqa: E402  Canonical データローダ
import measure_base_rate as mbr  # noqa: E402  Canonical カレンダー/bars

LOG_PATH = APP / "data/price_watch/forward_log.jsonl"
SPEC_VERSION = 1
WINDOWS_BD = {"w8": 40, "w15": 75}  # 営業日（≒8週 / ≒15週）
CODE_RE = re.compile(r"(?<![0-9A-Za-z])([0-9]{4}|[0-9]{3}[A-Z])(?![0-9A-Za-z])")


def to_code5(code4: str) -> str:
    return code4.upper() + "0"


def load_topix() -> dict[str, float]:
    obj = jq_fetch.read_json_gz(jq_fetch.DATA_ROOT / "topix.json.gz")
    return {r["Date"].replace("-", ""): float(r["C"]) for r in obj["data"] if r.get("C")}


def business_days() -> list[str]:
    return mbr.all_business_days(mbr.load_calendar_days())


def latest_bar_day(bdays: list[str], on_or_before: str) -> str | None:
    """指定日以前で bars が実在する直近営業日（look-ahead 防止のため未来は見ない）。"""
    idx = bisect.bisect_right(bdays, on_or_before) - 1
    for i in range(idx, max(idx - 10, -1), -1):
        if (jq_fetch.DATA_ROOT / "bars" / f"{bdays[i]}.json.gz").exists():
            return bdays[i]
    return None


def close_of(code5: str, day: str) -> float | None:
    try:
        rec = mbr.load_bars_day(day).get(code5)
    except SystemExit:
        return None
    if not rec or not rec.get("AdjC"):
        return None
    return float(rec["AdjC"])


def append(event: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_log() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    out = []
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def ensure_preregistration() -> None:
    """事前宣言を1回だけ書き込む（凍結条件の証跡。以後の変更は version を上げる）。"""
    if any(e.get("type") == "preregistration" and e.get("spec_version") == SPEC_VERSION
           for e in read_log()):
        return
    append({
        "type": "preregistration", "spec_version": SPEC_VERSION,
        "registered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hypothesis": "商品価格の発火（weekly>=+5% or 4週累積>=+10%）から、受益銘柄が"
                      "8〜15週で TOPIX を超過する",
        "entry_rule": "発火日時点の直近営業日 AdjC（look-aheadなし）",
        "windows_bd": WINDOWS_BD,
        "metric": "銘柄リターン - TOPIXリターン（超過リターン）",
        "hit_rule": "超過リターン > 0",
        "universe_rule": "configs/price_universe_sources.json の当該系列の受益銘柄・全件（後知恵の選別禁止）",
        "note": "観察記録。trials.jsonl（正式αレーン）には登録しない",
    })
    print(f"[forward] 事前宣言 v{SPEC_VERSION} を記録")


def record_firings(alerts: list, fire_date: str) -> int:
    """price_universe_check の発火を前向き記録する。alerts=[(series_cfg, row, triggers)]"""
    if not alerts:
        return 0
    ensure_preregistration()
    bdays = business_days()
    base_day = latest_bar_day(bdays, fire_date.replace("-", ""))
    if base_day is None:
        print("[forward] WARN: 基準日の bars が無いため記録をスキップ")
        return 0
    base_idx = bdays.index(base_day)
    eval_days = {k: bdays[base_idx + n] if base_idx + n < len(bdays) else None
                 for k, n in WINDOWS_BD.items()}
    topix = load_topix()

    n = 0
    for series, row, triggers in alerts:
        # 帰属プロトコルv2: 受益カードの sign=+ かつ confirmed/provisional を全件記録
        # （仮=provisional も記録に入れる裁定 2026-07-28。rejected は買いシグナル禁止で除外）。
        # 旧形式（stocks 自由文字列）の config にも後方互換
        if "beneficiaries" in series:
            code_tiers = [(to_code5(b["code"]), b.get("tier", "confirmed"))
                          for b in series["beneficiaries"]
                          if b.get("sign") == "+" and b.get("tier") in ("confirmed", "provisional")]
        else:
            code_tiers = [(to_code5(c), "confirmed")
                          for c in CODE_RE.findall(series.get("stocks", ""))]
        stocks = []
        for c5, tier in code_tiers:
            px = close_of(c5, base_day)
            if px is not None:
                stocks.append({"code": c5, "entry_close": px, "tier": tier})
        append({
            "type": "firing", "spec_version": SPEC_VERSION,
            "fire_date": fire_date, "series_id": series["id"], "series_jp": series["jp"],
            "triggers": triggers,
            "commodity": {"value": row.get("value"), "weekly_pct": row.get("weekly_pct"),
                          "four_week_pct": row.get("four_week_pct")},
            "base_day": base_day, "topix_entry": topix.get(base_day),
            "eval_days": eval_days, "stocks": stocks,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        n += 1
        print(f"[forward] 記録: {series['jp']}（{'/'.join(triggers)}）銘柄{len(stocks)}件 "
              f"基準={base_day} 評価予定={eval_days['w8']}/{eval_days['w15']}")
    return n


def evaluate() -> int:
    """期日が到来した firing を評価して evaluation を append する（冪等）。"""
    log = read_log()
    firings = [e for e in log if e.get("type") == "firing"]
    done = {(e["fire_date"], e["series_id"], e["window"]) for e in log
            if e.get("type") == "evaluation"}
    if not firings:
        print("[forward] 発火記録がまだありません（発火時に自動で記録されます）")
        return 0
    topix = load_topix()
    today = datetime.now().strftime("%Y%m%d")
    n_new = 0
    for f in firings:
        for win, eval_day in (f.get("eval_days") or {}).items():
            if not eval_day or eval_day > today:
                continue
            if (f["fire_date"], f["series_id"], win) in done:
                continue
            tpx_now = topix.get(eval_day)
            tpx_ent = f.get("topix_entry")
            if not tpx_now or not tpx_ent:
                continue
            tpx_ret = (tpx_now / tpx_ent - 1) * 100
            results = []
            for s in f.get("stocks", []):
                px = close_of(s["code"], eval_day)
                if px is None:
                    results.append({"code": s["code"], "status": "no_data"})
                    continue
                ret = (px / s["entry_close"] - 1) * 100
                results.append({"code": s["code"], "ret_pct": round(ret, 2),
                                "excess_pct": round(ret - tpx_ret, 2),
                                "hit": bool(ret - tpx_ret > 0), "status": "ok"})
            scored = [r for r in results if r["status"] == "ok"]
            append({
                "type": "evaluation", "spec_version": SPEC_VERSION,
                "fire_date": f["fire_date"], "series_id": f["series_id"], "window": win,
                "eval_day": eval_day, "topix_ret_pct": round(tpx_ret, 2),
                "results": results,
                "n_hit": sum(1 for r in scored if r["hit"]), "n_scored": len(scored),
                "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            n_new += 1
            hits = sum(1 for r in scored if r["hit"])
            print(f"[eval] {f['series_jp']} {win}: {hits}/{len(scored)} hit "
                  f"(TOPIX {tpx_ret:+.1f}%)")
    print(f"[forward] 新規評価 {n_new} 件")
    return n_new


def status() -> None:
    log = read_log()
    firings = [e for e in log if e.get("type") == "firing"]
    evals = [e for e in log if e.get("type") == "evaluation"]
    print(f"=== 前向き記録の蓄積状況（spec v{SPEC_VERSION}）===")
    print(f"発火記録: {len(firings)} 件 / 評価済み: {len(evals)} 件")
    if firings:
        print(f"期間: {min(f['fire_date'] for f in firings)} 〜 {max(f['fire_date'] for f in firings)}")
        n_stocks = sum(len(f.get("stocks", [])) for f in firings)
        print(f"のべ銘柄観測: {n_stocks} 件（判定に必要な目安 n>=100）")
    if evals:
        hit = sum(e["n_hit"] for e in evals)
        tot = sum(e["n_scored"] for e in evals)
        rate = f"{hit / tot:.1%}" if tot else "-"
        print(f"超過リターン勝率（暫定・検定前）: {hit}/{tot} = {rate}")
        print("※ 勝率は参考値。事前宣言どおり n>=100 かつ独立エピソード複数まで判定しない")
    pending = [f for f in firings
               if not any(e["fire_date"] == f["fire_date"] and e["series_id"] == f["series_id"]
                          for e in evals)]
    if pending:
        print(f"評価待ち: {len(pending)} 件（直近の評価予定日: "
              f"{min(f['eval_days']['w8'] for f in pending if f.get('eval_days', {}).get('w8'))}）")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--eval", action="store_true", help="期日到来分を評価")
    parser.add_argument("--status", action="store_true", help="蓄積状況を表示")
    args = parser.parse_args()
    if args.eval:
        evaluate()
    status()
    return 0


if __name__ == "__main__":
    sys.exit(main())
