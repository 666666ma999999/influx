"""営業日計算ユーティリティ。

numpy.busday_offset を使用して営業日ベースの日付計算を行う。
"""

import numpy as np
from datetime import datetime


def add_business_days(date_str: str, n: int, market: str = "US") -> str:
    """基準日からn営業日後の日付を返す。

    Args:
        date_str: 基準日 (YYYY-MM-DD形式)
        n: 営業日数（正: 未来、負: 過去）
        market: 市場識別子（現在は "US" のみ対応、平日ベース）

    Returns:
        n営業日後の日付文字列 (YYYY-MM-DD形式)
    """
    base_date = np.datetime64(date_str, "D")
    target_date = np.busday_offset(base_date, n)
    return str(target_date)


def is_business_day(date_str: str) -> bool:
    """指定日が営業日かどうかを判定する。

    Args:
        date_str: 日付 (YYYY-MM-DD形式)

    Returns:
        True: 営業日, False: 非営業日
    """
    return bool(np.is_busday(np.datetime64(date_str, "D")))


def business_days_between(start_str: str, end_str: str) -> int:
    """2つの日付間の営業日数を返す。

    Args:
        start_str: 開始日 (YYYY-MM-DD形式)
        end_str: 終了日 (YYYY-MM-DD形式)

    Returns:
        営業日数
    """
    start = np.datetime64(start_str, "D")
    end = np.datetime64(end_str, "D")
    return int(np.busday_count(start, end))
