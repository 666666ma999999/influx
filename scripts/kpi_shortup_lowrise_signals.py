#!/usr/bin/env python3
"""空売り残高増加×安値切り上げ シグナル生成器（カタログ§7-B v2-3・第13周B）。

docs/stock-algo-kpi-catalog.md の §7-B v2-3「空売り残高増加×安値切り上げ」を実装する。
最有望系（shortcover_turn＝減少転換・第4周確定 リフト2.29 CI[1.82,2.97]）の逆側からの直交仮説:
空売り残高が増えているのに株価の安値が切り上がる「静かな歪み」を、将来の減少転換（買い戻し）の
先行検知として捉える。

定義（グリッドサーチはしない・1構成のみ・team-lead指示どおり）:
    (1) 空売り増加: 銘柄別の空売り残高割合合計(genuine集計)が、直近2回の開示イベント連続で
        genuine増加（前回開示比 genuine_delta > 0）。報告消失（0.5%割れ）は集計除外・
        genuine_deltaにも加算しない（scripts/kpi_shortcover_signals.py の genuine判定流儀を踏襲）。
        連続増加が途切れたら（genuine_delta <= 0 の開示があれば）カウンタは0にリセットする。
        状態は開示イベントの公表翌営業日（disc_date+1bd）から有効になる（§2-B公表ラグ厳守）。
    (2) 安値切り上げ: 直近20営業日の最安値（D-20〜D-1） > その前20営業日の最安値（D-40〜D-21）。
        AdjL使用・シグナル判定日D自身は含めない（team-lead指示の日付レンジをそのまま採用）。
        「直近N営業日」は kpi_high52_signals.py / kpi_volshock_signals.py と同じ流儀で
        「直近N回の有効AdjL観測値」の実務版（暦固定窓ではない。売買停止等の欠測日を挟んでも
        自然にスキップされ、look-aheadにもならない）。
    (3) シグナル確定日 = (1)AND(2) が「不成立→成立」に遷移した営業日D（状態型・連続発火は初日のみ）。
        (1)は開示イベント発生日（公表翌営業日）のみ状態が更新され、それ以外の営業日は直前の状態を
        保持する。(2)は毎営業日評価される。両者のANDを毎営業日評価し、遷移日のみ採用する。

データ経路: `data/jquants/shortsale/YYYYMMDD.json.gz`（空売り残高報告）を
scripts/kpi_shortcover_signals.load_all_shortsale_records() でロードする（Canonical Module
原則・データ読み込みの再実装はしない）。銘柄別genuine集計ロジック自体（開示バッチ単位で
SSName別残高を追跡し報告消失を除外する変換）は、ゲート方向が真逆（減少判定 vs 2回連続増加判定）
のため関数として共有できず、同スクリプトのロジックをこのファイル内で鏡写しする
（compute_short_increase_events。docstringで由来を明記・値の突合は§6検証ステップで確認する）。
既存のkpi_shortcover_signals.py（frozen baseline・shortcover_turn n=3525が他レポートに
引用済み）は不変更のまま参照専用とする。

フォワードリターン計算・ユニバース判定・ブートストラップCI等は scripts/kpi_event_study.py の
run_event_study() に委譲する（Canonical Module原則・再実装しない）。

Usage:
    python3 scripts/kpi_shortup_lowrise_signals.py --start 2016-11 --end 2022-11 --defer-entry
    python3 scripts/kpi_shortup_lowrise_signals.py --start 2016-11 --end 2022-11 --skip-harness
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict, deque
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: DATA_ROOT を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: run_event_study を再利用)
import kpi_shortcover_signals  # noqa: E402  (Canonical Module: load_all_shortsale_records・IN_SAMPLE定数等を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読み込みを再利用)

KPI_NAME = "shortup_lowrise"

STREAK_LEN = 2  # 「直近2回の報告連続で増加」（グリッドサーチ対象外の固定仕様）
LOW_WINDOW = 20  # 現在窓・比較窓とも20営業日
LOW_HISTORY_TOTAL = LOW_WINDOW * 2  # 40件保持（直近20 + その前20）
WARMUP_BDAYS_MARGIN = 45  # 40件窓よりわずかに大きいマージン。実データ最古日でさらに切り上げる


def _earliest_bars_date() -> str:
    """data/jquants/bars/ に実在する最古の営業日(YYYYMMDD)を返す(ハードコードせず実ファイルから確認)。

    kpi_high52_signals.py / kpi_volshock_signals.py / kpi_exit_study.py と同じ役割の
    ヘルパー(このプロジェクトでは各kpiスクリプトが自己完結する既存慣習に合わせ複製する)。
    """
    bars_dir = jq_fetch.DATA_ROOT / "bars"
    dates = sorted(p.name[:8] for p in bars_dir.glob("*.json.gz"))
    if not dates:
        raise SystemExit(f"FATAL: bars キャッシュが1件も見つかりません: {bars_dir}")
    return dates[0]


# --- 空売り増加ストリーク状態の計算 ------------------------------------------------


def compute_short_increase_events(
    bday_index: dict[str, int],
    all_bdays: list[str],
    exit_threshold: float,
) -> tuple[dict[str, list[tuple]], dict]:
    """空売り残高「直近2回連続genuine増加」の状態遷移イベントを計算する。

    kpi_shortcover_signals.generate_shortcover_signals() のバッチ処理ループ（銘柄別・
    開示日(DiscDate)バッチ単位でSSName別残高を追跡し、報告消失(<exit_threshold)は
    集計除外・genuine_deltaに加算しない）を同一データに対して鏡写しで適用し、
    「genuine_delta > 0」が直近2回の開示イベント連続で成立したかを状態として持つ
    （成立が途切れたらカウンタを0にリセット）。

    Returns:
        (events_by_available_date, diag)。
        events_by_available_date: dict[available_date(YYYYMMDD)] -> list of
            (code, streak_state:bool, disc_date, aggregate_after) タプル。
            available_date = disc_date の公表翌営業日（§2-B公表ラグ厳守。
            kpi_shortcover_signals と同じ規約）。
    """
    df = kpi_shortcover_signals.load_all_shortsale_records()
    df.sort_values(["Code", "DiscDate", "CalcDate"], inplace=True)

    diag = {
        "total_records": int(len(df)),
        "disclosure_events_processed": 0,
        "exit_events_total": 0,
        "increase_batches": 0,
        "streak_activations": 0,  # 2回連続増加に新規到達した回数(銘柄あたり複数回あり得る)
        "skipped_disc_date_not_business_day": 0,
        "skipped_calendar_end": 0,
    }

    events_by_available_date: dict[str, list[tuple]] = defaultdict(list)

    for code, grp in df.groupby("Code", sort=False):
        state: dict[str, float] = {}
        consec_increase = 0
        for disc_date, batch in grp.groupby("DiscDate", sort=True):
            genuine_delta = 0.0
            for _, rec in batch.sort_values("CalcDate").iterrows():
                ssname = rec["SSName"]
                new_ratio = float(rec["ShrtPosToSO"])
                old_ratio = state.get(ssname, 0.0)
                if new_ratio < exit_threshold:
                    diag["exit_events_total"] += 1
                    state.pop(ssname, None)
                    # genuine_delta には加算しない（閾値アーティファクトを増減に混入させない）
                else:
                    genuine_delta += new_ratio - old_ratio
                    state[ssname] = new_ratio
            after_aggregate = sum(state.values())
            diag["disclosure_events_processed"] += 1

            is_increase = genuine_delta > 0
            if is_increase:
                diag["increase_batches"] += 1
                consec_increase += 1
            else:
                consec_increase = 0
            streak_state = consec_increase >= STREAK_LEN
            if streak_state and consec_increase == STREAK_LEN:
                diag["streak_activations"] += 1

            if disc_date not in bday_index:
                diag["skipped_disc_date_not_business_day"] += 1
                continue
            idx = bday_index[disc_date]
            if idx + 1 >= len(all_bdays):
                diag["skipped_calendar_end"] += 1
                continue
            available_date = all_bdays[idx + 1]
            events_by_available_date[available_date].append(
                (code, streak_state, disc_date, after_aggregate)
            )

    return events_by_available_date, diag


# --- シグナル生成（安値切り上げ×空売り増加ストリークのAND・毎営業日走査） -----------------


def generate_shortup_lowrise_signals(
    start_bd: str,
    end_bd: str,
    exit_threshold: float = kpi_shortcover_signals.EXIT_THRESHOLD_DEFAULT,
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd](YYYYMMDD営業日)内で空売り増加×安値切り上げシグナルを生成する。

    空売り側の状態は開示のあった営業日のみ更新され、それ以外の日は直前状態を保持する
    （kpi_shortcover_signalsと同じ規約）。価格側（安値切り上げ）は毎営業日評価する
    （kpi_high52_signals/kpi_volshock_signalsと同じ「直近N回の有効観測値」の実務版）。
    両者のANDは毎営業日評価し、「不成立→成立」に遷移した日のみシグナルとして採用する。
    ウォームアップ期間（start_bd以前）も内部では走査し続け、start_bd時点の直前状態を
    正しく引き継ぐ（kpi_shortcover_signalsが全履歴を通しで状態管理してから出力範囲を
    絞り込む方式と同じ考え方）。

    Returns:
        (signals_df, diag)。signals_df 列: signal_date, code, short_disc_date,
        short_available_date, short_aggregate_after, low_min_recent20, low_min_prior20。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    events_by_available_date, short_diag = compute_short_increase_events(
        bday_index, all_bdays, exit_threshold
    )

    idx_start = bday_index[start_bd]
    idx_end = bday_index[end_bd]
    idx_earliest_bars = bday_index[_earliest_bars_date()]
    warmup_idx = max(idx_earliest_bars, idx_start - WARMUP_BDAYS_MARGIN)
    scan_days = all_bdays[warmup_idx : idx_end + 1]

    low_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=LOW_HISTORY_TOTAL))
    short_state: dict[str, bool] = {}
    short_meta: dict[str, tuple] = {}  # code -> (disc_date, available_date, aggregate_after)
    prev_and_state: dict[str, bool] = {}

    diag = {
        **{f"short_{k}": v for k, v in short_diag.items()},
        "business_days_scanned": 0,
        "code_day_observations": 0,
        "insufficient_price_history": 0,
        "low_rise_true": 0,
        "short_up_true": 0,
        "and_gate_pass": 0,
        "signals_transitions": 0,
    }

    rows: list[dict] = []

    for d in scan_days:
        for code, streak_state, disc_date, aggregate_after in events_by_available_date.get(d, []):
            short_state[code] = streak_state
            short_meta[code] = (disc_date, d, aggregate_after)

        bars_d = measure_base_rate.load_bars_day(d)
        in_event_window = start_bd <= d <= end_bd
        if in_event_window:
            diag["business_days_scanned"] += 1

        for code, rec in bars_d.items():
            adjl_d = rec.get("AdjL")

            hist = low_hist.get(code)
            if hist is not None and len(hist) == LOW_HISTORY_TOTAL:
                hist_list = list(hist)
                window_prior = hist_list[:LOW_WINDOW]  # D-40..D-21
                window_recent = hist_list[LOW_WINDOW:]  # D-20..D-1
                low_min_prior = min(window_prior)
                low_min_recent = min(window_recent)
                low_rise = low_min_recent > low_min_prior
                short_up = short_state.get(code, False)
                and_now = low_rise and short_up
                prev = prev_and_state.get(code, False)
                is_transition = and_now and not prev
                prev_and_state[code] = and_now

                if in_event_window:
                    diag["code_day_observations"] += 1
                    if low_rise:
                        diag["low_rise_true"] += 1
                    if short_up:
                        diag["short_up_true"] += 1
                    if and_now:
                        diag["and_gate_pass"] += 1
                    if is_transition:
                        diag["signals_transitions"] += 1
                        meta = short_meta.get(code, (None, None, None))
                        rows.append(
                            {
                                "signal_date": d,
                                "code": code,
                                "short_disc_date": meta[0],
                                "short_available_date": meta[1],
                                "short_aggregate_after": meta[2],
                                "low_min_recent20": low_min_recent,
                                "low_min_prior20": low_min_prior,
                            }
                        )
            else:
                if in_event_window:
                    diag["code_day_observations"] += 1
                    diag["insufficient_price_history"] += 1

            if adjl_d is not None:
                low_hist[code].append(adjl_d)

    return pd.DataFrame(rows), diag


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="空売り残高増加×安値切り上げ シグナル生成器(カタログ§7-B v2-3) + KPI検証ハーネス実行"
    )
    parser.add_argument(
        "--start", default=kpi_shortcover_signals.IN_SAMPLE_START,
        help=f"検索開始月 YYYY-MM（既定 {kpi_shortcover_signals.IN_SAMPLE_START}）",
    )
    parser.add_argument(
        "--end", default=kpi_shortcover_signals.IN_SAMPLE_END,
        help=f"検索終了月 YYYY-MM（既定 {kpi_shortcover_signals.IN_SAMPLE_END}）",
    )
    parser.add_argument(
        "--exit-threshold", type=float, default=kpi_shortcover_signals.EXIT_THRESHOLD_DEFAULT
    )
    parser.add_argument("--kpi-name", default=KPI_NAME)
    parser.add_argument("--output-dir", default="output/kpi", help="ハーネス出力先ルート（kpi-name配下に生成）")
    parser.add_argument(
        "--skip-harness", action="store_true", help="シグナル生成のみ行いハーネス実行をスキップ（デバッグ用）"
    )
    parser.add_argument(
        "--defer-entry", action="store_true",
        help="T+1にAdjOが無い(S高等)場合、最大3営業日までエントリーを繰り延べる（§6手順6実装）",
    )
    args = parser.parse_args()

    if not kpi_shortcover_signals.MONTH_RE.match(args.start) or not kpi_shortcover_signals.MONTH_RE.match(args.end):
        raise SystemExit("FATAL: --start/--end は YYYY-MM 形式で指定してください")
    if args.end > kpi_shortcover_signals.IN_SAMPLE_END:
        print(
            f"WARN: --end={args.end} は§6で凍結したholdout期間(2023年以降)に抵触します。"
            f"in-sample評価は{kpi_shortcover_signals.IN_SAMPLE_END}までに限定してください"
            f"（holdoutは選抜済み候補の最終確認にのみ使用）。",
            file=sys.stderr,
        )

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays_all = measure_base_rate.all_business_days(calendar_days)
    start_bound = args.start.replace("-", "") + "01"
    end_bound = args.end.replace("-", "") + "31"
    start_bd = next((d for d in all_bdays_all if d >= start_bound), None)
    end_bd = next((d for d in reversed(all_bdays_all) if d <= end_bound), None)
    if start_bd is None or end_bd is None:
        raise SystemExit("FATAL: 指定期間に営業日が見つかりません")

    signals_df, diag = generate_shortup_lowrise_signals(start_bd, end_bd, args.exit_threshold)

    output_root = Path(args.output_dir)
    kpi_dir = output_root / args.kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_path = kpi_dir / "signals_raw.csv"
    signals_df.to_csv(signals_path, index=False)

    print(f"シグナル生成完了: {len(signals_df)}件")
    print(
        f"空売り側内訳: total_records={diag['short_total_records']} "
        f"disclosure_events={diag['short_disclosure_events_processed']} "
        f"exit_events={diag['short_exit_events_total']} "
        f"increase_batches={diag['short_increase_batches']} "
        f"streak_activations={diag['short_streak_activations']}"
    )
    print(
        f"価格・AND側内訳: 営業日走査={diag['business_days_scanned']}日 "
        f"銘柄日観測={diag['code_day_observations']} 履歴不足(40日未満)={diag['insufficient_price_history']} "
        f"low_rise成立={diag['low_rise_true']} short_up成立={diag['short_up_true']} "
        f"AND成立(延べ)={diag['and_gate_pass']} 遷移(シグナル)={diag['signals_transitions']}"
    )
    print(f"出力: {signals_path}")

    if args.skip_harness:
        return 0
    if signals_df.empty:
        print("WARN: シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    params = {
        "streak_len": STREAK_LEN,
        "low_window": LOW_WINDOW,
        "exit_threshold": args.exit_threshold,
        "defer_entry": args.defer_entry,
    }
    result = kpi_event_study.run_event_study(
        signals_df=signals_df[["signal_date", "code"]],
        kpi_name=args.kpi_name,
        params=params,
        period=(args.start, args.end),
        output_dir=output_root,
        defer_entry=args.defer_entry,
    )
    lift_str = f"{result['lift']:.2f}" if result["lift"] is not None else "-"
    ci_str = (
        f"[{result['ci_low']:.2f}, {result['ci_high']:.2f}]" if result["ci_low"] is not None else "[-, -]"
    )
    print(f"ハーネス実行完了: n={result['n']} lift={lift_str} ci95={ci_str} verdict={result['verdict']}")
    print(f"report: {result['report_path']}")
    print(f"returns: {result['returns_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
