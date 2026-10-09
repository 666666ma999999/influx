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
(g) main の終了コード（Codex Critical 7）: 1 件でも error ありなら 1（read.json は保存したまま）・全部成功なら 0・鍵なしは SystemExit。
    targets.json が無ければ後の期は読まずにスキップする
(i) T9a（P-INF-37）: 基準期は最大 10 行・新しい列（geo／value_type／value_basis／status／target_id）／--make-targets／
    指名読み（not_disclosed は quote・数字なしで通る・未知の target_id は捨てる）／segment_figures の分離／
    read.json の submit_date・mode／--dump-prompts → --import-responses の往復
(j) T10（P-INF-38 c）: 指名プロンプトの当期の XBRL 区分名一覧／概数・範囲・下限・上限・兆億万の数値化（kpi_value_text・
    value_approx）／expect_quote・expect_section／--make-targets --extend／--renominate-base（基準期の指名読み直し）

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
        self.assertEqual(set(rows[0]), set(yr.ROW_KEYS) | set(yr.DERIVED_KEYS))   # T10c: 原文と概数の種類の列
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

    def test_base_more_than_ten_rows_truncated(self):
        # T9a ①: 基準期の上限は 3 → 10 行（MAX_ROWS_BASE）
        rows, warnings = yr.normalize_rows([{"item": f"i{n}", "kpi_name": "k"} for n in range(12)])
        self.assertEqual([r["item"] for r in rows], [f"i{n}" for n in range(10)])
        self.assertEqual(len(warnings), 1)
        self.assertIn("行数 12", warnings[0])
        self.assertIn("上限 10", warnings[0])
        rows, warnings = yr.normalize_rows([{"item": f"i{n}", "kpi_name": "k"} for n in range(10)])
        self.assertEqual((len(rows), warnings), (10, []))

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


TARGET_T01 = {"target_id": "T01", "segment": "Seasonings And Foods", "item": "海外調味料", "kpi_name": "販売数量",
              "geo": "海外", "value_type": "数量", "value_basis": "前年比"}
TARGET_T02 = {"target_id": "T02", "segment": "Healthcare And Others", "item": "アミノ酸", "kpi_name": "稼働率",
              "geo": "全体", "value_type": "稼働率", "value_basis": "水準"}


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

    def _write_targets(self):
        # 基準期= S100TESTB（period_end が古い）。後の期 S100TESTA を指名読みにするための targets.json
        path = self.out / "9999" / "targets.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"code4": "9999", "base_doc_id": "S100TESTB", "base_period_end": "2025-03-31",
                                    "targets": [dict(TARGET_T01)]}, ensure_ascii=False), encoding="utf-8")

    def test_one_error_returns_1_and_keeps_read_json(self):
        self._write_targets()
        bad = dict(_ok_result(rows=[]), error="attempt1: http 500; attempt2: http 500")
        rc = self._main([_ok_result(), bad])  # period_end 降順: S100TESTA（指名読み）が先・S100TESTB（基準期）が error
        self.assertEqual(rc, 1)
        rec_a = json.loads((self.out / "9999" / "S100TESTA.read.json").read_text(encoding="utf-8"))
        rec_b = json.loads((self.out / "9999" / "S100TESTB.read.json").read_text(encoding="utf-8"))
        self.assertIsNone(rec_a["error"])
        self.assertIn("http 500", rec_b["error"])
        self.assertEqual(rec_b["rows"], [])

    def test_all_ok_returns_0(self):
        self._write_targets()
        self.assertEqual(self._main([_ok_result(), _ok_result()]), 0)

    def test_without_targets_only_base_is_read(self):
        # targets.json が無い → 後の期（S100TESTA）は読まずにスキップ・基準期（S100TESTB）だけ呼ぶ
        out = io.StringIO()
        argv = ["--code", "9999", "--data-dir", str(FIX.parent), "--out-dir", str(self.out), "--model", "m-test"]
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}), \
                mock.patch.object(yr, "call_claude", side_effect=[_ok_result()]) as m, \
                mock.patch("sys.stdout", out):
            rc = yr.main(argv)
        self.assertEqual((rc, m.call_count), (0, 1))
        self.assertEqual(m.call_args.kwargs["mode"], "base")
        self.assertTrue((self.out / "9999" / "S100TESTB.read.json").exists())
        self.assertFalse((self.out / "9999" / "S100TESTA.read.json").exists())
        self.assertIn("skipped", out.getvalue())
        self.assertIn("S100TESTA", out.getvalue())

    def test_targets_from_other_base_is_refused(self):
        path = self.out / "9999" / "targets.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"base_doc_id": "S100OTHER", "targets": [dict(TARGET_T01)]}), encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            self._main([_ok_result(), _ok_result()])
        self.assertIn("base_doc_id", str(cm.exception.code))

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


class BaseModeTest(unittest.TestCase):
    """T9a ①: 基準期の型（最大 10 行・区分ごとに 1 行以上・数量等を金額より優先・status は new）。"""

    def test_base_prompt_rules_and_new_columns(self):
        p = yr.build_prompt(_sections("S100TESTB"), None)
        self.assertIn("最大 10 行", p)
        self.assertIn("報告セグメントごとに", p)
        self.assertIn("数量・単価・受注・稼働率 を 金額 より優先", p)
        self.assertIn('status は全行 "new"', p)
        for col in ("geo", "value_type", "value_basis", "status", "target_id"):
            self.assertIn(f"- {col}:", p)
        self.assertNotIn("追う品目（target）", p)

    def test_base_status_forced_new(self):
        rows, warnings = yr.normalize_rows([
            {"item": "a", "kpi_name": "k", "status": "found", "target_id": "T01", "value_type": "数量",
             "value_basis": "前年比", "geo": " 海外 "},
            {"item": "b", "kpi_name": "k", "value_type": "売上", "value_basis": "累計"},
        ])
        self.assertEqual([r["status"] for r in rows], ["new", "new"])
        self.assertIsNone(rows[0]["target_id"])
        self.assertEqual((rows[0]["geo"], rows[0]["value_type"], rows[0]["value_basis"]), ("海外", "数量", "前年比"))
        self.assertIsNone(rows[1]["value_type"])    # 7 種以外は None
        self.assertIsNone(rows[1]["value_basis"])   # 水準／前年比 以外は None
        self.assertEqual(len(warnings), 4)
        self.assertTrue(any("status='found'" in w for w in warnings), warnings)
        self.assertTrue(any("target_id='T01'" in w for w in warnings), warnings)


class MakeTargetsTest(unittest.TestCase):
    """T9a ②: --make-targets は基準期の read.json の（normalize_rows を通った）行から targets.json を作る。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        (self.out / "9999").mkdir()
        self.base_read = self.out / "9999" / "S100TESTB.read.json"   # 基準期= period_end が古い S100TESTB

    def tearDown(self):
        self.tmp.cleanup()

    def _write_base(self, rows, mode="base", error=None):
        self.base_read.write_text(json.dumps({"doc_id": "S100TESTB", "mode": mode, "rows": rows, "error": error},
                                             ensure_ascii=False), encoding="utf-8")

    def _make(self, force=False):
        with mock.patch("sys.stdout", io.StringIO()):
            return yr.make_targets("9999", FIX.parent, self.out, force=force)

    def test_targets_from_surviving_rows(self):
        self._write_base([
            dict(TARGET_T01, target_id=None, status="new", kpi_value="108%", quote="q1"),
            {"segment": "S", "item": "x", "kpi_name": None},                         # 必須列欠落 → 捨てる
            dict(TARGET_T01, target_id=None, status="new", kpi_value="109%"),       # 同じ品目 → 1 件にまとめる
            dict(TARGET_T02, target_id=None, status="new", value_type="不明な種類"),  # value_type は None で残る
        ])
        doc = self._make()
        on_disk = json.loads((self.out / "9999" / "targets.json").read_text(encoding="utf-8"))
        self.assertEqual(doc, on_disk)
        self.assertEqual((doc["code4"], doc["base_doc_id"], doc["base_period_end"]), ("9999", "S100TESTB", "2025-03-31"))
        self.assertEqual([t["target_id"] for t in doc["targets"]], ["T01", "T02"])
        self.assertEqual(doc["targets"][0], dict(TARGET_T01, base_rows=[0, 2]))   # 基準期 read.json の行番号
        self.assertEqual(set(doc["targets"][1]), {"target_id", "base_rows", *yr.TARGET_KEYS})
        self.assertEqual(doc["targets"][1]["base_rows"], [3])
        self.assertIsNone(doc["targets"][1]["value_type"])

    def test_refuse_when_base_read_missing(self):
        with self.assertRaises(SystemExit) as cm:
            self._make()
        self.assertIn("基準期の read.json が無い", str(cm.exception.code))
        self.assertFalse((self.out / "9999" / "targets.json").exists())

    def test_refuse_old_mode_error_and_overwrite(self):
        self._write_base([dict(TARGET_T01)], mode=None)            # T9 以前の読み
        with self.assertRaises(SystemExit) as cm:
            self._make()
        self.assertIn("基準期の型で読まれていない", str(cm.exception.code))
        self._write_base([], error="attempt1: http 500")
        with self.assertRaises(SystemExit):
            self._make()
        self._write_base([dict(TARGET_T01)])
        self._make()
        with self.assertRaises(SystemExit) as cm:                   # 既にある → --force なしは拒否
            self._make()
        self.assertIn("既にある", str(cm.exception.code))
        self.assertEqual(len(self._make(force=True)["targets"]), 1)

    def test_main_make_targets_exit_1_when_missing(self):
        argv = ["--code", "9999", "--data-dir", str(FIX.parent), "--out-dir", str(self.out), "--make-targets"]
        with self.assertRaises(SystemExit) as cm:
            yr.main(argv)
        self.assertIn("基準期の read.json が無い", str(cm.exception.code))   # 文字列の SystemExit＝終了コード 1


class NominatedModeTest(unittest.TestCase):
    """T9a ③: 指名読み（指名 1 件に 1 行・not_disclosed は数字と quote なしで通る・未知の target_id は捨てる）。"""

    targets = [dict(TARGET_T01), dict(TARGET_T02)]

    def test_prompt_lists_targets(self):
        p = yr.build_prompt(_sections("S100TESTA"), None, mode="nominated", targets=self.targets)
        self.assertIn("ちょうど 1 行", p)
        self.assertIn("最大 5 行", p)   # 指名 2 件＋new 3 行
        self.assertIn("- T01: segment=Seasonings And Foods／item=海外調味料／kpi_name=販売数量", p)
        self.assertIn("- T02:", p)
        self.assertIn("当期の報告セグメント名", p)
        self.assertIn('status="not_disclosed"', p)
        self.assertNotIn("最大 10 行", p)

    def test_not_disclosed_accepted_and_unknown_target_dropped(self):
        rows, warnings = yr.normalize_rows([
            {"target_id": "T01", "status": "found", "segment": "Seasonings & Foods（当期名）", "kpi_value": "108%",
             "kpi_unit": "%", "quote": "海外調味料の販売数量が前期比8%増"},
            {"target_id": "T02", "status": "not_disclosed", "kpi_value": 85, "quote": None},  # 数字・quote なし（数字は消す）
            {"target_id": "T99", "status": "found", "item": "謎", "kpi_name": "k"},          # 未知 → 捨てる
            {"target_id": "T01", "status": "found", "item": "海外調味料", "kpi_name": "販売数量"},  # 2 行目 → 捨てる
            {"target_id": None, "status": "found", "item": "z", "kpi_name": "k"},            # ID なし → 捨てる
            {"status": "maybe", "item": "z", "kpi_name": "k"},                               # status 不正 → 捨てる
        ], mode="nominated", targets=self.targets)
        self.assertEqual([(r["target_id"], r["status"]) for r in rows], [("T01", "found"), ("T02", "not_disclosed")])
        found, nd = rows
        self.assertEqual(found["segment"], "Seasonings & Foods（当期名）")   # 当期の区分名を残す
        self.assertEqual((found["item"], found["kpi_name"], found["geo"]), ("海外調味料", "販売数量", "海外"))  # target で埋める
        self.assertEqual((nd["item"], nd["kpi_name"], nd["segment"]), ("アミノ酸", "稼働率", "Healthcare And Others"))
        self.assertIsNone(nd["kpi_value"])
        self.assertIsNone(nd["quote"])
        self.assertTrue(any("not_disclosed の kpi_value=85" in w for w in warnings), warnings)
        self.assertTrue(any("target_id='T99' は指名に無い" in w for w in warnings), warnings)
        self.assertTrue(any("target_id='T01' の 2 行目" in w for w in warnings), warnings)
        self.assertTrue(any("target_id=None は指名に無い" in w for w in warnings), warnings)
        self.assertTrue(any("status='maybe'" in w for w in warnings), warnings)

    def test_new_rows_capped_and_missing_target_warned(self):
        rows, warnings = yr.normalize_rows(
            [{"target_id": "T01", "status": "found", "kpi_value": 1}]
            + [{"status": "new", "item": f"n{i}", "kpi_name": "k", "target_id": "T02" if i == 0 else None}
               for i in range(5)],
            mode="nominated", targets=self.targets)
        self.assertEqual([r["item"] for r in rows], ["海外調味料", "n0", "n1", "n2"])   # 上限= 指名 2＋new 3
        self.assertIsNone(rows[1]["target_id"])                                        # new の target_id は None
        self.assertTrue(any("new の行数 5" in w for w in warnings), warnings)
        self.assertTrue(any("指名 T02 の行が無い" in w for w in warnings), warnings)
        self.assertLessEqual(len(rows), yr.max_rows_for("nominated", self.targets))


class SegmentFiguresTest(unittest.TestCase):
    """T9a ④: facts.segments が空の会社だけ segment_figures を頼み、行は read.json の segment_figures に分ける。"""

    def _no_segments(self) -> dict:
        sec = _sections("S100TESTB")
        sec["facts"] = dict(sec.get("facts") or {}, segments={})
        sec["accounting"] = "US GAAP"
        return sec

    def test_prompt_asks_only_when_segments_empty(self):
        self.assertNotIn('"kind": "segment_figures"', yr.build_prompt(_sections("S100TESTB"), None))
        p = yr.build_prompt(self._no_segments(), None)
        self.assertIn('"kind": "segment_figures"', p)
        self.assertIn("計算・推定・合算はしない", p)

    def test_split_and_validate(self):
        raw = [
            {"item": "a", "kpi_name": "k"},
            {"kind": "segment_figures", "segment": "北米", "sales_cur": "1,234", "sales_prev": 1000,
             "profit_cur": "△56", "profit_prev": "好調", "profit_measure": "営業利益", "unit": "百万円",
             "quote": "北米 1,234 1,000 △56"},
            {"kind": "segment_figures", "segment": "欧州"},                 # 数字なし → 捨てる
            {"kind": "segment_figures", "sales_cur": 5},                    # segment なし → 捨てる
        ]
        rows, figures, warnings = yr.normalize_response(raw)
        self.assertEqual([r["item"] for r in rows], ["a"])                  # KPI の行に混ざらない
        self.assertEqual(figures, [{"segment": "北米", "sales_cur": 1234, "sales_prev": 1000, "profit_cur": -56,
                                    "profit_prev": None, "profit_measure": "営業利益", "unit": "百万円",
                                    "quote": "北米 1,234 1,000 △56"}])
        self.assertEqual(len(warnings), 3)
        self.assertTrue(any("profit_prev='好調'" in w for w in warnings), warnings)
        # 受け皿なしの normalize_rows は捨てて警告（KPI の行には入れない）
        rows2, w2 = yr.normalize_rows(raw[:2])
        self.assertEqual(len(rows2), 1)
        self.assertTrue(any("受け皿が無い" in w for w in w2), w2)

    def test_call_claude_and_read_json_carry_figures(self):
        text = json.dumps([{"item": "a", "kpi_name": "k"},
                           {"kind": "segment_figures", "segment": "北米", "sales_cur": 10}], ensure_ascii=False)
        with mock.patch.object(yr.urllib.request, "urlopen", return_value=_FakeHTTP(_resp(text))):
            r = yr.call_claude("p", "model-x", "sk-ant-test", retry_wait=0)
        self.assertEqual(len(r["rows"]), 1)
        self.assertEqual(r["segment_figures"][0]["sales_cur"], 10)
        with tempfile.TemporaryDirectory() as td, mock.patch.object(yr, "call_claude", return_value=r), \
                mock.patch("sys.stdout", io.StringIO()):
            yr.read_one(self._no_segments(), "m", "sk-ant-test", FIX.parent, Path(td), False, 1000, 100)
            rec = json.loads((Path(td) / "9999" / "S100TESTB.read.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["segment_figures"][0]["segment"], "北米")
        self.assertEqual(len(rec["rows"]), 1)


class ReadJsonMetaTest(unittest.TestCase):
    """T9a ⑤前半: read.json の先頭に submit_date（sections.json の提出日）と mode。"""

    def test_submit_date_and_mode(self):
        with tempfile.TemporaryDirectory() as td, mock.patch("sys.stdout", io.StringIO()):
            out = Path(td)
            with mock.patch.object(yr, "call_claude", return_value=_ok_result()):
                yr.read_one(_sections("S100TESTB"), "m", "sk-ant-test", FIX.parent, out, False, 1000, 100)
            with mock.patch.object(yr, "call_claude", return_value=_ok_result()) as m:
                yr.read_one(_sections("S100TESTA"), "m", "sk-ant-test", FIX.parent, out, False, 1000, 100,
                            mode="nominated", targets=[dict(TARGET_T01)])
            self.assertEqual(m.call_args.kwargs["targets"], [TARGET_T01])
            base = json.loads((out / "9999" / "S100TESTB.read.json").read_text(encoding="utf-8"))
            nom = json.loads((out / "9999" / "S100TESTA.read.json").read_text(encoding="utf-8"))
        self.assertEqual((base["submit_date"], base["mode"]), ("2025-06-18 09:00", "base"))
        self.assertEqual((nom["submit_date"], nom["mode"]), ("2026-06-12 09:00", "nominated"))
        self.assertEqual(base["segment_figures"], [])
        self.assertEqual(list(base)[:5], ["doc_id", "code4", "period_end", "submit_date", "mode"])

    def test_base_doc_is_oldest_period(self):
        self.assertEqual(yr.base_doc_id(yr.load_sections("9999", FIX.parent)), "S100TESTB")
        self.assertIsNone(yr.base_doc_id([]))


class DumpImportRoundTripTest(unittest.TestCase):
    """T9a ⑤: --dump-prompts → （代走が response.json を書く）→ --import-responses の往復。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.pdir = self.out / "9999" / "prompts"

    def tearDown(self):
        self.tmp.cleanup()

    def _cli(self, *flags) -> int:
        argv = ["--code", "9999", "--data-dir", str(FIX.parent), "--out-dir", str(self.out), *flags]
        with mock.patch.object(yr, "call_claude", side_effect=AssertionError("API を呼んだ")), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            return yr.main(argv)

    def _read(self, doc_id: str) -> dict:
        return json.loads((self.out / "9999" / f"{doc_id}.read.json").read_text(encoding="utf-8"))

    def test_round_trip(self):
        # 1) targets.json なし → 基準期だけ書き、後の期はスキップ
        with mock.patch("sys.stdout", io.StringIO()) as so:
            res = yr.dump_prompts("9999", FIX.parent, self.out)
        self.assertEqual(res, {"written": ["S100TESTB"], "skipped": ["S100TESTA"]})
        self.assertIn("S100TESTA", so.getvalue())
        base_prompt = (self.pdir / "S100TESTB.prompt.txt").read_text(encoding="utf-8")
        self.assertIn("最大 10 行", base_prompt)
        self.assertEqual(base_prompt, yr.build_prompt(_sections("S100TESTB"), None))   # API 経路と同じプロンプト

        # 2) 代走の応答（コードフェンスつき）を取り込む
        (self.pdir / "S100TESTB.response.json").write_text("```json\n" + json.dumps(
            [dict(TARGET_T01, target_id=None, status="new", kpi_value="108%", quote="q")], ensure_ascii=False)
            + "\n```", encoding="utf-8")
        self.assertEqual(self._cli("--import-responses"), 0)
        rec = self._read("S100TESTB")
        self.assertEqual(rec["model"], yr.PROXY_MODEL)
        self.assertIn("代走", rec["model"])
        self.assertEqual(rec["usage"], {"input_tokens": 0, "output_tokens": 0})
        self.assertIs(rec["usage_ok"], False)
        self.assertEqual((rec["mode"], rec["submit_date"], rec["stop_reason"]), ("base", "2025-06-18 09:00", "subagent"))
        import hashlib
        self.assertEqual(rec["prompt_sha256"], hashlib.sha256(base_prompt.encode("utf-8")).hexdigest())
        self.assertEqual(rec["warnings"], [])
        self.assertEqual(rec["rows"][0]["status"], "new")
        self.assertFalse((self.out / "9999" / "S100TESTA.read.json").exists())   # 応答なし＝書かない

        # 3) targets → 後の期の指名読みプロンプト → 応答を取り込む
        self.assertEqual(self._cli("--make-targets"), 0)
        self.assertEqual(self._cli("--dump-prompts"), 0)
        nom_prompt = (self.pdir / "S100TESTA.prompt.txt").read_text(encoding="utf-8")
        self.assertIn("- T01: segment=Seasonings And Foods", nom_prompt)
        (self.pdir / "S100TESTA.response.json").write_text(json.dumps(
            [{"target_id": "T01", "status": "not_disclosed", "segment": "Seasonings And Foods"},
             {"target_id": "T42", "status": "found", "item": "x", "kpi_name": "k"}], ensure_ascii=False),
            encoding="utf-8")
        self.assertEqual(self._cli("--import-responses"), 0)
        nom = self._read("S100TESTA")
        self.assertEqual(nom["mode"], "nominated")
        self.assertEqual([(r["target_id"], r["status"]) for r in nom["rows"]], [("T01", "not_disclosed")])
        self.assertTrue(any("T42" in w for w in nom["warnings"]), nom["warnings"])

        # 4) prompt.txt が今の入力と違う → warnings に残す・JSON の無い応答は error で終了コード 1
        (self.pdir / "S100TESTA.prompt.txt").write_text("古いプロンプト", encoding="utf-8")
        (self.pdir / "S100TESTA.response.json").write_text("読めませんでした", encoding="utf-8")
        self.assertEqual(self._cli("--import-responses"), 1)
        nom = self._read("S100TESTA")
        self.assertEqual(nom["error"], "response に JSON 配列が無い")
        self.assertTrue(any("今の入力から作るプロンプトと違う" in w for w in nom["warnings"]), nom["warnings"])

    def test_import_without_targets_is_error(self):
        self.pdir.mkdir(parents=True)
        (self.pdir / "S100TESTA.response.json").write_text("[]", encoding="utf-8")   # 後の期の応答だけある
        self.assertEqual(self._cli("--import-responses"), 1)
        self.assertFalse((self.out / "9999" / "S100TESTA.read.json").exists())



class CodexT9FixesReadTest(unittest.TestCase):
    """Codex T9 実装レビューの回帰（読み側）: Critical 2（targets の行番号）・3（単一セグメントの宣言）・4（期待の根拠節）。"""

    def test_c2_targets_differing_only_by_segment_keep_own_base_rows(self):
        with tempfile.TemporaryDirectory() as td, mock.patch("sys.stdout", io.StringIO()):
            out = Path(td)
            (out / "9999").mkdir()
            rows = [dict(TARGET_T01, target_id=None, status="new"),
                    dict(TARGET_T01, target_id=None, status="new", segment="Healthcare And Others")]
            (out / "9999" / "S100TESTB.read.json").write_text(
                json.dumps({"doc_id": "S100TESTB", "mode": "base", "rows": rows, "error": None}, ensure_ascii=False),
                encoding="utf-8")
            doc = yr.make_targets("9999", FIX.parent, out)
        self.assertEqual([(t["target_id"], t["segment"], t["base_rows"]) for t in doc["targets"]],
                         [("T01", "Seasonings And Foods", [0]), ("T02", "Healthcare And Others", [1])])

    def test_c3_single_segment_declaration_needs_quote(self):
        sec = _sections("S100TESTB")
        sec["facts"] = dict(sec.get("facts") or {}, segments={})
        p = yr.build_prompt(sec, None)
        self.assertIn('"single_segment": true', p)
        self.assertIn("区分の表が無いことから単一と推定しない", p)
        raw = [{"kind": "segment_figures", "single_segment": True, "quote": "当社グループは単一セグメントであります。"},
               {"kind": "segment_figures", "single_segment": True}]        # quote なし → 捨てる
        rows, figures, warnings = yr.normalize_response(raw)
        self.assertEqual(rows, [])
        self.assertEqual(figures, [{"single_segment": True, "segment": None,
                                    "quote": "当社グループは単一セグメントであります。"}])
        self.assertTrue(any("quote が無い" in w for w in warnings), warnings)
        rec = yr._record(sec, "base", "m", {"rows": rows, "segment_figures": figures, "warnings": warnings,
                                             "usage": {}, "error": None, "attempts": 1, "stop_reason": "x"},
                         1, None, None)
        self.assertIs(rec["single_segment"], True)
        rec2 = yr._record(sec, "base", "m", {"rows": [], "segment_figures": [], "usage": {}, "error": None,
                                              "attempts": 1, "stop_reason": "x"}, 1, None, None)
        self.assertIs(rec2["single_segment"], False)

    def test_c4_prompt_allows_mdna_expectation_and_judge_accepts_it(self):
        # 実プロンプトの「期待」の規則に MD&A（と、資料を付けた時は説明会資料）が入っている
        p = yr.build_prompt(_sections("S100TESTA"), None)
        rule = next(line for line in p.splitlines() if line.startswith("- expectation:"))
        self.assertIn("経営者による分析（mdna）", rule)
        self.assertIn("経営方針（business_policy）", rule)
        self.assertNotIn("有報の経営方針で", rule)          # 旧規則（経営方針だけ）が残っていない
        self.assertNotIn("経営方針の節で成長事業", p)
        self.assertIn("決算説明会資料（ir_presentation）",
                      next(line for line in yr.build_prompt(_sections("S100TESTA"), "資料").splitlines()
                           if line.startswith("- expectation:")))
        # その規則で返った MD&A 由来の期待行を、読みの正規化 → yuho_judge の判定まで通す
        import yuho_judge as yj
        sec = _sections("S100TESTA")
        sec["sections"]["mdna"] = dict(sec["sections"]["mdna"], text="海外調味料を成長事業として拡大する。")
        raw = [{"segment": "調味料・食品", "item": "海外調味料", "kpi_name": "販売数量", "kpi_value": None,
                "quote": "海外調味料を成長事業として拡大する。", "quote_section": "mdna", "expectation": True,
                "status": "new"}]
        rows, _, _ = yr.normalize_response(raw)
        self.assertIs(rows[0]["expectation"], True)
        per = yj.build_products([sec], {sec["doc_id"]: {"doc_id": sec["doc_id"], "rows": rows}})["products"][0]["periods"][0]
        self.assertEqual((per["expectation"], per["expect_basis"], per["label"]),
                         (True, "MD&Aの本文に品目名つきの引用あり", "期待"))


class T10ReadTest(unittest.TestCase):
    """T10（2026-10-08 P-INF-38 c）読み側: a2 区分名の一覧／c 概数・範囲／d 期待の別欄／e --extend／基準期の指名読み直し。"""

    targets = [dict(TARGET_T01), dict(TARGET_T02)]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # 3 期の data_dir: S100TESTB（2025-03 基準期）・S100TESTA（2026-03 真ん中）・S100TESTC（2027-03 最後）
        self.data = self.root / "data"
        (self.data / "9999").mkdir(parents=True)
        for d in ("S100TESTA", "S100TESTB"):
            (self.data / "9999" / f"{d}.sections.json").write_text(
                (FIX / f"{d}.sections.json").read_text(encoding="utf-8"), encoding="utf-8")
        c = _sections("S100TESTA")
        c.update(doc_id="S100TESTC", period_start="2026-04-01", period_end="2027-03-31", submit_date="2027-06-15 09:00")
        (self.data / "9999" / "S100TESTC.sections.json").write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        self.out = self.root / "out"
        (self.out / "9999").mkdir(parents=True)
        self.pdir = self.out / "9999" / "prompts"

    def tearDown(self):
        self.tmp.cleanup()

    def _cli(self, *flags) -> int:
        argv = ["--code", "9999", "--data-dir", str(self.data), "--out-dir", str(self.out), *flags]
        with mock.patch.object(yr, "call_claude", side_effect=AssertionError("API を呼んだ")), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            return yr.main(argv)

    def _write(self, doc_id, mode, rows):
        (self.out / "9999" / f"{doc_id}.read.json").write_text(json.dumps(
            {"doc_id": doc_id, "mode": mode, "rows": rows, "error": None}, ensure_ascii=False), encoding="utf-8")

    def _targets(self) -> dict:
        return json.loads((self.out / "9999" / "targets.json").read_text(encoding="utf-8"))

    def _base_and_targets(self):
        self._write("S100TESTB", "base", [dict(TARGET_T01, target_id=None, status="new")])
        self.assertEqual(self._cli("--make-targets"), 0)

    # ---- a2
    def test_a2_nominated_prompt_lists_current_xbrl_segments(self):
        p = yr.build_prompt(_sections("S100TESTA"), None, mode="nominated", targets=self.targets)
        rule = next(line for line in p.splitlines() if line.startswith("- segment は当期の報告セグメント名"))
        self.assertIn("「調味料・食品／ヘルスケア等」の 2 つ", rule)
        self.assertIn("この中のどれか 1 つ", rule)
        self.assertIn("どれにも当てはまらなければ null", rule)
        self.assertIn("target の区分名をそのまま写さない", rule)
        sec = _sections("S100TESTA")
        sec["facts"] = dict(sec["facts"], segments={})
        rule = next(line for line in yr.build_prompt(sec, None, mode="nominated", targets=self.targets).splitlines()
                    if line.startswith("- segment は当期の報告セグメント名"))
        self.assertIn("本文のセグメント情報の区分名", rule)
        self.assertNotIn("XBRL の区分名は「", yr.build_prompt(_sections("S100TESTA"), None))   # 基準期の型には載せない

    # ---- c
    def test_c_parse_kpi_value_cases(self):
        cases = {
            ("3～5%", "%"): (4, "範囲"), ("3~5", "%"): (4, "範囲"), ("3-5", "%"): (4, "範囲"),
            ("3～5兆円", "億円"): (40000, "範囲"),
            ("約3,000", "トン"): (3000, "概数"), ("3,000程度", "トン"): (3000, "概数"),
            ("100前後", "台"): (100, "概数"), ("およそ50", "%"): (50, "概数"),
            ("100万トン超", "万トン"): (100, "下限"), ("30%以上", "%"): (30, "下限"),
            ("3000億円を超える", "億円"): (3000, "下限"), ("50強", "%"): (50, "下限"),
            ("20弱", "%"): (20, "上限"), ("10未満", "%"): (10, "上限"), ("5以下", "%"): (5, "上限"),
            ("3兆210億円", "億円"): (30210, None), ("3兆210億円", "百万円"): (3021000, None),
            ("3兆210億円", "円"): (3021000000000, None), ("約1.2兆円", "億円"): (12000, "概数"),
            ("3兆210億円", "トン"): (None, None),          # 円の単位でなければ換算しない
            ("1.5億台", "台"): (None, None),
            ("70米ドル台中盤", "米ドル/バレル"): (None, None), ("好調", None): (None, None),
            ("1,234", "百万円"): (1234, None), ("106%", "%"): (106, None), (105, None): (105, None),
            ("120万トン", "万トン"): (120, None), (None, "%"): (None, None), (True, "%"): (None, None),
            ("13米ドル弱", "米ドル/百万Btu"): (13, "上限"),          # 8058 の実例（単位の頭が一致）
            # Codex T10 R1 Warning 4: 負数（-・全角－・▲・△）の範囲・複合金額
            ("-5～-3", "%"): (-4, "範囲"), ("－5～－3", "%"): (-4, "範囲"), ("▲5～▲3", "%"): (-4, "範囲"),
            ("-5~3", "%"): (-1, "範囲"), ("約-3兆円", "億円"): (-30000, "概数"), ("▲0.8", "%"): (-0.8, None),
            ("△1,234", "百万円"): (-1234, None), ("▲3兆210億円", "億円"): (-30210, None),
            # Codex T10 R2 Warning: 正の符号（+・全角＋・全角％）
            ("+3～+5%", "%"): (4, "範囲"), ("＋3～＋5％", "%"): (4, "範囲"), ("約+3兆円", "億円"): (30000, "概数"),
            ("+3.2", "%"): (3.2, None), ("＋3.2", "%"): (3.2, None), ("+3-5", "%"): (4, "範囲"),
            ("3兆210億円", None): (None, None),                      # 6501 の実例: kpi_unit が null＝換算先が決まらない
        }
        for (v, u), want in cases.items():
            self.assertEqual(yr.parse_kpi_value(v, u), want, (v, u))

    def test_c_normalize_keeps_text_and_approx(self):
        rows, warnings = yr.normalize_rows([
            {"item": "a", "kpi_name": "k", "kpi_value": "約3,000", "kpi_unit": "トン", "kpi_prev_value": "2,800"},
            {"item": "b", "kpi_name": "k", "kpi_value": "3兆210億円", "kpi_unit": "億円", "kpi_prev_value": "3～5兆円"},
            {"item": "c", "kpi_name": "k", "kpi_value": "70米ドル台中盤", "kpi_unit": "米ドル", "kpi_prev_value": 65},
            {"item": "d", "kpi_name": "k", "kpi_value": 105, "kpi_prev_value": " 106% "},
        ])
        a, b, c, d = rows
        self.assertEqual((a["kpi_value"], a["value_approx"], a["kpi_value_text"]), (3000, "概数", "約3,000"))
        self.assertEqual((a["kpi_prev_value"], a["prev_value_approx"], a["kpi_prev_value_text"]), ("2,800", None, "2,800"))
        self.assertEqual((b["kpi_value"], b["value_approx"], b["kpi_value_text"]), (30210, None, "3兆210億円"))
        self.assertEqual((b["kpi_prev_value"], b["prev_value_approx"]), (40000, "範囲"))
        self.assertEqual((c["kpi_value"], c["value_approx"], c["kpi_value_text"]), (None, None, "70米ドル台中盤"))
        self.assertEqual((c["kpi_prev_value"], c["kpi_prev_value_text"]), (65, None))
        self.assertEqual((d["kpi_value"], d["value_approx"], d["kpi_value_text"]), (105, None, None))
        self.assertEqual((d["kpi_prev_value"], d["kpi_prev_value_text"]), ("106%", "106%"))   # 素の数値は従来どおり
        self.assertEqual(len(warnings), 1)
        self.assertIn("70米ドル台中盤", warnings[0])
        self.assertIn("kpi_value_text に残す", warnings[0])
        # not_disclosed は原文も消す（数字を補わない）
        rows, _ = yr.normalize_rows([{"target_id": "T01", "status": "not_disclosed", "kpi_value": "約5"}],
                                    mode="nominated", targets=self.targets)
        self.assertEqual((rows[0]["kpi_value"], rows[0]["kpi_value_text"], rows[0]["value_approx"]), (None, None, None))

    # ---- d
    def test_d_expect_quote_prompt_and_pass_through(self):
        p = yr.build_prompt(_sections("S100TESTA"), None)
        for col in ("expect_quote", "expect_section"):
            self.assertIn(f"- {col}:", p)
        self.assertIn("expectation=true の時は必須", p)
        unit_rule = next(l for l in p.splitlines() if l.startswith("- kpi_unit:"))   # 単位語つきの値は kpi_unit 必須
        self.assertIn("必ず書く", unit_rule)
        self.assertIn('kpi_value "3兆210億円" → kpi_unit "億円"', unit_rule)
        self.assertIn("数字（kpi_value）の根拠", next(l for l in p.splitlines() if l.startswith("- quote:")))
        self.assertIn("expect_quote にその文を写し", next(l for l in p.splitlines() if l.startswith("- expectation:")))
        rows, warnings = yr.normalize_rows([
            {"item": "a", "kpi_name": "k", "expectation": True, "quote": "数量 105%", "quote_section": "mdna",
             "expect_quote": " a を成長の柱とする ", "expect_section": "business_policy"},
            {"item": "b", "kpi_name": "k", "expectation": True, "expect_quote": "x", "expect_section": "zzz"},
            {"item": "c", "kpi_name": "k", "expectation": True, "quote": "旧形式"},   # expect_quote なしも捨てない
        ])
        self.assertEqual((rows[0]["expect_quote"], rows[0]["expect_section"]), ("a を成長の柱とする", "business_policy"))
        self.assertIsNone(rows[1]["expect_section"])
        self.assertEqual((rows[2]["expect_quote"], rows[2]["quote"]), (None, "旧形式"))
        self.assertEqual(len(warnings), 1)
        self.assertIn("expect_section='zzz'", warnings[0])
        # 代走の取り込みでも通る（expect_* と kpi_value_text が read.json に残る）
        self._write("S100TESTB", "base", [])
        self.pdir.mkdir(parents=True)
        self.assertEqual(self._cli("--dump-prompts"), 0)
        (self.pdir / "S100TESTB.response.json").write_text(json.dumps([
            {"item": "a", "kpi_name": "k", "kpi_value": "約10", "expectation": True, "quote": "q", "quote_section": "mdna",
             "expect_quote": "a を拡大", "expect_section": "mdna"}], ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self._cli("--import-responses"), 0)
        row = json.loads((self.out / "9999" / "S100TESTB.read.json").read_text(encoding="utf-8"))["rows"][0]
        self.assertEqual((row["expect_quote"], row["expect_section"], row["kpi_value"], row["kpi_value_text"],
                          row["value_approx"]), ("a を拡大", "mdna", 10, "約10", "概数"))

    # ---- e
    def test_e_extend_targets_from_middle_period(self):
        self._base_and_targets()
        self._write("S100TESTA", "nominated", [
            dict(TARGET_T01, status="found", kpi_value=1),
            {"segment": "Healthcare And Others", "item": "新製品", "kpi_name": "出荷台数", "geo": "国内",
             "value_type": "数量", "value_basis": "水準", "status": "new", "target_id": None},
            dict(TARGET_T01, target_id=None, status="new"),                         # 既存 T01 と同じ品目 → 足さない
            {"item": None, "kpi_name": "k", "status": "new"},                        # item なし → 足さない
        ])
        self.assertEqual(self._cli("--make-targets", "--extend"), 0)
        doc = self._targets()
        self.assertEqual([t["target_id"] for t in doc["targets"]], ["T01", "T02"])
        t2 = doc["targets"][1]
        self.assertEqual((t2["item"], t2["added_from"], t2["added_period_end"], t2["added_rows"]),
                         ("新製品", "S100TESTA", "2026-03-31", [1]))
        self.assertEqual(doc["base_doc_id"], "S100TESTB")
        # 2 回目は何も足さない（冪等）
        self.assertEqual(self._cli("--make-targets", "--extend"), 0)
        self.assertEqual(len(self._targets()["targets"]), 2)
        # 最後の期は T02 も指名・真ん中の期と基準期は指名しない（真ん中の期のプロンプトは足す前と同じ）
        self.assertEqual(self._cli("--dump-prompts"), 0)
        last = (self.pdir / "S100TESTC.prompt.txt").read_text(encoding="utf-8")
        mid = (self.pdir / "S100TESTA.prompt.txt").read_text(encoding="utf-8")
        self.assertIn("- T02: segment=Healthcare And Others／item=新製品", last)
        self.assertIn("最大 5 行", last)
        self.assertNotIn("- T02:", mid)
        self.assertEqual(mid, yr.build_prompt(_sections("S100TESTA"), None, mode="nominated",
                                              targets=[t for t in doc["targets"] if not t.get("added_from")]))
        # 最後の期の応答で T02 を found／not_disclosed で取り込める
        (self.pdir / "S100TESTC.response.json").write_text(json.dumps(
            [{"target_id": "T01", "status": "found", "kpi_value": 2},
             {"target_id": "T02", "status": "not_disclosed"}], ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self._cli("--import-responses"), 0)
        rec = json.loads((self.out / "9999" / "S100TESTC.read.json").read_text(encoding="utf-8"))
        self.assertEqual([(r["target_id"], r["status"]) for r in rec["rows"]], [("T01", "found"), ("T02", "not_disclosed")])
        self.assertEqual(rec["warnings"], [])

    def test_e_extend_rerun_with_reordered_rows_rebuilds_mapping(self):
        # Codex T10 R1 Critical 3: 真ん中の期を読み直して行順が変わっても、行番号は品目（6 列）で作り直す
        self._base_and_targets()
        a = {"segment": "Healthcare And Others", "item": "A品", "kpi_name": "数量", "geo": None, "value_type": "数量",
             "value_basis": "水準", "status": "new"}
        b = dict(a, item="B品")
        found = dict(TARGET_T01, status="found", kpi_value=1)
        self._write("S100TESTA", "nominated", [found, a, b])
        self.assertEqual(self._cli("--make-targets", "--extend"), 0)
        self.assertEqual([(t["target_id"], t["item"], t.get("added_rows")) for t in self._targets()["targets"]],
                         [("T01", "海外調味料", None), ("T02", "A品", [1]), ("T03", "B品", [2])])
        self._write("S100TESTA", "nominated", [b, found, a])          # 行順が入れ替わった
        self.assertEqual(self._cli("--make-targets", "--extend"), 0)
        tg = self._targets()["targets"]
        self.assertEqual([(t["target_id"], t["item"], t.get("added_rows")) for t in tg],
                         [("T01", "海外調味料", None), ("T02", "A品", [2]), ("T03", "B品", [0])])
        import yuho_judge as yj
        self.assertEqual(yj._added_row_maps({"targets": tg}), {"S100TESTA": {2: "T02", 0: "T03"}})
        # 品目が読み直しで消えた → added_rows は空（古い番号を残さない）
        self._write("S100TESTA", "nominated", [found, b])
        self.assertEqual(self._cli("--make-targets", "--extend"), 0)
        tg = {t["target_id"]: t for t in self._targets()["targets"]}
        self.assertEqual((tg["T02"]["added_rows"], tg["T03"]["added_rows"]), ([], [1]))
        # 同じ品目の new 行が 2 行＝1 対 1 に結べない → 終了コード 1 相当（SystemExit）で targets.json は変えない
        before = (self.out / "9999" / "targets.json").read_text(encoding="utf-8")
        self._write("S100TESTA", "nominated", [found, a, dict(a)])
        with mock.patch("sys.stdout", io.StringIO()), self.assertRaises(SystemExit) as cm:
            yr.extend_targets("9999", self.data, self.out)
        self.assertIn("1 対 1 に結べない", str(cm.exception))
        self.assertEqual((self.out / "9999" / "targets.json").read_text(encoding="utf-8"), before)

    def test_e_extend_refusals(self):
        with mock.patch("sys.stdout", io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                yr.extend_targets("9999", self.data, self.out)            # targets.json が無い
            self.assertIn("targets.json が要る", str(cm.exception))
            self._base_and_targets()
            with self.assertRaises(SystemExit) as cm:
                yr.extend_targets("9999", self.data, self.out)            # 真ん中の期の read.json が無い
            self.assertIn("真ん中の期の read.json が無い", str(cm.exception))
            self._write("S100TESTA", "base", [])
            with self.assertRaises(SystemExit):
                yr.extend_targets("9999", self.data, self.out)            # 指名読みでない
            (self.data / "9999" / "S100TESTC.sections.json").unlink()
            with self.assertRaises(SystemExit) as cm:
                yr.extend_targets("9999", self.data, self.out)            # 3 期に満たない
            self.assertIn("3 期", str(cm.exception))
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as cm:
            yr.main(["--code", "9999", "--extend"])                      # --make-targets なし
        self.assertEqual(cm.exception.code, 2)

    # ---- 基準期の指名読み直し（T10g）
    def test_renominate_base_dump_and_import(self):
        self._base_and_targets()
        # 足した品目があっても基準期には指名しない
        doc = self._targets()
        doc["targets"].append({"target_id": "T02", "segment": "x", "item": "新製品", "kpi_name": "k", "geo": None,
                               "value_type": None, "value_basis": None, "added_from": "S100TESTA",
                               "added_period_end": "2026-03-31", "added_rows": [1]})
        (self.out / "9999" / "targets.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self._cli("--dump-prompts"), 0)                  # フラグなし＝従来どおり基準期の型
        old_base_prompt = (self.pdir / "S100TESTB.prompt.txt").read_text(encoding="utf-8")
        self.assertIn("最大 10 行", old_base_prompt)
        (self.pdir / "S100TESTA.prompt.txt").unlink()
        self.assertEqual(self._cli("--dump-prompts", "--renominate-base"), 0)
        p = (self.pdir / "S100TESTB.prompt.txt").read_text(encoding="utf-8")
        self.assertIn("指名読み", p)
        self.assertIn("- T01: segment=Seasonings And Foods", p)
        self.assertNotIn("- T02:", p)
        self.assertIn('status="new" の行は 0 行', p)
        self.assertIn("最大 1 行", p)
        self.assertFalse((self.pdir / "S100TESTA.prompt.txt").exists())   # 他の期は書かない
        (self.pdir / "S100TESTB.response.json").write_text(json.dumps(
            [{"target_id": "T01", "status": "found", "kpi_value": "約108", "segment": "Seasonings And Foods"},
             {"status": "new", "item": "余計", "kpi_name": "k"}], ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self._cli("--import-responses", "--renominate-base"), 0)
        rec = json.loads((self.out / "9999" / "S100TESTB.read.json").read_text(encoding="utf-8"))
        self.assertEqual((rec["mode"], rec.get("renominated_base")), ("nominated", True))
        self.assertEqual([(r["target_id"], r["status"], r["kpi_value"]) for r in rec["rows"]], [("T01", "found", 108)])
        self.assertTrue(any("new の行数 1 が上限 0" in w for w in rec["warnings"]), rec["warnings"])
        self.assertFalse(any("今の入力から作るプロンプトと違う" in w for w in rec["warnings"]), rec["warnings"])
        # フラグなしの取り込みは基準期の型（従来どおり・プロンプト違いの警告が出る）
        self.assertEqual(self._cli("--import-responses"), 0)
        rec = json.loads((self.out / "9999" / "S100TESTB.read.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["mode"], "base")
        self.assertNotIn("renominated_base", rec)
        self.assertTrue(any("今の入力から作るプロンプトと違う" in w for w in rec["warnings"]), rec["warnings"])

    def test_renominate_base_refusals(self):
        with mock.patch("sys.stdout", io.StringIO()), self.assertRaises(SystemExit) as cm:
            yr.dump_prompts("9999", self.data, self.out, renominate_base=True)   # targets.json が無い
        self.assertIn("targets.json が要る", str(cm.exception))
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as cm:
            yr.main(["--code", "9999", "--make-targets", "--renominate-base"])
        self.assertEqual(cm.exception.code, 2)

    # ---- T11 5 期化: 基準期より前の期だけを、今の targets.json のまま指名読み（--older-than-base）
    def test_older_than_base_dump_and_import(self):
        self._base_and_targets()
        doc = self._targets()
        doc["targets"].append({"target_id": "T02", "segment": "x", "item": "新製品", "kpi_name": "k", "geo": None,
                               "value_type": None, "value_basis": None, "added_from": "S100TESTA",
                               "added_period_end": "2026-03-31", "added_rows": [1]})
        (self.out / "9999" / "targets.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        z = _sections("S100TESTB")
        z.update(doc_id="S100TESTZ", period_start="2023-04-01", period_end="2024-03-31", submit_date="2024-06-15 09:00")
        (self.data / "9999" / "S100TESTZ.sections.json").write_text(json.dumps(z, ensure_ascii=False), encoding="utf-8")
        base_read = (self.out / "9999" / "S100TESTB.read.json").read_text(encoding="utf-8")
        with mock.patch("sys.stdout", io.StringIO()), self.assertRaises(SystemExit) as cm:
            yr.dump_prompts("9999", self.data, self.out)                  # フラグなし＝基準期がずれて止まる（従来どおり）
        self.assertIn("今の基準期 'S100TESTZ' と違う", str(cm.exception))
        self.assertEqual(self._cli("--dump-prompts", "--older-than-base"), 0)
        self.assertEqual(sorted(x.name for x in self.pdir.iterdir()), ["S100TESTZ.prompt.txt"])   # 古い期だけ
        p = (self.pdir / "S100TESTZ.prompt.txt").read_text(encoding="utf-8")
        self.assertIn("指名読み", p)
        self.assertIn("- T01: segment=Seasonings And Foods", p)
        self.assertNotIn("- T02:", p)                                     # 足した品目は新設前の期に指名しない
        self.assertIn('status="new" の行は 0 行', p)
        (self.pdir / "S100TESTZ.response.json").write_text(json.dumps(
            [{"target_id": "T01", "status": "found", "kpi_value": 105, "segment": "Seasonings And Foods"}],
            ensure_ascii=False), encoding="utf-8")
        (self.pdir / "S100TESTB.response.json").write_text("[]", encoding="utf-8")   # 基準期の応答は取り込まない
        self.assertEqual(self._cli("--import-responses", "--older-than-base"), 0)
        rec = json.loads((self.out / "9999" / "S100TESTZ.read.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["mode"], "nominated")
        self.assertEqual([(r["target_id"], r["status"], r["kpi_value"]) for r in rec["rows"]], [("T01", "found", 105)])
        self.assertFalse(any("今の入力から作るプロンプトと違う" in w for w in rec["warnings"]), rec["warnings"])
        self.assertEqual((self.out / "9999" / "S100TESTB.read.json").read_text(encoding="utf-8"), base_read)
        self.assertEqual(self._targets(), doc)                             # targets.json も変えない

    def _add_sec(self, doc_id, start, end):
        z = _sections("S100TESTB")
        z.update(doc_id=doc_id, period_start=start, period_end=end, submit_date=f"{(end or '2020')[:4]}-06-15 09:00")
        (self.data / "9999" / f"{doc_id}.sections.json").write_text(json.dumps(z, ensure_ascii=False), encoding="utf-8")

    def test_older_than_base_api_path_reads_only_older(self):        # (a) API 経路
        self._base_and_targets()
        self._add_sec("S100TESTZ", "2023-04-01", "2024-03-31")
        with mock.patch.object(yr, "read_one", return_value={"doc_id": "x"}) as ro, \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            rc = yr.main(["--code", "9999", "--data-dir", str(self.data), "--out-dir", str(self.out),
                          "--older-than-base", "--force"])
        self.assertEqual(rc, 0)
        self.assertEqual([c.args[0]["doc_id"] for c in ro.call_args_list], ["S100TESTZ"])
        kw = ro.call_args.kwargs
        self.assertEqual((kw["mode"], kw["max_new_rows"], kw["renominated_base"]), ("nominated", 0, False))
        self.assertEqual([t["target_id"] for t in kw["targets"]], ["T01"])

    def test_older_than_base_import_cuts_new_rows(self):              # (b) new の行は 0 行に切る
        self._base_and_targets()
        self._add_sec("S100TESTZ", "2023-04-01", "2024-03-31")
        self.assertEqual(self._cli("--dump-prompts", "--older-than-base"), 0)
        (self.pdir / "S100TESTZ.response.json").write_text(json.dumps(
            [{"target_id": "T01", "status": "found", "kpi_value": 105, "segment": "Seasonings And Foods"},
             {"status": "new", "item": "余計", "kpi_name": "k"}], ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self._cli("--import-responses", "--older-than-base"), 0)
        rec = json.loads((self.out / "9999" / "S100TESTZ.read.json").read_text(encoding="utf-8"))
        self.assertEqual([r["status"] for r in rec["rows"]], ["found"])
        self.assertTrue(any("new の行数 1 が上限 0" in w for w in rec["warnings"]), rec["warnings"])

    def test_older_than_base_selection_guards(self):                 # (c)(d)(e)
        tdoc = {"base_doc_id": "B", "base_period_end": "2025-03-31", "targets": []}
        docs = [{"doc_id": "B", "period_end": None}, {"doc_id": "N", "period_end": None},
                {"doc_id": "Q", "period_end": "2025-3-31"}, {"doc_id": "Z", "period_end": "2024-03-31"},
                {"doc_id": "A", "period_end": "2026-03-31"}]
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            base_id, older = yr.older_than_base_docs(docs, tdoc)
        self.assertEqual((base_id, [d["doc_id"] for d in older]), ("B", ["Z"]))   # 期末欠損・基準期は入らない
        self.assertIn("期末の無い doc 3 本は対象外", out.getvalue())
        with mock.patch("sys.stdout", io.StringIO()):
            _, older = yr.older_than_base_docs([{"doc_id": "B", "period_end": "2024-03-31"}],
                                               dict(tdoc, base_period_end="2026-03-31"))
        self.assertEqual(older, [])                                       # 期末が前でも基準期そのものは除く
        with self.assertRaises(SystemExit) as cm:                         # (d) 旧形式（base_period_end 無し）
            yr.older_than_base_docs(docs, {"base_doc_id": "B", "targets": []})
        self.assertIn("base_period_end", str(cm.exception))
        self._base_and_targets()                                          # (e) 前の期が無い＝正常終了
        out = io.StringIO()
        with mock.patch.object(yr, "call_claude", side_effect=AssertionError("API を呼んだ")), mock.patch("sys.stdout", out):
            rc = yr.main(["--code", "9999", "--data-dir", str(self.data), "--out-dir", str(self.out),
                          "--dump-prompts", "--older-than-base"])
        self.assertEqual(rc, 0)
        self.assertIn("より前の期が無い", out.getvalue())
        self.assertFalse(self.pdir.exists())

    def test_older_than_base_judge_five_periods(self):               # (f) judge が 5 期を昇順に並べる
        import yuho_judge as yj
        self._base_and_targets()
        self._add_sec("S100TESTZ", "2023-04-01", "2024-03-31")
        self._add_sec("S100TESTY", "2022-04-01", "2023-03-31")
        for d in ("S100TESTY", "S100TESTZ", "S100TESTB", "S100TESTA", "S100TESTC"):
            self._write(d, "nominated", [dict(TARGET_T01, status="found", kpi_value=100)])
        docs, reads = yj.load_inputs("9999", self.data, self.out)
        targets, ir_texts = yj.load_extras("9999", self.data, self.out, reads)
        res = yj.build_products(docs, reads, targets, ir_texts)
        t01 = next(p for p in res["products"] if p["target_id"] == "T01")
        self.assertEqual([per["period_end"] for per in t01["periods"]],
                         ["2023-03-31", "2024-03-31", "2025-03-31", "2026-03-31", "2027-03-31"])
        self.assertTrue(all(per["label"] != yj.LABEL_OUT_OF_SCOPE for per in t01["periods"]))
        self.assertTrue(t01["three_period_complete"])

    def test_older_than_base_refusals(self):
        with mock.patch("sys.stdout", io.StringIO()), self.assertRaises(SystemExit) as cm:
            yr.dump_prompts("9999", self.data, self.out, older_than_base=True)   # targets.json が無い
        self.assertIn("targets.json が要る", str(cm.exception))
        for flags in (["--make-targets"], ["--dump-prompts", "--renominate-base"]):
            with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as cm:
                yr.main(["--code", "9999", *flags, "--older-than-base"])
            self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
