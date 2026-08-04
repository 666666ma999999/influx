"""price_watch: 日次件数台帳から急増（zスコア）アラートを判定する.

data/x_price_watch/ledger.jsonl の直近 baseline_days（既定14日・status==ok かつ
censored==false の日のみ）を query_id ごとのベースラインとし、対象日の件数の
zスコア >= z_threshold（既定3.0）でアラートを出す。

規約:
- ベースラインの仕様は「**直近 baseline_days 暦日ぶんのエントリのうち clean 行のみ**」
  （clean = status==ok かつ censored==false かつ query_sha が対象日と同一）。
  欠測や打ち切りがあると実効ベースラインは baseline_days より少なくなる（仕様として宣言）。
- 同日に複数回実行された日は「最後の clean 行」を優先採用（無ければ最終行）。
- ベースライン日数 < min_baseline_days（既定7）はウォームアップ扱い＝判定スキップ。
- σ=0（毎日同数）はゼロ除算せず、count > mean のときのみ sigma0_jump として警告扱い。
- 同一 (date, query_id) のアラートは重複追記しない（再実行しても通知が増えない）。
- **受益銘柄は configs/x_shortage_map.json 経由でのみ付ける**（2026-07-28 追加）。
  台帳 data/center_pin/center_pin.jsonl（TOP1000）に実在するコードだけが出る＝関門B。
  転売プレ値・供給断絶・主題不明のクエリは銘柄を出さず「なぜ出さないか」を表示する。
  対応表が壊れている（検証エラー）ときは銘柄を一切出さない＝誤帰属より沈黙を選ぶ。
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
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, pstdev

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent

# 検出器の版。前向き記録に刻み、ルール変更をまたいだ発火を同じ検定に混ぜない。
#   v1 = 単日 z>=3.0（〜2026-08-03。較正で陽性3/4・陰性3/4＝判別力ほぼ無しと判明）
#   v2 = 波（直近28日平均/前28日平均>=2.0）かつ当日>=10件（陽性4/4・陰性0/4）
RULE_VERSION = "v2-wave28x2"

DEFAULT_CONFIG = APP / "configs/x_price_watch.json"
DEFAULT_LEDGER = APP / "data/x_price_watch/ledger.jsonl"
DEFAULT_OUT_DIR = APP / "output/price_watch"


def load_shortage_map() -> tuple[dict | None, str]:
    """品薄→受益銘柄の対応表を読み、自己検証を通ったときだけ返す。

    検証エラー時は None を返して銘柄付与を止める（誤った銘柄を出すより沈黙を選ぶ）。
    対応表が無い環境でもアラート判定自体は動く（銘柄欄が「対応表なし」になるだけ）。
    """
    try:
        sys.path.insert(0, str(APP / "scripts"))
        import x_shortage_map as xsm
        m = xsm.load()
        errors = xsm.validate(m)
        if errors:
            return None, f"対応表に検証エラー{len(errors)}件（銘柄付与を停止）: {errors[0][:60]}"
        return m, ""
    except Exception as exc:  # noqa: BLE001
        # 例外の型まで出す。全部「読めません」に畳むと、一時I/O障害と実装バグを
        # 切り分けられない（Codex NIT-8）
        return None, f"対応表を読めません [{type(exc).__name__}]: {str(exc)[:80]}"


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


def wave_state(by_date: dict[str, dict], target_date: str, is_clean, cfg: dict) -> dict:
    """「投稿の波」＝直近 wave_days 日の平均が、その前 wave_days 日の平均の何倍かを返す。

    なぜ単日 z でなく波なのか（2026-08-04 実装・較正は backfill 実データ）:
    後ろ向き検証で成立した型は「投稿が数週間かけて3〜10倍に膨らんだ後、株の2段目が来る」
    （§16h/§16i）。ところが検出器は単日の z を見ており**型と検出器が不一致**だった。
    実際、成立4カテゴリ（銅・レアメタル・HDD・メモリ）と非成立4カテゴリ（金・原油・ポケカ・
    中古車）の過去日次件数で較正すると:
        単日 z>=3          → 陽性3/4 ・ **陰性3/4**（金もポケカも原油も鳴る＝判別力ほぼ無し）
        単日 z + 最小件数   → 陽性3/4 ・ 陰性3/4（変わらず）
        14日内に z>=3 が2回 → 陽性2/4 ・ 陰性2/4（改善せず）
        **28日平均 / 前28日平均 >= 2.0** → **陽性4/4 ・ 陰性0/4**（完全分離）
    しかも発火日が実用的で、レアメタルは 2026-01-14 に立つ（後ろ向き検証で「1/15買い→
    4戦4勝・平均超過+30.8pt」だった日と一致）。倍率1.75〜2.0・窓21/28/35日のどれでも
    同じ 4/4 vs 0/4 になる**広い安全域**があり、1点狙いの過適合ではない（1.5では陰性2/4が誤発火）。

    Returns: {"wave_ratio": float|None, "n_cur": int, "n_prev": int}
    （履歴不足なら wave_ratio=None ＝ 発火させない）
    """
    win = cfg.get("wave_days", 28)
    t = datetime.strptime(target_date, "%Y-%m-%d")
    cur, prev = [], []
    for d, row in by_date.items():
        if not is_clean(row):
            continue
        age = (t - datetime.strptime(d, "%Y-%m-%d")).days
        if 0 <= age < win:
            cur.append(row["count"])
        elif win <= age < win * 2:
            prev.append(row["count"])
    need = cfg.get("wave_min_samples", 3)
    # 両窓の標本数が釣り合っていないと平均の比が意味を持たない。
    # 例: 収集開始直後は「直近28日=28標本 / 前28日=3標本」になり、3日ぶんの平均と
    # 28日ぶんの平均を比べることになる。実測でこの状態は 2026-08-24〜08-29 に発生する。
    # 較正データ（2〜4日おきの疎な標本）では cur≈prev なので 0.3 でも陽性4/4・陰性0/4・
    # 発火日まで完全に不変。0.5 まで上げると銅を取り逃す（実測）ので 0.3 を採る。
    bal = cfg.get("wave_balance", 0.3)
    if len(cur) < need or len(prev) < need or mean(prev) <= 0 or len(prev) < len(cur) * bal:
        return {"wave_ratio": None, "n_cur": len(cur), "n_prev": len(prev)}
    return {"wave_ratio": round(mean(cur) / mean(prev), 2),
            "n_cur": len(cur), "n_prev": len(prev)}


def judge_query(history: list[dict], target_date: str, alert_cfg: dict) -> dict:
    """1クエリ分の履歴（date昇順・同日複数は最終行採用）から対象日を判定する。

    発火の条件（2026-08-04 改定）:
      alert = **波条件**（wave_ratio >= wave_ratio 既定2.0）かつ 当日 count >= min_abs_count
      watch = 単日 z>=閾値（または σ=0跳び）だが波が未成立/履歴不足 ＝ **候補どまり・発火しない**
    単日 z は候補の目印としてのみ残す。低λクエリ（平時1〜2件）では Poisson の離散性だけで
    z>=3 に届き、実測で年62件規模の誤発火が見込まれたため、最小件数の床も併用する。

    Returns:
        {"verdict": alert|watch|ok|warmup|no_data|not_clean|sigma0_jump, "z", "count",
         "baseline_mean", "baseline_std", "n_baseline", "wave_ratio", "n_wave_cur", "n_wave_prev"}
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
        and by_date[d].get("query_sha") == target.get("query_sha")  # クエリ凍結の実効化（クエリ単位）
    ]
    wave = wave_state(by_date, target_date, is_clean, alert_cfg)
    result = {"count": target["count"], "n_baseline": len(baseline),
              "baseline_mean": round(mean(baseline), 2) if baseline else None,
              "baseline_std": round(pstdev(baseline), 2) if baseline else None, "z": None,
              "wave_ratio": wave["wave_ratio"], "n_wave_cur": wave["n_cur"],
              "n_wave_prev": wave["n_prev"]}
    if len(baseline) < alert_cfg["min_baseline_days"]:
        return {**result, "verdict": "warmup"}

    min_abs = alert_cfg.get("min_abs_count", 10)
    wave_ok = (wave["wave_ratio"] is not None
               and wave["wave_ratio"] >= alert_cfg.get("wave_ratio", 2.0))

    mu, sigma = mean(baseline), pstdev(baseline)
    if sigma == 0:
        spike = target["count"] > mu
    else:
        z = (target["count"] - mu) / sigma
        result["z"] = round(z, 2)
        spike = z >= alert_cfg["z_threshold"]

    # 波が立っていれば発火（単日 z は不要＝ゆっくり膨らむ本物の波を取り逃がさない）。
    # 波が未成立なら、単日の跳びは候補（watch）どまりで台帳にも通知にも出さない。
    if wave_ok and target["count"] >= min_abs:
        return {**result, "verdict": "alert"}
    if spike and target["count"] >= min_abs:
        return {**result, "verdict": "sigma0_jump" if sigma == 0 else "watch"}
    return {**result, "verdict": "ok"}


def selftest() -> int:
    alert_cfg = {"baseline_days": 14, "min_baseline_days": 7, "z_threshold": 3.0,
                 "wave_days": 28, "wave_ratio": 2.0, "wave_min_samples": 3, "min_abs_count": 10}
    days = [f"2026-07-{d:02d}" for d in range(1, 16)]

    def rows(query_id, counts, **extra):
        return [{"date": d, "query_id": query_id, "count": c, "status": "ok",
                 "censored": False, **extra} for d, c in zip(days, counts)]

    spike = rows("q-spike", [10, 11, 9, 10, 12, 10, 11, 9, 10, 11, 10, 9, 11, 10, 50])
    flat = rows("q-flat", [10, 11, 9, 10, 12, 10, 11, 9, 10, 11, 10, 9, 11, 10, 11])
    warm = rows("q-warm", [5, 6, 5, 4, 5])  # 5日分しかない
    censored_target = rows("q-cens", [10] * 14 + [80])
    censored_target[-1]["censored"] = True

    # --- 波条件の検証（2026-08-04 追加）。56日ぶんの履歴を作る ---
    long_days = [(datetime(2026, 5, 1) + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(60)]

    def long_rows(qid, counts):
        return [{"date": d, "query_id": qid, "count": c, "status": "ok", "censored": False}
                for d, c in zip(long_days, counts)]

    # 平時5件が最後の28日で15件へ＝3倍の波（単日zは立たなくても発火すべき）
    wave_up = long_rows("q-wave", [5] * 32 + [15] * 28)
    # 平時5件前後のまま最終日だけ50件＝単日スパイク（波は無い＝候補どまり）
    spike_only = long_rows("q-spike2", [5, 6, 4, 5, 7, 4] * 9 + [5] * 5 + [50])
    # 波は立っているが当日が9件＝最小件数10未満（低λの Poisson ノイズ対策）
    wave_small = long_rows("q-small", [2] * 32 + [6] * 27 + [9])
    # 波が2.0倍に届かない（1.6倍）＝発火しない
    wave_weak = long_rows("q-weak", [10] * 32 + [16] * 28)

    checks = [
        ("波3倍→alert（単日zに依らず発火）",
         judge_query(wave_up, long_days[-1], alert_cfg)["verdict"], "alert"),
        ("単日スパイクのみ→watch（発火させない）",
         judge_query(spike_only, long_days[-1], alert_cfg)["verdict"], "watch"),
        ("波ありでも当日9件→ok（最小件数10の床）",
         judge_query(wave_small, long_days[-1], alert_cfg)["verdict"], "ok"),
        ("波1.6倍→ok（閾値2.0に届かない）",
         judge_query(wave_weak, long_days[-1], alert_cfg)["verdict"], "ok"),
        ("履歴不足で波が測れない→発火しない",
         judge_query(spike, days[-1], alert_cfg)["wave_ratio"], None),
        ("spike→watch（旧alertから降格）", judge_query(spike, days[-1], alert_cfg)["verdict"], "watch"),
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
    print(f"selftest PASS ({len(checks)}/{len(checks)})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--date", help="判定対象日 YYYY-MM-DD（省略時=台帳の最新日）")
    parser.add_argument("--selftest", action="store_true")
    # 前向き記録は仮説検定の証拠台帳（n>=100で初めて結論を出す）。合成データや
    # 別台帳での検証実行が本番台帳へ混入すると検定が壊れるため、--ledger を既定以外に
    # した実行は既定で記録しない（2026-07-28: 合成テストが実際に3行汚染した事故の恒久対策）。
    parser.add_argument("--forward", choices=["auto", "on", "off"], default="auto",
                        help="前向き記録への追記。auto=既定台帳のときだけ記録（既定）")
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
    # 日付判定は全部文字列比較なので、不正な --date は「発火して銘柄を出す瞬間」まで
    # 生き延びてから落ちる。入口で弾く（Codex PLAUSIBLE-6）。
    try:
        datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        print(f"--date が不正です（YYYY-MM-DD で指定）: {target_date!r}")
        return 1

    by_query: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_query[row["query_id"]].append(row)

    alerts: list[dict] = []
    watches: list[dict] = []
    print(f"=== price_watch 判定 {target_date}（発火=直近{alert_cfg.get('wave_days', 28)}日平均が"
          f"前{alert_cfg.get('wave_days', 28)}日の{alert_cfg.get('wave_ratio', 2.0)}倍以上"
          f"かつ{alert_cfg.get('min_abs_count', 10)}件以上／単日z>={alert_cfg['z_threshold']}は候補）===")
    hdr = f"{'query_id':<24}{'count':>7}{'mean':>8}{'std':>7}{'z':>7}{'wave':>8}  verdict"
    print(hdr)
    print("-" * len(hdr))
    for entry in config["queries"]:
        qid = entry["id"]
        res = judge_query(by_query.get(qid, []), target_date, alert_cfg)
        z_txt = "-" if res["z"] is None else f"{res['z']:.2f}"
        cnt = "-" if res["count"] is None else res["count"]
        mu = "-" if res["baseline_mean"] is None else res["baseline_mean"]
        sd = "-" if res["baseline_std"] is None else res["baseline_std"]
        wr = "-" if res.get("wave_ratio") is None else f"{res['wave_ratio']:.2f}x"
        print(f"{qid:<24}{cnt:>7}{mu:>8}{sd:>7}{z_txt:>7}{wr:>8}  {res['verdict']}")
        if res["verdict"] in ("watch", "sigma0_jump"):
            # 候補は表示だけ。台帳にも通知にも出さない（誤発火予算を守る・2026-08-04）
            watches.append({"query_id": qid, **res})
        if res["verdict"] == "alert":
            latest = max(
                (r for r in by_query[qid] if r["date"] == target_date),
                key=lambda r: r.get("run_at", ""),
            )
            alerts.append({"date": target_date, "query_id": qid, "lane": entry.get("lane", ""),
                           "q": entry["q"], **res, "samples": latest.get("samples", [])})

    # 受益銘柄の付与（対応表が健全なときのみ・関門B=TOP1000台帳内に限る）
    smap, smap_err = load_shortage_map()
    if smap_err:
        print(f"\n[shortage_map] WARN: {smap_err}")
    if alerts and smap is None:
        # 銘柄欠落のまま台帳へ書くと、(date,query_id)の重複排除で二度と補完されず
        # 「本来出るはずの銘柄が恒久的に消えた発火」が証拠台帳に固定される（Codex CONFIRMED-4）。
        # アラート自体は日次台帳から決定的に再計算できるので、書かずに落として再実行を促す方が安全。
        print("\nFATAL: 対応表が使えないため、銘柄なしの発火を記録しません"
              "（重複排除により後から補完できないため）。原因を直して再実行してください。")
        print(f"  検知していた発火: {[a['query_id'] for a in alerts]}")
        return 2
    if alerts and smap is not None:
        import x_shortage_map as xsm
        for a in alerts:
            a["stocks"] = [{"code": c["code"], "name": c.get("name", ""),
                            "tier": c["tier"], "layer": c["layer"],
                            "subject": c["subject"], "shortage_type": c["shortage_type"]}
                           for c in xsm.cards_for_query(smap, a["query_id"])]
            a["stocks_display"] = xsm.display_for_query(smap, a["query_id"], target_date)
            a["subjects"] = [s["id"] for s in xsm.subjects_for_query(smap, a["query_id"])]

    if alerts:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        out = args.out_dir / f"alerts-{target_date}.jsonl"
        seen = {(r.get("date"), r.get("query_id")) for r in load_ledger(out)}
        new_alerts = [a for a in alerts if (a["date"], a["query_id"]) not in seen]
        with out.open("a", encoding="utf-8") as fh:
            for a in new_alerts:
                fh.write(json.dumps(a, ensure_ascii=False) + "\n")
        print(f"\n🚨 アラート {len(alerts)} 件（新規追記 {len(new_alerts)} 件）→ {out}")
        for a in alerts:
            print(f"  {a['query_id']:<24} → {a.get('stocks_display', '対応表なし')}")
        # 前向き記録（受益銘柄つき。銘柄が出ない分類は理由を note に残す）
        # 字面比較だと `./data/...` や symlink 経由の既定台帳を「別台帳」と誤判定して
        # 記録を取りこぼす（Codex PLAUSIBLE-7）。実体パスで比較する。
        def _same_file(a: Path, b: Path) -> bool:
            try:
                return a.resolve() == b.resolve()
            except OSError:
                return a == b
        do_forward = (args.forward == "on"
                      or (args.forward == "auto" and _same_file(args.ledger, DEFAULT_LEDGER)))
        if not do_forward:
            print(f"[forward] SKIP: 前向き記録に追記しません"
                  f"（--forward={args.forward} / ledger={args.ledger.name}）")
        else:
            try:
                sys.path.insert(0, str(APP / "scripts"))
                import price_watch_forward as fwd
                for a in new_alerts:
                    stocks = a.get("stocks", [])
                    fwd.append({"type": "x_firing", "spec_version": fwd.SPEC_VERSION,
                                # 検出器の版。v1=単日z>=3（〜2026-08-03）/ v2=波28日2倍＋最小件数
                                # （2026-08-04〜）。ルールが変わった前後の発火を同じ検定に
                                # 混ぜないための分離キー（v1の発火は v1 として据え置き・消さない）
                                "rule_version": RULE_VERSION,
                                "fire_date": a["date"], "query_id": a["query_id"],
                                "lane": a.get("lane", ""), "count": a.get("count"),
                                "z": a.get("z"), "wave_ratio": a.get("wave_ratio"),
                                "verdict": a.get("verdict"),
                                "subjects": a.get("subjects", []),
                                "stocks": [{"code": s["code"], "tier": s["tier"]}
                                           for s in stocks],
                                "note": a.get("stocks_display", "対応表なし")
                                if not stocks else f"受益{len(stocks)}社"})
            except Exception as exc:  # noqa: BLE001
                print(f"[forward] WARN: X発火の記録に失敗: {str(exc)[:80]}")
    else:
        print("\nアラートなし")
    if watches:
        # 候補は「まだ波になっていない単日の跳び」。台帳・通知には出さないが、
        # 波に育つかを追うために毎回表示する（履歴が wave_days*2 貯まるまでは全部ここに出る）
        print(f"\n👀 候補 {len(watches)} 件（単日の跳び・波は未成立＝発火せず・台帳にも残さない）")
        for w in sorted(watches, key=lambda x: -(x.get("z") or 0)):
            wr = "履歴不足" if w.get("wave_ratio") is None else f"波{w['wave_ratio']:.2f}倍"
            z_txt = "-" if w.get("z") is None else f"{w['z']:.2f}"
            print(f"  {w['query_id']:<24} {w['count']:>4}件 (平均{w['baseline_mean']}・z={z_txt}・{wr})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
