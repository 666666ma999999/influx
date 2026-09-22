"""flea_panel_track の単体テスト（合成 __NEXT_DATA__・通信なし）。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import flea_panel_track as fpt  # noqa: E402


def page(items):
    data = {"props": {"initialState": {"searchState": {"search": {"result": {"items": items}}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def item(i, status, price, title="Switch 2 本体"):
    return {"id": f"z{i}", "title": title, "price": price, "itemStatus": status,
            "openTime": "2026-09-22T10:00:00+09:00", "endTime": "2026-09-22T11:00:00+09:00",
            "condition": "used20", "isPriceDown": False}


CFG = {"board": "yahoo_flea", "search_url": "https://x.example/search/{query}", "sold_param": "sold=1",
       "user_agent": "ua", "request_interval_sec": 0, "stale_days": 7,
       "queries": [{"id": "q1", "query": "Switch 2 本体", "group": "game-console"}]}


class ParseTest(unittest.TestCase):
    def test_no_next_data_is_none_not_empty(self):
        self.assertIsNone(fpt.parse_items("<html>Error Page</html>"))

    def test_parse_returns_items(self):
        got = fpt.parse_items(page([item(1, "OPEN", 100), item(2, "SOLD", 90)]))
        self.assertEqual([g["id"] for g in got], ["z1", "z2"])

    def test_build_url_sold(self):
        self.assertTrue(fpt.build_url(CFG, "Switch 2 本体", sold=True).endswith("?sold=1"))
        self.assertNotIn("?", fpt.build_url(CFG, "Switch 2 本体", sold=False))


class RunTest(unittest.TestCase):
    def _run(self, day, open_items, sold_items, data_dir):
        def fake_fetch(url, ua):
            return page(sold_items) if "sold=1" in url else page(open_items)
        return fpt.run(CFG, day, data_dir, fetch=fake_fetch)

    def test_two_days_state_transitions(self):
        with tempfile.TemporaryDirectory() as d:
            dd = Path(d)
            rc = self._run("2026-09-01", [item(1, "OPEN", 100), item(2, "OPEN", 110)], [item(9, "SOLD", 95)], dd)
            self.assertEqual(rc, 0)
            # 8日後: z1 はまだ出品中（stale）、z2 は売れた、z3 は新規
            rc = self._run("2026-09-09", [item(1, "OPEN", 100), item(3, "OPEN", 120)],
                           [item(2, "SOLD", 105), item(9, "SOLD", 95)], dd)
            self.assertEqual(rc, 0)
            rows = [json.loads(l) for l in (dd / "daily.jsonl").read_text().splitlines()]
            day2 = [r for r in rows if r["date"] == "2026-09-09"][0]
            self.assertEqual(day2["new_open"], 1)      # z3
            self.assertEqual(day2["stale_n"], 1)       # z1（9/1 初見・まだ出品中）
            self.assertEqual(day2["sold_new"], 1)      # z2 だけ（z9 は 9/1 に売却済で観測済み）
            self.assertEqual(day2["sold_median"], 100.0)
            state = json.loads((dd / "state.json").read_text())
            self.assertEqual(state["q1:z2"]["sold_seen"], "2026-09-09")
            self.assertEqual(state["q1:z2"]["first_seen"], "2026-09-01")
            self.assertTrue((dd / "snapshots" / "2026-09-09.jsonl").exists())

    def test_same_day_rerun_keeps_counts(self):
        """Codex P1-1: 同日再実行で new_open / sold_new が 0 に化けない（件数は state の日付から導く）。"""
        with tempfile.TemporaryDirectory() as d:
            dd = Path(d)
            self._run("2026-09-01", [item(1, "OPEN", 100)], [item(9, "SOLD", 95)], dd)
            self._run("2026-09-01", [item(1, "OPEN", 100)], [item(9, "SOLD", 95)], dd)
            rows = [json.loads(l) for l in (dd / "daily.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["new_open"], 1)
            self.assertEqual(rows[0]["sold_new"], 1)
            self.assertEqual(rows[0]["sold_new_median"], 95.0)

    def test_stale_counts_only_current_open_set(self):
        """Codex P2-4: 朝は古い A・再実行で新しい B だけ → stale_n は最新の観測（B）で数えるので 0。"""
        with tempfile.TemporaryDirectory() as d:
            dd = Path(d)
            self._run("2026-09-01", [item(1, "OPEN", 100)], [], dd)
            self._run("2026-09-09", [item(1, "OPEN", 100)], [], dd)      # 朝: A は stale
            self._run("2026-09-09", [item(2, "OPEN", 120)], [], dd)      # 再実行: B だけ
            row = [json.loads(l) for l in (dd / "daily.jsonl").read_text().splitlines() if '"2026-09-09"' in l][0]
            self.assertEqual(row["open_n"], 1)
            self.assertEqual(row["stale_n"], 0)

    def test_relist_after_sold_is_counted_again(self):
        """Codex P2-5: SOLD→OPEN（再出品）で sold_seen が解除され、再成約・滞留が再び数えられる。"""
        with tempfile.TemporaryDirectory() as d:
            dd = Path(d)
            self._run("2026-09-01", [item(1, "OPEN", 100)], [], dd)
            self._run("2026-09-02", [], [item(1, "SOLD", 100)], dd)
            self._run("2026-09-03", [item(1, "OPEN", 130)], [], dd)      # 再出品
            self._run("2026-09-11", [item(1, "OPEN", 130)], [], dd)      # 8 日後もまだ出品中 → stale
            rows = {json.loads(l)["date"]: json.loads(l) for l in (dd / "daily.jsonl").read_text().splitlines()}
            self.assertEqual(rows["2026-09-03"]["new_open"], 1)
            self.assertEqual(rows["2026-09-11"]["stale_n"], 1)
            self._run("2026-09-12", [], [item(1, "SOLD", 125)], dd)
            rows = {json.loads(l)["date"]: json.loads(l) for l in (dd / "daily.jsonl").read_text().splitlines()}
            self.assertEqual(rows["2026-09-12"]["sold_new"], 1)
            state = json.loads((dd / "state.json").read_text())
            self.assertEqual(state["q1:z1"]["relisted"], 1)

    def test_zero_after_status_filter_is_error(self):
        """Codex P1-2: 生の件数はあるが OPEN/SOLD 以外だけ → error・exit 2。"""
        with tempfile.TemporaryDirectory() as d:
            rc = self._run("2026-09-01", [item(1, "CLOSED", 100)], [item(2, "CANCELLED", 90)], Path(d))
            self.assertEqual(rc, 2)

    def test_zero_both_sides_is_error_exit2(self):
        with tempfile.TemporaryDirectory() as d:
            rc = self._run("2026-09-01", [], [], Path(d))
            self.assertEqual(rc, 2)
            row = json.loads((Path(d) / "daily.jsonl").read_text().splitlines()[0])
            self.assertEqual(row["status"], "error")

    def test_blocked_page_is_error_exit2(self):
        with tempfile.TemporaryDirectory() as d:
            rc = fpt.run(CFG, "2026-09-01", Path(d), fetch=lambda u, ua: "<html>Error Page</html>")
            self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
