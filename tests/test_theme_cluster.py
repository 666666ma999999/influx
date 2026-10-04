"""テーマ初動 v0（tasks/theme_cluster_v0.md）の回帰テスト（unittest 自走・実データに触れない）。

生成器 kpi_theme_cluster_signals.generate_theme_cluster_signals:
  (a) 受益 4 社中 3 社が対TOPIX +4pt → 発火し受益銘柄全部（4 行）が出る・n_hits=3/n_ben=4
  (b) 3 社未満（2 社）→ 非発火
  (c) 3 割未満（11 社中 3 社 = 27%）→ 非発火／10 社中 3 社（30%）→ 発火（境界は ≥）
  (d) 分割日（AdjFactor≠1）の銘柄は分子から外れ、3 社目が分割日なら非発火
  (e) 再発火 5 営業日: 発火の 3 営業日後は出ない・6 営業日後は出る・走査開始前の発火でも抑止される
  (f) bars / TOPIX が欠ける日は判定せず diag に件数
  (g) daily_screen 分岐: FROZEN_START 前の signal_date を落とし (signal_date, code) で畳む
x_shortage_map.x_backing:
  (h) posted_at（UTC）を JST 日に直して数える・status_id 重複除去・前 14 日平均・ベース不足・初出語・代表投稿 40 字

合成暦: 2026-10-01 からの平日 20 営業日（祝日なし）。TOPIX は毎日 1000 固定（＝超過 = 銘柄の騰落率）。
実行: docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_theme_cluster
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import jq_fetch  # noqa: E402
import kpi_theme_cluster_signals as T  # noqa: E402
import measure_base_rate  # noqa: E402
import x_shortage_map as X  # noqa: E402


def make_bdays(start: str = "20261001", n: int = 20) -> list[str]:
    d = dt.date(int(start[:4]), int(start[4:6]), int(start[6:]))
    out: list[str] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += dt.timedelta(days=1)
    return out


BDAYS = make_bdays()
BIDX = {d: i for i, d in enumerate(BDAYS)}


def subject_map(n_ben: int = 4, subject_id: str = "t-subj") -> dict:
    """受益 n_ben 社（code 1001..）の subject 1 つ＋受益 2 社の subject（母集団外）。"""
    ben = [{"code": str(1000 + k), "name": f"社{k}", "tier": "provisional"} for k in range(1, n_ben + 1)]
    ben.append({"code": "1999", "name": "却下社", "tier": "rejected"})  # tier=rejected は数えない
    small = [{"code": "3001", "tier": "confirmed"}, {"code": "3002", "tier": "confirmed"}]
    return {
        "subjects": [
            {"id": subject_id, "label": "テスト題材", "queries": ["q1", "q2"], "discover_tokens": ["語A"],
             "beneficiaries": ben},
            {"id": "small", "label": "2社", "queries": ["q9"], "beneficiaries": small},
        ]
    }


class GeneratorBase(unittest.TestCase):
    """合成 bars/TOPIX を DATA_ROOT に書き、x_shortage_map.load を差し替える。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "bars").mkdir()
        self.p_root = mock.patch.object(jq_fetch, "DATA_ROOT", self.root)
        self.p_root.start()
        measure_base_rate.load_bars_day.cache_clear()
        self.prices: dict[str, dict[str, float]] = {}   # day -> code5 -> AdjC
        self.splits: set[tuple[str, str]] = set()        # (day, code5) で AdjFactor=0.5
        self.n_ben = 4
        self.topix_days = list(BDAYS)

    def tearDown(self):
        self.p_root.stop()
        measure_base_rate.load_bars_day.cache_clear()
        self.tmp.cleanup()

    def codes(self) -> list[str]:
        return [str(1000 + k) + "0" for k in range(1, self.n_ben + 1)]

    def up(self, day: str, n_up: int, pct: float = 4.0) -> None:
        """day に先頭 n_up 社を pct% 上げる（他は横ばい 1000）。"""
        for i, c in enumerate(self.codes()):
            self.prices.setdefault(day, {})[c] = 1000.0 * (1 + pct / 100) if i < n_up else 1000.0

    def write(self, skip_bars: set[str] = frozenset()) -> None:
        for d in BDAYS:
            if d in skip_bars:
                continue
            recs = []
            for c in self.codes():
                adj_c = self.prices.get(d, {}).get(c, 1000.0)
                recs.append({"Code": c, "AdjO": adj_c, "AdjC": adj_c,
                             "AdjFactor": 0.5 if (d, c) in self.splits else 1.0})
            with gzip.open(self.root / "bars" / f"{d}.json.gz", "wt", encoding="utf-8") as fh:
                json.dump({"data": recs}, fh)
        topix = [{"Date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "O": 1000.0, "C": 1000.0} for d in self.topix_days]
        with gzip.open(self.root / "topix.json.gz", "wt", encoding="utf-8") as fh:
            json.dump({"data": topix}, fh)

    def run_gen(self, start: str, end: str):
        with mock.patch.object(X, "load", return_value=subject_map(self.n_ben)):
            return T.generate_theme_cluster_signals(start, end, BDAYS, BIDX)


class TestFire(GeneratorBase):
    def test_a_three_of_four_fires_all_beneficiaries(self):
        self.up(BDAYS[3], 3)
        self.write()
        df, diag = self.run_gen(BDAYS[1], BDAYS[6])
        self.assertEqual(list(df.columns), ["signal_date", "code", "subject", "n_hits", "n_ben"])
        self.assertEqual(len(df), 4)                      # 受益銘柄全部（却下社は含まない）
        self.assertEqual(set(df["signal_date"]), {BDAYS[3]})
        self.assertEqual(set(df["code"]), set(self.codes()))
        self.assertEqual(set(df["subject"]), {"t-subj"})
        self.assertEqual((df["n_hits"].iloc[0], df["n_ben"].iloc[0]), (3, 4))
        self.assertEqual(diag["subjects_eligible"], 1)   # 受益 2 社の subject は母集団外
        self.assertEqual(diag["fires_total"], 1)

    def test_b_two_hits_no_fire(self):
        self.up(BDAYS[3], 2)
        self.write()
        df, diag = self.run_gen(BDAYS[1], BDAYS[6])
        self.assertTrue(df.empty)
        self.assertEqual(diag["fires_total"], 0)

    def test_c_ratio_boundary(self):
        self.n_ben = 11                                   # 3/11 = 27% < 30% → 非発火
        self.up(BDAYS[3], 3)
        self.write()
        df, _ = self.run_gen(BDAYS[1], BDAYS[6])
        self.assertTrue(df.empty)
        self.n_ben = 10                                   # 3/10 = 30% → 発火（境界は ≥）
        self.prices.clear()
        self.up(BDAYS[3], 3)
        self.write()
        measure_base_rate.load_bars_day.cache_clear()
        df, _ = self.run_gen(BDAYS[1], BDAYS[6])
        self.assertEqual(len(df), 10)

    def test_d_split_day_excluded(self):
        self.up(BDAYS[3], 3)
        self.splits.add((BDAYS[3], self.codes()[0]))      # 3 社目（先頭）が分割日 → 分子 2
        self.write()
        df, diag = self.run_gen(BDAYS[1], BDAYS[6])
        self.assertTrue(df.empty)
        self.assertEqual(diag["split_day_exclusions"], 1)

    def test_e_refire_suppression(self):
        self.up(BDAYS[2], 3)
        self.up(BDAYS[5], 3)                              # 3 営業日後 → 抑止
        self.up(BDAYS[8], 3)                              # 6 営業日後 → 出る
        self.write()
        df, diag = self.run_gen(BDAYS[1], BDAYS[10])
        self.assertEqual(sorted(set(df["signal_date"])), [BDAYS[2], BDAYS[8]])
        self.assertEqual(diag["fires_refire_suppressed"], 1)
        # 走査開始を BDAYS[4] にしても BDAYS[2] の発火が見えて BDAYS[5] は出ない
        df2, diag2 = self.run_gen(BDAYS[4], BDAYS[6])
        self.assertTrue(df2.empty)
        self.assertEqual(diag2["fires_before_start"], 1)

    def test_f_missing_bars_or_topix_not_judged(self):
        self.up(BDAYS[3], 3)
        self.up(BDAYS[12], 3)
        self.topix_days = [d for d in BDAYS if d != BDAYS[12]]  # TOPIX 欠落日
        self.write(skip_bars={BDAYS[3]})                       # bars 欠落日
        df, diag = self.run_gen(BDAYS[1], BDAYS[15])
        self.assertTrue(df.empty)
        self.assertEqual(diag["days_skipped_bars_missing"], 2)  # BDAYS[3] 自身と翌日（前日欠落）
        self.assertEqual(diag["days_skipped_topix_missing"], 2)  # BDAYS[12] 自身と翌日
        self.assertEqual(diag["business_days_scanned"], 15 - 4)

    def test_f2_empty_bars_file_counts_as_missing(self):
        """bars ファイルはあるが中身が空（data: []）の日は欠落扱い（当日欠落・前日欠落の両方）。"""
        self.up(BDAYS[3], 3)
        self.up(BDAYS[7], 3)
        self.write()
        for d in (BDAYS[3], BDAYS[6]):   # BDAYS[3]=当日が空／BDAYS[6]=BDAYS[7] の前日が空
            with gzip.open(self.root / "bars" / f"{d}.json.gz", "wt", encoding="utf-8") as fh:
                json.dump({"data": []}, fh)
        df, diag = self.run_gen(BDAYS[1], BDAYS[10])
        self.assertTrue(df.empty)
        self.assertEqual(diag["days_skipped_bars_missing"], 4)   # [3],[4](前日空),[6],[7](前日空)
        self.assertEqual(diag["business_days_scanned"], 10 - 4)


class TestDailyScreenBranch(unittest.TestCase):
    def test_g_frozen_start_and_dedupe(self):
        import daily_screen as ds  # noqa: WPS433  (import が重いのでテスト内で)

        raw = pd.DataFrame([
            {"signal_date": "20261001", "code": "10010", "subject": "a", "n_hits": 3, "n_ben": 4},
            {"signal_date": "20261006", "code": "10010", "subject": "a", "n_hits": 3, "n_ben": 4},
            {"signal_date": "20261006", "code": "10010", "subject": "b", "n_hits": 3, "n_ben": 3},
            {"signal_date": "20261006", "code": "10020", "subject": "b", "n_hits": 3, "n_ben": 3},
        ])
        entry = {"kpi_name": "theme_cluster_v0", "params": {}}
        with mock.patch.object(T, "generate_theme_cluster_signals", return_value=(raw, {})):
            out = ds.generate_kpi_signals(entry, "20261001", "20261006", {}, BIDX, BDAYS)
        self.assertEqual(list(out.columns), ["signal_date", "code"])
        self.assertEqual(sorted(map(tuple, out.values.tolist())), [("20261006", "10010"), ("20261006", "10020")])
        self.assertEqual(ds.THEME_CLUSTER_KPI_NAME, "theme_cluster_v0")
        self.assertEqual(T.FROZEN_START, "20261005")

    def test_g2_universe_exempt_and_append_all_beneficiaries(self):
        """受益銘柄全部が台帳に残る: ユニバース免除の宣言＋append_new_signals が全行を追記する。"""
        import daily_screen as ds  # noqa: WPS433

        self.assertIn("theme_cluster_v0", ds.UNIVERSE_EXEMPT_KPI_NAMES)
        self.assertNotIn("three_up_ignition", ds.UNIVERSE_EXEMPT_KPI_NAMES)
        df = pd.DataFrame([{"signal_date": "20261006", "code": c} for c in ("10010", "10020", "99340", "74560")])
        entry = {"kpi_name": "theme_cluster_v0", "defer_entry": True, "max_defer_bdays": 3}
        records: list[dict] = []
        added = ds.append_new_signals(records, entry, df, BIDX, BDAYS, run_id="t")
        self.assertEqual(added, 4)
        self.assertEqual(sorted(r["code"] for r in records), ["10010", "10020", "74560", "99340"])
        self.assertTrue(all(r["planned_entry_date"] == "20261007" for r in records))


class TestRecoCell(unittest.TestCase):
    def test_md_cell_escapes_pipe_and_newline(self):
        import build_daily_reco as B  # noqa: WPS433

        self.assertEqual(B.md_cell("DRAM | NAND\n供給"), "DRAM ｜ NAND 供給")
        info = {"label": "L", "queries": [{"query_id": "q", "today": 1, "base_mean": 2.0, "base_days": 7, "base_ok": True}],
                "first_seen_tokens": [], "sample": "a|b"}
        self.assertEqual(B.md_cell(B.format_x_backing(info)), "L: q=1/2.0／初出=なし／「a｜b」")


class TestXBacking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.texts = Path(self.tmp.name) / "texts"
        self.texts.mkdir()
        self.queue = Path(self.tmp.name) / "discovery_queue.jsonl"
        self.m = subject_map()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_texts(self, name: str, rows: list[dict]) -> None:
        (self.texts / f"{name}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def test_h_counts_first_seen_and_sample(self):
        day = "20261002"
        rows = [
            # 10-01 16:00Z = 10-02 01:00 JST → 当日に数える
            {"query_id": "q1", "status_id": "s1", "text": "  当日の\n投稿 " + "あ" * 50, "posted_at": "2026-10-01T16:00:00Z"},
            {"query_id": "q1", "status_id": "s1", "text": "重複", "posted_at": "2026-10-01T16:00:00Z"},  # 重複除去
            {"query_id": "q1", "status_id": "s2", "text": "二本目", "posted_at": "2026-10-02T03:00:00+00:00"},
            # 10-02 15:30Z = 10-03 00:30 JST → 当日ではない
            {"query_id": "q1", "status_id": "s3", "text": "翌日", "posted_at": "2026-10-02T15:30:00Z"},
            {"query_id": "q9", "status_id": "s4", "text": "別 subject", "posted_at": "2026-10-02T03:00:00Z"},
        ]
        # q1 のベース: 前 14 日のうち 8 日に投稿（各日 2 件・うち 1 日は 4 件）→ 平均 (7*2+4)/8 = 2.25
        for k in range(1, 9):
            d = dt.date(2026, 10, 2) - dt.timedelta(days=k)
            n = 4 if k == 1 else 2
            for j in range(n):
                rows.append({"query_id": "q1", "status_id": f"b{k}-{j}", "text": "base",
                             "posted_at": f"{d.isoformat()}T03:00:00Z"})
        # q2 のベース: 3 日だけ → ベース不足
        for k in (2, 5, 20):   # 20 日前は 14 日の外
            d = dt.date(2026, 10, 2) - dt.timedelta(days=k)
            rows.append({"query_id": "q2", "status_id": f"c{k}", "text": "base", "posted_at": f"{d.isoformat()}T03:00:00Z"})
        self._write_texts("2026-10-02", rows[:3])
        self._write_texts("2026-10-03", rows[3:])
        queue_rows = [
            {"type": "candidate_batch", "date": "2026-10-03", "candidates": [
                {"token": "語A", "first_seen": True}, {"token": "可能性", "first_seen": True},
                {"token": "語A", "first_seen": True}]},                                  # ±1 日・重複は 1 回
            {"type": "candidate_batch", "date": "2026-10-02", "candidates": [{"token": "語A", "first_seen": False}]},
            {"type": "candidate_batch", "date": "2026-10-05", "candidates": [{"token": "語A", "first_seen": True}]},  # 窓外
            {"type": "other", "date": "2026-10-02"},
        ]
        self.queue.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in queue_rows), encoding="utf-8")

        info = X.x_backing(self.m, "t-subj", day, texts_dir=self.texts, queue_path=self.queue)
        q1, q2 = info["queries"]
        self.assertEqual((q1["query_id"], q1["today"], q1["base_days"], q1["base_ok"]), ("q1", 2, 8, True))
        self.assertAlmostEqual(q1["base_mean"], 2.25)
        self.assertEqual((q2["query_id"], q2["today"], q2["base_days"], q2["base_ok"]), ("q2", 0, 2, False))
        self.assertEqual(info["first_seen_tokens"], ["語A"])
        self.assertEqual(info["sample"], ("当日の 投稿 " + "あ" * 50)[:40])
        self.assertEqual(len(info["sample"]), 40)
        self.assertEqual(info["label"], "テスト題材")

    def test_h2_no_texts_no_queue(self):
        info = X.x_backing(self.m, "t-subj", "20261002", texts_dir=self.texts / "none", queue_path=self.queue)
        self.assertIsNone(info["sample"])
        self.assertEqual(info["first_seen_tokens"], [])
        self.assertFalse(info["queries"][0]["base_ok"])
        self.assertEqual(info["n_texts_files"], 0)
        with self.assertRaises(KeyError):
            X.x_backing(self.m, "unknown", "20261002", texts_dir=self.texts, queue_path=self.queue)


if __name__ == "__main__":
    unittest.main()
