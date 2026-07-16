#!/usr/bin/env python3
"""需給・決算系 特徴量の Canonical 部品（銘柄×日付→スカラー）。

将来のスクリーニング batch_v2（期間再定義後）で使う需給・決算系の特徴量を、
既存の凍結済みシグナル生成器（committed 試行の再現性保護対象）から一切独立させず、
それらの Canonical 関数を「そのまま再利用」して (code, asof_bd) → スカラー の照会
インターフェースへ被せるだけのモジュール。シグナル生成ロジックは本モジュールでは
一切再実装しない（Dual-Path 禁止・look-ahead/単位不整合/会計年度キー欠落の既知バグを
二重管理で引き込まないため）。

part-ization 対象と Canonical owner:
    - compute_margin_absorb_days / compute_karauri_growth
        → scripts/kpi_margin_supply_signals.build_margin_series_with_volume（第20周・§7-I）。
          AdjVo/Vo 単位調整（Codexレビュー⑧修正）・公表ラグ・AdjVo20日平均は同関数が保持。
    - compute_shortcover_state
        → scripts/kpi_shortcover_signals.generate_shortcover_signals（§2-B #7・遷移日ロジック）。
    - compute_beat_pct
        → scripts/kpi_event_batch_signals.generate_sue_beat_signals（§7-G T2・fiscal_year_key=True・as-of）。

いずれも「コンテキストを一度だけ構築 → (code, asof_bd) で O(1) 照会」の型。asof_bd は
各シグナルが PIT で利用可能になる日（既存生成器の signal_date / margin の usable_date）。

正しさは scripts/_tmp_features_equivalence.py が既存凍結出力（output/kpi/*/signals_raw.csv）
との数値一致で検証する。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import kpi_event_batch_signals as keb  # noqa: E402  (Canonical: generate_sue_beat_signals / _event_bd_bounds / T2_*)
import kpi_margin_supply_signals as kms  # noqa: E402  (Canonical: build_margin_series_with_volume / 各既定定数)
import kpi_shortcover_signals as kss  # noqa: E402  (Canonical: generate_shortcover_signals / 各既定閾値)
import measure_base_rate  # noqa: E402  (Canonical: カレンダー・営業日列)

# 全 KPI 共通の in-sample 期間（§6 で凍結・kms/kss/pead いずれも同値）。
IN_SAMPLE_START = kms.IN_SAMPLE_START
IN_SAMPLE_END = kms.IN_SAMPLE_END


def _all_bdays() -> list[str]:
    """全営業日列を Canonical 経路（measure_base_rate）から得る。"""
    calendar_days = measure_base_rate.load_calendar_days()
    return measure_base_rate.all_business_days(calendar_days)


# --- margin 系（買残消化日数 / 売残急増率） -------------------------------------------


@dataclass
class MarginFeatureContext:
    """build_margin_series_with_volume の出力に (code, usable_date) 索引を付けた保持体。

    series_by_code[code] は usable_date 昇順の記録リスト（Canonical と同一オブジェクト・
    karauri の n回前参照が依存する並び順をそのまま保持）。lookup[(code, usable_date)] は
    (record, その series 内での位置 index) を返す。
    """

    series_by_code: dict[str, list[dict]]
    lookup: dict[tuple[str, str], tuple[dict, int]]
    start_bd: str
    end_bd: str
    diag: dict


def build_margin_feature_context(
    start_month: str = IN_SAMPLE_START,
    end_month: str = IN_SAMPLE_END,
    publish_lag_bdays: Optional[int] = None,
    volume_window: Optional[int] = None,
) -> MarginFeatureContext:
    """margin 特徴量コンテキストを一度だけ構築する（既定引数は凍結生成時と同一）。

    kms.build_margin_series_with_volume をそのまま呼ぶ（引数未指定時は同関数の既定＝
    MARGIN_PUBLISH_LAG_BDAYS_DEFAULT / AVG_VOLUME_WINDOW_DEFAULT を採用し、
    output/kpi/margin_pampan・karauri_fuel の凍結出力と同一系列を再現する）。
    """
    all_bdays = _all_bdays()
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    start_bd, end_bd = keb._event_bd_bounds(start_month, end_month, all_bdays)

    kwargs: dict[str, Any] = {}
    if publish_lag_bdays is not None:
        kwargs["publish_lag_bdays"] = publish_lag_bdays
    if volume_window is not None:
        kwargs["volume_window"] = volume_window
    series_by_code, diag = kms.build_margin_series_with_volume(
        all_bdays, bday_index, end_bd, **kwargs
    )

    lookup: dict[tuple[str, str], tuple[dict, int]] = {}
    for code, series in series_by_code.items():
        for i, rec in enumerate(series):
            lookup[(code, rec["usable_date"])] = (rec, i)

    return MarginFeatureContext(
        series_by_code=series_by_code, lookup=lookup, start_bd=start_bd, end_bd=end_bd, diag=diag
    )


def compute_margin_absorb_days(
    code: str, asof_bd: str, ctx: MarginFeatureContext
) -> Optional[float]:
    """買残消化日数 = LongVol_adj(基準日) / AdjVo20日平均(usable_date=asof_bd)。

    kms.generate_margin_pampan_signals と同一定義（調整済み株数 long_vol_adj を AdjVo20日
    平均で割る）。該当スナップショットが無い・平均や買残が欠測/非正なら None を返す。

    Args:
        code: 銘柄コード。
        asof_bd: margin スナップショットの usable_date（PIT 使用可能日 = 凍結 CSV の signal_date）。
        ctx: build_margin_feature_context の返り値。

    Returns:
        消化日数（float）。計算不能なら None。
    """
    entry = ctx.lookup.get((code, asof_bd))
    if entry is None:
        return None
    rec, _ = entry
    avg20 = rec["avg20_adjvo"]
    long_v = rec["long_vol_adj"]
    if avg20 is None or avg20 <= 0:
        return None
    if long_v is None or long_v <= 0:
        return None
    return long_v / avg20


def compute_karauri_growth(
    code: str,
    asof_bd: str,
    ctx: MarginFeatureContext,
    growth_lookback: Optional[int] = None,
) -> Optional[float]:
    """売残急増率 = ShrtVol_adj(asof) / ShrtVol_adj(growth_lookback回前スナップショット) - 1。

    kms.generate_karauri_fuel_signals と同一定義（前後とも調整済み株数・週次パネルの並び順で
    growth_lookback 回前。暦日固定ではない）。位置が不足（新規上場等）・前値が欠測/非正・
    現値が欠測なら None を返す。

    Args:
        code: 銘柄コード。
        asof_bd: margin スナップショットの usable_date（= 凍結 CSV の signal_date）。
        ctx: build_margin_feature_context の返り値。
        growth_lookback: 何回前のスナップショットと比較するか。既定は kms の
            GROWTH_LOOKBACK_SNAPSHOTS_DEFAULT。

    Returns:
        増加率（float・0.5 なら +50%）。計算不能なら None。
    """
    lookback = growth_lookback if growth_lookback is not None else kms.GROWTH_LOOKBACK_SNAPSHOTS_DEFAULT
    entry = ctx.lookup.get((code, asof_bd))
    if entry is None:
        return None
    rec, i = entry
    if i < lookback:
        return None
    prior = ctx.series_by_code[code][i - lookback]
    prior_short = prior["short_vol_adj"]
    cur_short = rec["short_vol_adj"]
    if prior_short is None or prior_short <= 0 or cur_short is None:
        return None
    return cur_short / prior_short - 1


# --- shortsale 系（空売り残高 減少転換状態） ----------------------------------------


@dataclass
class ShortcoverFeatureContext:
    """generate_shortcover_signals の遷移日出力を (code, signal_date) で索引した保持体。"""

    lookup: dict[tuple[str, str], dict]
    start_bd: str
    end_bd: str
    diag: dict


def build_shortcover_feature_context(
    start_month: str = IN_SAMPLE_START,
    end_month: str = IN_SAMPLE_END,
    aggregate_threshold: Optional[float] = None,
    exit_threshold: Optional[float] = None,
) -> ShortcoverFeatureContext:
    """shortcover 特徴量コンテキストを一度だけ構築する（既定閾値は凍結生成時と同一）。

    kss.generate_shortcover_signals をそのまま呼び、返る遷移日シグナル（既に
    [start_bd, end_bd] に絞り込み済み）を (code, signal_date) で索引する。
    """
    all_bdays = _all_bdays()
    start_bd, end_bd = keb._event_bd_bounds(start_month, end_month, all_bdays)
    agg = aggregate_threshold if aggregate_threshold is not None else kss.AGGREGATE_THRESHOLD_DEFAULT
    exit_t = exit_threshold if exit_threshold is not None else kss.EXIT_THRESHOLD_DEFAULT

    signals_df, diag = kss.generate_shortcover_signals(start_bd, end_bd, agg, exit_t)

    lookup: dict[tuple[str, str], dict] = {}
    for rec in signals_df.to_dict("records"):
        lookup[(str(rec["code"]), str(rec["signal_date"]))] = rec

    return ShortcoverFeatureContext(lookup=lookup, start_bd=start_bd, end_bd=end_bd, diag=diag)


def compute_shortcover_state(
    code: str, asof_bd: str, ctx: ShortcoverFeatureContext
) -> Optional[dict]:
    """空売り残高の減少転換状態を返す（asof_bd に遷移シグナルが立っていれば、その状態）。

    kss.generate_shortcover_signals の遷移検出（(合計>=閾値) かつ (genuine_delta<0) が成立に
    転じた公表日の翌営業日）そのもの。asof_bd に遷移が無ければ None。

    Args:
        code: 銘柄コード。
        asof_bd: シグナル確定日（= DiscDate の翌営業日・凍結 CSV の signal_date）。
        ctx: build_shortcover_feature_context の返り値。

    Returns:
        {disc_date, aggregate_after, genuine_delta, has_exit_event_same_batch,
        n_active_reporters, ...} の dict。遷移が無ければ None。
    """
    return ctx.lookup.get((str(code), str(asof_bd)))


# --- 決算系（対直前予想 営業利益ビート率） -------------------------------------------


@dataclass
class BeatFeatureContext:
    """generate_sue_beat_signals の出力を (code, signal_date) で索引した保持体。"""

    lookup: dict[tuple[str, str], dict]
    start_bd: str
    end_bd: str
    diag: dict


def build_beat_feature_context(
    start_month: str = IN_SAMPLE_START,
    end_month: str = IN_SAMPLE_END,
    threshold: Optional[float] = None,
    lookback_days: Optional[int] = None,
) -> BeatFeatureContext:
    """beat_pct 特徴量コンテキストを一度だけ構築する（既定は凍結生成時と同一）。

    keb.generate_sue_beat_signals をそのまま呼ぶ（fiscal_year_key=True・as-of・reaction_day は
    同関数が保持）。既定 threshold/lookback は T2 の凍結値。出力を (code, signal_date) で索引。
    """
    all_bdays = _all_bdays()
    start_bd, end_bd = keb._event_bd_bounds(start_month, end_month, all_bdays)
    th = threshold if threshold is not None else keb.T2_THRESHOLD
    lb = lookback_days if lookback_days is not None else keb.T2_LOOKBACK_DAYS

    signals_df, diag = keb.generate_sue_beat_signals(start_bd, end_bd, th, lb)

    lookup: dict[tuple[str, str], dict] = {}
    for rec in signals_df.to_dict("records"):
        lookup[(str(rec["code"]), str(rec["signal_date"]))] = rec

    return BeatFeatureContext(lookup=lookup, start_bd=start_bd, end_bd=end_bd, diag=diag)


def compute_beat_pct(code: str, asof_bd: str, ctx: BeatFeatureContext) -> Optional[float]:
    """直近決算の対直前予想 営業利益ビート率 = OP実績 / 直前OP予想(同一CurFYEn) - 1。

    keb.generate_sue_beat_signals と同一定義（2Q/FY 限定・fiscal_year_key=True・as-of の
    reaction_day で確定）。asof_bd（= signal_date）にビート開示が無ければ None。

    Args:
        code: 銘柄コード。
        asof_bd: 反応日 signal_date（= 凍結 CSV の signal_date）。
        ctx: build_beat_feature_context の返り値。

    Returns:
        ビート率（float・0.10 なら +10%）。該当が無ければ None。
    """
    rec = ctx.lookup.get((str(code), str(asof_bd)))
    if rec is None:
        return None
    return rec["beat_pct"]
