"""scripts/fdr_regime_sim.py 凍結審査R2（残3ブロッカー+MINOR4）・凍結審査R3（残1ブロッカー+MINOR3）
修正の契約テスト。

- R2-1: gate1同時信頼係数（4採用候補arm×22セル=88主張・z_{0.05/88}の実行時厳密計算）
- R2-2: LORD++閉形式γ系列の正規化（部分和+解析的尾部積分でΣγ_j≈1）
- R2-3: --decideのfail-closed化（期待セル集合完全一致・n_sim一致・spec/コードSHA-256一致・
  spec status==FROZEN。1つでも不成立ならdecision.jsonを出力せずFATAL終了）
- R3-1: summary.csvの完全性チェーン（本実行直後のSHA-256をmeta記録・--decideは現在の
  summary.csvと突き合わせ不一致ならFATAL）+ meta検証にrun_mode=='full'・seed一致を追加

台帳(data/kpi_trials/*)・config/paper_watchlist.json・カタログは一切参照しない
（本テストは合成spec/合成summary CSVのみを使い、実specファイルへの書き込みは一切行わない）。

実行:
  python3 tests/test_fdr_regime_sim.py
  docker compose run --rm xstock python -m unittest tests.test_fdr_regime_sim -v
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from scripts import fdr_regime_sim


# ============================================================================
# R2-1: gate1同時信頼係数
# ============================================================================

class TestGate1ZCritical(unittest.TestCase):
    def test_matches_normaldist_inv_cdf_directly(self):
        """gate1_z_critical(88)がstatistics.NormalDist().inv_cdf(1-0.05/88)の直接計算値と一致する
        （ハードコード値ではなく実行時計算であることの確認）。"""
        import statistics
        claims = 88
        expected = statistics.NormalDist().inv_cdf(1.0 - 0.05 / claims)
        actual = fdr_regime_sim.gate1_z_critical(claims)
        self.assertAlmostEqual(actual, expected, places=12)

    def test_z_0_05_88_near_expected_magnitude(self):
        """z_{0.05/88}が旧確定値z_{0.05/12}=2.6383より大きい（claims増加で臨界値が厳しくなる）
        こと、および概算値3.2〜3.3近傍であることを確認。"""
        z = fdr_regime_sim.gate1_z_critical(88)
        self.assertGreater(z, 2.6383)
        self.assertAlmostEqual(z, 3.25, delta=0.05)

    def test_claims_must_be_positive(self):
        with self.assertRaises(ValueError):
            fdr_regime_sim.gate1_z_critical(0)


# ============================================================================
# R2-2: γ系列正規化（解析的尾部積分）
# ============================================================================

class TestGammaSeriesNormalization(unittest.TestCase):
    def test_partial_sum_plus_analytic_tail_approx_one(self):
        """(a) Σ_{j<=200,000}γ_j + 解析的尾部 ≈ 1（誤差<1e-6）。"""
        series = fdr_regime_sim.LordPlusPlusGammaSeries()
        n = series.N_NORM
        partial_unnorm = float(np.sum(
            fdr_regime_sim._lord_pp_gamma_unnormalized(np.arange(1, n + 1, dtype=float))))
        tail_unnorm = fdr_regime_sim._lord_pp_gamma_tail_integral(float(n))
        total_normalized = (partial_unnorm + tail_unnorm) / series._z
        self.assertLess(abs(total_normalized - 1.0), 1e-6)

    def test_tail_formula_invariant_to_truncation_point(self):
        """尾部積分公式が任意の打ち切り点Nで同一の正規化定数Zを再現する（実装が内部で使う
        N_NORM=200,000だけでなく、別の打ち切り点N=500,000でも部分和+尾部=Zが成立することを
        確認し、公式自体の正しさをN_NORM値への依存から切り離して検証する）。"""
        series = fdr_regime_sim.LordPlusPlusGammaSeries()
        for n_alt in (50_000, 500_000):
            partial_unnorm = float(np.sum(
                fdr_regime_sim._lord_pp_gamma_unnormalized(np.arange(1, n_alt + 1, dtype=float))))
            tail_unnorm = fdr_regime_sim._lord_pp_gamma_tail_integral(float(n_alt))
            z_alt = partial_unnorm + tail_unnorm
            self.assertLess(abs(z_alt - series._z) / series._z, 1e-6, msg=f"n_alt={n_alt}")

    def test_nonnegative_and_nonincreasing(self):
        """(b) 非負・非増加（j=1から大きなjまで一貫して非増加であること）。"""
        series = fdr_regime_sim.LordPlusPlusGammaSeries()
        js = list(range(1, 3001)) + [10_000, 50_000, 200_000, 500_000, 2_000_000]
        values = [series(j) for j in js]
        self.assertTrue(all(v >= 0.0 for v in values))
        for i in range(1, len(values)):
            self.assertLessEqual(values[i], values[i - 1] + 1e-15,
                                  msg=f"j={js[i - 1]}->{js[i]}で非増加が破れている")

    def test_partial_sums_never_exceed_one(self):
        """(c) 部分和が1を超えない（単調増加かつ上限1に収束することを複数チェックポイントで確認）。"""
        series = fdr_regime_sim.LordPlusPlusGammaSeries()
        checkpoints = [1, 10, 100, 1_000, 10_000, 100_000, 200_000, 500_000, 1_000_000]
        prev = 0.0
        for m in checkpoints:
            cum = float(np.sum(
                fdr_regime_sim._lord_pp_gamma_unnormalized(np.arange(1, m + 1, dtype=float)))) / series._z
            self.assertLessEqual(cum, 1.0 + 1e-9, msg=f"m={m}で部分和が1を超過")
            self.assertGreaterEqual(cum, prev - 1e-12, msg=f"m={m}で部分和が単調増加でない")
            prev = cum


# ============================================================================
# R2-3: --decide のfail-closed化
# ============================================================================

GRID_P = [0.05, 0.1, 0.2]
GRID_LAM = [0, 5, 10]
GRID_MODE = ["conservative", "optimistic"]
FIXED_P = 0.1
FIXED_LAM = 10
WINNER_ARM = "a2_lord_async"


def _build_valid_spec(status: str = "FROZEN", n_sim: int = 100) -> dict:
    spec = {
        "_meta": {"status": status},
        "seed": 999,
        "n_sim": n_sim,
        "grid": {"p_true": GRID_P, "lambda_per_year": GRID_LAM, "n_eff_mode": GRID_MODE},
        "stress_scenarios": {"fixed_p_true": FIXED_P, "fixed_lambda_per_year": FIXED_LAM},
        "acceptance_criteria": {},
    }
    n_cells = len(fdr_regime_sim._expected_gate1_keys(spec))  # 22 (evaluable12+stress10)
    spec["acceptance_criteria"]["gate1_simultaneous_claims"] = len(fdr_regime_sim.CANDIDATE_ARMS) * n_cells
    return spec


def _build_valid_summary_rows(winner: str = WINNER_ARM) -> list:
    """gate1/gate2の期待セル集合を過不足なく満たす合成summary行を構築する。
    winnerのみgate1(fdr小)+gate2(true_disc大)を通過し、他candidateはgate1で確実に落ちるよう
    fdr_meanを高く設定する（機械判定の正しさを一意に検証するため）。"""
    rows = []
    lam_eval = [lam for lam in GRID_LAM if lam > 0]
    for arm in fdr_regime_sim.ARM_ORDER:
        for p in GRID_P:
            for lam in lam_eval:
                for mode in GRID_MODE:
                    if arm == winner:
                        fdr_mean, fdr_se, disc_mean = 0.01, 0.001, 5.0
                    elif arm == fdr_regime_sim.BASELINE_ARM:
                        fdr_mean, fdr_se, disc_mean = 0.05, 0.005, 2.0
                    else:
                        fdr_mean, fdr_se, disc_mean = 0.5, 0.05, 2.0
                    rows.append({
                        "variant": "main", "arm": arm, "scenario_kind": "grid",
                        "p_true": p, "lambda_per_year": lam, "n_eff_mode": mode, "stress_id": "",
                        "fdr_2030_mean": fdr_mean, "fdr_2030_se": fdr_se,
                        "true_disc_2030_mean": disc_mean,
                    })
        if arm in fdr_regime_sim.CANDIDATE_ARMS:
            for sid in fdr_regime_sim.STRESS_IDS:
                for mode in GRID_MODE:
                    fdr_mean, fdr_se = (0.01, 0.001) if arm == winner else (0.5, 0.05)
                    rows.append({
                        "variant": "main", "arm": arm, "scenario_kind": "stress",
                        "p_true": FIXED_P, "lambda_per_year": FIXED_LAM, "n_eff_mode": mode,
                        "stress_id": sid,
                        "fdr_2030_mean": fdr_mean, "fdr_2030_se": fdr_se,
                        "true_disc_2030_mean": float("nan"),
                    })
    return rows


class DecideFailClosedTestCase(unittest.TestCase):
    """--decideのfail-closed検証・共通ヘルパー。"""

    def _run_decide_case(self, spec: dict, rows: list, meta_overrides: dict = None,
                          tamper_rows_after_meta: list = None):
        tmpdir = Path(self._tmp.name)
        spec_path = tmpdir / "spec.json"
        summary_path = tmpdir / "summary.csv"
        meta_path = tmpdir / "summary.meta.json"

        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        pd.DataFrame(rows).to_csv(summary_path, index=False)

        code_path = Path(fdr_regime_sim.__file__).resolve()
        meta = {
            "run_mode": "full",
            "spec_path": str(spec_path),
            "spec_sha256": fdr_regime_sim._sha256_file(spec_path),
            "code_path": str(code_path),
            "code_sha256": fdr_regime_sim._sha256_file(code_path),
            "seed": spec["seed"],
            "n_sim": spec["n_sim"],
            # R3-1確定: main()の本実行がsummary.csv書き出し直後に記録するのと同じ値
            # （書き出し直後のファイル内容のSHA-256）をここでも記録する。
            "summary_sha256": fdr_regime_sim._sha256_file(summary_path),
            "generated_at": "2026-07-21T00:00:00+00:00",
        }
        if meta_overrides:
            meta.update(meta_overrides)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        if tamper_rows_after_meta is not None:
            # R3-1: meta.summary_sha256は「書き出し直後（改変前）」の内容を指したまま、
            # summary.csvだけ後から書き換える（本実行後の事後改変・差し替えを模擬する）。
            pd.DataFrame(tamper_rows_after_meta).to_csv(summary_path, index=False)

        args = argparse.Namespace(spec=str(spec_path), out_dir=str(tmpdir), summary_csv=str(summary_path))
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = fdr_regime_sim.run_decide(args)
        decision_path = tmpdir / "decision.json"
        return rc, decision_path, buf_err.getvalue()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()


class TestDecideFailClosedCases(DecideFailClosedTestCase):
    """欠損セル/重複/余剰/未知arm/n_sim不一致/status非FROZEN → 全てFATAL確認（decision.json不出力）。"""

    def test_missing_cell_is_fatal(self):
        spec = _build_valid_spec()
        rows = _build_valid_summary_rows()
        rows = [r for r in rows if not (
            r["arm"] == WINNER_ARM and r["scenario_kind"] == "grid"
            and r["p_true"] == 0.05 and r["lambda_per_year"] == 5 and r["n_eff_mode"] == "conservative"
        )]
        rc, decision_path, err = self._run_decide_case(spec, rows)
        self.assertEqual(rc, 1)
        self.assertFalse(decision_path.exists())
        self.assertIn("欠損セル", err)

    def test_duplicate_cell_is_fatal(self):
        spec = _build_valid_spec()
        rows = _build_valid_summary_rows()
        dup = next(r for r in rows if r["arm"] == WINNER_ARM and r["scenario_kind"] == "grid")
        rows.append(dict(dup))
        rc, decision_path, err = self._run_decide_case(spec, rows)
        self.assertEqual(rc, 1)
        self.assertFalse(decision_path.exists())
        self.assertIn("重複セル", err)

    def test_extra_cell_is_fatal(self):
        spec = _build_valid_spec()
        rows = _build_valid_summary_rows()
        rows.append({
            "variant": "main", "arm": WINNER_ARM, "scenario_kind": "grid",
            "p_true": 0.15, "lambda_per_year": 5, "n_eff_mode": "conservative", "stress_id": "",
            "fdr_2030_mean": 0.01, "fdr_2030_se": 0.001, "true_disc_2030_mean": 5.0,
        })
        rc, decision_path, err = self._run_decide_case(spec, rows)
        self.assertEqual(rc, 1)
        self.assertFalse(decision_path.exists())
        self.assertIn("未知/余剰セル", err)

    def test_unknown_arm_is_fatal(self):
        spec = _build_valid_spec()
        rows = _build_valid_summary_rows()
        rows.append({
            "variant": "main", "arm": "a6_ghost_arm", "scenario_kind": "grid",
            "p_true": 0.05, "lambda_per_year": 5, "n_eff_mode": "conservative", "stress_id": "",
            "fdr_2030_mean": 0.01, "fdr_2030_se": 0.001, "true_disc_2030_mean": 5.0,
        })
        rc, decision_path, err = self._run_decide_case(spec, rows)
        self.assertEqual(rc, 1)
        self.assertFalse(decision_path.exists())
        self.assertIn("未知のarm", err)

    def test_n_sim_mismatch_is_fatal(self):
        spec = _build_valid_spec(n_sim=100)
        rows = _build_valid_summary_rows()
        rc, decision_path, err = self._run_decide_case(spec, rows, meta_overrides={"n_sim": 101})
        self.assertEqual(rc, 1)
        self.assertFalse(decision_path.exists())
        self.assertIn("n_sim不一致", err)

    def test_status_not_frozen_is_fatal(self):
        spec = _build_valid_spec(status="FROZEN_CANDIDATE - PENDING CODEX R3 GO")
        rows = _build_valid_summary_rows()
        rc, decision_path, err = self._run_decide_case(spec, rows)
        self.assertEqual(rc, 1)
        self.assertFalse(decision_path.exists())
        self.assertIn("FROZENではない", err)

    def test_spec_sha256_mismatch_is_fatal(self):
        spec = _build_valid_spec()
        rows = _build_valid_summary_rows()
        rc, decision_path, err = self._run_decide_case(
            spec, rows, meta_overrides={"spec_sha256": "0" * 64})
        self.assertEqual(rc, 1)
        self.assertFalse(decision_path.exists())
        self.assertIn("spec SHA-256不一致", err)

    def test_code_sha256_mismatch_is_fatal(self):
        spec = _build_valid_spec()
        rows = _build_valid_summary_rows()
        rc, decision_path, err = self._run_decide_case(
            spec, rows, meta_overrides={"code_sha256": "0" * 64})
        self.assertEqual(rc, 1)
        self.assertFalse(decision_path.exists())
        self.assertIn("コードSHA-256不一致", err)


class TestDecideHappyPath(DecideFailClosedTestCase):
    """正常系: 合成CSVで機械判定が正しいarmを選ぶこと・decision.jsonが出力されること。"""

    def test_correct_arm_selected_and_decision_written(self):
        spec = _build_valid_spec()
        rows = _build_valid_summary_rows(winner=WINNER_ARM)
        rc, decision_path, err = self._run_decide_case(spec, rows)
        self.assertEqual(rc, 0, msg=f"stderr={err}")
        self.assertTrue(decision_path.exists())
        result = json.loads(decision_path.read_text(encoding="utf-8"))
        self.assertEqual(result["final_arm"], WINNER_ARM)
        self.assertEqual(result["passing_arms"], [WINNER_ARM])
        self.assertEqual(result["gate1_claims"], 88)
        for arm in fdr_regime_sim.CANDIDATE_ARMS:
            if arm == WINNER_ARM:
                self.assertTrue(result["arms"][arm]["both_pass"])
            else:
                self.assertFalse(result["arms"][arm]["both_pass"])
        # R2-3確定: SHA-256は切詰め禁止(常にフル64桁hex)
        for sha in result["input_files_sha256"].values():
            self.assertEqual(len(sha), 64)


# ============================================================================
# R3-1: summary.csvの完全性チェーン（凍結審査R3の唯一のブロッカー対応）
# ============================================================================

class TestDecideSummaryIntegrityChain(DecideFailClosedTestCase):
    """R3-1確定: summary.csvの完全性チェーン検証（本実行直後のSHA-256記録 vs 現在ファイルの
    再計算値の突き合わせ）+ meta検証拡張(run_mode/seed)の契約テスト。"""

    def test_summary_cell_tamper_after_meta_is_fatal(self):
        """(a) 本実行直後にメタへ記録されたSHA-256の後、summary.csvの数値セルを1つだけ
        改変すると、再計算したSHA-256がmeta記録値と一致せずFATALになること
        （decision.jsonは出力されない）。"""
        spec = _build_valid_spec()
        rows = _build_valid_summary_rows()
        tampered_rows = [dict(r) for r in rows]
        tampered_rows[0]["fdr_2030_mean"] = float(tampered_rows[0]["fdr_2030_mean"]) + 0.4321
        rc, decision_path, err = self._run_decide_case(
            spec, rows, tamper_rows_after_meta=tampered_rows)
        self.assertEqual(rc, 1)
        self.assertFalse(decision_path.exists())
        self.assertIn("summary CSV SHA-256不一致", err)

    def test_run_mode_smoke_is_fatal(self):
        """(b) meta.run_mode='smoke'（--smoke実行由来のsummary.csv）からの--decideはFATALに
        なること。"""
        spec = _build_valid_spec()
        rows = _build_valid_summary_rows()
        rc, decision_path, err = self._run_decide_case(
            spec, rows, meta_overrides={"run_mode": "smoke"})
        self.assertEqual(rc, 1)
        self.assertFalse(decision_path.exists())
        self.assertIn("run_mode", err)

    def test_seed_mismatch_is_fatal(self):
        """(c) meta.seedがspec.seedと不一致ならFATALになること。"""
        spec = _build_valid_spec()
        rows = _build_valid_summary_rows()
        rc, decision_path, err = self._run_decide_case(
            spec, rows, meta_overrides={"seed": spec["seed"] + 1})
        self.assertEqual(rc, 1)
        self.assertFalse(decision_path.exists())
        self.assertIn("seed不一致", err)

    def test_valid_summary_and_meta_pass(self):
        """(d) 正常系: 改変なしのsummary.csv・整合したmeta(summary_sha256/run_mode/seed全て
        一致)ならdecision.jsonが出力されること（R3-1で追加した3チェックが偽陽性を出さない
        ことの確認）。"""
        spec = _build_valid_spec()
        rows = _build_valid_summary_rows(winner=WINNER_ARM)
        rc, decision_path, err = self._run_decide_case(spec, rows)
        self.assertEqual(rc, 0, msg=f"stderr={err}")
        self.assertTrue(decision_path.exists())
        result = json.loads(decision_path.read_text(encoding="utf-8"))
        self.assertEqual(result["final_arm"], WINNER_ARM)
        self.assertEqual(len(result["meta"]["summary_sha256"]), 64)
        self.assertEqual(result["meta"]["run_mode"], "full")
        self.assertEqual(result["meta"]["seed"], spec["seed"])


if __name__ == "__main__":
    unittest.main()
