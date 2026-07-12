"""bookmarks_keyword_ingest.py（柵・台帳・render）のfixtureテスト。

X ブックマーク→キーワード抽出パイプラインのingest CLI（検証→generationイベント
append→latest.json再導出→ノートrender）を検証する。設計正本:
~/.claude/docs/x-keywords-plan.md §B/§C/§D/§F/§G（B3バッチ）。

Docker・ネットワーク不要、stdlib unittestのみで完結する（全fixtureはtempdir。
実データ書込は一切しない）。

実行:
    python3 tests/test_bookmarks_keyword_ingest.py -v
    docker compose run --rm xstock python -m unittest tests.test_bookmarks_keyword_ingest -v
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bookmarks_keyword_common as common  # noqa: E402
import bookmarks_keyword_ingest as ingest  # noqa: E402
import bookmarks_keyword_worklist as worklist  # noqa: E402


def _rec(url, text, **extra):
    d = {"url": url, "text": text, "author": "@x", "like_count": 3, "created_at": "2026-06-01T00:00:00.000Z"}
    d.update(extra)
    return d


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _q(q, q_simple=None, intent="意図"):
    return {"q": q, "q_simple": q_simple or q, "intent": intent}


def _proposal(name="クラスタ", why="理由の説明", keywords_ja=None, keywords_en=None,
              queries=None, evidence_urls=None):
    return {
        "op": "propose_new",
        "name_ja": name,
        "why": why,
        "keywords_ja": keywords_ja if keywords_ja is not None else ["生成AI", "活用事例"],
        "keywords_en": keywords_en if keywords_en is not None else [],
        "queries": queries or [_q("生成AI 活用事例"), _q("生成AI OR AI活用")],
        "evidence_urls": evidence_urls or [],
    }


def _wrap_fence(payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"# 生成結果\n\n```json x-keywords-generation\n{body}\n```\n"


DEFAULT_RAW_RECORDS = [
    _rec("https://x.com/u/status/1001", "生成AIの活用事例をまとめました"),
    _rec("https://x.com/u/status/1002", "生成AIで業務改善した話"),
    _rec("https://x.com/u/status/1003", "生成AIの最新動向まとめ"),
    _rec("https://x.com/u/status/2001", "Claude Codeで開発効率が上がった"),
    _rec("https://x.com/u/status/2002", "Claude Codeのベストプラクティス"),
    _rec("https://x.com/u/status/2003", "Claude Codeを使い倒す方法"),
    _rec("https://x.com/u/status/3001", "投資の勉強を始めた記録"),
    _rec("https://x.com/u/status/3002", "投資信託の選び方"),
    _rec("https://x.com/u/status/3003", "投資で失敗した話"),
    _rec("https://x.com/u/status/4001", ""),  # text空（evidence対象外の検証用）
    _rec("https://x.com/u/status/9001", "生成AI と Claude Code と 投資 について書いた総合記事"),  # 3クラスタ共通一致（reuse検証用）
]


class IngestTestBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.raw_path = os.path.join(self._tmpdir.name, "bookmarks.jsonl")
        self.ledger_path = os.path.join(self._tmpdir.name, "ledger.jsonl")
        self.latest_path = os.path.join(self._tmpdir.name, "latest.json")
        self.note_path = os.path.join(self._tmpdir.name, "note.md")
        self.worklist_path = os.path.join(self._tmpdir.name, "worklist.json")
        self.result_path = os.path.join(self._tmpdir.name, "result.md")
        _write_jsonl(self.raw_path, DEFAULT_RAW_RECORDS)

    # --- 補助 ---

    def _args(self, baseline=False, render_only=False):
        return argparse.Namespace(
            result_md=self.result_path, worklist=self.worklist_path, raw=self.raw_path,
            ledger=self.ledger_path, latest=self.latest_path, note=self.note_path,
            baseline=baseline, render_only=render_only,
        )

    def _write_result(self, fence_payload, extra_text=""):
        content = extra_text + _wrap_fence(fence_payload)
        with open(self.result_path, "w", encoding="utf-8") as f:
            f.write(content)

    def _write_worklist(self, **overrides):
        """P0-3鮮度検証を通過するworklist fixtureを書く。previous_generationは呼出時点の
        台帳末尾世代から自動算出する（overridesで明示的に上書きしない限り常に整合する）。"""
        records, _ = common.load_bookmarks(self.raw_path)
        url_keys = {common.canonical_url_key(r["url"]) for r in records if r.get("url")}
        events = common.read_ledger(self.ledger_path)
        replay_state = common.replay(events)
        last_gen = replay_state["last_generation"]
        payload = {
            "run_id": "run-test",
            "trigger": {"fired": True, "reason": "delta_threshold"},
            "fetch_status": "SUCCESS",
            "current_snapshot": {
                "url_count": len(url_keys), "raw_line_count": len(records),
                "urls_sha256": common.sha256_of_urls(url_keys),
            },
            "delta": {"count": 0, "eligible_nonempty_count": 0, "urls": []},
            "continuity_stats": [],
            "previous_generation": {
                "generation": last_gen["generation"] if last_gen else None,
                "revision": last_gen.get("revision", 0) if last_gen else None,
            },
            "note_hash": common.sha256_of_file(self.note_path) if Path(self.note_path).exists() else None,
        }
        payload.update(overrides)
        with open(self.worklist_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return payload

    def _run(self, baseline=False, render_only=False):
        return ingest.run_ingest(self._args(baseline=baseline, render_only=render_only), datetime.now(timezone.utc))

    def _events(self):
        return common.read_ledger(self.ledger_path)

    def _latest(self):
        return json.loads(Path(self.latest_path).read_text(encoding="utf-8"))

    def _seed_baseline(self):
        """genAI/ClaudeCode/投資 の3クラスタでbaseline受理する。戻り値: latest.json dict。"""
        fence = {
            "clusters": [],
            "proposals": [
                _proposal(
                    "生成AI活用", keywords_ja=["生成AI", "活用事例"],
                    queries=[_q("生成AI 活用事例"), _q("生成AI OR AI活用")],
                    evidence_urls=["https://x.com/u/status/1001", "https://x.com/u/status/1002",
                                   "https://x.com/u/status/1003"],
                ),
                _proposal(
                    "Claude Code", keywords_ja=[], keywords_en=["Claude Code", "coding tips"],
                    queries=[_q("Claude Code ベストプラクティス"), _q("Claude Code tips")],
                    evidence_urls=["https://x.com/u/status/2001", "https://x.com/u/status/2002",
                                   "https://x.com/u/status/2003"],
                ),
                _proposal(
                    "投資", keywords_ja=["投資", "資産運用"],
                    queries=[_q("投資 勉強法"), _q("投資 失敗談")],
                    evidence_urls=["https://x.com/u/status/3001", "https://x.com/u/status/3002",
                                   "https://x.com/u/status/3003"],
                ),
            ],
            "notes": "baseline seed",
        }
        self._write_result(fence)
        code = self._run(baseline=True)
        self.assertEqual(code, 0, "baseline seedが失敗した")
        return self._latest()

    def _keep_entry(self, cluster, op="keep"):
        keywords = cluster["keywords"]
        return {
            "op": op,
            "id": cluster["cluster_id"],
            "name_ja": cluster["name"],
            "why": cluster["why"],
            "keywords_ja": [k["keyword"] for k in keywords if not k["keyword"].isascii()],
            "keywords_en": [k["keyword"] for k in keywords if k["keyword"].isascii()],
            "queries": [_q(q["q"], q["q_simple"], q["intent"]) for q in cluster["queries"]],
            "evidence_urls": cluster["evidence_urls"],
        }


class TestBaselineAcceptance(IngestTestBase):
    def test_baseline_accepted_generates_ledger_latest_note(self):
        latest = self._seed_baseline()
        self.assertEqual(latest["generation"], 1)
        self.assertEqual(latest["revision"], 0)
        self.assertEqual(latest["generation_reason"], "baseline")
        self.assertEqual(len(latest["active_clusters"]), 3)

        events = self._events()
        self.assertEqual([e["type"] for e in events], ["generation"])
        gen_ev = events[0]
        self.assertEqual(gen_ev["generation"], 1)
        self.assertIn("snapshot_urls", gen_ev)

        # cluster_id / query_id が採番されている
        for cluster in latest["active_clusters"]:
            self.assertTrue(cluster["cluster_id"].startswith("c-"))
            for q in cluster["queries"]:
                self.assertTrue(q["query_id"].startswith("q-"))

        self.assertTrue(Path(self.note_path).exists())
        note_text = Path(self.note_path).read_text(encoding="utf-8")
        self.assertIn("project: influx", note_text)
        self.assertIn("type: auto-generated-mirror", note_text)
        self.assertIn("生成AI活用", note_text)


class TestFenceExtraction(IngestTestBase):
    def test_zero_fences_rejected(self):
        with open(self.result_path, "w", encoding="utf-8") as f:
            f.write("フェンスなしの本文だけ\n")
        code = self._run(baseline=True)
        self.assertEqual(code, 6)
        events = self._events()
        self.assertEqual(events[-1]["type"], "rejection")
        self.assertEqual(events[-1]["code"], "fence_count")

    def test_two_fences_rejected(self):
        fence = {"clusters": [], "proposals": [_proposal(evidence_urls=[
            "https://x.com/u/status/1001", "https://x.com/u/status/1002", "https://x.com/u/status/1003"])]}
        body = _wrap_fence(fence)
        with open(self.result_path, "w", encoding="utf-8") as f:
            f.write(body + "\n" + body)
        code = self._run(baseline=True)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "fence_count")


class TestSecretScan(IngestTestBase):
    def test_dummy_aws_key_rejected(self):
        fence = {"clusters": [], "proposals": [_proposal(evidence_urls=[
            "https://x.com/u/status/1001", "https://x.com/u/status/1002", "https://x.com/u/status/1003"])]}
        self._write_result(fence, extra_text="漏洩例: AKIAABCDEFGHIJKLMNOP\n")
        code = self._run(baseline=True)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "secret_detected")

    def test_newline_split_aws_key_rejected(self):
        """P1-3: 改行でキーを分割して秘密スキャンを回避しようとするケースを検知できること。"""
        fence = {"clusters": [], "proposals": [_proposal(evidence_urls=[
            "https://x.com/u/status/1001", "https://x.com/u/status/1002", "https://x.com/u/status/1003"])]}
        self._write_result(fence, extra_text="漏洩例: AKIAABCDEFGH\nIJKLMNOP\n")
        code = self._run(baseline=True)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "secret_detected")


class TestWorklistFreshness(IngestTestBase):
    """P0-3: ingestがworklistの真正性・鮮度を検証すること。"""

    def setUp(self):
        super().setUp()
        self.latest1 = self._seed_baseline()

    def _valid_delta_fence(self):
        clusters_in = [self._keep_entry(c) for c in self.latest1["active_clusters"]]
        return {"clusters": clusters_in, "proposals": []}

    def test_trigger_not_fired_rejected(self):
        self._write_result(self._valid_delta_fence())
        self._write_worklist(trigger={"fired": False, "reason": "no_change"})
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "trigger_not_fired")

    def test_fetch_status_degraded_rejected(self):
        self._write_result(self._valid_delta_fence())
        self._write_worklist(fetch_status="DEGRADED")
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "fetch_status_invalid")

    def test_fetch_status_skipped_manual_without_force_rejected(self):
        self._write_result(self._valid_delta_fence())
        self._write_worklist(fetch_status="SKIPPED_MANUAL", trigger={"fired": True, "reason": "delta_threshold"})
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "fetch_status_invalid")

    def test_fetch_status_skipped_manual_with_force_accepted(self):
        self._write_result(self._valid_delta_fence())
        self._write_worklist(fetch_status="SKIPPED_MANUAL", trigger={"fired": True, "reason": "force: manual test"})
        code = self._run(baseline=False)
        self.assertEqual(code, 0)

    def test_raw_changed_since_worklist_rejected(self):
        self._write_result(self._valid_delta_fence())
        self._write_worklist(current_snapshot={"url_count": 1, "raw_line_count": 1, "urls_sha256": "deadbeef"})
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "raw_changed_since_worklist")

    def test_ledger_advanced_since_worklist_rejected(self):
        # worklistが読んだ時点の世代参照を古いまま(gen0)にしておく -> 現在の台帳(gen1)と不一致。
        self._write_result(self._valid_delta_fence())
        self._write_worklist(previous_generation={"generation": 0, "revision": 0})
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "ledger_advanced_since_worklist")

    def test_baseline_skips_freshness_checks(self):
        """--baseline時はworklistが無い/fired=false等でも鮮度検証をスキップすること
        （そもそも初回はworklistが鮮度検証の対象外という設計のため、新規の空台帳で確認）。"""
        code = self._run(baseline=True)  # setUpのbaselineとは別の空台帳を使うため再構築
        # 既にbaseline済みなので2回目のbaselineはbaseline_on_existing_ledgerでrejectされるが、
        # それはfreshness検証とは別のvalidationであることを確認する。
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "baseline_on_existing_ledger")


class TestDeltaGenerationValidation(IngestTestBase):
    """baselineをseedした上で、2世代目のクラスタ検証各種をテストする。"""

    def setUp(self):
        super().setUp()
        self.latest1 = self._seed_baseline()
        self.clusters = {c["name"]: c for c in self.latest1["active_clusters"]}

    def _valid_keep_all(self):
        return [self._keep_entry(c) for c in self.latest1["active_clusters"]]

    def _delta_fence(self, clusters_in, proposals_in=None):
        return {"clusters": clusters_in, "proposals": proposals_in or []}

    def test_unknown_cluster_id_rejected(self):
        clusters_in = self._valid_keep_all()
        clusters_in.append({**clusters_in[0], "id": "c-doesnotexist", "op": "keep"})
        self._write_result(self._delta_fence(clusters_in))
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "unknown_active_id")

    def test_missing_active_id_rejected(self):
        clusters_in = self._valid_keep_all()[:-1]  # 1つ欠落させる
        self._write_result(self._delta_fence(clusters_in))
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "missing_active_id")

    def test_duplicate_active_id_rejected(self):
        clusters_in = self._valid_keep_all()
        clusters_in.append(dict(clusters_in[0]))  # 重複
        self._write_result(self._delta_fence(clusters_in))
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "duplicate_cluster_id")

    def test_fake_evidence_url_rejected(self):
        clusters_in = self._valid_keep_all()
        clusters_in[0]["evidence_urls"] = ["https://x.com/u/status/9999999"]
        self._write_result(self._delta_fence(clusters_in))
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "evidence_url_not_found")

    def test_empty_text_evidence_url_rejected(self):
        clusters_in = self._valid_keep_all()
        clusters_in[0]["evidence_urls"] = ["https://x.com/u/status/4001"]
        self._write_result(self._delta_fence(clusters_in))
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "evidence_url_empty_text")

    def test_keyword_mismatch_evidence_rejected(self):
        clusters_in = self._valid_keep_all()
        # 生成AIクラスタのevidenceを「投資」クラスタの記事に差し替える -> keyword不一致
        gen_ai = next(c for c in clusters_in if c["id"] == self.clusters["生成AI活用"]["cluster_id"])
        gen_ai["evidence_urls"] = ["https://x.com/u/status/3001"]
        self._write_result(self._delta_fence(clusters_in))
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "evidence_keyword_mismatch")

    def test_query_too_long_rejected(self):
        clusters_in = self._valid_keep_all()
        clusters_in[0]["queries"][0]["q"] = "a" * 121
        self._write_result(self._delta_fence(clusters_in))
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "query_too_long")

    def test_unknown_operator_rejected(self):
        clusters_in = self._valid_keep_all()
        clusters_in[0]["queries"][0]["q"] = "生成AI foo:bar"
        self._write_result(self._delta_fence(clusters_in))
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "query_unknown_operator")

    def test_or_exceeded_rejected(self):
        clusters_in = self._valid_keep_all()
        clusters_in[0]["queries"][0]["q"] = "A OR B OR C OR D OR E"
        self._write_result(self._delta_fence(clusters_in))
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "query_or_exceeded")

    def test_url_reuse_exceeded_rejected(self):
        clusters_in = self._valid_keep_all()
        # 3クラスタ全てのkeywordに一致する共通記事を全クラスタのevidenceにする -> 使い回し3件で上限(2)超過
        shared_url = "https://x.com/u/status/9001"
        for c in clusters_in:
            c["evidence_urls"] = [shared_url]
        self._write_result(self._delta_fence(clusters_in))
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "evidence_url_reuse_exceeded")

    def test_proposals_truncated_to_three(self):
        clusters_in = self._valid_keep_all()
        # 先頭3件は既存クラスタと同じevidenceセット(使い回し2回までなので許容範囲)、
        # 4,5件目は検証されず切り詰められるだけなので内容は問わない。
        proposals_in = [
            _proposal("提案0", keywords_ja=["生成AI", "活用事例"],
                      queries=[_q("生成AI 提案テスト"), _q("生成AI 提案テスト2")],
                      evidence_urls=["https://x.com/u/status/1001", "https://x.com/u/status/1002",
                                     "https://x.com/u/status/1003"]),
            _proposal("提案1", keywords_ja=[], keywords_en=["Claude Code", "coding tips"],
                      queries=[_q("Claude Code 提案テスト"), _q("Claude Code 提案テスト2")],
                      evidence_urls=["https://x.com/u/status/2001", "https://x.com/u/status/2002",
                                     "https://x.com/u/status/2003"]),
            _proposal("提案2", keywords_ja=["投資", "資産形成"],
                      queries=[_q("投資 提案テスト"), _q("投資 提案テスト2")],
                      evidence_urls=["https://x.com/u/status/3001", "https://x.com/u/status/3002",
                                     "https://x.com/u/status/3003"]),
            _proposal("提案3(切り捨て予定)", keywords_ja=["投資", "資産形成"],
                      queries=[_q("投資 切り捨て")], evidence_urls=["https://x.com/u/status/9999"]),
            _proposal("提案4(切り捨て予定)", keywords_ja=["投資", "資産形成"],
                      queries=[_q("投資 切り捨て2")], evidence_urls=["https://x.com/u/status/9999"]),
        ]
        self._write_result(self._delta_fence(clusters_in, proposals_in))
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 0, "5件提案は超過分ドロップで受理されるべき")
        events = self._events()
        gen_ev = [e for e in events if e["type"] == "generation"][-1]
        self.assertEqual(len(gen_ev["proposals"]), 3, "提案は先頭3件に切り詰められるべき")
        self.assertEqual(
            {p["name_ja"] for p in gen_ev["proposals"]}, {"提案0", "提案1", "提案2"},
            "切り詰め後に残るのは先頭3件であるべき",
        )


class TestCheckmarkConstraints(IngestTestBase):
    def setUp(self):
        super().setUp()
        self.latest1 = self._seed_baseline()
        self.gen_ai_cluster = next(c for c in self.latest1["active_clusters"] if c["name"] == "生成AI活用")
        self.qid = self.gen_ai_cluster["queries"][0]["query_id"]
        self.old_q_text = self.gen_ai_cluster["queries"][0]["q"]

    def _mark_eval(self, mark):
        common.append_event(self.ledger_path, {
            "type": "evaluation_batch", "note_revision": "rev-test",
            "evaluations": [{"id_type": "qid", "id": self.qid, "mark": mark}],
        })

    def test_checkmark_verbatim_violation_rejected(self):
        self._mark_eval("✅")
        clusters_in = [self._keep_entry(c) for c in self.latest1["active_clusters"]]
        gen_ai_entry = next(c for c in clusters_in if c["id"] == self.gen_ai_cluster["cluster_id"])
        gen_ai_entry["op"] = "revise"
        gen_ai_entry["queries"][0]["q"] = "生成AI 変更後のクエリ"  # ✅なのに変更 -> 違反
        self._write_result({"clusters": clusters_in, "proposals": []})
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "checkmark_violated")

    def test_reject_mark_reappearance_violation_rejected(self):
        self._mark_eval("❌")
        clusters_in = [self._keep_entry(c) for c in self.latest1["active_clusters"]]
        # 何も変更せず(op=keep) -> ❌の付いた旧qがそのまま残ってしまう -> 違反
        self._write_result({"clusters": clusters_in, "proposals": []})
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 6)
        self.assertEqual(self._events()[-1]["code"], "reject_mark_violated")

    def test_checkmark_preserved_verbatim_accepted(self):
        self._mark_eval("✅")
        clusters_in = [self._keep_entry(c) for c in self.latest1["active_clusters"]]
        # op=keepでqテキストは変えない -> ✅は保持されている -> 受理される
        self._write_result({"clusters": clusters_in, "proposals": []})
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 0)


class TestQueryIdContinuity(IngestTestBase):
    def test_verbatim_q_keeps_same_qid(self):
        latest1 = self._seed_baseline()
        gen_ai = next(c for c in latest1["active_clusters"] if c["name"] == "生成AI活用")
        old_qid_0 = gen_ai["queries"][0]["query_id"]
        old_q_0 = gen_ai["queries"][0]["q"]

        clusters_in = [self._keep_entry(c) for c in latest1["active_clusters"]]
        gen_ai_entry = next(c for c in clusters_in if c["id"] == gen_ai["cluster_id"])
        gen_ai_entry["op"] = "revise"
        gen_ai_entry["queries"] = [
            _q(old_q_0),  # 逐語一致 -> qid継承
            _q("生成AI 新規クエリ全く別物"),  # 新規 -> 新しいqid
        ]
        self._write_result({"clusters": clusters_in, "proposals": []})
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 0)

        latest2 = self._latest()
        gen_ai2 = next(c for c in latest2["active_clusters"] if c["cluster_id"] == gen_ai["cluster_id"])
        by_text = {q["q"]: q["query_id"] for q in gen_ai2["queries"]}
        self.assertEqual(by_text[old_q_0], old_qid_0, "逐語一致のqueryはqidを継承すべき")
        self.assertNotEqual(by_text["生成AI 新規クエリ全く別物"], old_qid_0)


class TestProposalDecision(IngestTestBase):
    def test_pid_approve_promotes_to_active(self):
        latest1 = self._seed_baseline()
        clusters_in = [self._keep_entry(c) for c in latest1["active_clusters"]]
        new_proposal = _proposal(
            "副業", keywords_ja=["投資", "資産形成"], queries=[_q("投資 副業テスト"), _q("投資 副業テスト2")],
            evidence_urls=["https://x.com/u/status/3001", "https://x.com/u/status/3002",
                           "https://x.com/u/status/3003"],
        )
        self._write_result({"clusters": clusters_in, "proposals": [new_proposal]})
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 0)

        latest2 = self._latest()
        self.assertEqual(len(latest2["proposals"]), 1)
        pid = latest2["proposals"][0]["proposal_id"]

        # pidに✅評価を追加してから3世代目を実行 -> active昇格されるはず
        common.append_event(self.ledger_path, {
            "type": "evaluation_batch", "note_revision": "rev-test-2",
            "evaluations": [{"id_type": "pid", "id": pid, "mark": "✅"}],
        })
        clusters_in_3 = [self._keep_entry(c) for c in latest2["active_clusters"]]
        self._write_result({"clusters": clusters_in_3, "proposals": []})
        self._write_worklist()
        code3 = self._run(baseline=False)
        self.assertEqual(code3, 0)

        events = self._events()
        decision_events = [e for e in events if e["type"] == "proposal_decision"]
        self.assertEqual(len(decision_events), 1)
        self.assertEqual(decision_events[0]["proposal_id"], pid)
        self.assertEqual(decision_events[0]["decision"], "approve")

        latest3 = self._latest()
        self.assertEqual(len(latest3["active_clusters"]), 4, "pid承認クラスタが昇格しactiveが4件になるべき")
        self.assertEqual(len(latest3["proposals"]), 0)


class TestToctou(IngestTestBase):
    def test_note_mismatch_aborts_before_ledger_append(self):
        """P0-4: TOCTOU検出は台帳append前に行われ、不一致なら台帳には何も記録されない。"""
        latest1 = self._seed_baseline()
        note_hash_at_worklist_time = common.sha256_of_file(self.note_path)

        # workklistが読んだ時点のnote_hashを保存した「後」に、人間がノートを編集した想定
        with open(self.note_path, "a", encoding="utf-8") as f:
            f.write("\n人間による評価編集\n")
        modified_content = Path(self.note_path).read_text(encoding="utf-8")

        clusters_in = [self._keep_entry(c) for c in latest1["active_clusters"]]
        self._write_result({"clusters": clusters_in, "proposals": []})
        self._write_worklist(note_hash=note_hash_at_worklist_time)
        code = self._run(baseline=False)
        self.assertEqual(code, 3)

        # ノートは無傷（上書きされていない）
        self.assertEqual(Path(self.note_path).read_text(encoding="utf-8"), modified_content)

        # 台帳append前に中止するため、baseline世代のみが残り新規generationは増えない。
        events = self._events()
        self.assertEqual([e["type"] for e in events], ["generation"], "TOCTOU不一致時は台帳append自体が起きないべき")

        # latest.jsonもbaseline時点のまま変化していない。
        self.assertEqual(self._latest(), latest1)


class TestLedgerCorruption(IngestTestBase):
    def test_tail_corruption_returns_exit5(self):
        common.append_event(self.ledger_path, {"type": "fetch_run", "status": "SUCCESS"})
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write("{not valid json at all\n")
        self._write_result({"clusters": [], "proposals": []})
        code = self._run(baseline=True)
        self.assertEqual(code, 5)


class TestRenderOnly(IngestTestBase):
    def test_render_only_recovers_latest_and_note(self):
        latest1 = self._seed_baseline()
        os.remove(self.latest_path)
        code = self._run(render_only=True)
        self.assertEqual(code, 0)
        recovered = self._latest()
        self.assertEqual(recovered["generation"], latest1["generation"])
        self.assertEqual(
            {c["cluster_id"] for c in recovered["active_clusters"]},
            {c["cluster_id"] for c in latest1["active_clusters"]},
        )
        self.assertTrue(Path(self.note_path).exists())

    def test_render_only_no_ledger_is_noop(self):
        code = self._run(render_only=True)
        self.assertEqual(code, 0)
        self.assertFalse(Path(self.latest_path).exists())


class TestManualRegenSameSnapshot(IngestTestBase):
    def test_force_same_snapshot_bumps_revision_not_generation(self):
        latest1 = self._seed_baseline()
        clusters_in = [self._keep_entry(c) for c in latest1["active_clusters"]]
        self._write_result({"clusters": clusters_in, "proposals": []})

        records, _ = common.load_bookmarks(self.raw_path)
        url_keys = {common.canonical_url_key(r["url"]) for r in records if r.get("url")}
        same_sha = common.sha256_of_urls(url_keys)
        self._write_worklist(trigger={"fired": True, "reason": "force: manual test"},
                              current_snapshot={"url_count": len(url_keys), "raw_line_count": len(records),
                                                 "urls_sha256": same_sha})
        code = self._run(baseline=False)
        self.assertEqual(code, 0)

        latest2 = self._latest()
        self.assertEqual(latest2["generation"], 1, "同一snapshotのforce regenはgenerationを進めない")
        self.assertEqual(latest2["revision"], 1, "revisionが+1されるべき")
        self.assertEqual(latest2["generation_reason"], "manual_regen")


class TestContinuousScenario(IngestTestBase):
    """受理 -> worklist再実行(実装呼出) -> 2世代目受理、の一連を通しで検証する。"""

    def test_two_generation_cycle_via_real_worklist(self):
        latest1 = self._seed_baseline()

        # 新規ブックマークを2件追加してdelta発生条件を作る(force指定で閾値10を待たず即トリガー)
        _write_jsonl(self.raw_path, DEFAULT_RAW_RECORDS + [
            _rec("https://x.com/u/status/5001", "新しい生成AIツールを試した"),
            _rec("https://x.com/u/status/5002", "投資の新しい話題"),
        ])

        wl_out_path = os.path.join(self._tmpdir.name, "worklist_gen2.json")
        wl_args = argparse.Namespace(
            force=True, reason="continuous-scenario-test", fetch_status="SUCCESS",
            raw=self.raw_path, ledger=self.ledger_path, note=self.note_path, out=wl_out_path,
        )
        wl_payload, wl_exit = worklist.build_worklist(wl_args)
        self.assertEqual(wl_exit, 0)
        self.assertTrue(wl_payload["trigger"]["fired"])
        with open(self.worklist_path, "w", encoding="utf-8") as f:
            json.dump(wl_payload, f, ensure_ascii=False)

        clusters_in = [self._keep_entry(c) for c in latest1["active_clusters"]]
        self._write_result({"clusters": clusters_in, "proposals": []})
        code = self._run(baseline=False)
        self.assertEqual(code, 0, "2世代目のingestが受理されるべき")

        events = self._events()
        self.assertEqual([e["type"] for e in events], ["generation", "generation"],
                         "台帳イベントはbaseline+delta(2世代目)のgenerationのみのはず")
        gen2 = events[-1]
        self.assertEqual(gen2["generation"], 2)
        self.assertEqual(gen2["generation_reason"], "delta")

        latest2 = self._latest()
        self.assertEqual(latest2["baseline_url_count"], len(DEFAULT_RAW_RECORDS) + 2,
                         "fixture全件+新規2件がbaselineに積算されるべき")

        self._event_type_sequence = [e["type"] for e in events]


class TestEvalHistoryRecorded(IngestTestBase):
    """追加1(Critical): consumeした評価をeval_historyへ記録し、次回render時の
    「前回評価」列に反映されること。"""

    def test_consumed_checkmark_appears_in_note_prev_eval_column(self):
        latest1 = self._seed_baseline()
        gen_ai = next(c for c in latest1["active_clusters"] if c["name"] == "生成AI活用")
        qid = gen_ai["queries"][0]["query_id"]

        common.append_event(self.ledger_path, {
            "type": "evaluation_batch", "note_revision": "rev-test",
            "evaluations": [{"id_type": "qid", "id": qid, "mark": "✅"}],
        })
        clusters_in = [self._keep_entry(c) for c in latest1["active_clusters"]]
        self._write_result({"clusters": clusters_in, "proposals": []})
        self._write_worklist()
        code = self._run(baseline=False)
        self.assertEqual(code, 0)

        latest2 = self._latest()
        gen_ai2 = next(c for c in latest2["active_clusters"] if c["cluster_id"] == gen_ai["cluster_id"])
        q2 = next(q for q in gen_ai2["queries"] if q["query_id"] == qid)
        self.assertEqual(len(q2["eval_history"]), 1, "consumeした評価がeval_historyへ1件追記されるべき")
        self.assertEqual(q2["eval_history"][0]["mark"], "✅")
        self.assertIn("at", q2["eval_history"][0])

        note_text = Path(self.note_path).read_text(encoding="utf-8")
        self.assertIn("✅ (", note_text, "render後のノートの前回評価列に✅と日付が表示されるべき")


class TestMarkdownInjectionDefense(IngestTestBase):
    """P0-5: LLM由来のname_ja/why等にmdテーブル破壊・評価マーカー偽造を仕込んでも
    render→再parseで評価が収穫されないこと。"""

    def test_forged_pid_marker_in_name_ja_not_harvested(self):
        latest1 = self._seed_baseline()
        malicious_latest = dict(latest1)
        malicious_latest["active_clusters"] = []
        malicious_latest["dormant_clusters"] = []
        malicious_latest["proposals"] = [{
            "name_ja": "本物提案 | ✅ <!-- pid:p-fake -->",
            "why": "理由",
            "proposal_id": "p-real000",
        }]

        note_text = ingest.render_note(malicious_latest, {}, canonical_total=0, empty_text_excluded=0)

        self.assertNotIn("<!-- pid:p-fake -->", note_text, "偽造マーカーのHTMLコメント開始が無害化されているべき")

        evaluations, unknown = common.note_eval_parse(note_text)
        harvested_ids = {e["id"] for e in evaluations}
        self.assertNotIn("p-fake", harvested_ids, "name_jaに埋め込まれた偽造pidマーカーが収穫されてはならない")


class TestPipelineLockExclusion(IngestTestBase):
    """P1-1: .pipeline.lockを既に保持しているプロセスが存在する場合、ingestはexit 7で
    停止し台帳に何も書かないこと。"""

    def test_locked_pipeline_returns_exit7(self):
        latest1 = self._seed_baseline()
        clusters_in = [self._keep_entry(c) for c in latest1["active_clusters"]]
        self._write_result({"clusters": clusters_in, "proposals": []})
        self._write_worklist()

        with common.pipeline_lock(self.ledger_path, blocking=False):
            code = self._run(baseline=False)

        self.assertEqual(code, 7)
        events = self._events()
        self.assertEqual([e["type"] for e in events], ["generation"], "ロック取得失敗時は台帳に何も書かないべき")


if __name__ == "__main__":
    unittest.main(verbosity=2)
