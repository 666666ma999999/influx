"""ResearchScorecardBuilder の `_calc_score` と `rank_influencers` の契約テスト。

plan.md M0 T0.3 / T0.5 で明示した:
- score = win_rate * min(trackable / 10, 1.0) の 0-100 スケール
- ランキングソートキー: (score, avg_return_pct) 降順（win_rate は tiebreak に含めない）
を SST として固定する。

実行:
  docker compose run --rm xstock python -m unittest tests.test_research_scorecard -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extensions.tier1_collection.grok_discoverer.research_scorecard import (
    ResearchScorecardBuilder,
)


class TestCalcScore(unittest.TestCase):
    """`_calc_score` の境界ケース。"""

    def test_zero_win_rate(self):
        self.assertEqual(
            ResearchScorecardBuilder._calc_score({"win_rate": 0.0, "trackable": 20}), 0.0
        )

    def test_zero_trackable(self):
        # trackable=0 → reliability=0 → 結果 0.0
        self.assertEqual(
            ResearchScorecardBuilder._calc_score({"win_rate": 80.0, "trackable": 0}), 0.0
        )

    def test_trackable_below_threshold(self):
        # trackable=5 → reliability=0.5, win_rate=60 → 30.0
        self.assertEqual(
            ResearchScorecardBuilder._calc_score({"win_rate": 60.0, "trackable": 5}), 30.0
        )

    def test_trackable_at_threshold(self):
        # trackable=10 → reliability=1.0, win_rate=60 → 60.0
        self.assertEqual(
            ResearchScorecardBuilder._calc_score({"win_rate": 60.0, "trackable": 10}), 60.0
        )

    def test_trackable_above_threshold_caps(self):
        # trackable=25 でも reliability は 1.0 止まり
        self.assertEqual(
            ResearchScorecardBuilder._calc_score({"win_rate": 60.0, "trackable": 25}), 60.0
        )

    def test_structural_upper_bound(self):
        # win_rate=100 の理論上限。勝率100%×trackable≥10 → 100.0
        self.assertEqual(
            ResearchScorecardBuilder._calc_score({"win_rate": 100.0, "trackable": 10}), 100.0
        )

    def test_rounding_one_decimal(self):
        # 73.33 * 0.3 = 21.999 → round(21.999, 1) = 22.0
        result = ResearchScorecardBuilder._calc_score({"win_rate": 73.33, "trackable": 3})
        self.assertAlmostEqual(result, 22.0, places=1)

    def test_missing_keys_default_zero(self):
        self.assertEqual(ResearchScorecardBuilder._calc_score({}), 0.0)


class TestRankInfluencers(unittest.TestCase):
    """`rank_influencers` のソート契約。"""

    def _scorecard(self, influencers):
        return {
            "metadata": {},
            "influencers": influencers,
        }

    def test_sorts_by_score_desc(self):
        builder = ResearchScorecardBuilder()
        scorecard = self._scorecard({
            "low": {"display_name": "L", "total_signals": 10,
                    "horizon_20bd": {"trackable": 10, "winners": 3, "win_rate": 30.0,
                                     "avg_return_pct": 1.0, "score": 30.0}},
            "high": {"display_name": "H", "total_signals": 10,
                     "horizon_20bd": {"trackable": 10, "winners": 8, "win_rate": 80.0,
                                      "avg_return_pct": 0.5, "score": 80.0}},
            "mid": {"display_name": "M", "total_signals": 10,
                    "horizon_20bd": {"trackable": 10, "winners": 5, "win_rate": 50.0,
                                     "avg_return_pct": 0.8, "score": 50.0}},
        })
        ranking = builder.rank_influencers(scorecard, horizon="horizon_20bd")
        self.assertEqual([r["username"] for r in ranking], ["high", "mid", "low"])

    def test_tiebreak_by_avg_return_pct_not_win_rate(self):
        # 同 score で win_rate が等しいが avg_return_pct が異なる → avg_return_pct tiebreak
        builder = ResearchScorecardBuilder()
        scorecard = self._scorecard({
            "high_winrate_low_return": {
                "display_name": "A", "total_signals": 10,
                "horizon_20bd": {"trackable": 10, "winners": 6, "win_rate": 60.0,
                                 "avg_return_pct": 0.1, "score": 60.0},
            },
            "low_winrate_high_return": {
                "display_name": "B", "total_signals": 10,
                "horizon_20bd": {"trackable": 10, "winners": 6, "win_rate": 60.0,
                                 "avg_return_pct": 2.5, "score": 60.0},
            },
        })
        ranking = builder.rank_influencers(scorecard, horizon="horizon_20bd")
        # avg_return_pct tiebreak により B が先
        self.assertEqual(ranking[0]["username"], "low_winrate_high_return")
        self.assertEqual(ranking[1]["username"], "high_winrate_low_return")

    def test_horizon_selects_correct_score(self):
        builder = ResearchScorecardBuilder()
        scorecard = self._scorecard({
            "alice": {
                "display_name": "A", "total_signals": 20,
                "horizon_5bd": {"trackable": 10, "winners": 9, "win_rate": 90.0,
                                "avg_return_pct": 1.5, "score": 90.0},
                "horizon_20bd": {"trackable": 10, "winners": 3, "win_rate": 30.0,
                                 "avg_return_pct": 0.5, "score": 30.0},
            },
            "bob": {
                "display_name": "B", "total_signals": 20,
                "horizon_5bd": {"trackable": 10, "winners": 4, "win_rate": 40.0,
                                "avg_return_pct": 0.3, "score": 40.0},
                "horizon_20bd": {"trackable": 10, "winners": 7, "win_rate": 70.0,
                                 "avg_return_pct": 1.0, "score": 70.0},
            },
        })
        # 5BD なら alice が上位
        r5 = builder.rank_influencers(scorecard, horizon="horizon_5bd")
        self.assertEqual(r5[0]["username"], "alice")
        # 20BD なら bob が上位
        r20 = builder.rank_influencers(scorecard, horizon="horizon_20bd")
        self.assertEqual(r20[0]["username"], "bob")

    def test_build_and_rank_end_to_end(self):
        # build() が horizon_stats["score"] に値を入れるかを end-to-end で確認
        builder = ResearchScorecardBuilder()
        evaluations = [
            # alice: 20BD で 勝ち4/trackable4（win_rate=100）→ trackable<10 ペナルティ → 100*0.4=40.0
            {"username": "alice", "display_name": "A",
             "horizon_5bd": {"status": "ok", "is_win": True, "return_pct": 1.0},
             "horizon_20bd": {"status": "ok", "is_win": True, "return_pct": 2.0}},
            {"username": "alice", "display_name": "A",
             "horizon_5bd": {"status": "ok", "is_win": True, "return_pct": 1.5},
             "horizon_20bd": {"status": "ok", "is_win": True, "return_pct": 2.5}},
            {"username": "alice", "display_name": "A",
             "horizon_5bd": {"status": "ok", "is_win": True, "return_pct": 2.0},
             "horizon_20bd": {"status": "ok", "is_win": True, "return_pct": 3.0}},
            {"username": "alice", "display_name": "A",
             "horizon_5bd": {"status": "ok", "is_win": True, "return_pct": 0.5},
             "horizon_20bd": {"status": "ok", "is_win": True, "return_pct": 1.0}},
        ]
        scorecard = builder.build(evaluations)
        alice_h20 = scorecard["influencers"]["alice"]["horizon_20bd"]
        # win_rate=100.0, trackable=4 → 100 * 0.4 = 40.0
        self.assertEqual(alice_h20["score"], 40.0)


if __name__ == "__main__":
    unittest.main()
