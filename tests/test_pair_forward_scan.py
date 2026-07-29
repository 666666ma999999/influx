"""pair_forward_scan の回帰テスト（Codex R3 の必須シナリオ・unittest自走）。

対象: 保有期間dedup（BLOCKER-2）・pre-startガードのresolve比較（BLOCKER-3）・
成熟待機条件（MAJOR-1）・ペア間の主イベント採否共有。
実行: python3 tests/test_pair_forward_scan.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pair_forward_scan as pfs  # noqa: E402

BDAYS = [f"202607{d:02d}" for d in range(1, 31)]  # 合成連番30営業日
BIDX = {d: i for i, d in enumerate(BDAYS)}


def prow(kpi, code, sd, status="closed", entry=None, exit_d=None, **kw):
    r = {"kpi_name": kpi, "code": code, "signal_date": sd, "status": status,
         "entry_date": entry, "exit_date": exit_d, "planned_entry_date": entry}
    r.update(kw)
    return r


class TestHoldPeriodDedup(unittest.TestCase):
    def test_blocks_second_signal_during_hold(self):
        """D採用（entry=D+1・満期D+21）→ D+2 の再発火は除外（Codexシナリオ1）。"""
        rows = [prow("three_up_ignition", "9999", "20260701", status="open", entry="20260702"),
                prow("three_up_ignition", "9999", "20260703", status="open", entry="20260704")]
        acc = pfs.accepted_ledger_mains(rows, "three_up_ignition", BIDX, BDAYS)
        self.assertIn(("9999", "20260701"), acc)
        self.assertNotIn(("9999", "20260703"), acc, "保有期間中の再発火が採用されている")

    def test_closed_time_exit_unblocks(self):
        """先行が time_exit で closed（exit=D+5）なら、それ以降の発火は採用。"""
        rows = [prow("three_up_ignition", "9999", "20260701", entry="20260702",
                     exit_d="20260706", exit_reason="time_exit"),
                prow("three_up_ignition", "9999", "20260707", status="open", entry="20260708")]
        acc = pfs.accepted_ledger_mains(rows, "three_up_ignition", BIDX, BDAYS)
        self.assertIn(("9999", "20260707"), acc)

    def test_stop_loss_does_not_release_early(self):
        """R4 BLOCKER-1: stop8早期決済（exit=D+3）でも nostop満期(entry+20bd)まで占有継続。"""
        rows = [prow("three_up_ignition", "9999", "20260701", entry="20260702",
                     exit_d="20260704", exit_reason="stop_loss"),
                prow("three_up_ignition", "9999", "20260707", status="open", entry="20260708")]
        acc = pfs.accepted_ledger_mains(rows, "three_up_ignition", BIDX, BDAYS)
        self.assertNotIn(("9999", "20260707"), acc, "stop8の早期exitで占有が解除されている")

    def test_stop_loss_with_nostop_exit_releases_at_nostop(self):
        """stop8でも exit_date_nostop 確定済みならその日以降は解除。"""
        rows = [prow("three_up_ignition", "9999", "20260701", entry="20260702",
                     exit_d="20260704", exit_reason="stop_loss", exit_date_nostop="20260710"),
                prow("three_up_ignition", "9999", "20260711", status="open", entry="20260712")]
        acc = pfs.accepted_ledger_mains(rows, "three_up_ignition", BIDX, BDAYS)
        self.assertIn(("9999", "20260711"), acc)

    def test_entry_missing_does_not_block(self):
        """entry_missing 行は採用もブロックもしない（Codexシナリオ3）。"""
        rows = [prow("three_up_ignition", "9999", "20260701", status="entry_missing"),
                prow("three_up_ignition", "9999", "20260703", status="open", entry="20260704")]
        acc = pfs.accepted_ledger_mains(rows, "9999" and "three_up_ignition", BIDX, BDAYS)
        self.assertNotIn(("9999", "20260701"), acc)
        self.assertIn(("9999", "20260703"), acc)

    def test_other_kpi_rows_ignored(self):
        rows = [prow("volshock_5x", "9999", "20260701", status="open", entry="20260702")]
        acc = pfs.accepted_ledger_mains(rows, "three_up_ignition", BIDX, BDAYS)
        self.assertEqual(acc, {})

    def test_shared_acceptance_is_deterministic(self):
        """P2/P3（同一主KPI）で使う採否集合が同一入力から同一に再現される。"""
        rows = [prow("turnover_rank_surge", "9999", "20260701", status="open", entry="20260702"),
                prow("turnover_rank_surge", "9999", "20260703", status="open", entry="20260704")]
        a1 = pfs.accepted_ledger_mains(rows, "turnover_rank_surge", BIDX, BDAYS)
        a2 = pfs.accepted_ledger_mains(rows, "turnover_rank_surge", BIDX, BDAYS)
        self.assertEqual(set(a1), set(a2))


class TestGuards(unittest.TestCase):
    def test_pre_start_guard_rejects_relative_path(self):
        """相対パス指定でも本番台帳をresolveで検知しFATAL（BLOCKER-3）。"""
        rel = Path("data/paper_trades/pair_forward_ledger.jsonl")
        import os
        cwd = os.getcwd()
        os.chdir(ROOT)
        try:
            with self.assertRaises(SystemExit):
                pfs.check_pre_start_guard(rel, allow_pre_start=True)
        finally:
            os.chdir(cwd)

    def test_pre_start_guard_allows_scratch(self):
        pfs.check_pre_start_guard(Path("/tmp/nonexistent_pair_smoke.jsonl"), allow_pre_start=True)

    def test_guard_noop_without_flag(self):
        pfs.check_pre_start_guard(Path("data/paper_trades/pair_forward_ledger.jsonl"),
                                  allow_pre_start=False)


class TestMaturityGate(unittest.TestCase):
    def test_closed_without_nostop_waits(self):
        """closedでも ret_nostop 未確定なら成熟させない（MAJOR-1・Codexシナリオ）。"""
        row = prow("three_up_ignition", "9999", "20260701",
                   ret_net=-0.083, ret_nostop=None, ret_e1=0.01)
        self.assertFalse(pfs.ledger_outcome_ready(row))

    def test_all_numeric_ready(self):
        row = prow("three_up_ignition", "9999", "20260701",
                   ret_net=-0.083, ret_nostop=0.02, ret_e1=0.01)
        self.assertTrue(pfs.ledger_outcome_ready(row))

    def test_open_not_ready(self):
        row = prow("three_up_ignition", "9999", "20260701", status="open",
                   ret_net=None, ret_nostop=None, ret_e1=None)
        self.assertFalse(pfs.ledger_outcome_ready(row))


class TestH52Classification(unittest.TestCase):
    """R4 MAJOR-2: classify_h52_candidate の終端分類。"""

    ACC = {("8888", "20260701"): {"in_universe": True, "exit_date": "20260725",
                                  "entry_date": "20260702", "ret": 0.05, "ret_stop8": 0.05,
                                  "defer_bdays": 0}}

    def test_accepted_in_universe_matures(self):
        ev, out = pfs.classify_h52_candidate("8888", "20260701", self.ACC)
        self.assertEqual(ev, "matured")
        self.assertEqual(out["primary_ret"], 0.05)

    def test_accepted_out_of_universe(self):
        acc = {("7777", "20260701"): {"in_universe": False, "exit_date": "20260725"}}
        ev, out = pfs.classify_h52_candidate("7777", "20260701", acc)
        self.assertEqual(ev, "rejected_out_of_universe")
        self.assertIsNone(out)

    def test_duplicate_during_hold(self):
        """先行採用(exit=07/25)の保有期間中 D+2 の再発火は rejected_duplicate。"""
        ev, out = pfs.classify_h52_candidate("8888", "20260703", self.ACC)
        self.assertEqual(ev, "rejected_duplicate")

    def test_entry_missing_when_no_prior(self):
        ev, out = pfs.classify_h52_candidate("6666", "20260703", self.ACC)
        self.assertEqual(ev, "rejected_entry_missing")

    def test_p7_p8_share_same_terminal(self):
        """P7/P8は同一 h52_accepted を共有するため、同じ主キーは同じ終端。"""
        r1 = pfs.classify_h52_candidate("8888", "20260703", self.ACC)
        r2 = pfs.classify_h52_candidate("8888", "20260703", dict(self.ACC))
        self.assertEqual(r1, r2)

    def test_order_invariance(self):
        """辞書の構築順によらず分類が一致（決定性）。"""
        acc_a = {("8888", "20260701"): self.ACC[("8888", "20260701")],
                 ("8888", "20260710"): {"in_universe": True, "exit_date": "20260728"}}
        acc_b = dict(reversed(list(acc_a.items())))
        for key in [("8888", "20260703"), ("8888", "20260711"), ("8888", "20260629")]:
            self.assertEqual(pfs.classify_h52_candidate(key[0], key[1], acc_a),
                             pfs.classify_h52_candidate(key[0], key[1], acc_b))


if __name__ == "__main__":
    unittest.main(verbosity=2)
