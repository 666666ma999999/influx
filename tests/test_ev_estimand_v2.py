"""EV estimand v2（tasks/ev_estimand_v2_preregister.md R4）の検証テスト。

1. 回帰: bootstrap_ev_ci の省略時挙動が変更前fixtureと完全一致（R4 §9）
2. two-stage 点推定の手計算一致（R4 §1）
3. admission_ev の fail-closed（R4 §5: v1へのsilent fallback拒否）
4. ev_v2_summary の欠測・異常系（R4 §7）
"""

import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from kpi_event_study import admission_ev, bootstrap_ev_ci, ev_v2_summary  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "bootstrap_ev_ci_v1_expected.json"


def _fixture_df() -> pd.DataFrame:
    """fixture採取時（2026-08-01・変更前コード）と同一の合成データを再構築する。"""
    rng = np.random.default_rng(7)
    n = 120
    months = [f"2021{m:02d}" for m in range(1, 13)]
    return pd.DataFrame({
        "month": [months[i % 12] for i in range(n)],
        "ret": rng.normal(0.01, 0.08, n),
        "ret_stop8": rng.normal(0.005, 0.05, n),
    })


class TestV1Regression(unittest.TestCase):
    def test_default_behavior_unchanged(self):
        expected = json.loads(FIXTURE.read_text())["results"]
        df = _fixture_df()
        for col in ("ret", "ret_stop8"):
            for lvl in (0.95, 0.90):
                r = bootstrap_ev_ci(df, ev_column=col, cost=0.003, n_boot=200, seed=42, ci_level=lvl)
                got = {k: (round(v, 12) if isinstance(v, float) else v) for k, v in r.items()}
                self.assertEqual(got, expected[f"{col}@{lvl}"], f"{col}@{lvl} が変更前挙動と不一致")


class TestTwoStage(unittest.TestCase):
    def test_point_estimate_month_equal(self):
        small = pd.DataFrame({"month": ["202101", "202101", "202102"], "ret": [0.10, 0.20, 0.30]})
        r = bootstrap_ev_ci(small, ev_column="ret", cost=0.0, n_boot=10, seed=1, month_equal_weight=True)
        self.assertAlmostEqual(r["point_ev"], ((0.10 + 0.20) / 2 + 0.30) / 2, places=12)

    def test_cost_subtracted_once(self):
        small = pd.DataFrame({"month": ["202101"], "ret": [0.10]})
        r = bootstrap_ev_ci(small, ev_column="ret", cost=0.003, n_boot=5, seed=1, month_equal_weight=True)
        self.assertAlmostEqual(r["point_ev"], 0.097, places=12)


class TestAdmissionEv(unittest.TestCase):
    def test_raises_on_v1_only_entry(self):
        with self.assertRaises(ValueError):
            admission_ev({"kpi_name": "x", "in_sample": {"ev_none": 0.01}})

    def test_raises_on_not_computed(self):
        with self.assertRaises(ValueError):
            admission_ev({"kpi_name": "x", "in_sample": {"estimand_v2": {"status": "not_computed", "reason": "no_returns_csv"}}})

    def test_returns_computed(self):
        ev2 = {"status": "computed", "ev_none_v2": 0.01}
        self.assertIs(admission_ev({"kpi_name": "x", "in_sample": {"estimand_v2": ev2}}), ev2)


class TestEvV2Summary(unittest.TestCase):
    def test_missing_column(self):
        df = pd.DataFrame({"month": ["202101"], "ret": [0.1]})
        r = ev_v2_summary(df, ev_column="ret_stop8", cost=0.0)
        self.assertEqual(r, {"status": "not_computed", "reason": "missing_columns"})

    def test_all_nonfinite(self):
        df = pd.DataFrame({"month": ["202101", "202102"], "ret": [float("nan"), float("inf")]})
        r = ev_v2_summary(df, ev_column="ret", cost=0.0)
        self.assertEqual(r["status"], "not_computed")
        self.assertEqual(r["reason"], "empty_after_nonfinite_filter")
        self.assertEqual(r["n_excluded_nonfinite"], 2)

    def test_empty_input(self):
        df = pd.DataFrame({"month": pd.Series(dtype=str), "ret": pd.Series(dtype=float)})
        r = ev_v2_summary(df, ev_column="ret", cost=0.0)
        self.assertEqual(r["reason"], "empty_after_in_universe_filter")

    def test_computed_excludes_nonfinite(self):
        df = pd.DataFrame({"month": ["202101", "202101", "202102"], "ret": [0.10, float("nan"), 0.30]})
        r = ev_v2_summary(df, ev_column="ret", cost=0.0)
        self.assertEqual(r["status"], "computed")
        self.assertEqual(r["n_used"], 2)
        self.assertEqual(r["n_excluded_nonfinite"], 1)
        self.assertEqual(r["months_spanned"], 2)
        self.assertAlmostEqual(r["ev_v2"], (0.10 + 0.30) / 2, places=12)


if __name__ == "__main__":
    unittest.main()
