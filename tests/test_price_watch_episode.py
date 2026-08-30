"""月次レーンのエピソード重複排除（P1）と事前登録 v3（P2）の回帰テスト（unittest自走）。

オーナー裁定 2026-08-30（敵対レビュー wf_ada84c33-b50）:
- checker: 前月比/前年同月比は閾値を「新たに跨いだ」公表月だけ鳴る（閾値上に留まる間は沈黙・
  閾値未満へ落ちて再び跨げば次のエピソード）
- forward: 同じ (series_id, code) を直近6ヶ月以内に記録済みなら skipped_dup（reason=episode）
- forward: 事前登録 v3 を1回だけ追記（冪等）・新規発火は spec_version 3
実行: python3 tests/test_price_watch_episode.py
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
import price_universe_check as puc  # noqa: E402
import price_watch_forward as fwd  # noqa: E402


def hrow(src_date: str, yoy=None, mom=None, status="ok") -> dict:
    return {"id": "s", "status": status, "src_date": src_date, "yoy_pct": yoy,
            "monthly_pct": mom, "value": 100.0, "date": src_date + "-15", "run_at": ""}


class TestMonthlyEpisodeTrigger(unittest.TestCase):
    TH = {"monthly_pct": 999.0, "yoy_pct": 3.0}

    def test_yoy_fires_once_then_silent_then_refires_after_dropping(self):
        """(a) 跨いだ月だけ鳴る → 閾値上の継続は沈黙 → 未満へ落ちて再跨ぎで再発火。"""
        hist: list[dict] = []
        fired = []
        for ym, yoy in [("2026-01", 2.0), ("2026-02", 3.6), ("2026-03", 4.5),
                        ("2026-04", 5.0), ("2026-05", 1.0), ("2026-06", 3.2)]:
            parsed = {"src_date": ym, "yoy_pct": yoy, "monthly_pct": 0.1, "value": 100.0}
            t = puc.monthly_triggers(parsed, "ok", hist, self.TH)
            fired.append(bool(t))
            hist.append(hrow(ym, yoy=yoy))
        self.assertEqual(fired, [False, True, False, False, False, True])

    def test_same_month_rerun_does_not_refire(self):
        """同じ公表月を週次実行で再び見ても鳴らない（既存の抑止は維持）。"""
        hist = [hrow("2026-06", yoy=4.5)]
        parsed = {"src_date": "2026-06", "yoy_pct": 4.5, "monthly_pct": None}
        self.assertEqual(puc.monthly_triggers(parsed, "ok", hist, self.TH), [])

    def test_first_observation_fires(self):
        """履歴なし（初回観測）は新規跨ぎ扱いで鳴る。"""
        parsed = {"src_date": "2026-06", "yoy_pct": 4.5, "monthly_pct": None}
        t = puc.monthly_triggers(parsed, "ok", [], self.TH)
        self.assertEqual(len(t), 1)
        self.assertIn("前年同月比 +4.50%(2026-06分)", t[0])

    def test_monthly_pct_episode_and_status_guard(self):
        """前月比も同じ規則。status!=ok では鳴らない。"""
        th = {"monthly_pct": 1.0}
        hist = [hrow("2026-05", mom=1.5)]
        parsed = {"src_date": "2026-06", "monthly_pct": 1.2}
        self.assertEqual(puc.monthly_triggers(parsed, "ok", hist, th), [])
        hist = [hrow("2026-05", mom=0.2)]
        self.assertEqual(puc.monthly_triggers(parsed, "ok", hist, th),
                         ["前月比 +1.20%(2026-06公表)"])
        self.assertEqual(puc.monthly_triggers(parsed, "suspect_jump", hist, th), [])

    def test_yoy_requires_explicit_threshold(self):
        """alert.yoy_pct を明示しない系列では前年同月比で鳴らない（Codex NIT-6 維持）。"""
        parsed = {"src_date": "2026-06", "yoy_pct": 50.0, "monthly_pct": 0.0}
        self.assertEqual(puc.monthly_triggers(parsed, "ok", [], {"monthly_pct": 1.0}), [])


# 合成「営業日」: 2026-01-01〜2026-12-28 の全暦日（評価予定日の算出に十分な長さ）
BDAYS = [f"2026{m:02d}{d:02d}" for m in range(1, 13) for d in range(1, 29)]


class TestForwardEpisodeSkip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "forward_log.jsonl"
        self.patches = [
            mock.patch.object(fwd, "LOG_PATH", self.log),
            mock.patch.object(fwd, "business_days", lambda: BDAYS),
            mock.patch.object(fwd, "latest_bar_day", lambda bdays, d: d),
            mock.patch.object(fwd, "load_topix", lambda: {d: 2000.0 for d in BDAYS}),
            mock.patch.object(fwd, "close_of", lambda c5, day: 1234.0),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def rows(self):
        return [json.loads(l) for l in self.log.read_text().splitlines() if l.strip()]

    @staticmethod
    def series(sid="sppi-road-freight", cadence="monthly"):
        return {"id": sid, "jp": "道路貨物", "cadence": cadence,
                "beneficiaries": [{"code": "9075", "sign": "+", "tier": "confirmed"}]}

    def test_same_pair_within_6_months_skipped_as_episode(self):
        """(b) 同じ (series, code) が6ヶ月以内に記録済み → skipped_dup reason=episode。"""
        s = self.series()
        fwd.record_firings([(s, {"value": 1.0}, ["前年同月比 +3.62%(2026-06分)"])], "2026-08-19")
        fwd.record_firings([(s, {"value": 1.0}, ["前年同月比 +4.54%(2026-07分)"])], "2026-09-20")
        firings = [r for r in self.rows() if r["type"] == "firing"]
        self.assertEqual(len(firings), 2)
        self.assertEqual([x["code"] for x in firings[0]["stocks"]], ["90750"])
        self.assertEqual(firings[1]["stocks"], [])
        self.assertEqual(firings[1]["skipped_dup"], ["90750"])
        self.assertEqual(firings[1]["skipped_dup_reason"], {"90750": "episode"})
        self.assertEqual(firings[1]["spec_version"], 3)

    def test_pair_older_than_6_months_is_recorded_again(self):
        """6ヶ月を超えて前の記録は再記録を妨げない（次のエピソード）。"""
        s = self.series()
        fwd.record_firings([(s, {}, ["前年同月比 +3.62%(2026-01分)"])], "2026-02-10")
        fwd.record_firings([(s, {}, ["前年同月比 +4.54%(2026-07分)"])], "2026-08-19")
        firings = [r for r in self.rows() if r["type"] == "firing"]
        self.assertEqual([x["code"] for x in firings[1]["stocks"]], ["90750"])
        self.assertEqual(firings[1]["skipped_dup"], [])

    def test_weekly_lane_unchanged(self):
        """週次レーンにはエピソード排除を適用しない（同日排除のみ従来どおり）。"""
        s = self.series(sid="wti", cadence="weekly")
        fwd.record_firings([(s, {}, ["weekly +5.1%"])], "2026-08-19")
        fwd.record_firings([(s, {}, ["weekly +6.0%"])], "2026-08-26")
        firings = [r for r in self.rows() if r["type"] == "firing"]
        self.assertEqual([x["code"] for x in firings[1]["stocks"]], ["90750"])
        # 同日の別系列は従来どおり same_day
        fwd.record_firings([(self.series(sid="brent", cadence="weekly"), {}, ["weekly +6%"])],
                           "2026-08-26")
        last = [r for r in self.rows() if r["type"] == "firing"][-1]
        self.assertEqual(last["skipped_dup_reason"], {"90750": "same_day"})

    def test_prereg_v3_appended_once(self):
        """(c) v3 事前登録は1回だけ（冪等）。旧 v2 行はそのまま残る。"""
        self.log.write_text(json.dumps({"type": "preregistration", "spec_version": 2}) + "\n")
        fwd.ensure_preregistration()
        fwd.ensure_preregistration()
        fwd.record_firings([(self.series(), {}, ["x"])], "2026-08-19")
        pre = [r for r in self.rows() if r["type"] == "preregistration"]
        self.assertEqual([p["spec_version"] for p in pre], [2, 3])
        v3 = pre[1]
        for needle in ("weekly>=+5%", "4週累積>=+10%", "前月比", "alert.monthly_pct",
                       "前年同月比", "alert.yoy_pct", "エピソード", "6ヶ月"):
            self.assertIn(needle, v3["hypothesis"], needle)
        self.assertEqual(v3["windows_bd"], {"w8": 40, "w15": 75})
        self.assertEqual(v3["episode_dedup_months"], 6)

    def test_months_before(self):
        self.assertEqual(fwd._months_before("2026-08-30", 6), "2026-02-28")
        self.assertEqual(fwd._months_before("2026-03-31", 1), "2026-02-28")
        self.assertEqual(fwd._months_before("2026-01-15", 6), "2025-07-15")


if __name__ == "__main__":
    unittest.main()
