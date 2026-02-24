"""
構造化ログユーティリティ
セキュリティイベントのJSON構造化ログを提供する
"""
import logging
import json
import sys
from datetime import datetime, timezone


class StructuredFormatter(logging.Formatter):
    """JSON構造化ログフォーマッタ"""

    def format(self, record):
        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": record.module,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra_data"):
            log_data.update(record.extra_data)
        return json.dumps(log_data, ensure_ascii=False)


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """構造化ロガーを取得"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredFormatter())
        logger.addHandler(handler)
        logger.setLevel(level)
    return logger
