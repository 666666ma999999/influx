#!/usr/bin/env python3
"""週次margin 需給系バッチ シグナル生成器（第20周・カタログ§7-I I1/I2）。

docs/stock-algo-kpi-catalog.md の §7-I を実装する。`data/jquants/margin/YYYYMMDD.json.gz`
（週次信用取引残高）から、I1 `margin_pampan`（信用買残の重さ＝負けフィルタ仮説の単純化1条件版）
と I2 `karauri_fuel`（信用売残の急増＝踏み上げ燃料仮説）の2シグナルを生成する。

margin レスポンスには公表日（PublishedDate系）フィールドが無い（実データ確認済み・§7-I参照。
`Date`は基準日=週末営業日のみ）。したがって scripts/kpi_margin_signals.py の
`MARGIN_PUBLISH_LAG_BDAYS_DEFAULT`=4（基準日+4営業日を保守的な使用可能日とする既存ルール）
をそのまま再利用する。マージンスナップショットの読み込みは
`kpi_margin_signals.load_margin_snapshots()` をimport再利用する（Canonical Module原則・
データ取得経路の再実装はしない）。20日平均出来高（AdjVo・trailing・当日を含まない）の
計算規約は scripts/kpi_volshock_signals.py の va_avg20 と同一（平均はその日自身のAdjVoを
追加する前の状態で計算する＝look-aheadにならない）。

両KPIとも週次パネル由来のため、生成側では状態遷移（transition）による間引きを行わない
（該当する全ての週を生シグナルとして出力する）。同一銘柄の連続週発火の抑制は
`kpi_event_study.compute_signal_returns` の非重複ポジションルール（直前シグナルのexit_date
以前は破棄）にのみ委ね、実行後に生シグナル数と採用n(dedup後)の差を報告する。

ハーネス呼び出しは `run_event_study()` を使わず低レベルCanonical関数を直接呼ぶ
（EV(なし)の月次ブロック・ブートストラップCIをtrials.jsonlのparamsに含めるため、
compute_stats後・append_trial前に追加計算する必要がある。実装の流儀は
scripts/kpi_event_batch_signals.py の run_trial / classify_exploratory を踏襲・再利用する）。

Usage:
    python3 scripts/kpi_margin_supply_signals.py --kpi margin_pampan --start 2016-11 --end 2022-11
    python3 scripts/kpi_margin_supply_signals.py --kpi karauri_fuel --start 2016-11 --end 2022-11
"""
from __future__ import annotations

import argparse
import re
import sys
import uuid
from collections import defaultdict, deque
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: DATA_ROOT/read_json_gz/now_jstを再利用)
import kpi_event_batch_signals  # noqa: E402  (Canonical Module: classify_exploratory/MULTI_TRIAL_NOTE型の実装流儀を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: compute_signal_returns/compute_stats/judge/bootstrap_ev_ci/write_report_md/append_trialを再利用)
import kpi_margin_signals  # noqa: E402  (Canonical Module: load_margin_snapshots/MARGIN_PUBLISH_LAG_BDAYS_DEFAULTを再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読み込みを再利用)

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# §0確認事項5・§6標本分割で凍結: in-sampleは2016-11〜2022-11。holdout(2023以降)はロック中。
IN_SAMPLE_START = "2016-11"
IN_SAMPLE_END = "2022-11"

KPI_NAME_I1 = "margin_pampan"
KPI_NAME_I2 = "karauri_fuel"

AVG_VOLUME_WINDOW_DEFAULT = 20
ABSORB_DAYS_THRESHOLD_DEFAULT = 10.0  # I1: 買残消化日数 >= 10日（カタログ§2-B原義の簡易1条件版）
GROWTH_LOOKBACK_SNAPSHOTS_DEFAULT = 4  # I2: 週次パネルの並び順で4回前（暦28日固定ではない）
GROWTH_PCT_THRESHOLD_DEFAULT = 0.5  # I2: 4回前比 +50%以上
LEVEL_DAYS_THRESHOLD_DEFAULT = 2.0  # I2: 20日平均出来高の2日分以上（ノイズ除外）

# ウォームアップ用に in-sample 開始より前のデータも計算に含める（20日平均出来高の窓・
# 4回前スナップショット参照の確保）。margin/bars とも実データは2016-07開始のため全期間で計算する。
COMPUTE_FLOOR = "20160701"

PERIOD = (IN_SAMPLE_START, IN_SAMPLE_END)
BASE_RATE_DIR = Path("output/base_rate")
UNIVERSE_WINDOW = 21
TRIALS_PATH = Path("data/kpi_trials/trials.jsonl")
OUTPUT_ROOT = Path("output/kpi")

MULTI_TRIAL_NOTE = (
    "本ラウンド(第20周・需給系バッチ)はI1・I2の2試行同時登録であり累積試行数割引の対象。"
    "個々の結果単独で運用変更しない。"
)
PRE_REGISTERED_SUCCESS_RULE = (
    "promising: EV(なし)CI下限>0 かつ point_lift>=1.5 / "
    "rejected: EV点推定<=0 または point_lift<1.0 / inconclusive: それ以外"
)


# --- margin+出来高 結合系列の構築 --------------------------------------------------


def build_margin_series_with_volume(
    all_bdays: list[str],
    bday_index: dict[str, int],
    end_bd: str,
    publish_lag_bdays: int = kpi_margin_signals.MARGIN_PUBLISH_LAG_BDAYS_DEFAULT,
    volume_window: int = AVG_VOLUME_WINDOW_DEFAULT,
) -> tuple[dict[str, list[dict]], dict]:
    """全履歴の週次marginスナップショットに usable_date と AdjVo20日平均を付与し、
    銘柄別に usable_date 昇順のリストとして返す（I1/I2共通の入力データ）。

    Returns:
        (series_by_code, diag)。series_by_code[code] は usable_date 昇順の
        {"basis_date", "usable_date", "long_vol_adj", "short_vol_adj", "adj_ratio",
        "avg20_adjvo"} のリスト。long/short は basis_date 時点の AdjVo/Vo 倍率で
        調整済み株数に変換済み（Codexレビュー⑧修正・分割/併合銘柄の単位整合）。
        avg20_adjvo は usable_date 時点で観測可能な直近volume_window営業日分のAdjVo平均
        （当日を含まないtrailing窓・kpi_volshock系のva_avg20と同一規約）。窓が揃わない場合はNone。
    """
    snapshots = kpi_margin_signals.load_margin_snapshots()

    diag = {
        "snapshots_total": len(snapshots),
        "skipped_no_bday": 0,
        "skipped_out_of_calendar": 0,
    }

    usable_snapshots: list[tuple[str, str, dict]] = []  # (basis_date, usable_date, by_code)
    for basis_date, by_code in snapshots:
        if basis_date not in bday_index:
            diag["skipped_no_bday"] += 1
            continue
        idx = bday_index[basis_date]
        usable_idx = idx + publish_lag_bdays
        if usable_idx >= len(all_bdays):
            diag["skipped_out_of_calendar"] += 1
            continue
        usable_date = all_bdays[usable_idx]
        usable_snapshots.append((basis_date, usable_date, by_code))

    if not usable_snapshots:
        raise SystemExit("FATAL: 使用可能なmarginスナップショットが0件です")

    usable_snapshots.sort(key=lambda t: t[1])
    usable_date_to_snapshot = {u: (b, by_code) for b, u, by_code in usable_snapshots}
    usable_date_set = set(usable_date_to_snapshot.keys())

    scan_end_idx = min(bday_index[end_bd], len(all_bdays) - 1)
    scan_days = all_bdays[: scan_end_idx + 1]  # 全履歴を通しでAdjVo履歴を積み上げる(ウォームアップ込み)

    adjvo_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=volume_window))
    avg20_by_usable_date: dict[str, dict[str, float]] = {}

    for d in scan_days:
        if d in usable_date_set:
            _basis_date, by_code = usable_date_to_snapshot[d]
            snap_avg: dict[str, float] = {}
            for code in by_code:
                hist = adjvo_hist.get(code)
                if hist is not None and len(hist) == volume_window:
                    snap_avg[code] = sum(hist) / len(hist)
            avg20_by_usable_date[d] = snap_avg
        bars_d = measure_base_rate.load_bars_day(d)
        for code, rec in bars_d.items():
            adjvo = rec.get("AdjVo")
            if adjvo:
                adjvo_hist[code].append(adjvo)

    series_by_code: dict[str, list[dict]] = defaultdict(list)
    diag["snapshot_code_rows"] = 0
    diag["insufficient_volume_history"] = 0
    # Codexレビュー⑧修正: margin残（未調整株数）を basis_date 時点の AdjVo/Vo で
    # 調整済み株数へ変換してから格納する（分割・併合銘柄の単位不整合を解消）
    diag["adj_ratio_uncomputable"] = 0
    diag["adj_ratio_walkback_used"] = 0
    diag["adj_ratio_neq_one_rows"] = 0
    for basis_date, usable_date, by_code in usable_snapshots:
        avgs = avg20_by_usable_date.get(usable_date, {})
        for code, (long_v, short_v) in by_code.items():
            diag["snapshot_code_rows"] += 1
            avg20 = avgs.get(code)
            if avg20 is None:
                diag["insufficient_volume_history"] += 1
            ratio, walkback = _adj_ratio_at(code, basis_date, bday_index, all_bdays)
            if ratio is None:
                diag["adj_ratio_uncomputable"] += 1
                continue
            if walkback:
                diag["adj_ratio_walkback_used"] += 1
            if abs(ratio - 1.0) > 1e-9:
                diag["adj_ratio_neq_one_rows"] += 1
            series_by_code[code].append(
                {
                    "basis_date": basis_date,
                    "usable_date": usable_date,
                    "long_vol_adj": (long_v * ratio) if long_v is not None else None,
                    "short_vol_adj": (short_v * ratio) if short_v is not None else None,
                    "adj_ratio": ratio,
                    "avg20_adjvo": avg20,
                }
            )
    return series_by_code, diag


# --- I1: margin_pampan（信用買残の重さ） -------------------------------------------


def _adj_ratio_at(code: str, basis_date: str, bday_index: dict, all_bdays: list, max_walkback_bdays: int = 5) -> tuple:
    """basis_date（margin基準日）時点の調整倍率 AdjVo/Vo を返す（Codexレビュー⑧修正）。

    margin の LongVol/ShrtVol は未調整の実株数であり、分割調整済みの AdjVo と直接割ると
    分割・併合銘柄で単位が崩れる（例: 10倍乖離）。同一日付の bars レコードから
    ratio = AdjVo/Vo を取り、信用残を調整済み株数へ変換してから比較する。
    basis_date に bars レコードがない（売買停止等）場合は最大 max_walkback_bdays 営業日
    遡る。それでも不能なら (None, None) を返し、呼び出し側で除外・診断カウントする。

    Returns:
        (ratio, walkback_used): ratio=None なら計算不能。walkback_used は遡った営業日数。
    """
    idx = bday_index.get(basis_date)
    if idx is None:
        return None, None
    for back in range(max_walkback_bdays + 1):
        j = idx - back
        if j < 0:
            break
        rec = measure_base_rate.load_bars_day(all_bdays[j]).get(code)
        if rec:
            vo = rec.get("Vo")
            adjvo = rec.get("AdjVo")
            if vo and adjvo:
                return adjvo / vo, back
    return None, None


def generate_margin_pampan_signals(
    series_by_code: dict[str, list[dict]],
    start_bd: str,
    end_bd: str,
    absorb_days_threshold: float = ABSORB_DAYS_THRESHOLD_DEFAULT,
) -> tuple[pd.DataFrame, dict]:
    """消化日数 = LongVol(基準日) / avg20_adjvo(usable_date) が absorb_days_threshold 以上の
    週を生シグナルとして全件出力する（状態遷移による間引きは行わない）。
    """
    diag = {
        "rows_in_window": 0,
        "rows_missing_avg20": 0,
        "rows_zero_or_missing_longvol": 0,
        "signals_raw": 0,
    }
    rows = []
    for code, series in series_by_code.items():
        for rec in series:
            usable_date = rec["usable_date"]
            if not (start_bd <= usable_date <= end_bd):
                continue
            diag["rows_in_window"] += 1
            avg20 = rec["avg20_adjvo"]
            long_v = rec["long_vol_adj"]  # Codexレビュー⑧修正: 調整済み株数
            if avg20 is None or avg20 <= 0:
                diag["rows_missing_avg20"] += 1
                continue
            if long_v is None or long_v <= 0:
                diag["rows_zero_or_missing_longvol"] += 1
                continue
            absorb_days = long_v / avg20
            if absorb_days >= absorb_days_threshold:
                diag["signals_raw"] += 1
                rows.append(
                    {
                        "signal_date": usable_date,
                        "code": code,
                        "basis_date": rec["basis_date"],
                        "long_vol_adj": long_v,
                        "adj_ratio": rec["adj_ratio"],
                        "avg20_adjvo": avg20,
                        "absorb_days": absorb_days,
                    }
                )
    return pd.DataFrame(rows), diag


# --- I2: karauri_fuel（信用売残の急増） --------------------------------------------


def generate_karauri_fuel_signals(
    series_by_code: dict[str, list[dict]],
    start_bd: str,
    end_bd: str,
    growth_lookback: int = GROWTH_LOOKBACK_SNAPSHOTS_DEFAULT,
    growth_pct_threshold: float = GROWTH_PCT_THRESHOLD_DEFAULT,
    level_days_threshold: float = LEVEL_DAYS_THRESHOLD_DEFAULT,
) -> tuple[pd.DataFrame, dict]:
    """(ShrtVolの growth_lookback回前比 増加率 >= growth_pct_threshold) かつ
    (ShrtVol >= avg20_adjvo(usable_date) * level_days_threshold) の週を
    生シグナルとして全件出力する（状態遷移による間引きは行わない）。
    """
    diag = {
        "rows_in_window": 0,
        "rows_insufficient_snapshot_history": 0,  # growth_lookback回未満(新規上場等)
        "rows_missing_avg20": 0,
        "rows_prior_shortvol_undefined": 0,  # 4回前ShrtVolが0以下または欠測
        "growth_pass": 0,
        "level_pass": 0,
        "signals_raw": 0,
    }
    rows = []
    for code, series in series_by_code.items():
        for i, rec in enumerate(series):
            usable_date = rec["usable_date"]
            if not (start_bd <= usable_date <= end_bd):
                continue
            diag["rows_in_window"] += 1
            if i < growth_lookback:
                diag["rows_insufficient_snapshot_history"] += 1
                continue
            prior = series[i - growth_lookback]
            prior_short = prior["short_vol_adj"]  # Codexレビュー⑧修正: 両端とも調整済み株数
            cur_short = rec["short_vol_adj"]
            avg20 = rec["avg20_adjvo"]
            if avg20 is None or avg20 <= 0:
                diag["rows_missing_avg20"] += 1
                continue
            if prior_short is None or prior_short <= 0 or cur_short is None:
                diag["rows_prior_shortvol_undefined"] += 1
                continue
            growth = cur_short / prior_short - 1
            is_growth = growth >= growth_pct_threshold
            is_level = cur_short >= avg20 * level_days_threshold
            if is_growth:
                diag["growth_pass"] += 1
            if is_level:
                diag["level_pass"] += 1
            if is_growth and is_level:
                diag["signals_raw"] += 1
                rows.append(
                    {
                        "signal_date": usable_date,
                        "code": code,
                        "basis_date": rec["basis_date"],
                        "short_vol_adj": cur_short,
                        "prior_short_vol_adj": prior_short,
                        "growth": growth,
                        "avg20_adjvo": avg20,
                        "level_ratio": cur_short / avg20,
                    }
                )
    return pd.DataFrame(rows), diag


# --- パフォーマンス最適化（統計結果を変えない前提の事前フィルタ） -----------------------


def filter_never_in_universe(
    signals_df: pd.DataFrame, universes_by_month: dict[str, set]
) -> tuple[pd.DataFrame, dict]:
    """`kpi_event_study._universe_membership`（実際の判定と完全同一のCanonical関数）を
    生シグナル全件に事前適用し、月次確定ユニバース所属で真にout_of_universeになる行を
    ハーネス投入前に除外する（統計結果を変えない純粋な性能最適化）。

    `kpi_event_study.compute_signal_returns` の非重複ポジション状態（last_exit_date）は
    真にin_universeだった行のみが更新する。out_of_universeの行は元々n（採用シグナル）に
    寄与せず、他行の重複除去状態にも一切影響しない（in_universeな行同士の順序・間引きは
    out_of_universeの行の有無に依存しない）。したがってこの事前フィルタはn/lift/EV等の
    統計量を一切変えず、`compute_forward_return_for_code`（bars読込×損切りシミュレーション）
    を無駄撃ちする回数だけを削減する（実測: margin_pampanで生186,738件中、真にin_universeなのは
    937件[0.5%]のみ。「一度でもどこかの月のユニバースに入った銘柄」まで緩めたバージョンでは
    51,253件までしか絞れずハーネスに1時間超かかったため、月次判定まで正確に適用する版に変更）。
    """
    signal_months = signals_df["signal_date"].astype(str).str[:6]
    codes_str = signals_df["code"].astype(str)
    keep_mask = pd.Series(
        [
            kpi_event_study._universe_membership(code, month, universes_by_month)[0]
            for code, month in zip(codes_str, signal_months)
        ],
        index=signals_df.index,
    )
    diag = {
        "rows_before_universe_prefilter": int(len(signals_df)),
        "rows_dropped_not_in_universe_that_month": int((~keep_mask).sum()),
        "rows_after_universe_prefilter": int(keep_mask.sum()),
    }
    return signals_df[keep_mask].reset_index(drop=True), diag


# --- ハーネス実行（EV(なし)CIをtrials.jsonlに含めるため低レベルCanonical関数を直接呼ぶ） -------


def run_trial(
    signals_df: pd.DataFrame,
    kpi_name: str,
    base_params: dict,
    all_bdays: list[str],
    bday_index: dict[str, int],
    regime_by_day: dict[str, str],
    base_rate_by_month: dict[str, float],
    universes_by_month: dict[str, set],
    signals_df_for_csv: pd.DataFrame | None = None,
) -> dict:
    """低レベルCanonical関数（kpi_event_study.compute_signal_returns/compute_stats/judge/
    bootstrap_ev_ci/write_report_md/append_trial）を直接呼び出す。実装の流儀は
    scripts/kpi_event_batch_signals.run_trial を踏襲する（run_event_study()を使わない理由も同じ）。

    signals_df はハーネス投入用（ユニバース事前フィルタ後）。signals_df_for_csv を指定した場合、
    signals_raw.csv にはそちら（真の生シグナル・フィルタ前）を保存する
    （filter_never_in_universe は性能最適化のみが目的で「生シグナル」の定義自体を変えないため）。
    """
    harness_signals_df = signals_df[["signal_date", "code"]].copy()
    returns_df, diag = kpi_event_study.compute_signal_returns(
        harness_signals_df, bday_index, all_bdays, regime_by_day, universes_by_month, defer_entry=False
    )
    diag["regime_filter"] = None
    diag["pre_regime_filter_count"] = len(harness_signals_df)

    in_universe_df = (
        returns_df[returns_df["in_universe"]].reset_index(drop=True) if len(returns_df) else returns_df
    )
    if in_universe_df.empty:
        raise SystemExit(f"FATAL: {kpi_name}のin_universeシグナルが0件です")

    stats = kpi_event_study.compute_stats(in_universe_df, base_rate_by_month, PERIOD)
    verdict, reasons = kpi_event_study.judge(stats)

    ev_ci = kpi_event_study.bootstrap_ev_ci(in_universe_df, ev_column="ret")
    conclusion = kpi_event_batch_signals.classify_exploratory(
        stats.get("point_lift"), ev_ci["point_ev"], ev_ci["ci_low"]
    )

    params = {
        **base_params,
        "defer_entry": False,
        "harness_input_signal_count": diag["raw_signal_count"],  # ユニバース事前フィルタ後の投入数
        "duplicate_discarded": diag["duplicate_discarded"],
        "ev_none_point": ev_ci["point_ev"],
        "ev_none_ci_low": ev_ci["ci_low"],
        "ev_none_ci_high": ev_ci["ci_high"],
        "ev_ci_n_boot_valid": ev_ci["n_boot_valid"],
        "exploratory_conclusion": conclusion,
        "pre_registered_success_rule": PRE_REGISTERED_SUCCESS_RULE,
        "round": "20_margin_supply_batch",
        "multi_trial_discount_note": MULTI_TRIAL_NOTE,
    }

    kpi_dir = OUTPUT_ROOT / kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    returns_path = kpi_dir / "returns.csv"
    report_path = kpi_dir / "report.md"
    signals_path = kpi_dir / "signals_raw.csv"
    (signals_df_for_csv if signals_df_for_csv is not None else signals_df).to_csv(signals_path, index=False)
    returns_df.to_csv(returns_path, index=False)
    kpi_event_study.write_report_md(
        report_path, kpi_name, params, PERIOD, diag, stats, verdict, reasons, in_universe_df,
        defer_stats=None,
    )
    # Codexレビュー⑧[LOW]修正: write_report_md（Canonical・他KPIスクリプト共有）の
    # 「生シグナル数」ラベルは本スクリプトではユニバース事前フィルタ後の値を指しており誤解を招くため、
    # 共有関数自体は変更せず本スクリプトの出力ファイルのみ書き出し後にラベルを置換する。
    report_text = report_path.read_text(encoding="utf-8")
    report_text = report_text.replace(
        f"- 生シグナル数: {diag['raw_signal_count']}",
        f"- ハーネス投入数(ユニバース事前フィルタ後): {diag['raw_signal_count']}"
        f"　※真の生シグナル数={params.get('true_raw_signal_count_before_prefilter', '-')}"
        "（下記「探索的一次結論」参照）",
        1,
    )
    report_path.write_text(report_text, encoding="utf-8")
    with open(report_path, "a", encoding="utf-8") as f:
        f.write("\n## 探索的一次結論（第20周・カタログ§7-I事前登録の共通ルール）\n")
        if ev_ci["point_ev"] is not None:
            f.write(
                f"- EV(なし・往復コスト0.3%控除込)点推定 = {ev_ci['point_ev']:.4%}"
                f"（月次ブロック・ブートストラップ95%CI="
                f"[{ev_ci['ci_low']:.4%}, {ev_ci['ci_high']:.4%}]"
                f"・有効ブートストラップ数={ev_ci['n_boot_valid']}）\n"
            )
        else:
            f.write("- EV(なし)点推定 = 計算不能\n")
        f.write(f"- リフト点推定 = {stats.get('point_lift')}\n")
        f.write(
            f"- 真の生シグナル数={params.get('true_raw_signal_count_before_prefilter', '-')} "
            f"/ ユニバース事前フィルタ後(ハーネス投入)={diag['raw_signal_count']} "
            f"/ 重複除去={diag['duplicate_discarded']} / 採用n(dedup後)={stats['n']}\n"
        )
        f.write(f"- **探索的一次結論: {conclusion}**\n")
        f.write(f"- {MULTI_TRIAL_NOTE}\n")

    trial_record = {
        "run_id": uuid.uuid4().hex,
        "ts": jq_fetch.now_jst().isoformat(),
        "kpi_name": kpi_name,
        "params": params,
        "period": {"start": PERIOD[0], "end": PERIOD[1]},
        "n": stats["n"],
        "lift": stats.get("point_lift"),
        "ci_low": stats.get("ci_low"),
        "ci_high": stats.get("ci_high"),
        "ev": stats.get("ev_stop8"),
        "verdict": verdict,
        "entry_mode": "fixed_t1",
        "regime_filter": None,
    }
    kpi_event_study.append_trial(trial_record, TRIALS_PATH)

    print(
        f"{kpi_name} 完了: 真の生シグナル={params.get('true_raw_signal_count_before_prefilter', '-')} "
        f"ハーネス投入(事前フィルタ後)={diag['raw_signal_count']} 重複除去={diag['duplicate_discarded']} "
        f"採用n={stats['n']} lift={stats.get('point_lift')} "
        f"ci95=[{stats.get('ci_low')},{stats.get('ci_high')}] verdict(§6)={verdict} "
        f"EV(なし)点推定={ev_ci['point_ev']} CI95=[{ev_ci['ci_low']},{ev_ci['ci_high']}] "
        f"月平均n={stats['avg_monthly_n']:.2f} 探索的一次結論={conclusion}"
    )
    print(f"{kpi_name} report: {report_path}")
    print(f"{kpi_name} returns: {returns_path}")
    print(f"{kpi_name} signals_raw: {signals_path}")

    return {
        "kpi_name": kpi_name,
        "n": stats["n"],
        "lift": stats.get("point_lift"),
        "ev_ci": ev_ci,
        "verdict": verdict,
        "conclusion": conclusion,
        "stats": stats,
        "diag": diag,
    }


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="週次margin 需給系バッチ シグナル生成器(カタログ§7-I I1/I2) + KPI検証ハーネス実行"
    )
    parser.add_argument("--kpi", required=True, choices=[KPI_NAME_I1, KPI_NAME_I2], help="実行するKPI")
    parser.add_argument("--start", default=IN_SAMPLE_START, help=f"検索開始月 YYYY-MM（既定 {IN_SAMPLE_START}）")
    parser.add_argument("--end", default=IN_SAMPLE_END, help=f"検索終了月 YYYY-MM（既定 {IN_SAMPLE_END}）")
    parser.add_argument("--publish-lag-bdays", type=int, default=kpi_margin_signals.MARGIN_PUBLISH_LAG_BDAYS_DEFAULT)
    parser.add_argument("--absorb-days-threshold", type=float, default=ABSORB_DAYS_THRESHOLD_DEFAULT)
    parser.add_argument("--growth-lookback-snapshots", type=int, default=GROWTH_LOOKBACK_SNAPSHOTS_DEFAULT)
    parser.add_argument("--growth-pct-threshold", type=float, default=GROWTH_PCT_THRESHOLD_DEFAULT)
    parser.add_argument("--level-days-threshold", type=float, default=LEVEL_DAYS_THRESHOLD_DEFAULT)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="台帳追記(append_trial)とreport.md/returns.csv書き込みをスキップし、生シグナル数のみ表示",
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
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    start_bound = args.start.replace("-", "") + "01"
    end_bound = args.end.replace("-", "") + "31"
    start_bd = next((d for d in all_bdays if d >= start_bound), None)
    end_bd = next((d for d in reversed(all_bdays) if d <= end_bound), None)
    if start_bd is None or end_bd is None:
        raise SystemExit("FATAL: 指定期間に営業日が見つかりません")

    print(f"marginスナップショット読み込み・AdjVo20日平均系列を構築中（{args.kpi}）...", file=sys.stderr)
    series_by_code, series_diag = build_margin_series_with_volume(
        all_bdays, bday_index, end_bd, args.publish_lag_bdays
    )
    print(
        f"系列構築完了: snapshots_total={series_diag['snapshots_total']} "
        f"skipped_no_bday={series_diag['skipped_no_bday']} "
        f"skipped_out_of_calendar={series_diag['skipped_out_of_calendar']} "
        f"snapshot_code_rows={series_diag['snapshot_code_rows']} "
        f"insufficient_volume_history={series_diag['insufficient_volume_history']}",
        file=sys.stderr,
    )

    if args.kpi == KPI_NAME_I1:
        signals_df, gen_diag = generate_margin_pampan_signals(
            series_by_code, start_bd, end_bd, args.absorb_days_threshold
        )
        base_params = {
            "signal_definition": "消化日数=LongVol_adj(基準日)/AdjVo20日平均(usable_date)>=absorb_days_threshold"
            "（LongVol_adj=LongVol×adj_ratio[=同日AdjVo/Vo]で分割調整済み株数に変換済み。"
            "カタログ§2-B原義[倍率>10 かつ 消化日数>10日 かつ 高値-15%]の簡易1条件版・"
            "倍率条件と高値からの下落条件は含めない）",
            "absorb_days_threshold": args.absorb_days_threshold,
            "publish_lag_bdays": args.publish_lag_bdays,
            "avg_volume_window": AVG_VOLUME_WINDOW_DEFAULT,
            "codex_review8_fix": (
                "Codexレビュー⑧指摘（単位不整合）反映: LongVol/ShrtVolは未調整実株数のため、"
                "basis_date時点の同日bars(Vo,AdjVo)から adj_ratio=AdjVo/Vo を求め調整済み株数に"
                "変換してからAdjVo20日平均と比較。adj_ratio_uncomputable="
                f"{series_diag.get('adj_ratio_uncomputable')} / walkback_used="
                f"{series_diag.get('adj_ratio_walkback_used')} / neq_one_rows="
                f"{series_diag.get('adj_ratio_neq_one_rows')}（分母={series_diag.get('snapshot_code_rows')}）"
            ),
        }
    else:
        signals_df, gen_diag = generate_karauri_fuel_signals(
            series_by_code, start_bd, end_bd,
            args.growth_lookback_snapshots, args.growth_pct_threshold, args.level_days_threshold,
        )
        base_params = {
            "signal_definition": "(ShrtVol_adjのgrowth_lookback_snapshots回前比増加率>=growth_pct_threshold)"
            " かつ (ShrtVol_adj>=AdjVo20日平均(usable_date)*level_days_threshold)"
            "（ShrtVol_adj=ShrtVol×adj_ratio[=同日AdjVo/Vo]で両端とも分割調整済み株数に変換済み）",
            "growth_lookback_snapshots": args.growth_lookback_snapshots,
            "growth_pct_threshold": args.growth_pct_threshold,
            "level_days_threshold": args.level_days_threshold,
            "codex_review8_fix": (
                "Codexレビュー⑧指摘（単位不整合）反映: ShrtVolは未調整実株数のため、"
                "basis_date時点の同日bars(Vo,AdjVo)から adj_ratio=AdjVo/Vo を求め調整済み株数に"
                "変換してから比較（4回前スナップショットも同様に調整済み同士で比較）。"
                f"adj_ratio_uncomputable={series_diag.get('adj_ratio_uncomputable')} / walkback_used="
                f"{series_diag.get('adj_ratio_walkback_used')} / neq_one_rows="
                f"{series_diag.get('adj_ratio_neq_one_rows')}（分母={series_diag.get('snapshot_code_rows')}）"
            ),
            "publish_lag_bdays": args.publish_lag_bdays,
            "avg_volume_window": AVG_VOLUME_WINDOW_DEFAULT,
        }

    print(f"シグナル生成完了({args.kpi}): 生成内訳={gen_diag}", file=sys.stderr)

    if signals_df.empty:
        print(f"WARN: {args.kpi} シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    base_rate_by_month = kpi_event_study.load_base_rate_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)
    universes_by_month = kpi_event_study.load_universe_by_month(BASE_RATE_DIR, UNIVERSE_WINDOW)

    signals_df_raw = signals_df
    signals_df, prefilter_diag = filter_never_in_universe(signals_df, universes_by_month)
    print(
        f"ユニバース事前フィルタ（性能最適化・統計結果は不変・kpi_event_study._universe_membership"
        f"と完全同一の月次判定を再利用）: "
        f"{prefilter_diag['rows_before_universe_prefilter']}件 → "
        f"{prefilter_diag['rows_after_universe_prefilter']}件 "
        f"（月次確定ユニバース非該当={prefilter_diag['rows_dropped_not_in_universe_that_month']}件を除外）",
        file=sys.stderr,
    )
    base_params_prefilter_note = {
        "universe_prefilter_note": (
            f"性能最適化のみ目的の事前フィルタ: `kpi_event_study._universe_membership`"
            f"（実際の判定と完全同一のCanonical関数）を全件に事前適用し、月次確定ユニバース"
            f"非該当の生シグナル{prefilter_diag['rows_dropped_not_in_universe_that_month']}件"
            f"（真の生シグナル総数{len(signals_df_raw)}件中）をハーネス投入前に除外。"
            f"これらは元々out_of_universeでnに寄与せず他銘柄の重複除去状態にも無関係のため、"
            f"n/lift/EV等の統計量への影響はゼロ（compute_forward_return_for_codeの実行回数のみ削減）。"
        ),
        "true_raw_signal_count_before_prefilter": len(signals_df_raw),
    }

    if args.dry_run:
        print(
            f"--dry-run: {args.kpi} 真の生シグナル数={len(signals_df_raw)}件 / "
            f"ハーネス投入数(事前フィルタ後)={len(signals_df)}件（ハーネス実行・台帳追記はスキップ）"
        )
        return 0

    topix_close = measure_base_rate.load_topix_series()
    regime_by_day = measure_base_rate.build_regime_series(topix_close)

    run_trial(
        signals_df, args.kpi, {**base_params, **base_params_prefilter_note},
        all_bdays, bday_index, regime_by_day, base_rate_by_month, universes_by_month,
        signals_df_for_csv=signals_df_raw,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
