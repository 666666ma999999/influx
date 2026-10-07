"""scripts/yuho_fetch.py（P-INF-34 工程①・plan T2）の単体テスト。

1. extract_sections: fixture CSV（UTF-16 LE・タブ区切り・tests/fixtures/yuho_mini.csv）を
   テスト内で zip に詰め、節 3 本の文字数・連結 2 指標・セグメント 2 つ・profit_measure・
   Reconciling を検証する（監査報告書の jpaud-*.csv は読まないことも含む）
2. find_yuho_docs: tmp の documents_all 偽物（gz 2 日分 + .rev 1 つ）から docTypeCode=120 だけを
   submitDateTime 降順で返し、.rev・他社・大量保有(350)・同一 docID の重複を落とす
3. 金額要素の取り違え防止（Codex Critical 6）: fixture の GrossProfitMargin（ユニット pure）を採らない・
   `_pick_element` の部分一致が Margin|Ratio|Rate|Percent|PerShare|SummaryOfBusinessResults を外す・
   ユニット ID が JPY でない行は金額として採らない
4. 終了コード（Codex Critical 7）: run/main が --no-fetch の zip 無し・csvFlag=0・鍵なしを failed に積み main が 1 を返す。
   csvFlag=0 は EDINET を叩かない
5. 鍵の伏せ字（Codex Critical 9）: fetch_csv_zip の例外文に api_key が出ない（HTTP エラー本文・JSON message・非 zip 応答）
6. ir_pdf_text（Codex Critical 7）: 全ページ本文 0 字なら main が 1 を返し `chars=0 failed_pages=<n>` を出す
"""

import gzip
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ir_pdf_text  # noqa: E402
import yuho_fetch as yf  # noqa: E402
from yuho_fetch import SECTION_KEYS, extract_sections, find_yuho_docs  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "yuho_mini.csv"
ENTITY = "jpcrp030000-asr_E99999-000"


def _make_zip(tmp: Path) -> Path:
    """fixture を本物と同じ zip 内パスに詰める（監査 CSV のダミーも同梱）。"""
    zip_path = tmp / "S1MINI.csv.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "XBRL_TO_CSV/jpaud-aar-cn-001_E99999-000_2025-03-31_01_2025-06-18.csv",
            "audit dummy".encode("utf-16"),
        )
        zf.writestr(
            "XBRL_TO_CSV/jpcrp030000-asr-001_E99999-000_2025-03-31_01_2025-06-18.csv",
            FIXTURE.read_bytes(),
        )
    return zip_path


class TestExtractSections(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.result = extract_sections(_make_zip(Path(cls.tmp.name)))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_sections_chars(self):
        s = self.result["sections"]
        self.assertEqual(set(s), set(SECTION_KEYS))
        self.assertEqual(len(SECTION_KEYS), 7)
        # タグ <p></p> を除いた文字数
        self.assertEqual(s["business_policy"]["chars"], len("経営方針のテスト本文です。"))
        self.assertEqual(s["business_policy"]["text"], "経営方針のテスト本文です。")
        self.assertEqual(s["mdna"]["chars"], len("当期の売上高は増収。冷凍食品が伸びた。"))
        self.assertEqual(s["risks"]["chars"], len("原材料高騰リスク。"))
        self.assertEqual(
            s["business_policy"]["element_id"],
            "jpcrp_cor:BusinessPolicyBusinessEnvironmentIssuesToAddressEtcTextBlock",
        )
        self.assertEqual(sum(1 for k in SECTION_KEYS if s[k]), 3)
        for key in ("segment_notes", "business_results", "business_description", "production_sales"):
            self.assertIsNone(s[key], key)

    def test_accounting_and_consolidated(self):
        self.assertEqual(self.result["accounting"], "IFRS")
        facts = self.result["facts"]
        self.assertEqual(facts["unit"], "円")
        # RevenueIFRSSummaryOfBusinessResults（部分一致の罠）ではなく完全一致の NetSalesIFRS を採る
        self.assertEqual(facts["elements"]["net_sales"], "jpigp_cor:NetSalesIFRS")
        self.assertEqual(facts["elements"]["operating_profit"], "jpigp_cor:OperatingProfitLossIFRS")
        self.assertIsNone(facts["elements"]["gross_profit"])
        self.assertIsNone(facts["elements"]["business_profit"])
        cur = facts["consolidated"]["CurrentYearDuration"]
        prior = facts["consolidated"]["Prior1YearDuration"]
        self.assertEqual(cur["net_sales"], 1000000)
        self.assertEqual(prior["net_sales"], 900000)
        self.assertEqual(cur["operating_profit"], 100000)
        self.assertEqual(prior["operating_profit"], 80000)
        self.assertIsNone(cur["gross_profit"])
        self.assertIsNone(cur["business_profit"])

    def test_segments(self):
        facts = self.result["facts"]
        self.assertEqual(len(facts["segments"]), 2)
        self.assertEqual(facts["profit_measure"], f"{ENTITY}:BusinessProfitLossIFRS")
        self.assertEqual(facts["sales_measure"], "jpigp_cor:NetSalesIFRS")
        foods = facts["segments"][f"{ENTITY}FoodsReportableSegmentMember"]
        self.assertEqual(foods["label"], "Foods")
        self.assertEqual(foods["ctx"]["CurrentYearDuration"], {"net_sales": 600000, "segment_profit": 70000})
        self.assertEqual(foods["ctx"]["Prior1YearDuration"], {"net_sales": 540000, "segment_profit": 60000})
        chem = facts["segments"][f"{ENTITY}ChemicalsReportableSegmentMember"]
        self.assertEqual(chem["ctx"]["CurrentYearDuration"], {"net_sales": 400000, "segment_profit": 30000})
        self.assertEqual(chem["ctx"]["Prior1YearDuration"], {"net_sales": 360000, "segment_profit": 20000})
        # Reconciling は別キー・売上行が無いので net_sales は None
        self.assertEqual(facts["reconciling"]["CurrentYearDuration"]["segment_profit"], -5000)
        self.assertIsNone(facts["reconciling"]["CurrentYearDuration"]["net_sales"])
        self.assertEqual(facts["total_segments"], {})


def _write_gz(path: Path, results: list) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump({"date": path.name[:8], "total_count": len(results), "results": results}, f)


def _rec(doc_id: str, doc_type: str, sec_code: str, submit: str, period_end: str) -> dict:
    return {
        "docID": doc_id,
        "docTypeCode": doc_type,
        "secCode": sec_code,
        "submitDateTime": submit,
        "periodStart": f"{int(period_end[:4]) - 1}-04-01",
        "periodEnd": period_end,
        "csvFlag": "1",
        "filerName": "テスト株式会社",
    }


class TestFindYuhoDocs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        old = _rec("S1OLD", "120", "28020", "2024-06-25 14:09", "2024-03-31")
        holding = _rec("S1HOLD", "350", "28020", "2024-06-25 15:00", "2024-03-31")
        new = _rec("S1NEW", "120", "28020", "2025-06-18 09:39", "2025-03-31")
        other = _rec("S1OTHER", "120", "99990", "2025-06-18 10:00", "2025-03-31")
        rev = _rec("S1REV", "120", "28020", "2025-06-18 23:00", "2025-03-31")
        _write_gz(d / "20240625.json.gz", [old, holding])
        _write_gz(d / "20250618.json.gz", [new, other, new])  # 同じ docID が 2 回出ても 1 回
        _write_gz(d / "20250618.rev1.json.gz", [rev])  # .rev は読まない
        self.dir = d

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_120_desc(self):
        docs = find_yuho_docs("28020", 3, self.dir)
        self.assertEqual([x["docID"] for x in docs], ["S1NEW", "S1OLD"])
        self.assertEqual(docs[0]["periodEnd"], "2025-03-31")
        self.assertEqual(docs[0]["periodStart"], "2024-04-01")
        self.assertEqual(docs[0]["csvFlag"], "1")
        self.assertEqual(set(docs[0]), {"docID", "submitDateTime", "periodStart", "periodEnd", "csvFlag"})

    def test_n_limit_and_no_match(self):
        self.assertEqual([x["docID"] for x in find_yuho_docs("28020", 1, self.dir)], ["S1NEW"])
        self.assertEqual(find_yuho_docs("00000", 3, self.dir), [])


def _money_row(element_id: str, ctx: str, unit: str, value: str) -> list:
    """CSV 9 列の数値行（要素ID／項目名／コンテキストID／相対年度／連結・個別／期間・時点／ユニットID／単位／値）。"""
    return [element_id, "", ctx, "", "その他", "期間", unit, "", value]


class TestMoneyElementGuard(unittest.TestCase):
    """Codex Critical 6: 比率要素・JPY 以外の行を金額として採らない。"""

    def test_fixture_margin_trap_not_picked(self):
        with tempfile.TemporaryDirectory() as td:
            facts = extract_sections(_make_zip(Path(td)))["facts"]
        # fixture に jpigp_cor:GrossProfitMargin（ユニット pure・0.4）があっても gross_profit には採らない
        self.assertIsNone(facts["elements"]["gross_profit"])
        self.assertIsNone(facts["consolidated"]["CurrentYearDuration"]["gross_profit"])
        self.assertIsNone(facts["consolidated"]["Prior1YearDuration"]["gross_profit"])
        self.assertEqual(facts["unit"], "円")
        self.assertIsNone(facts["single_segment"])

    def test_pick_element_excludes_ratio_like_names(self):
        traps = ["GrossProfitMargin", "GrossProfitRatio", "GrossProfitRate", "GrossProfitPercent",
                 "GrossProfitPerShare", "GrossProfitSummaryOfBusinessResults"]
        self.assertIsNone(yf._pick_element(traps, ("GrossProfitIFRS", "GrossProfit")))
        # 罠と一緒に正しい部分一致候補があればそれを採る
        self.assertEqual(yf._pick_element(traps + ["GrossProfitIFRS2"], ("GrossProfitIFRS", "GrossProfit")),
                         "GrossProfitIFRS2")
        # 完全一致は従来どおり最優先
        self.assertEqual(yf._pick_element(traps + ["GrossProfit"], ("GrossProfitIFRS", "GrossProfit")), "GrossProfit")

    def test_non_jpy_unit_rows_are_not_money(self):
        rows = [
            _money_row("jpigp_cor:NetSalesIFRS", "CurrentYearDuration", "JPY", "1000"),
            _money_row("jpigp_cor:GrossProfitIFRS", "CurrentYearDuration", "pure", "40"),  # 名前は正しいが単位が比率
            _money_row("jpigp_cor:OperatingProfitLossIFRS", "CurrentYearDuration", "JPYPerShares", "12"),
        ]
        consolidated, elements, units = yf._extract_consolidated(rows)
        self.assertEqual(consolidated["CurrentYearDuration"]["net_sales"], 1000)
        self.assertIsNone(consolidated["CurrentYearDuration"]["gross_profit"])
        self.assertIsNone(consolidated["CurrentYearDuration"]["operating_profit"])
        self.assertEqual(elements["net_sales"], "jpigp_cor:NetSalesIFRS")
        self.assertIsNone(elements["gross_profit"])
        self.assertEqual(units, {"JPY"})
        # セグメント側も同じ: 利益行が pure なら profit_measure は None
        seg_rows = [
            _money_row("jpigp_cor:NetSalesIFRS", f"CurrentYearDuration_{ENTITY}FoodsReportableSegmentMember", "JPY", "600"),
            _money_row(f"{ENTITY}:BusinessProfitLossIFRS", f"CurrentYearDuration_{ENTITY}FoodsReportableSegmentMember", "pure", "0.1"),
        ]
        seg = yf._extract_segments(seg_rows)
        self.assertEqual(seg["sales_measure"], "jpigp_cor:NetSalesIFRS")
        self.assertIsNone(seg["profit_measure"])
        self.assertEqual(seg["units"], {"JPY"})
        # 何も採れなければ unit は None
        self.assertIsNone(yf._unit_label(set()))
        self.assertEqual(yf._unit_label({"JPY"}), "円")


class _HTTPBody:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestFetchCsvZipRedactsKey(unittest.TestCase):
    """Codex Critical 9: EDINET のエラー文に渡した api_key を出さない。"""

    KEY = "secret-key-ABC123"

    def _fetch(self, td: str):
        return yf.fetch_csv_zip("S1X", self.KEY, Path(td))

    def test_http_error_body(self):
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(f"denied for {self.KEY}".encode()))
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(yf.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as cm:
                self._fetch(td)
        msg = str(cm.exception)
        self.assertIn("HTTP 401", msg)
        self.assertNotIn(self.KEY, msg)
        self.assertIn("***", msg)

    def test_json_status_message(self):
        body = json.dumps({"StatusCode": 401, "message": f"bad key {self.KEY}"}).encode()
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(yf.urllib.request, "urlopen", return_value=_HTTPBody(body)):
            with self.assertRaises(RuntimeError) as cm:
                self._fetch(td)
        msg = str(cm.exception)
        self.assertIn("StatusCode 401", msg)
        self.assertNotIn(self.KEY, msg)
        self.assertIn("***", msg)

    def test_non_zip_head(self):
        body = f"<html>{self.KEY}</html>".encode()
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(yf.urllib.request, "urlopen", return_value=_HTTPBody(body)):
            with self.assertRaises(RuntimeError) as cm:
                self._fetch(td)
        msg = str(cm.exception)
        self.assertIn("not a zip", msg)
        self.assertNotIn(self.KEY, msg)
        self.assertIn("***", msg)

    def test_non_zip_head_key_after_garbage_prefix_is_not_leaked(self):
        # Codex 2 回目 Critical 7: 先頭 16 バイトのゴミ＋32 文字の鍵。旧実装（40 バイトに切ってから伏せる）だと
        # 鍵が 24 文字で途切れて _redact に掛からず、先頭の断片が例外文に出る
        key32 = "0123456789abcdef0123456789abcdef"
        self.assertEqual(len(key32), 32)
        body = bytes(range(0x80, 0x90)) + key32.encode() + b"</html>"
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(yf.urllib.request, "urlopen", return_value=_HTTPBody(body)):
            with self.assertRaises(RuntimeError) as cm:
                yf.fetch_csv_zip("S1X", key32, Path(td))
        msg = str(cm.exception)
        self.assertIn("not a zip", msg)
        self.assertNotIn(key32[:8], msg)
        self.assertNotIn(key32, msg)
        self.assertIn("***", msg)

    def test_redact_noop_without_key(self):
        self.assertEqual(yf._redact("abc", None), "abc")
        self.assertEqual(yf._redact("abc", ""), "abc")


class TestRunExitCodes(unittest.TestCase):
    """Codex Critical 7: 保存できなかった docID があれば failed に積み、main は 1 を返す。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.docs_dir = root / "documents_all"
        self.docs_dir.mkdir()
        self.out_root = root / "yuho"
        (self.out_root / "9999").mkdir(parents=True)
        _make_zip(self.out_root / "9999")  # S1MINI.csv.zip だけ zip 済み
        self.have = _rec("S1MINI", "120", "99990", "2026-06-20 09:00", "2026-03-31")
        self.nozip = _rec("S1NOZIP", "120", "99990", "2025-06-20 09:00", "2025-03-31")
        self.nocsv = dict(_rec("S1NOCSV", "120", "99990", "2024-06-20 09:00", "2024-03-31"), csvFlag="0")

    def tearDown(self):
        self.tmp.cleanup()

    def _write_docs(self, *recs):
        _write_gz(self.docs_dir / "20260620.json.gz", list(recs))

    def test_no_fetch_marks_missing_zip_failed(self):
        self._write_docs(self.have, self.nozip, self.nocsv)
        saved, failed = yf.run("9999", 5, True, self.docs_dir, self.out_root)
        self.assertEqual([p.name for p in saved], ["S1MINI.sections.json"])
        self.assertEqual({f["docID"]: f["reason"] for f in failed},
                         {"S1NOZIP": "zip なし（--no-fetch）", "S1NOCSV": "CSV なし（csvFlag=0）"})
        self.assertTrue((self.out_root / "9999" / "S1MINI.sections.json").exists())

    def test_main_returns_1_when_any_failed_and_0_when_all_saved(self):
        self._write_docs(self.have, self.nozip)
        with mock.patch.object(yf, "DOCS_ALL_DIR", self.docs_dir), mock.patch.object(yf, "YUHO_DIR", self.out_root):
            self.assertEqual(yf.main(["--code", "9999", "--n", "5", "--no-fetch"]), 1)
            self.assertEqual(yf.main(["--code", "9999", "--n", "1", "--no-fetch"]), 0)  # 直近 1 期は zip 済み

    def test_csvflag_zero_never_hits_edinet(self):
        self._write_docs(self.have, self.nocsv)
        with mock.patch.object(yf, "get_api_key", return_value="k"), \
                mock.patch.object(yf, "fetch_csv_zip", side_effect=AssertionError("EDINET を叩いた")) as m:
            saved, failed = yf.run("9999", 5, False, self.docs_dir, self.out_root)
        self.assertEqual(m.call_count, 0)
        self.assertEqual([p.name for p in saved], ["S1MINI.sections.json"])
        self.assertEqual(failed, [{"docID": "S1NOCSV", "reason": "CSV なし（csvFlag=0）"}])

    def test_no_key_continues_with_existing_zip(self):
        self._write_docs(self.nozip, self.have)  # 鍵なしの docID が先でも、zip 済みの方は処理する
        with mock.patch.object(yf, "get_api_key", return_value=None), \
                mock.patch.object(yf, "fetch_csv_zip", side_effect=AssertionError("EDINET を叩いた")):
            saved, failed = yf.run("9999", 5, False, self.docs_dir, self.out_root)
        self.assertEqual([p.name for p in saved], ["S1MINI.sections.json"])
        self.assertEqual(failed, [{"docID": "S1NOZIP", "reason": "鍵なし"}])

    def test_no_docs(self):
        self._write_docs(self.have)
        self.assertEqual(yf.run("0000", 3, True, self.docs_dir, self.out_root), ([], []))
        with mock.patch.object(yf, "DOCS_ALL_DIR", self.docs_dir), mock.patch.object(yf, "YUHO_DIR", self.out_root):
            self.assertEqual(yf.main(["--code", "0000", "--no-fetch"]), 1)


class _FakePage:
    def __init__(self, text, raise_=False):
        self._text, self._raise = text, raise_

    def extract_text(self):
        if self._raise:
            raise ValueError("font")
        return self._text


class _FakeReader:
    def __init__(self, pages):
        self.pages = pages


class TestIrPdfTextExitCode(unittest.TestCase):
    """Codex Critical 7（ir_pdf_text）: 全ページ本文 0 字なら exit 1・`chars=0 failed_pages=<n>`。"""

    def _run(self, pages):
        with tempfile.TemporaryDirectory() as td:
            pdf = Path(td) / "x.pdf"
            pdf.write_bytes(b"%PDF-1.4 dummy")
            buf = io.StringIO()
            with mock.patch.object(ir_pdf_text, "PdfReader", return_value=_FakeReader(pages)), \
                    mock.patch("sys.stdout", buf):
                rc = ir_pdf_text.main(["--pdf", str(pdf)])
            txt = (Path(td) / "x.txt").read_text(encoding="utf-8")
        return rc, buf.getvalue(), txt

    def test_all_pages_empty_is_failure(self):
        rc, out, txt = self._run([_FakePage(""), _FakePage("  ", raise_=False), _FakePage("", raise_=True)])
        self.assertEqual(rc, 1)
        self.assertIn("pages=3 chars=0 failed_pages=3", out)
        self.assertEqual(txt, "[page 1]\n\n\n[page 2]\n\n\n[page 3]\n")  # 見出しだけの .txt は残す

    def test_some_text_is_success(self):
        rc, out, txt = self._run([_FakePage("本文あり"), _FakePage("", raise_=True)])
        self.assertEqual(rc, 0)
        self.assertIn("pages=2 chars=4 failed_pages=1", out)
        self.assertEqual(txt, "[page 1]\n本文あり\n\n[page 2]\n")
        self.assertEqual(ir_pdf_text.join_pages(["a", "b"]), "[page 1]\na\n\n[page 2]\nb")


if __name__ == "__main__":
    unittest.main()
