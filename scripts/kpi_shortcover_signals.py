#!/usr/bin/env python3
"""空売り残高報告 減少転換 シグナル生成器（買い戻し開始候補・カタログ§2-B #7）。

docs/stock-algo-kpi-catalog.md の §2-B「空売り残高報告（0.5%超）合計・減少転換」を実装する。
`data/jquants/shortsale/YYYYMMDD.json.gz`（空売り残高報告・別ビルダー取得分。disc_date単位で
市場全銘柄を1リクエスト取得済み）から、銘柄別に継続報告中の報告者(SSName)のShrtPosToSOを
合計し、(合計 >= 3%) AND (最新報告で前回報告比減少) をシグナル条件とする。

報告消失（0.5%割れ）の扱い（本KPIの核心・実データで確認済みの挙動）:
    - JPXルールでは報告義務は残高割合0.5%未満で消滅するが、実疎通で確認した限り
      「0.5%を下回った」こと自体が明示的な1レコードとして報告される
      （ShrtPosToSO<0.5%の値、しばしば0.0で登場。Notes欄は通常"-"のまま）。
      したがって「継続報告者が黙って消える」ケースは実質発生せず、この明示レコードの
      有無で機械的に判定できる。
    - この「消失」レコードは (a) 集計対象からは除外（その報告者の寄与を0にする=将来の
      集計から外す）(b) ただし「前回報告比減少」の判定（genuine_delta）には**加算しない**
      （閾値アーティファクトによる見かけの減少を「本物の買い戻し」と誤認しないため）。

公表ラグ（look-ahead回避）:
    - レスポンス自体に DiscDate（公表日）フィールドが含まれる（margin-interestと異なり
      推定不要）。シグナル確定日 = DiscDate の翌営業日（17時公表ルール・team lead指示）。
    - 状態更新の時系列順は DiscDate（公表日）を主キーとする（CalcDateの新しい記録が
      DiscDateの古い記録より後に公表される「遅れて公表される訂正」のケースがあるため、
      市場が実際に情報を得た順=DiscDate順で状態を更新しないとlook-ahead違反になる）。

定義（グリッドサーチはしない・1構成のみ）:
    - 銘柄コード別に、報告公表日(DiscDate)ごとにバッチ処理する
      （同日に複数機関・複数CalcDateの報告が公表されることがあるため）。
    - バッチ内の各レコードについて、ShrtPosToSO < EXIT_THRESHOLD なら「消失」イベント
      （集計から除外・genuine_deltaに加算しない）。それ以外は「変更/新規」として
      genuine_delta += (新ratio - 旧ratio) を積み上げる。
    - シグナル: (バッチ処理後の合計 >= AGGREGATE_THRESHOLD) AND (genuine_delta < 0)
    - 上記条件が「成立した状態」に**遷移した公表日のみ**をシグナル確定日とする（状態型・
      KPI#5 margin_urinaga_trendと同じ遷移日ベースの考え方。カタログ原義が「減少転換」
      であり継続的な減少の毎日ではなく転換点を指すこと、および緩やかな複数日下落が続く
      銘柄で開示イベントの度に発火すると同一下落トレンドが数百件の別シグナルに水増しされる
      ことを確認したため、生成側で遷移検出まで行う。連続する同一状態の2日目以降は間引く）

フォワードリターン計算・ユニバース判定・ブートストラップCI等は scripts/kpi_event_study.py の
run_event_study() に委譲する（Canonical Module原則・再実装しない）。

Usage:
    python3 scripts/kpi_shortcover_signals.py --start 2016-11 --end 2022-11
    python3 scripts/kpi_shortcover_signals.py --start 2016-11 --end 2022-11 --skip-harness
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: DATA_ROOT / read_json_gz を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: run_event_study を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー読み込みを再利用)

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

SHORTSALE_DIR = jq_fetch.DATA_ROOT / "shortsale"

# §0確認事項5・§6標本分割で凍結: in-sampleは2016-11〜2022-11。holdout(2023以降)はロック中。
IN_SAMPLE_START = "2016-11"
IN_SAMPLE_END = "2022-11"

KPI_NAME = "shortcover_turn"

AGGREGATE_THRESHOLD_DEFAULT = 0.03  # 銘柄別合計 >= 3%
EXIT_THRESHOLD_DEFAULT = 0.005  # 0.5%未満 = 報告消失（義務消滅）とみなす


# --- shortsale 読み込み -----------------------------------------------------------


def load_all_shortsale_records() -> pd.DataFrame:
    """data/jquants/shortsale/*.json.gz を全て読み込み1つのDataFrameに結合する（未取得なら明確なエラーで停止）。"""
    if not SHORTSALE_DIR.exists() or not any(SHORTSALE_DIR.glob("*.json.gz")):
        raise SystemExit(
            f"FATAL: shortsaleキャッシュが見つかりません: {SHORTSALE_DIR}\n"
            f"先に `python3 scripts/jq_fetch.py --only shortsale` を実行してください。"
        )
    rows: list[dict] = []
    for path in sorted(SHORTSALE_DIR.glob("*.json.gz")):
        obj = jq_fetch.read_json_gz(path)
        rows.extend(obj["data"])
    if not rows:
        raise SystemExit(f"FATAL: shortsaleキャッシュが空です: {SHORTSALE_DIR}")
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["DiscDate", "CalcDate", "Code", "SSName", "ShrtPosToSO"])
    df["DiscDate"] = df["DiscDate"].str.replace("-", "", regex=False)
    df["CalcDate"] = df["CalcDate"].str.replace("-", "", regex=False)
    return df[["DiscDate", "CalcDate", "Code", "SSName", "ShrtPosToSO"]]


# --- シグナル生成 ---------------------------------------------------------------


def generate_shortcover_signals(
    start_bd: str,
    end_bd: str,
    aggregate_threshold: float = AGGREGATE_THRESHOLD_DEFAULT,
    exit_threshold: float = EXIT_THRESHOLD_DEFAULT,
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]（YYYYMMDD営業日）内で空売り残高報告の減少転換シグナルを生成する。

    銘柄ごとに全履歴（データ取得開始の2016-07〜）を通しで状態管理し、出力のみ
    [start_bd, end_bd] に絞り込む（状態のウォームアップを正しく反映するため）。

    Returns:
        (signals_df, diag)。signals_df 列: signal_date, code
        （+ 診断用列: disc_date, aggregate_after, genuine_delta, has_exit_event_same_batch,
        n_active_reporters）。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    df = load_all_shortsale_records()
    df.sort_values(["Code", "DiscDate", "CalcDate"], inplace=True)

    diag = {
        "total_records": int(len(df)),
        "disclosure_events_processed": 0,
        "exit_events_total": 0,
        "gate_aggregate_pass": 0,
        "gate_aggregate_and_decrease_pass": 0,
        "gate_transition_pass": 0,
        "skipped_disc_date_not_business_day": 0,
        "skipped_calendar_end": 0,
    }

    rows: list[dict] = []
    for code, grp in df.groupby("Code", sort=False):
        state: dict[str, float] = {}
        prev_gate = False  # 直前の公表日時点でシグナル条件が成立していたか（遷移検出用）
        for disc_date, batch in grp.groupby("DiscDate", sort=True):
            genuine_delta = 0.0
            has_exit_event = False
            for _, rec in batch.sort_values("CalcDate").iterrows():
                ssname = rec["SSName"]
                new_ratio = float(rec["ShrtPosToSO"])
                old_ratio = state.get(ssname, 0.0)
                if new_ratio < exit_threshold:
                    has_exit_event = True
                    diag["exit_events_total"] += 1
                    state.pop(ssname, None)
                    # genuine_delta には加算しない（閾値アーティファクトを「減少」に混入させない）
                else:
                    genuine_delta += new_ratio - old_ratio
                    state[ssname] = new_ratio
            after_aggregate = sum(state.values())
            diag["disclosure_events_processed"] += 1

            gate_aggregate = after_aggregate >= aggregate_threshold
            gate_decrease = genuine_delta < 0
            gate_now = gate_aggregate and gate_decrease
            if gate_aggregate:
                diag["gate_aggregate_pass"] += 1
            if gate_now:
                diag["gate_aggregate_and_decrease_pass"] += 1

            is_transition = gate_now and not prev_gate
            prev_gate = gate_now

            if disc_date not in bday_index:
                diag["skipped_disc_date_not_business_day"] += 1
                continue

            if is_transition:
                diag["gate_transition_pass"] += 1
                idx = bday_index[disc_date]
                if idx + 1 >= len(all_bdays):
                    diag["skipped_calendar_end"] += 1
                    continue
                signal_date = all_bdays[idx + 1]
                rows.append(
                    {
                        "signal_date": signal_date,
                        "code": code,
                        "disc_date": disc_date,
                        "aggregate_after": after_aggregate,
                        "genuine_delta": genuine_delta,
                        "has_exit_event_same_batch": has_exit_event,
                        "n_active_reporters": len(state),
                    }
                )

    signals_df = pd.DataFrame(rows)
    diag["signals_before_range_filter"] = int(len(signals_df))
    if not signals_df.empty:
        signals_df = signals_df[
            (signals_df["signal_date"] >= start_bd) & (signals_df["signal_date"] <= end_bd)
        ].reset_index(drop=True)
    diag["signals_in_range"] = int(len(signals_df))
    return signals_df, diag


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="空売り残高報告 減少転換シグナル生成器 + KPI検証ハーネス実行"
    )
    parser.add_argument("--start", default=IN_SAMPLE_START, help=f"検索開始月 YYYY-MM（既定 {IN_SAMPLE_START}）")
    parser.add_argument("--end", default=IN_SAMPLE_END, help=f"検索終了月 YYYY-MM（既定 {IN_SAMPLE_END}）")
    parser.add_argument("--aggregate-threshold", type=float, default=AGGREGATE_THRESHOLD_DEFAULT)
    parser.add_argument("--exit-threshold", type=float, default=EXIT_THRESHOLD_DEFAULT)
    parser.add_argument("--kpi-name", default=KPI_NAME)
    parser.add_argument("--output-dir", default="output/kpi", help="ハーネス出力先ルート（kpi-name配下に生成）")
    parser.add_argument(
        "--skip-harness", action="store_true", help="シグナル生成のみ行いハーネス実行をスキップ（デバッグ用）"
    )
    parser.add_argument(
        "--defer-entry", action="store_true",
        help="T+1にAdjOが無い(S高等)場合、最大3営業日までエントリーを繰り延べる（第4周・§6手順6実装）",
    )
    args = parser.parse_args()

    if not MONTH_RE.match(args.start) or not MONTH_RE.match(args.end):
        raise SystemExit("FATAL: --start/--end は YYYY-MM 形式で指定してください")
    if args.end > IN_SAMPLE_END:
        print(
            f"WARN: --end={args.end} は§6で凍結したholdout期間(2023年以降)に抵触します。"
            f"in-sample評価は{IN_SAMPLE_END}までに限定してください（holdoutは選抜済み候補の最終確認にのみ使用）。",
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

    signals_df, diag = generate_shortcover_signals(
        start_bd, end_bd, args.aggregate_threshold, args.exit_threshold
    )

    output_root = Path(args.output_dir)
    kpi_dir = output_root / args.kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_path = kpi_dir / "signals_raw.csv"
    signals_df.to_csv(signals_path, index=False)

    print(f"シグナル生成完了: {len(signals_df)}件")
    print(
        f"内訳: total_records={diag['total_records']} "
        f"disclosure_events_processed={diag['disclosure_events_processed']} "
        f"exit_events_total={diag['exit_events_total']} "
        f"gate_aggregate_pass={diag['gate_aggregate_pass']} "
        f"gate_aggregate_and_decrease_pass={diag['gate_aggregate_and_decrease_pass']} "
        f"gate_transition_pass={diag['gate_transition_pass']}"
    )
    print(f"出力: {signals_path}")

    if args.skip_harness:
        return 0
    if signals_df.empty:
        print("WARN: シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    params = {
        "aggregate_threshold": args.aggregate_threshold,
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
