"""開示表題の供給側レーン（P-INF-29・オーナー裁定 A 2026-10-04）の回帰テスト（実データに触れない）。

守らせる点:
1. index は Tdnet.id で重複除去する（週次と日次のチャンクで同じ開示が 3 回出る）。
2. 語グループ capacity/order/price・除外語（ETF|基準価額|投資信託|訂正|経過）・company_code 無しは落とす。
3. 「受注速報」は order のまま periodic=true。
4. TOP1000 判定は center_pin の 4 桁 code（5 桁末尾 0 → 4 桁）。
5. pubdate の範囲（since ≤ 日付 ≤ until）で絞る。
6. 台帳は id で冪等 append（2 回走らせても増えない）。
7. daily_reco の 🏭 節: 行が出る／定期の印／台帳が無い時は「⚠️ 生成失敗」（0 件に見せない）。
8. driver_discover_boj.scan_tdnet は共有ローダに乗り換えても出力の鍵（date, code, title）で畳む（従来どおり）。

実行: docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_tdnet_supply_lane
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import tdnet_index_lib as L  # noqa: E402
import tdnet_supply_lane as T  # noqa: E402


def rec(tid: str, pubdate: str, code: str, name: str, title: str) -> dict:
    return {"Tdnet": {"id": tid, "pubdate": pubdate, "company_code": code, "company_name": name,
                      "title": title, "document_url": "https://example.invalid/x.pdf"}}


ITEMS = [
    rec("1", "2026-09-30 15:30:00", "49710", "メック", "生産能力増強に向けた設備投資の追加に関するお知らせ"),   # capacity・TOP1000内
    rec("2", "2026-09-29 15:30:00", "37760", "ＢＢタワー", "大口受注に関するお知らせ"),                       # order・TOP1000外
    rec("3", "2026-09-28 15:30:00", "66510", "日東工", "価格改定のお知らせ"),                                 # price・TOP1000内
    rec("4", "2026-09-10 15:30:00", "14190", "タマホーム", "2026年８月度 受注速報"),                           # order・periodic（全角数字）
    rec("5", "2026-09-11 15:30:00", "13060", "ETF", "ＥＴＦ 基準価額に関するお知らせ 増産"),                # 除外（全角 ETF→NFKC）
    rec("6", "2026-09-24 15:30:00", "99990", "経過", "（開示事項の経過）大口受注（予定）に関するお知らせ"),  # 除外（経過）
    rec("7", "2026-09-25 15:30:00", "", "無コード", "新工場建設に関するお知らせ"),                           # company_code 無し
    rec("8", "2026-08-01 15:30:00", "49710", "メック", "増産に関するお知らせ"),                               # 範囲外（since 前）
    rec("9", "2026-09-26 15:30:00", "12340", "無関係", "剰余金の配当に関するお知らせ"),                      # どの語にも当たらない
]
CENTER_PIN = {"4971": {"code": "4971", "name": "メック"}, "6651": {"code": "6651", "name": "日東工"},
              "1419": {"code": "1419", "name": "タマホーム"}}


def write_index(root: Path, name: str, items: list[dict]) -> None:
    f = root / "2026" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(f, "wt", encoding="utf-8") as fh:
        json.dump({"total_count": len(items), "items": items}, fh, ensure_ascii=False)


class TestLaneScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.index = Path(self.tmp.name) / "index"
        # 週次チャンクと日次チャンクに同じ id を重ねて置く（実データの重なりを再現）
        write_index(self.index, "20260904_20260910.json.gz", ITEMS[3:5] + ITEMS[7:8])
        write_index(self.index, "20260911_20260917.json.gz", ITEMS[4:5])
        write_index(self.index, "20260925_20261001.json.gz", ITEMS)
        write_index(self.index, "20260930_20260930.json.gz", ITEMS[:1])
        self.ledger = Path(self.tmp.name) / "supply_lane.jsonl"
        self.since, self.until = dt.date(2026, 9, 1), dt.date(2026, 10, 4)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dedup_by_id_and_classification(self):
        rows = T.load_index(self.index, self.since)
        self.assertEqual(len(rows), 9, "id で重複除去される（9 件・重なり分は増えない）")
        cands = T.scan(rows, self.since, self.until, CENTER_PIN, "t")
        got = {r["id"]: r for r in cands}
        self.assertEqual(sorted(got), ["1", "2", "3", "4"])
        self.assertEqual(got["1"]["group"], "capacity")
        self.assertEqual(got["2"]["group"], "order")
        self.assertEqual(got["3"]["group"], "price")
        self.assertEqual(got["4"]["group"], "order")
        self.assertTrue(got["4"]["periodic"], "受注速報は periodic=true")
        self.assertFalse(got["2"]["periodic"])
        self.assertEqual(got["1"]["code4"], "4971")
        self.assertTrue(got["1"]["in_top1000"])
        self.assertTrue(got["3"]["in_top1000"])
        self.assertFalse(got["2"]["in_top1000"])
        self.assertEqual(got["1"]["rule_version"], T.RULE_VERSION)
        self.assertEqual([r["id"] for r in cands], ["4", "3", "2", "1"], "pubdate 昇順")

    def test_classify_edges(self):
        self.assertEqual(T.classify("ＥＴＦの基準価額 増産"), (None, False))
        self.assertEqual(T.classify("（開示事項の経過）大口受注のお知らせ"), (None, False))
        self.assertEqual(T.classify("（訂正）価格改定のお知らせ"), (None, False))
        self.assertEqual(T.classify("生産体制の強化について"), ("capacity", False))
        self.assertEqual(T.classify("生産体制強化について"), ("capacity", False))
        self.assertEqual(T.classify("大型案件の受注に関するお知らせ"), (None, False), "v0-title の語には無い（記録のみ）")
        self.assertEqual(T.classify("2026年9月度 受注速報"), ("order", True))
        self.assertEqual(T.classify("新工場の稼働状況および2026年9月度受注速報"), ("order", True),
                         "定期は他グループの語が同居しても periodic=true（採点母集団に混ぜない）")
        self.assertEqual(T.classify("値上げのお知らせ"), ("price", False))
        self.assertEqual(T.classify(""), (None, False))

    def test_v1_capex_group_does_not_change_v0(self):
        # P-INF-30: v1 の「設備」は v0 の 3 グループに当たらない表題だけ・行の rule_version で別採点
        for title in ("量産体制構築に向けた設備投資に関するお知らせ", "生産設備の増設について",
                      "新たな生産拠点の設立に関するお知らせ", "第二工場の建設に関するお知らせ", "量産開始のお知らせ"):
            self.assertEqual(T.classify(title), ("capex", False), title)
            self.assertEqual(T.rule_version_of("capex"), "v1-title")
        # v0 の語が同居したら v0 のグループのまま（v0 の事象は 1 件も変わらない）
        self.assertEqual(T.classify("生産能力増強に向けた設備投資の追加に関するお知らせ"), ("capacity", False))
        self.assertEqual(T.rule_version_of("capacity"), T.RULE_VERSION)
        self.assertEqual(T.classify("（訂正）設備投資に関するお知らせ"), (None, False), "除外語は v1 にも効く")
        self.assertEqual(T.classify("2026年9月度 受注速報 増設"), ("order", True), "定期の優先は v1 より上")
        rows = {"10": {"id": "10", "pubdate": "2026-09-30 15:30:00", "company_code": "49710",
                       "company_name": "メック", "title": "生産設備の増設について"}}
        got = T.scan(rows, self.since, self.until, CENTER_PIN, "t")
        self.assertEqual([(r["group"], r["rule_version"]) for r in got], [("capex", "v1-title")])

    def test_pubdate_range(self):
        rows = T.load_index(self.index, dt.date(2026, 1, 1))
        cands = T.scan(rows, dt.date(2026, 9, 29), dt.date(2026, 9, 29), CENTER_PIN, "t")
        self.assertEqual([r["id"] for r in cands], ["2"])
        cands = T.scan(rows, dt.date(2026, 8, 1), dt.date(2026, 8, 31), CENTER_PIN, "t")
        self.assertEqual([r["id"] for r in cands], ["8"])

    def test_idempotent_append_and_run_counts(self):
        res1 = T.run(self.since, self.until, index_dir=self.index, ledger_path=self.ledger, center_pin=CENTER_PIN)
        self.assertEqual((res1["candidates"], res1["new"], res1["existing"], res1["periodic"], res1["in_top1000"]),
                         (4, 4, 0, 1, 3))
        self.assertEqual(res1["scanned"], 8, "走査数= 重複除去後に期間内へ絞った表題数（8 月の id=8 は数えない）")
        res2 = T.run(self.since, self.until, index_dir=self.index, ledger_path=self.ledger, center_pin=CENTER_PIN)
        self.assertEqual((res2["new"], res2["existing"]), (0, 4), "2 回目は全て既存")
        self.assertEqual(len(T.load_ledger(self.ledger)), 4, "台帳は増えない")
        # dry-run は書かない
        self.ledger.unlink()
        res3 = T.run(self.since, self.until, dry_run=True, index_dir=self.index, ledger_path=self.ledger,
                     center_pin=CENTER_PIN)
        self.assertEqual(res3["new"], 4)
        self.assertFalse(self.ledger.exists(), "dry-run は台帳を作らない")
        # 正常実行で候補 0 件でも台帳ファイルは作る（表示側が「生成失敗」と誤判定しないため）
        res4 = T.run(dt.date(2026, 9, 26), dt.date(2026, 9, 26), index_dir=self.index, ledger_path=self.ledger,
                     center_pin=CENTER_PIN)
        self.assertEqual(res4["candidates"], 0)
        self.assertTrue(self.ledger.exists())
        self.assertEqual(T.load_ledger(self.ledger), [])

    def test_missing_index_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            T.run(self.since, self.until, index_dir=Path(self.tmp.name) / "nope", ledger_path=self.ledger,
                  center_pin=CENTER_PIN)
        with self.assertRaises(FileNotFoundError):
            T.run(dt.date(2027, 1, 1), dt.date(2027, 1, 2), index_dir=self.index, ledger_path=self.ledger,
                  center_pin=CENTER_PIN)
        # 対象ファイルは存在する（終了日 10-01 ≥ since 10-01）が収録行は全部 9 月＝期間外 → 候補 0 の正常終了にしない
        self.assertTrue(T.load_index(self.index, dt.date(2026, 10, 1)), "ファイルは読まれている（行は非空）")
        with self.assertRaises(FileNotFoundError):
            T.run(dt.date(2026, 10, 1), dt.date(2026, 10, 4), index_dir=self.index, ledger_path=self.ledger,
                  center_pin=CENTER_PIN)
        self.assertFalse(self.ledger.exists())
        with mock.patch("tdnet_supply_lane.INDEX_DIR", Path(self.tmp.name) / "nope"):
            self.assertEqual(T.main(["--days", "3"]), 1, "入力欠損は非ゼロ終了（候補 0 の正常終了にしない）")

    def test_driver_discover_scan_tdnet_keeps_output_key(self):
        """発見器②は共有ローダに乗り換えても (date, code, title) で畳む従来の出力のまま。"""
        try:
            import driver_discover_boj as D  # noqa: WPS433  (monthly_sources→requests が要る＝Docker のみ)
        except ModuleNotFoundError as exc:
            self.skipTest(f"driver_discover_boj の依存が無い環境: {exc}")
        pin_by = {"4971": {"code": "4971", "pin": "樹脂"}}
        hits = D.scan_tdnet(40, pin_by, index_dir=str(self.index), today=dt.date(2026, 10, 4))
        titles = [h["title"] for h in hits]
        self.assertEqual(titles.count("生産能力増強に向けた設備投資の追加に関するお知らせ"), 1, "重なりは 1 件に畳む")
        self.assertTrue(any(h["code"] == "4971" and h["in_top1000"] for h in hits))
        self.assertEqual(L.normalize_code("49710"), "4971")
        self.assertEqual(L.normalize_code("482A0"), "482A")


class TestRecoSection(unittest.TestCase):
    def setUp(self):
        import build_daily_reco as B  # noqa: WPS433
        self.B = B
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "supply_lane.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rows: list[dict]) -> None:
        self.ledger.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def test_section_rows_and_periodic_mark(self):
        self._write([
            {"id": "1", "pubdate": "2026-09-30 15:30:00", "code": "49710", "code4": "4971", "name": "メック",
             "title": "生産能力増強|に向けた" + "あ" * 80, "group": "capacity", "periodic": False, "in_top1000": True},
            {"id": "4", "pubdate": "2026-09-10 15:30:00", "code": "14190", "code4": "1419", "name": "タマホーム",
             "title": "2026年８月度 受注速報", "group": "order", "periodic": True, "in_top1000": True},
            {"id": "8", "pubdate": "2026-08-01 15:30:00", "code": "49710", "code4": "4971", "name": "メック",
             "title": "古い", "group": "capacity", "periodic": False, "in_top1000": True},
        ])
        with mock.patch("tdnet_supply_lane.LEDGER_PATH", self.ledger):
            lines, n, err = self.B.tdnet_supply_section(dt.date(2026, 9, 5))
        self.assertIsNone(err)
        self.assertEqual(n, 2, "窓より古い id=8 は出ない")
        text = "\n".join(lines)
        self.assertIn("## 🏭 企業発（開示表題・供給側）: 2 件", text)
        self.assertIn("| 日時 | 銘柄 | 社名 | 区分(増産/受注/価格/設備) | 表題(60字) | TOP1000 |", text)
        self.assertIn("| 受注（定期） |", text, "periodic 行には「定期」を付ける")
        self.assertIn("| 増産 |", text)
        row1 = next(l for l in lines if "| 4971 |" in l)
        self.assertIn("｜", row1, "表題の | は md_cell で無害化")
        self.assertNotIn("あ" * 61, row1, "表題は 60 字まで")
        self.assertTrue(lines.index(row1) < lines.index(next(l for l in lines if "| 1419 |" in l)), "新しい順")

    def test_section_missing_ledger_is_loud(self):
        with mock.patch("tdnet_supply_lane.LEDGER_PATH", self.ledger):
            lines, n, err = self.B.tdnet_supply_section(dt.date(2026, 9, 5))
        self.assertEqual(n, 0)
        self.assertIsNotNone(err)
        text = "\n".join(lines)
        self.assertIn("⚠️ 生成失敗", text)
        self.assertNotIn(": 0 件", text, "失敗を 0 件に見せない")

    def test_section_broken_ledger_is_loud(self):
        self.ledger.write_text("{not json\n", encoding="utf-8")
        with mock.patch("tdnet_supply_lane.LEDGER_PATH", self.ledger):
            lines, n, err = self.B.tdnet_supply_section(dt.date(2026, 9, 5))
        self.assertIn("⚠️ 生成失敗", "\n".join(lines))
        self.assertIn("JSONDecodeError", err)


if __name__ == "__main__":
    unittest.main()
