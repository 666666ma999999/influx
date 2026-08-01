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
ALERTS_OUT = APP / "data/x_price_watch/mention_alerts.jsonl"   # 発火の証拠台帳（完了条件の判定用）
BARS_DIR = APP / "data/jquants/bars"
TOPIX_PATH = APP / "data/jquants/topix.json.gz"
JST = timezone(timedelta(hours=9))

SCANNED_MARK = "__scanned__"   # その日を処理した印（言及ゼロの日を台帳に残すため）

BASELINE_DAYS = 14
MIN_BASELINE_DAYS = 7
Z_THRESHOLD = 3.0
# 1投稿にこれ以上の銘柄が並んだら「銘柄羅列まとめ」（値上がりランキング転載・注目銘柄リスト等）
# とみなし、言及として数えない。2026-07-30 実データで、1本のまとめ投稿がデンソー・日産・SUBARU等
# 5〜6銘柄に1件ずつ薄く計上されるノイズを確認したための対策。品薄・値上がりの体験談で
# 4銘柄以上を並べて語る投稿は実データ上ほぼ存在しない。
MAX_CODES_PER_POST = 3
# 言及アラートの固定窓評価（営業日）。「株価が連動して上がっている」の確認を
# 発火時の一回きりでなく、決まった時点で機械的に再測定する（ユーザー完了条件の判定を固定化）。
EVAL_WINDOWS_BD = {"w5": 5, "w20": 20}
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
        ms = xmd.find_mentions(r.get("text", ""), matcher, codes)
        if len({m["code"] for m in ms}) > MAX_CODES_PER_POST:
            continue   # 銘柄羅列まとめ投稿（ランキング転載等）は言及として数えない
        for m in ms:
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

    # 羅列ガードの検証（extract_day 相当の入口ロジックを直接試す）
    import x_mention_dict as xmd
    table = xmd.build_dict(); matcher = xmd.build_matcher(table)
    codes = {c for c, _ in table.values()}
    listicle = "本日値上がり トヨタ自動車 ソニーグループ 三菱重工業 任天堂 フジクラ"
    normal = "メモリ品薄でキオクシアが上昇、ディスコも高い"
    n_list = len({m["code"] for m in xmd.find_mentions(listicle, matcher, codes)})
    n_norm = len({m["code"] for m in xmd.find_mentions(normal, matcher, codes)})
    ok_guard = n_list > MAX_CODES_PER_POST and n_norm <= MAX_CODES_PER_POST
    print(f"  {'OK ' if ok_guard else 'NG '} 羅列ガード: 羅列投稿={n_list}銘柄(>{MAX_CODES_PER_POST}で除外) "
          f"/ 通常投稿={n_norm}銘柄(採用)")
    ng0 = 0 if ok_guard else 1

    cases = [
        ({"2026-07-29": 1}, "ok", "ゼロ続きの後の1件では鳴らない（鳴りすぎ防止）"),
        ({"2026-07-29": 3}, "sigma0_jump", "ゼロ続きの後の3件では鳴る"),
        ({"2026-07-15": 2, "2026-07-20": 1, "2026-07-25": 3, "2026-07-29": 20}, "alert",
         "たまにしか言及されない銘柄の急増を捕まえる（見逃しバグの再発防止）"),
        ({d: 2 for d in base_days}, "ok", "平常運転では鳴らない"),
    ]
    ng = ng0
    for mentions, want, why in cases:
        got = [r for r in judge(mk(mentions), "2026-07-29") if r["code"] == "9509"]
        v = got[0]["verdict"] if got else "(なし)"
        ok = v == want
        ng += not ok
        print(f"  {'OK ' if ok else 'NG '} {why}\n       期待={want} 実際={v}")
    return ng


def price_linkage(code: str, mention_date: str, horizon: int = 6) -> dict | None:
    """言及日からの株価連動を測る（ユーザー完了条件 2026-07-30 の「連動して上がっている」の実測）。

    基準日 = 言及日以前の直近営業日の終値。そこから直近の営業日までの
    株価上昇率と、同期間の TOPIX 上昇率・超過(pt) を返す。データが無ければ None。
    J-Quants のコードは5桁（4桁 + 末尾0）なので変換して引く。
    """
    import gzip
    import glob as _glob
    code5 = code + "0"
    files = sorted(_glob.glob(str(BARS_DIR / "*.json.gz")))
    if not files or not TOPIX_PATH.exists():
        return None
    day8 = mention_date.replace("-", "")
    base_files = [f for f in files if Path(f).name[:8] <= day8]
    if not base_files:
        return None
    after = [base_files[-1]] + [f for f in files if Path(f).name[:8] > day8][:horizon]

    def close_of(f: str) -> float | None:
        with gzip.open(f, "rt") as fh:
            for r in json.load(fh)["data"]:
                if r["Code"] == code5:
                    return r.get("AdjC")
        return None

    p0, p1 = close_of(after[0]), close_of(after[-1])
    if not p0 or not p1 or len(after) < 2:
        return None
    with gzip.open(TOPIX_PATH, "rt") as fh:
        tp = {r["Date"].replace("-", ""): r["C"] for r in json.load(fh)["data"]}
    d0, d1 = Path(after[0]).name[:8], Path(after[-1]).name[:8]
    if d0 not in tp or d1 not in tp:
        return None
    stock = (p1 / p0 - 1) * 100
    topix = (tp[d1] / tp[d0] - 1) * 100
    return {"base_date": d0, "to_date": d1, "days": len(after) - 1,
            "stock_pct": round(stock, 2), "topix_pct": round(topix, 2),
            "excess_pt": round(stock - topix, 2)}


def evaluate_alerts() -> int:
    """mention_alerts.jsonl の発火を固定窓（+5/+20営業日）で再測定する（冪等）。

    商品レーンの前向き記録（price_watch_forward.py・n>=100の事前登録検定）には**混ぜない**。
    レーンが違う発火を同じ台帳に入れると検定の分母が汚れるため、言及レーンは自前の
    evaluation 行を同じ mention_alerts.jsonl に追記する（type で区別・(date,code,window) で冪等）。
    ユーザー完了条件「株価が連動して上がっている」の確認を、発火時の一回きりでなく
    決まった時点の再測定として固定化するのが目的。
    """
    import glob as _glob
    if not ALERTS_OUT.exists():
        print("[eval] 発火がまだありません")
        return 0
    rows = [json.loads(l) for l in ALERTS_OUT.read_text(encoding="utf-8").splitlines()
            if l.strip()]
    firings = [r for r in rows if r.get("verdict") in ("alert", "sigma0_jump")
               and "window" not in r]
    done = {(r["date"], r["code"], r["window"]) for r in rows if r.get("type") == "evaluation"}
    bfiles = sorted(_glob.glob(str(BARS_DIR / "*.json.gz")))
    bdays = [Path(f).name[:8] for f in bfiles]
    if not firings or not bdays:
        print(f"[eval] 評価対象なし（発火 {len(firings)} 件）")
        return 0

    import gzip

    def close_of(code5: str, day8: str) -> float | None:
        f = str(BARS_DIR / f"{day8}.json.gz")
        try:
            with gzip.open(f, "rt") as fh:
                for r in json.load(fh)["data"]:
                    if r["Code"] == code5:
                        return r.get("AdjC")
        except OSError:
            return None
        return None

    with gzip.open(TOPIX_PATH, "rt") as fh:
        tp = {r["Date"].replace("-", ""): r["C"] for r in json.load(fh)["data"]}

    n_new = 0
    with ALERTS_OUT.open("a", encoding="utf-8") as fh:
        for f0 in firings:
            day8 = f0["date"].replace("-", "")
            base_cands = [d for d in bdays if d <= day8]
            if not base_cands:
                continue
            base = base_cands[-1]
            bidx = bdays.index(base)
            for win, n_bd in EVAL_WINDOWS_BD.items():
                if (f0["date"], f0["code"], win) in done:
                    continue
                if bidx + n_bd >= len(bdays):
                    continue   # 期日未到来
                ev_day = bdays[bidx + n_bd]
                p0, p1 = close_of(f0["code"] + "0", base), close_of(f0["code"] + "0", ev_day)
                if not p0 or not p1 or base not in tp or ev_day not in tp:
                    continue
                stock = (p1 / p0 - 1) * 100
                topix = (tp[ev_day] / tp[base] - 1) * 100
                rec = {"type": "evaluation", "date": f0["date"], "code": f0["code"],
                       "name": f0.get("name", ""), "window": win, "base_day": base,
                       "eval_day": ev_day, "stock_pct": round(stock, 2),
                       "topix_pct": round(topix, 2), "excess_pt": round(stock - topix, 2),
                       "goal_confirmed": bool(stock > 0
                                              and not (f0.get("trade500") or {}).get(
                                                  "in_beneficiaries", False)),
                       "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                done.add((f0["date"], f0["code"], win))
                n_new += 1
                mark = " 🎯連動確認" if rec["goal_confirmed"] else ""
                print(f"[eval] {f0['date']} {f0['code']}{f0.get('name','')} {win}: "
                      f"株価{stock:+.1f}% TOPIX{topix:+.1f}% 超過{stock - topix:+.1f}pt{mark}")
    if n_new == 0:
        print(f"[eval] 新規評価なし（発火 {len(firings)} 件・期日未到来か評価済み）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="判定ロジックの固定テスト")
    ap.add_argument("--eval", action="store_true",
                    help="発火の固定窓評価（+5/+20営業日・冪等）のみ実行")
    ap.add_argument("--date", help="対象日 YYYY-MM-DD（省略時=texts/ の最新日）")
    ap.add_argument("--rebuild", action="store_true", help="texts/ の全日を作り直す")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    if args.selftest:
        print("=== 判定の自己テスト ===")
        ng = _selftest()
        print("\nOK: 全通過" if not ng else f"\nNG: {ng} 件失敗")
        return 1 if ng else 0

    if getattr(args, "eval"):
        return evaluate_alerts()

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
        # 完了条件（ユーザー定義 2026-07-30）: 「今リスト（受益カード）に入っていない
        # 取引高TOP500の銘柄で、品薄・値上がりの言及を拾い、株価が連動して上がっている」
        # → 発火ごとに ①リスト内外・取引高順位 ②対TOPIXの連動 を機械で付け、証拠台帳へ残す
        origin = xmd.universe_origin()
        run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"\n🚨 言及が急増した銘柄 {len(alerts)}社:")
        ALERTS_OUT.parent.mkdir(parents=True, exist_ok=True)
        with ALERTS_OUT.open("a", encoding="utf-8") as fh:
            for r in alerts:
                o = origin.get(r["code"], {})
                pl = price_linkage(r["code"], target)
                in_list = o.get("in_beneficiaries", False)
                tags = []
                if o:
                    tags.append(f"取引高{o['rank']}位")
                tags.append("リスト内(受益カード有)" if in_list else "リスト外")
                link = "株価データなし"
                if pl:
                    link = (f"{pl['base_date']}→{pl['to_date']}({pl['days']}営業日) "
                            f"株価{pl['stock_pct']:+.1f}% / TOPIX{pl['topix_pct']:+.1f}% "
                            f"/ 超過{pl['excess_pt']:+.1f}pt")
                print(f"  {r['code']} {r['name']}  [{'/'.join(tags)}]")
                print(f"     連動: {link}")
                if not in_list and pl and pl["stock_pct"] > 0:
                    print("     🎯 完了条件の候補: リスト外銘柄の言及急増 ＋ 株価上昇。"
                          "本文を目視で確認してください")
                fh.write(json.dumps({"date": target, "code": r["code"], "name": r["name"],
                                     "count": r["count"], "z": r["z"], "verdict": r["verdict"],
                                     "trade500": o or None, "price_linkage": pl,
                                     "goal_candidate": bool(not in_list and pl
                                                           and pl["stock_pct"] > 0),
                                     "run_at": run_at}, ensure_ascii=False) + "\n")
    else:
        print("\nアラートなし")
    return 0


if __name__ == "__main__":
    sys.exit(main())
