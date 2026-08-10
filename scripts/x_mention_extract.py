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
from functools import lru_cache
from pathlib import Path
from statistics import mean, pstdev

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "scripts"))

import x_mention_dict as xmd  # noqa: E402

TEXTS_DIR = APP / "data/x_price_watch/texts"
LEDGER = APP / "data/x_price_watch/ledger.jsonl"   # 収集台帳（どの実行がcleanかの正本）
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


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _run_time(run_at: str) -> datetime:
    """run_at を時刻として比較できる形にする。

    文字列 max はタイムゾーン表記の揺れで壊れる（"…T13:00+09:00"(=04:00Z) が
    "…T05:00+00:00" より辞書順で後になる）。解釈できない値は最古扱いにして、
    まともな run_at を持つ実行に必ず負けるようにする。
    """
    try:
        dt = datetime.fromisoformat(str(run_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return _EPOCH
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@lru_cache(maxsize=1)
def run_quality() -> dict[tuple[str, str], int]:
    """収集台帳から (対象日, run_at) → **clean に成功したクエリ数** を作る。

    clean の定義は件数レーン（price_watch_alert.judge）と同一
    （status=="ok" かつ censored でない かつ count が None でない）。
    どの実行がその日の代表かを決めるのに使う＝両レーンで同じ「品質」定義を共有する。
    """
    idx: dict[tuple[str, str], int] = {}
    if not LEDGER.exists():
        return idx
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("status") == "ok" and not r.get("censored") and r.get("count") is not None:
            key = (r.get("date", ""), r.get("run_at", ""))
            idx[key] = idx.get(key, 0) + 1
    return idx


def canonical_run(rows: list[dict], quality: dict[str, int] | None = None
                  ) -> tuple[list[dict], int, str]:
    """同じUTC日を複数回収集していたら、**その日の代表となる1回分**だけを返す。

    texts/<day>.jsonl は収集のたび追記されるため、同じ日を手動で追加収集すると
    全実行の**和集合**が入る。status_id の重複は extract_day の seen が落とすが、
    X検索は毎回違う部分集合を返すので和集合は母集団そのものが膨らみ、
    **その日だけ言及数が水増しされてベースラインが歪む**（実測 2026-07-31: 5回収集で
    本文2007行→言及125件。代表1回だけなら1053行→94件＝+33%の水増し）。
    zスコアは「今日 vs 過去14日」の比較なので、日ごとに収集回数が違うと判定が壊れる。
    取りこぼしより**日間の比較可能性**を優先する。

    代表の選び方＝**網羅クエリ数の多い実行**（同数なら後の実行）。時刻だけで「最後」を
    採ってはいけない: collector は `--queries` で一部クエリだけの実行を正式に許可しており
    （price_watch_collect.py の --queries）、完全実行の後に2クエリだけの手動実行があると
    最後を採る規約では**残り48クエリを丸ごと言及ゼロとして記録**してしまう。実データにも
    2026-07-31 に「42クエリ完全 → 3 → 3 → 2 → 50クエリ完全」の並びが実在する（Codex指摘）。
    網羅数は収集台帳の clean 定義（run_quality）で測り、台帳に対応が無ければ texts 側の
    distinct query_id で代用する。

    run_at を持たない行が1つでもあれば畳まない（古い形式の texts を静かに捨てないため。
    ただし複数実行が混在していれば警告を返す＝黙って和集合に戻らない）。
    戻り値: (採用する行, 捨てた行数, 経緯の1行説明)
    """
    if not rows:
        return rows, 0, ""
    if any(not r.get("run_at") for r in rows):
        n_runs = len({r.get("run_at", "") for r in rows})
        note = ("run_at 欠損行があるため畳まない（古い形式の texts）"
                + ("／⚠️複数実行が混在＝この日は水増しの可能性あり" if n_runs > 1 else ""))
        return rows, 0, note
    by_run: dict[str, list[dict]] = {}
    for r in rows:
        by_run.setdefault(r["run_at"], []).append(r)
    if len(by_run) <= 1:
        return rows, 0, ""

    def coverage(run: str) -> int:
        if quality and run in quality:
            return quality[run]
        return len({r.get("query_id", "") for r in by_run[run]})

    best = max(by_run, key=lambda run: (coverage(run), _run_time(run)))
    others = "／".join(f"{r[11:16]}={coverage(r)}ク" for r in sorted(by_run) if r != best)
    note = (f"{len(by_run)}回の収集を検出 → 網羅{coverage(best)}クエリの回"
            f"(run_at={best})を代表に採用（他: {others}）")
    return by_run[best], len(rows) - len(by_run[best]), note


def extract_day(day: str, matcher, codes: set[str]) -> list[dict]:
    """1日分の本文から (code, query_id) 別の言及数を作る。"""
    path = TEXTS_DIR / f"{day}.jsonl"
    if not path.exists():
        return []
    seen: set[tuple[str, str]] = set()   # (status_id, code) の重複排除（再実行での二重計上防止）
    seen_text: set[tuple[str, str]] = set()  # (本文冒頭60字, code) — bot 対策
    per: Counter = Counter()
    n_posts = 0
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    q_all = run_quality()
    quality = {run_at: n for (d, run_at), n in q_all.items() if d == day}
    rows, dropped, note = canonical_run(rows, quality)
    if note:
        # 何を捨てたか・なぜその回を選んだかを必ず出す（黙って母集団を変えない）
        print(f"[{day}] {note}" + (f"／{dropped}行を判定から除外" if dropped else ""))
    for r in rows:
        n_posts += 1
        ms = xmd.find_mentions(r.get("text", ""), matcher, codes)
        if len({m["code"] for m in ms}) > MAX_CODES_PER_POST:
            continue   # 銘柄羅列まとめ投稿（ランキング転載等）は言及として数えない
        for m in ms:
            key = (r.get("status_id", ""), m["code"])
            if key in seen:
                continue
            seen.add(key)
            # 同一文面の繰り返し（別status_idのbot広告）は1日1回しか数えない。
            # 実例 2026-07-31: 「Amazon直販 ━ 人気過ぎて品薄の商品が入荷 『タカラトミー…』」が
            # 同一文面×3投稿でタカラトミー8件の中身を水増ししていた
            tkey = (" ".join(r.get("text", "").split())[:60], m["code"])
            if tkey in seen_text:
                continue
            seen_text.add(tkey)
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

    # 同日複数回収集の畳み込み（2026-08-03 新設・実測 7/31 の +33% 水増しの再発防止）
    def mk_run(run_at, queries):
        return [{"status_id": f"{run_at}-{q}-{i}", "run_at": run_at, "query_id": q}
                for q in queries for i in range(2)]

    A = "2026-08-01T02:14:00+00:00"   # 完全実行（10クエリ）
    B = "2026-08-01T13:10:00+00:00"   # 後から来た部分実行（2クエリ）
    full_then_partial = mk_run(A, [f"q{i}" for i in range(10)]) + mk_run(B, ["q0", "q1"])
    kept, dropped, _ = canonical_run(full_then_partial)
    # 【Codex CONFIRMED-1】時刻だけで「最後」を採ると、部分実行が完全実行を潰し
    # 残り8クエリが言及ゼロとして記録される。網羅数で選べば完全実行が残る
    ok_c1 = all(r["run_at"] == A for r in kept) and len(kept) == 20 and dropped == 4
    # 網羅が同数なら後の実行を採る
    same_cov = mk_run(A, ["q0", "q1"]) + mk_run(B, ["q0", "q1"])
    ok_c2 = all(r["run_at"] == B for r in canonical_run(same_cov)[0])
    # 台帳の clean 数（quality）が texts の見かけより優先される
    ok_c3 = all(r["run_at"] == B for r in
                canonical_run(full_then_partial, {A: 1, B: 40})[0])
    # タイムゾーン表記が混ざっても時刻として比較する（文字列maxなら誤判定するケース）
    tz = mk_run("2026-08-01T13:00:00+09:00", ["q0"]) + mk_run("2026-08-01T05:00:00+00:00", ["q1"])
    ok_c4 = all(r["run_at"] == "2026-08-01T05:00:00+00:00" for r in canonical_run(tz)[0])
    single = [{"status_id": "a", "run_at": B, "query_id": "q0"}]
    ok_c5 = canonical_run(single)[:2] == (single, 0)
    legacy = [{"status_id": "a"}, {"status_id": "b", "run_at": B}]
    lk, ld, lnote = canonical_run(legacy)
    ok_c6 = (lk, ld) == (legacy, 0) and "⚠️" in lnote   # 畳まないが警告は出す
    ok_c7 = canonical_run([])[:2] == ([], 0)
    for ok, why in ((ok_c1, "部分実行が完全実行を潰さない（網羅数で選ぶ）"),
                    (ok_c2, "網羅同数なら後の実行"),
                    (ok_c3, "台帳のclean数が優先される"),
                    (ok_c4, "TZ表記が混ざっても時刻で比較"),
                    (ok_c5, "単一実行は素通し"),
                    (ok_c6, "run_at欠損混在は畳まず警告（古いtextsを捨てない）"),
                    (ok_c7, "空入力")):
        print(f"  {'OK ' if ok else 'NG '} 同日畳み込み: {why}")
        ng0 += not ok

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

    # 株価突き合わせの基準日 = 実投稿日（JST最大）であることの固定テスト。
    # ラベル日に戻ると look-ahead が復活する（tasks/date_label_offset_audit.md・
    # 実測で既存17アラート全件が +1日ずれていた）
    import tempfile
    global TEXTS_DIR
    saved_texts_dir = TEXTS_DIR
    try:
        with tempfile.TemporaryDirectory() as td:
            TEXTS_DIR = Path(td)
            label = "2026-08-04"
            # UTC 表記で保存され、JST では翌日になる投稿を混ぜる
            recs = [
                # JST 2026-08-04 12:00（ラベル日と同じ）
                {"status_id": "s1", "text": "キオクシアのメモリが品薄", "posted_at": "2026-08-04T03:00:00.000Z"},
                # JST 2026-08-05 07:00（ラベル+1日）＝これが最大になるべき
                {"status_id": "s2", "text": "キオクシアの値上げ観測", "posted_at": "2026-08-04T22:00:00.000Z"},
                # posted_at 欠落・壊れた値は無視されるべき
                {"status_id": "s3", "text": "キオクシア"},
                {"status_id": "s4", "text": "キオクシア", "posted_at": "not-a-date"},
            ]
            (TEXTS_DIR / f"{label}.jsonl").write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in recs), encoding="utf-8")

            got_any = post_date_jst_max(label)
            ok_d1 = got_any == "2026-08-05"
            print(f"  {'OK ' if ok_d1 else 'NG '} 実投稿日=JST最大を採る（銘柄指定なし）"
                  f"\n       期待=2026-08-05 実際={got_any}")
            ng += not ok_d1

            # フォールバック: texts が無い日はラベル日に倒す（従来より甘い側に倒さないため）
            got_fb = effective_base_date("2026-01-01")
            ok_d2 = got_fb == "2026-01-01"
            print(f"  {'OK ' if ok_d2 else 'NG '} texts 不在ならラベル日にフォールバック"
                  f"\n       期待=2026-01-01 実際={got_fb}")
            ng += not ok_d2

            # 銘柄指定: その銘柄に言及していない投稿は基準日に影響しない
            (TEXTS_DIR / "2026-08-06.jsonl").write_text("\n".join([
                json.dumps({"status_id": "t1", "text": "キオクシアが品薄",
                            "posted_at": "2026-08-06T03:00:00.000Z"}, ensure_ascii=False),
                json.dumps({"status_id": "t2", "text": "関係のない雑談",
                            "posted_at": "2026-08-06T22:00:00.000Z"}, ensure_ascii=False),
            ]), encoding="utf-8")
            got_code = post_date_jst_max("2026-08-06", "285A")   # キオクシアHD
            ok_d3 = got_code == "2026-08-06"
            print(f"  {'OK ' if ok_d3 else 'NG '} 銘柄指定時は非言及投稿を基準日に含めない"
                  f"\n       期待=2026-08-06 実際={got_code}")
            ng += not ok_d3
    finally:
        TEXTS_DIR = saved_texts_dir
    return ng


def post_date_jst_max(day: str, code: str | None = None) -> str | None:
    """収集日ラベル `day` の本文のうち、実投稿日（JST）の最大値を返す。

    収集クエリ `since:D until:D+1` は X 側で「ログイン中アカウントのTZ・until 含む」と解釈され、
    実際には JST D 00:00〜D+1 実行時刻の約2日窓になる。実測で **x_price_watch は投稿の 59.8%、
    sedori_trend は 92.2% が「ラベル+1日」**（棚卸し正本: tasks/date_label_offset_audit.md）。
    株価と突き合わせる基準日にラベルを使うと、基準日が実投稿より前に置かれ、測定窓が
    **投稿当日の同時進行の値動きを内包**する（ラベル日の終値では翌日の投稿を知らないと買えない
    ＝look-ahead）。そこで「いちばん遅い投稿の日」を採り、リード時間を保守的に見積もる。

    code 指定時はその銘柄に言及した投稿だけを見る。該当なし・posted_at 皆無なら None。
    """
    path = TEXTS_DIR / f"{day}.jsonl"
    if not path.exists():
        return None
    matcher = codes = None
    if code:
        table = xmd.build_dict()
        matcher = xmd.build_matcher(table)
        codes = {c for c, _ in table.values()}
    best: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(r, dict):
            continue
        stamp = r.get("posted_at")
        if not isinstance(stamp, str) or not stamp:
            continue
        if code:
            ms = xmd.find_mentions(r.get("text", "") or "", matcher, codes)
            if len({m["code"] for m in ms}) > MAX_CODES_PER_POST:
                continue    # 銘柄羅列まとめ投稿は言及として数えない（judge 側と同じ扱い）
            if code not in {m["code"] for m in ms}:
                continue
        try:
            jst = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(JST)
        except ValueError:
            continue
        d = jst.strftime("%Y-%m-%d")
        if best is None or d > best:
            best = d
    return best


def effective_base_date(day: str, code: str | None = None) -> str:
    """株価突き合わせの基準に使う日。実投稿日（JST最大）が取れればそれ、取れなければラベル日。

    フォールバックがラベル日なのは、実時刻が無い古いデータで**従来より甘い判定に倒さない**ため
    （ラベル日基準は実投稿より前＝甘い側なので、取れた時だけ厳しい側へ寄せる）。
    """
    return post_date_jst_max(day, code) or day


def price_linkage(code: str, mention_date: str, horizon: int = 6) -> dict | None:
    """言及日からの株価連動を測る（ユーザー完了条件 2026-07-30 の「連動して上がっている」の実測）。

    基準日 = 言及日以前の直近営業日の終値。そこから直近の営業日までの
    株価上昇率と、同期間の TOPIX 上昇率・超過(pt) を返す。データが無ければ None。
    J-Quants のコードは5桁（4桁 + 末尾0）なので変換して引く。

    ⚠️ `mention_date` には**収集日ラベルではなく実投稿日**（`effective_base_date()` の戻り）を渡す。
    ラベルを渡すと look-ahead が入る（上記 post_date_jst_max の docstring 参照）。
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
            # 基準日は実投稿日（JST最大）。古い行に post_date_jst が無ければ texts から引き直す
            # （ラベル日を使うと基準日が実投稿より前になり look-ahead が入る
            #  ＝ tasks/date_label_offset_audit.md）
            base_date = f0.get("post_date_jst") or effective_base_date(f0["date"], f0["code"])
            day8 = base_date.replace("-", "")
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
                       "name": f0.get("name", ""), "window": win,
                       "post_date_jst": base_date, "base_day": base,
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
    if args.rebuild and ALERTS_OUT.exists():
        # 過去日を作り直しても発火台帳は追記専用で、判定するのは最終日だけ（前向き記録の
        # 不可逆性を守る設計）。よって「再計算で消えた過去の発火」が台帳に残り続けうる。
        # 黙って不整合を作らないため、rebuild 時に必ず知らせる（Codex CONFIRMED-3）
        print(f"⚠️  {ALERTS_OUT.name} が既に存在します。--rebuild は mentions を作り直しますが、"
              "過去の発火記録は再整合しません（前向き記録は追記専用）。\n"
              "    再計算で判定が変わった日がある場合は、発火台帳と突き合わせて手で確認してください。")

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
        # 同じ (対象日, 銘柄) は1回しか記録しない。発火台帳は完了条件の**証拠**であり
        # w5/w20 の固定窓評価もこの行を数えるため、同日再実行（手動の前倒し実行と
        # 22:10 の定時実行が同じ対象日を判定する等）で二重計上されると検定が壊れる。
        # 件数レーン price_watch_alert の (date, query_id) 重複排除と同じ規約
        # （2026-08-03 追加。それ以前は append のみで重複しうる状態だった）
        already = set()
        if ALERTS_OUT.exists():
            for line in ALERTS_OUT.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        prev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    already.add((prev.get("date"), prev.get("code")))
        n_dup = sum(1 for r in alerts if (target, r["code"]) in already)
        if n_dup:
            print(f"  （うち {n_dup} 件は記録済みのため台帳へは追記しない）")
        with ALERTS_OUT.open("a", encoding="utf-8") as fh:
            for r in alerts:
                o = origin.get(r["code"], {})
                # 収集日ラベルではなく実投稿日（JST最大）を基準にする（look-ahead 除去）
                base_date = effective_base_date(target, r["code"])
                pl = price_linkage(r["code"], base_date)
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
                if (target, r["code"]) in already:
                    continue   # 既に記録済み（同日再実行）＝台帳は不変に保つ
                fh.write(json.dumps({"date": target, "code": r["code"], "name": r["name"],
                                     "count": r["count"], "z": r["z"], "verdict": r["verdict"],
                                     # 実投稿日（JST最大）を台帳に残す。後段の固定窓評価が
                                     # 同じ基準日を使えるようにするため（ラベル日と別物）
                                     "post_date_jst": base_date,
                                     "trade500": o or None, "price_linkage": pl,
                                     "goal_candidate": bool(not in_list and pl
                                                           and pl["stock_pct"] > 0),
                                     "run_at": run_at}, ensure_ascii=False) + "\n")
    else:
        print("\nアラートなし")
    return 0


if __name__ == "__main__":
    sys.exit(main())
