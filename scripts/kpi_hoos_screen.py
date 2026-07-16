#!/usr/bin/env python3
"""§7-AC historical OOS（2023-2026・汚染済み）一括棄却スクリーン（事前登録・凍結）。

docs/stock-algo-kpi-catalog.md §7-AC を実装する。対象14家族それぞれについて、各家族の
§7凍結パラメータ・エントリー/exit/コスト/ユニバースを一切変更せず、**期間だけ**
2023-01〜2026-06 に変更してシグナルを生成し、リターンを評価する。判定は
「H0: EV(なし・コスト込) ≥ +1%/回 への14家族同時補正の片側検定」であり、各家族の
EV について**片側 99.643% CI 上限**（= 1 − 0.05/14 片側・月次ブロック・ブートストラップ）を
算出して +1% 未満なら hoos_rejected とする。

Canonical Module 原則（再実装禁止）:
  - シグナル生成: scripts/daily_screen.py の generate_kpi_signals ディスパッチをそのまま呼ぶ
    （各家族の正本モジュールを import 再利用する経路）。**raw_strev_entry のみ**、daily_screen
    ラッパー generate_raw_strev_signals が「end_bd が月末営業日の朝のみ・当月分だけ」発火する
    日次設計のため、[start,end] の各月末営業日で反復呼び出しして全期間を再構成する（生成
    ロジック・凍結パラメータは不変。呼び出し境界を日次→月次反復に変えるだけ）。
  - リターン評価: scripts/kpi_event_study.py の compute_signal_returns（フォワードリターン・
    重複除去・ユニバース所属）と bootstrap_ev_ci（月次ブロック・ブートストラップEV CI・
    percentile法）をそのまま使う。コストは measure_base_rate.ROUND_TRIP_COST(0.3%) 既定。

判定（§7-AC凍結・厳守）:
  - n < 30 → hoos_underpowered（判定保留・前向き継続）
  - 片側99.643%CI上限 < +1% → hoos_rejected（運用価値なしが同時補正下でも確定）
  - それ以外 → hoos_survived_tainted（生存に確証効力なし・前向き継続）

台帳（data/kpi_trials/trials.jsonl）へ14行 append（1家族1行・verdict=hoos_*・run_id・診断込み）。
SUE家族は primary=sue_x_above200 を家族判定行とし、sue_beat は診断記録のみ（台帳行にしない）。

Usage:
    # smoke（1家族・短期間・軽いブートストラップ・台帳非追記）
    python3 scripts/kpi_hoos_screen.py --smoke
    # 本実行（全14家族・n_boot=10万・台帳追記）
    python3 scripts/kpi_hoos_screen.py --log-file output/kpi_hoos/run.log
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import daily_screen  # noqa: E402  (Canonical: generate_kpi_signals ディスパッチ)
import jq_fetch  # noqa: E402  (Canonical: now_jst)
import kpi_event_study  # noqa: E402  (Canonical: compute_signal_returns / bootstrap_ev_ci / append_trial)
import measure_base_rate  # noqa: E402  (Canonical: 営業日/レジーム/ユニバース/コスト)

# §7-AC 凍結（結果を見た後の変更禁止）
BONFERRONI_M = 14  # 同時補正の家族数
ALPHA = 0.05
EV_FLOOR = 0.01  # H0: EV ≥ +1%/回（枠F運用価値の下限）
N_BOOT_DEFAULT = 100_000  # Codex62指示・凍結
SEED_DEFAULT = 20260715  # Codex62指示・凍結
CI_LEVEL = 1 - 2 * (ALPHA / BONFERRONI_M)  # bootstrap_ev_ci の ci_high が片側99.643%上限になる二側水準
ONE_SIDED_UPPER_PCTILE = 100 * (1 - ALPHA / BONFERRONI_M)  # 99.642857…%
UNIVERSE_WINDOW = 21  # §6凍結の月次ユニバース窓（daily_screen/kpi_event_study と同一）
UNDERPOWERED_MIN_N = 30  # OOS期間 n < 30 は判定保留

# フォワードリターン評価に必要な最大先行営業日数。compute_signal_returns は
# T+1エントリー・FORWARD_WINDOW_BD(20)営業日後イグジット（defer_entry時は最大
# MAX_ENTRY_DEFER_BDAYS営業日の繰り延べ後）を無条件に load_bars_day する。よって
# シグナルT の T+FORWARD_REACH_BDAYS 営業日後までの bars が揃っていないと FATAL になる。
# データ端（未取得日）を超えてフォワード窓が未完了のシグナルは「まだ評価不能」であり
# 評価対象から外す（凍結パラメータ・判定基準は不変。データ可用性による純粋な端の扱い）。
FORWARD_REACH_BDAYS = (
    measure_base_rate.FORWARD_WINDOW_BD + 1 + kpi_event_study.MAX_ENTRY_DEFER_BDAYS
)

DEFAULT_PERIOD_START = "2023-01"
DEFAULT_PERIOD_END = "2026-06"
DEFAULT_OUTPUT_DIR = Path("output/kpi_hoos")

# 対象14家族（§7-AC凍結列挙・この順で台帳/レポートに出す）。
# family_id: 判定行のkpi_name（watchlistから凍結paramsを引く）。
FROZEN_FAMILIES = [
    "volshock_x_above200_quiet",  # champion
    "sue_x_above200",             # SUE primary（family判定・sue_beatは診断のみ）
    "sell_reg_trigger_rebound",
    "sh_dip_reentry",
    "turnover_rank_surge",
    "margin_expand_yoy",
    "raw_strev_entry",
    "gap_hold_close_strong",
    "engulf_reversal_day",
    "three_up_ignition",
    "sales_beat",
    "guidance_fy_strong",
    "cfo_margin_improve",
    "earnings_spillover",
]
SUE_DIAGNOSTIC_KPI = "sue_beat"  # 家族判定には使わない診断対照


def _log(msg: str, log_fh) -> None:
    print(msg, flush=True)
    if log_fh is not None:
        log_fh.write(msg + "\n")
        log_fh.flush()


def load_watchlist_entries() -> dict[str, dict]:
    """config/paper_watchlist.json を kpi_name -> entry の dict で返す（凍結paramsの正本）。"""
    path = Path("config/paper_watchlist.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {e["kpi_name"]: e for e in data["watchlist"]}


def month_end_bdays(start_bd: str, end_bd: str, all_bdays: list[str]) -> list[str]:
    """[start_bd, end_bd] に含まれる各暦月の最終営業日（YYYYMMDD）を昇順で返す。"""
    ends: list[str] = []
    for i, d in enumerate(all_bdays):
        if d < start_bd or d > end_bd:
            continue
        if i + 1 >= len(all_bdays) or all_bdays[i + 1][:6] != d[:6]:
            ends.append(d)
    return ends


def find_latest_bars_bd(all_bdays: list[str], upper_bd: str) -> Optional[str]:
    """upper_bd 以下で bars キャッシュが実在する最新の営業日を返す（後方走査）。

    jq_fetch のバックグラウンド取得がどこまで到達しているかに依存するデータ端を検出する。
    load_bars_day と同一のパス（jq_fetch.DATA_ROOT/bars/{d}.json.gz）で存在確認する。
    """
    bars_root = jq_fetch.DATA_ROOT / "bars"
    for d in reversed([x for x in all_bdays if x <= upper_bd]):
        if (bars_root / f"{d}.json.gz").exists():
            return d
    return None


def compute_eval_end_bd(
    nominal_end_bd: str, all_bdays: list[str], bday_index: dict[str, int],
) -> tuple[str, str, int]:
    """フォワード窓が完了しているシグナルの最終日（eval_end_bd）を返す。

    Returns: (eval_end_bd, latest_bars_bd, dropped_bdays)。
      eval_end_bd = min(nominal_end_bd, latest_bars_bd の FORWARD_REACH_BDAYS 手前)。
      dropped_bdays = nominal_end_bd から eval_end_bd までに落とした営業日数（0なら未切り詰め）。
    """
    # データ端は nominal_end_bd より後（フォワード窓の分）にありうるので、探索上限を広めに取る。
    upper_idx = min(bday_index[nominal_end_bd] + FORWARD_REACH_BDAYS + 40, len(all_bdays) - 1)
    latest_bars_bd = find_latest_bars_bd(all_bdays, all_bdays[upper_idx])
    if latest_bars_bd is None:
        raise SystemExit("FATAL: bars キャッシュが1日も見つかりません")
    safe_idx = bday_index[latest_bars_bd] - FORWARD_REACH_BDAYS
    if safe_idx < 0:
        raise SystemExit("FATAL: bars 履歴がフォワード窓に満たない")
    safe_signal_end = all_bdays[safe_idx]
    eval_end_bd = min(nominal_end_bd, safe_signal_end)
    dropped = max(0, bday_index[nominal_end_bd] - bday_index[eval_end_bd])
    return eval_end_bd, latest_bars_bd, dropped


def generate_family_signals(
    entry: dict, start_bd: str, end_bd: str,
    regime_by_day: dict[str, str], bday_index: dict[str, int], all_bdays: list[str],
    log_fh,
) -> pd.DataFrame:
    """1家族の全期間シグナルを Canonical ディスパッチで生成する。

    raw_strev_entry は日次ラッパーが月末当月分のみ発火するため月末営業日で反復して結合する。
    他13家族は generate_kpi_signals が [start_bd, end_bd] を1回で全走査する。
    """
    kpi_name = entry["kpi_name"]
    if kpi_name == daily_screen.RAW_STREV_KPI_NAME:
        ends = month_end_bdays(start_bd, end_bd, all_bdays)
        _log(f"    raw_strev: 月末営業日 {len(ends)} 回で反復生成", log_fh)
        frames: list[pd.DataFrame] = []
        for me in ends:
            df = daily_screen.generate_kpi_signals(
                entry, me, me, regime_by_day, bday_index, all_bdays,
            )
            if df is not None and not df.empty:
                frames.append(df[["signal_date", "code"]])
        if not frames:
            return pd.DataFrame(columns=["signal_date", "code"])
        return pd.concat(frames, ignore_index=True)
    df = daily_screen.generate_kpi_signals(
        entry, start_bd, end_bd, regime_by_day, bday_index, all_bdays,
    )
    if df is None or df.empty:
        return pd.DataFrame(columns=["signal_date", "code"])
    return df[["signal_date", "code"]].reset_index(drop=True)


def evaluate_family(
    entry: dict, start_bd: str, end_bd: str, period: tuple[str, str],
    regime_by_day: dict[str, str], bday_index: dict[str, int], all_bdays: list[str],
    universes_by_month: dict[str, set], n_boot: int, seed: int, log_fh,
) -> dict:
    """1家族について シグナル生成→リターン評価→EV補正CI上限→判定 を行い診断dictを返す。

    verdict は付けない（呼び出し側が n と ci_upper から §7-AC 判定を確定する）。
    """
    kpi_name = entry["kpi_name"]
    defer_entry = bool(entry.get("defer_entry", False))
    _log(f"  [{kpi_name}] 生成中（defer_entry={defer_entry}）…", log_fh)
    signals_df = generate_family_signals(
        entry, start_bd, end_bd, regime_by_day, bday_index, all_bdays, log_fh,
    )
    raw_n = int(len(signals_df))
    if raw_n == 0:
        _log(f"    生シグナル0件", log_fh)
        return {
            "kpi_name": kpi_name, "raw_signal_count": 0, "n": 0,
            "point_ev": None, "ci_upper": None, "n_boot_valid": 0, "tail_n": 0,
            "returns_diag": {}, "defer_entry": defer_entry,
        }

    returns_df, rdiag = kpi_event_study.compute_signal_returns(
        signals_df, bday_index, all_bdays, regime_by_day, universes_by_month,
        defer_entry=defer_entry,
    )
    in_universe_df = (
        returns_df[returns_df["in_universe"]].reset_index(drop=True)
        if len(returns_df) else returns_df
    )
    n = int(len(in_universe_df))
    _log(f"    生{raw_n}件 -> ユニバース内{n}件・EVブートストラップ（n_boot={n_boot}）…", log_fh)

    if n == 0:
        return {
            "kpi_name": kpi_name, "raw_signal_count": raw_n, "n": 0,
            "point_ev": None, "ci_upper": None, "n_boot_valid": 0, "tail_n": 0,
            "returns_diag": rdiag, "defer_entry": defer_entry,
        }

    boot = kpi_event_study.bootstrap_ev_ci(
        in_universe_df, ev_column="ret", cost=measure_base_rate.ROUND_TRIP_COST,
        n_boot=n_boot, seed=seed, ci_level=CI_LEVEL,
    )
    point_ev = boot["point_ev"]
    ci_upper = boot["ci_high"]  # 二側 ci_level の上端 = 片側99.643%上限（percentile法）
    n_boot_valid = int(boot["n_boot_valid"])
    # 尾部標本数（上位0.357%＝n_boot×0.05/14）: 極端裾percentileが十分な再標本に支えられるかの診断
    tail_n = int(round(n_boot_valid * (ALPHA / BONFERRONI_M)))
    _log(
        f"    n={n} EV点={point_ev:+.4%} 片側99.643%上限={ci_upper:+.4%} "
        f"（有効ブート{n_boot_valid}・尾部{tail_n}本）",
        log_fh,
    )
    return {
        "kpi_name": kpi_name, "raw_signal_count": raw_n, "n": n,
        "point_ev": point_ev, "ci_upper": ci_upper,
        "n_boot_valid": n_boot_valid, "tail_n": tail_n,
        "returns_diag": rdiag, "defer_entry": defer_entry,
    }


def decide_verdict(n: Optional[int], ci_upper: Optional[float]) -> str:
    """§7-AC凍結の判定を確定する。"""
    if n is None or n < UNDERPOWERED_MIN_N:
        return "hoos_underpowered"
    if ci_upper is None:
        return "hoos_underpowered"  # ブートストラップ不能（有効再標本なし）は判定保留に倒す
    if ci_upper < EV_FLOOR:
        return "hoos_rejected"
    return "hoos_survived_tainted"


def build_trial_record(
    res: dict, verdict: str, params: dict, period: tuple[str, str],
    n_boot: int, seed: int, extra: Optional[dict] = None,
) -> dict:
    """台帳1行（append_trial 用）を組み立てる。verdict=hoos_* / run_id / 診断込み。"""
    rec = {
        "run_id": uuid.uuid4().hex,
        "ts": jq_fetch.now_jst().isoformat(),
        "kpi_name": res["kpi_name"],
        "trial_type": "historical_oos_screen_7AC",
        "params": params,
        "period": {"start": period[0], "end": period[1]},
        "n": res["n"],
        "ev_none_cost_point": res["point_ev"],
        "ev_ci_upper_one_sided_99643": res["ci_upper"],
        "ci_level_two_sided_equiv": CI_LEVEL,
        "one_sided_upper_pctile": ONE_SIDED_UPPER_PCTILE,
        "ev_floor": EV_FLOOR,
        "bonferroni_m": BONFERRONI_M,
        "n_boot": n_boot,
        "n_boot_valid": res["n_boot_valid"],
        "seed": seed,
        "tail_n_upper_0357pct": res["tail_n"],
        "tail_sufficient": bool(res["tail_n"] >= 100),
        "raw_signal_count": res["raw_signal_count"],
        "verdict": verdict,
        "entry_mode": "defer_max3bd" if res["defer_entry"] else "fixed_t1",
        "regime_filter": None,
    }
    if extra:
        rec.update(extra)
    return rec


def fmt_pct(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:+.3%}"


def write_report(
    report_path: Path, rows: list[dict], sue_diag: Optional[dict],
    period: tuple[str, str], start_bd: str, end_bd: str, n_boot: int, seed: int,
    eval_end_bd: str, latest_bars_bd: str, dropped_bdays: int,
) -> None:
    """家族×n×EV×補正CI上限×verdict の一覧表レポートを書く。"""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# §7-AC historical OOS（2023-2026・汚染済み）一括棄却スクリーン 結果")
    lines.append("")
    lines.append(
        f"- 期間: {period[0]}〜{period[1]}（営業日 {start_bd}〜{end_bd}）・1家族1回のみ"
    )
    if dropped_bdays > 0:
        lines.append(
            f"- **フォワード窓の端の扱い**: bars データ端={latest_bars_bd}・フォワード必要先行"
            f"={FORWARD_REACH_BDAYS}営業日のため、評価対象シグナルは {eval_end_bd} まで"
            f"（名目期間端 {end_bd} の手前 {dropped_bdays} 営業日は 20営業日フォワード窓が未完了で"
            f"まだ評価不能＝評価対象外）。凍結パラメータ・判定基準は不変・純粋なデータ可用性による端の切り詰め"
        )
    lines.append(
        f"- 判定: H0 EV(なし・コスト{measure_base_rate.ROUND_TRIP_COST:.1%}込) ≥ +{EV_FLOOR:.0%}/回 への"
        f"{BONFERRONI_M}家族同時補正・**片側{ONE_SIDED_UPPER_PCTILE:.3f}%CI上限**"
        f"（= 1 − {ALPHA}/{BONFERRONI_M} 片側）"
    )
    lines.append(
        f"- ブートストラップ: n_boot={n_boot:,}・seed={seed}・月次ブロック再標本化・percentile法（凍結）"
    )
    lines.append(
        "- **汚染開示**: 対象家族は in-sample/第1回開封/2026運用観測を知った後に設計されており"
        "完全独立の未見OOSではない。本スクリーンは**棄却側にのみ効力**を持つ（生存に確証効力なし）"
    )
    lines.append("")
    lines.append("## 判定一覧")
    lines.append("")
    lines.append(
        "| # | 家族 | N | EV点推定(なし・コスト込) | 補正CI上限(片側99.643%) | verdict |"
    )
    lines.append("|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        lines.append(
            f"| {i} | {r['kpi_name']} | {r['n']} | {fmt_pct(r['point_ev'])} | "
            f"{fmt_pct(r['ci_upper'])} | {r['verdict']} |"
        )
    lines.append("")
    # verdict集計
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    lines.append("### verdict内訳")
    lines.append("")
    for v in ("hoos_rejected", "hoos_survived_tainted", "hoos_underpowered"):
        lines.append(f"- {v}: {counts.get(v, 0)} 家族")
    lines.append("")
    lines.append("## 診断")
    lines.append("")
    lines.append(
        "| 家族 | 生件数 | ユニバース内N | 有効ブート | 尾部標本(上位0.357%) | 尾部十分 | entry |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        tail_ok = "○" if r["tail_n"] >= 100 else "×"
        entry_mode = "defer≤3bd" if r["defer_entry"] else "T+1固定"
        lines.append(
            f"| {r['kpi_name']} | {r['raw_signal_count']} | {r['n']} | "
            f"{r['n_boot_valid']} | {r['tail_n']} | {tail_ok} | {entry_mode} |"
        )
    lines.append("")
    if sue_diag is not None:
        lines.append("## SUE診断対照（sue_beat・家族判定には不使用）")
        lines.append("")
        lines.append(
            f"- sue_beat: N={sue_diag['n']}・EV点={fmt_pct(sue_diag['point_ev'])}・"
            f"補正CI上限={fmt_pct(sue_diag['ci_upper'])}"
            f"（参考: 家族判定は primary=sue_x_above200 で行う。§7-J踏襲・Codex61凍結）"
        )
        lines.append("")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="§7-AC historical OOS 一括棄却スクリーン")
    parser.add_argument("--start", default=DEFAULT_PERIOD_START, help="期間開始 YYYY-MM")
    parser.add_argument("--end", default=DEFAULT_PERIOD_END, help="期間終了 YYYY-MM")
    parser.add_argument("--n-boot", type=int, default=N_BOOT_DEFAULT)
    parser.add_argument("--seed", type=int, default=SEED_DEFAULT)
    parser.add_argument("--no-trials-append", action="store_true", help="台帳(trials.jsonl)へ追記しない")
    parser.add_argument("--families", nargs="*", default=None, help="対象家族を限定（既定=全14）")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--log-file", default=None, help="進捗ログの出力先")
    parser.add_argument(
        "--smoke", action="store_true",
        help="スモーク: sell_reg_trigger_rebound 1家族・短期間・軽いブート・台帳非追記",
    )
    args = parser.parse_args()

    if args.smoke:
        args.families = ["sell_reg_trigger_rebound"]
        args.start = "2023-01"
        args.end = "2023-06"
        args.n_boot = 2000
        args.no_trials_append = True

    log_fh = open(args.log_file, "a", encoding="utf-8") if args.log_file else None
    try:
        period = (args.start, args.end)
        _log(
            f"=== §7-AC historical OOS screen 開始 period={period} "
            f"n_boot={args.n_boot} seed={args.seed} "
            f"trials_append={not args.no_trials_append} ===",
            log_fh,
        )

        # --- ハーネス構築（kpi_event_study.run_event_study と同一の Canonical 経路） ---
        calendar_days = measure_base_rate.load_calendar_days()
        all_bdays = measure_base_rate.all_business_days(calendar_days)
        bday_index = {d: i for i, d in enumerate(all_bdays)}
        topix_close = measure_base_rate.load_topix_series()
        regime_by_day = measure_base_rate.build_regime_series(topix_close)
        universes_by_month = kpi_event_study.load_universe_by_month(
            kpi_event_study.DEFAULT_BASE_RATE_DIR, UNIVERSE_WINDOW,
        )

        start_key = args.start.replace("-", "") + "01"
        end_key = args.end.replace("-", "") + "31"
        in_range = [d for d in all_bdays if start_key <= d <= end_key]
        if not in_range:
            _log(f"FATAL: 期間 {period} に営業日がありません", log_fh)
            return 1
        start_bd, end_bd = in_range[0], in_range[-1]
        # フォワード窓が未完了（データ端超え）のシグナルは評価不能なので eval_end_bd まで切り詰める。
        eval_end_bd, latest_bars_bd, dropped_bdays = compute_eval_end_bd(
            end_bd, all_bdays, bday_index,
        )
        _log(f"営業日レンジ: {start_bd} 〜 {end_bd}（名目期間端）", log_fh)
        _log(
            f"bars データ端: {latest_bars_bd} / フォワード必要先行={FORWARD_REACH_BDAYS}営業日 "
            f"=> 評価対象シグナル最終日 eval_end_bd={eval_end_bd}"
            f"（フォワード窓未完了で除外した末尾={dropped_bdays}営業日）",
            log_fh,
        )

        watchlist = load_watchlist_entries()
        families = args.families if args.families else list(FROZEN_FAMILIES)

        rows: list[dict] = []
        trial_records: list[dict] = []
        sue_diag_res: Optional[dict] = None

        for fam in families:
            if fam not in watchlist:
                _log(f"FATAL: watchlist に家族 {fam} が見つかりません", log_fh)
                return 1
            entry = watchlist[fam]
            res = evaluate_family(
                entry, start_bd, eval_end_bd, period,
                regime_by_day, bday_index, all_bdays, universes_by_month,
                args.n_boot, args.seed, log_fh,
            )
            verdict = decide_verdict(res["n"], res["ci_upper"])
            res["verdict"] = verdict
            _log(f"    => verdict={verdict}", log_fh)

            # SUE primary の行に sue_beat 診断を計算して同梱（台帳行は sue_beat を作らない）
            extra = None
            if fam == "sue_x_above200":
                sue_beat_entry = watchlist[SUE_DIAGNOSTIC_KPI]
                _log(f"  [SUE診断] {SUE_DIAGNOSTIC_KPI} を診断計算…", log_fh)
                sue_diag_res = evaluate_family(
                    sue_beat_entry, start_bd, eval_end_bd, period,
                    regime_by_day, bday_index, all_bdays, universes_by_month,
                    args.n_boot, args.seed, log_fh,
                )
                extra = {
                    "sue_beat_diagnostic": {
                        "n": sue_diag_res["n"],
                        "ev_none_cost_point": sue_diag_res["point_ev"],
                        "ev_ci_upper_one_sided_99643": sue_diag_res["ci_upper"],
                        "note": "診断対照のみ・家族判定には不使用（§7-J踏襲・Codex61凍結）",
                    }
                }

            rows.append(res)
            rec = build_trial_record(
                res, verdict, entry["params"], period, args.n_boot, args.seed, extra
            )
            rec["eval_window"] = {
                "start_bd": start_bd,
                "nominal_end_bd": end_bd,
                "eval_end_bd": eval_end_bd,
                "latest_bars_bd": latest_bars_bd,
                "forward_reach_bdays": FORWARD_REACH_BDAYS,
                "dropped_tail_bdays_forward_incomplete": dropped_bdays,
            }
            trial_records.append(rec)

        # --- レポート出力 ---
        output_dir = Path(args.output_dir)
        report_path = output_dir / "report.md"
        write_report(
            report_path, rows, sue_diag_res, period, start_bd, end_bd, args.n_boot, args.seed,
            eval_end_bd=eval_end_bd, latest_bars_bd=latest_bars_bd, dropped_bdays=dropped_bdays,
        )
        _log(f"レポート出力: {report_path}", log_fh)

        # --- 台帳追記（家族判定行のみ・1家族1行） ---
        if args.no_trials_append:
            _log(f"[--no-trials-append] 台帳追記スキップ（{len(trial_records)}行を保留）", log_fh)
        else:
            trials_path = kpi_event_study.DEFAULT_TRIALS_PATH
            for rec in trial_records:
                kpi_event_study.append_trial(rec, trials_path)
            _log(f"台帳追記: {len(trial_records)}行 -> {trials_path}", log_fh)

        # --- サマリー ---
        _log("=== 判定サマリー ===", log_fh)
        for r in rows:
            _log(
                f"  {r['kpi_name']}: N={r['n']} EV点={fmt_pct(r['point_ev'])} "
                f"補正CI上限={fmt_pct(r['ci_upper'])} => {r['verdict']}",
                log_fh,
            )
        _log("=== 完了 ===", log_fh)
        return 0
    finally:
        if log_fh is not None:
            log_fh.close()


if __name__ == "__main__":
    raise SystemExit(main())
