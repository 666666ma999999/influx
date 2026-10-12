"""news_shock 本線の v3（掲載 2 日以内）の印と成績表（2026-10-12 P-INF-47 b・事前登録 §8）の回帰テスト。ネット不要。

背景: Google News RSS が検索句の when:2d を守らず、通知した 360 事象のうち掲載 2 日以内は 68 件だけだった。
v3 は発火条件を「v2 ＋ 掲載 2 日以内」にした新しい版で、v2 と並走する（v2 の行・評価・config は変えない）。
守ること: hit 行に v3_target の印が付く（2.0 日以内は対象・2.01 日は対象外・掲載時刻が読めない記事は対象外・
開始の境目より前の便は対象外・重複事象は対象外）／成績表の v3 の表は v3_target=true の事象だけを数え、0 件でも落ちない／
関門を外すと古い記事にも印が付く（故障注入）。本番の台帳には書かない（一時ファイル）。
実行: docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_news_shock_v3_mark -v
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
import news_shock_eval as nse  # noqa: E402

CFG = ns.load_config()
QE = next(q for q in CFG["queries"] if q.get("term"))
TERM = QE["term"]
RUN_AT = "2026-10-13T10:00:00+00:00"                    # 2026-10-13 19:00 JST 便（開始の境目より後）
OLD = {"title": f"Boliden declares force majeure on {TERM} amid Norway strike", "desc": "",
       "link": "http://example.test/old", "pubdate": "Wed, 24 Aug 2022 07:00:00 GMT"}


def _item(link: str, hours_ago: float, title: str | None = None) -> dict:
    pub = format_datetime(datetime.now(timezone.utc) - timedelta(hours=hours_ago))
    return {"title": title or f"Miner declares force majeure on {TERM} shipments", "desc": "",
            "link": link, "pubdate": pub}


def _run_main(items: list[dict], start: str = "2000-01-01T00:00:00+00:00",
              max_age: object = "keep") -> tuple[mock.Mock, list[dict]]:
    """本線 main() を、取得と通知だけ差し替えて 1 便ぶん走らせる（test_news_shock_stale_gate と同じ型）。
    start= v3 の開始の境目（既定は過去＝今の便が対象になる）。返り値= (notify の mock, 台帳の行)。"""
    with tempfile.TemporaryDirectory() as td:
        ledger = Path(td) / "news_log.jsonl"
        patches = [
            mock.patch.object(ns, "LEDGER", ledger),
            mock.patch.object(ns, "load_seen", return_value=(set(), set())),
            mock.patch.object(ns, "fetch_rss",
                              side_effect=lambda qe, cfg: list(items) if qe is not None and qe.get("term") == TERM else []),
            mock.patch.object(ns, "notify"),
            mock.patch.object(sys, "argv", ["news_shock_collect.py"]),
            mock.patch.object(ns, "V3_START_UTC", start),
        ]
        if max_age != "keep":
            patches.append(mock.patch.object(ns, "V3_MAX_AGE_DAYS", max_age))
        with contextlib.ExitStack() as st:
            mocks = [st.enter_context(p) for p in patches]
            with contextlib.redirect_stdout(io.StringIO()):
                rc = ns.main()
            notify = mocks[3]
            rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert rc == 0, rc
        return notify, rows


class V3MarkTest(unittest.TestCase):
    def test_boundary_two_days(self) -> None:
        """掲載から便まで 2.0 日は対象・2.01 日（表示用の 0.1 日丸めでは 2.0）は対象外＝丸めずに比べる。"""
        self.assertTrue(ns.is_v3_target("Sun, 11 Oct 2026 10:00:00 GMT", RUN_AT, False))      # ちょうど 2.0 日
        self.assertFalse(ns.is_v3_target("Sun, 11 Oct 2026 09:45:36 GMT", RUN_AT, False))     # 2.01 日
        self.assertEqual(ns.pub_age_days("Sun, 11 Oct 2026 09:45:36 GMT", RUN_AT), 2.0)      # 丸めると 2.0 に見える

    def test_unparseable_pubdate_is_not_target(self) -> None:
        """掲載時刻が読めない記事は v3 の対象外（安全側。通知の鮮度関門は逆に通す＝v2 の出方は不変）。"""
        self.assertFalse(ns.is_v3_target("いつか", RUN_AT, False))
        self.assertFalse(ns.is_v3_target("", RUN_AT, False))
        self.assertFalse(ns.is_stale(ns.pub_age_days("いつか", RUN_AT)))

    def test_future_pubdate_and_bad_run_at(self) -> None:
        """便より後の掲載時刻は 1 日先までが対象（媒体の時差の付け間違いの幅）・それより先は対象外。便の時刻が読めなければ対象外。"""
        self.assertTrue(ns.is_v3_target("Tue, 13 Oct 2026 19:00:00 GMT", RUN_AT, False))      # 便の 9 時間後＝対象
        self.assertTrue(ns.is_v3_target("Wed, 14 Oct 2026 10:00:00 GMT", RUN_AT, False))      # ちょうど 1 日先＝対象
        self.assertFalse(ns.is_v3_target("Wed, 14 Oct 2026 10:01:00 GMT", RUN_AT, False))     # 1 日と 1 分先＝対象外
        self.assertFalse(ns.is_v3_target("Mon, 09 Aug 2027 10:00:00 GMT", RUN_AT, False))     # 300 日先＝対象外
        self.assertFalse(ns.is_v3_target("Tue, 13 Oct 2026 09:00:00 GMT", "garbage", False))  # 便の時刻が読めない＝落ちずに対象外
        # 故障注入: 下限を外すと 300 日先の記事にも印が付く＝下限が効いている
        self.assertTrue(ns.is_v3_target("Mon, 09 Aug 2027 10:00:00 GMT", RUN_AT, False, min_age_days=float("-inf")))

    def test_before_start_and_dup_are_not_target(self) -> None:
        fresh = "Mon, 12 Oct 2026 02:00:00 GMT"
        self.assertFalse(ns.is_v3_target(fresh, "2026-10-12T08:59:59+00:00", False))   # 境目の 1 秒前（10-12 17:59:59 JST）
        self.assertTrue(ns.is_v3_target(fresh, "2026-10-12T09:00:00+00:00", False))    # 境目ちょうど（10-12 18:00 JST）
        self.assertFalse(ns.is_v3_target("Sun, 11 Oct 2026 10:00:00 GMT", RUN_AT, True))   # 重複事象

    def test_main_marks_fresh_only(self) -> None:
        """本線を 1 便走らせると、新しい記事だけ v3_target=true・古い記事は false・run_summary に件数。"""
        notify, rows = _run_main([OLD, _item("http://example.test/new", 1)])
        by = {r["link"]: r for r in rows if r.get("status") == "hit"}
        self.assertEqual(len(by), 2)
        self.assertTrue(by["http://example.test/new"]["v3_target"])
        self.assertFalse(by["http://example.test/old"]["v3_target"])
        self.assertTrue(by["http://example.test/old"]["stale"])          # v2 の鮮度関門は従来どおり
        summary = [r for r in rows if r.get("type") == "run_summary"][0]
        self.assertEqual((summary["hit"], summary["v3_target"]), (2, 1))
        notify.assert_called_once()                                      # 通知の出方は変えない

    def test_main_before_start_marks_nothing(self) -> None:
        """開始の境目より前の便では新しい記事にも印が付かない（過去分は v3 に数えない）。"""
        _, rows = _run_main([_item("http://example.test/new", 1)], start="2999-01-01T00:00:00+00:00")
        hit = [r for r in rows if r.get("status") == "hit"][0]
        self.assertFalse(hit["v3_target"])

    def test_main_dup_event_not_marked(self) -> None:
        """同じ事象（同商品・同ファミリー・同 UTC 日）の 2 件目は dup_event=true で v3_target=false。"""
        a = _item("http://example.test/a", 1)
        b = dict(a, link="http://example.test/b")
        _, rows = _run_main([a, b])
        hits = [r for r in rows if r.get("status") == "hit"]
        self.assertEqual([h["dup_event"] for h in hits], [False, True])
        self.assertEqual([h["v3_target"] for h in hits], [True, False])

    def test_fault_injection_gate_off_marks_old(self) -> None:
        """関門（2 日）を外すと 2022 年の記事にも印が付く＝対象を絞っているのは関門そのもの。"""
        _, rows = _run_main([OLD], max_age=float("inf"))
        self.assertTrue([r for r in rows if r.get("status") == "hit"][0]["v3_target"])


class V3ReportTest(unittest.TestCase):
    @staticmethod
    def _ledger(td: Path, with_v3: bool) -> Path:
        hit = lambda link, **kw: {"run_at": RUN_AT, "link": link, "status": "hit", "config_version": 2,
                                  "event_id": link, "dup_event": False, **kw}
        ev = lambda link, code, win, x: {"type": "evaluation", "link": link, "code": code, "window": win,
                                         "excess_pt": x}
        rows = [hit("http://v2only", v3_target=False),
                hit("http://nomark"),                                   # 印なし（開始前の行）＝v3 に入れない
                hit("http://v1", config_version=1),                     # v1 行＝両表に入れない
                ev("http://v2only", "1111", "w5", 3.0), ev("http://nomark", "1111", "w5", 1.0),
                ev("http://v1", "1111", "w5", 9.0)]
        if with_v3:
            rows += [hit("http://v3a", v3_target=True), hit("http://v3fp", v3_target=True),
                     {"type": "false_positive", "link": "http://v3fp"},   # FP 化された v3 行は数えない
                     ev("http://v3a", "1111", "w5", -2.0), ev("http://v3a", "2222", "w5", 4.0),
                     ev("http://v3fp", "1111", "w5", 8.0),
                     {**ev("http://v3a", "1111", "w5", 0.5), "type": "evaluation_correction"}]   # 訂正行を優先
        p = td / "news_log.jsonl"
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        return p

    def test_v3_table_counts_only_marked_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            led = self._ledger(Path(td), with_v3=True)
            before = led.read_bytes()
            rep = nse.report(led)
            self.assertEqual(led.read_bytes(), before)                    # 読み取り専用
            v2_tbl, v3_tbl = rep.split("## v2")[1].split("## v3")
            self.assertIn("発火事象 3 件", v2_tbl)                          # v2only・nomark・v3a（v1 と FP は入れない）
            self.assertIn("| +5 営業日 | 3 | 3 | +2.25 | 4 | 4 | +2.00 |", v2_tbl)   # 事象= 銘柄平均: 3.0・1.0・(0.5+4.0)/2=2.25 → 中央値 2.25
            self.assertIn("発火事象 1 件", v3_tbl)
            self.assertIn("| +5 営業日 | 1 | 1 | +2.25 | 2 | 2 | +2.25 |", v3_tbl)   # v3a だけ・訂正 0.5 が −2.0 を上書き
            self.assertIn("| +20 営業日 | 0 | 0 | — |", v3_tbl)

    def test_v3_zero_events_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rep = nse.report(self._ledger(Path(td), with_v3=False))
            v3_tbl = rep.split("## v3")[1]
            self.assertIn("発火事象 0 件", v3_tbl)
            self.assertIn("対象 0 件", v3_tbl)
            self.assertIn("| +5 営業日 | 0 | 0 | — | 0 | 0 | — |", v3_tbl)
            self.assertIn("対象 0 件", nse.report(Path(td) / "missing.jsonl"))


if __name__ == "__main__":
    unittest.main()
