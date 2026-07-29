"""daily_screen の「最終走査日取りこぼし」修理（2026-07-29）の回帰テスト。

対象バグ: pead_effective_end で scan_end が end_bd-1 に縮む開示反応系分岐は、
単日走査(start_bd==end_bd)で filtered が常に空になり、翌実行のカーソル前進で
恒久取りこぼしになっていた（SUE系2週間0件・生成器直叩き25件の実測）。

テスト方針: 生成器はスタブ（重い fins 全走査を避ける）。検証対象は
(1) pead_effective_end の縮み境界 (2) recover_start による翌実行回収
(3) append_new_signals の冪等（既存キー・同一df内重複・カレンダー終端）。

実行: python3 tests/test_daily_screen_recover.py   （unittest 自走・pytest 不要）
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import daily_screen as ds  # noqa: E402

BDAYS = [f"202607{d:02d}" for d in range(1, 11)]  # 合成カレンダー（連番10営業日）
BIDX = {d: i for i, d in enumerate(BDAYS)}


def sue_entry() -> dict:
    return {"kpi_name": "sue_beat", "params": {"threshold": 0.1},
            "defer_entry": True, "max_defer_bdays": 3}


def stub_generator(scan_start: str, scan_end: str, threshold: float):
    """SUE生成器スタブ: scan_end までの signal_date を持つ固定シグナルを返す。"""
    rows = [{"signal_date": d, "code": "99990"} for d in BDAYS
            if scan_start <= d <= scan_end]
    return pd.DataFrame(rows), {"stub": True}


class SwapMixin:
    """monkeypatch相当（teardownで確実に復元）。"""

    def swap(self, obj, name, value):
        original = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, original)


class TestPeadEffectiveEnd(SwapMixin, unittest.TestCase):
    def test_shrinks_when_next_bars_missing(self):
        self.swap(ds, "_bars_available", lambda d: False)
        self.assertEqual(ds.pead_effective_end("20260705", BIDX, BDAYS), "20260704")

    def test_keeps_end_when_next_bars_exist(self):
        self.swap(ds, "_bars_available", lambda d: True)
        self.assertEqual(ds.pead_effective_end("20260705", BIDX, BDAYS), "20260705")


class TestRecoverStart(SwapMixin, unittest.TestCase):
    def test_single_day_scan_recovers_previous_day(self):
        """単日走査 start=end=D で、縮んだ前回分（D-1）のシグナルが回収される（修理の本体）。"""
        self.swap(ds, "_bars_available", lambda d: False)  # 常に縮む=毎朝の実環境
        self.swap(ds, "_generate_sue_beat_signals_cached", stub_generator)
        df = ds.generate_kpi_signals(sue_entry(), "20260705", "20260705", {}, BIDX, BDAYS)
        got = set(df["signal_date"])
        self.assertIn("20260704", got, "前営業日の取りこぼし回収が失われている")
        self.assertTrue(got <= {"20260704", "20260705"},
                        "回収は1営業日前まで・過去を無制限に再放出しない")

    def test_calendar_head_boundary(self):
        self.swap(ds, "_bars_available", lambda d: True)
        self.swap(ds, "_generate_sue_beat_signals_cached", stub_generator)
        df = ds.generate_kpi_signals(sue_entry(), BDAYS[0], BDAYS[0], {}, BIDX, BDAYS)
        self.assertEqual(set(df["signal_date"]), {BDAYS[0]})  # recover_start=先頭日で例外なし


class TestAppendIdempotency(unittest.TestCase):
    def test_idempotent_across_runs(self):
        records = [{"kpi_name": "sue_beat", "code": "99990", "signal_date": "20260703"}]
        df = pd.DataFrame([{"signal_date": "20260703", "code": "99990"},   # 既存キー
                           {"signal_date": "20260704", "code": "99990"}])  # 新規
        self.assertEqual(ds.append_new_signals(records, sue_entry(), df, BIDX, BDAYS, "t1"), 1)
        self.assertEqual(ds.append_new_signals(records, sue_entry(), df, BIDX, BDAYS, "t2"), 0,
                         "再走査（回収の副作用）で重複追記してはならない")

    def test_idempotent_within_single_df(self):
        records: list[dict] = []
        df = pd.DataFrame([{"signal_date": "20260704", "code": "99990"},
                           {"signal_date": "20260704", "code": "99990"}])  # 同一df内重複
        self.assertEqual(ds.append_new_signals(records, sue_entry(), df, BIDX, BDAYS, "t3"), 1,
                         "同一実行内の同一キー重複は1件に畳む（2026-07-29堅牢化）")

    def test_skips_calendar_tail(self):
        records: list[dict] = []
        df = pd.DataFrame([{"signal_date": BDAYS[-1], "code": "99990"}])  # entry翌日が無い
        self.assertEqual(ds.append_new_signals(records, sue_entry(), df, BIDX, BDAYS, "t4"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
