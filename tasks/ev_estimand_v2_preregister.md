# 事前登録: EV estimand v2（§6計測仕様の1回改定）— R4（Codex R3残2件反映・SHA凍結対象版）

状態: R4（2026-08-01・R1=7→R2=4→R3=2ブロッカー全対応）。ゲート: ①ユーザー検収✅(2026-08-01「実装」) → ②Codex敵対レビューGO → ③実装・catalog §6付記追記・以後再凍結。
根拠裁定: decisions.md 2026-07-31「influx 凍結EV計算式のユーザー裁定3件」（割れ2=A案・割れ4=片側95%）。

## 目的

凍結EVの「点推定=全シグナルプール平均」と「CI=月ブロック・ブートストラップ」が別の量を指す内部不整合を是正し、
目標文言（§0付記II「超過EVの片側95%下限>0」）が要求する統計量を凍結メタデータとして持てるようにする。
**これは推定量の定義変更であり、新しい仮説検定ではない（α非消費）。**

## 変更内容（凍結案）

1. **EV点推定 v2 = two-stage（月等ウェイト）**: シグナルが1件以上ある各暦月（`month`列=シグナル月）の月内平均を先に取り、
   その月平均のグランド平均を EV とする。月内n=1の月はその1観測が月平均（正常扱い）。
2. **EV CI = 片側95%下限を正**: 月ブロック復元抽出（既存方式）で two-stage estimand を再計算し、
   片側95%下限 = `ci_level=0.90` の下側5パーセンタイル。両側95%は `ci_level=0.95` で別途算出し参考併記。
   n_boot=2000・seed=42 固定。
3. **コスト規約（R1ブロッカー1）**: `ret`（nostop）は**生値のため cost=0.003 を控除**。`ret_stop8` は
   **生成時に控除済み（kpi_event_study.py:222）のため cost=0.0**（二重控除禁止）。
4. **保存スキーマ（R2ブロッカー1/2反映・単一ネストキー・status必須）**: watchlist 各エントリの
   `in_sample` 直下に**キー `estimand_v2` 1個のみ**を追加する。
   - **算出時（status="computed"）の必須フィールド**: `{status, method, amendment_date, computed_at,
     months_spanned_none, months_spanned_stop8, n_boot, seed,
     n_used_none, n_excluded_nonfinite_none, ev_none_v2, ev_none_ci1s_low, ev_none_ci95_low, ev_none_ci95_high,
     n_used_stop8, n_excluded_nonfinite_stop8, ev_stop8_v2, ev_stop8_ci1s_low, ev_stop8_ci95_low, ev_stop8_ci95_high}`
     （件数・除外数・有効月数は **exit別に保存**＝有限値除外後の有効月集合がexitごとに異なり得るため。`reason` フィールドは禁止）
   - **未算出時（status="not_computed"）の必須フィールド**: `{status, reason, amendment_date, computed_at}`
     のみ（数値フィールドは全て禁止＝欠測を数値で偽装しない）
   - **既存キーは1つも変更・削除しない**（v1値は無傷保持）。
5. **適用範囲の機械ゲート（R2ブロッカー3反映・正本関数の一本化）**: 機械層と過程規則を分けて凍結する。
   - **機械層（コードで強制）**: v2 の計算正本は `kpi_event_study.py` に新設する
     **`ev_v2_summary(in_universe_df, ev_column, cost)`** 1関数のみ（two-stage点推定＋片側95%下限＋
     両側95%を一括返却）。台帳書込みスクリプト `scripts/ev_estimand_v2.py` と、改定日以降の新規陣入り
     判定のEV算出は**同一関数を import して使う**（Dual-Path禁止）。watchlist 読取側アクセサ
     **`admission_ev(entry)`** は estimand_v2 が無い/`status!="computed"` なら **例外を投げる**
     （v1への silent fallback 拒否・単体テストで検証）。
   - **過程規則（人間の運転規約・機械強制不能と明記）**: 改定日（`amendment_date`）以降に**新設される
     §7-\* 事前登録節**（注: 既存の§7-X/§7-Yは実在節のため「テンプレ」呼称を撤回・「新設節」と規定）は、
     運用開始ライン（§0枠S/枠F）のEV条件を **ev_v2_summary の値で記載する義務**を負う。
     改定日より前の trial・verdict は v1 のまま（遡及禁止）。
6. **原子的書込みと不変条件検査（R2ブロッカー4反映・表現訂正）**: 一時ファイル→再読込→**「新JSONから
   estimand_v2 キーを除去した後の JSON 構造・値が旧JSONと完全一致」の検査**→合格時のみ `os.replace`。
   不一致なら書込み拒否。（再シリアライズを伴うため byte 同一は要求しない＝旧「byte単位で無傷」表現は撤回。
   保証するのは**構造・値の同一性**）
7. **欠測・異常系（R1ブロッカー6・fail-closed）**: 「未算出」となる条件と理由コードを固定:
   `no_returns_csv`（実測3本: volshock_x_above200_quiet / pead_gap8_vol3 / sue_x_above200）/
   `missing_columns` / `empty_after_in_universe_filter` / `empty_after_nonfinite_filter`（R3指摘: 全値NaN/inf）。
   数値は**有限値のみ使用**し、NaN/inf は除外して exit別 `n_excluded_nonfinite_*` に記録。除外後0件も「未算出」。
8. **本文SHA凍結と結果の分離（R1ブロッカー7）**: 算出前に本ファイル本文の SHA256 を凍結し、
   v1→v2 比較表は**別ファイル `tasks/ev_estimand_v2_results.md`**（生成物・本文SHA・入力ファイルhash・
   生成時刻を冒頭に記録）へ出力する。本ファイルへの結果追記はしない。
9. **`bootstrap_ev_ci` の後方互換（R1ブロッカー5）**: 引数は**末尾追加** `month_equal_weight: bool = False`
   （既定False=v1挙動完全不変）。変更**前**の実挙動を固定fixture
   `tests/fixtures/bootstrap_ev_ci_v1_expected.json`（2026-08-01採取済み）として保存済みで、
   回帰テスト `tests/test_ev_estimand_v2.py` が省略時の全戻り値・seed再現性の完全一致を検証する。

## 禁止（凍結）

- v2 算出後の n_boot / seed / 月定義 / 片側水準 / コスト規約の変更（変えるなら v3 として再事前登録）
- v2 値を見てからの適用範囲・欠測規則の変更
- 本改定を理由とした過去 verdict の再裁定
- ev_v2_summary / admission_ev を経由しない新規陣入りEV算出・参照（機械層の迂回）
- 改定日以降の新設 §7-* 事前登録節に v1 EV を運用開始ライン条件として記載すること（過程規則違反）

## 成功基準

- returns.csv を持つ16系統全部に estimand_v2(status=computed) が付与され、estimand_v2除去後のJSON構造・値が旧と完全一致（不変条件検査PASS）
- 回帰テストPASS（bootstrap_ev_ci 省略時挙動が fixture と完全一致）
- 欠測3本が status=not_computed + reason 付きで記録され、数値フィールドを持たない
- `tasks/ev_estimand_v2_results.md` に v1→v2 比較表・本文SHA・入力hashが記録されている
