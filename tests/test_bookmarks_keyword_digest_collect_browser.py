"""bookmarks_keyword_digest_collect_browser.py のpureヘルパーfixtureテスト。

xai_sdk版digest収集(bookmarks_keyword_digest_collect.py)の無料ブラウザ収集版ツイン。
出力契約(digest_raw/digest_status)はxai_sdk版と同一で、apply側テストは
test_bookmarks_keyword_digest.py側でカバー済み。本ファイルはplaywright非依存の
pureヘルパー（代表クエリ選定・検索URL組立・likes文字列変換・DOM由来候補の正規化）のみを
検証する。playwright依存部（scrape_search_results_from_dom/collect_for_cluster内の
ページ操作/main）はhostにplaywrightブラウザ実体が無いため実行しない
（実走検証は別バッチでDocker/xstock-vnc上のPlaywrightで行う）。
このテストファイル自身がモジュール先頭で
`import bookmarks_keyword_digest_collect_browser`することが、
「playwrightなしでもmoduleがimportできる」ことの直接的な証明になる（遅延import設計の検証）。

実行:
    python3 -m unittest tests.test_bookmarks_keyword_digest_collect_browser -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bookmarks_keyword_digest_collect_browser as browser_collect  # noqa: E402


class SelectRepresentativeQueryTest(unittest.TestCase):
    def test_empty_queries_returns_none_none(self):
        self.assertEqual(browser_collect.select_representative_query({"queries": []}), (None, None))

    def test_missing_queries_key_returns_none_none(self):
        self.assertEqual(browser_collect.select_representative_query({}), (None, None))

    def test_picks_min_faves_median_query_with_operators_intact(self):
        cluster = {
            "queries": [
                {"query_id": "q-1", "q": '"Claude Code" min_faves:100 lang:ja -filter:replies'},
                {"query_id": "q-2", "q": '"Claude Code" min_faves:200 lang:ja -filter:replies'},
                {"query_id": "q-3", "q": '"Claude Code" min_faves:300 lang:ja -filter:replies'},
            ]
        }
        query_id, q = browser_collect.select_representative_query(cluster)
        self.assertEqual(query_id, "q-2")
        self.assertEqual(q, '"Claude Code" min_faves:200 lang:ja -filter:replies')

    def test_no_min_faves_falls_back_to_first_query(self):
        cluster = {
            "queries": [
                {"query_id": "q-1", "q": "何か lang:ja"},
                {"query_id": "q-2", "q": "他のクエリ lang:ja"},
            ]
        }
        self.assertEqual(browser_collect.select_representative_query(cluster), ("q-1", "何か lang:ja"))


class BuildSearchUrlTest(unittest.TestCase):
    def test_appends_since_and_encodes(self):
        now = datetime(2026, 7, 13, tzinfo=timezone.utc)
        url = browser_collect.build_search_url('"Claude Code" min_faves:150 lang:ja', 7, now=now)
        self.assertIn("since%3A2026-07-06", url)
        self.assertTrue(url.startswith("https://x.com/search?q="))
        self.assertTrue(url.endswith("&f=live"))

    def test_does_not_duplicate_existing_since(self):
        now = datetime(2026, 7, 13, tzinfo=timezone.utc)
        url = browser_collect.build_search_url("何か since:2026-01-01", 7, now=now)
        self.assertEqual(url.count("since%3A"), 1)
        self.assertIn("since%3A2026-01-01", url)


class ParseLikesTextTest(unittest.TestCase):
    def test_comma_separated(self):
        self.assertEqual(browser_collect.parse_likes_text("1,234"), 1234)

    def test_japanese_man_suffix(self):
        self.assertEqual(browser_collect.parse_likes_text("1.5万"), 15000)

    def test_japanese_oku_suffix(self):
        self.assertEqual(browser_collect.parse_likes_text("2億"), 200_000_000)

    def test_english_k_suffix(self):
        self.assertEqual(browser_collect.parse_likes_text("12.3K"), 12300)

    def test_english_m_suffix(self):
        self.assertEqual(browser_collect.parse_likes_text("4.5M"), 4_500_000)

    def test_plain_digits(self):
        self.assertEqual(browser_collect.parse_likes_text("42"), 42)

    def test_zero_and_empty(self):
        self.assertEqual(browser_collect.parse_likes_text("0"), 0)
        self.assertEqual(browser_collect.parse_likes_text(""), 0)
        self.assertEqual(browser_collect.parse_likes_text(None), 0)

    def test_unparseable_falls_back_to_zero(self):
        self.assertEqual(browser_collect.parse_likes_text("いいね"), 0)


class IsLoginWallUrlTest(unittest.TestCase):
    def test_actual_observed_onboarding_redirect_2026_07_13(self):
        """2026-07-13の実走診断で観測した実際のリダイレクト先(/i/jf/onboarding/web?...&mode=login)。
        当初の`/login`・`/i/flow/login`のみのチェックでは検知できずwait_timeoutとして
        黙って0件になっていた不具合の再発防止。"""
        url = (
            "https://x.com/i/jf/onboarding/web?redirect_after_login=%2Fsearch%3Fq%3Dfoo&mode=login"
        )
        self.assertTrue(browser_collect.is_login_wall_url(url))

    def test_flow_login_path(self):
        self.assertTrue(browser_collect.is_login_wall_url("https://x.com/i/flow/login"))

    def test_plain_login_path(self):
        self.assertTrue(browser_collect.is_login_wall_url("https://x.com/login"))

    def test_normal_search_result_is_not_login_wall(self):
        self.assertFalse(browser_collect.is_login_wall_url("https://x.com/search?q=foo&f=live"))

    def test_empty_url_is_not_login_wall(self):
        self.assertFalse(browser_collect.is_login_wall_url(""))


class ExtractAuthorFromUrlTest(unittest.TestCase):
    def test_extracts_handle(self):
        self.assertEqual(browser_collect.extract_author_from_url("https://x.com/foo/status/12345"), "foo")

    def test_no_status_returns_empty(self):
        self.assertEqual(browser_collect.extract_author_from_url("https://x.com/foo"), "")

    def test_empty_url_returns_empty(self):
        self.assertEqual(browser_collect.extract_author_from_url(""), "")


class BuildCandidateFromRawTest(unittest.TestCase):
    def test_full_fields_normalized_to_contract_shape(self):
        candidate = browser_collect.build_candidate_from_raw(
            url="https://x.com/foo/status/12345",
            author="foo",
            content="本文テスト",
            likes_text="1,234",
            datetime_attr="2026-07-01T09:00:00.000Z",
        )
        self.assertEqual(candidate, {
            "id": "12345",
            "url": "https://x.com/foo/status/12345",
            "author": "foo",
            "content": "本文テスト",
            "likes": 1234,
            "impressions": 0,
            "posted_at": "2026-07-01",
        })

    def test_missing_status_id_yields_empty_id(self):
        candidate = browser_collect.build_candidate_from_raw(
            url="https://x.com/foo", author="foo", content="x", likes_text="", datetime_attr="",
        )
        self.assertEqual(candidate["id"], "")
        self.assertEqual(candidate["posted_at"], "")

    def test_normalize_item_roundtrip_matches_xai_contract_schema(self):
        """digest_collect.normalize_item()に渡した後の最終行がxai_sdk版と同じキー集合を持つこと。"""
        import bookmarks_keyword_digest_collect as digest_collect

        candidate = browser_collect.build_candidate_from_raw(
            url="https://x.com/foo/status/12345",
            author="foo",
            content="本文",
            likes_text="42",
            datetime_attr="2026-07-01T09:00:00.000Z",
        )
        row = digest_collect.normalize_item(candidate, "c-1", "q-1")
        self.assertEqual(
            set(row.keys()),
            {"url", "id", "author", "content", "likes", "impressions", "posted_at", "cluster_id", "seed_query_id"},
        )
        self.assertEqual(row["cluster_id"], "c-1")
        self.assertEqual(row["seed_query_id"], "q-1")
        self.assertEqual(row["likes"], 42)


class BuildContextKwargsTest(unittest.TestCase):
    """C6: 掲載記事本文が英語自動翻訳されていた事象への対策(locale/timezone/Accept-Language)。"""

    def test_locale_timezone_and_accept_language_set(self):
        kwargs = browser_collect.build_context_kwargs()
        self.assertEqual(kwargs["locale"], "ja-JP")
        self.assertEqual(kwargs["timezone_id"], "Asia/Tokyo")
        self.assertEqual(kwargs["extra_http_headers"], {"Accept-Language": "ja"})
        self.assertEqual(kwargs["user_agent"], browser_collect.USER_AGENT)

    def test_user_agent_overridable(self):
        kwargs = browser_collect.build_context_kwargs(user_agent="custom-ua")
        self.assertEqual(kwargs["user_agent"], "custom-ua")


class ModuleImportableWithoutPlaywrightTest(unittest.TestCase):
    def test_no_playwright_import_at_module_level(self):
        """モジュールソース中、トップレベルではplaywrightをimportしていないことを確認する
        （main()実行時のみ遅延importする設計を静的にも保証する）。playwright非依存を謳う
        コメント行(例: "# noqa: ... playwright非依存")を誤検知しないよう、実際にplaywright
        パッケージをimportしている行(import playwright / from playwright ...)のみを見る。"""
        src = Path(browser_collect.__file__).read_text(encoding="utf-8")
        top_level_playwright_imports = [
            ln for ln in src.splitlines()
            if not ln.startswith((" ", "\t"))
            and (ln.strip().startswith("import playwright") or ln.strip().startswith("from playwright"))
        ]
        self.assertEqual(top_level_playwright_imports, [])


if __name__ == "__main__":
    unittest.main()
