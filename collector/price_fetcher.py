"""yfinanceを使用した価格取得・キャッシュモジュール"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

try:
    import yfinance as yf
except ImportError:
    yf = None


PRICE_CACHE_FILE = "output/price_cache.json"


class PriceFetcher:
    """yfinanceで価格を取得し、JSONキャッシュするクラス"""

    def __init__(self, cache_file: str = PRICE_CACHE_FILE):
        """初期化。

        Args:
            cache_file: キャッシュファイルのパス
        """
        self.cache_file = cache_file
        self.cache = self._load_cache()

    def get_price_at_date(self, ticker: str, date: str) -> Dict[str, Any]:
        """指定日の終値を取得する（キャッシュ優先）。

        Args:
            ticker: ティッカーシンボル (e.g., "AVGO", "7203.T", "BTC-USD")
            date: 日付文字列 (YYYY-MM-DD)

        Returns:
            {"close": 185.50, "date": "2026-03-04"} or
            {"close": None, "date": date, "error": "エラーメッセージ"}
        """
        cache_key = f"{ticker}:{date}"

        # キャッシュチェック
        if cache_key in self.cache:
            cached = self.cache[cache_key]
            if cached.get("close") is not None:
                return {"close": cached["close"], "date": cached["date"]}

        # yfinance から取得
        price_data = self._fetch_price(ticker, date)
        if price_data["close"] is not None:
            self.cache[cache_key] = {
                "close": price_data["close"],
                "date": price_data["date"],
                "fetched_at": datetime.now().strftime("%Y-%m-%d"),
            }
            self._save_cache()

        return price_data

    def get_current_price(self, ticker: str) -> Dict[str, Any]:
        """最新価格を取得する。

        Args:
            ticker: ティッカーシンボル

        Returns:
            {"close": 192.30, "date": "2026-03-07"} or
            {"close": None, "date": None, "error": "エラーメッセージ"}
        """
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"{ticker}:current"

        # キャッシュチェック（当日のみ有効）
        if cache_key in self.cache:
            cached = self.cache[cache_key]
            if cached.get("fetched_at") == today and cached.get("close") is not None:
                return {"close": cached["close"], "date": cached["date"]}

        # yfinanceから最新価格を取得
        try:
            if yf is None:
                return {"close": None, "date": None, "error": "yfinanceがインストールされていません"}

            stock = yf.Ticker(ticker)
            hist = stock.history(period="5d")

            if hist.empty:
                return {"close": None, "date": None, "error": f"{ticker}の価格データが取得できません"}

            last_row = hist.iloc[-1]
            close_price = round(float(last_row["Close"]), 2)
            price_date = hist.index[-1].strftime("%Y-%m-%d")

            # キャッシュ更新
            self.cache[cache_key] = {
                "close": close_price,
                "date": price_date,
                "fetched_at": today,
            }
            self._save_cache()

            return {"close": close_price, "date": price_date}

        except Exception as e:
            return {"close": None, "date": None, "error": str(e)}

    def _fetch_price(self, ticker: str, date: str) -> Dict[str, Any]:
        """yfinanceから指定日付近の終値を取得する（内部メソッド）。

        指定日が市場休日の場合、直前の営業日の終値を返す。

        Args:
            ticker: ティッカーシンボル
            date: 日付文字列 (YYYY-MM-DD)

        Returns:
            {"close": float|None, "date": str}
        """
        if yf is None:
            return {"close": None, "date": date, "error": "yfinanceがインストールされていません"}

        try:
            target_date = datetime.strptime(date, "%Y-%m-%d")
            # 指定日の前後で取得（休日対策）
            start = (target_date - timedelta(days=5)).strftime("%Y-%m-%d")
            end = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")

            stock = yf.Ticker(ticker)
            hist = stock.history(start=start, end=end)

            if hist.empty:
                return {"close": None, "date": date, "error": f"{ticker}の{date}付近の価格データなし"}

            # 指定日以前で最も近い営業日のデータを取得
            hist_before = hist[hist.index <= target_date.strftime("%Y-%m-%d 23:59:59")]
            if hist_before.empty:
                # 指定日以前にデータがない場合、最も古いデータを使用
                row = hist.iloc[0]
                actual_date = hist.index[0].strftime("%Y-%m-%d")
            else:
                row = hist_before.iloc[-1]
                actual_date = hist_before.index[-1].strftime("%Y-%m-%d")

            close_price = round(float(row["Close"]), 2)
            return {"close": close_price, "date": actual_date}

        except Exception as e:
            return {"close": None, "date": date, "error": str(e)}

    def _load_cache(self) -> Dict[str, Any]:
        """JSONキャッシュを読み込む。

        Returns:
            キャッシュデータの辞書
        """
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_cache(self) -> None:
        """キャッシュをJSONファイルに保存する。"""
        os.makedirs(os.path.dirname(self.cache_file) or ".", exist_ok=True)
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=2)
