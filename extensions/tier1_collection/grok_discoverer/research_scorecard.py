"""リサーチスコアカード計算モジュール。

シグナル評価結果からインフルエンサー別の勝率・リターンを算出し、
ランキングレポートを生成する。
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class ResearchScorecardBuilder:
    """インフルエンサー別リサーチスコアカード計算クラス。

    シグナル評価レコードを元に、ホライズン別（5BD/20BD）の
    勝率・平均リターンを算出する。
    """

    def build(self, evaluations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """スコアカードを構築する。

        Args:
            evaluations: 評価レコードのリスト

        Returns:
            スコアカード辞書
        """
        now = datetime.now()

        # インフルエンサー別に集計
        by_influencer: Dict[str, List[Dict[str, Any]]] = {}
        for ev in evaluations:
            username = ev.get("username", "unknown")
            by_influencer.setdefault(username, []).append(ev)

        influencers = {}
        for username, evals in sorted(by_influencer.items()):
            influencers[username] = self._calc_influencer_stats(evals)

        # グローバルサマリー
        global_5bd = self._calc_horizon_stats(evaluations, "horizon_5bd")
        global_20bd = self._calc_horizon_stats(evaluations, "horizon_20bd")

        return {
            "generated_at": now.isoformat(),
            "total_signals": len(evaluations),
            "total_influencers": len(influencers),
            "global_summary": {
                "horizon_5bd": global_5bd,
                "horizon_20bd": global_20bd,
            },
            "influencers": influencers,
        }

    def rank_influencers(self, scorecard: Dict[str, Any], horizon: str = "horizon_20bd") -> List[Dict[str, Any]]:
        """インフルエンサーを score でランキングする（plan.md M0 Exit #4）。

        Args:
            scorecard: build() の出力
            horizon: 表示する win_rate / avg_return_pct のホライズン (horizon_5bd or horizon_20bd)。
                score はホライズンごとに `horizon_stats["score"]` に保存されており、
                ここで指定されたホライズンの score を第1キーとしてソートする。

        Returns:
            ランキングリスト。ソートキー: (score, avg_return_pct) 降順。
            tiebreak は plan.md M0 T0.5 で明示した avg_return_pct（win_rate は含めない）。
        """
        ranking = []
        for username, stats in scorecard.get("influencers", {}).items():
            horizon_stats = stats.get(horizon, {})
            # ホライズン別 score を優先、未設定なら influencer トップレベル score
            score = horizon_stats.get("score", stats.get("score", 0.0))
            ranking.append({
                "username": username,
                "display_name": stats.get("display_name", ""),
                "total_signals": stats.get("total_signals", 0),
                "trackable": horizon_stats.get("trackable", 0),
                "winners": horizon_stats.get("winners", 0),
                "win_rate": horizon_stats.get("win_rate", 0.0),
                "avg_return_pct": horizon_stats.get("avg_return_pct", 0.0),
                "score": score,
            })

        ranking.sort(key=lambda x: (x["score"], x["avg_return_pct"]), reverse=True)
        return ranking

    def save(self, scorecard: Dict[str, Any], path: str) -> None:
        """スコアカードをJSONファイルに保存する。

        Args:
            scorecard: スコアカード辞書
            path: 出力ファイルパス
        """
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(scorecard, f, ensure_ascii=False, indent=2)
        logger.info("リサーチスコアカード保存: %s", path)

    def _calc_influencer_stats(self, evals: List[Dict[str, Any]]) -> Dict[str, Any]:
        """インフルエンサー別の統計を計算する。"""
        display_name = ""
        for ev in evals:
            if ev.get("display_name"):
                display_name = ev["display_name"]
                break

        h5 = self._calc_horizon_stats(evals, "horizon_5bd")
        h20 = self._calc_horizon_stats(evals, "horizon_20bd")
        # horizon 別 score を horizon_stats 内に埋める（plan.md M0 T0.3 後方互換 +
        # rank_influencers の API 契約正常化: horizon 引数で正しく score を引ける）
        h5["score"] = self._calc_score(h5)
        h20["score"] = self._calc_score(h20)

        return {
            "display_name": display_name,
            "total_signals": len(evals),
            "horizon_5bd": h5,
            "horizon_20bd": h20,
            # トップレベル score は 20BD 基準（後方互換、plan.md M0 Exit #3 評価用）
            "score": h20["score"],
        }

    @staticmethod
    def _calc_score(horizon_stats: Dict[str, Any]) -> float:
        """plan.md M0 T0.3: 勝率×trackable信頼性の合成スコア（0-100スケール）。

        score = win_rate * min(trackable / 10, 1.0)
        - win_rate: `_calc_horizon_stats` が返す 0-100 スケール（パーセント）
        - trackable が 10 未満の influencer は信頼性ペナルティ
        - horizon_stats は呼び出し側が 5BD / 20BD どちらでも渡せる（API 契約：
          rank_influencers(horizon=...) はこの horizon 別 score を使う）
        """
        win_rate = horizon_stats.get("win_rate", 0.0)
        trackable = horizon_stats.get("trackable", 0)
        reliability = min(trackable / 10.0, 1.0)
        return round(win_rate * reliability, 1)

    @staticmethod
    def _calc_horizon_stats(evals: List[Dict[str, Any]], horizon_key: str) -> Dict[str, Any]:
        """特定ホライズンの統計を計算する。

        Args:
            evals: 評価レコードリスト
            horizon_key: "horizon_5bd" or "horizon_20bd"

        Returns:
            統計辞書
        """
        trackable = []
        for ev in evals:
            horizon = ev.get(horizon_key, {})
            if horizon and horizon.get("return_pct") is not None:
                trackable.append(horizon)

        if not trackable:
            return {
                "total": len(evals),
                "trackable": 0,
                "winners": 0,
                "losers": 0,
                "win_rate": 0.0,
                "avg_return_pct": 0.0,
                "total_return_pct": 0.0,
                "best_return_pct": None,
                "worst_return_pct": None,
            }

        winners = [h for h in trackable if h.get("is_win")]
        losers = [h for h in trackable if not h.get("is_win")]
        returns = [h["return_pct"] for h in trackable]

        return {
            "total": len(evals),
            "trackable": len(trackable),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(len(winners) / len(trackable) * 100, 1),
            "avg_return_pct": round(sum(returns) / len(returns), 2),
            "total_return_pct": round(sum(returns), 2),
            "best_return_pct": round(max(returns), 2),
            "worst_return_pct": round(min(returns), 2),
        }
