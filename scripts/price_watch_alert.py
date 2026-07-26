"""price_watch: 日次件数台帳から急増（zスコア）アラートを判定する.

data/x_price_watch/ledger.jsonl の直近 baseline_days（既定14日・status==ok かつ
censored==false の日のみ）を query_id ごとのベースラインとし、対象日の件数の
zスコア >= z_threshold（既定3.0）でアラートを出す。

規約:
- ベースラインの仕様は「**直近 baseline_days 暦日ぶんのエントリのうち clean 行のみ**」
  （clean = status==ok かつ censored==false かつ battery_sha が対象日と同一）。
  欠測や打ち切りがあると実効ベースラインは baseline_days より少なくなる（仕様として宣言）。
- 同日に複数回実行された日は「最後の clean 行」を優先採用（無ければ最終行）。
- ベースライン日数 < min_baseline_days（既定7）はウォームアップ扱い＝判定スキップ。
- σ=0（毎日同数）はゼロ除算せず、count > mean のときのみ sigma0_jump として警告扱い。
- 同一 (date, query_id) のアラートは重複追記しない（再実行しても通知が増えない）。
- 出力: output/price_watch/alerts-YYYY-MM-DD.jsonl（検知時のみ append・gitignore対象）
  + stdout の全クエリ表。

実行（stdlibのみ・ホスト/コンテナ両対応）:
    python3 scripts/price_watch_alert.py            # 台帳の最新日を判定
    python3 scripts/price_watch_alert.py --selftest # 合成データで検知ロジックを自己検証
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent

DEFAULT_CONFIG = APP / "configs/x_price_watch.json"
DEFAULT_LEDGER = APP / "data/x_price_watch/ledger.jsonl"
DEFAULT_OUT_DIR = APP / "output/price_watch"


def load_ledger(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # 壊れ行は捨てる（x_watchlist_tracer.load_recent_snapshots と同じガード）
        if not isinstance(row.get("date"), str) or not isinstance(row.get("query_id"), str):
            continue
        rows.append(row)
    return rows


def judge_query(history: list[dict], target_date: str, alert_cfg: dict) -> dict:
    """1クエリ分の履歴（date昇順・同日複数は最終行採用）から対象日を判定する。

    Returns:
        {"verdict": alert|ok|warmup|no_data|not_clean|sigma0_jump, "z", "count",
         "baseline_mean", "baseline_std", "n_baseline"}
    """
    def is_clean(row: dict) -> bool:
        return row.get("status") == "ok" and not row.get("censored") and row.get("count") is not None

    by_date: dict[str, dict] = {}
    for row in sorted(history, key=lambda r: (r["date"], r.get("run_at", ""))):
        # 同日再実行は「最後の clean 行」を優先（後の blocked/crash が正常値を潰さない）
        if row["date"] not in by_date or is_clean(row) or not is_clean(by_date[row["date"]]):
            by_date[row["date"]] = row

    target = by_date.get(target_date)
    if target is None or target.get("count") is None:
        return {"verdict": "no_data", "z": None, "count": None,
                "baseline_mean": None, "baseline_std": None, "n_baseline": 0}
    if target.get("status") != "ok" or target.get("censored"):
        return {"verdict": "not_clean", "z": None, "count": target.get("count"),
                "baseline_mean": None, "baseline_std": None, "n_baseline": 0}

    baseline_dates = sorted(d for d in by_date if d < target_date)
    baseline = [
        by_date[d]["count"]
        for d in baseline_dates[-alert_cfg["baseline_days"]:]
        if is_clean(by_date[d])
        and by_date[d].get("battery_sha") == target.get("battery_sha")  # クエリ凍結の実効化
    ]
    result = {"count": target["count"], "n_baseline": len(baseline),
              "baseline_mean": round(mean(baseline), 2) if baseline else None,
              "baseline_std": round(pstdev(baseline), 2) if baseline else None, "z": None}
    if len(baseline) < alert_cfg["min_baseline_days"]:
        return {**result, "verdict": "warmup"}

    mu, sigma = mean(baseline), pstdev(baseline)
    if sigma == 0:
        verdict = "sigma0_jump" if target["count"] > mu else "ok"
        return {**result, "verdict": verdict}
    z = (target["count"] - mu) / sigma
    result["z"] = round(z, 2)
    return {**result, "verdict": "alert" if z >= alert_cfg["z_threshold"] else "ok"}


def selftest() -> int:
    alert_cfg = {"baseline_days": 14, "min_baseline_days": 7, "z_threshold": 3.0}
    days = [f"2026-07-{d:02d}" for d in range(1, 16)]

    def rows(query_id, counts, **extra):
        return [{"date": d, "query_id": query_id, "count": c, "status": "ok",
                 "censored": False, **extra} for d, c in zip(days, counts)]

    spike = rows("q-spike", [10, 11, 9, 10, 12, 10, 11, 9, 10, 11, 10, 9, 11, 10, 50])
    flat = rows("q-flat", [10, 11, 9, 10, 12, 10, 11, 9, 10, 11, 10, 9, 11, 10, 11])
    warm = rows("q-warm", [5, 6, 5, 4, 5])  # 5日分しかない
    censored_target = rows("q-cens", [10] * 14 + [80])
    censored_target[-1]["censored"] = True

    checks = [
        ("spike→alert", judge_query(spike, days[-1], alert_cfg)["verdict"], "alert"),
        ("flat→ok", judge_query(flat, days[-1], alert_cfg)["verdict"], "ok"),
        ("warmup→warmup", judge_query(warm, warm[-1]["date"], alert_cfg)["verdict"], "warmup"),
        ("censored対象日→not_clean", judge_query(censored_target, days[-1], alert_cfg)["verdict"], "not_clean"),
        ("sigma0で増→sigma0_jump",
         judge_query(rows("q-s0", [10] * 14 + [20]), days[-1], alert_cfg)["verdict"], "sigma0_jump"),
    ]
    failed = [f"{name}: got={got} want={want}" for name, got, want in checks if got != want]
    for name, got, want in checks:
        print(f"  {'PASS' if got == want else 'FAIL'} {name} (got={got})")
    if failed:
        print(f"selftest FAILED: {failed}")
        return 1
    print("selftest PASS (5/5)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--date", help="判定対象日 YYYY-MM-DD（省略時=台帳の最新日）")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    config = json.loads(args.config.read_text())
    alert_cfg = config["alert"]
    rows = load_ledger(args.ledger)
    if not rows:
        print(f"台帳が空です: {args.ledger}")
        return 1
    target_date = args.date or max(r["date"] for r in rows)

    by_query: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_query[row["query_id"]].append(row)

    alerts = []
    print(f"=== price_watch 判定 {target_date}（baseline {alert_cfg['baseline_days']}日・"
          f"z>={alert_cfg['z_threshold']}）===")
    hdr = f"{'query_id':<24}{'count':>7}{'mean':>8}{'std':>7}{'z':>7}  verdict"
    print(hdr)
    print("-" * len(hdr))
    for entry in config["queries"]:
        qid = entry["id"]
        res = judge_query(by_query.get(qid, []), target_date, alert_cfg)
        z_txt = "-" if res["z"] is None else f"{res['z']:.2f}"
        cnt = "-" if res["count"] is None else res["count"]
        mu = "-" if res["baseline_mean"] is None else res["baseline_mean"]
        sd = "-" if res["baseline_std"] is None else res["baseline_std"]
        print(f"{qid:<24}{cnt:>7}{mu:>8}{sd:>7}{z_txt:>7}  {res['verdict']}")
        if res["verdict"] in ("alert", "sigma0_jump"):
            latest = max(
                (r for r in by_query[qid] if r["date"] == target_date),
                key=lambda r: r.get("run_at", ""),
            )
            alerts.append({"date": target_date, "query_id": qid, "lane": entry.get("lane", ""),
                           "q": entry["q"], **res, "samples": latest.get("samples", [])})

    if alerts:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        out = args.out_dir / f"alerts-{target_date}.jsonl"
        seen = {(r.get("date"), r.get("query_id")) for r in load_ledger(out)}
        new_alerts = [a for a in alerts if (a["date"], a["query_id"]) not in seen]
        with out.open("a", encoding="utf-8") as fh:
            for a in new_alerts:
                fh.write(json.dumps(a, ensure_ascii=False) + "\n")
        print(f"\n🚨 アラート {len(alerts)} 件（新規追記 {len(new_alerts)} 件）→ {out}")
    else:
        print("\nアラートなし")
    return 0


if __name__ == "__main__":
    sys.exit(main())
