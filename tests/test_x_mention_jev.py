"""判定器 Jev で関係ない投稿を外す口（scripts/x_mention_jev.py・2026-10-29 P-ENV-242 案 1 の③）の回帰テスト。

守る決め:
- 線未満だけ外す・点が無い時（失敗・鍵なし・時間切れ・429・伏せきれず・予算切れ）は外さない
- 伏せきれない本文は送らない／点の貯めにある投稿は送り直さない／429 は 1 回だけ待ってやり直し、続けば止める
- extract_day は keep を渡さなければ今までと同じ・外しても「その日を処理した印」は残る
- 設定が無い・壊れている時は切（＝今までと同じ動き）

実行: python3 tests/test_x_mention_jev.py   （unittest 自走・pytest 不要。Jev へは送らない＝偽の呼び出し口）
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import x_mention_jev as xj  # noqa: E402

CFG = {**xj.DEFAULTS, "enabled": True, "retry_429_wait_sec": 0}


class FakeClient:
    """偽の Jev。answers を順に返す（('p', 値) は 2 問とも同じ確率・('fail', 理由) は失敗）。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[str] = []

    def __call__(self, state, qs, timeout):
        self.calls.append(state)
        kind, v = self.answers.pop(0) if self.answers else ("fail", "empty")
        if kind == "p":
            return {k: v for k in qs}, {"model": "fake", "ms": 1}
        return None, v


def post(sid: str, text: str = "メモリが品薄で値上げ") -> dict:
    return {"status_id": sid, "text": text, "query_id": "q"}


class MaskTest(unittest.TestCase):
    def test_masks_url_name_mail_tel(self):
        t, ok = xj.mask_text("詳しくは https://example.com/a @abc_1 a.b@example.co.jp 03-1234-5678 まで")
        self.assertTrue(ok)
        for leak in ("https://", "@abc", "example.co.jp", "1234-5678"):
            self.assertNotIn(leak, t)

    def test_masks_uppercase_url_and_intl_paren_tel(self):
        # 2026-10-29 Codex 指摘の取りこぼし（大文字の URL・国番号つき・かっこ書きの電話）
        for s in ("HTTPS://example.com/private", "+819012345678", "+1-212-555-0123", "03(1234)5678"):
            t, ok = xj.mask_text(f"連絡先 {s} まで")
            self.assertTrue(ok)
            self.assertNotIn(s, t)
        self.assertEqual(xj.mask_text("価格は1,980円で+10%")[0], "価格は1,980円で+10%")   # 値段・率は伏せない

    def test_truncates_long_text(self):
        t, ok = xj.mask_text("あ" * 5000)
        self.assertTrue(ok)
        self.assertEqual(len(t), xj.MAX_CHARS)


class FilterTest(unittest.TestCase):
    def mk(self, answers, scores=None, cfg=None):
        cl = FakeClient(answers)
        return xj.JevFilter(cfg or CFG, client=cl, scores={} if scores is None else scores,
                            sleep=lambda s: None), cl

    def test_drop_only_below_threshold(self):
        f, _ = self.mk([("p", 0.10), ("p", 0.15), ("p", 0.90)])
        self.assertFalse(f.keep("d", post("1"), ["1111"]))   # 0.10 < 0.15 → 外す
        self.assertTrue(f.keep("d", post("2"), ["1111"]))    # 0.15 は線上＝残す
        self.assertTrue(f.keep("d", post("3"), ["1111"]))
        self.assertEqual(f.stats["d"]["外した"], 1)
        self.assertEqual(len(f.dropped["d"]), 1)
        self.assertEqual(f.dropped["d"][0]["status_id"], "1")

    def test_failure_keeps_and_records_reason(self):
        f, _ = self.mk([("fail", "TimeoutError")])
        self.assertTrue(f.keep("d", post("1"), ["1111"]))
        self.assertEqual(f.unjudged["d"]["TimeoutError"], 1)
        self.assertEqual(f.stats["d"]["判定できず残した"], 1)

    def test_no_key_stops_and_keeps_rest_without_calling(self):
        f, cl = self.mk([("fail", "no_key"), ("p", 0.0)])
        self.assertTrue(f.keep("d", post("1"), ["1111"]))
        self.assertTrue(f.keep("d", post("2"), ["1111"]))
        self.assertEqual(len(cl.calls), 1)          # 2 件目は送らない
        self.assertEqual(f.stop_reason, "no_key")
        self.assertEqual(f.unjudged["d"]["no_key"], 2)

    def test_429_retries_once_then_stops(self):
        f, cl = self.mk([("fail", "http_429"), ("fail", "http_429"), ("p", 0.0)])
        self.assertTrue(f.keep("d", post("1"), ["1111"]))
        self.assertTrue(f.keep("d", post("2"), ["1111"]))
        self.assertEqual(len(cl.calls), 2)          # 最初の 1 件で 1 回だけやり直し
        self.assertEqual(f.stop_reason, "http_429")

    def test_429_then_success(self):
        f, cl = self.mk([("fail", "http_429"), ("p", 0.01)])
        self.assertFalse(f.keep("d", post("1"), ["1111"]))
        self.assertIsNone(f.stop_reason)

    def test_consecutive_failures_open_circuit(self):
        f, cl = self.mk([("fail", "URLError")] * 10, cfg={**CFG, "max_consecutive_fail": 3})
        for i in range(6):
            self.assertTrue(f.keep("d", post(str(i)), ["1111"]))
        self.assertEqual(len(cl.calls), 3)
        self.assertTrue(f.stop_reason.startswith("consecutive_fail"))

    def test_mask_left_is_not_sent(self):
        # 伏せの 2 段目で「残っている」と出た本文は送らずに数えに残す
        f2, cl2 = self.mk([("p", 0.0)])
        orig = xj.mask_text
        try:
            xj.mask_text = lambda t: (t, False)
            self.assertTrue(f2.keep("d", post("1"), ["1111"]))
        finally:
            xj.mask_text = orig
        self.assertEqual(cl2.calls, [])
        self.assertEqual(f2.unjudged["d"]["mask_left"], 1)

    def test_cache_hit_not_resent(self):
        cache = {"1": {"status_id": "1", "qv": xj.QV, "score": 0.05, "p": {}}}
        f, cl = self.mk([("p", 0.9)], scores=cache)
        self.assertFalse(f.keep("d", post("1"), ["1111"]))
        self.assertEqual(cl.calls, [])
        self.assertEqual(f.dropped["d"][0]["src"], "cache")

    def test_one_decision_per_post(self):
        f, cl = self.mk([("p", 0.9)])
        self.assertTrue(f.keep("d", post("1"), ["1111", "2222"]))
        self.assertTrue(f.keep("d", post("1"), ["1111", "2222"]))
        self.assertEqual(len(cl.calls), 1)
        self.assertEqual(f.stats["d"]["判定にかけた"], 1)

    def test_budget_exhausted_keeps(self):
        t = [0.0]
        cl = FakeClient([("p", 0.0)] * 5)
        f = xj.JevFilter({**CFG, "budget_sec": 10}, client=cl, scores={}, sleep=lambda s: None,
                         clock=lambda: t[0])
        self.assertFalse(f.keep("d", post("1"), ["1111"]))
        t[0] = 11.0
        self.assertTrue(f.keep("d", post("2"), ["1111"]))
        self.assertEqual(f.stop_reason, "budget")

    def test_no_client_keeps_everything(self):
        f = xj.JevFilter(CFG, client=None, scores={}, sleep=lambda s: None)
        f.client = None
        f.stop_reason = "no_client"
        self.assertTrue(f.keep("d", post("1"), ["1111"]))
        self.assertEqual(f.unjudged["d"]["no_client"], 1)


class ConfigAndScoresTest(unittest.TestCase):
    def test_missing_or_broken_config_is_off(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(xj.load_config(Path(td) / "nai.json")["enabled"])
            p = Path(td) / "broken.json"
            p.write_text("{壊れた", encoding="utf-8")
            self.assertFalse(xj.load_config(p)["enabled"])

    def test_repo_config_values(self):
        cfg = xj.load_config()
        self.assertEqual(cfg["threshold"], 0.15)

    def test_scores_ignore_other_question_version(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.jsonl"
            p.write_text("\n".join([
                json.dumps({"status_id": "1", "qv": xj.QV, "score": 0.1}),
                json.dumps({"status_id": "2", "qv": "v1", "score": 0.1}),
                "壊れた行",
                json.dumps({"status_id": "1", "qv": xj.QV, "score": 0.7}),   # 後の行が勝つ
            ]), encoding="utf-8")
            s = xj.load_scores(p)
            self.assertEqual(set(s), {"1"})
            self.assertEqual(s["1"]["score"], 0.7)


class ExtractDayHookTest(unittest.TestCase):
    """extract_day の keep 口: 省略時は今までと同じ・外しても処理済みの印は残る。"""

    def setUp(self):
        import x_mention_dict as xmd
        import x_mention_extract as xme
        self.xme = xme
        table = xmd.build_dict()
        self.matcher = xmd.build_matcher(table)
        self.codes = {c for c, _ in table.values()}
        self.td = tempfile.TemporaryDirectory()
        self.saved = xme.TEXTS_DIR
        xme.TEXTS_DIR = Path(self.td.name)
        recs = [{"status_id": "a", "text": "キオクシアのメモリが品薄で値上げ", "query_id": "q"},
                {"status_id": "b", "text": "キオクシアの株価が上がった、買い増し", "query_id": "q"},
                {"status_id": "c", "text": "今日は晴れて気持ちがいい", "query_id": "q"}]
        (xme.TEXTS_DIR / "2026-10-01.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in recs), encoding="utf-8")

    def tearDown(self):
        self.xme.TEXTS_DIR = self.saved
        self.td.cleanup()

    @staticmethod
    def strip(rows):
        return [{k: v for k, v in r.items() if k != "run_at"} for r in rows]

    def test_no_keep_is_unchanged(self):
        a = self.xme.extract_day("2026-10-01", self.matcher, self.codes)
        b = self.xme.extract_day("2026-10-01", self.matcher, self.codes, keep=None, quiet=True)
        self.assertEqual(self.strip(a), self.strip(b))

    def test_keep_drops_post_and_keeps_scanned_mark(self):
        base = {r["code"]: r["count"] for r in self.xme.extract_day("2026-10-01", self.matcher, self.codes)}
        seen = []
        rows = self.xme.extract_day("2026-10-01", self.matcher, self.codes,
                                    keep=lambda p, codes: seen.append(p["status_id"]) or p["status_id"] != "b")
        got = {r["code"]: r["count"] for r in rows}
        self.assertIn(self.xme.SCANNED_MARK, got)
        kio = [c for c in base if c != self.xme.SCANNED_MARK and base[c] == 2]
        self.assertTrue(kio, "キオクシアが 2 件で数えられる前提")
        self.assertEqual(got[kio[0]], 1)                 # b を外した分だけ減る
        self.assertEqual(sorted(set(seen)), ["a", "b"])   # 銘柄に当たらない投稿は判定器に聞かない


if __name__ == "__main__":
    unittest.main()
