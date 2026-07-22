#!/usr/bin/env python3
"""記述的診断: 既存KPIレシピの過去シグナルが、entry_dateから7営業日以内に+6%へタッチする確率。

============================================================================
位置づけ（厳守）: 本スクリプトは**記述的診断**であり、選抜・合否判定を一切行わない。
data/kpi_trials/* (試行台帳) には不算入。config/* / docs/*（stock-algo-kpi-catalog.md 等）
のカタログ定義は一切変更しない。新規作成物はこのファイル自身と output/kpi_touch67/ 配下のみ。

参考観測・合否判定なし。この結果で閾値を調整して同一期間に再検定することは §8-6 により
禁止。正式化は新規事前登録（Codex凍結ゲート）経由。
============================================================================

対象: output/kpi/*/returns.csv を持つ全KPI（HOLDOUTディレクトリは名前で除外・さらに
signal_date を in-sample期間 [2016-11, 2022-11] へ日付でも絞り込む二重ガード。理由:
margin_release のように非HOLDOUT名でも2023年以降の行を含むreturns.csvが実在するため）。
in_universe==True 行のみを対象にする。

各シグナルの entry_date・entry_price は returns.csv の既存列をそのまま使う（再計算しない）。
data/jquants/bars/ の日足 AdjH/AdjC/AdjO/AdjL から、entry_date を1日目として7営業日以内に
高値が entry_price×1.06 以上へ到達したか（touch_6pct_7bd）を計算する。
measure_base_rate.py の Canonical Module（load_bars_day / all_business_days /
load_calendar_days）をそのまま再利用する（再実装しない）。

主読み出し（ユーザー指定・事前固定）: P(touch +6% within 7bd)。
文脈として同時集計する固定セット（グリッド漁り禁止・この4項目のみ）:
    ① タッチ到達日数の中央値
    ② タッチ後に7bd終値が+6%未満へ垂れた率（「触るが持たない」率）
    ③ 参考エグジット試算: +6%指値、未達なら7bd終値で手仕舞い（コスト0.3%控除・損切りなし）のEVと勝率
    ④ 同試算で-4%ストップ併用版（同日に両方触れうる場合はストップ優先＝保守的仮定）
※③④は参考であり、いかなる採否判定にも使わない。

Usage:
    docker compose run --rm xstock python scripts/kpi_touch_rate_diag.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import measure_base_rate  # noqa: E402  (Canonical Module: load_bars_day/all_business_days/load_calendar_days を再利用)

KPI_ROOT = Path("output/kpi")
OUTPUT_DIR = Path("output/kpi_touch67")

# §6凍結カタログの in-sample 期間定義をそのまま踏襲（変更しない）。holdout(2023+)はここで機械的に除外。
IN_SAMPLE_START = 20161101
IN_SAMPLE_END = 20221130

TOUCH_WINDOW_BD = 7  # entry_date を1日目として何営業日以内か
TOUCH_TARGET = 0.06  # +6%
ROUND_TRIP_COST = measure_base_rate.ROUND_TRIP_COST  # 0.3%（既存カタログ規約を再利用）
STOP_LEVEL_SIM4 = 0.04  # ④の参考ストップ幅 -4%

DISCLAIMER = (
    "参考観測・合否判定なし・この結果で閾値を調整して同一期間に再検定することは"
    "§8-6により禁止・正式化は新規事前登録（Codex凍結ゲート）経由"
)


def discover_kpi_dirs() -> list[Path]:
    """output/kpi/*/returns.csv を持つ全KPIディレクトリを返す（HOLDOUT系は名前でも除外＝二重ガードの1つ目）。"""
    dirs = []
    for p in sorted(KPI_ROOT.iterdir()):
        if not p.is_dir():
            continue
        if "HOLDOUT" in p.name.upper():
            continue
        if (p / "returns.csv").exists():
            dirs.append(p)
    return dirs


def load_in_sample_signals(returns_path: Path) -> pd.DataFrame:
    """in_universe==True かつ signal_date が in-sample期間内の行のみ返す（二重ガードの2つ目＝日付）。"""
    df = pd.read_csv(returns_path, usecols=["signal_date", "code", "in_universe", "entry_date", "entry_price"])
    df = df[df["in_universe"] == True]  # noqa: E712 (bool列との明示比較で意図を明確化)
    df = df[(df["signal_date"] >= IN_SAMPLE_START) & (df["signal_date"] <= IN_SAMPLE_END)]
    return df.sort_values("entry_date")  # load_bars_day の lru_cache 局所性を活かすため日付順に処理する


def compute_touch_for_signal(
    code: str,
    entry_date: str,
    entry_price: float,
    bday_index: dict[str, int],
    all_bdays: list[str],
) -> Optional[dict]:
    """1シグナル分のタッチ判定・参考exitシムを計算する。

    Returns:
        touch_6pct_7bd(0/1) / touch_day(1-based, 未タッチはNone) / day7_close /
        reverted_flag(0/1, 未タッチはNone) / ret_sim3 / win_sim3(0/1) / ret_sim4 / win_sim4(0/1)。
        entry_date がカレンダー範囲外の場合は None（呼び出し側でskip集計）。
    """
    if entry_date not in bday_index:
        return None
    idx = bday_index[entry_date]
    end_idx = idx + TOUCH_WINDOW_BD - 1
    if end_idx >= len(all_bdays):
        raise SystemExit(
            f"FATAL: entry_date={entry_date} の{TOUCH_WINDOW_BD}営業日後がカレンダー範囲外です"
            f"（カレンダーキャッシュの延長が必要）。"
        )
    fwd_days = all_bdays[idx : end_idx + 1]  # 長さ7: index0=1日目(entry_date)〜index6=7日目

    target_price = entry_price * (1 + TOUCH_TARGET)
    stop_price_sim4 = entry_price * (1 - STOP_LEVEL_SIM4)

    touch_day: Optional[int] = None
    touch_fill_price: Optional[float] = None
    sim4_exit_price: Optional[float] = None

    last_seen_close = entry_price  # 窓内で一度もAdjCが取れない場合のフォールバック
    for offset, d in enumerate(fwd_days, start=1):
        bars = measure_base_rate.load_bars_day(d)
        rec = bars.get(code)
        if rec is None:
            continue  # 売買停止等の欠損日はスキップ（compute_forward_return_for_codeと同型の扱い）
        ao, ah, al, ac = rec.get("AdjO"), rec.get("AdjH"), rec.get("AdjL"), rec.get("AdjC")
        if ac is not None:
            last_seen_close = ac

        # --- 主読み出し: +6%タッチ判定（高値AdjHで判定） ---
        if touch_day is None and ah is not None and ah >= target_price:
            touch_day = offset
            # ギャップアップで寄り付きから既に目標超えなら寄り値、そうでなければ指値そのもの
            touch_fill_price = ao if (ao is not None and ao >= target_price) else target_price

        # --- ④参考: 同日にストップ/利確どちらも触れうる場合はストップ優先（保守的仮定） ---
        if sim4_exit_price is None:
            if ao is not None and ao <= stop_price_sim4:
                sim4_exit_price = ao
            elif al is not None and al <= stop_price_sim4:
                sim4_exit_price = stop_price_sim4
            elif ao is not None and ao >= target_price:
                sim4_exit_price = ao
            elif ah is not None and ah >= target_price:
                sim4_exit_price = target_price

    day7_close = last_seen_close  # 7日目終値（上場廃止等はwindow内最終既知終値で代替）

    # --- ③参考: 損切りなし。+6%指値、未達なら7bd終値で手仕舞い ---
    exit_price_sim3 = touch_fill_price if touch_fill_price is not None else day7_close
    ret_sim3 = (exit_price_sim3 / entry_price - 1) - ROUND_TRIP_COST

    # --- ④参考: ③に-4%ストップを併用 ---
    if sim4_exit_price is None:
        sim4_exit_price = day7_close
    ret_sim4 = (sim4_exit_price / entry_price - 1) - ROUND_TRIP_COST

    touch_flag = int(touch_day is not None)
    return {
        "touch_6pct_7bd": touch_flag,
        "touch_day": touch_day,
        "day7_close": day7_close,
        "reverted_flag": (int(day7_close < target_price) if touch_flag else None),
        "ret_sim3": ret_sim3,
        "win_sim3": int(ret_sim3 > 0),
        "ret_sim4": ret_sim4,
        "win_sim4": int(ret_sim4 > 0),
    }


def summarize_kpi(label: str, df: pd.DataFrame) -> dict:
    """1KPI分（または全体プール分）の集計統計を返す。"""
    n = len(df)
    if n == 0:
        return {"kpi": label, "n": 0}
    touched = df[df["touch_6pct_7bd"] == 1]
    return {
        "kpi": label,
        "n": n,
        "touch_rate": df["touch_6pct_7bd"].mean(),
        "median_touch_day": (touched["touch_day"].median() if len(touched) else None),
        "revert_rate": (touched["reverted_flag"].mean() if len(touched) else None),
        "ev_sim3": df["ret_sim3"].mean(),
        "winrate_sim3": df["win_sim3"].mean(),
        "ev_sim4": df["ret_sim4"].mean(),
        "winrate_sim4": df["win_sim4"].mean(),
    }


def fmt_pct(x) -> str:
    return f"{x:.1%}" if x is not None and pd.notna(x) else "-"


def fmt_num(x, digits=1) -> str:
    return f"{x:.{digits}f}" if x is not None and pd.notna(x) else "-"


def format_row(s: dict) -> str:
    if s["n"] == 0:
        return f"| {s['kpi']} | 0 | - | - | - | - | - | - | - |"
    return (
        f"| {s['kpi']} | {s['n']} | {fmt_pct(s['touch_rate'])} | "
        f"{fmt_num(s['median_touch_day'])} | {fmt_pct(s['revert_rate'])} | "
        f"{fmt_pct(s['ev_sim3'])} | {fmt_pct(s['winrate_sim3'])} | "
        f"{fmt_pct(s['ev_sim4'])} | {fmt_pct(s['winrate_sim4'])} |"
    )


TABLE_HEADER = (
    "| KPI | n | P(touch+6%/7bd) | 到達日数中央値 | 触るが持たない率 | "
    "EV③(指値のみ) | 勝率③ | EV④(指値+-4%SL) | 勝率④ |\n"
    "|---|---|---|---|---|---|---|---|---|"
)


def run() -> None:
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    kpi_dirs = discover_kpi_dirs()
    print(f"対象KPIディレクトリ: {len(kpi_dirs)}件")

    per_kpi_summaries: list[dict] = []
    pooled_frames: list[pd.DataFrame] = []
    total_skipped_out_of_calendar = 0

    for kpi_dir in kpi_dirs:
        kpi_name = kpi_dir.name
        sig_df = load_in_sample_signals(kpi_dir / "returns.csv")
        if len(sig_df) == 0:
            per_kpi_summaries.append(summarize_kpi(kpi_name, sig_df))
            print(f"[{kpi_name}] n=0（in_universe & in-sample該当なし）スキップ")
            continue

        rows = []
        skipped = 0
        for rec in sig_df.itertuples(index=False):
            code = str(int(rec.code))
            entry_date = str(int(rec.entry_date))
            entry_price = float(rec.entry_price)
            result = compute_touch_for_signal(code, entry_date, entry_price, bday_index, all_bdays)
            if result is None:
                skipped += 1
                continue
            rows.append(result)
        total_skipped_out_of_calendar += skipped

        kpi_result_df = pd.DataFrame(rows)
        pooled_frames.append(kpi_result_df)
        s = summarize_kpi(kpi_name, kpi_result_df)
        per_kpi_summaries.append(s)
        print(
            f"[{kpi_name}] n={s['n']} touch_rate={fmt_pct(s.get('touch_rate'))} "
            f"skipped_out_of_calendar={skipped}"
        )

    pooled_df = pd.concat(pooled_frames, ignore_index=True) if pooled_frames else pd.DataFrame()
    pooled_summary = summarize_kpi("全体（全KPIプール）", pooled_df)

    # touch率降順ソート（n=0は末尾）
    sortable = [s for s in per_kpi_summaries if s["n"] > 0]
    zero_n = [s for s in per_kpi_summaries if s["n"] == 0]
    sortable.sort(key=lambda s: -s["touch_rate"])
    ordered_summaries = sortable + zero_n

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    write_report(ordered_summaries, pooled_summary, len(kpi_dirs), total_skipped_out_of_calendar)
    write_csv(ordered_summaries, pooled_summary)

    print(f"\n完了: KPI={len(kpi_dirs)}件・プール全シグナル={pooled_summary['n']}件")
    print(f"出力: {OUTPUT_DIR / 'report.md'}\n出力: {OUTPUT_DIR / 'touch_rate_by_kpi.csv'}")


def write_report(ordered_summaries: list[dict], pooled_summary: dict, n_kpi_dirs: int, n_skipped: int) -> None:
    lines = [
        "# KPI touch+6%/7bd 診断レポート（記述的診断・参考観測）",
        "",
        f"**{DISCLAIMER}**",
        "",
        "## 位置づけ",
        "- 記述的診断・選抜なし・台帳不算入。`data/kpi_trials/*` `config/*` `docs/*` catalog は無変更。",
        "- 対象: `output/kpi/*/returns.csv` の in_universe==True 行のみ、"
        "signal_date が in-sample期間 [2016-11-01, 2022-11-30] の行に限定"
        "（HOLDOUTディレクトリは名前で除外・非HOLDOUT名でも2023年以降を含む行は日付で除外＝二重ガード）。",
        f"- 対象KPIディレクトリ数: {n_kpi_dirs} / カレンダー範囲外でスキップしたシグナル: {n_skipped}件",
        "- 主読み出し（事前固定）: P(touch +6% within 7bd)",
        "- 文脈集計（固定セットのみ・グリッド漁り禁止）: "
        "①タッチ到達日数の中央値 ②タッチ後に7bd終値が+6%未満へ垂れた率 "
        "③参考エグジット試算EV/勝率（+6%指値・未達は7bd終値・コスト0.3%控除・損切りなし） "
        "④同試算+-4%ストップ併用版",
        "- **③④は参考であり、いかなる採否判定にも使わない**"
        "（同日に指値/ストップ両方触れうる場合はストップ優先の保守的仮定で計算）。",
        "",
        "## 全体プール値（全KPI・全シグナルを1プールとして集計）",
        TABLE_HEADER,
        format_row(pooled_summary),
        "",
        "## KPI別（touch率降順）",
        TABLE_HEADER,
    ]
    for s in ordered_summaries:
        lines.append(format_row(s))
    lines.append("")
    (OUTPUT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def write_csv(ordered_summaries: list[dict], pooled_summary: dict) -> None:
    rows = [pooled_summary] + ordered_summaries
    df = pd.DataFrame(rows)
    cols = [
        "kpi", "n", "touch_rate", "median_touch_day", "revert_rate",
        "ev_sim3", "winrate_sim3", "ev_sim4", "winrate_sim4",
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df = df[cols]
    df.to_csv(OUTPUT_DIR / "touch_rate_by_kpi.csv", index=False)


def main() -> int:
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
