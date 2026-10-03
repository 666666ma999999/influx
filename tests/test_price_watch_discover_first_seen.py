"""発見器の初出レーン（P-INF-25・オーナー裁定 a 2026-10-03）の回帰テスト。

守らせる 3 点:
1. extract_candidates は候補ごとに first_posted_at（その語を含む投稿の最も早い posted_at_full）を出す。
   posted_at_full が無い旧形式の行だけなら空文字（「未記録」と区別できる）。
2. recorded_tokens は過去の candidate_batch の token を集め、before_day より後の便は数えない
   （過去日の取り直しで「自分より後に出た語」を既出と誤判定しない）。mark_first_seen はそれで first_seen を付ける。
3. x_shortage_map.subjects_for_token は subject の discover_tokens を NFKC で引く。

発見器本体は playwright 系を import するため Docker で回す（CLAUDE.md の unittest 経路）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import price_watch_discover as d  # noqa: E402
import x_shortage_map as xsm  # noqa: E402


class FirstPostedAt(unittest.TestCase):
    def test_earliest_posted_at_full_is_kept(self):
        posts = [
            {"id": "1", "author": "a", "likes": 30, "content": "ベースダイが品薄で 3,000円 値上げ",
             "posted_at_full": "2026-10-02T09:30:00.000Z"},
            {"id": "2", "author": "b", "likes": 20, "content": "ベースダイ 入手困難",
             "posted_at_full": "2026-10-02T01:05:00.000Z"},
            {"id": "3", "author": "c", "likes": 25, "content": "ベースダイ また値上げ",
             "posted_at_full": "2026-10-02T12:00:00.000Z"},
        ]
        cands = d.extract_candidates(posts, known=set())
        by = {c["token"]: c for c in cands}
        self.assertIn("ベースダイ", by)
        self.assertEqual(by["ベースダイ"]["first_posted_at"], "2026-10-02T01:05:00.000Z")

    def test_missing_posted_at_full_gives_empty_string(self):
        posts = [
            {"id": "1", "author": "a", "likes": 30, "content": "ベースダイ 品薄", "posted_at": "2026-10-02"},
            {"id": "2", "author": "b", "likes": 20, "content": "ベースダイ 入手困難", "posted_at": "2026-10-02"},
        ]
        by = {c["token"]: c for c in d.extract_candidates(posts, known=set())}
        self.assertEqual(by["ベースダイ"]["first_posted_at"], "")


class FirstSeen(unittest.TestCase):
    def _queue(self, rows):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        for r in rows:
            tmp.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.close()
        return Path(tmp.name)

    def test_recorded_tokens_respects_before_day(self):
        q = self._queue([
            {"type": "candidate_batch", "date": "2026-09-26", "candidates": [{"token": "ナプキン"}]},
            {"type": "candidate_batch", "date": "2026-10-02", "candidates": [{"token": "ベースダイ"}]},
            {"type": "other", "date": "2026-10-02", "candidates": [{"token": "無視"}]},
        ])
        self.assertEqual(d.recorded_tokens(q), {"ナプキン", "ベースダイ"})
        self.assertEqual(d.recorded_tokens(q, before_day="2026-10-01"), {"ナプキン"})
        self.assertEqual(d.recorded_tokens(Path("/nonexistent/x.jsonl")), set())

    def test_mark_first_seen(self):
        out = d.mark_first_seen([{"token": "ナプキン"}, {"token": "新語"}], {"ナプキン"})
        self.assertEqual([c["first_seen"] for c in out], [False, True])


class SubjectsForToken(unittest.TestCase):
    def test_nfkc_match_and_absent(self):
        m = {"subjects": [
            {"id": "dram", "discover_tokens": ["ベースダイ", "HBM3E"]},
            {"id": "copper", "queries": ["gen1-douka-kotou"]},
        ]}
        self.assertEqual([s["id"] for s in xsm.subjects_for_token(m, "ＨＢＭ３Ｅ")], ["dram"])
        self.assertEqual(xsm.subjects_for_token(m, "ナプキン"), [])


if __name__ == "__main__":
    unittest.main()
