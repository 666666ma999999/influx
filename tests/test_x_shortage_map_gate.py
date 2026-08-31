"""x_shortage_map.validate() の center_pin 整合ゲート（2026-08-30）の回帰テスト。

対象: 受益カードの tier が正本（data/center_pin/center_pin.jsonl）の裁定とズレたまま
表に残る事故を validate() が fail-closed で落とすこと。
(a) confirmed + watch=none → NG  (b) provisional + watch=none → OK
(c) note に「不成立」+ provisional → NG  (d) capex subject + confirmed + watch=none → OK（数量型が設計どおり）

追加（2026-08-31 P-INF-12 Q1）: subject_op_pct の定義ゲート
(e) 0 → NG（非開示なら null・実測ゼロならあり得ない）  (f) null → OK  (g) 150超（例200）→ NG

実行: python3 tests/test_x_shortage_map_gate.py   （unittest 自走・pytest 不要）
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import x_shortage_map as x  # noqa: E402


_UNSET = object()


def _map(tier: str, stype: str = "price", op_pct=_UNSET) -> dict:
    card = {
        "code": "9999", "name": "テスト社", "layer": "maker", "sign": "+",
        "tier": tier, "evidence": "テスト根拠", "verified": "2026-08-30",
        "source": {"title": "t", "url": "https://example.com"},
    }
    if op_pct is not _UNSET:
        card["subject_op_pct"] = op_pct
    return {
        "version": 1,
        "generated": "2026-08-30",
        "shortage_types": {
            "price": {"actionable": True, "desc": ""},
            "capex": {"actionable": True, "desc": ""},
            "secondary": {"actionable": True, "desc": ""},
            "volume": {"actionable": True, "desc": ""},
            "resale": {"actionable": False, "desc": ""},
            "hedge": {"actionable": False, "desc": ""},
            "unclear": {"actionable": False, "desc": ""},
        },
        "subjects": [{
            "id": "t-subject", "label": "t", "shortage_type": stype,
            "actionable": True, "reason": "テスト", "queries": ["q1"],
            "beneficiaries": [card],
        }],
    }


def _errors(tier: str, watch: str, note: str, stype: str = "price", op_pct=_UNSET) -> list[str]:
    with tempfile.TemporaryDirectory() as d:
        cp = Path(d) / "center_pin.jsonl"
        cp.write_text(json.dumps({"code": "9999", "name": "テスト社", "watch": watch,
                                  "note": note}, ensure_ascii=False) + "\n",
                      encoding="utf-8")
        return x.validate(_map(tier, stype, op_pct=op_pct),
                          center_pin=x.load_center_pin(cp), query_ids={"q1"})


class CenterPinGateTest(unittest.TestCase):
    def test_confirmed_with_watch_none_is_ng(self):
        errs = _errors("confirmed", "none", "数量が利益を決める")
        self.assertTrue(any("watch=none" in e for e in errs), errs)

    def test_provisional_with_watch_none_is_ok(self):
        self.assertEqual(_errors("provisional", "none", "数量が利益を決める"), [])

    def test_capex_confirmed_with_watch_none_is_ok(self):
        # 4062 イビデン型: capex 主題の受益者は volume ピンが正常（統括判断 2026-08-30）
        self.assertEqual(_errors("confirmed", "none", "基板発注量が利益を決める", stype="capex"), [])

    def test_note_fuseiritsu_with_provisional_is_ng(self):
        errs = _errors("provisional", "manual", "価格直結は不成立")
        self.assertTrue(any("不成立/却下" in e for e in errs), errs)

    def test_rejected_with_note_fuseiritsu_is_ok(self):
        self.assertEqual(_errors("rejected", "none", "価格直結は不成立"), [])


class SubjectOpPctGateTest(unittest.TestCase):
    """subject_op_pct の定義ゲート（2026-08-31 P-INF-12 Q1・docs §0b）。"""

    def test_op_pct_zero_is_ng(self):
        errs = _errors("provisional", "manual", "n", op_pct=0.0)
        self.assertTrue(any("subject_op_pct=0 は禁止" in e for e in errs), errs)

    def test_op_pct_null_is_ok(self):
        self.assertEqual(_errors("provisional", "manual", "n", op_pct=None), [])

    def test_op_pct_over_150_is_ng(self):
        errs = _errors("provisional", "manual", "n", op_pct=200)
        self.assertTrue(any("150超" in e for e in errs), errs)


if __name__ == "__main__":
    unittest.main()
