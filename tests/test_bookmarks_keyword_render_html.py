"""bookmarks_keyword_render_html.py のfixtureテスト。

閲覧・コピー専用WebページレンダラーがX検索キーワード集のHTMLを正しく生成するかを
検証する。Docker・ネットワーク不要、stdlib unittestのみで完結する（全fixtureは
tempdir）。

実行:
    python3 tests/test_bookmarks_keyword_render_html.py -v
    docker compose run --rm xstock python -m unittest tests.test_bookmarks_keyword_render_html -v
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bookmarks_keyword_render_html as render_html  # noqa: E402


def _query(qid, q, q_simple, intent):
    return {"query_id": qid, "q": q, "q_simple": q_simple, "intent": intent, "eval_history": []}


def _cluster(cluster_id, name, why, queries, evidence_urls):
    return {
        "cluster_id": cluster_id,
        "name": name,
        "why": why,
        "keywords": [{"keyword": "テスト", "mode": "normalized_substring"}],
        "queries": queries,
        "evidence_urls": evidence_urls,
    }


def _make_latest(active_clusters, **overrides):
    latest = {
        "generation": 1,
        "revision": 0,
        "generation_reason": "baseline",
        "generated_at": "2026-07-12T00:52:48.291521+00:00",
        "active_clusters": active_clusters,
        "dormant_clusters": [],
        "proposals": [],
        "baseline_url_count": 10,
        "last_fetch_success_at": None,
    }
    latest.update(overrides)
    return latest


class RenderHtmlTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = Path(self._tmpdir.name)
        self.latest_path = self.tmp / "keywords_latest.json"
        self.ledger_path = self.tmp / "keywords_ledger.jsonl"
        self.out_path = self.tmp / "x_keywords.html"

    def _write_latest(self, latest: dict) -> None:
        with open(self.latest_path, "w", encoding="utf-8") as f:
            json.dump(latest, f, ensure_ascii=False)

    def _run(self) -> int:
        argv = [
            "bookmarks_keyword_render_html.py",
            "--latest", str(self.latest_path),
            "--ledger", str(self.ledger_path),
            "--out", str(self.out_path),
        ]
        old_argv = sys.argv
        sys.argv = argv
        try:
            return render_html.main()
        finally:
            sys.argv = old_argv


class TestHtmlGeneration(RenderHtmlTestCase):
    def test_html_file_is_generated(self):
        clusters = [
            _cluster(
                "c-1", "クラスタA", "理由A",
                [_query("q-1", "query A", "簡易A", "意図A")],
                ["https://x.com/a/status/1"],
            ),
        ]
        self._write_latest(_make_latest(clusters))

        exit_code = self._run()

        self.assertEqual(exit_code, 0)
        self.assertTrue(self.out_path.exists())
        text = self.out_path.read_text(encoding="utf-8")
        self.assertIn("<!DOCTYPE html>", text)

    def test_all_cluster_names_present(self):
        clusters = [
            _cluster("c-1", "クラスタA", "理由A", [_query("q-1", "qA", "sA", "iA")], []),
            _cluster("c-2", "クラスタB", "理由B", [_query("q-2", "qB", "sB", "iB")], []),
            _cluster("c-3", "クラスタC", "理由C", [_query("q-3", "qC", "sC", "iC")], []),
        ]
        self._write_latest(_make_latest(clusters))
        self._run()
        text = self.out_path.read_text(encoding="utf-8")

        self.assertIn("クラスタA", text)
        self.assertIn("クラスタB", text)
        self.assertIn("クラスタC", text)

    def test_copy_button_count_matches_two_per_query(self):
        """各クエリ行は「コピー用クエリ」と「簡易版」の両方に個別コピーボタンを持つ仕様
        （team-lead spec: 各クエリ行に「コピー」ボタン+簡易版側にも「こちらにもコピー
        ボタン」の記載あり）のため、コピーボタン総数はクエリ総数の2倍になる。"""
        clusters = [
            _cluster(
                "c-1", "クラスタA", "理由A",
                [
                    _query("q-1", "query 1", "簡易1", "意図1"),
                    _query("q-2", "query 2", "簡易2", "意図2"),
                ],
                [],
            ),
            _cluster(
                "c-2", "クラスタB", "理由B",
                [_query("q-3", "query 3", "簡易3", "意図3")],
                [],
            ),
        ]
        self._write_latest(_make_latest(clusters))
        self._run()
        text = self.out_path.read_text(encoding="utf-8")

        total_queries = 3
        copy_button_count = len(re.findall(r'class="copy-btn[^"]*"', text))
        self.assertEqual(copy_button_count, total_queries * 2)

    def test_xss_escape_in_cluster_name(self):
        malicious_name = '<script>alert(1)</script>'
        clusters = [
            _cluster("c-1", malicious_name, "理由A", [_query("q-1", "qA", "sA", "iA")], []),
        ]
        self._write_latest(_make_latest(clusters))
        self._run()
        text = self.out_path.read_text(encoding="utf-8")

        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)

    def test_evidence_links_present(self):
        urls = [
            "https://x.com/a/status/1",
            "https://x.com/b/status/2",
            "https://x.com/c/status/3",
            "https://x.com/d/status/4",
        ]
        clusters = [
            _cluster("c-1", "クラスタA", "理由A", [_query("q-1", "qA", "sA", "iA")], urls),
        ]
        self._write_latest(_make_latest(clusters))
        self._run()
        text = self.out_path.read_text(encoding="utf-8")

        self.assertIn("根拠: 4件", text)
        # 表示上限(3件)を超えるURLは本文に含めない
        self.assertIn('href="https://x.com/a/status/1"', text)
        self.assertIn('href="https://x.com/c/status/3"', text)
        self.assertNotIn('href="https://x.com/d/status/4"', text)

    def test_dangerous_scheme_evidence_not_linked(self):
        """P1-4: https://x.com/ or https://twitter.com/ 以外のスキームはリンク化しない。"""
        urls = ["javascript:alert(1)"]
        clusters = [
            _cluster("c-1", "クラスタA", "理由A", [_query("q-1", "qA", "sA", "iA")], urls),
        ]
        self._write_latest(_make_latest(clusters))
        self._run()
        text = self.out_path.read_text(encoding="utf-8")

        self.assertNotIn('href="javascript:alert(1)"', text)
        self.assertIn("javascript:alert(1)", text, "リンク化はしないがテキストとしては表示されるべき")


class TestMissingLatest(RenderHtmlTestCase):
    def test_missing_latest_renders_placeholder_and_exits_zero(self):
        # latest.json をあえて書かない（存在しないケース）
        exit_code = self._run()

        self.assertEqual(exit_code, 0)
        self.assertTrue(self.out_path.exists())
        text = self.out_path.read_text(encoding="utf-8")
        self.assertIn("まだキーワード集が生成されていません", text)


if __name__ == "__main__":
    unittest.main()
