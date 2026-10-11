"""news_shock 本線の「古い記事は通知しない」関門（2026-10-11 P-INF-46 付帯）の回帰テスト。ネット不要。

背景: Google News RSS は検索句の when:2d を守らず数年前の記事を返す（実害= 2022 年の Boliden 亜鉛・
2021 年の Johnson Matthey プラチナの記事が 2026-10 に発火して通知）。
守ること: 掲載から 180 日超の記事は Mac 通知だけ止まり、台帳には status=hit のまま stale=true で残る
（事前登録の発火母集団を変えない）／新しい記事は従来どおり通知／関門を外すと古い記事も通知される（故障注入）。
実行: docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_news_shock_stale_gate -v
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import news_shock_collect as ns  # noqa: E402

CFG = ns.load_config()
QE = next(q for q in CFG["queries"] if q.get("term"))
TERM = QE["term"]
OLD = {"title": f"Boliden declares force majeure on {TERM} amid Norway strike", "desc": "",
       "link": "http://example.test/old", "pubdate": "Wed, 24 Aug 2022 07:00:00 GMT"}


def _fresh() -> dict:
    pub = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1))
    return {"title": f"Miner declares force majeure on {TERM} shipments", "desc": "",
            "link": "http://example.test/new", "pubdate": pub}


def _run_main(items: list[dict], max_age: object = "keep") -> tuple[mock.Mock, list[dict]]:
    """本線 main() を、取得と通知だけ差し替えて 1 便ぶん走らせる。返り値= (notify の mock, 台帳の行)。"""
    with tempfile.TemporaryDirectory() as td:
        ledger = Path(td) / "news_log.jsonl"
        patches = [
            mock.patch.object(ns, "LEDGER", ledger),
            mock.patch.object(ns, "load_seen", return_value=(set(), set())),
            mock.patch.object(ns, "fetch_rss",
                              side_effect=lambda qe, cfg: list(items) if qe is not None and qe.get("term") == TERM else []),
            mock.patch.object(ns, "notify"),
            mock.patch.object(sys, "argv", ["news_shock_collect.py"]),
        ]
        if max_age != "keep":
            patches.append(mock.patch.object(ns, "NOTIFY_MAX_AGE_DAYS", max_age))
        with contextlib.ExitStack() as st:
            mocks = [st.enter_context(p) for p in patches]
            with contextlib.redirect_stdout(io.StringIO()):
                rc = ns.main()
            notify = mocks[3]
            rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert rc == 0, rc
        return notify, rows


class StaleGateTest(unittest.TestCase):
    def test_judge_matches_fixture(self) -> None:
        """前提: 2 本の見本記事が判定（商品名 AND 語彙）に当たること。当たらなければ以下は空振り。"""
        for it in (OLD, _fresh()):
            self.assertIsNotNone(ns.judge(it["title"], [TERM], CFG["vocab"]), it["title"])

    def test_old_article_recorded_but_not_notified(self) -> None:
        notify, rows = _run_main([OLD])
        hits = [r for r in rows if r.get("status") == "hit"]
        self.assertEqual(len(hits), 1)                       # 台帳には hit のまま残る
        self.assertTrue(hits[0]["stale"])
        self.assertGreater(hits[0]["pub_age_days"], 1000)
        notify.assert_not_called()                           # 通知は出ない
        summary = [r for r in rows if r.get("type") == "run_summary"][0]
        self.assertEqual((summary["hit"], summary["stale"]), (1, 1))

    def test_fresh_article_still_notified(self) -> None:
        notify, rows = _run_main([OLD, _fresh()])
        by = {r["link"]: r for r in rows if r.get("status") == "hit"}
        self.assertEqual(len(by), 2)
        self.assertTrue(by["http://example.test/old"]["stale"])
        self.assertFalse(by["http://example.test/new"]["stale"])
        notify.assert_called_once()
        self.assertIn("1事象", notify.call_args[0][0])        # 通知に入るのは新しい 1 件だけ

    def test_fault_injection_gate_off_notifies_old(self) -> None:
        """関門を外す（None）と古い記事も通知される＝止めているのは関門そのもの。"""
        notify, rows = _run_main([OLD], max_age=None)
        notify.assert_called_once()
        self.assertFalse([r for r in rows if r.get("status") == "hit"][0]["stale"])

    def test_unparseable_pubdate_is_notified(self) -> None:
        it = dict(_fresh(), pubdate="いつか", link="http://example.test/nodate")
        notify, rows = _run_main([it])
        hit = [r for r in rows if r.get("status") == "hit"][0]
        self.assertIsNone(hit["pub_age_days"])
        self.assertFalse(hit["stale"])
        notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
