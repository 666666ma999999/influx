"""鉱工業指数 業種別 在庫率（季節調整済）の取得器と下落側の発火判定（P-INF-29 案 4・2026-10-04）。

- monthly_sources.parse_iip_inventory_ratio: e-Stat の b2020_gsm1j.xlsx『在庫率』シートの型（2026-10-04 実測）を
  手書き fixture で再現し、fail-closed の各分岐を検査する（ネットワーク不要）
- price_universe_check.monthly_triggers: alert.yoy_pct_down（下落側）のエピソード規則と、既存の上昇判定が不変なこと
- monthly_sources.parse_jmtba(part=...): 工作機械受注の内需・外需の内訳
- configs/price_universe_sources.json: 新系列が受益カード無し・下落閾値つきで登録されていること
実行: docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_iip_inventory_ratio
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import monthly_sources as ms  # noqa: E402
import price_universe_check as puc  # noqa: E402

# 2025-07〜2026-08 の 14 ヶ月（前年同月比と「前月の前年同月比」が両方計算できる最小幅）
MONTHS = [f"2025{m:02d}" for m in range(7, 13)] + [f"2026{m:02d}" for m in range(1, 9)]


def book(title: str = "項目：総合季節調整済指数【月次】在庫率（2020＝100.0）", last_label: str = "p 202608",
         rows: list | None = None) -> list:
    header = ["品目番号", "品目名称", "在庫率ウエイト"] + [int(m) for m in MONTHS[:-1]] + [last_label]
    if rows is None:
        rows = [
            # 電子部品: 前年比 = 90/100-1 = -10%、前月の前年比 = 99/100-1 = -1%
            (1105000000, "電子部品・デバイス工業", 479.0, 100.0, 100.0) + (100.0,) * 10 + (99.0, 90.0),
            # 当月だけ *** の業種（化学工業＝医薬品込みの型）
            (1109000000, "化学工業", 1894.2) + (100.0,) * 13 + ("***",),
            # 桁違い
            (1199000000, "桁違い工業", 1.0) + (100.0,) * 13 + (5000.0,),
        ]
    return [(title,), (None, None, "時系列コード"), tuple(header)] + [tuple(r) for r in rows]


class TestParseIipInventoryRatio(unittest.TestCase):
    def test_latest_yoy_prev_yoy_and_preliminary(self):
        r = ms.parse_iip_inventory_ratio(book(), "1105000000", "電子部品・デバイス工業", "2026-10")
        self.assertIsNotNone(r)
        self.assertEqual(r["value"], 90.0)
        self.assertEqual(r["src_date"], "2026-08")
        self.assertEqual(r["yoy_pct"], -10.0)
        self.assertEqual(r["prev_yoy_pct"], -1.0)
        self.assertAlmostEqual(r["monthly_pct"], -9.09, places=2)
        self.assertTrue(r["preliminary"])
        self.assertEqual(r["layout"], "iip_gsm1j_zaikoritsu_v1")

    def test_confirmed_month_is_not_preliminary(self):
        r = ms.parse_iip_inventory_ratio(book(last_label="202608"), "1105000000")
        self.assertFalse(r["preliminary"])

    def test_title_must_be_seasonally_adjusted_inventory_ratio(self):
        self.assertIsNone(ms.parse_iip_inventory_ratio(
            book(title="項目：総合原指数【月次】在庫率（2020＝100.0）"), "1105000000"))
        self.assertIsNone(ms.parse_iip_inventory_ratio(
            book(title="項目：総合季節調整済指数【月次】在庫（2020＝100.0）"), "1105000000"))

    def test_unknown_code_or_renamed_industry_is_none(self):
        self.assertIsNone(ms.parse_iip_inventory_ratio(book(), "1234567890"))
        self.assertIsNone(ms.parse_iip_inventory_ratio(book(), "1105000000", "電気機械工業"))

    def test_latest_month_masked_does_not_fall_back_to_previous_month(self):
        """当月が *** の業種を前月値で『最新』と見せない。"""
        self.assertIsNone(ms.parse_iip_inventory_ratio(book(), "1109000000"))

    def test_out_of_range_value_is_none(self):
        self.assertIsNone(ms.parse_iip_inventory_ratio(book(), "1199000000"))

    def test_stale_book_is_none(self):
        self.assertIsNone(ms.parse_iip_inventory_ratio(book(), "1105000000", today="2027-01"))
        self.assertIsNotNone(ms.parse_iip_inventory_ratio(book(), "1105000000", today="2026-12"))

    def test_unsorted_month_header_is_none(self):
        b = book()
        hdr = list(b[2])
        hdr[3], hdr[4] = hdr[4], hdr[3]
        b[2] = tuple(hdr)
        self.assertIsNone(ms.parse_iip_inventory_ratio(b, "1105000000"))

    def test_blank_month_header_in_the_middle_is_none(self):
        """空ヘッダで値が1列ずれるのを通さない（parse_sheet は空ヘッダを捨てるが値は詰めない）。"""
        b = book()
        hdr = list(b[2])
        hdr[5] = None
        b[2] = tuple(hdr)
        self.assertIsNone(ms.parse_iip_inventory_ratio(b, "1105000000"))

    def test_month_gap_in_header_is_none(self):
        b = book()
        hdr = list(b[2])
        hdr[5] = 202512  # 202509 の位置に飛んだ月（昇順のまま・連続でない）
        hdr[6], hdr[7], hdr[8] = 202601, 202602, 202603
        b[2] = tuple(hdr)
        self.assertIsNone(ms.parse_iip_inventory_ratio(b, "1105000000"))

    def test_prev_monthly_pct(self):
        r = ms.parse_iip_inventory_ratio(book(), "1105000000")
        self.assertEqual(r["prev_monthly_pct"], -1.0)  # 7月 99 / 6月 100

    def test_missing_year_ago_month_gives_none_ratio(self):
        rows = [(1105000000, "電子部品・デバイス工業", 479.0, "X", "X") + (100.0,) * 10 + (99.0, 90.0)]
        r = ms.parse_iip_inventory_ratio(book(rows=rows), "1105000000")
        self.assertIsNone(r["yoy_pct"])
        self.assertIsNone(r["prev_yoy_pct"])
        self.assertEqual(r["value"], 90.0)


def hrow(src_date: str, yoy: float) -> dict:
    return {"id": "s", "status": "ok", "src_date": src_date, "yoy_pct": yoy, "monthly_pct": 0.0,
            "value": 100.0, "date": src_date + "-28", "run_at": ""}


class TestDownTrigger(unittest.TestCase):
    TH = {"monthly_pct": 999.0, "yoy_pct_down": -10.0}

    def test_fires_once_on_new_crossing_then_silent_then_refires(self):
        hist: list[dict] = []
        fired = []
        for ym, yoy in [("2026-01", -5.0), ("2026-02", -10.0), ("2026-03", -14.0),
                        ("2026-04", -3.0), ("2026-05", -11.0)]:
            t = puc.monthly_triggers({"src_date": ym, "yoy_pct": yoy, "monthly_pct": 0.0, "value": 90.0},
                                     "ok", hist, self.TH)
            fired.append(bool(t))
            hist.append(hrow(ym, yoy))
        self.assertEqual(fired, [False, True, False, False, True])

    def test_first_observation_uses_prev_from_same_file(self):
        """台帳に前月が無い初回は、同じファイルの前月の前年比で『局面の途中か』を判定する。"""
        cont = {"src_date": "2026-08", "yoy_pct": -15.5, "prev_yoy_pct": -10.3, "monthly_pct": 1.0}
        self.assertEqual(puc.monthly_triggers(cont, "ok", [], self.TH), [])
        new = {"src_date": "2026-08", "yoy_pct": -15.5, "prev_yoy_pct": -3.0, "monthly_pct": 1.0}
        t = puc.monthly_triggers(new, "ok", [], self.TH)
        self.assertEqual(len(t), 1)
        self.assertIn("前年同月比 -15.50%(2026-08分・低下側 閾値-10.0%以下)", t[0])

    def test_ledger_history_wins_over_file_prev(self):
        hist = [hrow("2026-07", -12.0)]
        p = {"src_date": "2026-08", "yoy_pct": -15.0, "prev_yoy_pct": -3.0, "monthly_pct": 0.0}
        self.assertEqual(puc.monthly_triggers(p, "ok", hist, self.TH), [])

    def test_ledger_gap_falls_back_to_file_prev_not_older_month(self):
        """台帳の前月が取得漏れなら、もっと古い月ではなく同じファイルの前月で比べる（Codex 2026-10-04）。"""
        hist = [hrow("2026-06", -3.0)]   # 7月は取得漏れ
        cont = {"src_date": "2026-08", "yoy_pct": -15.0, "prev_yoy_pct": -12.0, "monthly_pct": 0.0}
        self.assertEqual(puc.monthly_triggers(cont, "ok", hist, self.TH), [])
        new = {"src_date": "2026-08", "yoy_pct": -15.0, "prev_yoy_pct": -4.0, "monthly_pct": 0.0}
        self.assertEqual(len(puc.monthly_triggers(new, "ok", hist, self.TH)), 1)

    def test_unknown_previous_does_not_fire(self):
        """比較相手が台帳にもファイルにも無い＝新規か継続か判別不能 → 鳴らさない（fail-closed）。"""
        p = {"src_date": "2026-08", "yoy_pct": -15.0, "monthly_pct": 0.0}
        self.assertEqual(puc.monthly_triggers(p, "ok", [], self.TH), [])

    def test_january_compares_with_december(self):
        hist = [hrow("2025-12", -12.0)]
        p = {"src_date": "2026-01", "yoy_pct": -15.0, "prev_yoy_pct": -1.0, "monthly_pct": 0.0}
        self.assertEqual(puc.monthly_triggers(p, "ok", hist, self.TH), [])

    def test_status_and_same_month_guards(self):
        p = {"src_date": "2026-08", "yoy_pct": -15.0, "monthly_pct": 0.0}
        self.assertEqual(puc.monthly_triggers(p, "suspect_jump", [], self.TH), [])
        self.assertEqual(puc.monthly_triggers(p, "ok", [hrow("2026-08", -15.0)], self.TH), [])

    def test_down_keys_absent_keeps_existing_rise_behavior(self):
        """*_down を持たない既存系列は、大きな下落でも鳴らない（従来どおり上昇だけ）。"""
        th = {"monthly_pct": 1.0, "yoy_pct": 3.0}
        p = {"src_date": "2026-08", "yoy_pct": -40.0, "monthly_pct": -20.0}
        self.assertEqual(puc.monthly_triggers(p, "ok", [], th), [])
        up = {"src_date": "2026-08", "yoy_pct": 4.0, "monthly_pct": 0.0}
        self.assertEqual(len(puc.monthly_triggers(up, "ok", [], th)), 1)

    def test_monthly_down_key(self):
        th = {"monthly_pct": 999.0, "monthly_pct_down": -5.0}
        p = {"src_date": "2026-08", "monthly_pct": -6.0, "prev_monthly_pct": 1.0}
        t = puc.monthly_triggers(p, "ok", [], th)
        self.assertEqual(len(t), 1)
        self.assertIn("前月比 -6.00%", t[0])


JMTBA_TXT = ("受注総額\n2026年8月分　受注速報\n26/8月 前月比 前年同月比 2026年累計 前年同期比\n"
             "197,880 102.5 164.7 1,446,111 140.9\n"
             "うち内需 51,533 96.8 161.6 377,666 130.4\n"
             "うち外需 146,347 104.6 165.8 1,068,445 145.1\n")  # 2026-10-04 実PDFの pypdf 抽出そのまま


class TestJmtbaParts(unittest.TestCase):
    def test_parts(self):
        t = ms.parse_jmtba(JMTBA_TXT)
        d = ms.parse_jmtba(JMTBA_TXT, "domestic")
        f = ms.parse_jmtba(JMTBA_TXT, "foreign")
        self.assertEqual((t["value"], t["yoy_pct"]), (197880.0, 64.7))
        self.assertEqual((d["value"], d["yoy_pct"], d["monthly_pct"]), (51533.0, 61.6, -3.2))
        self.assertEqual((f["value"], f["yoy_pct"]), (146347.0, 65.8))
        self.assertEqual(d["src_date"], "2026-08")

    def test_parts_are_selected_by_label_not_position(self):
        lines = JMTBA_TXT.splitlines()
        swapped = "\n".join(lines[:4] + [lines[5], lines[4]]) + "\n"  # 内需と外需の行順を入れ替え
        self.assertEqual(ms.parse_jmtba(swapped, "domestic")["value"], 51533.0)
        self.assertEqual(ms.parse_jmtba(swapped, "foreign")["value"], 146347.0)
        self.assertIsNone(ms.parse_jmtba(JMTBA_TXT.replace("うち内需", ""), "domestic"))  # ラベル欠落

    def test_unknown_part_and_sum_check(self):
        self.assertIsNone(ms.parse_jmtba(JMTBA_TXT, "asia"))
        self.assertIsNone(ms.parse_jmtba(JMTBA_TXT.replace("51,533", "61,533"), "domestic"))


class TestConfigRegistration(unittest.TestCase):
    def test_new_series_are_environment_indicators(self):
        cfg = json.loads((ROOT / "configs" / "price_universe_sources.json").read_text(encoding="utf-8"))
        iip = [s for s in cfg["series"] if s["type"] == "iip_inventory_ratio"]
        self.assertTrue(5 <= len(iip) <= 7)
        for s in iip:
            self.assertEqual(s["beneficiaries"], [], s["id"])
            self.assertEqual(s["cadence"], "monthly")
            self.assertLess(s["alert"]["yoy_pct_down"], 0)
            self.assertGreaterEqual(s["alert"]["monthly_pct"], 999.0)  # 上昇側は無効
            self.assertRegex(s["iip_code"], r"^\d{10}$")
            self.assertIn("成績の主張禁止", s["note"])
        parts = {s.get("part") for s in cfg["series"] if s["type"] == "jmtba"}
        self.assertEqual(parts, {None, "domestic", "foreign"})
        ids = [s["id"] for s in cfg["series"]]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
