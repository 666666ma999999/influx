#!/usr/bin/env python3
"""合格基準 v2（scripts/formal_excess_v2.py）の単体テスト。正本: tasks/topix_excess_criterion_v2.md T4。

実行: docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_formal_excess_v2
"""
from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import formal_excess_v2 as F  # noqa: E402


def _topix(days: list[tuple[str, float, float]]) -> F.Topix:
    return F.Topix([{"Date": d, "O": o, "C": c} for d, o, c in days])


class TestExcessAndInput(unittest.TestCase):
    def setUp(self):
        self.t = _topix([("2026-08-03", 100, 101), ("2026-08-04", 102, 104),
                         ("2026-08-31", 103, 110), ("2026-09-01", 111, 112)])

    def test_excess_hand(self):
        # ret 0.05、TOPIX 110/100−1=0.10 → 超過 0.05 − (0.10 − 0.003) = −0.047
        row = {"ret_nostop": 0.05, "entry_date": "20260803", "exit_date_nostop": "20260831"}
        self.assertAlmostEqual(F.row_excess(row, self.t), 0.05 - (0.10 - 0.003), places=12)

    def test_defer_rows_use_own_dates(self):
        r1 = {"ret_nostop": 0.0, "entry_date": "20260803", "exit_date_nostop": "20260831"}
        r2 = {"ret_nostop": 0.0, "entry_date": "20260804", "exit_date_nostop": "20260901"}
        e1, e2 = F.row_excess(r1, self.t), F.row_excess(r2, self.t)
        self.assertAlmostEqual(e1, -(110 / 100 - 1 - 0.003))
        self.assertAlmostEqual(e2, -(112 / 102 - 1 - 0.003))
        v = F.day_votes([("20260731", e1), ("20260731", e2)])
        self.assertAlmostEqual(v["20260731"], (e1 + e2) / 2)

    def test_missing_topix_is_pending(self):
        row = {"ret_nostop": 0.05, "entry_date": "20260803", "exit_date_nostop": "20260930"}
        self.assertIsNone(F.row_excess(row, self.t))

    def test_input_rejects(self):
        for bad in ([("2026-08-03", 0, 1)], [("2026-08-03", 1, -1)],
                    [("2026-08-03", 1, 1), ("2026-08-03", 1, 1)], [("2026-08-03", float("nan"), 1)],
                    [("2026-08-03", float("inf"), 1)]):
            with self.assertRaises(F.InputError):
                _topix(bad)

    def test_judgeable_rows(self):
        self.assertTrue(F.is_judgeable_row({"ret_nostop": 0.1, "entry_date": "x", "exit_date_nostop": "y"}))
        # stop8 で終わっても nostop が未満期なら対象外
        self.assertFalse(F.is_judgeable_row({"ret_stop8": -0.08, "ret_nostop": None, "entry_date": "x",
                                             "exit_date_nostop": None}))
        self.assertFalse(F.is_judgeable_row({"ret_nostop": 0.1, "entry_date": "x", "exit_date_nostop": "y",
                                             "split_false_signal": True}))

    def test_same_day_is_one_vote(self):
        v = F.day_votes([("20260803", 0.1), ("20260803", 0.3), ("20260901", -0.1)])
        self.assertEqual(v, {"20260803": 0.2, "20260901": -0.1})


class TestClusterT(unittest.TestCase):
    def test_cluster_key(self):
        self.assertEqual(F.cluster_key("202607"), -1)
        self.assertEqual(F.cluster_key("202608"), 0)
        self.assertEqual(F.cluster_key("202609"), 0)
        self.assertEqual(F.cluster_key("202610"), 1)
        self.assertEqual(F.cluster_key("202701"), 2)
        self.assertEqual(F.cluster_key("202606"), -1)
        self.assertEqual(F.cluster_key("202605"), -2)

    def test_cr1_hand(self):
        votes = {"20260715": 0.03, "20260805": 0.01, "20260910": 0.02, "20261005": -0.01, "20261120": 0.04}
        D = 5
        X = sum(votes.values()) / D
        e = {-1: 0.03 - X, 0: (0.01 - X) + (0.02 - X), 1: (-0.01 - X) + (0.04 - X)}
        G = 3
        V = G / (G - 1) * sum(x * x for x in e.values()) / D ** 2
        ct = F.cluster_t(votes)
        self.assertEqual(ct["G"], 3)
        self.assertAlmostEqual(ct["X"], X)
        self.assertAlmostEqual(ct["SE"], math.sqrt(V))
        from scipy import stats as st
        self.assertAlmostEqual(F.pvalue(votes), float(st.t.sf(X / math.sqrt(V), 2)))

    def test_se_zero(self):
        # 各クラスタの残差和が 0（同値）→ SE=0
        pos = {"20260805": 0.01, "20261005": 0.01, "20261205": 0.01}
        self.assertEqual(F.pvalue(pos), 0.0)
        zero = {"20260805": 0.0, "20261005": 0.0, "20261205": 0.0}
        self.assertEqual(F.pvalue(zero), 1.0)
        neg = {"20260805": -0.01, "20261005": -0.01, "20261205": -0.01}
        self.assertEqual(F.pvalue(neg), 1.0)

    def test_robust_min_months(self):
        votes = {f"2026{m:02d}05": 0.01 * (m % 3) for m in (8, 9, 10, 11, 12)}  # 5 か月
        r = F.robust_condition(votes, "sell_reg_trigger_rebound", 6)
        self.assertEqual(r["status"], "unmet")
        self.assertIn("fail_min_months", r["reason"])

    def test_alpha_eff_registered_and_not_looser(self):
        self.assertEqual(set(F.ALPHA_EFF), set(F.E20_ALPHA))
        for k, a in F.E20_ALPHA.items():
            self.assertLessEqual(F.ALPHA_EFF[k], a, k)
            self.assertGreater(F.ALPHA_EFF[k], 0, k)
        with self.assertRaises(KeyError):
            F.robust_condition({"20260805": 0.1}, "unknown_kpi", 1)


class TestV1Bootstrap(unittest.TestCase):
    def setUp(self):
        self.votes = {}
        vals = [0.02, -0.01, 0.03, 0.00, 0.04, -0.02, 0.01, 0.05, -0.03, 0.02, 0.01, 0.03]
        for i, v in enumerate(vals):
            self.votes[f"2026{8 + i:02d}05" if 8 + i <= 12 else f"2027{8 + i - 12:02d}05"] = v

    def test_reproducible(self):
        a = F.v1_centered_p(self.votes, 0.005)
        b = F.v1_centered_p(self.votes, 0.005)
        self.assertEqual(a["p"], b["p"])
        c1 = F.v1_percentile_ci(self.votes)
        c2 = F.v1_percentile_ci(self.votes)
        self.assertEqual(c1, c2)
        self.assertLess(c1["lower"], c1["upper"])

    def test_boundary_rule(self):
        p0 = F.v1_centered_p(self.votes, 0.5)["p"]
        r = F.v1_centered_p(self.votes, p0)  # α を p に重ねる → 境界再計算
        self.assertTrue(r["boundary_recalc"])
        self.assertEqual(r["n_boot"], F.V1_N_BOOT_BOUNDARY)

    def test_v1_kind(self):
        self.assertEqual(F.V1_KIND["sue_beat"], "ci_lower")
        self.assertEqual(F.V1_KIND["sell_reg_trigger_rebound"], "p")
        r = F.v1_condition(self.votes, "sue_beat", 6)
        self.assertIn("ci_lower", r)
        r = F.v1_condition({"20260805": 0.1}, "sue_beat", 6)
        self.assertEqual(r["status"], "unmet")

    def test_interim(self):
        self.assertEqual(F.interim_reject({"20260805": -0.1}, "sue_beat")["status"], "not_computable")
        neg = {f"2026{m:02d}05": -0.05 - 0.001 * m for m in (8, 9, 10)}
        self.assertEqual(F.interim_reject(neg, "sue_beat")["status"], "reject")

    def test_interim_quiet_one_month(self):
        # quiet（Q6）: 30 行到達が 1 か月に収まっても計算する。全部負なら棄却・非負なら継続
        one_neg = {"20260805": -0.02, "20260812": -0.01, "20260820": -0.03}
        self.assertEqual(F.interim_reject(one_neg, F.QUIET)["status"], "reject")
        one_pos = {"20260805": 0.02, "20260812": -0.01, "20260820": 0.03}
        self.assertEqual(F.interim_reject(one_pos, F.QUIET)["status"], "continue")
        self.assertEqual(F.interim_reject(one_neg, "sue_beat")["status"], "not_computable")


class TestLabelsAndCounting(unittest.TestCase):
    def test_scenario_label(self):
        self.assertEqual(F.scenario_label(True, True, True), "formal_pass")
        self.assertEqual(F.scenario_label(True, True, False), "formal_fail_excess")
        self.assertEqual(F.scenario_label(True, False, True), "formal_fail")
        self.assertEqual(F.scenario_label(False, True, True), "formal_fail")

    def test_combine_order(self):
        kw = dict(completeness_ok=True, pending=False, pending_deadline_passed=False, early_rejected=False)
        self.assertEqual(F.combine_label(**{**kw, "completeness_ok": False}, scenario_labels=["formal_pass"]),
                         "window_expired")
        self.assertEqual(F.combine_label(**{**kw, "pending": True}, scenario_labels=["formal_pass"]),
                         "formal_pending_topix")
        self.assertEqual(F.combine_label(**{**kw, "pending": True, "pending_deadline_passed": True},
                                         scenario_labels=["formal_pass"]), "window_expired")
        self.assertEqual(F.combine_label(**{**kw, "early_rejected": True}, scenario_labels=["formal_pass"]),
                         "formal_fail")
        self.assertEqual(F.combine_label(**kw, scenario_labels=["formal_pass", "formal_fail_excess", "formal_pass"]),
                         "window_expired")
        self.assertEqual(F.combine_label(**kw, scenario_labels=["formal_pass"] * 3), "formal_pass")

    def test_sue_pair(self):
        self.assertEqual(F.sue_pair_decision("met", "pending", None)["decision"], "pending")
        self.assertEqual(F.sue_pair_decision("met", "unmet", None), {"decision": "h1", "count": 1})
        self.assertEqual(F.sue_pair_decision("expired", "met", None), {"decision": "raw", "count": 1})
        self.assertEqual(F.sue_pair_decision("met", "met", 0.01)["decision"], "h1")
        self.assertEqual(F.sue_pair_decision("met", "met", -0.01)["decision"], "raw")
        self.assertEqual(F.sue_pair_decision("unmet", "unmet", None)["count"], 0)

    def test_count_formal_passes(self):
        V = F.PASS_RULE_VERSION
        rows = [
            {"kpi_name": "sales_beat", "verdict": "formal_pass"},                       # 版なし
            {"kpi_name": "sales_beat", "verdict": "pass", "pass_rule_version": V},       # 旧 pass
            {"kpi_name": "margin_expand_yoy", "verdict": "confirm_pass", "pass_rule_version": V},
            {"kpi_name": "turnover_rank_surge", "verdict": "formal_fail_excess", "pass_rule_version": V},
            {"kpi_name": "unknown_kpi", "verdict": "formal_pass", "pass_rule_version": V},
        ]
        self.assertEqual(F.count_formal_passes(rows), 0)
        rows.append({"kpi_name": "cfo_margin_improve", "verdict": "formal_pass", "pass_rule_version": V})
        self.assertEqual(F.count_formal_passes(rows), 1)
        # SUE: 採択印なしは数えない（ペア保留中に片側だけ書かれた行など）
        rows.append({"kpi_name": "sue_x_above200", "verdict": "formal_pass", "pass_rule_version": V})
        self.assertEqual(F.count_formal_passes(rows), 1)
        rows.append({"kpi_name": "sue_x_above200", "verdict": "formal_pass", "pass_rule_version": V,
                     "sue_pair_adopted": True})
        rows.append({"kpi_name": "sue_beat", "verdict": "formal_pass", "pass_rule_version": V,
                     "sue_pair_adopted": True})
        self.assertEqual(F.count_formal_passes(rows), 2)  # ペアは最大1本

    def test_count_requires_both_registries(self):
        V = F.PASS_RULE_VERSION
        row = {"kpi_name": "new_kpi_x", "verdict": "formal_pass", "pass_rule_version": V}
        F.E20_ALPHA["new_kpi_x"] = 0.001       # α だけ登録（較正なし）
        try:
            self.assertEqual(F.count_formal_passes([row]), 0)
            F.ALPHA_EFF["new_kpi_x"] = 0.0009
            self.assertEqual(F.count_formal_passes([row]), 1)
        finally:
            F.E20_ALPHA.pop("new_kpi_x", None)
            F.ALPHA_EFF.pop("new_kpi_x", None)

    def test_adverse_index(self):
        self.assertEqual(F.adverse_fixed_value([5, 1, 3, 2, 4, 6, 7, 8, 9, 10, 11]), 2)
        self.assertEqual(F.adverse_fixed_value([3.0]), 3.0)

    def test_lift_pending_and_value(self):
        rows = [{"signal_date": "20260805", "ret_nostop": 0.25}, {"signal_date": "20260905", "ret_nostop": 0.0}]
        self.assertEqual(F.lift_point(rows, {"202608": 0.1})["status"], "pending_base_rate")
        r = F.lift_point(rows, {"202608": 0.1, "202609": 0.1})
        self.assertAlmostEqual(r["lift"], 0.5 / 0.1)
        # gross 判定: 0.197 + 0.003 = 0.20 は当たり
        r = F.lift_point([{"signal_date": "20260805", "ret_nostop": 0.197}], {"202608": 0.5})
        self.assertEqual(r["hits"], 1)

    def test_frequency(self):
        rows = [{"signal_date": "20260805"}] * 6 + [{"signal_date": "20260725"}]
        self.assertAlmostEqual(F.frequency(rows, ["202608", "202609"]), 3.0)
        self.assertIsNone(F.frequency(rows, []))


class TestLanes(unittest.TestCase):
    def setUp(self):
        days = []
        import datetime as dt
        d = dt.date(2026, 8, 3)
        while len(days) < 60:
            if d.weekday() < 5:
                days.append((d.isoformat(), 100.0 + len(days), 100.5 + len(days)))
            d += dt.timedelta(days=1)
        self.t = _topix(days)
        self.days = [x[0].replace("-", "") for x in days]

    def test_pair_exit_is_20_bdays(self):
        ev = [{"event": "matured", "pair_id": "P1", "signal_date": "20260731",
               "outcome": {"entry_date": self.days[0], "primary_ret": 0.02, "exit_date": self.days[3]}}]
        r = F.pair_excess(ev, self.t)
        tr = self.t.c[self.days[20]] / self.t.o[self.days[0]] - 1
        self.assertAlmostEqual(r["items"][0][2], 0.02 - (tr - 0.003))

    def test_tob_censored_pending(self):
        deals = [{"deal_key": "A", "closed": True, "net": 0.01, "entry_day": self.days[0],
                  "exit_target": self.days[5], "censored": False},
                 {"deal_key": "B", "closed": True, "net": 0.01, "entry_day": self.days[0],
                  "exit_target": self.days[5], "censored": True}]
        r = F.tob_excess_by_month(deals, self.t)
        self.assertEqual(r["pending"], ["B"])
        self.assertEqual(sum(len(v) for v in r["by_month"].values()), 1)

    def test_influencer_first_call(self):
        d0, d1, d2 = self.days[0], self.days[1], self.days[7]   # d0・d1 は同じ ISO 週、d2 は翌週
        rows = [
            {"prospective": "True", "status": "scored", "post_date": d1, "code": "11110", "entry_date": d1, "exit_date": d2, "net_return": "0.5"},
            {"prospective": "True", "status": "scored", "post_date": d0, "code": "11110", "entry_date": d0, "exit_date": d2, "net_return": "0.1"},
            {"prospective": "True", "status": "scored", "post_date": d2, "code": "11110", "entry_date": d2, "exit_date": self.days[27], "net_return": "0.0"},
            {"prospective": "True", "status": "censored", "post_date": d0, "code": "22220", "entry_date": d0, "exit_date": "", "net_return": ""},
            {"prospective": "True", "status": "scored", "post_date": "20260701", "code": "33330", "entry_date": d0, "exit_date": d2, "net_return": "0.1"},
        ]
        r = F.influencer_excess(rows, self.t, "20260724", s33_of=lambda c, d: "3050")
        self.assertEqual((r["n_independent"], r["pending_clusters"]), (3, 1))  # 22220 は未成熟で保留
        tr = self.t.c[d2] / self.t.o[d0] - 1
        wk = f"11110|{F.iso_week(d0)}"
        got = {a: v for a, _, v in r["items"]}
        self.assertAlmostEqual(got[wk], 0.1 - (tr - 0.003))   # 同週の最初の適格コール（d0）を採る
        self.assertEqual(len(r["items"]), 2)
        self.assertTrue(all(b.endswith("|3050") for _, b, _ in r["items"]))

    def test_influencer_eligibility_edges(self):
        d0, d1, d2 = self.days[0], self.days[1], self.days[7]
        base = {"prospective": "True", "code": "44440", "exit_date": d2}
        # 先頭が約定不可（excluded）→ 次の scored を採る
        rows = [{**base, "status": "excluded", "post_date": d0, "entry_date": d0, "net_return": ""},
                {**base, "status": "scored", "post_date": d1, "entry_date": d1, "net_return": "0.2"}]
        r = F.influencer_excess(rows, self.t, "20260724")
        self.assertEqual(len(r["items"]), 1)
        self.assertAlmostEqual(r["items"][0][2], 0.2 - (self.t.c[d2] / self.t.o[d1] - 1 - 0.003))
        # 先頭が未成熟（censored）→ クラスタごと保留（後続の scored で置き換えない）
        rows = [{**base, "status": "censored", "post_date": d0, "entry_date": d0, "net_return": ""},
                {**base, "status": "scored", "post_date": d1, "entry_date": d1, "net_return": "0.2"}]
        r = F.influencer_excess(rows, self.t, "20260724")
        self.assertEqual((len(r["items"]), r["pending_clusters"]), (0, 1))
        # prospective でない行はコールでない
        rows = [{**base, "prospective": "False", "status": "scored", "post_date": d0, "entry_date": d0,
                 "net_return": "0.2"}]
        r = F.influencer_excess(rows, self.t, "20260724")
        self.assertEqual((r["n_prospective"], len(r["items"])), (0, 0))

    def test_influencer_v2_needs_both_clusters(self):
        items = [(f"c{i}|W", "202608|A", 0.05 + 0.001 * i) for i in range(30)]
        r = F.influencer_v2_condition(items)
        self.assertEqual(r["status"], "met")
        # 主クラスタ（62 個）は下限>0 だが、副クラスタは 2 個（B が負）で下限<0 → 必ず未達（AND）
        items = [(f"c{i}|W", "202608|A", 0.05) for i in range(60)] + \
                [("x1|W", "202609|B", -0.3), ("x2|W", "202609|B", -0.3)]
        r = F.influencer_v2_condition(items)
        self.assertGreater(r["lower95_code_week"], 0)
        self.assertLess(r["lower95_month_s33"], 0)
        self.assertEqual(r["status"], "unmet")


if __name__ == "__main__":
    unittest.main()
