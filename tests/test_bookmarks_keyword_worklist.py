"""bookmarks_keyword_common.py / bookmarks_keyword_worklist.py のfixtureテスト。

X ブックマーク→キーワード抽出パイプラインの共通庫（正規化・照合・URL正規キー・
台帳I/O）とworklist生成CLIの振る舞いを検証する。設計正本:
~/.claude/docs/x-keywords-plan.md §A/§B/§D/§E（B2バッチ）。

Docker・ネットワーク不要、stdlib unittestのみで完結する（全fixtureはtempdir）。

実行:
    python3 tests/test_bookmarks_keyword_worklist.py -v
    docker compose run --rm xstock python -m unittest tests.test_bookmarks_keyword_worklist -v
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bookmarks_keyword_common as common  # noqa: E402
import bookmarks_keyword_worklist as worklist  # noqa: E402


def _rec(url, text, **extra):
    d = {"url": url, "text": text, "author": "@x", "like_count": 0, "created_at": "2026-06-01T00:00:00.000Z"}
    d.update(extra)
    return d


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class TestNormalizeText(unittest.TestCase):
    def test_nfkc_fullwidth_to_halfwidth(self):
        self.assertEqual(common.normalize_text("ＡＢＣ"), "abc")

    def test_casefold(self):
        self.assertEqual(common.normalize_text("HELLO World"), "hello world")

    def test_url_removed(self):
        self.assertEqual(common.normalize_text("見て https://x.com/a/status/1 ね"), "見て ね")

    def test_mention_removed(self):
        self.assertEqual(common.normalize_text("@testbot said hello"), "said hello")

    def test_zero_width_and_control_removed(self):
        self.assertEqual(common.normalize_text("a​\x01b﻿c"), "abc")

    def test_whitespace_collapsed_including_fullwidth_space(self):
        self.assertEqual(common.normalize_text("Claude　Code  is\tgreat"), "claude code is great")

    def test_empty_input(self):
        self.assertEqual(common.normalize_text(""), "")
        self.assertEqual(common.normalize_text(None), "")


class TestDefaultMatchMode(unittest.TestCase):
    def test_ascii_only_keyword(self):
        self.assertEqual(common.default_match_mode("AI"), "ascii_token")
        self.assertEqual(common.default_match_mode("GPT-4"), "ascii_token")
        self.assertEqual(common.default_match_mode("Claude Code"), "ascii_token")

    def test_non_ascii_keyword(self):
        self.assertEqual(common.default_match_mode("投資"), "normalized_substring")
        self.assertEqual(common.default_match_mode("生成AI"), "normalized_substring")


class TestKeywordMatching(unittest.TestCase):
    """陽性/陰性 55件の照合fixture（要求される50件以上を満たす）。"""

    CASES = [
        # --- ascii_token: 境界判定 ---
        ("AI", "ascii_token", "I love AI research", True, "標準的な独立語としてのAI"),
        ("AI", "ascii_token", "OpenAI is great", False, "AIがOpenAIの一部として誤マッチしない"),
        ("AI", "ascii_token", "AI", True, "文字列全体がキーワードのみ"),
        ("AI", "ascii_token", "the-AI-boom", True, "ハイフンは境界とみなす"),
        ("AI", "ascii_token", "AI2027 predictions", False, "直後が数字は境界にならない"),
        ("AI", "ascii_token", "生成AIとは何か", True, "前後が非ASCII(CJK)でも境界として成立"),
        ("GPT-4", "ascii_token", "new GPT-4 model release", True, "ハイフン付き複合語の一致"),
        ("GPT-4", "ascii_token", "GPT-4o is different", False, "直後にoが続く場合は誤マッチしない"),
        ("Claude", "ascii_token", "Claude Code is great", True, "標準的な一致"),
        ("Claude", "ascii_token", "Claudette is a name", False, "前方一致で終わる語に誤マッチしない"),
        ("Python", "ascii_token", "I code in Python daily", True, "標準的な一致"),
        ("Python", "ascii_token", "Pythonic style", False, "直後にicが続く場合は誤マッチしない"),
        ("OpenAI", "ascii_token", "OpenAI released GPT-5.5", True, "標準的な一致"),
        ("Claude Code", "ascii_token", "Claude Code のベストプラクティス", True, "複合語+CJK境界"),
        ("Claude Code", "ascii_token", "Claude　Code  最高", True, "全角スペース・複数スペースの空白正規化"),
        ("n8n", "ascii_token", "n8n workflow automation", True, "英数字混在キーワードの一致"),
        ("n8n", "ascii_token", "n8ntest", False, "直後に文字が続く場合は誤マッチしない"),
        ("n8n", "ascii_token", "using n8n_workflow now", True, "アンダースコアは境界とみなす仕様（[^a-z0-9]境界）"),
        ("Claude", "ascii_token", "", False, "空文字テキストには一致しない"),
        ("iPhone", "ascii_token", "iPhone 17 pro max review", True, "標準的な一致"),
        ("iPhone", "ascii_token", "iPhone17", False, "直後が数字は境界にならない"),
        ("a", "ascii_token", "a cat sat", True, "ASCII単一文字キーワードは禁止対象外（非ASCII単一文字のみ禁止）"),
        ("test", "ascii_token", "testing123", False, "直後に文字が続く場合は誤マッチしない"),
        ("cat", "ascii_token", "concatenate", False, "語中への埋め込みに誤マッチしない"),
        ("cat", "ascii_token", "the cat sat", True, "標準的な一致"),
        ("OpenAI", "ascii_token", "check out openai's new model", True, "casefold+アポストロフィ境界"),
        ("PYTHON", "ascii_token", "i love python", True, "大文字キーワードもcasefoldで一致"),
        ("Claude", "ascii_token", "Claude(Anthropic)は素晴らしい", True, "括弧は境界とみなす"),
        ("", "ascii_token", "some text here", False, "空キーワードは常にFalse"),

        # --- normalized_substring: 日本語・境界なし部分一致 ---
        ("投資", "normalized_substring", "自己投資を始めた", True, "偽陽性として仕様通り許容(部分一致)"),
        ("投資", "normalized_substring", "今日は良い天気だ", False, "含まれない語には一致しない"),
        ("生成AI", "normalized_substring", "生成ＡＩの活用事例", True, "全角ＡＩのNFKC正規化一致"),
        ("機械学習", "normalized_substring", "機械学習の基礎を学ぶ", True, "標準的な一致"),
        ("機械学習", "normalized_substring", "深層学習について", False, "含まれない語には一致しない"),
        ("副業", "normalized_substring", "副業で稼ぐ方法", True, "標準的な一致"),
        ("スキル", "normalized_substring", "新しいスキルを身につける", True, "カタカナの一致"),
        ("スキル", "normalized_substring", "料理が上手だ", False, "含まれない語には一致しない"),
        ("プロンプト", "normalized_substring", "プロンプトエンジニアリング入門", True, "カタカナの一致"),
        ("エンジニア", "normalized_substring", "私はエンジニアです", True, "標準的な一致"),
        ("エンジニア", "normalized_substring", "私は医者です", False, "含まれない語には一致しない"),
        ("分析", "normalized_substring", "データ分析の手法", True, "標準的な一致"),
        ("分析", "normalized_substring", "料理のレシピ", False, "含まれない語には一致しない"),
        ("株", "normalized_substring", "株価が急騰した", False, "日本語1文字キーワードは常にFalse（禁止）"),
        ("資", "normalized_substring", "投資の話をしよう", False, "日本語1文字キーワードは常にFalse（禁止）"),
        ("金", "normalized_substring", "お金の話", False, "日本語1文字キーワードは常にFalse（禁止）"),
        ("最高", "normalized_substring", "今日は最高😀の一日だった", True, "絵文字除去後も部分一致は成立"),

        # --- URL/mention除去後の照合 ---
        ("AI", "ascii_token", "https://openai.com/blog/AI-news", False, "URL除去で残テキストが空になり一致しない"),
        ("bot", "ascii_token", "@testbot said hello", False, "mention除去でtestbotごと消える"),
        ("hello", "ascii_token", "@testbot said hello", True, "mention除去後も本文側の語は残る"),
        ("AI", "ascii_token", "🤖AI🤖", True, "絵文字除去後も前後がスペースとなり一致する"),

        # --- exact_phrase: Unicode対応の境界一致（英数字+CJKを「語」とみなす） ---
        ("Claude Code", "exact_phrase", "Claude Code入門", False, "直後にCJK文字が続く場合はexact_phraseでは境界不成立"),
        ("Claude Code", "exact_phrase", "Claude Code の話", True, "直後がスペースなら境界成立"),
        ("投資", "exact_phrase", "自己投資を始めた", False, "exact_phraseはnormalized_substringと異なりCJK内埋め込みを拒否する"),
        ("投資", "exact_phrase", "テーマ: 投資 について", True, "前後が非文字(コロン/スペース)なら境界成立"),
    ]

    def test_all_fixtures(self):
        self.assertGreaterEqual(len(self.CASES), 50, "fixtureは50件以上を要求される")
        for keyword, mode, text, expected, desc in self.CASES:
            with self.subTest(keyword=keyword, mode=mode, text=text, desc=desc):
                normalized = common.normalize_text(text)
                actual = common.keyword_matches(keyword, mode, normalized)
                self.assertEqual(actual, expected, f"{desc}: keyword={keyword!r} text={text!r}")

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            common.keyword_matches("AI", "bogus_mode", "ai research")


class TestCanonicalUrlKey(unittest.TestCase):
    def test_x_com_status(self):
        self.assertEqual(
            common.canonical_url_key("https://x.com/user/status/123456"), "status:123456"
        )

    def test_twitter_com_status(self):
        self.assertEqual(
            common.canonical_url_key("https://twitter.com/user/status/123456"), "status:123456"
        )

    def test_query_string_stripped(self):
        self.assertEqual(
            common.canonical_url_key("https://twitter.com/user/status/999?ref=abc&s=20"),
            "status:999",
        )

    def test_uppercase_host_scheme_same_key(self):
        a = common.canonical_url_key("HTTPS://X.COM/user/status/999")
        b = common.canonical_url_key("https://twitter.com/user/status/999")
        self.assertEqual(a, b)
        self.assertEqual(a, "status:999")

    def test_www_prefix_normalized(self):
        self.assertEqual(
            common.canonical_url_key("https://www.x.com/user/status/42"), "status:42"
        )

    def test_no_status_falls_back_to_normalized_url(self):
        self.assertEqual(
            common.canonical_url_key("https://X.COM/SomeUser/"), "https://x.com/SomeUser"
        )

    def test_fragment_stripped_in_fallback(self):
        self.assertEqual(
            common.canonical_url_key("https://example.com/page/#section"),
            "https://example.com/page",
        )

    def test_empty_url(self):
        self.assertEqual(common.canonical_url_key(""), "")


class TestLoadBookmarks(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = os.path.join(self._tmpdir.name, "bookmarks.jsonl")

    def test_stats_and_bad_lines(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(json.dumps(_rec("https://x.com/a/status/1", "hello")) + "\n")
            f.write(json.dumps(_rec("https://x.com/a/status/2", "")) + "\n")
            f.write("{not valid json\n")
            f.write(json.dumps(_rec("https://x.com/a/status/1", "dup url")) + "\n")

        records, stats = common.load_bookmarks(self.path)
        self.assertEqual(len(records), 3)
        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["empty_text"], 1)
        self.assertEqual(stats["bad_lines"], [3])
        self.assertEqual(stats["unique_urls"], 2)

    def test_missing_file(self):
        records, stats = common.load_bookmarks(os.path.join(self._tmpdir.name, "nope.jsonl"))
        self.assertEqual(records, [])
        self.assertEqual(stats, {"total": 0, "empty_text": 0, "bad_lines": [], "unique_urls": 0})


class TestNoteEvalParse(unittest.TestCase):
    def test_four_valid_marks(self):
        note = (
            "| q | eval |\n"
            "| --- | --- |\n"
            "| クエリA | ✅ <!-- qid:q-0001 --> |\n"
            "| クエリB | 🔁 <!-- qid:q-0002 --> |\n"
            "| クエリC | 🔍 <!-- qid:q-0003 --> |\n"
            "| クエリD | ❌ <!-- qid:q-0004 --> |\n"
        )
        evaluations, unknown = common.note_eval_parse(note)
        self.assertEqual(unknown, [])
        self.assertEqual(
            evaluations,
            [
                {"id_type": "qid", "id": "q-0001", "mark": "✅"},
                {"id_type": "qid", "id": "q-0002", "mark": "🔁"},
                {"id_type": "qid", "id": "q-0003", "mark": "🔍"},
                {"id_type": "qid", "id": "q-0004", "mark": "❌"},
            ],
        )

    def test_unevaluated_empty_cell_skipped(self):
        note = "| クエリA | <!-- qid:q-0005 --> |\n"
        evaluations, unknown = common.note_eval_parse(note)
        self.assertEqual(evaluations, [])
        self.assertEqual(unknown, [])

    def test_unknown_value_collected(self):
        note = "| クエリA | ✅ 良かった <!-- qid:q-0006 --> |\n"
        evaluations, unknown = common.note_eval_parse(note)
        self.assertEqual(evaluations, [])
        self.assertEqual(unknown, ["✅ 良かった"])

    def test_comment_line_outside_table_ignored(self):
        note = "<!-- qid:q-0007 --> これはテーブル外のコメント行\n"
        evaluations, unknown = common.note_eval_parse(note)
        self.assertEqual(evaluations, [])
        self.assertEqual(unknown, [])

    def test_pid_marker(self):
        note = "| 提案X | ✅ <!-- pid:p-0001 --> |\n"
        evaluations, unknown = common.note_eval_parse(note)
        self.assertEqual(evaluations, [{"id_type": "pid", "id": "p-0001", "mark": "✅"}])
        self.assertEqual(unknown, [])

    def test_two_markers_same_line_rejected_as_unknown(self):
        """P0-5: 1行に評価マーカーが2個以上出現した場合、どちらも収穫せず行全体をunknown扱い
        にする（マーカー偽造・多重埋め込み対策）。"""
        note = "| A | ✅ <!-- qid:q-0001 --> | ✅ <!-- qid:q-0002 --> |\n"
        evaluations, unknown = common.note_eval_parse(note)
        self.assertEqual(evaluations, [])
        self.assertEqual(unknown, ["| A | ✅ <!-- qid:q-0001 --> | ✅ <!-- qid:q-0002 --> |"])

    def test_known_ids_filters_unknown_marker(self):
        """P0-5: known_ids指定時、既知idの評価は収穫し未知idは無視する（unknownには入れない）。"""
        note = (
            "| クエリA | ✅ <!-- qid:q-known --> |\n"
            "| クエリB | ✅ <!-- qid:q-unknown --> |\n"
        )
        evaluations, unknown = common.note_eval_parse(note, known_ids={("qid", "q-known")})
        self.assertEqual(unknown, [])
        self.assertEqual(evaluations, [{"id_type": "qid", "id": "q-known", "mark": "✅"}])

    def test_known_ids_none_does_not_filter(self):
        """known_ids未指定(None)なら従来通りフィルタなしで全件収穫する(後方互換)。"""
        note = "| クエリA | ✅ <!-- qid:q-anything --> |\n"
        evaluations, unknown = common.note_eval_parse(note)
        self.assertEqual(evaluations, [{"id_type": "qid", "id": "q-anything", "mark": "✅"}])
        self.assertEqual(unknown, [])


class TestLedgerIO(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.ledger_path = os.path.join(self._tmpdir.name, "ledger.jsonl")

    def test_append_and_read_roundtrip(self):
        common.append_event(self.ledger_path, {"type": "fetch_run", "status": "SUCCESS"})
        common.append_event(self.ledger_path, {"type": "fetch_run", "status": "DEGRADED"})
        events = common.read_ledger(self.ledger_path)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["status"], "SUCCESS")
        self.assertEqual(events[1]["status"], "DEGRADED")
        self.assertIn("ts", events[0])
        self.assertIn("host", events[0])

    def test_tail_corruption_detected(self):
        common.append_event(self.ledger_path, {"type": "fetch_run", "status": "SUCCESS"})
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write("{not valid json at all\n")
        with self.assertRaises(common.LedgerTailCorruption):
            common.read_ledger(self.ledger_path)

    def test_mid_line_corruption_detected(self):
        common.append_event(self.ledger_path, {"type": "fetch_run", "status": "SUCCESS"})
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write("{not valid json\n")
        common.append_event(self.ledger_path, {"type": "fetch_run", "status": "DEGRADED"})
        with self.assertRaises(common.LedgerCorruption):
            common.read_ledger(self.ledger_path)
        # LedgerTailCorruptionはLedgerCorruptionのサブクラスではあるが、
        # 途中行破損はLedgerTailCorruptionそのものではないことを確認する。
        try:
            common.read_ledger(self.ledger_path)
        except common.LedgerCorruption as exc:
            self.assertNotIsInstance(exc, common.LedgerTailCorruption)

    def test_missing_ledger_returns_empty_list(self):
        self.assertEqual(common.read_ledger(self.ledger_path), [])


class TestPipelineLock(unittest.TestCase):
    """P1-1: pipeline_lockのflock(LOCK_EX)排他区間。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.ledger_path = os.path.join(self._tmpdir.name, "ledger.jsonl")

    def test_nonblocking_acquire_fails_while_held(self):
        with common.pipeline_lock(self.ledger_path, blocking=False):
            with self.assertRaises(BlockingIOError):
                with common.pipeline_lock(self.ledger_path, blocking=False):
                    pass

    def test_lock_released_after_context_exit(self):
        with common.pipeline_lock(self.ledger_path, blocking=False):
            pass
        # 直前のwithを抜けた後は解放されているため、再取得できるべき。
        with common.pipeline_lock(self.ledger_path, blocking=False):
            pass


class TestReplayAndIdempotentHarvest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.ledger_path = os.path.join(self._tmpdir.name, "ledger.jsonl")
        self.note_path = os.path.join(self._tmpdir.name, "note.md")

    def _write_note(self, mark="✅"):
        with open(self.note_path, "w", encoding="utf-8") as f:
            f.write(f"| クエリA | {mark} <!-- qid:q-1000 --> |\n")

    def test_replay_baseline_urls_from_generation_events(self):
        common.append_event(self.ledger_path, {
            "type": "generation", "generation": 1, "revision": 0,
            "generation_reason": "baseline",
            "snapshot_urls": ["status:1", "status:2"],
            "active_clusters": [], "dormant_clusters": [],
        })
        common.append_event(self.ledger_path, {
            "type": "generation", "generation": 2, "revision": 0,
            "generation_reason": "delta",
            "delta_urls": ["status:3"],
            "active_clusters": [], "dormant_clusters": [],
        })
        state = common.replay(common.read_ledger(self.ledger_path))
        self.assertEqual(state["baseline_urls"], {"status:1", "status:2", "status:3"})
        self.assertEqual(state["last_generation"]["generation"], 2)

    def _seed_generation_with_qid(self, qid="q-1000"):
        """note_eval_parseのknown_idsフィルタ(P0-5)を通過させるため、対象qidを含む
        generationイベントを台帳へ先行して積む。"""
        common.append_event(self.ledger_path, {
            "type": "generation", "generation": 1, "revision": 0,
            "generation_reason": "baseline",
            "snapshot_urls": [],
            "active_clusters": [{
                "cluster_id": "c-1", "name": "テストクラスタ", "why": "",
                "keywords": [],
                "queries": [{"query_id": qid, "q": "クエリA", "q_simple": "クエリA",
                             "intent": "", "eval_history": []}],
                "evidence_urls": [],
            }],
            "dormant_clusters": [],
        })

    def test_evaluation_harvest_is_idempotent(self):
        self._seed_generation_with_qid()
        self._write_note(mark="✅")

        state1 = common.replay(common.read_ledger(self.ledger_path))
        note_hash1, unknown1 = worklist.harvest_evaluations(
            self.note_path, self.ledger_path, state1, "run-1"
        )
        self.assertIsNone(unknown1)
        self.assertTrue(note_hash1)

        events_after_first = common.read_ledger(self.ledger_path)
        eval_batches_first = [e for e in events_after_first if e["type"] == "evaluation_batch"]
        self.assertEqual(len(eval_batches_first), 1)

        # 同一ノート（同一ハッシュ・同一評価）を再度収穫しても増えないこと。
        state2 = common.replay(events_after_first)
        note_hash2, unknown2 = worklist.harvest_evaluations(
            self.note_path, self.ledger_path, state2, "run-2"
        )
        self.assertIsNone(unknown2)
        self.assertEqual(note_hash1, note_hash2)

        events_after_second = common.read_ledger(self.ledger_path)
        eval_batches_second = [e for e in events_after_second if e["type"] == "evaluation_batch"]
        self.assertEqual(len(eval_batches_second), 1, "同じ評価の再収穫でevaluation_batchが増えないこと(冪等)")

    def test_evaluation_harvest_unknown_value_aborts(self):
        with open(self.note_path, "w", encoding="utf-8") as f:
            f.write("| クエリA | ✅ 良かった <!-- qid:q-1001 --> |\n")
        state = common.replay(common.read_ledger(self.ledger_path))
        note_hash, unknown = worklist.harvest_evaluations(
            self.note_path, self.ledger_path, state, "run-1"
        )
        self.assertEqual(unknown, ["✅ 良かった"])
        # 台帳には何も追記されないこと。
        self.assertEqual(common.read_ledger(self.ledger_path), [])


class TestDeltaAndTrigger(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.raw_path = os.path.join(self._tmpdir.name, "bookmarks.jsonl")
        self.ledger_path = os.path.join(self._tmpdir.name, "ledger.jsonl")
        self.note_path = os.path.join(self._tmpdir.name, "note.md")  # 存在しない前提
        self.out_path = os.path.join(self._tmpdir.name, "worklist.json")

    def _args(self, force=False, reason=None, fetch_status="SUCCESS", record_fetch_run=False,
              observed_url_count=0):
        return argparse.Namespace(
            force=force, reason=reason, fetch_status=fetch_status,
            raw=self.raw_path, ledger=self.ledger_path, note=self.note_path, out=self.out_path,
            record_fetch_run=record_fetch_run, observed_url_count=observed_url_count,
        )

    def _seed_accepted_generation(self, baseline_records):
        snapshot_urls = [common.canonical_url_key(r["url"]) for r in baseline_records]
        common.append_event(self.ledger_path, {
            "type": "generation", "generation": 1, "revision": 0,
            "generation_reason": "baseline",
            "snapshot_urls": snapshot_urls,
            "active_clusters": [], "dormant_clusters": [],
        })

    def test_baseline_no_ledger_fires(self):
        _write_jsonl(self.raw_path, [_rec("https://x.com/a/status/1", "hello world")])
        payload, exit_code = worklist.build_worklist(self._args())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["trigger"], {"fired": True, "reason": "baseline"})

    def test_nine_delta_does_not_fire(self):
        baseline = [_rec(f"https://x.com/a/status/{i}", f"base{i}") for i in range(1, 6)]
        _write_jsonl(self.raw_path, baseline)
        self._seed_accepted_generation(baseline)

        new_records = baseline + [
            _rec(f"https://x.com/a/status/{100+i}", f"new{i}") for i in range(9)
        ]
        _write_jsonl(self.raw_path, new_records)

        payload, exit_code = worklist.build_worklist(self._args())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["delta"]["eligible_nonempty_count"], 9)
        self.assertFalse(payload["trigger"]["fired"])

    def test_ten_delta_fires(self):
        baseline = [_rec(f"https://x.com/a/status/{i}", f"base{i}") for i in range(1, 6)]
        _write_jsonl(self.raw_path, baseline)
        self._seed_accepted_generation(baseline)

        new_records = baseline + [
            _rec(f"https://x.com/a/status/{100+i}", f"new{i}") for i in range(10)
        ]
        _write_jsonl(self.raw_path, new_records)

        payload, exit_code = worklist.build_worklist(self._args())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["delta"]["eligible_nonempty_count"], 10)
        self.assertTrue(payload["trigger"]["fired"])
        self.assertEqual(payload["trigger"]["reason"], "delta_threshold")

    def test_degraded_does_not_fire_even_with_large_delta(self):
        baseline = [_rec(f"https://x.com/a/status/{i}", f"base{i}") for i in range(1, 6)]
        _write_jsonl(self.raw_path, baseline)
        self._seed_accepted_generation(baseline)

        new_records = baseline + [
            _rec(f"https://x.com/a/status/{200+i}", f"new{i}") for i in range(20)
        ]
        _write_jsonl(self.raw_path, new_records)

        payload, exit_code = worklist.build_worklist(self._args(fetch_status="DEGRADED"))
        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["trigger"]["fired"])
        self.assertEqual(payload["trigger"]["reason"], "fetch_not_success")

    def test_force_with_reason_fires(self):
        baseline = [_rec(f"https://x.com/a/status/{i}", f"base{i}") for i in range(1, 6)]
        _write_jsonl(self.raw_path, baseline)
        self._seed_accepted_generation(baseline)
        # 変更なし（delta 0件）でもforceは発火する。

        payload, exit_code = worklist.build_worklist(
            self._args(force=True, reason="wiring-test")
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["trigger"]["fired"])

    def test_skipped_manual_force_with_integrity_ok_fires(self):
        baseline = [_rec(f"https://x.com/a/status/{i}", f"base{i}") for i in range(1, 6)]
        _write_jsonl(self.raw_path, baseline)
        self._seed_accepted_generation(baseline)
        # 件数が前回snapshot以上・全行JSON妥当

        payload, exit_code = worklist.build_worklist(
            self._args(force=True, reason="manual-regen", fetch_status="SKIPPED_MANUAL")
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["trigger"]["fired"])

    def test_skipped_manual_force_with_integrity_fail_aborts(self):
        baseline = [_rec(f"https://x.com/a/status/{i}", f"base{i}") for i in range(1, 6)]
        _write_jsonl(self.raw_path, baseline)
        self._seed_accepted_generation(baseline)

        # 件数が前回snapshot(5件)を下回るraw（データ消失を模擬）。
        _write_jsonl(self.raw_path, baseline[:2])

        payload, exit_code = worklist.build_worklist(
            self._args(force=True, reason="manual-regen", fetch_status="SKIPPED_MANUAL")
        )
        self.assertEqual(exit_code, 4)
        self.assertEqual(payload["abort"]["reason"], "raw_integrity_failed")

    def test_record_fetch_run_appends_one_event_per_run(self):
        _write_jsonl(self.raw_path, [_rec("https://x.com/a/status/1", "hello world")])

        payload, exit_code = worklist.build_worklist(self._args(record_fetch_run=True))
        self.assertEqual(exit_code, 0)

        events = common.read_ledger(self.ledger_path)
        fetch_runs = [e for e in events if e["type"] == "fetch_run"]
        self.assertEqual(len(fetch_runs), 1)
        self.assertEqual(fetch_runs[0]["status"], "SUCCESS")
        self.assertEqual(fetch_runs[0]["raw_line_count"], 1)
        self.assertEqual(fetch_runs[0]["url_count"], 1)
        self.assertEqual(fetch_runs[0]["empty_text_count"], 0)
        self.assertEqual(fetch_runs[0]["bad_lines"], [])
        self.assertIn("run_id", fetch_runs[0])

        state = common.replay(events)
        self.assertIsNotNone(state["last_fetch_success_at"], "replayがfetch_run(SUCCESS)からlast_fetch_success_atを拾えること")

        # 冪等ではない: もう一度record_fetch_run=Trueで実行すればイベントが1個増える。
        worklist.build_worklist(self._args(record_fetch_run=True))
        events2 = common.read_ledger(self.ledger_path)
        self.assertEqual(len([e for e in events2 if e["type"] == "fetch_run"]), 2)

    def test_record_fetch_run_false_by_default_no_event(self):
        _write_jsonl(self.raw_path, [_rec("https://x.com/a/status/1", "hello world")])
        worklist.build_worklist(self._args())
        events = common.read_ledger(self.ledger_path)
        self.assertEqual([e for e in events if e["type"] == "fetch_run"], [])

    def test_record_fetch_run_stores_observed_url_count(self):
        # wrapper側のstatus-json由来のobserved_url_countがfetch_runイベントに
        # そのまま記録されること（次回以降の部分取得検知の基準値として使われる）。
        _write_jsonl(self.raw_path, [_rec("https://x.com/a/status/1", "hello world")])

        payload, exit_code = worklist.build_worklist(
            self._args(record_fetch_run=True, observed_url_count=42)
        )
        self.assertEqual(exit_code, 0)

        events = common.read_ledger(self.ledger_path)
        fetch_runs = [e for e in events if e["type"] == "fetch_run"]
        self.assertEqual(len(fetch_runs), 1)
        self.assertEqual(fetch_runs[0]["observed_url_count"], 42)

        # 未指定（デフォルト0）の場合は0が記録されること。
        worklist.build_worklist(self._args(record_fetch_run=True))
        events2 = common.read_ledger(self.ledger_path)
        fetch_runs2 = [e for e in events2 if e["type"] == "fetch_run"]
        self.assertEqual(fetch_runs2[-1]["observed_url_count"], 0)

    def test_force_requires_reason_at_cli_level(self):
        with self.assertRaises(SystemExit):
            sys_argv_backup = sys.argv
            sys.argv = ["bookmarks_keyword_worklist.py", "--force"]
            try:
                worklist.parse_args()
            finally:
                sys.argv = sys_argv_backup


class TestRealDataSmoke(unittest.TestCase):
    """実データ output/bookmarks.jsonl でのworklist生成smoke test（読むだけ）。"""

    def test_real_bookmarks_jsonl(self):
        repo_root = Path(__file__).resolve().parent.parent
        real_raw = repo_root / "output" / "bookmarks.jsonl"
        if not real_raw.exists():
            self.skipTest("output/bookmarks.jsonl が存在しません")

        mtime_before = real_raw.stat().st_mtime

        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(
                force=False, reason=None, fetch_status="SUCCESS",
                raw=str(real_raw),
                ledger=os.path.join(tmpdir, "ledger.jsonl"),
                note=os.path.join(tmpdir, "nonexistent_note.md"),
                out=os.path.join(tmpdir, "worklist.json"),
            )
            payload, exit_code = worklist.build_worklist(args)

        self.assertEqual(exit_code, 0)
        # 実データは週次fetchで増えるため、固定値でなく不変条件で検証する
        # (2026-07-12 データドリフトで固定値245/243/48が破綻した教訓)
        raw_lines = sum(1 for _ in open(real_raw))
        self.assertEqual(payload["current_snapshot"]["raw_line_count"], raw_lines)
        # canonical url_count は重複ブックマーク統合により raw 行数以下
        self.assertLessEqual(payload["current_snapshot"]["url_count"], raw_lines)
        self.assertGreater(payload["current_snapshot"]["url_count"], 0)
        self.assertGreaterEqual(payload["excluded"]["empty_text_count"], 0)
        self.assertLess(payload["excluded"]["empty_text_count"], raw_lines)
        self.assertEqual(payload["excluded"]["bad_lines"], [])
        self.assertLessEqual(len(payload["stats_candidates"]), 60)
        self.assertTrue(payload["trigger"]["fired"])
        self.assertEqual(payload["trigger"]["reason"], "baseline")

        top10_terms = [c["term"] for c in payload["stats_candidates"][:10]]
        for stoplisted in worklist._URL_RESIDUE_TERMS:
            self.assertNotIn(
                stoplisted, top10_terms,
                f"URL残渣トークン{stoplisted!r}がstats_candidates上位10語に混入している",
            )

        # 実データを読むだけであり、書き換えていないこと。
        self.assertEqual(real_raw.stat().st_mtime, mtime_before)

        print("\n[実データsmoke] stats_candidates 上位10語:")
        for c in payload["stats_candidates"][:10]:
            print(f"  {c['term']!r}: count={c['count']} weighted={c['weighted']} windows={c['windows']}")


if __name__ == "__main__":
    unittest.main()
