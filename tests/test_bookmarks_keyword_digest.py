"""bookmarks_keyword_digest_collect.py / bookmarks_keyword_digest_apply.py のfixtureテスト。

Xブックマーク→キーワード抽出パイプラインの「候補記事ダイジェスト」機能（収集はDocker実行の
xai_sdk依存、確定はhost柵のstdlibのみ）を検証する。設計正本: ~/.claude/docs/x-keywords-plan.md
末尾セクション「Plan: x-keywords v2 — AI 検索代行ダイジェスト」（C1バッチ）。

collect側はpureヘルパー（プロンプト組立・item正規化・status構築・min_faves中央値算出）のみを
テストする。xai_sdk依存部分（call_x_search/main）はhostにxai_sdkが無いため実行しない。
このテストファイル自身がモジュール先頭で`import bookmarks_keyword_digest_collect`することが、
「xai_sdkなしでもmoduleがimportできる」ことの直接的な証明になる（遅延import設計の検証）。

実行:
    python3 -m unittest tests.test_bookmarks_keyword_digest -v
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bookmarks_keyword_common as common  # noqa: E402
import bookmarks_keyword_digest_apply as apply_mod  # noqa: E402
import bookmarks_keyword_digest_collect as collect  # noqa: E402


# --- collect: pure ヘルパー ------------------------------------------------------

class ExtractMinFavesTest(unittest.TestCase):
    def test_extracts_value(self):
        self.assertEqual(collect.extract_min_faves('"Claude Code" min_faves:150 lang:ja'), 150)

    def test_none_when_absent(self):
        self.assertIsNone(collect.extract_min_faves('"Claude Code" lang:ja'))

    def test_none_for_empty_or_none(self):
        self.assertIsNone(collect.extract_min_faves(""))
        self.assertIsNone(collect.extract_min_faves(None))

    def test_zero_value(self):
        self.assertEqual(collect.extract_min_faves("min_faves:0"), 0)


class SelectMinLikesAndSeedTest(unittest.TestCase):
    def test_empty_queries_falls_back(self):
        self.assertEqual(collect.select_min_likes_and_seed([]), (80, None))

    def test_no_min_faves_falls_back(self):
        queries = [{"q": "何か lang:ja", "query_id": "q-1"}]
        self.assertEqual(collect.select_min_likes_and_seed(queries), (80, None))

    def test_single_query(self):
        queries = [{"q": "min_faves:100", "query_id": "q-1"}]
        self.assertEqual(collect.select_min_likes_and_seed(queries), (100, "q-1"))

    def test_odd_count_true_median(self):
        queries = [
            {"q": "min_faves:100", "query_id": "q-1"},
            {"q": "min_faves:200", "query_id": "q-2"},
            {"q": "min_faves:300", "query_id": "q-3"},
        ]
        self.assertEqual(collect.select_min_likes_and_seed(queries), (200, "q-2"))

    def test_even_count_lower_median(self):
        queries = [
            {"q": "min_faves:100", "query_id": "q-1"},
            {"q": "min_faves:200", "query_id": "q-2"},
            {"q": "min_faves:300", "query_id": "q-3"},
            {"q": "min_faves:400", "query_id": "q-4"},
        ]
        # ソート後(100,200,300,400) idx=(4-1)//2=1 -> 200件目のクエリ(q-2)
        self.assertEqual(collect.select_min_likes_and_seed(queries), (200, "q-2"))

    def test_mixed_with_and_without_min_faves(self):
        queries = [
            {"q": "min_faves:100", "query_id": "q-1"},
            {"q": "絞り込みなし", "query_id": "q-2"},
        ]
        self.assertEqual(collect.select_min_likes_and_seed(queries), (100, "q-1"))


class BuildDigestPromptTest(unittest.TestCase):
    def test_includes_why_keywords_and_min_likes(self):
        cluster = {
            "cluster_id": "c-1",
            "name": "Claude Code 運用ノウハウ",
            "why": "自分の運用を良くしたいから",
            "keywords": [{"keyword": "Claude Code", "mode": "ascii_token"}, {"keyword": "サブエージェント", "mode": "normalized_substring"}],
        }
        prompt = collect.build_digest_prompt(cluster, min_likes=150, days=7, max_per_cluster=10)
        self.assertIn("Claude Code 運用ノウハウ", prompt)
        self.assertIn("自分の運用を良くしたいから", prompt)
        self.assertIn("Claude Code", prompt)
        self.assertIn("サブエージェント", prompt)
        self.assertIn("150", prompt)
        self.assertIn("7", prompt)
        self.assertIn("10", prompt)


class NormalizeItemTest(unittest.TestCase):
    def test_maps_fields_and_attaches_cluster_seed(self):
        candidate = {
            "id": "12345", "url": "https://x.com/foo/status/12345", "author": "foo",
            "content": "本文", "likes": 42, "impressions": 999, "posted_at": "2026-07-01",
        }
        item = collect.normalize_item(candidate, "c-1", "q-1")
        self.assertEqual(item["cluster_id"], "c-1")
        self.assertEqual(item["seed_query_id"], "q-1")
        self.assertEqual(item["likes"], 42)
        self.assertEqual(item["url"], "https://x.com/foo/status/12345")

    def test_missing_fields_default(self):
        item = collect.normalize_item({}, "c-1", None)
        self.assertEqual(item["url"], "")
        self.assertEqual(item["likes"], 0)
        self.assertIsNone(item["seed_query_id"])


class BuildStatusTest(unittest.TestCase):
    def test_all_fields_present(self):
        status = collect.build_status(
            run_id="r1", source={"generation": 1, "revision": 0, "urls_sha256": "x"},
            per_cluster_counts={"c-1": 3}, errors=[], generated_at="2026-07-01T00:00:00+00:00",
            collect_days=7, complete=True,
        )
        self.assertEqual(status["run_id"], "r1")
        self.assertTrue(status["complete"])
        self.assertEqual(status["collect_days"], 7)
        self.assertEqual(status["per_cluster_counts"], {"c-1": 3})


# --- apply: fixture ヘルパー ------------------------------------------------------

def _sid(dt: datetime) -> str:
    """dtに対応するSnowflake風IDを生成する（apply_mod.X_EPOCH_MSを基準に逆算）。"""
    ms = int(dt.timestamp() * 1000)
    return str((ms - apply_mod.X_EPOCH_MS) << 22)


def _item(id_str, author="foo", likes=100, content="いい記事です", cluster_id="c-1",
          seed_query_id="q-1", posted_at="2026-07-10", url=None):
    if url is None:
        url = f"https://x.com/{author}/status/{id_str}"
    return {
        "url": url, "id": id_str, "author": author, "content": content, "likes": likes,
        "impressions": 1000, "posted_at": posted_at, "cluster_id": cluster_id,
        "seed_query_id": seed_query_id,
    }


def _write_jsonl(path: Path, rows) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class ApplyTestBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        root = Path(self._tmpdir.name)
        self.inbox_path = root / "digest_inbox"
        self.inbox_path.mkdir()
        self.ledger_path = root / "ledger.jsonl"
        self.raw_path = root / "bookmarks.jsonl"
        self.latest_path = root / "latest.json"
        self.now = datetime.now(timezone.utc)

        _write_jsonl(self.raw_path, [])
        common.append_event(self.ledger_path, {
            "type": "generation", "generation": 1, "revision": 0,
            "generation_reason": "baseline", "active_clusters": [], "dormant_clusters": [],
        })
        self._write_latest([
            {"cluster_id": "c-1", "queries": [{"query_id": "q-1", "q": "min_faves:125 lang:ja"}]},
            {"cluster_id": "c-2", "queries": [{"query_id": "q-2", "q": "min_faves:125 lang:ja"}]},
            {"cluster_id": "c-3", "queries": [{"query_id": "q-3", "q": "min_faves:125 lang:ja"}]},
        ])

    def _write_latest(self, clusters, urls_sha256="abc"):
        # source_snapshot.urls_sha256は既定で_seed_pairのstatus.source.urls_sha256("abc")と
        # 一致させ、sha照合(current_source_urls_sha256)が既定では通る状態にする。
        self.latest_path.write_text(
            json.dumps(
                {"active_clusters": clusters, "source_snapshot": {"urls_sha256": urls_sha256}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _write_bookmarks(self, urls):
        rows = [{"url": u, "text": "既存ブックマーク本文"} for u in urls]
        _write_jsonl(self.raw_path, rows)

    def _seed_pair(self, items, run_id="run-1", generation=1, revision=0,
                   generated_at=None, complete=True, collect_days=7):
        raw_path = self.inbox_path / f"digest_raw-{run_id}.jsonl"
        status_path = self.inbox_path / f"digest_status-{run_id}.json"
        _write_jsonl(raw_path, items)
        status = {
            "run_id": run_id,
            "source": {"generation": generation, "revision": revision, "urls_sha256": "abc"},
            "generated_at": (generated_at or (self.now - timedelta(minutes=10))).isoformat(),
            "collect_days": collect_days,
            "per_cluster_counts": {},
            "errors": [],
            "complete": complete,
        }
        status_path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        return raw_path, status_path

    def _args(self, min_publish=8, per_cluster=3, total=20, html_out=None, run_id=None):
        # html_outは常にtempdir配下を既定にする（未指定時にcwd相対の実output/を
        # 汚さないようにするため。実際のwrapper/CLI経由ではrender_html.DEFAULT_OUTが使われる）。
        root = Path(self._tmpdir.name)
        return argparse.Namespace(
            inbox=str(self.inbox_path), ledger=str(self.ledger_path), raw=str(self.raw_path),
            latest=str(self.latest_path), min_publish=min_publish, per_cluster=per_cluster, total=total,
            html_out=html_out or str(root / "x_keywords.html"), run_id=run_id,
        )

    def _last_ledger_event(self):
        events = common.read_ledger(self.ledger_path)
        return events[-1] if events else None

    def _digest_events(self):
        return [e for e in common.read_ledger(self.ledger_path) if e.get("type") == "digest"]


class ApplySuccessTest(ApplyTestBase):
    def test_applied_moves_to_processed_and_appends_event(self):
        items = [
            _item(_sid(self.now - timedelta(days=1, seconds=i)), author=f"u{i}", likes=200 + i, cluster_id="c-1", seed_query_id="q-1")
            for i in range(10)
        ]
        raw_path, status_path = self._seed_pair(items)

        rc = apply_mod.run_apply(self._args(min_publish=8, per_cluster=20, total=20), self.now)

        self.assertEqual(rc, 0)
        digest_events = self._digest_events()
        self.assertEqual(len(digest_events), 1)
        self.assertEqual(len(digest_events[0]["items"]), 10)
        self.assertFalse(raw_path.exists())
        self.assertFalse(status_path.exists())
        processed_dir = self.inbox_path / "processed"
        self.assertTrue((processed_dir / raw_path.name).exists())
        self.assertTrue((processed_dir / status_path.name).exists())

    def test_render_html_regenerated_after_digest_applied(self):
        """C2配線: DIGEST_APPLIED後にrender_html.render_to_file()が呼ばれHTMLが再生成されること。"""
        items = [
            _item(_sid(self.now - timedelta(days=1, seconds=i)), author=f"u{i}", likes=200 + i, cluster_id="c-1", seed_query_id="q-1")
            for i in range(10)
        ]
        self._seed_pair(items)
        html_out = Path(self._tmpdir.name) / "x_keywords.html"

        rc = apply_mod.run_apply(
            self._args(min_publish=8, per_cluster=20, total=20, html_out=str(html_out)), self.now
        )

        self.assertEqual(rc, 0)
        self.assertTrue(html_out.exists())
        text = html_out.read_text(encoding="utf-8")
        self.assertIn("今週の候補記事", text)
        self.assertIn("u0", text)  # 選定itemの著者名が反映されている

    def test_render_html_failure_is_warn_only_and_exit_stays_zero(self):
        """render_html側の例外はfail-soft: 台帳appendは維持されexit 0のまま(WARNログのみ)。"""
        import unittest.mock as mock

        items = [
            _item(_sid(self.now - timedelta(days=1, seconds=i)), author=f"u{i}", likes=200 + i)
            for i in range(8)
        ]
        self._seed_pair(items)

        with mock.patch.object(apply_mod.render_html, "render_to_file", side_effect=RuntimeError("boom")):
            rc = apply_mod.run_apply(self._args(min_publish=8, per_cluster=20, total=20), self.now)

        self.assertEqual(rc, 0)
        self.assertEqual(len(self._digest_events()), 1, "render失敗でも台帳appendは維持される")

    def test_excerpt_truncated_and_escaped(self):
        long_content = ("あ" * 130) + "|table|break"
        items = [
            _item(_sid(self.now - timedelta(days=1, seconds=i)), author=f"u{i}", likes=200, content=long_content)
            for i in range(8)
        ]
        self._seed_pair(items)
        rc = apply_mod.run_apply(self._args(min_publish=8, per_cluster=20, total=20), self.now)
        self.assertEqual(rc, 0)
        excerpt = self._digest_events()[0]["items"][0]["excerpt"]
        self.assertLessEqual(len(excerpt.replace("\\|", "|")), 120)
        self.assertNotIn("|table", excerpt)  # エスケープされ \\| になっている


class ApplyDiscoveryTest(ApplyTestBase):
    def test_run_id_mismatch_treated_as_no_pair(self):
        items = [_item(_sid(self.now - timedelta(days=1))) for _ in range(8)]
        raw_path, status_path = self._seed_pair(items, run_id="run-A")
        # ファイル名run-Aだが内容run_idはrun-B（改ざん/取り違えを模擬）
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["run_id"] = "run-B"
        status_path.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")

        with self._capture_stdout() as out:
            rc = apply_mod.run_apply(self._args(), self.now)
        self.assertEqual(rc, 0)
        self.assertIn("DIGEST_SKIPPED", out.getvalue())
        self.assertEqual(self._digest_events(), [])

    def test_empty_inbox_is_skipped(self):
        with self._capture_stdout() as out:
            rc = apply_mod.run_apply(self._args(), self.now)
        self.assertEqual(rc, 0)
        self.assertIn("DIGEST_SKIPPED", out.getvalue())

    def _capture_stdout(self):
        import contextlib
        import io
        return contextlib.redirect_stdout(io.StringIO())


class ApplyRunIdTest(ApplyTestBase):
    """--run-id指定時はそのペアのみを対象にする(collectが渡すrun固定・wrapper連携用)。"""

    def test_run_id_pins_to_specific_pair_ignoring_newer(self):
        items_old = [_item(_sid(self.now - timedelta(days=1, seconds=i)), author=f"old{i}", likes=200) for i in range(8)]
        self._seed_pair(items_old, run_id="run-old", generated_at=self.now - timedelta(minutes=30))
        items_new = [_item(_sid(self.now - timedelta(days=1, seconds=i)), author=f"new{i}", likes=200) for i in range(8)]
        self._seed_pair(items_new, run_id="run-new", generated_at=self.now - timedelta(minutes=5))

        rc = apply_mod.run_apply(
            self._args(min_publish=8, per_cluster=20, total=20, run_id="run-old"), self.now
        )
        self.assertEqual(rc, 0)
        event = self._digest_events()[0]
        authors = {it["author"] for it in event["items"]}
        self.assertEqual(authors, {f"old{i}" for i in range(8)})
        # run-newのペアは未処理のままinboxに残る（run固定=別run_idには一切触れない）
        self.assertTrue((self.inbox_path / "digest_raw-run-new.jsonl").exists())

    def test_unknown_run_id_is_skipped(self):
        items = [_item(_sid(self.now - timedelta(days=1, seconds=i))) for i in range(8)]
        self._seed_pair(items, run_id="run-exists")

        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = apply_mod.run_apply(self._args(run_id="run-does-not-exist"), self.now)
        self.assertEqual(rc, 0)
        self.assertIn("DIGEST_SKIPPED", out.getvalue())
        self.assertEqual(self._digest_events(), [])


class ApplyStalenessTest(ApplyTestBase):
    def test_complete_false_skips(self):
        items = [_item(_sid(self.now - timedelta(days=1))) for _ in range(8)]
        self._seed_pair(items, complete=False)
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = apply_mod.run_apply(self._args(), self.now)
        self.assertEqual(rc, 0)
        self.assertIn("SKIPPED_STALE", out.getvalue())
        self.assertIn("not_complete", out.getvalue())
        self.assertEqual(self._digest_events(), [])

    def test_stale_over_24h_skips(self):
        items = [_item(_sid(self.now - timedelta(days=1))) for _ in range(8)]
        self._seed_pair(items, generated_at=self.now - timedelta(hours=25))
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = apply_mod.run_apply(self._args(), self.now)
        self.assertEqual(rc, 0)
        self.assertIn("SKIPPED_STALE", out.getvalue())
        self.assertIn("stale_or_future", out.getvalue())
        self.assertEqual(self._digest_events(), [])

    def test_generation_mismatch_skips(self):
        items = [_item(_sid(self.now - timedelta(days=1))) for _ in range(8)]
        self._seed_pair(items, generation=2)  # 台帳はgeneration=1のまま
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = apply_mod.run_apply(self._args(), self.now)
        self.assertEqual(rc, 0)
        self.assertIn("SKIPPED_STALE", out.getvalue())
        self.assertIn("generation_mismatch", out.getvalue())
        self.assertEqual(self._digest_events(), [])

    def test_sha_mismatch_skips(self):
        """statusのsource.urls_sha256がlatest.json側snapshotと不一致ならSKIPPED_STALE。"""
        self._write_latest(
            [{"cluster_id": "c-1", "queries": [{"query_id": "q-1", "q": "min_faves:125 lang:ja"}]}],
            urls_sha256="current-sha",
        )
        items = [_item(_sid(self.now - timedelta(days=1, seconds=i))) for i in range(8)]
        self._seed_pair(items)  # status.source.urls_sha256は既定"abc"（不一致）
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = apply_mod.run_apply(self._args(), self.now)
        self.assertEqual(rc, 0)
        self.assertIn("SKIPPED_STALE", out.getvalue())
        self.assertIn("sha_mismatch", out.getvalue())
        self.assertEqual(self._digest_events(), [])

    def test_sha_both_none_allowed(self):
        """latest.json側にsource_snapshotが無く台帳にもgeneration.source_snapshotが無い場合、
        statusのurls_sha256もNoneであれば一致とみなし処理を続行する。"""
        self.latest_path.write_text(json.dumps({"active_clusters": [
            {"cluster_id": "c-1", "queries": [{"query_id": "q-1", "q": "min_faves:125 lang:ja"}]},
        ]}), encoding="utf-8")  # source_snapshotキー無し -> current側はNone
        items = [_item(_sid(self.now - timedelta(days=1, seconds=i)), likes=200 + i) for i in range(8)]
        raw_path = self.inbox_path / "digest_raw-run-none.jsonl"
        status_path = self.inbox_path / "digest_status-run-none.json"
        _write_jsonl(raw_path, items)
        status = {
            "run_id": "run-none",
            "source": {"generation": 1, "revision": 0, "urls_sha256": None},
            "generated_at": (self.now - timedelta(minutes=10)).isoformat(),
            "collect_days": 7, "per_cluster_counts": {}, "errors": [], "complete": True,
        }
        status_path.write_text(json.dumps(status), encoding="utf-8")

        rc = apply_mod.run_apply(self._args(min_publish=8, per_cluster=20, total=20), self.now)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._digest_events()), 1)


class ApplyForgedUrlTest(ApplyTestBase):
    def test_six_forgery_variants_all_dropped(self):
        good = [
            _item(_sid(self.now - timedelta(days=1, seconds=i)), author=f"good{i}", likes=200 + i)
            for i in range(8)
        ]
        good_id = _sid(self.now - timedelta(days=1, seconds=999))
        forged = [
            _item(good_id, author="bad1", likes=200, url=f"http://x.com/bad1/status/{good_id}"),  # httpスキーム
            _item(good_id, author="bad2", likes=200, url=f"https://evil.com/bad2/status/{good_id}"),  # 別host
            _item(good_id, author="bad3", likes=200, url=f"https://x.com/bad3/notstatus/{good_id}"),  # path不正
            _item(good_id, author="bad4", likes=200, url="https://x.com/bad4/status/99999999999999"),  # id不一致
            _item(_sid(self.now - timedelta(days=100)), author="bad5", likes=200),  # Snowflake窓外
            {**_item(_sid(self.now - timedelta(days=1)), author="bad6", likes=200), "likes": "200"},  # likes型不正
        ]
        self._seed_pair(good + forged)

        rc = apply_mod.run_apply(self._args(min_publish=8, per_cluster=20, total=20), self.now)
        self.assertEqual(rc, 0)

        event = self._digest_events()[0]
        self.assertEqual(len(event["items"]), 8)
        authors = {it["author"] for it in event["items"]}
        self.assertEqual(authors, {f"good{i}" for i in range(8)})

        dropped = event["dropped_counts"]
        self.assertGreaterEqual(dropped.get("bad_url_or_id", 0), 3)  # http/別host/path不正/id不一致のうち少なくとも一部
        self.assertEqual(dropped.get("snowflake_out_of_window", 0), 1)
        self.assertEqual(dropped.get("likes_type", 0), 1)


class ApplyUnknownIdTest(ApplyTestBase):
    """cluster_id/seed_query_idがlatest.jsonに実在しないitemのdrop + min-likes索引不在時の
    80下限フォールバック(索引が引けない場合に無条件通過してしまう迂回を塞ぐ)。"""

    def test_unknown_cluster_dropped(self):
        good = [_item(_sid(self.now - timedelta(days=1, seconds=i)), author=f"u{i}", likes=200) for i in range(8)]
        ghost = _item(
            _sid(self.now - timedelta(days=1, seconds=999)), author="ghost", likes=500,
            cluster_id="c-999", seed_query_id=None,
        )
        self._seed_pair(good + [ghost])

        rc = apply_mod.run_apply(self._args(min_publish=8, per_cluster=20, total=20), self.now)
        self.assertEqual(rc, 0)
        event = self._digest_events()[0]
        self.assertEqual(len(event["items"]), 8)
        self.assertEqual(event["dropped_counts"].get("unknown_cluster"), 1)

    def test_unknown_seed_query_dropped(self):
        good = [_item(_sid(self.now - timedelta(days=1, seconds=i)), author=f"u{i}", likes=200) for i in range(8)]
        forged_seed = _item(
            _sid(self.now - timedelta(days=1, seconds=999)), author="ghost2", likes=500,
            cluster_id="c-1", seed_query_id="q-does-not-belong-to-c-1",
        )
        self._seed_pair(good + [forged_seed])

        rc = apply_mod.run_apply(self._args(min_publish=8, per_cluster=20, total=20), self.now)
        self.assertEqual(rc, 0)
        event = self._digest_events()[0]
        self.assertEqual(len(event["items"]), 8)
        self.assertEqual(event["dropped_counts"].get("unknown_seed"), 1)

    def test_missing_min_faves_index_applies_80_floor(self):
        # c-4のクエリはmin_faves:を含まない -> build_min_likes_indexに(c-4,q-4)は登録されない。
        # 索引不在でも一律MIN_LIKES_INDEX_FALLBACK(80)*LIKES_FLOOR_RATIO(0.8)=64が下限になる。
        self._write_latest([
            {"cluster_id": "c-1", "queries": [{"query_id": "q-1", "q": "min_faves:125 lang:ja"}]},
            {"cluster_id": "c-4", "queries": [{"query_id": "q-4", "q": "何か lang:ja"}]},
        ])
        good = [
            _item(_sid(self.now - timedelta(days=1, seconds=i)), author=f"u{i}", likes=200,
                  cluster_id="c-1", seed_query_id="q-1")
            for i in range(8)
        ]
        below_floor = _item(
            _sid(self.now - timedelta(days=1, seconds=999)), author="lowlikes", likes=50,
            cluster_id="c-4", seed_query_id="q-4",
        )
        self._seed_pair(good + [below_floor])

        rc = apply_mod.run_apply(self._args(min_publish=8, per_cluster=20, total=20), self.now)
        self.assertEqual(rc, 0)
        event = self._digest_events()[0]
        self.assertEqual(len(event["items"]), 8)
        self.assertEqual(event["dropped_counts"].get("likes_below_floor"), 1)


class ApplyExclusionTest(ApplyTestBase):
    def test_already_bookmarked_excluded(self):
        existing_id = _sid(self.now - timedelta(days=1, seconds=999))
        existing_url = f"https://x.com/existing/status/{existing_id}"
        self._write_bookmarks([existing_url])
        good = [_item(_sid(self.now - timedelta(days=1, seconds=i)), author=f"u{i}", likes=200) for i in range(8)]
        dup = _item(existing_id, author="existing", likes=200, url=existing_url)
        self._seed_pair(good + [dup])

        rc = apply_mod.run_apply(self._args(min_publish=8, per_cluster=20, total=20), self.now)
        self.assertEqual(rc, 0)
        event = self._digest_events()[0]
        self.assertEqual(len(event["items"]), 8)
        self.assertEqual(event["dropped_counts"].get("already_bookmarked"), 1)

    def test_already_published_excluded(self):
        published_id = _sid(self.now - timedelta(days=1, seconds=999))
        published_url = f"https://x.com/published/status/{published_id}"
        common.append_event(self.ledger_path, {
            "type": "digest", "run_id": "prior", "source": {"generation": 1, "revision": 0, "urls_sha256": "abc"},
            "items": [{"url": published_url, "cluster_id": "c-1", "seed_query_id": "q-1",
                       "excerpt": "既掲載", "author": "published", "likes": 300, "posted_at": "2026-07-01"}],
            "dropped_counts": {}, "errors": [],
        })
        good = [_item(_sid(self.now - timedelta(days=1, seconds=i)), author=f"u{i}", likes=200) for i in range(8)]
        dup = _item(published_id, author="published", likes=200, url=published_url)
        self._seed_pair(good + [dup])

        rc = apply_mod.run_apply(self._args(min_publish=8, per_cluster=20, total=20), self.now)
        self.assertEqual(rc, 0)
        event = self._digest_events()[-1]
        self.assertEqual(len(event["items"]), 8)
        self.assertEqual(event["dropped_counts"].get("already_published"), 1)


class ApplyDegradedTest(ApplyTestBase):
    def test_below_min_publish_no_ledger_write(self):
        items = [_item(_sid(self.now - timedelta(days=1, seconds=i)), author=f"u{i}", likes=200) for i in range(3)]
        raw_path, status_path = self._seed_pair(items)

        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = apply_mod.run_apply(self._args(min_publish=8), self.now)
        self.assertEqual(rc, 0)
        self.assertIn("DIGEST_DEGRADED", out.getvalue())
        self.assertIn("n=3", out.getvalue())
        self.assertEqual(self._digest_events(), [])
        # 台帳に書かない=raw/statusもprocessedへ移動しない(前回表示維持のため残す)
        self.assertTrue(raw_path.exists())
        self.assertTrue(status_path.exists())


class ApplyCapTest(ApplyTestBase):
    def test_per_cluster_and_total_cap_enforced(self):
        items = []
        seq = 0
        for cid in ("c-1", "c-2", "c-3"):
            for i in range(5):
                items.append(_item(
                    _sid(self.now - timedelta(days=1, seconds=seq)), author=f"u{cid[-1]}{i}",
                    likes=100 + i, cluster_id=cid, seed_query_id=f"q-{cid[-1]}",
                ))
                seq += 1
        self._seed_pair(items)

        rc = apply_mod.run_apply(self._args(min_publish=1, per_cluster=2, total=4), self.now)
        self.assertEqual(rc, 0)
        event = self._digest_events()[0]
        self.assertEqual(len(event["items"]), 4)
        per_cluster_selected = {}
        for it in event["items"]:
            per_cluster_selected[it["cluster_id"]] = per_cluster_selected.get(it["cluster_id"], 0) + 1
        for count in per_cluster_selected.values():
            self.assertLessEqual(count, 2)
        likes_selected = sorted((it["likes"] for it in event["items"]), reverse=True)
        self.assertEqual(likes_selected, sorted(likes_selected, reverse=True))


class ApplyIdempotencyTest(ApplyTestBase):
    def test_rerun_after_success_is_no_op(self):
        items = [_item(_sid(self.now - timedelta(days=1, seconds=i)), author=f"u{i}", likes=200) for i in range(8)]
        self._seed_pair(items)

        rc1 = apply_mod.run_apply(self._args(min_publish=8, per_cluster=20, total=20), self.now)
        self.assertEqual(rc1, 0)
        self.assertEqual(len(self._digest_events()), 1)

        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc2 = apply_mod.run_apply(self._args(min_publish=8, per_cluster=20, total=20), self.now)
        self.assertEqual(rc2, 0)
        self.assertIn("DIGEST_SKIPPED", out.getvalue())
        self.assertEqual(len(self._digest_events()), 1)  # 新規イベントは増えない


# --- apply: pydantic非依存の証明 --------------------------------------------------

class ApplyPydanticIndependenceTest(unittest.TestCase):
    """bookmarks_keyword_digest_apply.pyはbookmarks_keyword_digest_collect.pyを
    importしない(pydantic非依存)ことをサブプロセスで証明する。sys.modules['pydantic']=Noneに
    することで`import pydantic`系の呼び出しを強制的に失敗させた状態でもapplyのimportが
    成功することを確認する（collect経由でpydanticが混入していればここでImportErrorになる）。
    """

    def test_apply_importable_with_pydantic_blocked(self):
        scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        code = (
            "import sys\n"
            "sys.modules['pydantic'] = None\n"
            f"sys.path.insert(0, {scripts_dir!r})\n"
            "import bookmarks_keyword_digest_apply\n"
            "print('IMPORT_OK')\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}")
        self.assertIn("IMPORT_OK", result.stdout)


# --- common.replay() digest拡張 ---------------------------------------------------

class ReplayDigestExtensionTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.ledger_path = Path(self._tmpdir.name) / "ledger.jsonl"

    def test_accumulates_digest_urls_across_events(self):
        common.append_event(self.ledger_path, {
            "type": "digest", "run_id": "r1", "source": {"generation": 1, "revision": 0, "urls_sha256": "a"},
            "items": [{"url": "https://x.com/a/status/1", "cluster_id": "c-1", "seed_query_id": "q-1",
                       "excerpt": "e1", "author": "a", "likes": 10, "posted_at": "2026-07-01"}],
            "dropped_counts": {}, "errors": [],
        })
        common.append_event(self.ledger_path, {
            "type": "digest", "run_id": "r2", "source": {"generation": 1, "revision": 0, "urls_sha256": "b"},
            "items": [{"url": "https://x.com/b/status/2", "cluster_id": "c-2", "seed_query_id": "q-2",
                       "excerpt": "e2", "author": "b", "likes": 20, "posted_at": "2026-07-02"}],
            "dropped_counts": {}, "errors": [],
        })
        events = common.read_ledger(self.ledger_path)
        state = common.replay(events)
        self.assertEqual(state["digest_urls"], {"status:1", "status:2"})
        self.assertEqual(state["digest_items"]["status:1"]["cluster_id"], "c-1")
        self.assertEqual(state["digest_items"]["status:2"]["cluster_id"], "c-2")
        self.assertIsNotNone(state["digest_items"]["status:1"]["published_at"])
        self.assertEqual(state["latest_digest"]["run_id"], "r2")

    def test_same_url_last_event_wins(self):
        common.append_event(self.ledger_path, {
            "type": "digest", "run_id": "r1", "source": {"generation": 1, "revision": 0, "urls_sha256": "a"},
            "items": [{"url": "https://x.com/a/status/1", "cluster_id": "c-1", "seed_query_id": "q-1",
                       "excerpt": "e1", "author": "a", "likes": 10, "posted_at": "2026-07-01"}],
            "dropped_counts": {}, "errors": [],
        })
        common.append_event(self.ledger_path, {
            "type": "digest", "run_id": "r2", "source": {"generation": 1, "revision": 0, "urls_sha256": "b"},
            "items": [{"url": "https://x.com/a/status/1", "cluster_id": "c-9", "seed_query_id": "q-9",
                       "excerpt": "e2", "author": "a", "likes": 99, "posted_at": "2026-07-02"}],
            "dropped_counts": {}, "errors": [],
        })
        state = common.replay(common.read_ledger(self.ledger_path))
        self.assertEqual(state["digest_items"]["status:1"]["cluster_id"], "c-9")

    def test_no_digest_events_yields_empty_state(self):
        common.append_event(self.ledger_path, {"type": "fetch_run", "status": "SUCCESS"})
        state = common.replay(common.read_ledger(self.ledger_path))
        self.assertEqual(state["digest_urls"], set())
        self.assertEqual(state["digest_items"], {})
        self.assertIsNone(state["latest_digest"])


if __name__ == "__main__":
    unittest.main()
