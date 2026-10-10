"""Wikipedia 閲覧数の週次見張り `scripts/wiki_pv_watch.py` の回帰テスト（unittest 自走・ネット不要）。

事前登録 tasks/shortage_presign_forward.md §A / §E（2026-10-10 固定）の規則をそのまま検査する:
- 発火= S ≥ 2.0×B かつ B ≥ 200（B= 直前 8 週のうち 7/7 日の週の中央値・最低 6 週）
- 7/7 日そろわない週= judged=false/incomplete・基準 6 週未満= judged=false/base_insufficient
- エピソード= 題名ごと・初発火の week_start から 56 日以内は同一・57 日目は新
- 初回判定が最終（judged=true の (title, week_start) は再判定しない）
- score_eligible= week_end ≥ known_date−8（鮮度条件のみ・収集区間の判定は採点器 presign_forward_eval.py が担う）
- --dry-run は台帳を増やさない／run_summary の titles_planned = titles_ok + titles_error
実行: docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_wiki_pv_watch -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import wiki_pv_watch as wpw  # noqa: E402

RULE = {"week_start": "monday", "require_days": 7, "base_weeks": 8, "base_min_weeks": 6,
        "ratio": 2.0, "base_floor_per_week": 200, "episode_days": 56}
WS0 = date(2026, 8, 3)                      # baseline_from（月曜）
KNOWN = date(2026, 10, 13)                  # 初便（火曜）


def cfg_with(titles: List[dict], fetch_weeks: int = 10) -> dict:
    return {"version": 1, "api": {"base": "https://example.invalid/pv", "user_agent": "t", "fetch_weeks": fetch_weeks,
                                  "retry": 6, "sleep_sec": 0, "timeout_sec": 5},
            "rule": dict(RULE), "baseline_from": WS0.isoformat(), "titles": titles}


def daily_const(ws0: date, per_day_by_week: List[Optional[int]]) -> Dict[date, int]:
    """週ごとの 1 日あたり PV を並べて日次 dict を作る（None の週は欠測）。"""
    d: Dict[date, int] = {}
    for k, v in enumerate(per_day_by_week):
        if v is None:
            continue
        for i in range(7):
            d[ws0 + timedelta(days=7 * k + i)] = v
    return d


def make_fetcher(series: Dict[str, Dict[date, int]], errors: Optional[set] = None):
    """題名→日次 dict の固定取得器。errors に入った題名は例外を投げる。"""
    def _f(title, start, end, cfg):
        if errors and title in errors:
            raise RuntimeError("http429 after 6 attempt(s)")
        return {d: v for d, v in series[title].items() if start <= d <= end}
    return _f


def weeks_of(per_day_by_week: List[Optional[int]]):
    n = len(per_day_by_week)
    return wpw.weekly_table(daily_const(WS0, per_day_by_week), WS0, WS0 + timedelta(days=7 * (n - 1)))


class TestJudgeWeek(unittest.TestCase):
    """①〜④ 規則の単体（純関数）。"""

    def test_01_fires_when_double_and_base_over_floor(self):
        wk = weeks_of([300] * 8 + [600])            # B=2100・S=4200（2.0 倍ちょうど）
        j = wpw.judge_week(wk, WS0 + timedelta(days=56), RULE)
        self.assertTrue(j["judged"])
        self.assertTrue(j["fired"])
        self.assertEqual(j["base_n"], 8)
        self.assertEqual(j["base_median8"], 2100.0)
        self.assertEqual(j["ratio"], 2.0)
        self.assertIsNone(j["reason"])

    def test_01b_below_double_not_fired(self):
        wk = weeks_of([300] * 8 + [599])
        j = wpw.judge_week(wk, WS0 + timedelta(days=56), RULE)
        self.assertTrue(j["judged"])
        self.assertFalse(j["fired"])

    def test_02_base_below_floor_not_fired_even_if_5x(self):
        wk = weeks_of([20] * 8 + [100])             # B=140 < 200・S=700（5 倍）
        j = wpw.judge_week(wk, WS0 + timedelta(days=56), RULE)
        self.assertTrue(j["judged"])
        self.assertFalse(j["fired"])
        self.assertEqual(j["base_median8"], 140.0)

    def test_03_base_insufficient(self):
        wk = weeks_of([None, None, None, 300, 300, 300, 300, 300, 600])   # 直前 8 週のうち 7/7 は 5 週
        j = wpw.judge_week(wk, WS0 + timedelta(days=56), RULE)
        self.assertFalse(j["judged"])
        self.assertEqual(j["reason"], "base_insufficient")
        self.assertEqual(j["base_n"], 5)
        self.assertFalse(j["fired"])
        # 6 週あれば判定できる（最低 6 週）
        wk = weeks_of([None, None, 300, 300, 300, 300, 300, 300, 600])
        j = wpw.judge_week(wk, WS0 + timedelta(days=56), RULE)
        self.assertTrue(j["judged"])
        self.assertEqual(j["base_n"], 6)

    def test_03b_base_uses_only_complete_weeks(self):
        d = daily_const(WS0, [300] * 8 + [600])
        del d[WS0 + timedelta(days=7 * 2 + 1)]      # 基準 3 週目を 6/7 日にする → 基準から外れる
        wk = wpw.weekly_table(d, WS0, WS0 + timedelta(days=56))
        j = wpw.judge_week(wk, WS0 + timedelta(days=56), RULE)
        self.assertTrue(j["judged"])
        self.assertEqual(j["base_n"], 7)

    def test_04_incomplete_week(self):
        d = daily_const(WS0, [300] * 8 + [600])
        del d[WS0 + timedelta(days=56 + 3)]         # 当週 6/7 日
        wk = wpw.weekly_table(d, WS0, WS0 + timedelta(days=56))
        j = wpw.judge_week(wk, WS0 + timedelta(days=56), RULE)
        self.assertFalse(j["judged"])
        self.assertEqual(j["reason"], "incomplete")
        self.assertEqual(j["days"], 6)
        self.assertFalse(j["fired"])
        self.assertIsNone(j["ratio"])


class TestEpisode(unittest.TestCase):
    """⑤ 56 日以内は同一・57 日目は新（初発火起点・連鎖させない）。"""

    def test_05_episode_window(self):
        eps: Dict[str, List[date]] = {}
        w0 = date(2026, 10, 5)
        e1, f1 = wpw.assign_episode("T", w0, eps, 56)
        e2, f2 = wpw.assign_episode("T", w0 + timedelta(days=56), eps, 56)
        e3, f3 = wpw.assign_episode("T", w0 + timedelta(days=57), eps, 56)
        self.assertTrue(f1)
        self.assertFalse(f2)
        self.assertEqual(e1, e2)
        self.assertTrue(f3)
        self.assertNotEqual(e3, e1)
        # 連鎖させない: 2 発火目（+56）を起点にせず、+57 は新エピソード、その +56 日後は新エピソードと同一
        e4, f4 = wpw.assign_episode("T", w0 + timedelta(days=57 + 56), eps, 56)
        self.assertFalse(f4)
        self.assertEqual(e4, e3)
        # 別題名は別エピソード
        e5, f5 = wpw.assign_episode("U", w0, eps, 56)
        self.assertTrue(f5)
        self.assertNotEqual(e5, e1)


class TestScoreEligible(unittest.TestCase):
    """⑦ 境界: week_end = known_date−8 は対象・−9 は対象外。収集区間は見ない（鮮度条件のみ）。"""

    def test_07_boundaries(self):
        self.assertTrue(wpw.is_score_eligible(KNOWN - timedelta(days=8), KNOWN))
        self.assertFalse(wpw.is_score_eligible(KNOWN - timedelta(days=9), KNOWN))
        self.assertTrue(wpw.is_score_eligible(KNOWN - timedelta(days=2), KNOWN))
        self.assertTrue(wpw.is_score_eligible(KNOWN, KNOWN))

    def test_07b_outside_collect_interval_still_true_when_fresh(self):
        # 収集区間（10/13〜12/11）の外でも鮮度条件を満たせば true＝区間・延長の判定は採点器が known_date で行う
        self.assertTrue(wpw.is_score_eligible(date(2026, 10, 4), date(2026, 10, 10)))      # 初便より前の便
        self.assertTrue(wpw.is_score_eligible(date(2026, 12, 6), date(2026, 12, 12)))      # end_exclusive 当日
        self.assertTrue(wpw.is_score_eligible(date(2026, 12, 13), date(2026, 12, 15)))     # 源別延長期間
        self.assertFalse(wpw.is_score_eligible(date(2026, 12, 6), date(2026, 12, 15)))     # 鮮度切れは区間内外に関係なく false
        # presign_forward.json への依存は無い
        self.assertFalse(hasattr(wpw, "load_collect_interval"))
        self.assertFalse(hasattr(wpw, "FORWARD_CONFIG"))


class TestPlanWeeks(unittest.TestCase):
    def test_first_run_plan(self):
        p = wpw.plan_weeks(KNOWN, cfg_with([]))
        self.assertEqual(p["cutoff"], date(2026, 10, 12))
        self.assertEqual(p["judge_week_starts"][0], WS0)
        self.assertEqual(p["judge_week_starts"][-1], date(2026, 10, 5))
        self.assertEqual(len(p["judge_week_starts"]), 10)
        self.assertEqual(p["fetch_start"], WS0)                  # baseline_from で下限
        self.assertEqual(p["fetch_end"], date(2026, 10, 11))     # 最新の判定週の日曜

    def test_later_run_fetches_base_weeks(self):
        p = wpw.plan_weeks(date(2026, 12, 15), cfg_with([]))
        self.assertEqual(p["judge_week_starts"][-1], date(2026, 12, 7))
        self.assertEqual(p["judge_week_starts"][0], date(2026, 10, 5))
        self.assertEqual(p["fetch_start"], date(2026, 10, 5) - timedelta(days=56))

    def test_cutoff_excludes_current_week(self):
        # 金曜 10/10 の便: cutoff 10/09 → 最新の判定週は 09/28..10/04（10/05 週はまだ終わっていない）
        p = wpw.plan_weeks(date(2026, 10, 10), cfg_with([]))
        self.assertEqual(p["judge_week_starts"][-1], date(2026, 9, 28))
        self.assertEqual(p["fetch_end"], date(2026, 10, 4))


class TestRunLedger(unittest.TestCase):
    """⑥⑧⑨ 便の挙動（取得器をモック・台帳は一時ファイル）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "wiki_pv_watch" / "ledger.jsonl"
        self.titles = [{"title": "A", "subject": "a"}, {"title": "B", "subject": "b"}]
        self.cfg = cfg_with(self.titles)
        # A: 10 週とも 300/日・最終週（10/05）だけ 700/日 → 発火。B: 平坦 → 非発火
        self.series = {"A": daily_const(WS0, [300] * 9 + [700]),
                       "B": daily_const(WS0, [300] * 10)}

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, dry_run=False, errors=None, known=KNOWN, series=None):
        return wpw.run(self.cfg, "deadbeef0123", known, known, self.ledger, dry_run=dry_run,
                       fetcher=make_fetcher(series or self.series, errors),
                       run_at="2026-10-13T00:30:00+00:00", verbose=False)

    def _rows(self):
        return wpw.read_ledger(self.ledger)

    def test_first_run_rows_and_summary(self):
        res = self._run()
        rows = self._rows()
        s = res["summary"]
        self.assertEqual(s["titles_planned"], 2)
        self.assertEqual(s["titles_ok"], 2)
        self.assertEqual(s["titles_error"], 0)
        # 10 週中 08/03..09/07 の 6 週は基準不足・09/14..10/05 の 4 週が判定（題名 2 本）
        self.assertEqual(s["weeks_judged"], 8)
        self.assertEqual(s["weeks_pending"], 12)
        self.assertEqual(s["fires"], 1)
        self.assertEqual(s["new_episodes"], 1)
        fires = [r for r in rows if r["type"] == "fire"]
        self.assertEqual(len(fires), 1)
        f = fires[0]
        self.assertEqual((f["title"], f["week_start"], f["week_end"]), ("A", "2026-10-05", "2026-10-11"))
        self.assertTrue(f["first_in_episode"])
        self.assertTrue(f["score_eligible"])                 # week_end 10/11 ≥ 10/13−8（鮮度のみ・区間は採点器が判定）
        self.assertEqual(f["episode_id"], "wiki|A|2026-10-05")
        self.assertEqual(f["config_sha"], "deadbeef0123")
        self.assertEqual(f["config_version"], 1)
        # 週 09/28 は score_eligible=false（week_end 10/04 < 10/05）
        w = next(r for r in rows if r["type"] == "week_obs" and r["title"] == "A" and r["week_start"] == "2026-09-28")
        self.assertFalse(w["score_eligible"])
        self.assertTrue(w["judged"])
        self.assertFalse(w["fired"])
        # スキーマ（§E）のキーが全て揃う
        need = {"type", "run_at", "known_date", "title", "subject", "week_start", "week_end", "days", "sum",
                "base_median8", "base_n", "ratio", "judged", "reason", "fired", "score_eligible", "episode_id",
                "config_version", "config_sha"}
        self.assertTrue(need <= set(w))
        self.assertEqual(rows[-1]["type"], "run_summary")
        pend = next(r for r in rows if r["type"] == "week_obs" and r["title"] == "A" and r["week_start"] == "2026-08-03")
        self.assertFalse(pend["judged"])
        self.assertEqual(pend["reason"], "base_insufficient")

    def test_06_judged_weeks_are_not_rejudged(self):
        # 既に judged=true の (A, 2026-10-05) を台帳に置く → 2 便目で再判定されない（データが発火でも fire 行が出ない）
        wpw.append_rows(self.ledger, [{"type": "week_obs", "run_at": "x", "known_date": "2026-10-13", "title": "A",
                                       "subject": "a", "week_start": "2026-10-05", "week_end": "2026-10-11",
                                       "days": 7, "sum": 2100, "base_median8": 2100.0, "base_n": 8, "ratio": 1.0,
                                       "judged": True, "reason": None, "fired": False, "score_eligible": True,
                                       "episode_id": None, "config_version": 1, "config_sha": "deadbeef0123"}])
        res = self._run()
        rows = self._rows()
        self.assertEqual(res["summary"]["fires"], 0)
        a_1005 = [r for r in rows if r["type"] == "week_obs" and r["title"] == "A" and r["week_start"] == "2026-10-05"]
        self.assertEqual(len(a_1005), 1)                      # 既存行のみ・新規行なし
        self.assertEqual(res["summary"]["weeks_judged"], 7)   # A は 3 週・B は 4 週

    def test_06b_pending_week_is_rejudged_and_episode_continues(self):
        # 1 便目: A の 10/05 週が 6/7 日 → pending。2 便目（10/20）: 7/7 日そろい発火。3 便目: 10/12 週も発火→同一エピソード
        s1 = {"A": dict(self.series["A"]), "B": self.series["B"]}
        del s1["A"][date(2026, 10, 8)]
        res1 = self._run(series=s1)
        self.assertEqual(res1["summary"]["fires"], 0)
        p = next(r for r in self._rows() if r["type"] == "week_obs" and r["title"] == "A" and r["week_start"] == "2026-10-05")
        self.assertFalse(p["judged"])
        self.assertEqual(p["reason"], "incomplete")
        s2 = {"A": daily_const(WS0, [300] * 9 + [700, 700]), "B": daily_const(WS0, [300] * 11)}
        res2 = wpw.run(self.cfg, "deadbeef0123", date(2026, 10, 20), date(2026, 10, 20), self.ledger, dry_run=False,
                       fetcher=make_fetcher(s2), run_at="r2", verbose=False)
        self.assertEqual(res2["summary"]["fires"], 2)         # 10/05 週（再判定）＋ 10/12 週
        self.assertEqual(res2["summary"]["new_episodes"], 1)  # 同一エピソード
        fires = [r for r in self._rows() if r["type"] == "fire"]
        self.assertEqual([f["week_start"] for f in fires], ["2026-10-05", "2026-10-12"])
        self.assertEqual({f["episode_id"] for f in fires}, {"wiki|A|2026-10-05"})
        self.assertEqual([f["first_in_episode"] for f in fires], [True, False])
        self.assertEqual([f["known_date"] for f in fires], ["2026-10-20", "2026-10-20"])
        # 10/05 週は known_date 10/20 で week_end 10/11 < 10/12 → 採点対象外・10/12 週は対象
        self.assertEqual([f["score_eligible"] for f in fires], [False, True])

    def test_06c_pending_week_out_of_range_becomes_unjudgeable(self):
        res1 = self._run()                                     # 08/03 週は base_insufficient（pending）
        self.assertTrue(any(r["type"] == "week_obs" and r["week_start"] == "2026-08-03" and not r["judged"]
                            for r in self._rows()))
        later = date(2026, 10, 20)                             # 判定窓 08/10..10/12 → 08/03 週は範囲外
        s2 = {"A": daily_const(WS0, [300] * 11), "B": daily_const(WS0, [300] * 11)}
        res2 = wpw.run(self.cfg, "deadbeef0123", later, later, self.ledger, dry_run=False,
                       fetcher=make_fetcher(s2), run_at="r2", verbose=False)
        self.assertEqual(res2["summary"]["weeks_unjudgeable"], 2)   # A・B の 08/03 週
        u = [r for r in self._rows() if r["type"] == "week_obs" and r["week_start"] == "2026-08-03" and r["judged"]]
        self.assertEqual(len(u), 2)
        self.assertTrue(all(r["reason"] == "unjudgeable" and r["fired"] is False for r in u))
        # 3 便目では再び書かれない（確定済み）
        res3 = wpw.run(self.cfg, "deadbeef0123", later, later, self.ledger, dry_run=False,
                       fetcher=make_fetcher(s2), run_at="r3", verbose=False)
        self.assertEqual(res3["summary"]["weeks_unjudgeable"], 0)
        self.assertEqual(res3["summary"]["weeks_judged"], 0)   # 全週 judged 済み（10/12 週は 2 便目で判定済み）

    def test_08_dry_run_does_not_write(self):
        res = self._run(dry_run=True)
        self.assertFalse(self.ledger.exists())
        self.assertFalse(self.ledger.parent.exists())
        self.assertEqual(res["summary"]["fires"], 1)           # 判定はする・書かないだけ
        self.assertEqual(res["rows"][-1]["type"], "run_summary")

    def test_09_summary_planned_equals_ok_plus_error(self):
        res = self._run(errors={"B"})
        s = res["summary"]
        self.assertEqual(s["titles_planned"], 2)
        self.assertEqual(s["titles_ok"], 1)
        self.assertEqual(s["titles_error"], 1)
        self.assertEqual(s["titles_planned"], s["titles_ok"] + s["titles_error"])
        errs = [r for r in self._rows() if r["type"] == "error"]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["title"], "B")
        self.assertIn("http429", errs[0]["error"])
        self.assertEqual(errs[0]["config_sha"], "deadbeef0123")
        self.assertFalse(any(r["type"] == "week_obs" and r["title"] == "B" for r in self._rows()))
        self.assertEqual(s["fires"], 1)                        # A は続行して発火


class TestFetchRetry(unittest.TestCase):
    """429 は Retry-After 優先（無ければ 5×試行回数）で最大 retry 回・404 は再試行しない。"""

    def _http_error(self, code, headers=None):
        import urllib.error
        from email.message import Message
        h = Message()
        for k, v in (headers or {}).items():
            h[k] = v
        return urllib.error.HTTPError("u", code, "x", h, None)

    def test_429_retries_with_retry_after_then_succeeds(self):
        cfg = cfg_with([])
        sleeps = []
        calls = {"n": 0}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"items": [{"timestamp": "2026100500", "views": 12}]}).encode()

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise self._http_error(429, {"Retry-After": "3"})
            return _Resp()

        with mock.patch.object(wpw, "_sleep", sleeps.append), \
                mock.patch("urllib.request.urlopen", fake_urlopen):
            out = wpw.fetch_daily("Nintendo Switch 2", date(2026, 10, 5), date(2026, 10, 11), cfg)
        self.assertEqual(out, {date(2026, 10, 5): 12})
        self.assertEqual(sleeps, [3.0, 3.0])
        self.assertEqual(calls["n"], 3)

    def test_429_gives_up_after_retry_count(self):
        cfg = cfg_with([])
        sleeps = []

        def fake_urlopen(req, timeout=None):
            raise self._http_error(429)

        with mock.patch.object(wpw, "_sleep", sleeps.append), \
                mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError) as cm:
                wpw.fetch_daily("A", date(2026, 10, 5), date(2026, 10, 11), cfg)
        self.assertIn("http429", str(cm.exception))
        self.assertEqual(sleeps, [5.0, 10.0, 15.0, 20.0, 25.0])    # 6 回試行・間の待ち 5 回

    def test_404_not_retried(self):
        cfg = cfg_with([])
        sleeps = []

        def fake_urlopen(req, timeout=None):
            raise self._http_error(404)

        with mock.patch.object(wpw, "_sleep", sleeps.append), \
                mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError):
                wpw.fetch_daily("A", date(2026, 10, 5), date(2026, 10, 11), cfg)
        self.assertEqual(sleeps, [])

    def test_article_url_encodes_title(self):
        cfg = cfg_with([])
        u = wpw.article_url(cfg, "Nintendo Switch 2", date(2026, 8, 3), date(2026, 10, 11))
        self.assertTrue(u.endswith("/Nintendo_Switch_2/daily/20260803/20261011"))
        u2 = wpw.article_url(cfg, "遊☆戯☆王オフィシャルカードゲーム", date(2026, 8, 3), date(2026, 10, 11))
        self.assertNotIn("☆", u2)
        self.assertIn("%E2%98%86", u2)


class TestConfigContract(unittest.TestCase):
    """実 config（事前登録 v1）が本スクリプトの読む形になっている。"""

    def test_real_config_loads(self):
        cfg = wpw.load_config(ROOT / "configs/wiki_pv_watch.json")
        self.assertEqual(cfg["version"], 1)
        self.assertEqual(len(cfg["titles"]), 50)
        self.assertEqual(len({t["title"] for t in cfg["titles"]}), 50)
        for k in ("require_days", "base_weeks", "base_min_weeks", "ratio", "base_floor_per_week", "episode_days"):
            self.assertIn(k, cfg["rule"])
        for k in ("base", "user_agent", "fetch_weeks", "retry", "sleep_sec", "timeout_sec"):
            self.assertIn(k, cfg["api"])
        self.assertEqual(len(wpw.config_sha(ROOT / "configs/wiki_pv_watch.json")), 12)


if __name__ == "__main__":
    unittest.main()
