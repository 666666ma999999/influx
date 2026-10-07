"""yuho_read（P-INF-34 工程③）の回帰テスト。Claude API は呼ばない（urllib をモック）。

対象:
(a) build_prompt: 11 列の名前・「数字を補わない」「JSON 配列だけ」・報告セグメント名・節本文が入る。
    説明会資料を渡すと末尾に「任意開示・有報を正」の注記と本文（max_ir_chars で切る）が付く
(b) extract_json_array: コードフェンス・前後の文を剥がして配列にする／配列でなければ None
(c) call_claude: usage を返す／JSON が取れない応答は 1 回だけ再試行（合計 2 回）し usage を合算・error を記録／
    HTTPError も 2 回で止まり rows=[]
(d) fy_label / find_ir_text: 2025-04-01〜2026-03-31 → FY2025・ir/FY2025*.txt を引く
(e) normalize_rows の型検証（Codex Critical 2・2 回目 Critical 5）: "false" は False・quote_section は節キーだけ・
    label_hint は 3 値だけ・4 行目以降は捨てる・文字列列は str 以外を None＋警告・strip・空白だけは None・
    kpi_value は数値か数値に読める文字列だけ・item か kpi_name が無い行は「必須列欠落」で捨てる・warnings に残る
(f) read_one のスキップ条件（Codex Critical 8・2 回目 Warning）: 同じ model で rows あり error なし・prompt_sha256 が
    一致の時だけスキップ。代走・モデル違い・error あり・hash 違い・hash なしは呼び直す。read.json に prompt_sha256・
    warnings・usage_ok が入る
(h) usage_ok（2 回目 Critical 6）: API 応答から usage を取れた時だけ True。HTTP エラー・本文が JSON でない時は False。
    JSON 配列が取れなくても usage があれば True（消費は実測）
(g) main の終了コード（Codex Critical 7）: 1 件でも error ありなら 1（read.json は保存したまま）・全部成功なら 0・鍵なしは SystemExit

実行: docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_yuho_read -v
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import yuho_read as yr  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "yuho" / "9999"


def _resp(text: str, inp: int = 100, out: int = 20, stop: str = "end_turn") -> dict:
    return {"content": [{"type": "text", "text": text}], "usage": {"input_tokens": inp, "output_tokens": out},
            "stop_reason": stop}


class _FakeHTTP:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class BuildPromptTest(unittest.TestCase):
    def setUp(self):
        self.sec = json.loads((FIX / "S100TESTA.sections.json").read_text(encoding="utf-8"))

    def test_columns_and_rules(self):
        p = yr.build_prompt(self.sec, None)
        for col in yr.ROW_KEYS:
            self.assertIn(f"- {col}:", p)
        self.assertIn("数字を補わない", p)
        self.assertIn("JSON 配列だけ", p)
        self.assertIn("成長事業", p)
        self.assertIn("調味料・食品／ヘルスケア等", p)
        self.assertIn("[mdna]", p)
        self.assertIn("海外調味料の販売数量が前期比8%増", p)
        self.assertNotIn("[business_results]", p)   # null の節は付けない
        self.assertNotIn("決算説明会資料", p)

    def test_ir_text_appended_and_clipped(self):
        p = yr.build_prompt(self.sec, "資料本文" * 100, max_ir_chars=50, ir_name="FY2025_presentation.txt")
        self.assertIn("任意開示", p)
        self.assertIn("有報と食い違う数字は有報を正", p)
        self.assertIn("FY2025_presentation.txt", p)
        tail = p.split("=== 決算説明会資料")[1]
        self.assertLessEqual(tail.count("資料本文"), 13)  # 50 字 ÷ 4 字
        self.assertIn('quote_section を "ir_presentation"', p)


class ExtractJsonTest(unittest.TestCase):
    def test_fenced_and_prefixed(self):
        self.assertEqual(yr.extract_json_array('```json\n[{"a": 1}]\n```'), [{"a": 1}])
        self.assertEqual(yr.extract_json_array('結果です:\n[{"a": 1}, {"b": 2}]\n以上'), [{"a": 1}, {"b": 2}])
        self.assertEqual(yr.extract_json_array("[]"), [])

    def test_not_array(self):
        self.assertIsNone(yr.extract_json_array('{"a": 1}'))
        self.assertIsNone(yr.extract_json_array("候補はありません"))
        self.assertIsNone(yr.extract_json_array(""))

    def test_normalize_rows(self):
        rows, warnings = yr.normalize_rows([{"segment": "A", "item": "x", "kpi_name": "k", "expectation": 1}, "junk",
                                            {"item": 5, "kpi_name": "k"}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), set(yr.ROW_KEYS))
        self.assertIs(rows[0]["expectation"], True)
        # "junk" を捨てた記録・item=5 は文字列でない→None・その行は必須列欠落で捨てた
        self.assertEqual(len(warnings), 3)
        self.assertIn("dict でない", warnings[0])
        self.assertIn("item=5 は文字列でない", warnings[1])
        self.assertIn("必須列欠落", warnings[2])


class NormalizeRowsTypeTest(unittest.TestCase):
    """Codex Critical 2: LLM 行の型検証。"""

    def test_parse_bool_strict(self):
        self.assertEqual(yr.parse_bool(True), (True, True))
        self.assertEqual(yr.parse_bool(False), (False, True))
        self.assertEqual(yr.parse_bool("true"), (True, True))
        self.assertEqual(yr.parse_bool(" TRUE "), (True, True))
        self.assertEqual(yr.parse_bool("false"), (False, True))
        self.assertEqual(yr.parse_bool(1), (True, True))
        self.assertEqual(yr.parse_bool(0), (False, True))
        self.assertEqual(yr.parse_bool(None), (False, True))
        for bad in ("yes", "no", "1", "", 2, -1, 1.0, [], {}):
            self.assertEqual(yr.parse_bool(bad), (False, False), repr(bad))

    def test_expectation_string_false_is_false(self):
        rows, warnings = yr.normalize_rows([
            {"item": "a", "kpi_name": "k", "expectation": "false"},
            {"item": "b", "kpi_name": "k", "expectation": "true"},
            {"item": "c", "kpi_name": "k", "expectation": "yes"},
        ])
        self.assertEqual([r["expectation"] for r in rows], [False, True, False])
        self.assertEqual(len(warnings), 1)
        self.assertIn("row2", warnings[0])
        self.assertIn("'yes'", warnings[0])

    def test_quote_section_and_label_hint_whitelist(self):
        rows, warnings = yr.normalize_rows([
            {"item": "a", "kpi_name": "k", "quote_section": "foo", "label_hint": "強気"},
            {"item": "b", "kpi_name": "k", "quote_section": "ir_presentation", "label_hint": "需要"},
            {"item": "c", "kpi_name": "k", "quote_section": "mdna", "label_hint": None},
        ])
        self.assertIsNone(rows[0]["quote_section"])
        self.assertIsNone(rows[0]["label_hint"])
        self.assertEqual(rows[1]["quote_section"], "ir_presentation")
        self.assertEqual(rows[1]["label_hint"], "需要")
        self.assertEqual(rows[2]["quote_section"], "mdna")
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("quote_section='foo'" in w for w in warnings))
        self.assertTrue(any("label_hint='強気'" in w for w in warnings))
        for key in yr.SECTION_TITLES:
            self.assertIn(key, yr.ALLOWED_SECTIONS)
        self.assertEqual(len(yr.ALLOWED_SECTIONS), 8)

    def test_more_than_three_rows_truncated(self):
        rows, warnings = yr.normalize_rows([{"item": f"i{n}", "kpi_name": "k"} for n in range(5)])
        self.assertEqual([r["item"] for r in rows], ["i0", "i1", "i2"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("行数 5", warnings[0])

    def test_rows_missing_item_or_kpi_name_are_dropped(self):
        rows, warnings = yr.normalize_rows([
            {"kpi_value": 10, "kpi_unit": "%"},                                   # item も kpi_name も無い → 捨てる
            {"segment": "S", "item": "a", "kpi_name": None, "quote": "本文だけ"},  # kpi_name 無し → 捨てる
            {"segment": "S", "item": "  ", "kpi_name": "k", "quote": "q"},        # item が空白だけ → None → 捨てる
            {"segment": None, "item": "b", "kpi_name": "k", "quote": None},       # item＋kpi_name があれば残す
        ])
        self.assertEqual([r["item"] for r in rows], ["b"])
        self.assertEqual(len(warnings), 3)
        self.assertTrue(all("必須列欠落" in w for w in warnings))
        self.assertIn("item=None kpi_name=None", warnings[0])
        self.assertIn("item='a' kpi_name=None", warnings[1])
        self.assertIn("item=None kpi_name='k'", warnings[2])

    def test_string_columns_non_str_to_none_and_strip(self):
        rows, warnings = yr.normalize_rows([{
            "segment": {"name": "S"}, "item": " 海外調味料 ", "kpi_name": " 数量 ", "quote": "   ",
            "quote_section": ["mdna"], "share_scope": 12, "label_hint": True, "kpi_unit": 1.5,
        }])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["item"], r["kpi_name"]), ("海外調味料", "数量"))   # strip
        self.assertIsNone(r["quote"])                                            # 空白だけ → None（警告なし）
        for key in ("segment", "quote_section", "share_scope", "label_hint", "kpi_unit"):
            self.assertIsNone(r[key], key)
        self.assertEqual(len(warnings), 5)
        self.assertTrue(all("は文字列でない" in w for w in warnings), warnings)
        self.assertTrue(any("segment={'name': 'S'} は文字列でない (dict)" in w for w in warnings), warnings)
        self.assertTrue(any("label_hint=True は文字列でない (bool)" in w for w in warnings), warnings)

    def test_kpi_value_numeric_or_numeric_string_only(self):
        base = {"item": "a", "kpi_name": "k"}
        rows, warnings = yr.normalize_rows([
            dict(base, kpi_value=105, kpi_prev_value=1.5),
            dict(base, kpi_value=" 106% ", kpi_prev_value="1,234"),
            dict(base, kpi_value=True, kpi_prev_value={"v": 1}),
        ])
        self.assertEqual([(r["kpi_value"], r["kpi_prev_value"]) for r in rows],
                         [(105, 1.5), ("106%", "1,234"), (None, None)])
        self.assertEqual(len(warnings), 2)
        self.assertIn("kpi_value=True は数値でない (bool)", warnings[0])
        self.assertIn("kpi_prev_value={'v': 1} は数値でない (dict)", warnings[1])
        rows, warnings = yr.normalize_rows([dict(base, kpi_value="好調"), dict(base, kpi_value=""), dict(base, kpi_value=[1])])
        self.assertEqual([r["kpi_value"] for r in rows], [None, None, None])
        self.assertEqual(len(warnings), 2)   # "" は警告なしで None
        self.assertIn("kpi_value='好調' は数値に読めない", warnings[0])
        self.assertIn("kpi_value=[1] は数値でない (list)", warnings[1])

    def test_parse_number(self):
        self.assertEqual(yr.parse_number("106%"), 106)
        self.assertEqual(yr.parse_number("1,234"), 1234)
        self.assertEqual(yr.parse_number("１０５"), 105)
        self.assertEqual(yr.parse_number(" 0.5 "), 0.5)
        self.assertEqual(yr.parse_number(96), 96)
        self.assertEqual(yr.parse_number(1.25), 1.25)
        for bad in (None, True, False, "", "好調", "約106", [1], {"v": 1}, float("nan")):
            self.assertIsNone(yr.parse_number(bad), repr(bad))

    def test_call_claude_carries_warnings(self):
        good = _resp('[{"segment": "A", "item": "x", "kpi_name": "k", "expectation": "false", "quote_section": "zzz"}]')
        with mock.patch.object(yr.urllib.request, "urlopen", return_value=_FakeHTTP(good)):
            r = yr.call_claude("p", "model-x", "sk-ant-test", retry_wait=0)
        self.assertIs(r["rows"][0]["expectation"], False)   # "false" は受け付ける値＝警告なし
        self.assertIsNone(r["rows"][0]["quote_section"])    # "zzz" は節キーでない＝None＋警告
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("quote_section='zzz'", r["warnings"][0])
        self.assertIsNone(r["error"])


def _sections(doc: str) -> dict:
    return json.loads((FIX / f"{doc}.sections.json").read_text(encoding="utf-8"))


def _ok_result(rows=None, warnings=None):
    return {"rows": rows if rows is not None else [{"segment": "A", "item": "x", "kpi_name": "k", "quote": "q"}],
            "warnings": warnings or [], "usage": {"input_tokens": 10, "output_tokens": 2}, "usage_ok": True,
            "error": None, "attempts": 1, "stop_reason": "end_turn"}


class ReadOneSkipTest(unittest.TestCase):
    """Codex Critical 8: スキップは同じ model・rows あり・error なしの時だけ。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.sec = _sections("S100TESTA")
        self.path = self.out / "9999" / "S100TESTA.read.json"
        self.path.parent.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _current_hash(self) -> str:
        import hashlib
        prompt = yr.build_prompt(self.sec, None, max_ir_chars=1000, ir_name=None)
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    def _write_prev(self, model: str, rows=None, error=None, sha="current"):
        rec = {"doc_id": "S100TESTA", "model": model, "rows": [{"item": "old"}] if rows is None else rows,
               "error": error}
        if sha == "current":
            rec["prompt_sha256"] = self._current_hash()
        elif sha is not None:
            rec["prompt_sha256"] = sha
        self.path.write_text(json.dumps(rec), encoding="utf-8")

    def _read(self, model="claude-sonnet-5", force=False):
        with mock.patch.object(yr, "call_claude", return_value=_ok_result()) as m:
            info = yr.read_one(self.sec, model, "sk-ant-test", FIX.parent, self.out, False, 1000, 100, force=force)
        return info, m.call_count

    def test_same_model_with_rows_skips(self):
        self._write_prev("claude-sonnet-5")
        info, calls = self._read()
        self.assertTrue(info.get("skipped"))
        self.assertEqual(calls, 0)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["rows"], [{"item": "old"}])

    def test_proxy_model_is_overwritten(self):
        self._write_prev("claude-sonnet (Claude Code subagent 代走・API 未使用)")
        info, calls = self._read()
        self.assertFalse(info.get("skipped"))
        self.assertEqual(calls, 1)
        rec = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(rec["model"], "claude-sonnet-5")
        self.assertEqual(rec["rows"][0]["item"], "x")
        self.assertEqual(len(rec["prompt_sha256"]), 64)
        self.assertEqual(rec["warnings"], [])
        self.assertIs(rec["usage_ok"], True)
        self.assertIsNone(rec["ir_source"])

    def test_prompt_hash_mismatch_or_missing_is_recalled(self):
        self._write_prev("claude-sonnet-5", sha="0" * 64)   # 入力が変わった（hash 不一致）→ 呼び直す
        info, calls = self._read()
        self.assertFalse(info.get("skipped"))
        self.assertEqual(calls, 1)
        self._write_prev("claude-sonnet-5", sha=None)       # hash の無い旧 read.json → 呼び直す
        info, calls = self._read()
        self.assertFalse(info.get("skipped"))
        self.assertEqual(calls, 1)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["prompt_sha256"], self._current_hash())

    def test_usage_ok_false_is_saved_as_false(self):
        bad = dict(_ok_result(rows=[]), usage_ok=False, error="attempt1: network x; attempt2: network x",
                   usage={"input_tokens": 0, "output_tokens": 0})
        with mock.patch.object(yr, "call_claude", return_value=bad):
            yr.read_one(self.sec, "claude-sonnet-5", "sk-ant-test", FIX.parent, self.out, False, 1000, 100)
        rec = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIs(rec["usage_ok"], False)
        self.assertIn("network", rec["error"])

    def test_error_or_empty_rows_is_recalled(self):
        self._write_prev("claude-sonnet-5", error="attempt1: http 429")
        self.assertEqual(self._read()[1], 1)
        self._write_prev("claude-sonnet-5", rows=[])
        self.assertEqual(self._read()[1], 1)
        self._write_prev("claude-sonnet-5")
        self.assertEqual(self._read(force=True)[1], 1)  # --force は同じ model でも呼び直す

    def test_prompt_sha256_matches_prompt(self):
        self._read()
        rec = json.loads(self.path.read_text(encoding="utf-8"))
        prompt = yr.build_prompt(self.sec, None, max_ir_chars=1000, ir_name=None)
        import hashlib
        self.assertEqual(rec["prompt_sha256"], hashlib.sha256(prompt.encode("utf-8")).hexdigest())
        self.assertEqual(rec["prompt_chars"], len(prompt))


class MainExitCodeTest(unittest.TestCase):
    """Codex Critical 7: 1 件でも error ありなら main は 1（read.json は保存したまま）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _main(self, side_effect):
        argv = ["--code", "9999", "--data-dir", str(FIX.parent), "--out-dir", str(self.out), "--model", "m-test"]
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}), \
                mock.patch.object(yr, "call_claude", side_effect=side_effect), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            return yr.main(argv)

    def test_one_error_returns_1_and_keeps_read_json(self):
        bad = dict(_ok_result(rows=[]), error="attempt1: http 500; attempt2: http 500")
        rc = self._main([_ok_result(), bad])  # period_end 降順: S100TESTA が先・S100TESTB が error
        self.assertEqual(rc, 1)
        rec_a = json.loads((self.out / "9999" / "S100TESTA.read.json").read_text(encoding="utf-8"))
        rec_b = json.loads((self.out / "9999" / "S100TESTB.read.json").read_text(encoding="utf-8"))
        self.assertIsNone(rec_a["error"])
        self.assertIn("http 500", rec_b["error"])
        self.assertEqual(rec_b["rows"], [])

    def test_all_ok_returns_0(self):
        self.assertEqual(self._main([_ok_result(), _ok_result()]), 0)

    def test_no_key_exits_1_without_writing(self):
        argv = ["--code", "9999", "--data-dir", str(FIX.parent), "--out-dir", str(self.out)]
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), \
                mock.patch.object(yr, "call_claude", side_effect=AssertionError("API を呼んだ")):
            with self.assertRaises(SystemExit) as cm:
                yr.main(argv)
        self.assertEqual(cm.exception.code, "ANTHROPIC_API_KEY が無い（docker-compose の environment で渡す）")
        self.assertFalse((self.out / "9999").exists())

    def test_dry_run_returns_0_without_key(self):
        argv = ["--code", "9999", "--data-dir", str(FIX.parent), "--out-dir", str(self.out), "--dry-run"]
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(yr.main(argv), 0)
        self.assertFalse((self.out / "9999").exists())


class CallClaudeTest(unittest.TestCase):
    def test_success_returns_rows_and_usage(self):
        good = _resp('[{"segment": "A", "item": "x", "kpi_name": "k", "expectation": false}]', 1234, 56)
        with mock.patch.object(yr.urllib.request, "urlopen", return_value=_FakeHTTP(good)) as m:
            r = yr.call_claude("p", "model-x", "sk-ant-test", max_tokens=10, retry_wait=0)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(r["usage"], {"input_tokens": 1234, "output_tokens": 56})
        self.assertIs(r["usage_ok"], True)
        self.assertEqual(r["rows"][0]["item"], "x")
        self.assertIsNone(r["error"])
        self.assertEqual(r["attempts"], 1)
        req = m.call_args[0][0]
        self.assertEqual(req.full_url, yr.API_URL)
        self.assertEqual(req.get_header("X-api-key"), "sk-ant-test")
        self.assertEqual(req.get_header("Anthropic-version"), yr.API_VERSION)
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual((body["model"], body["max_tokens"]), ("model-x", 10))
        self.assertEqual(body["messages"][0]["content"], "p")

    def test_bad_json_retries_once_then_records_error(self):
        bad = _resp("ごめんなさい、配列を書けません", 100, 10, stop="max_tokens")
        with mock.patch.object(yr.urllib.request, "urlopen", side_effect=[_FakeHTTP(bad), _FakeHTTP(bad)]) as m:
            r = yr.call_claude("p", "model-x", "sk-ant-test", retry_wait=0)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(r["rows"], [])
        self.assertEqual(r["usage"], {"input_tokens": 200, "output_tokens": 20})  # 2 回分を合算
        self.assertIn("no json array", r["error"])
        self.assertIn("max_tokens", r["error"])
        self.assertEqual(r["attempts"], 2)
        self.assertIs(r["usage_ok"], True)   # JSON 配列が取れなくても usage は返った＝消費は実測

    def test_non_json_body_is_recorded_and_unmeasured(self):
        # ゲートウェイの HTML 等・本文が JSON でない → JSONDecodeError を捕捉して error に残し usage_ok=False
        class _Raw:
            def __init__(self, body: bytes):
                self._body = body

            def read(self) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        raw = _Raw(b"<html>502 Bad Gateway sk-ant-test</html>")
        with mock.patch.object(yr.urllib.request, "urlopen", side_effect=[raw, _Raw(b"<html>again</html>")]) as m:
            r = yr.call_claude("p", "model-x", "sk-ant-test", retry_wait=0)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(r["rows"], [])
        self.assertIn("bad json body", r["error"])
        self.assertIs(r["usage_ok"], False)
        self.assertEqual(r["usage"], {"input_tokens": 0, "output_tokens": 0})
        self.assertEqual(r["attempts"], 2)

    def test_bad_then_good(self):
        bad = _resp("前置き", 100, 10)
        good = _resp("[]", 100, 5)
        with mock.patch.object(yr.urllib.request, "urlopen", side_effect=[_FakeHTTP(bad), _FakeHTTP(good)]):
            r = yr.call_claude("p", "model-x", "sk-ant-test", retry_wait=0)
        self.assertEqual(r["rows"], [])
        self.assertIsNone(r["error"])
        self.assertEqual(r["usage"]["input_tokens"], 200)

    def test_http_error_recorded_without_key_leak(self):
        err = urllib.error.HTTPError(yr.API_URL, 429, "rate", {}, io.BytesIO(b'{"error":"slow down sk-ant-abc123"}'))
        with mock.patch.object(yr.urllib.request, "urlopen", side_effect=[err, err]) as m:
            r = yr.call_claude("p", "model-x", "sk-ant-test", retry_wait=0)
        self.assertEqual(m.call_count, 2)
        self.assertEqual(r["rows"], [])
        self.assertIn("http 429", r["error"])
        self.assertNotIn("sk-ant-abc123", r["error"])
        self.assertEqual(r["usage"], {"input_tokens": 0, "output_tokens": 0})
        self.assertIs(r["usage_ok"], False)


class IrLookupTest(unittest.TestCase):
    def test_fy_label(self):
        self.assertEqual(yr.fy_label("2025-04-01", "2026-03-31"), 2025)
        self.assertEqual(yr.fy_label(None, "2026-03-31"), 2025)
        self.assertEqual(yr.fy_label(None, "2025-12-31"), 2025)
        self.assertIsNone(yr.fy_label(None, None))

    def test_find_ir_text(self):
        sec = json.loads((FIX / "S100TESTA.sections.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as td:
            ir = Path(td) / "9999" / "ir"
            ir.mkdir(parents=True)
            (ir / "FY2024_presentation.txt").write_text("old", encoding="utf-8")
            self.assertIsNone(yr.find_ir_text("9999", sec, Path(td)))
            (ir / "FY2025_presentation.txt").write_text("new", encoding="utf-8")
            self.assertEqual(yr.find_ir_text("9999", sec, Path(td)).name, "FY2025_presentation.txt")


if __name__ == "__main__":
    unittest.main()
