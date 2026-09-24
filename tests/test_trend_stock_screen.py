"""trend_stock_screen.classify() の回帰テスト（2026-09-24）。

対象: トレンド株を東証33業種ではなくセンターピン台帳の pin・sign で判定すること。
(a) AI SaaS → AIそのもの  (b) sign − （シャープ＝DRAM は原価側）→ 外す
(c) 車載半導体は AI の設備投資に入れない  (d) IHI は overrides（事業別利益で裏取り）が台帳より優先
(e) 台帳なし＋候補表あり → candidate、候補表なし → no_ledger  (f) 金利型は pin_type で拾う
(g) 実 config で港湾の貨物量（上組）は国の予算に入れない

実行: python3 -m unittest tests.test_trend_stock_screen
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import trend_stock_screen as t  # noqa: E402

TMAP = json.loads((ROOT / "config" / "trend_map.json").read_text())


def _cp(pin: str, sign: str = "+", pin_type: str = "volume") -> dict:
    return {"pin": pin, "sign": sign, "pin_type": pin_type}


class ClassifyTest(unittest.TestCase):
    def test_ai_saas(self):
        r = t.classify(_cp("AI SaaS・受託の契約数"), "3993", TMAP)
        self.assertEqual((r["group"], r["trend"]), ("ledger", "ai"))

    def test_sign_minus_excluded(self):
        r = t.classify(_cp("DRAM/NAND価格(PC・スマホの原価)", "-", "commodity"), "6753", TMAP)
        self.assertEqual(r["group"], "out")

    def test_auto_semis_not_ai_capex(self):
        r = t.classify(_cp("車載半導体の出荷数量(自動車生産台数)"), "6723", TMAP)
        self.assertEqual(r["group"], "out")

    def test_override_beats_ledger(self):
        r = t.classify(_cp("民間航空エンジンのスペア需要"), "7013", TMAP)
        self.assertEqual((r["group"], r["trend"], r["mark"]), ("override", "state", "△"))

    def test_not_in_ledger(self):
        self.assertEqual(t.classify(None, "6613", TMAP)["group"], "candidate")
        self.assertEqual(t.classify(None, "9999", TMAP)["group"], "no_ledger")

    def test_rate_by_pin_type(self):
        r = t.classify(_cp("国内金利(政策金利・長期金利)", "+", "rate"), "5838", TMAP)
        self.assertEqual(r["trend"], "rate")

    def test_port_cargo_not_state(self):
        r = t.classify(_cp("港湾取扱貨物量（コンテナ等）"), "9364", TMAP)
        self.assertEqual(r["group"], "out")


if __name__ == "__main__":
    unittest.main()
