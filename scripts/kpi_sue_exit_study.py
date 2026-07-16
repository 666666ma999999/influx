#!/usr/bin/env python3
"""SUE exit補完スタディ（第21周・カタログ§7-J事前登録・凍結）。

主候補 sue_x_above200（§7-H H1確定値・n=516）はEV実測が「損切りなし・20営業日後終値手仕舞い」
のみのため、枠F運用配線前に2種類の代替exitを補完する:

    T1 sue_x_above200_exit_stop8: C1固定-8%損切り
        （第12周Canonical `kpi_exit_study._walk_static_stop` を import 再利用・定義の再実装禁止）
    T2 sue_x_above200_exit_e1: E1シナリオ崩壊（保有中の終値SMA200割れ→翌営業日寄付手仕舞い・
        `kpi_exit_study._walk_e1_scenario_break` を import 再利用）

母集団は `output/kpi/sue_beat/returns.csv` の in_universe 行と
`output/kpi/sue_champion_composite/signals_features_h1.csv` を (signal_date, code) で内部結合し、
n=516（§7-H確定値）を FATAL アサートで再現してから実行する。

primary exit の事前固定（凍結・§7-J）: ペーパートレード期間中の primary exit は
**nostop（20営業日後終値手仕舞い）に固定**する。stop8/E1 は secondary endpoint として並走記録
するのみであり、本スクリプトの結果（in-sample EV点推定）でexitを選ぶ「winner selection」は
行わない。exit の変更は「paired ΔEV の95%CI下限>0（Holm補正後）」を満たした場合のみ、
将来の評価期間から適用する運用ルール（本スクリプトはこの判定材料を記録するのみ）。

Canonical Module再利用（新規ロジックは母集団結合・paired ΔEVブートストラップ・
Holm手順・E1内生性診断のみ）:
- 価格履歴プリロード・exitウォーク: `kpi_exit_study.py`（build_price_history /
  _walk_static_stop / _walk_e1_scenario_break / _time_based_exit / _earliest_bars_date /
  SCAN_BUFFER_BDAYS / MA200_WINDOW / TRIGGERED_REASONS）をそのまま再利用。
  同ファイルのPOPULATIONS辞書・既存関数の改変は一切行わない。
- EVブートストラップ・母集団統計・判定・台帳追記: `kpi_event_study.py`
  （bootstrap_ev_ci / compute_stats / judge / append_trial / load_base_rate_by_month）を
  そのまま再利用。同ファイルの改変も一切行わない。

Usage:
    docker compose run --rm xstock python scripts/kpi_sue_exit_study.py
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: now_jst を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: bootstrap_ev_ci/compute_stats/judge/append_trial等を再利用)
import kpi_exit_study  # noqa: E402  (Canonical Module: exitウォーク関数群を再利用。POPULATIONS/既存関数は改変しない)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読み込み・ROUND_TRIP_COSTを再利用)

# --- 本ラウンド固有パラメータ（§7-J事前登録仕様・凍結） --------------------------
SUE_BEAT_RETURNS = Path("output/kpi/sue_beat/returns.csv")
H1_SIGNALS = Path("output/kpi/sue_champion_composite/signals_features_h1.csv")
EXPECTED_N = 516  # §7-H確定値・§7-J「母集団（凍結）」再現保証
PERIOD = ("2016-11", "2022-11")  # in-sample。holdout(2023年以降)は開封しない

ROUND_TRIP_COST = measure_base_rate.ROUND_TRIP_COST
FORWARD_WINDOW_BD = measure_base_rate.FORWARD_WINDOW_BD

OUTPUT_DIR = Path("output/kpi/sue_exit_study")
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21

FAMILY_CI_LEVEL = 1 - 0.05 / 63  # §6 2026-07-11運用化ルール準拠（Bonferroni実計算と同じ流儀・63試行）
FAMILY_N_BOOT = 10000  # 既定N_BOOTSTRAP(1000)では極端分位を解像できないため（§7-J凍結記載どおり）

HOLM_SMALL_P_CI_LEVEL = 0.975  # Holm step-down: 小さい方のp値の比較に97.5%CI
HOLM_LARGE_P_CI_LEVEL = 0.95  # Holm step-down: 他方に95%CI


# --- 母集団再現（FATALアサート・§7-Hと同じ母集団再現保証） -----------------------


def load_population() -> pd.DataFrame:
    """sue_beat/returns.csv の in_universe 行と sue_champion_composite の H1 シグナルを
    (signal_date, code) で内部結合し、n=516（§7-H確定値）を FATAL アサートで再現する。

    `kpi_exit_study.load_positions()` は 'ret'（nostop基準リターン）・'ret_stop8'（stop8基準の
    独立クロスチェック用）列を落として signal_date/code/month/regime/entry_date/entry_price の
    6列だけを返す設計のため、本ラウンドの主要統計量に必要なこれらの列を保持するには直接読み込む
    必要がある（load_positions自体の改変・再実装ではなく、同一のin_universeフィルタを列選択だけ
    変えて適用する最小限の分岐）。
    """
    if not SUE_BEAT_RETURNS.exists():
        raise SystemExit(f"FATAL: {SUE_BEAT_RETURNS} が見つかりません")
    if not H1_SIGNALS.exists():
        raise SystemExit(f"FATAL: {H1_SIGNALS} が見つかりません")

    rb = pd.read_csv(
        SUE_BEAT_RETURNS,
        dtype={
            "signal_date": str, "code": str, "month": str, "regime": str,
            "entry_date": str, "exit_date": str, "universe_month_used": str,
        },
    )
    rb = rb[rb["in_universe"] == True].reset_index(drop=True)  # noqa: E712 (CSV読込のbool比較)

    h1 = pd.read_csv(H1_SIGNALS, dtype={"signal_date": str, "code": str})

    merged = rb.merge(h1[["signal_date", "code"]], on=["signal_date", "code"], how="inner")
    if len(merged) != EXPECTED_N:
        raise SystemExit(
            f"FATAL: 母集団再現に失敗しました（期待 n={EXPECTED_N}・実際 n={len(merged)}）。"
            f"§7-J凍結母集団の定義と sue_beat/sue_champion_composite 出力の整合を確認してください。"
        )
    # 全列を保持する（h1側は結合キー2列しか持たないため新規列は増えない）。
    # measure_base_rate.summarize が要求する mfe/mae/touch_-*/mfe20/mfe30 等の列を
    # kpi_event_study.compute_stats（母集団統計・§7-H確定値との整合確認に使用）に渡すために必要。
    return merged.copy()


# --- exitウォーク（kpi_exit_study.py の既存関数を再利用） -----------------------


def _last_seen_day(fwd_days: list[str], price_series: dict) -> str:
    """上場廃止等の欠損判定用の下限（`kpi_exit_study.walk_position` 内のインライン処理と同一
    ロジックの複製）。kpi_exit_study.py側はこの部分を独立関数化しておらず、同ファイルの改変が
    禁止されているためここに複製する（Canonical Module原則の例外・kpi_event_study.py の
    compute_forward_return_deferred と同じ性質の意図的複製）。
    """
    last_seen_day = fwd_days[0]
    for d in fwd_days[1:]:
        rec = price_series.get(d)
        if rec is not None and rec.get("AdjC") is not None:
            last_seen_day = d
    return last_seen_day


def walk_exits(
    positions: pd.DataFrame, all_bdays: list[str], bday_index: dict[str, int],
    prices: dict, sma200_by_code: dict,
) -> pd.DataFrame:
    """各ポジションについてT1(C1固定-8%損切り)・T2(E1シナリオ崩壊)のexitを1回の日次パスで
    計算する（kpi_exit_study._walk_static_stop / _walk_e1_scenario_break / _time_based_exit を
    import再利用。定義の再実装はしない）。
    """
    rows = []
    for pos in positions.itertuples(index=False):
        entry_idx = bday_index[pos.entry_date]
        exit_idx_max = entry_idx + FORWARD_WINDOW_BD
        fwd_days = all_bdays[entry_idx: exit_idx_max + 1]
        price_series = prices.get(pos.code, {})
        sma_series = sma200_by_code.get(pos.code, {})

        last_seen_day = _last_seen_day(fwd_days, price_series)
        time_exit = kpi_exit_study._time_based_exit(fwd_days, price_series)

        # C1定義: kpi_exit_study.walk_position の "entry_price * 0.92" と同一閾値
        stop8_threshold = pos.entry_price * (1 - 0.08)
        stop8_exit = kpi_exit_study._walk_static_stop(
            fwd_days, price_series, stop8_threshold, last_seen_day, time_exit
        )

        prev_day = all_bdays[entry_idx - 1]
        e1_exit = kpi_exit_study._walk_e1_scenario_break(
            fwd_days, price_series, sma_series, prev_day, time_exit
        )

        row = {
            "signal_date": pos.signal_date, "code": pos.code, "month": pos.month, "regime": pos.regime,
            "entry_date": pos.entry_date, "entry_price": pos.entry_price, "exit_date_nostop": pos.exit_date,
            "ret": pos.ret,  # nostop基準（既存returns.csvのret列。コスト未調整・§7-J凍結定義）
            "ret_stop8_existing": pos.ret_stop8,  # 独立クロスチェック用（既存列・コスト調整済み）
        }
        for tag, (exit_date, exit_price, reason) in (("stop8", stop8_exit), ("e1", e1_exit)):
            if exit_price is None:
                raise SystemExit(
                    f"FATAL: code={pos.code} entry={pos.entry_date} exit={tag} でexit_priceが計算できませんでした"
                )
            ret_raw = exit_price / pos.entry_price - 1
            row[f"exit_date_{tag}"] = exit_date
            row[f"exit_price_{tag}"] = exit_price
            row[f"reason_{tag}"] = reason
            row[f"ret_raw_{tag}"] = ret_raw
            row[f"ret_costadj_{tag}"] = ret_raw - ROUND_TRIP_COST
            row[f"holding_days_{tag}"] = bday_index[exit_date] - entry_idx
            # paired ΔEV基礎列。cost は両辺（当該exit・nostop）で同一のROUND_TRIP_COSTが相殺するため
            # 生リターンの差分でコスト調整後の差分と完全に一致する。
            row[f"diff_{tag}"] = ret_raw - pos.ret
        rows.append(row)
    return pd.DataFrame(rows)


# --- paired ΔEVブートストラップ・Holm手順（新規実装・§7-J凍結記載の手順） -------


def _p_value_boundary(df: pd.DataFrame, diff_col: str, n_boot: int = 1000, seed: int = 42) -> float:
    """paired ΔEVがゼロを跨ぐ境界のCI水準から両側p値相当を求める（§7-J Holm手順の定義:
    「CIの被覆水準を段階的に上げてゼロを跨ぐ境界水準から算出」の実装）。

    「両側p値相当」なので、点推定が正・負いずれの方向でも扱えるよう「0がCI区間の外側にあるか
    （下限>0 または 上限<0）」で判定する（下限>0のみのチェックは点推定が負の場合に常にp=1へ
    縮退してしまい、Holm順位付けの対象にならないバグを生むため両側判定にしている）。

    `kpi_event_study.bootstrap_ev_ci` はシード固定のため、ci_level を変えて繰り返し呼んでも
    再標本列自体は同一で percentile の読み出し位置だけが変わる（ブートストラップの再実装ではなく、
    既存関数を異なる ci_level で繰り返し呼ぶ二分探索）。
    """
    def excludes_zero(level: float) -> Optional[bool]:
        r = kpi_event_study.bootstrap_ev_ci(df, ev_column=diff_col, cost=0.0, n_boot=n_boot, seed=seed, ci_level=level)
        lo, hi = r["ci_low"], r["ci_high"]
        if lo is None or hi is None:
            return None
        return (lo > 0) or (hi < 0)

    lo, hi = 1e-6, 0.999999
    e_lo = excludes_zero(lo)
    if e_lo is None or not e_lo:
        return 1.0  # 最小水準（ほぼ点推定=中央値相当）でも0を排除できない -> 有意性なし(p=1として扱う)
    e_hi = excludes_zero(hi)
    if e_hi:
        return 1.0 - hi  # 最大水準でも0を排除したまま -> 非常に強い有意性。上限値として返す
    for _ in range(40):
        mid = (lo + hi) / 2
        e_mid = excludes_zero(mid)
        if e_mid:
            lo = mid
        else:
            hi = mid
    return 1.0 - lo


def compute_holm(df: pd.DataFrame) -> dict:
    """T1(stop8)・T2(e1)のpaired ΔEVについてHolm step-down手順（§7-J凍結）を適用する。

    手順: 2比較のブートストラップ両側p値相当を小さい順に並べ、最小p値の比較には97.5%CI・
    もう一方には95%CIを適用し、それぞれの下限>0を「Holm補正後CI下限>0」と判定する。
    """
    p_stop8 = _p_value_boundary(df, "diff_stop8")
    p_e1 = _p_value_boundary(df, "diff_e1")
    order = sorted([("stop8", p_stop8), ("e1", p_e1)], key=lambda kv: kv[1])
    ci_level_by_name = {order[0][0]: HOLM_SMALL_P_CI_LEVEL, order[1][0]: HOLM_LARGE_P_CI_LEVEL}
    result = {}
    for name, p in (("stop8", p_stop8), ("e1", p_e1)):
        level = ci_level_by_name[name]
        r = kpi_event_study.bootstrap_ev_ci(df, ev_column=f"diff_{name}", cost=0.0, n_boot=1000, seed=42, ci_level=level)
        result[name] = {
            "p_value_boundary": p,
            "holm_ci_level": level,
            "holm_ci_low": r["ci_low"],
            "holm_ci_high": r["ci_high"],
            "holm_ci_low_gt_zero": bool(r["ci_low"] is not None and r["ci_low"] > 0),
        }
    return result


# --- E1内生性診断（新規実装・§7-J凍結記載の必須項目） ---------------------------


def compute_e1_diagnostics(df: pd.DataFrame, bday_index: dict[str, int]) -> dict:
    """E1の内生性診断（入口dev200>0と出口SMA200割れが同一状態変数を共有するための分離報告）。

    avoided_loss_rate / loss_turned_nonnegative_rate / worsened_rate は発動群内で
    ret_raw_e1（E1実現リターン）と ret（nostop反実仮想リターン）を直接比較する
    （Codexレビュー指摘: 単に nostop<0 の割合を数えるだけではE1が実際に損失を縮小したかを
    確認できない。E1実現値との比較に修正・2026-07-11）。
    """
    triggered_mask = df["reason_e1"] == "cross_exit"  # kpi_exit_study.TRIGGERED_REASONS のE1版
    triggered_n = int(triggered_mask.sum())
    nostop_holding_days = df["exit_date_nostop"].map(bday_index) - df["entry_date"].map(bday_index)
    trig = df.loc[triggered_mask]
    nostop_loss = trig["ret"] < 0
    return {
        "triggered_n": triggered_n,
        "triggered_rate": triggered_n / len(df),
        "avg_holding_days_triggered": (
            float(trig["holding_days_e1"].mean()) if triggered_n else None
        ),
        "avg_holding_days_all_e1": float(df["holding_days_e1"].mean()),
        "avg_holding_days_nostop": float(nostop_holding_days.mean()),
        "counterfactual_nostop_mean_ret_triggered": (
            float(trig["ret"].mean()) if triggered_n else None
        ),
        "dropped_winner_rate": (
            float((trig["ret"] >= 0.20).mean()) if triggered_n else None
        ),
        "avoided_loss_rate": (
            float(((trig["ret_raw_e1"] > trig["ret"]) & nostop_loss).mean()) if triggered_n else None
        ),
        "loss_turned_nonnegative_rate": (
            float((nostop_loss & (trig["ret_raw_e1"] >= 0)).mean()) if triggered_n else None
        ),
        "worsened_rate": (
            float((trig["ret_raw_e1"] < trig["ret"]).mean()) if triggered_n else None
        ),
        "avg_paired_delta_ev_triggered": (
            float(trig["diff_e1"].mean()) if triggered_n else None
        ),
    }


# --- レポート出力 ---------------------------------------------------------------


def _fmt_pct(x: Optional[float]) -> str:
    return f"{x:.3%}" if x is not None else "-"


def _fmt_ci(lo: Optional[float], hi: Optional[float]) -> str:
    if lo is None or hi is None:
        return "[-, -]"
    return f"[{lo:.3%}, {hi:.3%}]"


def write_report(
    path: Path, n: int, pop_stats: dict, ev: dict, sens: dict, diff: dict, holm: dict,
    e1_diag: dict, stop8_diag: dict, cross_check: dict,
) -> None:
    lines = [
        "# SUE exit補完スタディ 比較レポート（第21周・カタログ§7-J事前登録・凍結）",
        "",
        f"生成日時: {jq_fetch.now_jst().isoformat()}",
        f"検証期間: {PERIOD[0]} 〜 {PERIOD[1]}（in-sample。holdout 2023年以降はロック中・本レポートの対象外）",
        f"母集団: n={n}（`output/kpi/sue_beat/returns.csv` in_universe行 ∩ "
        f"`output/kpi/sue_champion_composite/signals_features_h1.csv`、signal_date+codeキー結合。"
        f"§7-H確定値 sue_x_above200 と同一集合）",
        "",
        "> **primary exitの事前固定（§7-J凍結）**: ペーパートレード期間中の primary exit は "
        "**nostop（20営業日後終値手仕舞い）に固定**する。stop8/E1 は secondary endpoint として "
        "並走記録するのみであり、以下のEV点推定・paired ΔEVの大小をもって「推奨」「最善」等の "
        "優劣ラベルを付けない。exitの変更は paired ΔEV の95%CI下限>0（Holm補正後）を満たした "
        "場合のみ、次回評価期間から適用する。",
        "",
        "## 母集団統計（§7-H確定値との整合確認）",
        f"- リフト倍率（点推定） = {pop_stats['point_lift']:.4f} / "
        f"95%CI = [{pop_stats['ci_low']:.4f}, {pop_stats['ci_high']:.4f}]",
        f"- 月分散 = {pop_stats['months_spanned']} / bull含む={pop_stats['has_bull']} / "
        f"bear含む={pop_stats['has_bear']}",
        "",
        "## ①各exitのEV点推定 + 95%CI（月次ブロック・ブートストラップ、既定N_BOOTSTRAP=1000）",
        "",
        "| exit | EV点推定(コスト込) | 95%CI |",
        "|---|---|---|",
        f"| nostop（primary・固定） | {_fmt_pct(ev['nostop']['point_ev'])} | "
        f"{_fmt_ci(ev['nostop']['ci_low'], ev['nostop']['ci_high'])} |",
        f"| T1 stop8（C1固定-8%損切り） | {_fmt_pct(ev['stop8']['point_ev'])} | "
        f"{_fmt_ci(ev['stop8']['ci_low'], ev['stop8']['ci_high'])} |",
        f"| T2 e1（シナリオ崩壊） | {_fmt_pct(ev['e1']['point_ev'])} | "
        f"{_fmt_ci(ev['e1']['ci_low'], ev['e1']['ci_high'])} |",
        "",
        f"## ②family-wise感度CI（α=0.05/63・ci_level={FAMILY_CI_LEVEL:.6f}・n_boot={FAMILY_N_BOOT}）",
        "",
        f"- T1 stop8: 感度CI = {_fmt_ci(sens['stop8']['ci_low'], sens['stop8']['ci_high'])}",
        f"- T2 e1: 感度CI = {_fmt_ci(sens['e1']['ci_low'], sens['e1']['ci_high'])}",
        "",
        "## ③paired ΔEV（当該exit − nostop・同一銘柄ペア）+ Holm手順判定",
        "",
        "| 比較 | ΔEV点推定 | 95%CI | p値境界(両側相当) | Holm CI水準 | Holm補正後CI | 下限>0 |",
        "|---|---|---|---|---|---|---|",
        f"| stop8 − nostop | {_fmt_pct(diff['stop8']['point_ev'])} | "
        f"{_fmt_ci(diff['stop8']['ci_low'], diff['stop8']['ci_high'])} | "
        f"{holm['stop8']['p_value_boundary']:.4f} | {holm['stop8']['holm_ci_level']:.3f} | "
        f"{_fmt_ci(holm['stop8']['holm_ci_low'], holm['stop8']['holm_ci_high'])} | "
        f"{holm['stop8']['holm_ci_low_gt_zero']} |",
        f"| e1 − nostop | {_fmt_pct(diff['e1']['point_ev'])} | "
        f"{_fmt_ci(diff['e1']['ci_low'], diff['e1']['ci_high'])} | "
        f"{holm['e1']['p_value_boundary']:.4f} | {holm['e1']['holm_ci_level']:.3f} | "
        f"{_fmt_ci(holm['e1']['holm_ci_low'], holm['e1']['holm_ci_high'])} | "
        f"{holm['e1']['holm_ci_low_gt_zero']} |",
        "",
        "## ④E1内生性診断",
        "",
        f"- 発動件数（reason=cross_exit） = {e1_diag['triggered_n']} / 発動率 = "
        f"{_fmt_pct(e1_diag['triggered_rate'])}",
        f"- 平均保有日数（発動分のみ） = "
        f"{e1_diag['avg_holding_days_triggered']:.1f}日" if e1_diag['triggered_n'] else "- 平均保有日数（発動分のみ） = -（発動0件）",
        f"- 平均保有日数（E1全体） = {e1_diag['avg_holding_days_all_e1']:.1f}日 / "
        f"平均保有日数（nostop・参考） = {e1_diag['avg_holding_days_nostop']:.1f}日",
        f"- E1発動銘柄の nostop 反実仮想リターン平均 = "
        f"{_fmt_pct(e1_diag['counterfactual_nostop_mean_ret_triggered'])}",
        f"- dropped-winner率（E1発動後、nostopなら+20%到達していた率） = "
        f"{_fmt_pct(e1_diag['dropped_winner_rate'])}",
        f"- avoided-loss率（E1発動・nostop損失かつE1実現リターン>nostop反実仮想リターンの率。"
        "単なるnostop<0の割合ではなくE1が実際に損失を縮小したかを見る） = "
        f"{_fmt_pct(e1_diag['avoided_loss_rate'])}",
        f"- loss-turned-nonnegative率（nostop損失だったがE1実現リターンが0以上に転じた率） = "
        f"{_fmt_pct(e1_diag['loss_turned_nonnegative_rate'])}",
        f"- 悪化率（E1実現リターン<nostop反実仮想リターンの率） = "
        f"{_fmt_pct(e1_diag['worsened_rate'])}",
        f"- 発動群のpaired ΔEV平均（E1実現−nostop反実仮想、コスト相殺済み） = "
        f"{_fmt_pct(e1_diag['avg_paired_delta_ev_triggered'])}",
        "",
        "## T1(stop8)発動統計（参考・kpi_exit_study.TRIGGERED_REASONS基準）",
        f"- 発動件数 = {stop8_diag['triggered_n']} / 発動率 = {_fmt_pct(stop8_diag['triggered_rate'])}",
        "",
        "## 独立クロスチェック",
        f"- T1(stop8)ウォーク結果 vs `sue_beat/returns.csv` 既存 `ret_stop8` 列（同一定義・"
        "同一entry_price/entry_date前提での理論上の完全一致）: "
        f"最大絶対誤差 = {cross_check['max_abs_diff']:.2e}（{cross_check['n_mismatch']}件が"
        f"1e-9超で不一致）",
        "",
        "## 実装ノート",
        "- exitウォークは `scripts/kpi_exit_study.py`（第12周Canonical・改変禁止）の "
        "`_walk_static_stop`/`_walk_e1_scenario_break`/`_time_based_exit`/`build_price_history` を"
        "そのままimport再利用",
        "- EVブートストラップ・母集団統計は `scripts/kpi_event_study.py`（改変禁止）の "
        "`bootstrap_ev_ci`/`compute_stats` をそのまま再利用",
        "- paired ΔEVは当該exitのret_raw − nostopのret_raw（両者ともROUND_TRIP_COST控除前）の差分。"
        "コストは両辺で同一額が相殺するため、コスト調整後の差分と数学的に完全一致する",
        "- Holm手順のp値境界は `bootstrap_ev_ci` を異なる ci_level で繰り返し呼ぶ二分探索で求めた"
        "近似値（新規のブートストラップ再実装ではなく、固定シードの既存関数呼び出しの繰り返し）",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# --- 台帳追記（primary=nostop固定・EV最大選択の禁止を明記） ----------------------


def append_trial_record(
    kpi_name: str, ev_this: dict, sens_this: dict, diff_this: dict, holm_this: dict,
    n: int, pop_stats: dict, e1_diag: Optional[dict], stop8_diag: Optional[dict], exit_label: str,
) -> None:
    stats_for_judge = {
        "n": n,
        "months_spanned": pop_stats["months_spanned"],
        "has_bull": pop_stats["has_bull"],
        "has_bear": pop_stats["has_bear"],
        "ci_low": pop_stats["ci_low"],
        "ev_stop8": ev_this["point_ev"],
        "avg_monthly_n": pop_stats["avg_monthly_n"],
    }
    verdict, _reasons = kpi_event_study.judge(stats_for_judge)

    params = {
        "catalog_ref": "docs/stock-algo-kpi-catalog.md §7-J（2026-07-11事前登録・凍結）",
        "source_population": "sue_x_above200 H1（§7-H確定値・n=516）",
        "population_definition": (
            "output/kpi/sue_beat/returns.csv(in_universe=True) ∩ "
            "output/kpi/sue_champion_composite/signals_features_h1.csv "
            "(signal_date+codeキー結合)"
        ),
        "exit_rule": exit_label,
        "primary_exit_fixed": (
            "primary exitはnostop（20営業日後終値手仕舞い）に固定（§7-J凍結）。"
            "本行はsecondary endpointとして並走記録するのみで、EV最大選択によるexit変更・"
            "「推奨」「最善」等の優劣ラベル付けは行わない。"
        ),
        "ev_point": ev_this["point_ev"],
        "ev_ci95_low": ev_this["ci_low"],
        "ev_ci95_high": ev_this["ci_high"],
        "ev_ci_n_boot_valid": ev_this["n_boot_valid"],
        "family_wise_sensitivity_ci_level": FAMILY_CI_LEVEL,
        "family_wise_sensitivity_ci_low": sens_this["ci_low"],
        "family_wise_sensitivity_ci_high": sens_this["ci_high"],
        "family_wise_sensitivity_n_boot": FAMILY_N_BOOT,
        "paired_delta_ev_vs_nostop_point": diff_this["point_ev"],
        "paired_delta_ev_vs_nostop_ci95_low": diff_this["ci_low"],
        "paired_delta_ev_vs_nostop_ci95_high": diff_this["ci_high"],
        "holm_p_value_boundary": holm_this["p_value_boundary"],
        "holm_ci_level": holm_this["holm_ci_level"],
        "holm_ci_low": holm_this["holm_ci_low"],
        "holm_ci_high": holm_this["holm_ci_high"],
        "holm_ci_low_gt_zero": holm_this["holm_ci_low_gt_zero"],
        "round": "21_sue_exit_study",
        "multi_trial_discount_note": (
            "本ラウンド(第21周・SUE exit補完)はT1(stop8)・T2(e1)の2試行同時登録であり"
            "累積試行数割引の対象。個々の結果単独で運用変更しない。"
        ),
    }
    if e1_diag is not None:
        params["e1_endogeneity_diagnostics"] = e1_diag
    if stop8_diag is not None:
        params["stop8_trigger_diagnostics"] = stop8_diag

    record = {
        "run_id": uuid.uuid4().hex,
        "ts": jq_fetch.now_jst().isoformat(),
        "kpi_name": kpi_name,
        "params": params,
        "period": {"start": PERIOD[0], "end": PERIOD[1]},
        "n": n,
        "lift": pop_stats["point_lift"],
        "ci_low": pop_stats["ci_low"],
        "ci_high": pop_stats["ci_high"],
        "ev": ev_this["point_ev"],
        "verdict": verdict,
        "entry_mode": "reused_from_existing_returns_csv",
        "regime_filter": None,
    }
    kpi_event_study.append_trial(record, TRIALS_PATH)


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    positions = load_population()
    print(f"母集団再現OK: n={len(positions)}（期待値={EXPECTED_N}）", file=sys.stderr)

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    codes = set(positions["code"])
    entry_indices = [bday_index[e] for e in positions["entry_date"]]
    earliest_idx = bday_index[kpi_exit_study._earliest_bars_date()]
    min_idx, max_idx = min(entry_indices), max(entry_indices)
    scan_start_idx = max(earliest_idx, min_idx - kpi_exit_study.SCAN_BUFFER_BDAYS)
    scan_end_idx = min(len(all_bdays) - 1, max_idx + FORWARD_WINDOW_BD + 5)
    scan_days = all_bdays[scan_start_idx: scan_end_idx + 1]

    print(
        f"価格履歴プリロード開始: 銘柄数={len(codes)} 走査日数={len(scan_days)} "
        f"({scan_days[0]}〜{scan_days[-1]})", file=sys.stderr,
    )
    prices, sma200_by_code = kpi_exit_study.build_price_history(codes, scan_days)
    print("価格履歴プリロード完了", file=sys.stderr)

    detail_df = walk_exits(positions, all_bdays, bday_index, prices, sma200_by_code)

    detail_path = OUTPUT_DIR / "positions.csv"
    detail_path.parent.mkdir(parents=True, exist_ok=True)
    detail_df.to_csv(detail_path, index=False)
    print(f"positions -> {detail_path} (n={len(detail_df)})", file=sys.stderr)

    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    # compute_stats(measure_base_rate.summarize経由)はmfe/mae/touch_-*等の元列を要求するため、
    # ここでは元の全列を保持した positions（load_populationの返り値）を渡す（detail_dfは
    # exitウォーク専用に組み立てた最小列セットのため対象外）。
    pop_stats = kpi_event_study.compute_stats(positions, base_rate_by_month, PERIOD)
    print(
        f"母集団統計（H1再現・cross-check用）: lift={pop_stats['point_lift']:.4f} "
        f"CI95=[{pop_stats['ci_low']:.4f},{pop_stats['ci_high']:.4f}]", file=sys.stderr,
    )

    # ①各exitのEV点推定 + 95%CI
    ev = {
        "nostop": kpi_event_study.bootstrap_ev_ci(detail_df, ev_column="ret", cost=ROUND_TRIP_COST),
        "stop8": kpi_event_study.bootstrap_ev_ci(detail_df, ev_column="ret_raw_stop8", cost=ROUND_TRIP_COST),
        "e1": kpi_event_study.bootstrap_ev_ci(detail_df, ev_column="ret_raw_e1", cost=ROUND_TRIP_COST),
    }

    # ②family-wise感度CI
    sens = {
        "stop8": kpi_event_study.bootstrap_ev_ci(
            detail_df, ev_column="ret_raw_stop8", cost=ROUND_TRIP_COST,
            ci_level=FAMILY_CI_LEVEL, n_boot=FAMILY_N_BOOT,
        ),
        "e1": kpi_event_study.bootstrap_ev_ci(
            detail_df, ev_column="ret_raw_e1", cost=ROUND_TRIP_COST,
            ci_level=FAMILY_CI_LEVEL, n_boot=FAMILY_N_BOOT,
        ),
    }

    # ③paired ΔEV + Holm手順
    diff = {
        "stop8": kpi_event_study.bootstrap_ev_ci(detail_df, ev_column="diff_stop8", cost=0.0),
        "e1": kpi_event_study.bootstrap_ev_ci(detail_df, ev_column="diff_e1", cost=0.0),
    }
    holm = compute_holm(detail_df)

    # ④E1内生性診断（参考: T1発動統計も併記）
    e1_diag = compute_e1_diagnostics(detail_df, bday_index)
    stop8_triggered_mask = detail_df["reason_stop8"].isin(kpi_exit_study.TRIGGERED_REASONS)
    stop8_diag = {
        "triggered_n": int(stop8_triggered_mask.sum()),
        "triggered_rate": float(stop8_triggered_mask.mean()),
    }

    # 独立クロスチェック: T1(stop8)ウォーク結果 vs 既存 ret_stop8 列
    abs_diff = (detail_df["ret_costadj_stop8"] - detail_df["ret_stop8_existing"]).abs()
    cross_check = {
        "max_abs_diff": float(abs_diff.max()),
        "n_mismatch": int((abs_diff > 1e-9).sum()),
    }
    print(
        f"クロスチェック(stop8 vs 既存ret_stop8列): 最大絶対誤差={cross_check['max_abs_diff']:.2e} "
        f"不一致件数={cross_check['n_mismatch']}/{len(detail_df)}", file=sys.stderr,
    )

    report_path = OUTPUT_DIR / "report.md"
    write_report(
        report_path, len(detail_df), pop_stats, ev, sens, diff, holm, e1_diag, stop8_diag, cross_check,
    )
    print(f"report: {report_path}")

    append_trial_record(
        "sue_x_above200_exit_stop8", ev["stop8"], sens["stop8"], diff["stop8"], holm["stop8"],
        len(detail_df), pop_stats, e1_diag=None, stop8_diag=stop8_diag, exit_label="T1: C1固定-8%損切り",
    )
    append_trial_record(
        "sue_x_above200_exit_e1", ev["e1"], sens["e1"], diff["e1"], holm["e1"],
        len(detail_df), pop_stats, e1_diag=e1_diag, stop8_diag=None, exit_label="T2: E1シナリオ崩壊",
    )
    print(f"台帳追記完了: sue_x_above200_exit_stop8 / sue_x_above200_exit_e1 -> {TRIALS_PATH}")

    print(f"\n=== SUE exit補完スタディ (n={len(detail_df)}) ===")
    print(f"  nostop: EV={ev['nostop']['point_ev']:.3%} CI95={_fmt_ci(ev['nostop']['ci_low'], ev['nostop']['ci_high'])}")
    print(f"  T1 stop8: EV={ev['stop8']['point_ev']:.3%} CI95={_fmt_ci(ev['stop8']['ci_low'], ev['stop8']['ci_high'])}")
    print(f"  T2 e1: EV={ev['e1']['point_ev']:.3%} CI95={_fmt_ci(ev['e1']['ci_low'], ev['e1']['ci_high'])}")
    print(f"  paired ΔEV stop8-nostop: {diff['stop8']['point_ev']:.3%} Holm下限>0={holm['stop8']['holm_ci_low_gt_zero']}")
    print(f"  paired ΔEV e1-nostop: {diff['e1']['point_ev']:.3%} Holm下限>0={holm['e1']['holm_ci_low_gt_zero']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
