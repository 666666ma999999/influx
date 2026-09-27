"""imp_pilot の純関数テスト（P-INF-21・2026-09-27）。

(a) 丸め表示の数え直し  (b) 検索窓は 48h 以上前まで  (c) カードの permalink から投稿者と ID
(d) 集計は欠測を 0 に変えない（比は両方ある行だけ）

実行: python3 -m unittest tests.test_imp_pilot
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import imp_pilot as ip  # noqa: E402


class ParseCount(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(ip.parse_count("1,234 フォロワー"), 1234)
        self.assertEqual(ip.parse_count("1.2万 フォロワー"), 12000)
        self.assertEqual(ip.parse_count("12.3K"), 12300)
        self.assertEqual(ip.parse_count("3 フォロワー"), 3)
        self.assertIsNone(ip.parse_count(""))
        self.assertIsNone(ip.parse_count(None))
        self.assertIsNone(ip.parse_count("フォロワー"))


class ViewsFromLabel(unittest.TestCase):
    def test_group_label_picks_views_not_replies(self):
        ja = "1 件の返信、1 件のリポスト、20 件のいいね、1 件のブックマーク、6366 件の表示"
        self.assertEqual(ip.views_from_label(ja), 6366)
        self.assertEqual(ip.views_from_label("3 replies, 20 likes, 12.3K views"), 12300)
        self.assertEqual(ip.views_from_label("6,366 件の表示"), 6366)
        self.assertEqual(ip.views_from_label("1.2万 件の表示"), 12000)
        self.assertEqual(ip.views_from_label("12.3K 件の表示"), 12300)
        self.assertIsNone(ip.views_from_label("20 件のいいね"))
        self.assertIsNone(ip.views_from_label(None))


class Window(unittest.TestCase):
    def test_window_excludes_last_two_days(self):
        now = datetime(2026, 9, 27, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(ip.search_window(now, 5, 2), ("2026-09-22", "2026-09-25"))
        q = ip.compose_query("株 (爆益 OR 買い時)", "2026-09-22", "2026-09-25")
        self.assertIn("since:2026-09-22 until:2026-09-25", q)
        self.assertIn("-filter:nativeretweets", q)
        self.assertNotIn("min_faves", q)


class Card(unittest.TestCase):
    def test_parse_card(self):
        c = ip.parse_card("/some_user/status/123456", "本文  改行\nあり", "2026-09-23T01:02:03.000Z")
        self.assertEqual(c["username"], "some_user")
        self.assertEqual(c["status_id"], "123456")
        self.assertEqual(c["url"], "https://x.com/some_user/status/123456")
        self.assertEqual(c["text"], "本文 改行 あり")

    def test_drop_without_text_or_href(self):
        self.assertIsNone(ip.parse_card("/u/status/1", None, None))
        self.assertIsNone(ip.parse_card(None, "x", None))
        self.assertIsNone(ip.parse_card("/i/lists/1", "x", None))


class Dedupe(unittest.TestCase):
    def test_ok_wins_over_failed_retry(self):
        rows = [{"genre": "g", "status_id": "1", "status": "other"}, {"genre": "g", "status_id": "1", "status": "ok", "views": 5},
                {"genre": "g", "status_id": "2", "status": "other"}, {"genre": "g", "status_id": "2", "status": "other", "error_detail": "x"},
                {"genre": "h", "status_id": "1", "status": "ok"}]  # 同じ投稿が別ジャンルにも該当 → 両方残す
        out = {(r["genre"], r["status_id"]): r for r in ip.dedupe_latest(rows)}
        self.assertEqual(out[("g", "1")]["status"], "ok")
        self.assertEqual(out[("g", "2")].get("error_detail"), "x")
        self.assertEqual(len(out), 3)


class P90(unittest.TestCase):
    def test_nearest_rank(self):
        rows = [{"genre": "g", "views": v, "followers": 1} for v in range(1, 30)]  # 比 1..29
        self.assertEqual(ip.summarize(rows)["g"]["ratio_p90"], 27)  # ceil(0.9*29)=27 番目
        rows2 = [{"genre": "g", "views": v, "followers": 1} for v in (1, 2)]
        self.assertEqual(ip.summarize(rows2)["g"]["ratio_p90"], 2)


class Summary(unittest.TestCase):
    def test_missing_not_zero(self):
        rows = [
            {"genre": "kabu", "views": 1000, "followers": 100},
            {"genre": "kabu", "views": 300, "followers": 100},
            {"genre": "kabu", "views": None, "followers": 50},      # 閲覧数欠測
            {"genre": "kabu", "views": 10, "followers": 0},         # フォロワー 0 は比を出さない
            {"genre": "pokeca", "views": 50, "followers": None},    # フォロワー欠測
        ]
        s = ip.summarize(rows)
        self.assertEqual(s["kabu"]["n"], 4)
        # posts を渡すと「拾った」= 収集件数（metrics に無い未処理 2 件も母数に入る）
        posts = [{"genre": "kabu"}] * 6 + [{"genre": "pokeca"}]
        self.assertEqual(ip.summarize(rows, posts)["kabu"]["n"], 6)
        self.assertEqual(s["kabu"]["n_views"], 3)
        self.assertEqual(s["kabu"]["n_ratio"], 2)
        self.assertEqual(s["kabu"]["ratio_median"], 6.5)
        self.assertEqual(s["pokeca"]["n_ratio"], 0)
        self.assertIsNone(s["pokeca"]["ratio_median"])
        text = ip.render_report(s, {"kabu": {"label": "株", "intent": "儲かりそう"}}, {"run_id": "r", "window": "w"})
        self.assertIn("| 株 | 儲かりそう | 4 | 3 |", text)
        self.assertIn("| pokeca |", text)


if __name__ == "__main__":
    unittest.main()
