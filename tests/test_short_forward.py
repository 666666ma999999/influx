"""short_forward_v1（空売り版の前向き記録）の回帰テスト（unittest 自走・実データに触れない）。

期待値はすべて手計算で独立に置いた値（式を呼んで期待値を作らない）。
合成カレンダー: 2026-10-01 からの平日で、10/12（スポーツの日）と 11/3（文化の日）を休場にした取引所暦。
  idx: 0=1001 1=1002 2=1005 3=1006 ... 21=1102 22=1104 23=1105 24=1106
  合図日 D=1002 → 入口 D+2=1006（寄り）・出口 D+22=1105（引け）・実暦日数 30 日。
実行: python3 -m unittest tests.test_short_forward
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import jq_fetch  # noqa: E402
import measure_base_rate  # noqa: E402
import short_forward_runner as R  # noqa: E402
import short_forward_v1_spec as S  # noqa: E402

HOLIDAYS = {"20261012", "20261103"}


def make_bdays(start="20261001", n=30) -> list[str]:
    d = dt.date(int(start[:4]), int(start[4:6]), int(start[6:]))
    out = []
    while len(out) < n:
        s = d.strftime("%Y%m%d")
        if d.weekday() < 5 and s not in HOLIDAYS:
            out.append(s)
        d += dt.timedelta(days=1)
    return out


BDAYS = make_bdays()
FILLERS = [f"F{i:03d}0" for i in range(20)]
X = "11110"          # 合図を出す銘柄
NOW_EARLY = "2026-10-03T07:30:00+09:00"   # 入口 1006 の寄りより前に検出


def bar(code, o=1000.0, l=None, c=1000.0, fac=1.0):
    return {"Code": code, "AdjO": o, "AdjL": l, "AdjC": c, "AdjFactor": fac, "Va": 1.0}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "bars").mkdir()
        self.p1 = mock.patch.object(jq_fetch, "DATA_ROOT", self.root)
        self.p2 = mock.patch.object(jq_fetch, "LOG_PATH", self.root / "fetch_log.jsonl")
        self.p1.start()
        self.p2.start()
        measure_base_rate.load_bars_day.cache_clear()
        self.ledger = self.root / "ledger.jsonl"
        self.days: dict[str, dict[str, dict]] = {}
        for d in BDAYS:
            self.days[d] = {f: bar(f, 100.0, 100.0, 100.0) for f in FILLERS}
            self.days[d][X] = bar(X)                     # AdjL=None → 合図は出ない
        self.universe = set(FILLERS) | {X}

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()
        measure_base_rate.load_bars_day.cache_clear()
        self.tmp.cleanup()

    def signal_on(self, day, code=X):
        prev = BDAYS[BDAYS.index(day) - 1]
        self.days[prev][code] = bar(code, 1000.0, None, 1000.0)
        self.days[day][code] = bar(code, 1000.0, 880.0, 950.0)   # 安値 880 <= 前日終値1000×0.90

    def write(self, skip=()):
        for d, recs in self.days.items():
            if d in skip:
                continue
            with gzip.open(self.root / "bars" / f"{d}.json.gz", "wt", encoding="utf-8") as f:
                json.dump({"data": list(recs.values())}, f)
        measure_base_rate.load_bars_day.cache_clear()

    def env(self, topix_o=None, topix_c=None, fetch_ts=None):
        to = topix_o or {d: 2000.0 for d in BDAYS}
        tc = topix_c or {d: 2000.0 for d in BDAYS}
        if fetch_ts is None:   # 既定: 各日の bars は翌営業日 07:30 に取得（＝欠けの確定条件を満たす）
            fetch_ts = {d: f"{n[:4]}-{n[4:6]}-{n[6:]}T07:30:00+09:00" for d, n in zip(BDAYS, BDAYS[1:])}
        return R.Env(bdays=BDAYS, universe_fn=lambda d: (self.universe, "20260930", None),
                     topix_o=to, topix_c=tc, bars_fetch_ts=fetch_ts)

    def refetched(self, day, ts="2026-11-20T07:30:00+09:00"):
        """day の bars だけ後日（既定 11/20）に取り直した Env（3回目 (b) の確定条件）。"""
        return self.env(fetch_ts=dict(self.env().bars_fetch_ts, **{day: ts}))

    def run_scan(self, env=None, now=NOW_EARLY, frozen="20261002", **kw):
        return R.scan(env or self.env(), self.ledger, now, frozen_start=frozen, **kw)

    def events(self, name):
        return [r for r in R.load_ledger_lines(self.ledger) if r["event"] == name]


class TestFormulas(unittest.TestCase):
    def test_ret_short_sign_and_borrow_non28(self):
        # 1000 で空売り → 900 で買い戻し・30暦日: +10% − 0.3% − 1.15%×30/365(=0.0009452055)
        self.assertAlmostEqual(S.ret_short(1000, 900, 30), 0.0960547945, places=9)
        # 遡りの固定28日なら 0.0115×28/365 = 0.0008821918 → 0.0961178082（両方の値を固定）
        self.assertAlmostEqual(S.ret_short(1000, 900, 28), 0.0961178082, places=9)
        # 上がったら損: 1000→1100・30日 → −10% −0.3% −0.0009452055
        self.assertAlmostEqual(S.ret_short(1000, 1100, 30), -0.1039452055, places=9)

    def test_ret_pair_adds_topix_move(self):
        # TOPIX 寄り2000 → 引け2100 で +5% を足す
        self.assertAlmostEqual(S.ret_pair(0.0960547945, 2000, 2100), 0.1460547945, places=9)

    def test_calendar_days(self):
        self.assertEqual(S.calendar_days("20261006", "20261105"), 30)

    def test_day_gate_9_vs_10(self):
        self.assertEqual(S.day_gate(0), "zero")
        self.assertEqual(S.day_gate(1), "recorded")
        self.assertEqual(S.day_gate(9), "recorded")
        self.assertEqual(S.day_gate(10), "skipped_day_count")

    def test_frozen_start_constant(self):
        self.assertEqual(S.FROZEN_START, "20261005")   # 2026-10-03(土) の翌営業日


class TestCalendarArithmetic(Base):
    def test_offsets_use_exchange_calendar_even_if_bars_missing(self):
        self.signal_on("20261002")
        self.write(skip={"20261006"})                  # 入口日の bars が未取得
        self.run_scan()
        sig = self.events("signal")
        self.assertEqual(len(sig), 1)
        self.assertEqual(sig[0]["entry_day"], "20261006")  # 先送りしない（D+2 のまま）
        self.assertEqual(sig[0]["exit_day"], "20261105")   # 10/12・11/3 の休場を飛ばした D+22
        pend = self.events("pending_data")
        self.assertEqual([(p["stage"], p["missing_bars"]) for p in pend], [("entry", "20261006")])
        self.assertEqual(self.events("filled"), [])
        # 再実行しても pending_data は増えない
        self.run_scan()
        self.assertEqual(len(self.events("pending_data")), 1)
        # bars が来たら filled → matured
        self.write()
        self.run_scan()
        self.assertEqual(len(self.events("filled")), 1)
        self.assertEqual(len(self.events("matured")), 1)


class TestEndToEnd(Base):
    def test_matured_returns_hand_computed(self):
        self.signal_on("20261002")
        self.days["20261105"][X] = bar(X, 920.0, None, 900.0)
        self.write()
        to = {d: 2000.0 for d in BDAYS}
        tc = {d: 2000.0 for d in BDAYS}
        tc["20261105"] = 2100.0
        res = self.run_scan(env=self.env(to, tc))
        m = self.events("matured")
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]["caldays"], 30)
        self.assertAlmostEqual(m[0]["ret_short"], 0.0960547945, places=9)
        self.assertAlmostEqual(m[0]["ret_pair"], 0.1460547945, places=9)
        day = [r for r in self.events("day") if r["D"] == "20261002"][0]
        self.assertEqual((day["n_top500"], day["gate"], day["codes"]), (1, "recorded", [X]))
        self.assertIn("code_tree_sha", day)
        self.assertIn("detected_at", day)
        # 発火0件の日も day 事象が残る（1001 は前日 bars が無いので対象外・1005 以降は0件）
        zero = [r for r in self.events("day") if r["D"] == "20261005"]
        self.assertEqual((zero[0]["n_top500"], zero[0]["gate"]), (0, "zero"))
        self.assertEqual(self.events("signal")[0]["late_detected"], False)
        self.assertTrue(res["new_rows"])

    def test_idempotent_rerun_adds_no_rows(self):
        self.signal_on("20261002")
        self.write()
        self.run_scan()
        n1 = len(self.ledger.read_text().splitlines())
        res = self.run_scan()
        n2 = len(self.ledger.read_text().splitlines())
        self.assertEqual(res["new_rows"], [])
        self.assertEqual(n1, n2)

    def test_split_adjusted_to_entry_scale(self):
        # 10/20 に 1:2 分割（AdjFactor 0.5）。出口の引け 450 は新尺度 → 入口尺度で 900
        self.signal_on("20261002")
        for d in BDAYS[BDAYS.index("20261020"):]:
            self.days[d][X] = bar(X, 500.0, None, 500.0, fac=0.5 if d == "20261020" else 1.0)
        self.days["20261105"][X] = bar(X, 460.0, None, 450.0)
        self.write()
        self.run_scan()
        m = self.events("matured")[0]
        self.assertAlmostEqual(m["exit_px_adj"], 900.0, places=9)
        self.assertAlmostEqual(m["entry_px"], 1000.0, places=9)
        self.assertAlmostEqual(m["ret_short"], 0.0960547945, places=9)

    def test_censored_keeps_last_price_and_date(self):
        # 10/30 の引け 950 を最後に銘柄が消える（11/2・11/4・11/5 に bars はあるが行が無い）
        self.signal_on("20261002")
        self.days["20261030"][X] = bar(X, 960.0, None, 950.0)
        for d in ("20261102", "20261104", "20261105", "20261106"):
            del self.days[d][X]
        self.write()
        self.run_scan()                                   # 初回の欠け観測（取り直し前）＝まだ確定しない
        self.assertEqual(self.events("censored"), [])
        self.run_scan(env=self.refetched("20261105"))     # 出口日ファイルを後日取り直してもなお無い
        self.assertEqual(self.events("matured"), [])
        c = self.events("censored")
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["last_px_date"], "20261030")
        self.assertAlmostEqual(c[0]["last_px_adj"], 950.0)
        # 1006→1030 = 24暦日: +5% −0.3% −0.0115×24/365(=0.0007561644)
        self.assertAlmostEqual(c[0]["ret_short_at_last"], 0.0462438356, places=9)
        sm = R.summarize(R.load_ledger_lines(self.ledger))
        self.assertEqual(sm["day_values"], {})          # 打ち切りを含む日は主集計外
        self.assertEqual(sm["censored_days"], 1)

    def test_unfilled_when_bar_present_but_no_open(self):
        self.signal_on("20261002")
        self.days["20261006"][X] = bar(X, None, None, 1000.0)
        self.write()
        self.run_scan()                                   # 入口日ファイルは翌営業日 10/7 07:30 取得（既定）
        u = self.events("unfilled")
        self.assertEqual([(r["entry_day"], r["reason"]) for r in u], [("20261006", "no_open")])
        self.assertEqual(u[0]["null_confirm_fetched_at"], "2026-10-07T07:30:00+09:00")   # 4回目: 確定根拠の取得時刻
        self.assertEqual(self.events("filled"), [])
        self.assertEqual(self.events("matured"), [])


class TestCodexRound4PriceNull(Base):
    """Codex 4回目: 行はあるが価格が欠けている時、取得が翌営業日以降の時だけ確定する。"""

    def _ts(self, **over):
        return self.env(fetch_ts=dict(self.env().bars_fetch_ts, **over))

    def test_entry_intraday_fetch_pends_then_filled_when_open_arrives(self):
        self.signal_on("20261002")
        self.days["20261006"][X] = bar(X, None, None, 1000.0)
        self.write()
        intraday = self._ts(**{"20261006": "2026-10-06T11:30:00+09:00"})
        self.run_scan(env=intraday)
        self.run_scan(env=intraday)                                       # 同じ途中取得で再走査
        self.assertEqual(self.events("unfilled"), [])
        p = [(r["stage"], r["reason"]) for r in self.events("pending_data")]
        self.assertEqual(p, [("entry_price_null", "price_null_unconfirmed")])   # 重複排除で1行
        self.days["20261006"][X] = bar(X)                                 # 後で寄り値が補われた
        self.write()
        self.run_scan()                                                   # 10/7 07:30 取得のファイル
        self.assertEqual(self.events("unfilled"), [])
        self.assertEqual(self.events("filled")[0]["entry_px"], 1000.0)

    def test_entry_unknown_fetch_time_pends(self):
        self.signal_on("20261002")
        self.days["20261006"][X] = bar(X, None, None, 1000.0)
        self.write()
        no_ts = {d: t for d, t in self.env().bars_fetch_ts.items() if d != "20261006"}
        self.run_scan(env=self.env(fetch_ts=no_ts))
        self.assertEqual(self.events("unfilled"), [])
        self.assertEqual(self.events("pending_data")[0]["reason"], "price_null_unconfirmed")
        self.run_scan()                                                   # 翌営業日取得が判明 → 確定
        self.assertEqual([r["reason"] for r in self.events("unfilled")], ["no_open"])

    def test_exit_intraday_fetch_pends_then_matured_when_close_arrives(self):
        self.signal_on("20261002")
        self.days["20261105"][X] = bar(X, 920.0, None, None)              # 出口日: 行はあるが引け値なし
        self.write()
        self.run_scan(env=self._ts(**{"20261105": "2026-11-05T11:30:00+09:00"}))
        self.assertEqual(self.events("censored"), [])
        self.assertEqual([(r["stage"], r["reason"]) for r in self.events("pending_data")],
                         [("exit_price_null", "price_null_unconfirmed")])
        self.days["20261105"][X] = bar(X, 920.0, None, 900.0)             # 引け値が補われた
        self.write()
        self.run_scan()
        self.assertEqual(self.events("censored"), [])
        self.assertAlmostEqual(self.events("matured")[0]["exit_px_adj"], 900.0)

    def test_exit_next_business_day_fetch_with_null_close_is_censored(self):
        self.signal_on("20261002")
        self.days["20261105"][X] = bar(X, 920.0, None, None)
        self.write()
        self.run_scan(env=self._ts(**{"20261105": "2026-11-05T11:30:00+09:00"}))
        self.assertEqual(self.events("censored"), [])
        self.run_scan()                                                   # 11/6 07:30 取得でもなお引け値なし
        c = self.events("censored")
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["null_confirm_fetched_at"], "2026-11-06T07:30:00+09:00")
        self.assertEqual(self.events("matured"), [])

    def test_exit_unknown_fetch_time_pends(self):
        self.signal_on("20261002")
        self.days["20261105"][X] = bar(X, 920.0, None, None)
        self.write()
        no_ts = {d: t for d, t in self.env().bars_fetch_ts.items() if d != "20261105"}
        self.run_scan(env=self.env(fetch_ts=no_ts))
        self.assertEqual(self.events("censored"), [])
        self.assertEqual(self.events("pending_data")[0]["reason"], "price_null_unconfirmed")

    def test_gate_9_records_10_skips(self):
        nine = [f"9{i:03d}0" for i in range(9)]
        ten = [f"8{i:03d}0" for i in range(10)]
        for c in nine + ten:
            for d in BDAYS:
                self.days[d][c] = bar(c)
        for c in nine:
            self.signal_on("20261002", c)
        for c in ten:
            self.signal_on("20261005", c)
        self.days["20261002"]["77770"] = bar("77770", 1000.0, 800.0, 900.0)  # 名簿外の発火は数えない
        self.days["20261001"]["77770"] = bar("77770", 1000.0, None, 1000.0)
        self.universe |= set(nine) | set(ten)
        for d in BDAYS:      # 母数を揃えて incomplete 判定に掛からないようにする
            for c in nine + ten:
                self.days[d].setdefault(c, bar(c))
        self.write()
        self.run_scan()
        days = {r["D"]: r for r in self.events("day")}
        self.assertEqual((days["20261002"]["n_top500"], days["20261002"]["gate"]), (9, "recorded"))
        self.assertEqual((days["20261005"]["n_top500"], days["20261005"]["gate"]), (10, "skipped_day_count"))
        self.assertEqual(days["20261005"]["codes"], sorted(ten))
        sig_days = {r["D"] for r in self.events("signal")}
        self.assertEqual(sig_days, {"20261002"})
        self.assertEqual(len(self.events("signal")), 9)
        self.assertEqual([r["D"] for r in self.events("skipped_day_count")], ["20261005"])

    def test_late_detected_and_overlap(self):
        self.signal_on("20261002")
        self.days["20261007"][X] = bar(X, 1000.0, 850.0, 900.0)   # 保有中の再発火（前日終値1000）
        self.write()
        self.run_scan(now="2026-10-06T10:00:00+09:00")
        sig = {r["D"]: r for r in self.events("signal")}
        self.assertTrue(sig["20261002"]["late_detected"])        # 入口 1006 09:00 以降に初検出
        self.assertFalse(sig["20261007"]["late_detected"])       # 入口 1009 より前
        self.assertFalse(sig["20261002"]["overlap_open"])
        self.assertTrue(sig["20261007"]["overlap_open"])
        self.assertEqual(len(self.events("late_detected")), 1)

    def test_incomplete_day_not_recorded_and_retried(self):
        self.signal_on("20261002")
        self.days["20261008"] = {f: bar(f, 100.0, 100.0, 100.0) for f in FILLERS[:10]}  # 21→10銘柄
        self.write()
        self.run_scan()
        self.assertNotIn("20261008", {r["D"] for r in self.events("day")})
        self.assertEqual([r["D"] for r in self.events("incomplete")], ["20261008"])
        self.run_scan()
        self.assertEqual(len(self.events("incomplete")), 1)


class TestFrozenStartGuard(Base):
    def test_days_before_frozen_start_not_recorded(self):
        self.signal_on("20261002")
        self.write()
        self.run_scan(frozen="20261005", since="20261001")      # since を前に指定しても切り上げ
        days = {r["D"] for r in self.events("day")}
        self.assertNotIn("20261002", days)
        self.assertEqual(min(days), "20261005")
        self.assertEqual(self.events("signal"), [])

    def test_assert_and_production_ledger_guard(self):
        with self.assertRaises(SystemExit):
            R.assert_not_pre_start("20261002", "20261005", False)
        R.assert_not_pre_start("20261005", "20261005", False)    # 当日は可
        with self.assertRaises(SystemExit):
            R.check_pre_start_guard(R.LEDGER_DEFAULT, True)

    def test_default_frozen_start_records_nothing_before_it(self):
        self.signal_on("20261002")
        self.write()
        R.scan(self.env(), self.ledger, NOW_EARLY)               # 既定 FROZEN_START=20261005
        self.assertNotIn("20261002", {r["D"] for r in self.events("day")})


class TestCodexReviewFixes(Base):
    """Codex 実装レビュー 6件（2026-10-03）の回帰テスト。"""

    # #1 判定時点より後に成熟した結果を混ぜない・一度だけ凍結して再計算しない
    def test_judgment_uses_only_cohorts_matured_by_due_and_is_frozen(self):
        rows = []
        def cohort(d, exit_day, rp, rec="2027-10-16T07:40:00+09:00"):
            base = {"code": "11110", "D": d}
            rows.extend([{"event": "signal", **base, "entry_day": d, "exit_day": exit_day, "late_detected": False},
                         {"event": "filled", **base, "entry_px": 1.0},
                         {"event": "matured", **base, "exit_day": exit_day, "ret_pair": rp, "recorded_at": rec}])
        for i in range(60):                     # 10/29 までに成熟・記録された 60 日（各 +1%）
            cohort(f"202708{i:02d}", "20271015", 0.01)
        for i in range(5):                      # 判定時点の後（11/5）に成熟した 5 日（各 −50%）
            cohort(f"202709{i:02d}", "20271105", -0.5, rec="2027-11-06T07:40:00+09:00")
        for i in range(3):                      # 出口は判定日前だが、記録が判定日の翌日（再レビュー #1）
            cohort(f"202707{i:02d}", "20271015", -0.9, rec="2027-10-30T07:40:00+09:00")
        self.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
        env = self.env()
        env.month_ends = ["20271029", "20271130"]
        bars = self.root / "bars"
        (bars / "20271029.json.gz").write_bytes(b"")       # 最新 bars = 判定日 → まだ書かない（再レビュー #5）
        R.scan(env, self.ledger, NOW_EARLY, today="20271029")
        self.assertEqual(self.events("judgment"), [])
        (bars / "20271101.json.gz").write_bytes(b"")       # 判定日の翌営業日の bars が入った
        R.scan(env, self.ledger, NOW_EARLY, today="20271110")
        j = self.events("judgment")
        self.assertEqual(len(j), 1)
        self.assertEqual(j[0]["due"], "20271029")
        self.assertEqual(j[0]["result"]["n_days"], 60)            # 後から成熟した 5 日は入らない
        self.assertAlmostEqual(j[0]["result"]["mean_day"], 0.01)
        # 後日もう一度走らせても判定は増えず、表示は凍結値
        R.scan(env, self.ledger, NOW_EARLY, today="20271215")
        self.assertEqual(len(self.events("judgment")), 1)
        txt = R.format_summary(R.summarize(R.load_ledger_lines(self.ledger)))
        self.assertIn("20271029 で凍結", txt)

    # #2 入口価格は保存値を使う（後日の取り直しで変わらない）
    def test_entry_price_uses_stored_fill_not_reread(self):
        self.signal_on("20261002")
        self.days["20261105"][X] = bar(X, 920.0, None, 900.0)
        self.write(skip={d for d in BDAYS if d > "20261030"})      # まだ出口前
        self.run_scan()
        self.assertEqual(self.events("filled")[0]["entry_px"], 1000.0)
        self.days["20261006"][X] = bar(X, 5000.0, None, 5000.0)   # 入口日ファイルが別の値で取り直された
        self.write()
        self.run_scan()
        m = self.events("matured")[0]
        self.assertEqual(m["entry_px"], 1000.0)
        self.assertAlmostEqual(m["ret_short"], 0.0960547945, places=9)

    # #2 出口後の分割で出口日の足が新尺度に取り直されても、入口の尺度へ戻す
    def test_post_exit_split_normalized_to_entry_scale(self):
        self.signal_on("20261002")
        self.days["20261105"][X] = bar(X, 460.0, None, 450.0)     # 11/7 取得＝11/6 分割を反映した新尺度
        for d in BDAYS[BDAYS.index("20261106"):]:
            self.days[d][X] = bar(X, 450.0, None, 450.0, fac=0.5 if d == "20261106" else 1.0)
        (self.root / "fetch_log.jsonl").write_text(json.dumps(
            {"kind": "bars", "status": "saved", "date": "20261105", "ts": "2026-11-07T07:30:00+09:00"}) + "\n")
        self.write()
        self.run_scan()
        m = self.events("matured")[0]
        self.assertAlmostEqual(m["exit_px_adj"], 900.0, places=9)  # 450 × 2
        self.assertAlmostEqual(m["ret_short"], 0.0960547945, places=9)

    # #3 初検出時刻は日ごとの壁時計（走査開始の1回ではない）
    def test_detected_at_taken_per_day(self):
        self.signal_on("20261002")
        self.signal_on("20261008", "F0000")
        self.write()
        t = [dt.datetime(2026, 10, 3, 7, 0, tzinfo=S.JST)]
        def clock():
            t[0] += dt.timedelta(minutes=1)
            return t[0].isoformat()
        R.scan(self.env(), self.ledger, frozen_start="20261002", clock=clock)
        days = [r for r in self.events("day")]
        stamps = [r["detected_at"] for r in days]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(set(stamps)), len(stamps))            # 日ごとに違う時刻
        sig = {r["D"]: r for r in self.events("signal")}
        dmap = {r["D"]: r["detected_at"] for r in days}
        for d, s in sig.items():
            self.assertEqual(s["detected_at"], dmap[d])
            self.assertEqual(s["late_detected"], S.is_late(s["detected_at"], s["entry_day"]))

    # #4 不完全な入口日ファイル → pending_data（unfilled にしない）。完全なファイルで行が無ければ unfilled
    def test_incomplete_entry_file_pends_then_complete_absent_is_unfilled(self):
        self.signal_on("20261002")
        self.days["20261006"] = {f: bar(f, 100.0, 100.0, 100.0) for f in FILLERS[:10]}  # 21→10 銘柄・X なし
        self.write()
        self.run_scan()
        self.assertEqual(self.events("unfilled"), [])
        pend = self.events("pending_data")
        self.assertEqual([(p["stage"], p["reason"]) for p in pend], [("entry", "incomplete")])
        self.days["20261006"] = {f: bar(f, 100.0, 100.0, 100.0) for f in FILLERS}         # 完全・X の行なし
        self.days["20261006"]["22220"] = bar("22220")
        self.write()
        self.run_scan()
        self.assertEqual(self.events("unfilled"), [])     # 欠けの初回観測だけ（3回目 (b)）
        self.run_scan(env=self.refetched("20261006"))
        self.assertEqual([r["reason"] for r in self.events("unfilled")], ["no_bar"])

    # #4 出口範囲に不完全ファイル → censored にせず pending_data
    def test_incomplete_exit_range_pends_not_censored(self):
        self.signal_on("20261002")
        self.days["20261104"] = {f: bar(f, 100.0, 100.0, 100.0) for f in FILLERS[:10]}
        del self.days["20261105"][X]
        self.write()
        self.run_scan()
        self.assertEqual(self.events("censored"), [])
        self.assertEqual([(p["stage"], p["missing_bars"]) for p in self.events("pending_data")],
                         [("exit", "20261104")])

    # #4 D-1 が不完全なら D を 0件の日として確定しない
    def test_incomplete_prev_day_blocks_zero_day(self):
        self.days["20261008"] = {f: bar(f, 100.0, 100.0, 100.0) for f in FILLERS[:10]}
        self.write()
        self.run_scan()
        days = {r["D"] for r in self.events("day")}
        self.assertNotIn("20261008", days)
        self.assertNotIn("20261009", days)
        self.assertIn("20261013", days)

    # #5 TOPIX の出口引け・入口寄りが欠けている間は pending_data
    def test_topix_missing_pends_until_present(self):
        self.signal_on("20261002")
        self.write()
        tc = {d: 2000.0 for d in BDAYS if d != "20261105"}
        self.run_scan(env=self.env(topix_c=tc))
        self.assertEqual(self.events("matured"), [])
        self.assertEqual([(p["stage"], p["missing_topix"]) for p in self.events("pending_data")],
                         [("exit_topix", "20261105")])
        to = {d: 2000.0 for d in BDAYS if d != "20261006"}
        self.run_scan(env=self.env(topix_o=to))
        self.assertEqual(self.events("matured"), [])
        self.run_scan()
        self.assertEqual(len(self.events("matured")), 1)

    # #6 day を書いた直後に落ちた日（day_done なし）は再処理し、欠けた行だけ補う（重複なし）
    def test_crash_mid_day_is_reprocessed_without_duplicates(self):
        self.signal_on("20261002")
        self.write()
        self.run_scan()
        full = R.load_ledger_lines(self.ledger)
        crashed = [r for r in full if r["event"] == "day" and r["D"] == "20261002"]
        self.ledger.write_text("".join(json.dumps(r) + "\n" for r in crashed))
        self.run_scan()
        after = R.load_ledger_lines(self.ledger)
        keys = [R.event_key(r) for r in after]
        self.assertEqual(len(keys), len(set(keys)))                 # 事象キーの重複なし
        self.assertEqual(len([r for r in after if r["event"] == "day" and r["D"] == "20261002"]), 1)
        self.assertEqual([r["code"] for r in after if r["event"] == "signal"], [X])
        self.assertIn(("day_done", "20261002", None, None), set(keys))
        self.assertEqual(len([r for r in after if r["event"] == "matured"]), 1)
        self.assertEqual(sorted(R.event_key(r) for r in after), sorted(R.event_key(r) for r in full))


class TestCodexReReviewFixes(Base):
    """Codex 再レビュー（2026-10-03）で残った指摘の回帰テスト。"""

    # 再#2 分割走査の範囲（入口〜最新）で営業日の bars が欠けていたら飛ばさず待つ
    def test_missing_split_day_file_in_scan_range_pends(self):
        self.signal_on("20261002")
        self.days["20261105"][X] = bar(X, 460.0, None, 450.0)
        for d in BDAYS[BDAYS.index("20261106"):]:
            self.days[d][X] = bar(X, 450.0, None, 450.0, fac=0.5 if d == "20261106" else 1.0)
        (self.root / "fetch_log.jsonl").write_text(json.dumps(
            {"kind": "bars", "status": "saved", "date": "20261105", "ts": "2026-11-07T07:30:00+09:00"}) + "\n")
        self.write(skip={"20261106"})                     # 分割日のファイルだけ欠け（後の日はある）
        self.run_scan()
        self.assertEqual(self.events("matured"), [])
        self.assertEqual([p["stage"] for p in self.events("pending_data")], ["exit_splits"])
        self.write()
        self.run_scan()
        self.assertAlmostEqual(self.events("matured")[0]["exit_px_adj"], 900.0, places=9)

    # 再#3 入口日に行が無い／寄り値が無いことは、翌営業日以降の取得で確かめてから unfilled
    def test_unfilled_no_bar_requires_strictly_later_refetch(self):
        # 3回目 (b): 21銘柄中 X だけ欠けた 20銘柄のファイル（90% 関門は通る）に翌営業日の取得時刻
        self.signal_on("20261002")
        del self.days["20261006"][X]
        self.write()
        ts = self.env().bars_fetch_ts
        no_ts = {d: t for d, t in ts.items() if d != "20261006"}
        self.run_scan(env=self.env(fetch_ts=no_ts))                         # 取得時刻が不明 → 待つ
        self.run_scan()                                                     # 翌営業日 10/7 07:30 の取得 → 初回観測
        self.run_scan()                                                     # 同じ取得のまま再走査 → なお待つ
        self.assertEqual(self.events("unfilled"), [])
        firsts = [p.get("first_absent_fetched_at") for p in self.events("pending_data")]
        self.assertEqual(firsts, [None, "2026-10-07T07:30:00+09:00"])
        self.run_scan(env=self.refetched("20261006"))                       # 後日取り直してもなお無い
        u = self.events("unfilled")
        self.assertEqual([r["reason"] for r in u], ["no_bar"])
        self.assertEqual(u[0]["first_absent_fetched_at"], "2026-10-07T07:30:00+09:00")
        self.assertEqual(u[0]["confirm_fetched_at"], "2026-11-20T07:30:00+09:00")
        self.assertIn("recorded_at", u[0])

    def test_absent_code_reappears_on_refetch_fills(self):
        self.signal_on("20261002")
        del self.days["20261006"][X]
        self.write()
        self.run_scan()                                                     # 欠けの初回観測
        self.days["20261006"][X] = bar(X)                                   # 取り直したら行があった
        self.write()
        self.run_scan(env=self.refetched("20261006"))
        self.assertEqual(self.events("unfilled"), [])
        self.assertEqual(len(self.events("filled")), 1)

    def test_censored_requires_strictly_later_refetch(self):
        self.signal_on("20261002")
        self.days["20261030"][X] = bar(X, 960.0, None, 950.0)
        for d in ("20261102", "20261104", "20261105", "20261106"):
            del self.days[d][X]
        self.write()
        self.run_scan()
        self.run_scan()
        self.assertEqual(self.events("censored"), [])
        self.assertEqual([(p["stage"], p["reason"]) for p in self.events("pending_data")],
                         [("exit_absent", "absence_unconfirmed")])
        self.run_scan(env=self.refetched("20261105"))
        c = self.events("censored")
        self.assertEqual(c[0]["last_px_date"], "20261030")
        self.assertEqual(c[0]["confirm_fetched_at"], "2026-11-20T07:30:00+09:00")

    # 再#4 day 行だけ残って落ちた日は、保存済みの銘柄一覧と初検出時刻で復元（再計算しない）
    def test_crash_restore_uses_stored_codes_and_time(self):
        self.signal_on("20261002", "F0000")               # 今のデータで計算すると F0000 が出る
        self.write()
        stored = {"event": "day", "D": "20261002", "n_raw": 1, "n_top500": 1, "gate": "recorded",
                  "codes": [X], "detected_at": "2026-10-06T09:30:00+09:00"}   # 保存時は X・遅れて検出
        self.ledger.write_text(json.dumps(stored) + "\n")
        self.run_scan()
        sig = [r for r in self.events("signal") if r["D"] == "20261002"]
        self.assertEqual([r["code"] for r in sig], [X])                     # F0000 を再計算で足さない
        self.assertEqual(sig[0]["detected_at"], "2026-10-06T09:30:00+09:00")
        self.assertTrue(sig[0]["late_detected"])                            # 保存時刻で遅れ判定
        self.assertEqual(len([r for r in self.events("day") if r["D"] == "20261002"]), 1)
        self.assertEqual(len([r for r in self.events("day_done") if r["D"] == "20261002"]), 1)


class TestCodexRound3Fixes(Base):
    """Codex 3回目（2026-10-03）の回帰テスト (a)(c)。(b) は TestCodexReReviewFixes 側。"""

    @staticmethod
    def _cohort(rows, d, exit_day, rp, rec="2027-10-16T07:40:00+09:00", code="11110"):
        base = {"code": code, "D": d}
        rows.extend([{"event": "signal", **base, "entry_day": d, "exit_day": exit_day, "late_detected": False},
                     {"event": "filled", **base, "entry_px": 1.0},
                     {"event": "matured", **base, "exit_day": exit_day, "ret_pair": rp, "recorded_at": rec}])

    def _scan_judge(self, rows, today="20271110"):
        self.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
        env = self.env()
        env.month_ends = ["20271029", "20271130"]
        (self.root / "bars" / "20271101.json.gz").write_bytes(b"")
        R.scan(env, self.ledger, NOW_EARLY, today=today)
        return self.events("judgment")

    # (a) A は判定日前に成熟、同じ日の B が 11/10 に初めて unfilled → そのコホートは 10/29 判定に入らない
    def test_unfilled_recorded_after_cutoff_excludes_cohort(self):
        rows = []
        for i in range(60):
            self._cohort(rows, f"202708{i:02d}", "20271015", 0.01)
        mixed = "20270701"
        self._cohort(rows, mixed, "20271015", -0.9)                              # A: 10/16 記録で成熟
        rows += [{"event": "signal", "code": "22220", "D": mixed, "entry_day": mixed,
                  "exit_day": "20271015", "late_detected": False},
                 {"event": "unfilled", "code": "22220", "D": mixed, "reason": "no_bar",
                  "recorded_at": "2027-11-10T07:40:00+09:00"}]                   # B: 11/10 に確定
        sm = R.summarize(rows)
        self.assertEqual(sm["cohorts"][mixed][1], "2027-11-10T07:40:00+09:00")    # 全終端事象の最大
        j = self._scan_judge(rows)
        self.assertEqual(len(j), 1)
        self.assertEqual(j[0]["due"], "20271029")
        self.assertEqual(j[0]["result"]["n_days"], 60)                            # mixed（−90%）は入らない
        self.assertAlmostEqual(j[0]["result"]["mean_day"], 0.01)

    def test_unfilled_without_recorded_at_blocks_cohort(self):
        rows = []
        self._cohort(rows, "20270701", "20271015", 0.01)
        rows += [{"event": "signal", "code": "22220", "D": "20270701", "entry_day": "20270701",
                  "exit_day": "20271015", "late_detected": False},
                 {"event": "unfilled", "code": "22220", "D": "20270701", "reason": "no_open"}]  # 記録時刻なし
        self.assertIsNone(R.summarize(rows)["cohorts"]["20270701"][1])

    # (c) 出口は全部10月でも 59 コホートの記録が 11/10 → 10/29 は判定日にならず pass=True の凍結もしない
    def test_due_counts_only_cohorts_available_by_cutoff(self):
        rows = []
        self._cohort(rows, "20270801", "20271015", 0.5)                           # 10/16 記録（1件だけ間に合う）
        for i in range(59):
            self._cohort(rows, f"202709{i:02d}", "20271015", 0.01, rec="2027-11-10T07:40:00+09:00")
        self.assertEqual(self._scan_judge(rows, today="20271110"), [])
        sm = R.summarize(R.load_ledger_lines(self.ledger))
        mends = ["20271029", "20271130"]
        self.assertIsNone(S.judgment_due("20271110", sm["cohorts"], mends))
        self.assertEqual(S.judgment_due("20271201", sm["cohorts"], mends), "20271130")  # 11/30 なら60件揃う


class TestJudgmentDue(unittest.TestCase):
    def test_not_due_before_12_months_or_60_days(self):
        mends = ["20261030", "20271029", "20271130"]
        def coh(n, exit_day="20271001", rec="2027-10-02T07:40:00+09:00"):
            return {f"d{i}": (exit_day, rec, [0.01]) for i in range(n)}
        self.assertIsNone(S.judgment_due("20261102", coh(100, "20261101", "2026-11-02T07:40:00+09:00"), mends))
        self.assertIsNone(S.judgment_due("20271102", coh(59), mends))
        self.assertEqual(S.judgment_due("20271102", coh(60), mends), "20271029")
        self.assertIsNone(S.judgment_due("20271102", coh(60, rec=None), mends))     # 記録時刻なしは数えない
        self.assertEqual(S.judgment_due("20271230", {}, mends), "20271230")

class TestSplitFalseTrigger(Base):
    """分割の権利落ち日の見かけの急落は合図にしない（2026-10-03 4モデル監査・9/29 偽合図）。
    判定は取得時点に依らない未調整値 L(D) <= C(D-1) × AdjFactor(D) × 0.90。"""
    PREV, DAY = "20261001", "20261002"

    def put(self, prev_c, prev_adjc, low, adjl, fac):
        self.days[self.PREV][X] = dict(bar(X, 1000.0, None, prev_adjc), C=prev_c)
        self.days[self.DAY][X] = dict(bar(X, 500.0, adjl, 490.0, fac=fac), L=low)
        self.write()
        self.run_scan()
        return [r for r in self.events("day") if r["D"] == self.DAY][0]

    def test_unadjusted_prev_apparent_drop_is_dropped(self):
        # 1:2 分割・前日足は未調整（C=AdjC=1000）・当日安値480 → 調整後前日500比 −4% ＝偽合図
        row = self.put(1000.0, 1000.0, 480.0, 480.0, 0.5)
        self.assertEqual(row["split_dropped"], [X])
        self.assertEqual(self.events("signal"), [])

    def test_unadjusted_prev_real_drop_is_kept(self):
        # 分割後でも −12%（1000×0.5×0.90=450 >= 安値440）→ 本物の急落
        self.put(1000.0, 1000.0, 440.0, 440.0, 0.5)
        self.assertEqual(len(self.events("signal")), 1)

    def test_adjusted_prev_real_drop_is_not_double_corrected(self):
        # Codex 反例型（34940・2024-01-30）: 前日足は分割後に取得され調整済み（C=2396・AdjC=479.2）
        # 当日 AdjFactor 0.2・安値417 → 2396×0.2×0.90=431.3 >= 417 ＝本物の −13% を残す
        row = self.put(2396.0, 479.2, 417.0, 417.0, 0.2)
        self.assertEqual(row["split_dropped"], [])
        self.assertEqual(len(self.events("signal")), 1)

    def test_no_split_day_unchanged(self):
        self.put(1000.0, 1000.0, 880.0, 880.0, 1.0)
        self.assertEqual(len(self.events("signal")), 1)


if __name__ == "__main__":
    unittest.main()
