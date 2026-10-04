"""遡り評価の判定語（P-INF-31 段 0・2026-10-04 オーナー裁定 A）の回帰テスト。

守らせる点: actionable 主題の 1 文字 label（銅・金）が判定語から消えない。1 文字のまま部分一致にはしない（金曜・料金を当てない）。
実行: docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_x_vocab_backfill_eval_terms
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import x_vocab_backfill_eval as ev  # noqa: E402


class Terms(unittest.TestCase):
    def setUp(self):
        self.terms, _ = ev.load_subject_terms()

    def test_every_actionable_subject_has_terms(self):
        empty = [sid for sid, ws in self.terms.items() if not ws]
        self.assertEqual(empty, [])

    def test_copper_and_gold_use_aliases_not_single_char(self):
        self.assertIn("銅価格", self.terms["copper"])
        self.assertIn("金相場", self.terms["gold"])
        self.assertNotIn("銅", self.terms["copper"])
        self.assertNotIn("金", self.terms["gold"])
        hit = [sid for sid, ws in self.terms.items() if any(w in "金曜の料金が値上げ" for w in ws)]
        self.assertNotIn("gold", hit)


class LabelSample(unittest.TestCase):
    def test_balanced_and_deduped(self):
        rows, hits = [], []
        for i in range(30):
            rows.append({"word": "品薄" if i % 2 else "逼迫", "status_id": str(i), "text": f"別々の投稿 {i} 番目の本文です"})
            hits.append(i < 15)
        rows.append({"word": "調達難", "status_id": "s1", "text": "【東芝、HDD供給倍増へ600億円投資】"})
        hits.append(True)
        rows.append({"word": "調達難", "status_id": "s2", "text": "東芝、HDD供給倍増へ600億円投資 - 日本経済新聞"})
        hits.append(True)
        rows.append({"word": "品薄", "status_id": "3", "text": "同じ投稿が別の語でも取れた"})   # status_id 3 の重複
        hits.append(False)
        out = ev.draw_label_sample(rows, hits, n=20)
        self.assertEqual(len(out), 20)
        self.assertEqual(sum(r["judge_hit"] for r in out), 10)
        self.assertEqual(len({r["status_id"] for r in out}), 20)
        self.assertLessEqual(sum("東芝" in r["text"] for r in out), 1)
        self.assertEqual([r["label_id"] for r in out][:2], ["L01", "L02"])


if __name__ == "__main__":
    unittest.main()
