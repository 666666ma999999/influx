"""x_metrics_lib の純関数テスト（ネットワーク・Docker 不要・stdlib unittest のみ）。

実行:
  python3 -m unittest tests.test_x_metrics_lib -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from x_metrics_lib import METRIC_FIELDS, build_record, status_id_of  # noqa: E402

CAPTURED = "2026-08-19T00:00:00+00:00"


class TestStatusIdOf(unittest.TestCase):
    def test_plain_id(self):
        self.assertEqual(status_id_of("2085817074816070014"), "2085817074816070014")

    def test_url(self):
        self.assertEqual(status_id_of("https://x.com/a/status/123"), "123")

    def test_url_with_suffix(self):
        # /status/<id>/photo/1 の末尾要素 "1" を誤って拾わない（旧実装の欠陥）
        self.assertEqual(status_id_of("https://x.com/a/status/123/photo/1"), "123")

    def test_garbage(self):
        self.assertIsNone(status_id_of("https://x.com/home"))
        self.assertIsNone(status_id_of(""))
        self.assertIsNone(status_id_of(None))


class TestBuildRecord(unittest.TestCase):
    def test_missing_is_null_not_zero(self):
        """両経路とも落ちたら全指標 null（0 を書かない則）。"""
        rec = build_record("1", None, None, ["syn: HTTP404"], CAPTURED)
        for f in METRIC_FIELDS:
            self.assertIsNone(rec[f], f)
        self.assertEqual(rec["sources"], {})
        self.assertIn("syn: HTTP404", rec["errors"])

    def test_zero_is_kept_as_observation(self):
        """観測された 0 は欠測にしない（真の0いいねは 0 のまま）。"""
        rec = build_record("1", {"favorite_count": 0, "conversation_count": 0}, None, [], CAPTURED)
        self.assertEqual(rec["likes"], 0)
        self.assertEqual(rec["sources"]["likes"], "syndication")

    def test_likes_from_syndication_views_from_fxtwitter(self):
        """正本の割当: likes/replies=syndication・views/bookmarks/RT/quotes=fxtwitter。"""
        syn = {"favorite_count": 105, "conversation_count": 7}
        fx = {"tweet": {"likes": 104, "views": 46112, "bookmarks": 12,
                        "retweets": 3, "quotes": 1, "replies": 6}}
        rec = build_record("1", syn, fx, [], CAPTURED)
        self.assertEqual(rec["likes"], 105)
        self.assertEqual(rec["sources"]["likes"], "syndication")
        self.assertEqual(rec["views"], 46112)
        self.assertEqual(rec["sources"]["views"], "fxtwitter")
        self.assertEqual(rec["replies"], 7)
        self.assertEqual(rec["sources"]["replies"], "syndication")

    def test_fxtwitter_fills_when_syndication_down(self):
        fx = {"tweet": {"likes": 104, "replies": 6}}
        rec = build_record("1", None, fx, [], CAPTURED)
        self.assertEqual(rec["likes"], 104)
        self.assertEqual(rec["sources"]["likes"], "fxtwitter")
        self.assertEqual(rec["replies"], 6)

    def test_likes_mismatch_is_recorded(self):
        """両経路が生きていて likes が5%超ズレたら errors に記録（黙って見過ごさない）。"""
        syn = {"favorite_count": 1000, "conversation_count": 1}
        fx = {"tweet": {"likes": 2000}}
        rec = build_record("1", syn, fx, [], CAPTURED)
        self.assertTrue(any(e.startswith("likes_mismatch") for e in rec["errors"]))
        self.assertEqual(rec["likes"], 1000)  # 正本は syndication のまま

    def test_small_diff_not_flagged(self):
        """観測時刻差による小さなズレ（±5%以内）は誤報にしない。"""
        syn = {"favorite_count": 1000, "conversation_count": 1}
        fx = {"tweet": {"likes": 1030}}
        rec = build_record("1", syn, fx, [], CAPTURED)
        self.assertFalse(any(e.startswith("likes_mismatch") for e in rec["errors"]))

    def test_captured_at_always_present(self):
        rec = build_record("1", None, None, [], CAPTURED)
        self.assertEqual(rec["captured_at"], CAPTURED)


if __name__ == "__main__":
    unittest.main()
