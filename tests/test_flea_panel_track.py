"""flea_panel_track の単体テスト（合成 __NEXT_DATA__・通信なし）。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import flea_panel_track as fpt  # noqa: E402


def page(items, total=None):
    res = {"items": items, "totalResultsAvailable": total if total is not None else len(items), "totalResultsReturned": len(items)}
    data = {"props": {"initialState": {"searchState": {"search": {"result": res}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def item(i, status, price, title="Switch 2 本体", day="2026-08-31"):
    # openTime/endTime は「前日」窓（--date の前の暦日）に入る時刻を既定にする
    return {"id": f"z{i}", "title": title, "price": price, "itemStatus": status,
            "openTime": f"{day}T10:00:00+09:00", "endTime": f"{day}T11:00:00+09:00",
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


class FilterTest(unittest.TestCase):
    def test_title_must_exclude(self):
        q = {"must": ["5090"], "exclude": ["ゲーミングpc"]}
        self.assertTrue(fpt.title_ok("RTX 5090 単体", q))
        self.assertFalse(fpt.title_ok("RTX 4090", q))
        self.assertFalse(fpt.title_ok("ゲーミングPC RTX 5090 搭載", q))
        self.assertTrue(fpt.title_ok("anything", {}))

    def test_build_url_sort_page(self):
        u = fpt.build_url(CFG, "PS5 本体", sold=True, sort="endTime", order="desc", page=2)
        self.assertIn("sold=1", u); self.assertIn("sort=endTime&order=desc", u); self.assertIn("page=2", u)

    def test_fetch_window_stops_at_start_and_caps(self):
        import datetime as _dt
        jst = _dt.timezone(_dt.timedelta(hours=9))
        start = _dt.datetime(2026, 9, 8, tzinfo=jst); end = _dt.datetime(2026, 9, 9, tzinfo=jst)
        # 1 頁目: 今日分 1 件（読み飛ばし）＋前日分 2 件＋前々日 1 件（ここで停止）
        items = [item(1, "SOLD", 100, day="2026-09-09"), item(2, "SOLD", 100, day="2026-09-08"),
                 item(3, "SOLD", 100, day="2026-09-08"), item(4, "SOLD", 100, day="2026-09-07")]
        calls = []
        def f(url, ua):
            calls.append(url); return page(items, total=999)
        cfg = dict(CFG, request_interval_sec=0, max_pages=5)
        w = fpt.fetch_window(cfg, {"query": "x"}, sold=True, sort_key="endTime", start=start, end=end, fetch=f)
        self.assertEqual(len(w["items"]), 2); self.assertEqual(w["total"], 999)
        self.assertEqual(len(calls), 1); self.assertFalse(w["capped"])
        # 全頁が前日分で埋まる → max_pages で capped
        full = [item(i, "SOLD", 100, day="2026-09-08") for i in range(100)]
        cfg2 = dict(cfg, max_pages=2)
        w2 = fpt.fetch_window(cfg2, {"query": "x"}, sold=True, sort_key="endTime", start=start, end=end,
                              fetch=lambda u, ua: page(full))
        self.assertTrue(w2["capped"]); self.assertEqual(w2["pages"], 2)
        self.assertEqual(len(w2["items"]), 100)   # 同じ 100 ID が 2 頁に出ても重複計上しない（Codex 2026-09-23 #2）

    def test_fetch_window_all_today_pages_is_capped(self):
        """全頁が当日分（窓の後ろ）で前日に届かない → items 0 でも capped=True（Codex 2026-09-23 #1）。"""
        import datetime as _dt
        jst = _dt.timezone(_dt.timedelta(hours=9))
        start = _dt.datetime(2026, 9, 8, tzinfo=jst); end = _dt.datetime(2026, 9, 9, tzinfo=jst)
        today_full = [item(i, "SOLD", 100, day="2026-09-09") for i in range(100)]
        cfg = dict(CFG, request_interval_sec=0, max_pages=3)
        w = fpt.fetch_window(cfg, {"query": "x"}, sold=True, sort_key="endTime", start=start, end=end,
                             fetch=lambda u, ua: page(today_full))
        self.assertEqual(len(w["items"]), 0); self.assertTrue(w["capped"]); self.assertEqual(w["pages"], 3)

    def test_parse_total_no_regex_fallback(self):
        """search.result が無い時は None（別モジュールの 0 を拾わない・Codex 2026-09-23 #5）。"""
        html = '<script id="__NEXT_DATA__">{"props":{"initialState":{"searchState":{"search":{}}}},"other":{"totalResultsAvailable":0}}</script>'
        self.assertIsNone(fpt.parse_total(html))

    def test_fetch_window_non_monotonic_scans_all_pages(self):
        """出品中の openTime 順は古い物が混ざる＝古い 1 件で止めず頁を読み切る（2026-09-23 実害: new_open_d1 が全て 0）。"""
        import datetime as _dt
        jst = _dt.timezone(_dt.timedelta(hours=9))
        start = _dt.datetime(2026, 9, 8, tzinfo=jst); end = _dt.datetime(2026, 9, 9, tzinfo=jst)
        mixed = [item(1, "OPEN", 100, day="2026-09-09"), item(2, "OPEN", 100, day="2025-10-25"),
                 item(3, "OPEN", 100, day="2026-09-08")] + [item(i, "OPEN", 100, day="2026-09-08") for i in range(10, 107)]
        cfg = dict(CFG, request_interval_sec=0, max_pages=2)
        w = fpt.fetch_window(cfg, {"query": "x"}, sold=False, sort_key="openTime", start=start, end=end,
                             fetch=lambda u, ua: page(mixed), monotonic=False)
        self.assertEqual(w["pages"], 2); self.assertEqual(len(w["items"]), 98); self.assertTrue(w["capped"])
        w_mono = fpt.fetch_window(cfg, {"query": "x"}, sold=False, sort_key="openTime", start=start, end=end,
                                  fetch=lambda u, ua: page(mixed), monotonic=True)
        self.assertEqual(len(w_mono["items"]), 0)   # 旧挙動＝古い 1 件で停止して 0 になる


class RunTest(unittest.TestCase):
    def _run(self, day, open_items, sold_items, data_dir, open_total=None):
        """前日窓に入るよう各 item の日付を day-1 に揃えてから返す。page>1 は空。"""
        import datetime as _dt
        d1 = (_dt.date.fromisoformat(day) - _dt.timedelta(days=1)).isoformat()
        def stamp(items):
            return [dict(it, openTime=f"{d1}T10:00:00+09:00", endTime=f"{d1}T11:00:00+09:00") for it in items]
        oi, si = stamp(open_items), stamp(sold_items)
        def fake_fetch(url, ua):
            if "page=" in url:
                return page([], total=0)
            if "sold=1" in url:
                return page(si)
            return page(oi, total=open_total)
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
            self.assertEqual(day2["sold_d1"], 2)             # 前日窓の成約（z2・z9）
            self.assertEqual(day2["new_open_d1"], 2)         # 前日窓の新規出品（z1・z3 の openTime）
            self.assertEqual(day2["open_total"], 2)
            self.assertEqual(day2["window"], "2026-09-08")
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


class Retry429Test(unittest.TestCase):
    """2026-10-29: 429 で落ちたクエリを一巡の後に 1 回だけ試し直す（2 周目でも 429 なら打ち切り）。"""

    @staticmethod
    def _cfg(n):
        return dict(CFG, queries=[{"id": f"q{i}", "query": f"kw{i}x"} for i in range(n)])

    @staticmethod
    def _fetch(blocked):
        """blocked(query_text) が True の間は 429 を投げる取得器。"""
        import urllib.error
        def f(url, ua):
            if blocked(url):
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
            if "page=" in url:
                return page([], total=0)
            return page([item(1, "SOLD" if "sold=1" in url else "OPEN", 100)])
        return f

    def _rows(self, d):
        lines = [json.loads(l) for l in (Path(d) / "daily.jsonl").read_text().splitlines()]
        keys = [(r["date"], r["query_id"]) for r in lines]
        self.assertEqual(len(keys), len(set(keys)))             # 同じ日・同じクエリの行が二重にならない
        return {r["query_id"]: r for r in lines}

    def test_second_pass_recovers_and_exit0(self):
        state = {"pass": 1}
        waits = []
        def blocked(url):
            return state["pass"] == 1 and ("kw1x" in url or "kw2x" in url)
        def sleep(s):
            waits.append(s); state["pass"] = 2
        with tempfile.TemporaryDirectory() as d:
            rc = fpt.run(dict(self._cfg(3), retry_429_wait_sec=900), "2026-09-01", Path(d),
                         fetch=self._fetch(blocked), sleep=sleep)
            rows = self._rows(d)
            self.assertEqual(rc, 0)
            self.assertEqual(waits, [900])
            self.assertEqual(len(rows), 3)                       # error 行が残って二重にならない
            self.assertTrue(all(r["status"] == "ok" for r in rows.values()))

    def test_still_429_gives_up_rest_and_exit2(self):
        tried = []
        def blocked(url):
            tried.append(url)
            return "kw0x" not in url                             # q0 だけ通る
        with tempfile.TemporaryDirectory() as d:
            rc = fpt.run(dict(self._cfg(4), retry_429_wait_sec=5), "2026-09-01", Path(d),
                         fetch=self._fetch(blocked), sleep=lambda s: None)
            rows = self._rows(d)
            self.assertEqual(rc, 2)
            self.assertEqual([rows[f"q{i}"]["status"] for i in range(4)], ["ok", "error", "error", "error"])
            self.assertEqual(sum("kw1x" in u for u in tried), 2)  # 2 周目は q1 で 429 → 打ち切り
            self.assertEqual(sum("kw2x" in u for u in tried), 1)  # q2・q3 は 2 周目に試さない
            out = (Path(d) / "daily.jsonl").read_text().splitlines()
            self.assertEqual(len(out), 4)

    def test_non_429_error_is_not_retried(self):
        waits = []
        with tempfile.TemporaryDirectory() as d:
            rc = fpt.run(self._cfg(1), "2026-09-01", Path(d),
                         fetch=lambda u, ua: "<html>Error Page</html>", sleep=waits.append)
            self.assertEqual((rc, waits), (2, []))


class Retry404Test(unittest.TestCase):
    """2026-11-13: Yahoo!フリマが時々返す一時的な 404 を試し直す（fetch_html で待って再試行・一巡の後の 2 周目にも乗せる）。"""

    URL = "https://x.example/search/kw?sold=1"

    @staticmethod
    def _http404(url="https://x.example/search/kw"):
        import urllib.error
        return urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    def _fetch_html(self, outcomes):
        """urlopen を outcomes の順（例外 or 本文 bytes）に差し替えて fetch_html を呼ぶ。待ちは記録だけ。"""
        import io
        from unittest import mock
        seq = iter(outcomes)
        calls, waits = [], []
        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            o = next(seq)
            if isinstance(o, Exception):
                raise o
            return io.BytesIO(o)
        with mock.patch.object(fpt.urllib.request, "urlopen", side_effect=fake_urlopen):
            try:
                return fpt.fetch_html(self.URL, "ua", sleep=waits.append), calls, waits, None
            except Exception as e:  # noqa: BLE001
                return None, calls, waits, e

    def test_fetch_html_404_once_then_ok(self):
        body, calls, waits, err = self._fetch_html([self._http404(), b"<html>ok</html>"])
        self.assertIsNone(err)
        self.assertEqual(body, "<html>ok</html>")
        self.assertEqual(len(calls), 2)
        self.assertEqual(waits, [fpt.RETRY_WAITS_404[0]])
        self.assertTrue(all(30 <= w <= 60 for w in fpt.RETRY_WAITS_404))

    def test_fetch_html_404_persists_raises_with_url(self):
        import urllib.error
        n = len(fpt.RETRY_WAITS_404) + 1
        body, calls, waits, err = self._fetch_html([self._http404() for _ in range(n)])
        self.assertIsNone(body)
        self.assertIsInstance(err, urllib.error.HTTPError)
        self.assertEqual(err.code, 404)
        self.assertIn(self.URL, str(err))                      # どの頁・どの並びで落ちたか追える
        self.assertEqual(len(calls), n)                        # 2〜3 回まで試し直して打ち切る
        self.assertEqual(waits, list(fpt.RETRY_WAITS_404))

    def test_fetch_html_other_http_error_not_retried(self):
        import urllib.error
        e500 = urllib.error.HTTPError(self.URL, 500, "Server Error", {}, None)
        body, calls, waits, err = self._fetch_html([e500])
        self.assertEqual((err.code, len(calls), waits), (500, 1, []))

    @staticmethod
    def _cfg(n):
        return dict(CFG, queries=[{"id": f"q{i}", "query": f"kw{i}x"} for i in range(n)], retry_429_wait_sec=900)

    @staticmethod
    def _fetch(blocked):
        def f(url, ua):
            if blocked(url):
                raise Retry404Test._http404(url)
            if "page=" in url:
                return page([], total=0)
            return page([item(1, "SOLD" if "sold=1" in url else "OPEN", 100)])
        return f

    def _rows(self, d):
        lines = [json.loads(l) for l in (Path(d) / "daily.jsonl").read_text().splitlines()]
        self.assertEqual(len(lines), len({(r["date"], r["query_id"]) for r in lines}))
        return {r["query_id"]: r for r in lines}

    def test_run_404_once_then_ok_in_second_pass(self):
        """1 周目で 404（fetch_html の再試行でも解けず）→ 2 周目で通れば ok・exit 0。"""
        state = {"pass": 1}
        waits = []
        def sleep(s):
            waits.append(s); state["pass"] = 2
        with tempfile.TemporaryDirectory() as d:
            rc = fpt.run(self._cfg(3), "2026-09-01", Path(d),
                         fetch=self._fetch(lambda u: state["pass"] == 1 and "kw1x" in u), sleep=sleep)
            rows = self._rows(d)
            self.assertEqual(rc, 0)
            self.assertEqual(waits, [900])
            self.assertTrue(all(r["status"] == "ok" for r in rows.values()))

    def test_run_404_persists_is_error_exit2(self):
        """404 が出続ければ error 行・exit 2。404 では 2 周目を打ち切らない（q1・q2 とも 2 回試す）。"""
        tried = []
        def blocked(url):
            tried.append(url)
            return "kw1x" in url or "kw2x" in url
        with tempfile.TemporaryDirectory() as d:
            rc = fpt.run(self._cfg(3), "2026-09-01", Path(d), fetch=self._fetch(blocked), sleep=lambda s: None)
            rows = self._rows(d)
            self.assertEqual(rc, 2)
            self.assertEqual([rows[f"q{i}"]["status"] for i in range(3)], ["ok", "error", "error"])
            self.assertIn("404", rows["q1"]["error"])
            self.assertEqual(sum("kw1x" in u for u in tried), 2)
            self.assertEqual(sum("kw2x" in u for u in tried), 2)


class SubjectLinkTest(unittest.TestCase):
    """P-INF-29 案5: クエリの subject（X 品薄対応表の受益 subject id）を daily 行に出す（記録だけ）。"""

    def test_subject_written_to_daily_row(self):
        cfg = dict(CFG, queries=[{"id": "q1", "query": "Switch 2 本体", "group": "7974", "subject": "secondary-resale"},
                                 {"id": "q2", "query": "Switch 2 本体", "group": "7974"}])
        d1 = "2026-08-31"
        def f(url, ua):
            if "page=" in url:
                return page([], total=0)
            return page([item(1, "SOLD" if "sold=1" in url else "OPEN", 100, day=d1)])
        with tempfile.TemporaryDirectory() as d:
            rc = fpt.run(cfg, "2026-09-01", Path(d), fetch=f)
            self.assertEqual(rc, 0)
            rows = {json.loads(l)["query_id"]: json.loads(l) for l in (Path(d) / "daily.jsonl").read_text().splitlines()}
            self.assertEqual(rows["q1"]["subject"], "secondary-resale")
            self.assertIsNone(rows["q2"]["subject"])   # 無いクエリは null（既存クエリの行の形は変えない）

    def test_subject_written_on_error_row(self):
        cfg = dict(CFG, queries=[{"id": "q1", "query": "x", "subject": "secondary-resale"}])
        with tempfile.TemporaryDirectory() as d:
            fpt.run(cfg, "2026-09-01", Path(d), fetch=lambda u, ua: "<html>Error Page</html>")
            row = json.loads((Path(d) / "daily.jsonl").read_text().splitlines()[0])
            self.assertEqual((row["status"], row["subject"]), ("error", "secondary-resale"))

    def test_config_subjects_exist_and_actionable(self):
        """実 config の subject は x_shortage_map に実在し actionable=true（判定値の取り違え・綴り違いを止める）。"""
        root = Path(__file__).resolve().parent.parent
        cfg = json.loads((root / "configs" / "flea_panel.json").read_text(encoding="utf-8"))
        smap = json.loads((root / "configs" / "x_shortage_map.json").read_text(encoding="utf-8"))
        subjects = {s["id"]: s for s in smap["subjects"]}
        linked = [q for q in cfg["queries"] if q.get("subject")]
        # レビュー済みの紐付け（2026-10-04 Codex 指摘で確定）。変える時は tasks/flea_panel_subject_link.md の判定表と同時に
        expected = {q: "secondary-resale" for q in (
            "switch2-honntai", "ps5-honntai", "lizardon-psa10", "pokeca-151-box",
            "figure-onepiece-pop", "7832-ガンプラ-未組立", "9766-遊戯王-25th-クォーターセンチュリー")}
        self.assertEqual({q["id"]: q["subject"] for q in linked}, expected)
        # 測れないと判定した subject は紐付けない（定価改定／量販の新品販売／車体が取れていない）
        self.assertFalse({"kaden-neage", "aircon-reizouko", "used-car"} & {q["subject"] for q in linked})
        for q in linked:
            self.assertIn(q["subject"], subjects, q["id"])
            self.assertTrue(subjects[q["subject"]].get("actionable"), q["id"])
        ids = [q["id"] for q in cfg["queries"]]
        self.assertEqual(len(ids), len(set(ids)))   # query_id の重複は state のキーを混ぜる


if __name__ == "__main__":
    unittest.main()
