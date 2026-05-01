"""tier3_posting 用 posting 関連定数（collector からの抽出）。

Phase 1: collector/config.py の SST と同期させる必要がある（重複期間）。
Phase 3 物理分離後はこのファイルが SST になる。

抽出元シンボル:
- CATEGORY_TEMPLATE_MAP: カテゴリ → テンプレート名
- CATEGORY_ACCOUNT_MAP: カテゴリ → 投稿アカウント
- NON_CATEGORY_TEMPLATE_ACCOUNT_MAP: カテゴリ駆動でないテンプレート → 投稿アカウント
"""
from __future__ import annotations


CATEGORY_TEMPLATE_MAP = {
    "recommended_assets": "hot_picks",
    "purchased_assets":   "trade_activity",
    "ipo":                "hot_picks",
    "market_trend":       "market_summary",
    "bullish_assets":     "hot_picks",
    "bearish_assets":     "market_summary",
    "warning_signals":    "contrarian_signal",
}


CATEGORY_ACCOUNT_MAP = {
    "recommended_assets": "kabuki666999",
    "purchased_assets":   "kabuki666999",
    "ipo":                "kabuki666999",
    "market_trend":       "kabuki666999",
    "bullish_assets":     "kabuki666999",
    "bearish_assets":     "kabuki666999",
    "warning_signals":    "kabuki666999",
}


NON_CATEGORY_TEMPLATE_ACCOUNT_MAP = {
    "win_rate_ranking": "kabuki666999",
    "weekly_report":    "kabuki666999",
    "earnings_flash":   "kabuki666999",
}
