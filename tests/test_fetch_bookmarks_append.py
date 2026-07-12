"""fetch_bookmarks.py --appendモードのデータ消失バグに対する回帰テスト。

背景: --appendモードでテキスト空ブックマークの本文補完(backfill)が1件でも
成功すると、出力ファイルを"w"で開き直し、メモリ上の今回取得分のみで全体を
上書きしてしまい、既存レコードが失われるバグがあった。本テストは
merge_bookmark_records / atomic_write_jsonl の単体挙動と、その組み合わせで
データ消失が起きないことを検証する。

Playwright・Docker・ネットワーク不要、stdlib unittestのみで完結する。

実行:
  python3 tests/test_fetch_bookmarks_append.py -v
  docker compose run --rm xstock python -m unittest tests.test_fetch_bookmarks_append -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_bookmarks
from fetch_bookmarks import (
    BookmarkCountRegressionError,
    atomic_write_jsonl,
    merge_bookmark_records,
    _write_status_json,
)


def _rec(url, text, **extra):
    d = {"url": url, "text": text, "author": "@x", "like_count": 0}
    d.update(extra)
    return d


class TestMergeBookmarkRecords(unittest.TestCase):
    def test_existing_plus_new_plus_backfill(self):
        # 既存5件 + 新規3件 + 既存1件の本文補完（同一URL・text空→非空）
        existing = [
            _rec("https://x.com/a/status/1", "text1"),
            _rec("https://x.com/a/status/2", "text2"),
            _rec("https://x.com/a/status/3", "text3"),
            _rec("https://x.com/a/status/4", ""),  # これがbackfillで補完される
            _rec("https://x.com/a/status/5", "text5"),
        ]
        fetched = [
            _rec("https://x.com/a/status/4", "backfilled-text"),  # 補完結果（同一URL上書き）
            _rec("https://x.com/a/status/6", "text6"),
            _rec("https://x.com/a/status/7", "text7"),
            _rec("https://x.com/a/status/8", "text8"),
        ]

        merged = merge_bookmark_records(existing, fetched)

        self.assertEqual(len(merged), 8)
        urls = [r["url"] for r in merged]
        self.assertEqual(len(set(urls)), 8, "URLの重複がないこと")

        by_url = {r["url"]: r for r in merged}
        self.assertEqual(by_url["https://x.com/a/status/4"]["text"], "backfilled-text",
                         "補完後のテキストが反映されていること")
        for i in [1, 2, 3, 5]:
            self.assertEqual(by_url[f"https://x.com/a/status/{i}"]["text"], f"text{i}",
                              "既存のみのURLが保持されていること")
        for i in [6, 7, 8]:
            self.assertIn(f"https://x.com/a/status/{i}", by_url, "新規取得分が保持されていること")

        # 既存の並び順が先頭に維持され、新規は末尾に追加されること
        self.assertEqual(
            urls[:5],
            [f"https://x.com/a/status/{i}" for i in [1, 2, 3, 4, 5]],
        )

    def test_urls_without_url_field_are_ignored(self):
        existing = [_rec("", "no-url-record")]
        fetched = [_rec("https://x.com/a/status/1", "text1")]
        merged = merge_bookmark_records(existing, fetched)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["url"], "https://x.com/a/status/1")


class TestAtomicWriteJsonl(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.out_path = os.path.join(self._tmpdir.name, "bookmarks.jsonl")

    def _write_seed_file(self, records):
        with open(self.out_path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _read_jsonl(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_atomic_write_succeeds_and_replaces_file(self):
        records = [_rec("https://x.com/a/status/1", "t1"), _rec("https://x.com/a/status/2", "t2")]
        atomic_write_jsonl(self.out_path, records, min_count=0)
        result = self._read_jsonl(self.out_path)
        self.assertEqual(result, records)

    def test_count_regression_guard_blocks_write_and_leaves_original_intact(self):
        # 既存ファイルを5件で用意し、min_count=5に対して3件しか渡さない → 拒否されること
        seed = [_rec(f"https://x.com/a/status/{i}", f"t{i}") for i in range(1, 6)]
        self._write_seed_file(seed)

        smaller = [_rec(f"https://x.com/a/status/{i}", f"t{i}") for i in range(1, 4)]
        with self.assertRaises(BookmarkCountRegressionError):
            atomic_write_jsonl(self.out_path, smaller, min_count=len(seed))

        # 元ファイルが無傷であること（5件のまま）
        result = self._read_jsonl(self.out_path)
        self.assertEqual(result, seed)

    def test_atomic_write_uses_temp_file_and_original_untouched_on_failure(self):
        # os.replaceが失敗するケースを模擬し、元ファイルが無傷であることを確認する。
        seed = [_rec("https://x.com/a/status/1", "t1")]
        self._write_seed_file(seed)

        import fetch_bookmarks as fb

        original_replace = os.replace

        def _boom(_src, _dst):
            raise OSError("simulated os.replace failure")

        os.replace = _boom
        try:
            with self.assertRaises(OSError):
                atomic_write_jsonl(self.out_path, [_rec("https://x.com/a/status/2", "t2")], min_count=0)
        finally:
            os.replace = original_replace

        # 元ファイルが無傷であること
        result = self._read_jsonl(self.out_path)
        self.assertEqual(result, seed)

        # 一時ファイルが残っていないこと（クリーンアップされていること）
        remaining = [f for f in os.listdir(self._tmpdir.name) if f != "bookmarks.jsonl"]
        self.assertEqual(remaining, [], "一時ファイルが後始末されていること")


class TestOverwriteModeSourceUnaffected(unittest.TestCase):
    """--appendなし（上書きモード）の最終書き出し分岐が、mergeや原子的書き出しを
    経由せず「今回取得分(all_bookmarks)のみを書き直す」従来ロジックのままであることを
    ソース上で確認する。この分岐はPlaywrightで取得したpage/context前提のfetch_bookmarks()
    内部コードのため、end-to-endではなくソース検査で回帰を検知する。
    """

    def test_overwrite_branch_still_writes_all_bookmarks_only(self):
        source = Path(fetch_bookmarks.__file__).read_text(encoding="utf-8")
        marker = "elif filled and out_path:"
        self.assertIn(marker, source, "上書きモード分岐が存在すること")
        branch_start = source.index(marker)
        branch_end = source.index("elapsed = (time.time() - start_time)", branch_start)
        branch_code = source[branch_start:branch_end]
        self.assertIn("for bm in all_bookmarks:", branch_code)
        self.assertNotIn("merge_bookmark_records", branch_code)
        self.assertNotIn("atomic_write_jsonl", branch_code)


class TestWriteStatusJson(unittest.TestCase):
    """--status-json 出力（P0-2対策: exit 0でも0件取得をwrapper側が検知できるように）。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.status_path = os.path.join(self._tmpdir.name, "status.json")

    def test_writes_saved_dom_graphql_counts_and_reason(self):
        dom = {"https://x.com/a/status/1": object()}
        gql = {
            "https://x.com/a/status/1": object(),
            "https://x.com/a/status/2": object(),
        }
        saved = [object(), object(), object()]

        _write_status_json(self.status_path, dom, gql, saved, "max_empty")

        with open(self.status_path, encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload, {
            "saved": 3,
            "dom_count": 1,
            "graphql_count": 2,
            # dom(1件)とgql(2件)はstatus/1が重複するため、和集合は2件
            # （1+2=3の単純合算ではないことを確認する）。
            "observed_url_count": 2,
            "stopped_reason": "max_empty",
        })

    def test_dom_count_zero_is_preserved_not_omitted(self):
        # selector死・ログイン壁でDOM取得が0件だったケースをwrapper側が
        # dom_count==0として読み取れることを確認する（SUCCESS誤分類対策の核）。
        _write_status_json(self.status_path, {}, {}, [], "error")

        with open(self.status_path, encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["dom_count"], 0)
        self.assertEqual(payload["saved"], 0)
        self.assertEqual(payload["observed_url_count"], 0)
        self.assertEqual(payload["stopped_reason"], "error")

    def test_creates_parent_directory(self):
        nested_path = os.path.join(self._tmpdir.name, "nested", "dir", "status.json")
        _write_status_json(nested_path, {}, {}, [], "end")
        self.assertTrue(os.path.exists(nested_path))


if __name__ == "__main__":
    unittest.main()
