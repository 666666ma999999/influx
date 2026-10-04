"""稼働中レーンの株式分割の穴（2026-10-04 オーナー裁定・tasks/split_fix_live_lanes.md）の回帰テスト。

- 反発系の生成器: 切替日以降は未調整値 L(D) <= C(D-1)×AdjFactor(D)×0.90 で判定する
- ペーパー台帳: 偽合図の行に印を付け、成績表から外して件数を出す
- 価格レーン / ニュースレーン: 評価日の価格を発火時の尺度に揃える・既存行は補正行で訂正

期待値は手計算で置いた値（式を呼んで期待値を作らない）。実データには触れない。
実行: python3 -m unittest tests.test_split_fix_live_lanes
"""
from __future__ import annotations

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
import kpi_round23_signals as R23  # noqa: E402
import measure_base_rate  # noqa: E402
import news_shock_eval as NSE  # noqa: E402
import paper_eval  # noqa: E402
import price_watch_forward as fwd  # noqa: E402

X = "11110"


def bar(code, c=1000.0, adjc=None, low=None, adjl=None, fac=1.0, o=None):
    return {"Code": code, "AdjO": o, "AdjC": c if adjc is None else adjc, "C": c,
            "AdjL": adjl, "L": low, "AdjFactor": fac}


class TmpBars(unittest.TestCase):
    """jq_fetch.DATA_ROOT を一時ディレクトリへ差し替え、bars と fetch_log を書く土台。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "bars").mkdir()
        self.patches = [mock.patch.object(jq_fetch, "DATA_ROOT", self.root),
                        mock.patch.object(jq_fetch, "LOG_PATH", self.root / "fetch_log.jsonl")]
        for p in self.patches:
            p.start()
        self._clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._clear()
        self.tmp.cleanup()

    @staticmethod
    def _clear():
        measure_base_rate.load_bars_day.cache_clear() if hasattr(measure_base_rate.load_bars_day, "cache_clear") else None
        paper_eval.load_bars_day_if_cached.cache_clear()
        jq_fetch.bars_fetch_dates.cache_clear()
        jq_fetch.bars_fetch_history.cache_clear()

    def write_day(self, day, recs):
        with gzip.open(self.root / "bars" / f"{day}.json.gz", "wt", encoding="utf-8") as f:
            json.dump({"data": recs}, f)
        self._clear()

    def write_calendar(self, days):
        """取引所カレンダー（営業日だけ HolDiv=1）を一時ディレクトリへ書く。"""
        with gzip.open(self.root / "calendar.json.gz", "wt", encoding="utf-8") as f:
            json.dump({"data": [{"Date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "HolDiv": "1"} for d in days]}, f)

    def write_fetch_log(self, saved: dict[str, str]):
        """{bars の日付: 取得日 YYYY-MM-DD}"""
        with open(self.root / "fetch_log.jsonl", "w", encoding="utf-8") as f:
            for d, ts in saved.items():
                f.write(json.dumps({"kind": "bars", "status": "saved", "date": d,
                                    "ts": f"{ts}T07:30:00+09:00"}) + "\n")
        self._clear()


# --- 1. 反発系の生成器 ------------------------------------------------------------------


class TestGeneratorSplitSafe(TmpBars):
    PREV, DAY = "20261005", "20261006"          # 切替日（20261005）以降の合図日
    OLD_PREV, OLD_DAY = "20261001", "20261002"  # 切替日より前の合図日
    BDAYS = ["20261001", "20261002", "20261005", "20261006"]

    def gen(self, day):
        bidx = {d: i for i, d in enumerate(self.BDAYS)}
        df, _ = R23.generate_sell_reg_trigger_signals(day, day, self.BDAYS, bidx)
        return [] if df.empty else list(df["code"])

    def put(self, prev, day, prev_c, prev_adjc, low, adjl, fac):
        self.write_day(prev, [bar(X, c=prev_c, adjc=prev_adjc)])
        self.write_day(day, [bar(X, c=490.0, low=low, adjl=adjl, fac=fac)])
        for d in self.BDAYS:                      # 残りの日も空ファイルで置く（最古日の判定用）
            p = self.root / "bars" / f"{d}.json.gz"
            if not p.exists():
                self.write_day(d, [])

    def test_split_day_false_trigger_dropped(self):
        # 1:2 分割・前日足は未調整（C=AdjC=1000）・当日安値480 → 1000×0.5×0.9=450 < 480 ＝偽合図
        self.put(self.PREV, self.DAY, 1000.0, 1000.0, 480.0, 480.0, 0.5)
        self.assertEqual(self.gen(self.DAY), [])

    def test_real_drop_on_split_day_kept(self):
        # 分割後でも 1000×0.5×0.9=450 >= 安値440 → 本物の急落（−12%）
        self.put(self.PREV, self.DAY, 1000.0, 1000.0, 440.0, 440.0, 0.5)
        self.assertEqual(self.gen(self.DAY), [X])

    def test_already_adjusted_prev_counterexample_kept(self):
        # Codex 反例型（34940・2024-01-30）: 前日足は分割後に取得され調整済み（C=2396・AdjC=479.2）
        # 当日 AdjFactor 0.2・安値417 → 2396×0.2×0.9=431.28 >= 417 ＝本物（AdjC×AdjFactor の二重補正をしない）
        self.put(self.PREV, self.DAY, 2396.0, 479.2, 417.0, 417.0, 0.2)
        self.assertEqual(self.gen(self.DAY), [X])

    def test_before_cutover_keeps_legacy_rule(self):
        # 切替日より前の合図日は従来判定のまま（480 <= 1000×0.9 で出る＝履歴を動かさない）
        self.put(self.OLD_PREV, self.OLD_DAY, 1000.0, 1000.0, 480.0, 480.0, 0.5)
        self.assertEqual(self.gen(self.OLD_DAY), [X])

    def test_raw_missing_falls_back_to_legacy(self):
        # 未調整 L が無い銘柄は従来判定（AdjL 880 <= 1000×0.9）
        self.write_day(self.PREV, [{"Code": X, "AdjC": 1000.0, "AdjFactor": 1.0}])
        self.write_day(self.DAY, [{"Code": X, "AdjL": 880.0, "AdjC": 900.0, "AdjFactor": 1.0}])
        for d in self.BDAYS[:2]:
            self.write_day(d, [])
        self.assertEqual(self.gen(self.DAY), [X])

    def test_cutover_day_itself_uses_raw_rule(self):
        # 合図日 = 切替日 20261005 当日も未調整判定（偽合図は出ない）
        self.put("20261002", "20261005", 1000.0, 1000.0, 480.0, 480.0, 0.5)
        self.assertEqual(self.gen("20261005"), [])

    def test_before_cutover_diag_has_no_new_keys(self):
        self.put(self.OLD_PREV, self.OLD_DAY, 1000.0, 1000.0, 480.0, 480.0, 0.5)
        bidx = {d: i for i, d in enumerate(self.BDAYS)}
        _, diag = R23.generate_sell_reg_trigger_signals(self.OLD_DAY, self.OLD_DAY, self.BDAYS, bidx)
        self.assertEqual(set(diag), {"business_days_scanned", "prev_day_out_of_range", "code_day_observations",
                                     "prev_close_missing", "low_missing", "signals_sell_reg_trigger"})

    def test_raw_drop_hit_values(self):
        self.assertFalse(R23.raw_drop_hit({"L": 480.0, "AdjFactor": 0.5}, {"C": 1000.0}))
        self.assertTrue(R23.raw_drop_hit({"L": 417.0, "AdjFactor": 0.2}, {"C": 2396.0}))
        self.assertIsNone(R23.raw_drop_hit({"L": None, "AdjFactor": 1.0}, {"C": 1000.0}))


# --- 2. ペーパー台帳の印と成績表 --------------------------------------------------------


def ledger_row(code, sd, status="closed", ret=0.05, kpi="sell_reg_trigger_rebound"):
    return {"kpi_name": kpi, "code": code, "signal_date": sd, "status": status,
            "ret_net": ret if status == "closed" else None,
            "ret_gross": (ret + 0.01) if status == "closed" else None,
            "exit_reason": "time_exit" if status == "closed" else None,
            "ret_nostop": ret if status == "closed" else None}


class TestPaperLedgerFlag(TmpBars):
    BDAYS = ["20260928", "20260929"]

    def test_flag_false_and_keep_real(self):
        F, T = "22220", "33330"
        self.write_day("20260928", [bar(F, c=1000.0), bar(T, c=1000.0)])
        self.write_day("20260929", [bar(F, c=300.0, low=190.0, adjl=190.0, fac=0.2),   # 1000×0.2×0.9=180 < 190 偽
                                     bar(T, c=850.0, low=850.0, adjl=850.0, fac=1.0)])  # 850 <= 900 本物
        recs = [ledger_row(F, "20260929", "open"), ledger_row(T, "20260929", "open"),
                ledger_row(F, "20260929", "open", kpi="other_kpi")]
        bidx = {d: i for i, d in enumerate(self.BDAYS)}
        diag = paper_eval.flag_split_false_signals(recs, bidx, self.BDAYS)
        self.assertEqual(diag["flagged"], 1)
        self.assertTrue(recs[0]["split_false_signal"])
        self.assertIn("raw_recheck", recs[0]["split_false_method"])
        self.assertIn("split_false_flagged_at", recs[0])
        self.assertNotIn("split_false_signal", recs[1])
        self.assertNotIn("split_false_signal", recs[2])     # 他の KPI は判定しない
        # 冪等: 2回目は新規 0
        diag2 = paper_eval.flag_split_false_signals(recs, bidx, self.BDAYS)
        self.assertEqual((diag2["flagged"], diag2["already"]), (0, 1))

    def test_flagged_rows_excluded_from_scoreboard(self):
        recs = [ledger_row("A0000", "20260801", "closed", 0.10),
                ledger_row("B0000", "20260801", "closed", -0.02),
                {**ledger_row("C0000", "20260929", "closed", 0.30), "split_false_signal": True},
                {**ledger_row("D0000", "20260929", "open"), "split_false_signal": True}]
        sb = paper_eval.compute_scoreboard(recs)["sell_reg_trigger_rebound"]
        self.assertEqual(sb["n_split_excluded"], 2)
        self.assertEqual(sb["n_closed"], 2)
        self.assertEqual(sb["n_open"], 0)
        self.assertEqual(sb["n_nostop"], 2)
        self.assertAlmostEqual(sb["ev_nostop"], 0.04)     # (0.10 − 0.02) / 2
        out = self.root / "sb.md"
        paper_eval.write_scoreboard_md({"sell_reg_trigger_rebound": sb}, out)
        self.assertIn("分割の偽合図(除外)", out.read_text(encoding="utf-8"))
        self.assertIn("| 2 |\n", out.read_text(encoding="utf-8"))


# --- 3. 価格レーンの分割補正 -------------------------------------------------------------


class TestPriceWatchSplitScaled(TmpBars):
    BASE, MID, EV = "20260731", "20260929", "20260930"

    def setUp(self):
        super().setUp()
        self.log = self.root / "forward_log.jsonl"
        self.p_log = mock.patch.object(fwd, "LOG_PATH", self.log)
        self.p_log.start()
        self.bdays = [self.BASE, self.MID, self.EV]
        self.p_bd = mock.patch.object(fwd, "business_days", lambda: self.bdays)
        self.p_bd.start()
        self.p_tp = mock.patch.object(fwd, "load_topix",
                                      lambda: {self.BASE: 2000.0, self.MID: 2040.0, self.EV: 2080.0})
        self.p_tp.start()
        # 80350 型: base 7/31 AdjC 40000 → 9/29 に 1:5 分割（AdjFactor 0.2）→ 評価日 AdjC 9048
        self.write_day(self.BASE, [bar("80350", c=40000.0)])
        self.write_day(self.MID, [bar("80350", c=9000.0, fac=0.2)])
        self.write_day(self.EV, [bar("80350", c=9048.0)])
        self.write_fetch_log({self.BASE: "2026-08-03", self.MID: "2026-09-30", self.EV: "2026-10-01"})
        self.firing = {"type": "firing", "spec_version": 3, "lane": "rise", "fire_date": "2026-08-02",
                       "series_id": "semicon-equip", "series_jp": "半導体装置", "base_day": self.BASE,
                       "topix_entry": 2000.0, "eval_days": {"w8": self.EV},
                       "stocks": [{"code": "80350", "entry_close": 40000.0, "tier": "confirmed"}]}

    def tearDown(self):
        self.p_log.stop()
        self.p_bd.stop()
        self.p_tp.stop()
        super().tearDown()

    def rows(self):
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines() if x.strip()]

    def test_new_evaluation_is_split_scaled(self):
        self.log.write_text(json.dumps(self.firing) + "\n", encoding="utf-8")
        fwd.evaluate()
        ev = [r for r in self.rows() if r["type"] == "evaluation"][0]
        r = ev["results"][0]
        # 9048 × 5 / 40000 − 1 = +13.10%・TOPIX +4.00% → 超過 +9.10pt
        self.assertEqual(r["ret_pct"], 13.1)
        self.assertEqual(r["excess_pct"], 9.1)
        self.assertTrue(r["hit"])
        self.assertEqual(r["split_scale"], 5.0)
        self.assertEqual(ev["split_adjust"], "jq_fetch.split_scale")

    def test_old_evaluation_gets_correction_row_once(self):
        old = {"type": "evaluation", "spec_version": 3, "lane": "rise", "fire_date": "2026-08-02",
               "series_id": "semicon-equip", "window": "w8", "eval_day": self.EV, "topix_ret_pct": 4.0,
               "results": [{"code": "80350", "ret_pct": -77.38, "excess_pct": -81.38, "hit": False,
                            "status": "ok"}], "n_hit": 0, "n_scored": 1, "evaluated_at": "2026-10-01T00:00:00+00:00"}
        self.log.write_text(json.dumps(self.firing) + "\n" + json.dumps(old) + "\n", encoding="utf-8")
        fwd.evaluate()
        fwd.evaluate()                                   # 冪等: 2回目は増えない
        rows = self.rows()
        self.assertEqual(len([r for r in rows if r["type"] == "evaluation"]), 1)   # 元の行は残し・新規評価もしない
        corr = [r for r in rows if r["type"] == "evaluation_correction"]
        self.assertEqual(len(corr), 1)
        self.assertEqual(corr[0]["results"][0]["ret_pct"], 13.1)
        self.assertEqual(corr[0]["results"][0]["before"]["ret_pct"], -77.38)
        self.assertEqual((corr[0]["n_hit"], corr[0]["n_scored"]), (1, 1))
        eff = fwd.effective_evaluations(rows)
        self.assertEqual(len(eff), 1)
        self.assertEqual(eff[0]["type"], "evaluation_correction")


    def test_adjusted_denominator_not_double_corrected(self):
        # Codex 反例: 発火の記録時点で base_day のファイルが既に分割後に取り直されていた
        # （entry_close=8000＝分割調整済み）。評価日 AdjC 9048 → 正解 +13.10%（×5 を掛けてはいけない）
        self.write_fetch_log({self.BASE: "2026-09-30", self.MID: "2026-09-30", self.EV: "2026-10-01"})
        f = {**self.firing, "recorded_at": "2026-10-01T03:00:00+00:00",
             "stocks": [{"code": "80350", "entry_close": 8000.0, "tier": "confirmed"}]}
        self.log.write_text(json.dumps(f) + "\n", encoding="utf-8")
        fwd.evaluate()
        r = [x for x in self.rows() if x["type"] == "evaluation"][0]["results"][0]
        self.assertEqual(r["ret_pct"], 13.1)
        self.assertNotIn("split_scale", r)

    def test_scale_base_restored_as_of_record_time(self):
        # base_day ファイルは発火記録（8/3）の後の 10/2 にも取り直された。記録時点の取得（8/3）の尺度で揃える
        self.write_fetch_log({self.BASE: "2026-08-03", self.MID: "2026-09-30", self.EV: "2026-10-01"})
        with open(self.root / "fetch_log.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "bars", "status": "saved", "date": self.BASE,
                                 "ts": "2026-10-02T07:30:00+09:00"}) + "\n")
        self._clear()
        f = {**self.firing, "recorded_at": "2026-08-03T02:00:00+00:00"}
        self.log.write_text(json.dumps(f) + "\n", encoding="utf-8")
        fwd.evaluate()
        r = [x for x in self.rows() if x["type"] == "evaluation"][0]["results"][0]
        self.assertEqual(r["ret_pct"], 13.1)

    def test_missing_bars_in_range_holds_evaluation(self):
        # 途中（分割日）のファイルが欠けている間は評価を確定しない → 揃ったら評価する
        (self.root / "bars" / f"{self.MID}.json.gz").unlink()
        self._clear()
        self.log.write_text(json.dumps(self.firing) + "\n", encoding="utf-8")
        fwd.evaluate()
        self.assertEqual([r for r in self.rows() if r["type"] == "evaluation"], [])
        self.write_day(self.MID, [bar("80350", c=9000.0, fac=0.2)])
        fwd.evaluate()
        ev = [r for r in self.rows() if r["type"] == "evaluation"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["results"][0]["ret_pct"], 13.1)

    def test_partial_correction_waits_until_all_prices(self):
        # 補正が要る 2 銘柄のうち 1 つの評価日価格が無い間は訂正行を書かない（一部だけ直して固定しない）
        self.write_day(self.BASE, [bar("80350", c=40000.0), bar("99990", c=1000.0)])
        self.write_day(self.MID, [bar("80350", c=9000.0, fac=0.2), bar("99990", c=500.0, fac=0.5)])
        self.write_day(self.EV, [bar("80350", c=9048.0)])
        f = {**self.firing, "stocks": [{"code": "80350", "entry_close": 40000.0, "tier": "confirmed"},
                                       {"code": "99990", "entry_close": 1000.0, "tier": "confirmed"}]}
        old = {"type": "evaluation", "spec_version": 3, "lane": "rise", "fire_date": "2026-08-02",
               "series_id": "semicon-equip", "window": "w8", "eval_day": self.EV, "topix_ret_pct": 4.0,
               "results": [{"code": "80350", "ret_pct": -77.38, "excess_pct": -81.38, "hit": False, "status": "ok"},
                           {"code": "99990", "ret_pct": -45.0, "excess_pct": -49.0, "hit": False, "status": "ok"}],
               "n_hit": 0, "n_scored": 2}
        self.log.write_text(json.dumps(f) + "\n" + json.dumps(old) + "\n" + json.dumps(old) + "\n",
                            encoding="utf-8")
        fwd.evaluate()
        self.assertEqual([r for r in self.rows() if r["type"] == "evaluation_correction"], [])
        self.write_day(self.EV, [bar("80350", c=9048.0), bar("99990", c=550.0)])
        fwd.evaluate()
        corr = [r for r in self.rows() if r["type"] == "evaluation_correction"]
        self.assertEqual(len(corr), 1)                   # 同じキーの評価行が2行あっても訂正行は1行
        got = {r["code"]: r["ret_pct"] for r in corr[0]["results"]}
        self.assertEqual(got, {"80350": 13.1, "99990": 10.0})   # 550×2/1000−1 = +10%


    def test_refetched_eval_with_scale_one_still_corrected(self):
        # Codex 2回目の反例: 分母は分割反映済み 500（base を分割後に取得）・元の評価は評価日の足が未反映で
        # 1100 → +120% と記録。その後評価日の足を取り直して 550＝今の倍率は 1。正解 +10% に訂正する
        self.write_day(self.BASE, [bar("80350", c=500.0)])
        self.write_day(self.MID, [bar("80350", c=520.0, fac=0.5)])
        self.write_day(self.EV, [bar("80350", c=550.0)])
        self.write_fetch_log({self.BASE: "2026-10-01", self.MID: "2026-09-30", self.EV: "2026-10-02"})
        # 評価日の足は 10/2 取得＝10/1 までの分割を反映しうる → 10/1 の bars まで揃って初めて確定できる
        self.bdays = [self.BASE, self.MID, self.EV, "20261001"]
        self.write_day("20261001", [bar("80350", c=560.0)])
        f = {**self.firing, "recorded_at": "2026-10-01T03:00:00+00:00",
             "stocks": [{"code": "80350", "entry_close": 500.0, "tier": "confirmed"}]}
        old = {"type": "evaluation", "spec_version": 3, "lane": "rise", "fire_date": "2026-08-02",
               "series_id": "semicon-equip", "window": "w8", "eval_day": self.EV, "topix_ret_pct": 4.0,
               "results": [{"code": "80350", "ret_pct": 120.0, "excess_pct": 116.0, "hit": True, "status": "ok"}],
               "n_hit": 1, "n_scored": 1}
        self.log.write_text(json.dumps(f) + "\n" + json.dumps(old) + "\n", encoding="utf-8")
        fwd.evaluate()
        corr = [r for r in self.rows() if r["type"] == "evaluation_correction"]
        self.assertEqual(len(corr), 1)
        self.assertEqual(corr[0]["results"][0]["ret_pct"], 10.0)


class TestPriceWatchCalendarRange(TmpBars):
    """評価日がカレンダー外（カレンダーの収録が足りない）なら「分割なし」と確定せず保留する（Codex 4回目）。"""
    BASE, SPL, EV = "20260731", "20260929", "20260930"

    def setUp(self):
        super().setUp()
        self.log = self.root / "forward_log.jsonl"
        self.bdays = [self.BASE, "20260928"]                   # カレンダー末尾 9/28＝評価日 9/30 を収録していない
        self.ps = [mock.patch.object(fwd, "LOG_PATH", self.log),
                   mock.patch.object(fwd, "business_days", lambda: self.bdays),
                   mock.patch.object(fwd, "load_topix", lambda: {self.BASE: 2000.0, self.EV: 2080.0})]
        for p in self.ps:
            p.start()

    def tearDown(self):
        for p in self.ps:
            p.stop()
        super().tearDown()

    def test_eval_day_outside_calendar_holds(self):
        self.write_day(self.BASE, [bar("80350", c=1000.0)])
        self.write_day("20260928", [bar("80350", c=1010.0)])
        self.write_day(self.SPL, [bar("80350", c=520.0, fac=0.5)])
        self.write_day(self.EV, [bar("80350", c=550.0)])
        self.write_fetch_log({self.BASE: "2026-08-03", "20260928": "2026-09-29", self.SPL: "2026-09-30",
                              self.EV: "2026-10-01"})
        f = {"type": "firing", "spec_version": 3, "lane": "rise", "fire_date": "2026-08-02", "series_id": "s",
             "series_jp": "s", "base_day": self.BASE, "topix_entry": 2000.0, "eval_days": {"w8": self.EV},
             "recorded_at": "2026-08-03T02:00:00+00:00",
             "stocks": [{"code": "80350", "entry_close": 1000.0, "tier": "confirmed"}]}
        self.log.write_text(json.dumps(f) + "\n", encoding="utf-8")
        self.assertIsNone(fwd._split_scan("80350", f, self.EV, self.bdays))   # 分割なし([])と答えない
        self.bdays = [self.BASE, "20260928", self.SPL, self.EV]               # カレンダー復旧
        splits = fwd._split_scan("80350", f, self.EV, self.bdays)
        self.assertEqual(splits, [(self.SPL, 0.5)])
        r = fwd._score_stock(f["stocks"][0], f, self.EV, self.bdays, 4.0)
        self.assertEqual(r["ret_pct"], 10.0)                                   # 550×2/1000−1


class TestPriceWatchPostEvalHole(TmpBars):
    """評価日より後の bars の欠け・未着で、取り直した評価日の足に効く分割を見落とさない（Codex 2・3回目）。"""
    BASE, EV = "20260731", "20260924"
    AFTER = ["20260925", "20260928", "20260929", "20260930", "20261001"]

    def setUp(self):
        super().setUp()
        self.log = self.root / "forward_log.jsonl"
        bdays = [self.BASE, self.EV] + self.AFTER
        self.ps = [mock.patch.object(fwd, "LOG_PATH", self.log),
                   mock.patch.object(fwd, "business_days", lambda: bdays),
                   mock.patch.object(fwd, "load_topix", lambda: {self.BASE: 2000.0, self.EV: 2080.0})]
        for p in self.ps:
            p.start()
        self.write_day(self.BASE, [bar("80350", c=1000.0)])
        self.write_day(self.EV, [bar("80350", c=550.0)])        # 10/2 に取り直し＝9/29 の 1:2 分割を反映済み
        f = {"type": "firing", "spec_version": 3, "lane": "rise", "fire_date": "2026-08-02", "series_id": "s",
             "series_jp": "s", "base_day": self.BASE, "topix_entry": 2000.0, "eval_days": {"w8": self.EV},
             "recorded_at": "2026-08-03T02:00:00+00:00",
             "stocks": [{"code": "80350", "entry_close": 1000.0, "tier": "confirmed"}]}
        self.log.write_text(json.dumps(f) + "\n", encoding="utf-8")

    def tearDown(self):
        for p in self.ps:
            p.stop()
        super().tearDown()

    def evals(self):
        rows = [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if r["type"] == "evaluation"]

    def recover(self):
        self.write_day("20260925", [bar("80350", c=540.0)])
        self.write_day("20260928", [bar("80350", c=545.0)])
        self.write_day("20260929", [bar("80350", c=560.0, fac=0.5)])
        self.write_day("20260930", [bar("80350", c=555.0)])
        self.write_day("20261001", [bar("80350", c=558.0)])

    def test_latest_file_before_split_holds_then_scores(self):
        # Codex 3回目の再現: 入口 1000・評価日 9/24（10/2 取り直し 550）・最新ファイル 9/28・9/29 に 1:2 分割
        self.write_day("20260925", [bar("80350", c=540.0)])
        self.write_day("20260928", [bar("80350", c=545.0)])
        self.write_fetch_log({self.BASE: "2026-08-03", self.EV: "2026-10-02",
                              "20260925": "2026-09-26", "20260928": "2026-09-29"})
        fwd.evaluate()                                          # 分割日のファイルが未着の間は確定しない
        self.assertEqual(self.evals(), [])
        self.recover()
        self.write_fetch_log({self.BASE: "2026-08-03", self.EV: "2026-10-02", "20260925": "2026-09-26",
                              "20260928": "2026-09-29", "20260929": "2026-09-30", "20260930": "2026-10-01",
                              "20261001": "2026-10-02"})
        fwd.evaluate()
        self.assertEqual(self.evals()[0]["results"][0]["ret_pct"], 10.0)   # 550×2/1000−1

    def test_hold_until_post_eval_hole_filled(self):
        # 評価日の後（9/25）だけ欠けている間も確定しない
        self.recover()
        (self.root / "bars" / "20260925.json.gz").unlink()
        self._clear()
        self.write_fetch_log({self.BASE: "2026-08-03", self.EV: "2026-10-02", "20260929": "2026-09-30"})
        fwd.evaluate()
        self.assertEqual(self.evals(), [])
        self.write_day("20260925", [bar("80350", c=540.0)])
        fwd.evaluate()
        self.assertEqual(self.evals()[0]["results"][0]["ret_pct"], 10.0)


# --- 4. ニュースレーンの分割補正 ---------------------------------------------------------


class TestNewsShockSplitCorrection(TmpBars):
    def test_old_row_corrected_once(self):
        bars = self.root / "bars"
        self.write_day("20260918", [bar("57060", c=21000.0, o=21735.0)])
        self.write_day("20260929", [bar("57060", c=2300.0, fac=0.1)])
        self.write_day("20260930", [bar("57060", c=2291.0)])
        self.write_fetch_log({"20260918": "2026-09-19", "20260929": "2026-09-30", "20260930": "2026-10-01"})
        self.write_calendar(["20260918", "20260929", "20260930"])
        with gzip.open(self.root / "topix.json.gz", "wt") as fh:
            json.dump({"data": [{"Date": "2026-09-18", "C": 2000.0}, {"Date": "2026-09-30", "C": 2008.6}]}, fh)
        led = self.root / "news_log.jsonl"
        old = {"type": "evaluation", "link": "http://x", "code": "5706", "window": "w5",
               "entry_day": "20260918", "eval_day": "20260930", "entry_open": 21735.0,
               "stock_pct": -89.46, "topix_pct": 0.43, "excess_pt": -89.89, "run_at": "2026-10-01T22:20:14+00:00"}
        hit = {"run_at": "2026-09-17T22:00:00+00:00", "link": "http://x", "status": "hit",
               "beneficiaries": [{"code": "5706", "tier": "provisional", "type_label": "数量型"}]}
        led.write_text(json.dumps(hit) + "\n" + json.dumps(old) + "\n", encoding="utf-8")
        NSE.evaluate(led, bars, self.root / "topix.json.gz")
        NSE.evaluate(led, bars, self.root / "topix.json.gz")
        rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines()]
        corr = [r for r in rows if r.get("type") == "evaluation_correction"]
        self.assertEqual(len(corr), 1)
        # 2291 × 10 / 21735 − 1 = +5.41% → 超過 +5.41 − 0.43 = +4.98pt
        self.assertEqual(corr[0]["stock_pct"], 5.41)
        self.assertEqual(corr[0]["excess_pt"], 4.98)
        self.assertEqual(corr[0]["before"]["stock_pct"], -89.46)
        self.assertEqual(len([r for r in rows if r.get("type") == "evaluation"]), 1)


    def test_old_row_with_adjusted_entry_is_left_alone(self):
        # 元の評価時点（10/2）で基準日ファイルは分割後の取り直し済み（entry_open=2173.5＝調整済み）→ 訂正しない
        bars = self.root / "bars"
        self.write_day("20260918", [bar("57060", c=2100.0, o=2173.5)])
        self.write_day("20260929", [bar("57060", c=2300.0, fac=0.1)])
        self.write_day("20260930", [bar("57060", c=2291.0)])
        self.write_fetch_log({"20260918": "2026-10-01", "20260929": "2026-09-30", "20260930": "2026-10-01"})
        self.write_calendar(["20260918", "20260929", "20260930"])
        with gzip.open(self.root / "topix.json.gz", "wt") as fh:
            json.dump({"data": [{"Date": "2026-09-18", "C": 2000.0}, {"Date": "2026-09-30", "C": 2008.6}]}, fh)
        led = self.root / "news_log.jsonl"
        hit = {"run_at": "2026-09-17T22:00:00+00:00", "link": "http://x", "status": "hit",
               "beneficiaries": [{"code": "5706", "tier": "provisional", "type_label": "数量型"}]}
        old = {"type": "evaluation", "link": "http://x", "code": "5706", "window": "w5",
               "entry_day": "20260918", "eval_day": "20260930", "entry_open": 2173.5,
               "stock_pct": 5.41, "topix_pct": 0.43, "excess_pt": 4.98, "run_at": "2026-10-02T00:00:00+00:00"}
        led.write_text(json.dumps(hit) + "\n" + json.dumps(old) + "\n", encoding="utf-8")
        NSE.evaluate(led, bars, self.root / "topix.json.gz")
        rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r for r in rows if r.get("type") == "evaluation_correction"], [])


    def test_refetched_eval_with_scale_one_still_corrected(self):
        # 分母は調整済み 500・元の評価は評価日の足が未反映で +120% → 取り直した今の足 550（倍率 1）で +10% に訂正
        bars = self.root / "bars"
        self.write_day("20260918", [bar("57060", c=480.0, o=500.0)])
        self.write_day("20260929", [bar("57060", c=520.0, fac=0.5)])
        self.write_day("20260930", [bar("57060", c=550.0)])
        self.write_day("20261001", [bar("57060", c=560.0)])     # 評価日の足は 10/2 取得＝10/1 まで走査が要る
        self.write_fetch_log({"20260918": "2026-09-30", "20260929": "2026-09-30", "20260930": "2026-10-02"})
        self.write_calendar(["20260918", "20260929", "20260930", "20261001"])
        with gzip.open(self.root / "topix.json.gz", "wt") as fh:
            json.dump({"data": [{"Date": "2026-09-18", "C": 2000.0}, {"Date": "2026-09-30", "C": 2000.0}]}, fh)
        led = self.root / "news_log.jsonl"
        hit = {"run_at": "2026-09-17T22:00:00+00:00", "link": "http://x", "status": "hit",
               "beneficiaries": [{"code": "5706", "tier": "provisional", "type_label": "数量型"}]}
        old = {"type": "evaluation", "link": "http://x", "code": "5706", "window": "w5",
               "entry_day": "20260918", "eval_day": "20260930", "entry_open": 500.0,
               "stock_pct": 120.0, "topix_pct": 0.0, "excess_pt": 120.0, "run_at": "2026-10-01T00:00:00+00:00"}
        led.write_text(json.dumps(hit) + "\n" + json.dumps(old) + "\n", encoding="utf-8")
        NSE.evaluate(led, bars, self.root / "topix.json.gz")
        rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines()]
        corr = [r for r in rows if r.get("type") == "evaluation_correction"]
        self.assertEqual(len(corr), 1)
        self.assertEqual(corr[0]["stock_pct"], 10.0)


    def test_missing_split_day_holds_then_corrects(self):
        # Codex 3回目: 分割日（9/29）のファイルが欠けている間は訂正を書かない（カレンダーで欠けを検出）。
        # 欠けが戻った後に正しい値で訂正できる（保留が「訂正済み」として固定されない）
        bars = self.root / "bars"
        self.write_day("20260918", [bar("57060", c=21000.0, o=21735.0)])
        self.write_day("20260930", [bar("57060", c=2291.0)])
        self.write_fetch_log({"20260918": "2026-09-19", "20260929": "2026-09-30", "20260930": "2026-10-01"})
        self.write_calendar(["20260918", "20260929", "20260930"])
        with gzip.open(self.root / "topix.json.gz", "wt") as fh:
            json.dump({"data": [{"Date": "2026-09-18", "C": 2000.0}, {"Date": "2026-09-30", "C": 2008.6}]}, fh)
        led = self.root / "news_log.jsonl"
        hit = {"run_at": "2026-09-17T22:00:00+00:00", "link": "http://x", "status": "hit",
               "beneficiaries": [{"code": "5706", "tier": "provisional", "type_label": "数量型"}]}
        old = {"type": "evaluation", "link": "http://x", "code": "5706", "window": "w5",
               "entry_day": "20260918", "eval_day": "20260930", "entry_open": 21735.0,
               "stock_pct": -89.46, "topix_pct": 0.43, "excess_pt": -89.89, "run_at": "2026-10-01T22:20:14+00:00"}
        led.write_text(json.dumps(hit) + "\n" + json.dumps(old) + "\n", encoding="utf-8")
        NSE.evaluate(led, bars, self.root / "topix.json.gz")
        rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r for r in rows if r.get("type") == "evaluation_correction"], [])
        self.write_day("20260929", [bar("57060", c=2300.0, fac=0.1)])
        NSE.evaluate(led, bars, self.root / "topix.json.gz")
        rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines()]
        corr = [r for r in rows if r.get("type") == "evaluation_correction"]
        self.assertEqual(len(corr), 1)
        self.assertEqual(corr[0]["stock_pct"], 5.41)

    def test_calendar_start_after_entry_holds_then_corrects(self):
        # Codex 4回目の再現: 入口 9/18・1000、9/24 と 9/29 に各 1:2 分割、評価価格 275、カレンダー開始 9/25
        # → 9/24 の分割を見落として −45% に訂正しない（保留）。カレンダー復旧後 +10% に訂正する
        bars = self.root / "bars"
        self.write_day("20260918", [bar("57060", c=990.0, o=1000.0)])
        self.write_day("20260924", [bar("57060", c=500.0, fac=0.5)])
        self.write_day("20260925", [bar("57060", c=505.0)])
        self.write_day("20260929", [bar("57060", c=260.0, fac=0.5)])
        self.write_day("20260930", [bar("57060", c=275.0)])
        self.write_fetch_log({"20260918": "2026-09-19", "20260924": "2026-09-25", "20260925": "2026-09-26",
                              "20260929": "2026-09-30", "20260930": "2026-10-01"})
        self.write_calendar(["20260925", "20260929", "20260930"])
        with gzip.open(self.root / "topix.json.gz", "wt") as fh:
            json.dump({"data": [{"Date": "2026-09-18", "C": 2000.0}, {"Date": "2026-09-30", "C": 2000.0}]}, fh)
        led = self.root / "news_log.jsonl"
        hit = {"run_at": "2026-09-17T22:00:00+00:00", "link": "http://x", "status": "hit",
               "beneficiaries": [{"code": "5706", "tier": "provisional", "type_label": "数量型"}]}
        old = {"type": "evaluation", "link": "http://x", "code": "5706", "window": "w5",
               "entry_day": "20260918", "eval_day": "20260930", "entry_open": 1000.0,
               "stock_pct": -72.5, "topix_pct": 0.0, "excess_pt": -72.5, "run_at": "2026-10-01T22:20:14+00:00"}
        led.write_text(json.dumps(hit) + "\n" + json.dumps(old) + "\n", encoding="utf-8")
        NSE.evaluate(led, bars, self.root / "topix.json.gz")
        rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r for r in rows if r.get("type") == "evaluation_correction"], [])
        self.write_calendar(["20260918", "20260924", "20260925", "20260929", "20260930"])
        NSE.evaluate(led, bars, self.root / "topix.json.gz")
        rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines()]
        corr = [r for r in rows if r.get("type") == "evaluation_correction"]
        self.assertEqual(len(corr), 1)
        self.assertEqual(corr[0]["stock_pct"], 10.0)                           # 275×4/1000−1

    def test_no_calendar_holds(self):
        # カレンダーが無い（欠けを検査できない）時も訂正しない
        bars = self.root / "bars"
        self.write_day("20260918", [bar("57060", c=21000.0, o=21735.0)])
        self.write_day("20260929", [bar("57060", c=2300.0, fac=0.1)])
        self.write_day("20260930", [bar("57060", c=2291.0)])
        self.write_fetch_log({"20260918": "2026-09-19", "20260929": "2026-09-30", "20260930": "2026-10-01"})
        with gzip.open(self.root / "topix.json.gz", "wt") as fh:
            json.dump({"data": [{"Date": "2026-09-18", "C": 2000.0}, {"Date": "2026-09-30", "C": 2008.6}]}, fh)
        led = self.root / "news_log.jsonl"
        old = {"type": "evaluation", "link": "http://x", "code": "5706", "window": "w5",
               "entry_day": "20260918", "eval_day": "20260930", "entry_open": 21735.0,
               "stock_pct": -89.46, "topix_pct": 0.43, "excess_pt": -89.89, "run_at": "2026-10-01T22:20:14+00:00"}
        hit = {"run_at": "2026-09-17T22:00:00+00:00", "link": "http://x", "status": "hit",
               "beneficiaries": [{"code": "5706", "tier": "provisional", "type_label": "数量型"}]}
        led.write_text(json.dumps(hit) + "\n" + json.dumps(old) + "\n", encoding="utf-8")
        NSE.evaluate(led, bars, self.root / "topix.json.gz")
        rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r for r in rows if r.get("type") == "evaluation_correction"], [])


# --- 5. 本日レコメンドの除外表示 -----------------------------------------------------------


class TestDailyRecoExcludesFlagged(unittest.TestCase):
    def test_flagged_rows_excluded_and_counted(self):
        import types
        import build_daily_reco as BDR
        with tempfile.TemporaryDirectory() as td:
            led = Path(td) / "ledger.jsonl"
            rows = [{"kpi_name": "sell_reg_trigger_rebound", "code": "22220", "signal_date": "20260929",
                     "status": "open", "entry_date": "20260930", "split_false_signal": True},
                    {"kpi_name": "sell_reg_trigger_rebound", "code": "33330", "signal_date": "20260929",
                     "status": "open", "entry_date": "20260930"}]
            led.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            stub = types.ModuleType("build_shikomi_list")      # 仕込み型の節は本テストの対象外（失敗表示で通す）
            with mock.patch.object(BDR, "LEDGER_PATH", led), \
                    mock.patch.dict(sys.modules, {"build_shikomi_list": stub}):
                text, _summary = BDR.build()
        self.assertIn("偽合図 **1件**（うち保有中・エントリー前 1件）", text)
        self.assertNotIn("22220", text)


if __name__ == "__main__":
    unittest.main()
