"""保存したX投稿の本文から銘柄言及を抽出し、日次台帳とzスコア判定を作る.

位置づけ（2026-07-29 新設・docs/price-watch-universe.md §16c が正本）:
既存のXレーンは「その言葉が何件投稿されたか」だけを見ていた。だが2024-04-02（ラピダスへの
5,900億円追加支援の発表日）の「ラピダス」投稿数は対照日と同じ38件で ±0% だった一方、本文には

    「ラピダスに5900億円追加支援 この関係で北海道電力の株価が爆上がりしているのかな」

が含まれていた。**件数では鳴らないが中身には出ていた**。本スクリプトはその中身を数える。

規約（件数レーンと同じ規律を踏襲）:
- 銘柄は `data/center_pin/center_pin.jsonl`（TOP1000）に実在するものだけ（関門B）
- 1投稿で同じ銘柄を何度呼んでも1件（連呼で膨らませない）
- ベースラインは clean な日のみ・min_baseline_days 未満はウォームアップで判定しない
- **絶対件数は主張しない**。X検索は間引きがあるため同条件の相対比較専用

実行:
    python3 scripts/x_mention_extract.py                 # 最新日を抽出して判定
    python3 scripts/x_mention_extract.py --date 2026-07-29
    python3 scripts/x_mention_extract.py --rebuild       # texts/ 全日を作り直す
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "scripts"))

import x_mention_dict as xmd  # noqa: E402

TEXTS_DIR = APP / "data/x_price_watch/texts"
MENTIONS = APP / "data/x_price_watch/mentions.jsonl"
JST = timezone(timedelta(hours=9))

SCANNED_MARK = "__scanned__"   # その日を処理した印（言及ゼロの日を台帳に残すため）

BASELINE_DAYS = 14
MIN_BASELINE_DAYS = 7
Z_THRESHOLD = 3.0
# 言及ゼロが続いた後の「たった1件」で鳴らないための下限。ベースラインを0で埋めた結果、
# 大半の銘柄で標準偏差が0になり、1件でも sigma0_jump として発火してしまうため
# （2026-07-29: 見逃しバグの修正で今度は鳴りすぎる側の穴が開いたのを塞いだ）
MIN_COUNT_FOR_ALERT = 3


def extract_day(day: str, matcher, codes: set[str]) -> list[dict]:
    """1日分の本文から (code, query_id) 別の言及数を作る。"""
    path = TEXTS_DIR / f"{day}.jsonl"
    if not path.exists():
        return []
    seen: set[tuple[str, str]] = set()   # (status_id, code) の重複排除（再実行での二重計上防止）
    per: Counter = Counter()
    n_posts = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        n_posts += 1
        for m in xmd.find_mentions(r.get("text", ""), matcher, codes):
            key = (r.get("status_id", ""), m["code"])
            if key in seen:
                continue
            seen.add(key)
            per[(m["code"], m["name"], r.get("query_id", ""))] += 1
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = [{"date": day, "code": c, "name": n, "query_id": q, "count": v,
            "posts_scanned": n_posts, "run_at": run_at}
           for (c, n, q), v in sorted(per.items(), key=lambda x: -x[1])]
    # 「その日を処理した」という印を必ず1行残す。これが無いと言及ゼロの日が台帳から消え、
    # zスコアのベースラインに 0 が入らず**たまにしか名前が出ない銘柄が永久に鳴らない**
    # （2026-07-29 に実際に踏んだ見逃しバグ。14日中3日しか言及が無いと baseline_days=3 で
    #  warmup のまま固定され、20件に急増しても判定されなかった）
    out.append({"date": day, "code": SCANNED_MARK, "name": "", "query_id": "",
                "count": 0, "posts_scanned": n_posts, "run_at": run_at})
    return out


def load_mentions() -> list[dict]:
    if not MENTIONS.exists():
        return []
    out = []
    for line in MENTIONS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def write_day(rows: list[dict], day: str) -> int:
    """同じ日を再実行しても二重にならないよう、その日の行を差し替えて書き直す。"""
    keep = [r for r in load_mentions() if r.get("date") != day]
    MENTIONS.parent.mkdir(parents=True, exist_ok=True)
    with MENTIONS.open("w", encoding="utf-8") as fh:
        for r in keep + rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


def judge(rows: list[dict], target: str) -> list[dict]:
    """銘柄ごとに日次合計のzスコアを取る。ベースライン不足はウォームアップ。"""
    by_code_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    names: dict[str, str] = {}
    scanned: set[str] = set()      # 実際に処理した日（言及ゼロの日もここに入る）
    for r in rows:
        if r["code"] == SCANNED_MARK:
            scanned.add(r["date"])
            continue
        by_code_day[r["code"]][r["date"]] += r["count"]
        if r.get("name"):
            names[r["code"]] = r["name"]
    t = datetime.strptime(target, "%Y-%m-%d")
    window = {(t - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, BASELINE_DAYS + 1)}
    base_days = sorted(window & scanned)   # 処理済みの日だけをベースラインに使う
    out = []
    if target not in scanned:
        return out          # その日をまだ処理していない＝判定しない（ループ内で毎回見る必要はない）
    for code, days in by_code_day.items():
        cur = days.get(target, 0)          # 対象日に言及ゼロでも判定対象（急減も見える）
        # **言及が無かった日は 0 として数える**。これをやらないと、たまにしか名前が出ない銘柄が
        # baseline_days 不足で永久に warmup のまま固定され、急増しても鳴らない
        base = [days.get(d, 0) for d in base_days]
        if len(base) < MIN_BASELINE_DAYS:
            out.append({"code": code, "name": names.get(code, ""), "count": cur,
                        "z": None, "verdict": "warmup", "baseline_days": len(base)})
            continue
        mu, sd = mean(base), pstdev(base)
        if sd == 0:
            v = "sigma0_jump" if (cur > mu and cur >= MIN_COUNT_FOR_ALERT) else "ok"
            out.append({"code": code, "name": names.get(code, ""), "count": cur, "z": None,
                        "verdict": v, "baseline_mean": round(mu, 1), "baseline_days": len(base)})
            continue
        z = (cur - mu) / sd
        fired = z >= Z_THRESHOLD and cur >= MIN_COUNT_FOR_ALERT
        out.append({"code": code, "name": names.get(code, ""), "count": cur, "z": round(z, 2),
                    "verdict": "alert" if fired else "ok",
                    "baseline_mean": round(mu, 1), "baseline_days": len(base)})
    return sorted(out, key=lambda r: (r["z"] is None, -(r["z"] or 0), -r["count"]))


def _selftest() -> int:
    """判定の固定テスト。2026-07-29 に踏んだ2つのバグを再発させないための番人。"""
    base_days = [(datetime(2026, 7, 29) - timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range(15)]

    def mk(mentions: dict) -> list[dict]:
        rows = [{"date": d, "code": SCANNED_MARK, "name": "", "query_id": "", "count": 0}
                for d in base_days]
        rows += [{"date": d, "code": "9509", "name": "北海道電力", "query_id": "q", "count": c}
                 for d, c in mentions.items()]
        return rows

    cases = [
        ({"2026-07-29": 1}, "ok", "ゼロ続きの後の1件では鳴らない（鳴りすぎ防止）"),
        ({"2026-07-29": 3}, "sigma0_jump", "ゼロ続きの後の3件では鳴る"),
        ({"2026-07-15": 2, "2026-07-20": 1, "2026-07-25": 3, "2026-07-29": 20}, "alert",
         "たまにしか言及されない銘柄の急増を捕まえる（見逃しバグの再発防止）"),
        ({d: 2 for d in base_days}, "ok", "平常運転では鳴らない"),
    ]
    ng = 0
    for mentions, want, why in cases:
        got = [r for r in judge(mk(mentions), "2026-07-29") if r["code"] == "9509"]
        v = got[0]["verdict"] if got else "(なし)"
        ok = v == want
        ng += not ok
        print(f"  {'OK ' if ok else 'NG '} {why}\n       期待={want} 実際={v}")
    return ng


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="判定ロジックの固定テスト")
    ap.add_argument("--date", help="対象日 YYYY-MM-DD（省略時=texts/ の最新日）")
    ap.add_argument("--rebuild", action="store_true", help="texts/ の全日を作り直す")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    if args.selftest:
        print("=== 判定の自己テスト ===")
        ng = _selftest()
        print("\nOK: 全通過" if not ng else f"\nNG: {ng} 件失敗")
        return 1 if ng else 0

    table = xmd.build_dict()
    matcher = xmd.build_matcher(table)
    codes = {c for c, _ in table.values()}

    days = sorted(p.stem for p in TEXTS_DIR.glob("*.jsonl")) if TEXTS_DIR.exists() else []
    if not days:
        print(f"本文がまだありません: {TEXTS_DIR}")
        print("（日次収集 price_watch_collect.py を1回でも回すと作られます）")
        return 1
    targets = days if args.rebuild else [args.date or days[-1]]

    for day in targets:
        rows = extract_day(day, matcher, codes)
        n = write_day(rows, day)
        scanned = rows[0]["posts_scanned"] if rows else 0
        print(f"[{day}] 本文 {scanned} 件 → 言及 {n} 行 / {len({r['code'] for r in rows})} 銘柄")

    target = targets[-1]
    all_rows = load_mentions()
    res = judge(all_rows, target)
    print(f"\n=== 銘柄言及の判定 {target}（baseline {BASELINE_DAYS}日・z>={Z_THRESHOLD}）===")
    hdr = f"{'code':<7}{'銘柄':<22}{'言及':>5}{'平均':>7}{'z':>7}  verdict"
    print(hdr)
    print("-" * 62)
    for r in res[:args.top]:
        z = "-" if r["z"] is None else f"{r['z']:.2f}"
        mu = r.get("baseline_mean", "-")
        print(f"{r['code']:<7}{r['name'][:20]:<22}{r['count']:>5}{str(mu):>7}{z:>7}  {r['verdict']}")
    alerts = [r for r in res if r["verdict"] in ("alert", "sigma0_jump")]
    if alerts:
        names = ", ".join(f"{r['code']}{r['name']}" for r in alerts)
        print(f"\n🚨 言及が急増した銘柄 {len(alerts)}社: {names}")
    else:
        print("\nアラートなし")
    return 0


if __name__ == "__main__":
    sys.exit(main())
