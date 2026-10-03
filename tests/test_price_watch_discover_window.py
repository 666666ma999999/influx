"""発見器の収集窓（--days）の回帰テスト（P-INF-22・オーナー裁定 2026-10-03 Q2=a）。

裁定: 週 1 回・前日 1 日分だけの窓では 7 日中 6 日が未観測＝runner は --days 7 で過去 7 UTC 日を
1 日ずつ回す。再試行で二重に記録しないため、ok/partial/empty で記録済みの日は飛ばす（--force で取り直し）。
error/login_wall の日は再試行対象。

発見器本体は playwright 系を import するため Docker で回す（CLAUDE.md の unittest 経路）。
実行: docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_price_watch_discover_window
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
import price_watch_discover as d  # noqa: E402


class WindowDays(unittest.TestCase):
    def test_single_day(self):
        self.assertEqual(d.window_days("2026-10-03", 1), ["2026-10-03"])

    def test_seven_days_oldest_first_across_month_boundary(self):
        days = d.window_days("2026-10-03", 7)
        self.assertEqual(len(days), 7)
        self.assertEqual(days[0], "2026-09-27")
        self.assertEqual(days[-1], "2026-10-03")
        self.assertEqual(days, sorted(days))


class RecordedDays(unittest.TestCase):
    def _queue(self, rows):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        for r in rows:
            tmp.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.write("not json\n")
        tmp.close()
        return Path(tmp.name)

    def test_ok_partial_empty_are_recorded_but_error_and_wall_are_not(self):
        q = self._queue([
            {"type": "candidate_batch", "date": "2026-09-27", "status": "ok"},
            {"type": "candidate_batch", "date": "2026-09-28", "status": "partial"},
            {"type": "candidate_batch", "date": "2026-09-29", "status": "empty"},
            {"type": "candidate_batch", "date": "2026-09-30", "status": "error"},
            {"type": "candidate_batch", "date": "2026-10-01", "status": "login_wall"},
            {"type": "other", "date": "2026-10-02", "status": "ok"},
        ])
        self.assertEqual(d.recorded_days(q), {"2026-09-27", "2026-09-28", "2026-09-29"})

    def test_missing_queue(self):
        self.assertEqual(d.recorded_days(Path("/nonexistent/queue.jsonl")), set())


class MainWindowLoop(unittest.TestCase):
    def _run(self, argv, recorded):
        ran: list[str] = []
        with mock.patch.object(sys, "argv", ["x", *argv]), \
             mock.patch.object(d, "recorded_days", return_value=recorded), \
             mock.patch.object(d, "run_day", side_effect=lambda day, seen=None: ran.append(day) or 0), \
             mock.patch.object(d.time, "sleep", lambda *_: None):
            rc = d.main()
        return rc, ran

    def test_days7_skips_recorded_and_runs_rest_oldest_first(self):
        rc, ran = self._run(["--date", "2026-10-03", "--days", "7"], {"2026-09-27", "2026-09-30"})
        self.assertEqual(rc, 0)
        self.assertEqual(ran, ["2026-09-28", "2026-09-29", "2026-10-01", "2026-10-02", "2026-10-03"])

    def test_force_reruns_recorded_days(self):
        rc, ran = self._run(["--date", "2026-10-03", "--days", "2", "--force"], {"2026-10-02", "2026-10-03"})
        self.assertEqual(ran, ["2026-10-02", "2026-10-03"])

    def test_default_is_one_day_backward_compatible(self):
        rc, ran = self._run(["--date", "2026-10-03"], set())
        self.assertEqual(ran, ["2026-10-03"])

    def test_rc_is_max_of_days(self):
        with mock.patch.object(sys, "argv", ["x", "--date", "2026-10-03", "--days", "3"]), \
             mock.patch.object(d, "recorded_days", return_value=set()), \
             mock.patch.object(d, "run_day", side_effect=[0, 1, 0]), \
             mock.patch.object(d.time, "sleep", lambda *_: None):
            self.assertEqual(d.main(), 1)


class CrossDayDedup(unittest.TestCase):
    """X の since/until 窓は実測で連日重なる＝同じ実行内では前の日に見た投稿 id を次の日で除外する。"""

    def test_run_day_filters_seen_ids_and_extends_set(self):
        posts_day = [{"id": "a", "content": "x"}, {"id": "b", "content": "y"}]
        stats = {"per_query": {"q": 2}, "login_wall": False, "errors": []}
        seen = {"a"}
        captured = {}

        def fake_extract(posts, known):
            captured["ids"] = [p["id"] for p in posts]
            return []

        with mock.patch.object(d, "collect_posts", return_value=(posts_day, stats)), \
             mock.patch.object(d, "load_known_vocab", return_value=set()), \
             mock.patch.object(d, "extract_candidates", side_effect=fake_extract), \
             mock.patch.object(d, "llm_refine", return_value=None), \
             mock.patch.object(d, "append_event", lambda *_: None):
            rc = d.run_day("2026-10-02", seen)
        self.assertEqual(rc, 0)
        self.assertEqual(captured["ids"], ["b"])
        self.assertEqual(seen, {"a", "b"})


if __name__ == "__main__":
    unittest.main()
