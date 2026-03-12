"""インフルエンサー別スコアカード計算モジュール。

推奨レコードと現在価格からインフルエンサー別のスコアカードを構築する。
期間スライス（7d / 30d / all）、連勝/連敗、逆指標分析を含む。
"""

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ScorecardBuilder:
    """インフルエンサー別スコアカード計算クラス。

    推奨レコードのリストと現在価格を元に、
    インフルエンサー別のパフォーマンスデータを構築する。
    """

    PERIODS = {
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
        "all": None,
    }

    def build(
        self,
        recs: List[Dict[str, Any]],
        current_prices: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """スコアカードを構築する。

        Args:
            recs: 推奨レコードのリスト（RecommendationStore.load_all() の出力）
            current_prices: ティッカー別の現在価格
                {"AVGO": {"close": 192.30, "date": "2026-03-07"}, ...}

        Returns:
            スコアカード辞書
        """
        now = datetime.now()

        # 各レコードにリターンを計算
        enriched = self._enrich_with_returns(recs, current_prices)

        # インフルエンサー別集計
        influencers = {}
        all_trackable = [r for r in enriched if r.get("return_pct") is not None]

        # インフルエンサー名一覧
        inf_names = sorted(set(r.get("influencer", "") for r in enriched if r.get("influencer")))

        for inf_name in inf_names:
            inf_recs = [r for r in enriched if r.get("influencer") == inf_name]
            by_period = {}

            for period_name, delta in self.PERIODS.items():
                if delta is not None:
                    cutoff = (now - delta).strftime("%Y-%m-%d")
                    period_recs = [r for r in inf_recs if (r.get("recommended_at") or "") >= cutoff]
                else:
                    period_recs = inf_recs

                by_period[period_name] = self._calc_stats(period_recs)

            # 連勝/連敗
            streak = self._calc_streak(inf_recs)

            # picks: リターン降順
            picks = sorted(
                [r for r in inf_recs if r.get("return_pct") is not None],
                key=lambda r: r["return_pct"],
                reverse=True,
            )
            pick_summaries = [
                {
                    "rec_id": r.get("rec_id"),
                    "ticker": r.get("ticker"),
                    "matched_text": r.get("matched_text"),
                    "recommended_at": r.get("recommended_at"),
                    "return_pct": r.get("return_pct"),
                    "is_winner": r.get("return_pct", 0) > 0,
                }
                for r in picks
            ]

            influencers[inf_name] = {
                "display_name": next(
                    (r.get("display_name", "") for r in inf_recs if r.get("display_name")),
                    "",
                ),
                "is_contrarian": any(r.get("is_contrarian") for r in inf_recs),
                "by_period": by_period,
                "streak": streak,
                "picks": pick_summaries,
            }

        # グローバルサマリー
        global_summary = self._calc_stats(all_trackable)

        # 逆指標サマリー
        contrarian_recs = [r for r in all_trackable if r.get("is_contrarian")]
        contrarian_summary = self._calc_contrarian_stats(contrarian_recs)

        return {
            "generated_at": now.isoformat(),
            "total_recommendations": len(recs),
            "total_trackable": len(all_trackable),
            "global_summary": global_summary,
            "contrarian_summary": contrarian_summary,
            "influencers": influencers,
        }

    def save(self, scorecards: Dict[str, Any], path: str) -> None:
        """スコアカードをJSONファイルに保存する。

        Args:
            scorecards: スコアカード辞書
            path: 出力ファイルパス
        """
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(scorecards, f, ensure_ascii=False, indent=2)
        logger.info("スコアカード保存: %s", path)

    def save_snapshot(
        self,
        recs: List[Dict[str, Any]],
        current_prices: Dict[str, Dict[str, Any]],
        snapshot_dir: str,
    ) -> str:
        """日次スナップショットを保存する。

        Args:
            recs: 推奨レコードリスト
            current_prices: 現在価格辞書
            snapshot_dir: スナップショット保存ディレクトリ

        Returns:
            保存したファイルパス
        """
        today = datetime.now().strftime("%Y-%m-%d")
        snapshot = {
            "date": today,
            "prices": {},
        }

        for rec in recs:
            ticker = rec.get("ticker")
            if ticker and ticker in current_prices:
                price_info = current_prices[ticker]
                if price_info.get("close") is not None:
                    snapshot["prices"][ticker] = {
                        "close": price_info["close"],
                        "date": price_info.get("date"),
                    }

        os.makedirs(snapshot_dir, exist_ok=True)
        path = os.path.join(snapshot_dir, f"{today}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        logger.info("スナップショット保存: %s", path)
        return path

    def _enrich_with_returns(
        self,
        recs: List[Dict[str, Any]],
        current_prices: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """レコードにリターン情報を付加する。

        Args:
            recs: 推奨レコードリスト
            current_prices: 現在価格辞書

        Returns:
            リターン情報付きレコードリスト（元データは変更しない）
        """
        enriched = []
        for rec in recs:
            r = dict(rec)
            ticker = r.get("ticker")
            price_at_rec = r.get("price_at_recommendation")
            current = current_prices.get(ticker, {})
            current_price = current.get("close")

            if price_at_rec and current_price and price_at_rec > 0:
                r["current_price"] = current_price
                r["return_pct"] = round(
                    (current_price - price_at_rec) / price_at_rec * 100, 2
                )
            else:
                r["current_price"] = current_price
                r["return_pct"] = None

            enriched.append(r)
        return enriched

    @staticmethod
    def _calc_stats(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """統計を計算する。

        Args:
            recs: リターン情報付きレコードリスト

        Returns:
            統計辞書
        """
        trackable = [r for r in recs if r.get("return_pct") is not None]
        if not trackable:
            return {
                "total": len(recs),
                "trackable": 0,
                "winners": 0,
                "losers": 0,
                "win_rate": 0.0,
                "avg_return_pct": 0.0,
                "total_return_pct": 0.0,
                "best_pick": None,
                "worst_pick": None,
            }

        winners = [r for r in trackable if r["return_pct"] > 0]
        losers = [r for r in trackable if r["return_pct"] <= 0]
        returns = [r["return_pct"] for r in trackable]

        best = max(trackable, key=lambda r: r["return_pct"])
        worst = min(trackable, key=lambda r: r["return_pct"])

        return {
            "total": len(recs),
            "trackable": len(trackable),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(len(winners) / len(trackable) * 100, 1),
            "avg_return_pct": round(sum(returns) / len(returns), 2),
            "total_return_pct": round(sum(returns), 2),
            "best_pick": {
                "ticker": best.get("ticker"),
                "return_pct": best["return_pct"],
            },
            "worst_pick": {
                "ticker": worst.get("ticker"),
                "return_pct": worst["return_pct"],
            },
        }

    @staticmethod
    def _calc_streak(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """連勝/連敗を計算する。

        Args:
            recs: レコードリスト（推奨日順にソート済み想定）

        Returns:
            {"current": N, "type": "win"|"lose"|"none"}
        """
        trackable = [
            r for r in sorted(recs, key=lambda r: r.get("recommended_at", ""))
            if r.get("return_pct") is not None
        ]

        if not trackable:
            return {"current": 0, "type": "none"}

        # 最新から遡って連続を数える
        last = trackable[-1]
        streak_type = "win" if last["return_pct"] > 0 else "lose"
        count = 0

        for r in reversed(trackable):
            is_win = r["return_pct"] > 0
            if (streak_type == "win" and is_win) or (streak_type == "lose" and not is_win):
                count += 1
            else:
                break

        return {"current": count, "type": streak_type}

    @staticmethod
    def _calc_contrarian_stats(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """逆指標のパフォーマンスを計算する。

        Args:
            recs: 逆指標レコードリスト

        Returns:
            逆指標統計辞書
        """
        if not recs:
            return {
                "total": 0,
                "reverse_win_rate": 0.0,
                "reverse_return_pct": 0.0,
            }

        reverse_returns = [-r["return_pct"] for r in recs]
        reverse_winners = [rr for rr in reverse_returns if rr > 0]

        return {
            "total": len(recs),
            "reverse_win_rate": round(len(reverse_winners) / len(recs) * 100, 1),
            "reverse_return_pct": round(sum(reverse_returns) / len(reverse_returns), 2),
        }
