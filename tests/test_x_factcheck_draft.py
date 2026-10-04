"""裏取りレーン（P-INF-26・オーナー裁定 a 2026-10-04）の回帰テスト。

守らせる 5 点:
1. 事象の選別は追記9c と同一（first_seen=true・first_posted_at あり・買い候補 sign+ confirmed/provisional）。同じ語は最初の便だけ。
2. 同一記事の束ね= URL（t.co 以外）か本文先頭で束ね、最古時刻を残す。
3. 遅れラベル= 速報 <6h／当日 ≤24h／週内 ≤7d／再流通 >7d／投稿が先／未確認。
4. 公開時刻の抽出= article:published_time・JSON-LD・<time datetime>（UTC 化・取れなければ空）。
5. 台帳は append-only で最後の行が現在値。draft は台帳済みの語を二重に書かない・fill は選択肢外を拒む。

ネット・ブラウザは呼ばない（refetch_posts / fetch_published_time は差し替える）。
実行: docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_x_factcheck_draft
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import x_factcheck_draft as fc  # noqa: E402

MAP = {"subjects": [
    {"id": "dram", "label": "DRAM", "discover_tokens": ["マイクロン"], "beneficiaries": [
        {"code": "6857", "name": "アドバンテスト", "sign": "+", "tier": "confirmed"},
        {"code": "9999", "name": "損側", "sign": "-", "tier": "confirmed"},
        {"code": "8888", "name": "却下", "sign": "+", "tier": "rejected"},
    ]},
    {"id": "no-buy", "label": "売りだけ", "discover_tokens": ["光熱費"], "beneficiaries": [
        {"code": "7777", "name": "x", "sign": "-", "tier": "confirmed"}]},
]}

BATCHES = [
    {"type": "candidate_batch", "date": "2026-10-02", "candidates": [
        {"token": "マイクロン", "first_seen": True, "first_posted_at": "2026-10-01T23:30:00.000Z", "sample": "s2"},
    ]},
    {"type": "candidate_batch", "date": "2026-09-26", "candidates": [
        {"token": "マイクロン", "first_seen": True, "first_posted_at": "2026-09-25T10:00:00.000Z", "sample": "s1"},
        {"token": "光熱費", "first_seen": True, "first_posted_at": "2026-09-25T10:00:00.000Z"},
        {"token": "可能性", "first_seen": False, "first_posted_at": "2026-09-25T10:00:00.000Z"},
        {"token": "生産能力", "first_seen": True, "first_posted_at": ""},
    ]},
    {"type": "other", "date": "2026-09-27", "candidates": [{"token": "マイクロン", "first_seen": True, "first_posted_at": "x"}]},
]


class Events(unittest.TestCase):
    def test_selection_matches_prereg_and_first_batch_wins(self):
        ev = fc.events_from_queue(BATCHES, MAP)
        self.assertEqual([e["token"] for e in ev], ["マイクロン"])
        self.assertEqual(ev[0]["batch_date"], "2026-09-26")          # 便は date 昇順＝最初の便
        self.assertEqual(ev[0]["first_posted_at"], "2026-09-25T10:00:00.000Z")
        self.assertEqual([b["code"] for s in ev[0]["subjects"] for b in s["beneficiaries"]], ["6857"])  # sign- と rejected は落ちる


class Bundles(unittest.TestCase):
    def test_bundle_by_url_then_headline(self):
        posts = [
            {"status_id": "1", "author": "a", "posted_at_full": "2026-10-01T05:00:00.000Z", "text": "東芝が HDD 値上げ reuters.com/x/abc"},
            {"status_id": "2", "author": "b", "posted_at_full": "2026-10-01T03:00:00.000Z", "text": "速報 https://www.reuters.com/x/abc/"},
            {"status_id": "3", "author": "a", "posted_at_full": "2026-10-01T06:00:00.000Z", "text": "東芝が HDD 値上げを発表 https://t.co/zzz"},
            {"status_id": "4", "author": "c", "posted_at_full": "2026-10-01T07:00:00.000Z", "text": "【東芝】が HDD 値上げを発表 だそうです"},
        ]
        b = fc.bundle_posts(posts)
        self.assertEqual(len(b), 2)
        self.assertEqual(b[0]["kind"], "url")
        self.assertEqual(b[0]["n_posts"], 2)
        self.assertEqual(b[0]["earliest_posted_at"], "2026-10-01T03:00:00.000Z")
        self.assertEqual(sorted(b[0]["authors"]), ["a", "b"])
        self.assertEqual(b[1]["kind"], "headline")
        self.assertEqual(b[1]["n_posts"], 2)   # t.co だけの投稿は見出しで束ねる

    def test_url_key_lowercases_host_only(self):
        self.assertEqual(fc._url_key("https://WWW.Example.org/AbC/"), "example.org/AbC")
        self.assertNotEqual(fc._url_key("https://example.org/AbC"), fc._url_key("https://example.org/abc"))

    def test_source_url_only_from_oldest_post_that_reaches_first_post(self):
        ev = {"token": "t", "first_posted_at": "2026-10-02T07:52:56.000Z"}
        first = {"posted_at_full": "2026-10-02T07:52:56.000Z", "urls": ["https://a.example/first"]}
        later_big = [{"posted_at_full": "2026-10-02T12:00:00.000Z", "urls": ["https://b.example/other"]}] * 3
        self.assertEqual(fc.source_url_for_first_post(ev, later_big), "")             # 初出まで届かない → 空
        self.assertEqual(fc.source_url_for_first_post(ev, later_big + [first]), "https://a.example/first")
        first_nourl = dict(first, urls=[])
        self.assertEqual(fc.source_url_for_first_post(ev, later_big + [first_nourl]), "")  # 最古が URL 無し → 多数派の URL は採らない

    def test_extract_urls_strips_trailing_punct(self):
        self.assertEqual(fc.extract_urls("見て https://eia.gov/a/b)。"), ["https://eia.gov/a/b"])


class DelayLabel(unittest.TestCase):
    def test_bands(self):
        src = "2026-10-01T00:00:00Z"
        self.assertEqual(fc.delay_label("2026-10-01T05:59:00Z", src), "速報")
        self.assertEqual(fc.delay_label("2026-10-01T06:00:00Z", src), "当日")
        self.assertEqual(fc.delay_label("2026-10-02T00:00:00Z", src), "当日")
        self.assertEqual(fc.delay_label("2026-10-05T00:00:00Z", src), "週内")
        self.assertEqual(fc.delay_label("2026-10-08T00:00:01Z", src), "再流通")
        self.assertEqual(fc.delay_label("2026-09-30T23:00:00Z", src), "投稿が先")
        self.assertEqual(fc.delay_label("2026-10-01T05:59:00Z", ""), "未確認")
        self.assertEqual(fc.delay_label("", src), "未確認")


class PublishedTime(unittest.TestCase):
    def test_meta_property(self):
        html = '<meta property="article:published_time" content="2026-10-01T09:30:00+09:00">'
        self.assertEqual(fc.parse_published_time(html), "2026-10-01T00:30:00Z")

    def test_json_ld_and_missing_and_ambiguous_values_are_empty(self):
        self.assertEqual(fc.parse_published_time('{"datePublished":"2026-10-01T00:00:00Z"}'), "2026-10-01T00:00:00Z")
        self.assertEqual(fc.parse_published_time("<html>no date</html>"), "")
        self.assertEqual(fc.parse_published_time('<meta property="article:published_time" content="junk">'), "")
        # 日付だけ・タイムゾーン無し・<time> タグは補わない（Codex #3）
        self.assertEqual(fc.parse_published_time('<meta property="article:published_time" content="2026-10-02">'), "")
        self.assertEqual(fc.parse_published_time('{"datePublished":"2026-10-02T09:00:00"}'), "")
        self.assertEqual(fc.parse_published_time('<time datetime="2026-10-01T12:00:00Z">'), "")


class CoversFirstPost(unittest.TestCase):
    def test_flag_true_only_when_refetch_reaches_first_post_time(self):
        ev = {"token": "t", "batch_date": "d", "first_posted_at": "2026-10-02T07:52:56.000Z", "subjects": []}
        late = [{"posted_at_full": "2026-10-02T12:00:00.000Z", "text": "", "author": "a"}]
        early = late + [{"posted_at_full": "2026-10-02T07:52:56.000Z", "text": "", "author": "b"}]
        self.assertFalse(fc.draft_row(ev, late, "ok", "", "")["covers_first_post"])
        self.assertTrue(fc.draft_row(ev, early, "ok", "", "")["covers_first_post"])


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.queue = self.tmp / "q.jsonl"
        self.ledger = self.tmp / "l.jsonl"
        self.queue.write_text("\n".join(json.dumps(b, ensure_ascii=False) for b in BATCHES) + "\nnot json\n", encoding="utf-8")

    def _run(self, argv):
        with mock.patch.object(sys, "argv", ["x", *argv]), \
             mock.patch.object(fc, "QUEUE", self.queue), mock.patch.object(fc, "LEDGER", self.ledger), \
             mock.patch.object(fc.xsm, "load", return_value=MAP), \
             mock.patch.object(fc, "refetch_posts", side_effect=AssertionError("ブラウザを呼んではいけない")), \
             mock.patch.object(fc, "fetch_published_time", side_effect=AssertionError("ネットを呼んではいけない")):
            return fc.main()

    def test_draft_once_then_fill_then_state(self):
        self.assertEqual(self._run(["draft", "--no-fetch"]), 0)
        rows = fc.read_jsonl(self.ledger)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["type"], r["status"], r["token"], r["refetch_status"], r["delay_label"]),
                         ("factcheck", "draft", "マイクロン", "skipped", "未確認"))
        self.assertEqual(r["first_posted_at_jst"], "2026-09-25 19:00")
        self.assertEqual(r["human"], fc.empty_human())
        self.assertFalse(r["covers_first_post"])
        # 二重書き防止
        self.assertEqual(self._run(["draft", "--no-fetch"]), 0)
        self.assertEqual(len(fc.read_jsonl(self.ledger)), 1)
        # fill は追記・最後の行が現在値
        self.assertEqual(self._run(["fill", "--token", "マイクロン", "--primary", "○", "--label", "一次", "--scope", "一致", "--note", "決算"]), 0)
        rows = fc.read_jsonl(self.ledger)
        self.assertEqual(len(rows), 2)
        cur = fc.ledger_state(rows)["マイクロン"]
        self.assertEqual(cur["status"], "filled")
        self.assertEqual(cur["human"]["primary_source"], "○")
        self.assertEqual(cur["human"]["note"], "決算")
        self.assertTrue(cur["human"]["filled_at"])
        self.assertEqual(rows[0]["status"], "draft")   # 元の下書きは消えない
        self.assertEqual(self._run(["list"]), 0)

    def test_fill_unknown_token_and_bad_choice(self):
        self.assertEqual(self._run(["fill", "--token", "無い語", "--primary", "○", "--label", "一次", "--scope", "一致"]), 2)
        with self.assertRaises(ValueError):
            fc.fill_row({"token": "x", "human": {}}, "◎", "一次", "一致", "")

    def test_error_or_skipped_draft_is_retried_when_fetching(self):
        self.assertEqual(self._run(["draft", "--no-fetch"]), 0)              # 骨だけ（refetch_status=skipped）
        self.assertEqual(self._run(["draft", "--no-fetch"]), 0)              # 骨の二重書きはしない
        self.assertEqual(len(fc.read_jsonl(self.ledger)), 1)
        fake = [{"id": "9", "author": "z", "likes": 30, "posted_at_full": "2026-09-25T10:00:00.000Z", "content": "本文"}]
        with mock.patch.object(sys, "argv", ["x", "draft"]), \
             mock.patch.object(fc, "QUEUE", self.queue), mock.patch.object(fc, "LEDGER", self.ledger), \
             mock.patch.object(fc.xsm, "load", return_value=MAP), \
             mock.patch.object(fc, "refetch_posts", return_value=(fc.normalize_refetched(fake), "ok")), \
             mock.patch.object(fc, "fetch_published_time", return_value=""), \
             mock.patch.object(fc.time, "sleep", lambda *_: None):
            self.assertEqual(fc.main(), 0)                                   # 取り直しありなら再試行して追記
            self.assertEqual(fc.main(), 0)                                   # ok になった後は追記しない
        rows = fc.read_jsonl(self.ledger)
        self.assertEqual([r["refetch_status"] for r in rows], ["skipped", "ok"])
        self.assertTrue(rows[1]["covers_first_post"])
        self.assertTrue(fc.needs_draft({"status": "draft", "refetch_status": "error"}, fetch=True))
        self.assertFalse(fc.needs_draft({"status": "filled", "refetch_status": "error"}, fetch=True))
        self.assertFalse(fc.needs_draft({"status": "draft", "refetch_status": "error"}, fetch=False))

    def test_fill_with_source_time_relabels_delay_and_rejects_naive_time(self):
        self.assertEqual(self._run(["draft", "--no-fetch"]), 0)
        self.assertEqual(self._run(["fill", "--token", "マイクロン", "--primary", "△", "--label", "二次", "--scope", "一致",
                                    "--source-time", "2026-09-24T10:00:00+09:00", "--source-url", "https://x.example/r"]), 0)
        cur = fc.ledger_state(fc.read_jsonl(self.ledger))["マイクロン"]
        self.assertEqual(cur["source_published_at"], "2026-09-24T01:00:00Z")
        self.assertEqual(cur["delay_label"], "週内")        # 初出 09-25T10:00Z − 元報道 09-24T01:00Z = 33h
        self.assertEqual(cur["source_url"], "https://x.example/r")
        self.assertEqual(self._run(["fill", "--token", "マイクロン", "--primary", "△", "--label", "二次", "--scope", "一致",
                                    "--source-time", "2026-09-24T10:00:00"]), 2)

    def test_dry_run_writes_nothing(self):
        self.assertEqual(self._run(["draft", "--no-fetch", "--dry-run"]), 0)
        self.assertFalse(self.ledger.exists())


if __name__ == "__main__":
    unittest.main()
