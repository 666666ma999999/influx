#!/usr/bin/env python3
"""既試行fingerprint索引ビルダー（近傍再投入の機械的防止資産・2026-07-18ユーザー承認）。

新しい仮説候補が「既に試した/棄却した/回避確定の系統」と近傍かどうかを機械照合できる
署名表 data/kpi_trials/trial_fingerprints.json を生成する。今後の全収穫・全事前登録の
dedupゲートの実体になる。

情報源（read-only。本スクリプトはこれらを一切変更しない）:
- data/kpi_trials/trials.jsonl（110行・kpi_name/verdict等を機械パース）
- docs/stock-algo-kpi-catalog.md §7-D（採掘したが見送り・catalog:574-582）・
  §8-2（突合で落ちた/降格したもの・catalog:2541-2548）・
  §7-AF（除外family5件 F04/F07/F09/F10/F11・catalog:2514,2581-2592）・
  回避知見（GC逆指標catalog:1486-1491・低ボラ入口不可catalog:73/764・
  S高追随EV-8%catalog:731・S安反発EV-5.14%catalog:1784/1881・
  §7-AD無条件リバーサル不利catalog:2453-2464）はスクリプト内定数として手動キュレーション。

trials.jsonl側のfamily/entry_conditionはkpi_nameからの規則ベース対応表
（KNOWN_TRIALS）で持つ。**不明なものはfamily="その他"のまま残し、UNKNOWNリストとして
出力に含める（無理に推測しない）**。

Usage:
    docker compose run --rm xstock python scripts/build_trial_fingerprints.py
    # 出力: data/kpi_trials/trial_fingerprints.json（ensure_ascii=False・冪等）
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

TRIALS_JSONL = Path("data/kpi_trials/trials.jsonl")
CATALOG_MD = Path("docs/stock-algo-kpi-catalog.md")
OUTPUT_PATH = Path("data/kpi_trials/trial_fingerprints.json")

# ---------------------------------------------------------------------------
# KNOWN_TRIALS: kpi_name -> {family, entry_condition}（手動キュレーション・trials.jsonl実読ベース）
# family enum: 価格リバーサル|出来高ショック|SUE/PEAD|業種波及|ブレイクアウト|MA/GC|
#              ストップ高安|需給/信用|イベント|その他
# ---------------------------------------------------------------------------
KNOWN_TRIALS: dict[str, dict[str, str]] = {
    # --- SUE/PEAD family（決算サプライズ・ドリフト・進捗率・増額修正・売上/利益率イベント） ---
    "pead_initial_gap8_vol3": {
        "family": "SUE/PEAD",
        "entry_condition": "決算発表寄付ギャップ+8%以上×出来高3倍以上（FinancialStatements）の買い",
    },
    "pead_x_max20_10": {
        "family": "SUE/PEAD",
        "entry_condition": "pead_gap8_vol3にfilter_max20(20日高値からの位置10%以内)を追加した絞り込み",
    },
    "pead_x_dev25_10": {
        "family": "SUE/PEAD",
        "entry_condition": "pead_gap8_vol3にfilter_dev25(25日線乖離10%以内)を追加した絞り込み",
    },
    "pead_x_max20_dev25_10": {
        "family": "SUE/PEAD",
        "entry_condition": "pead_gap8_vol3にmax20とdev25の両フィルタを重ねた絞り込み",
    },
    "uprev_fop10": {
        "family": "SUE/PEAD",
        "entry_condition": "業績予想の上方修正10%以上（EarnForecastRevision）の買い",
    },
    "pead_gap8_vol3_defer3": {
        "family": "SUE/PEAD",
        "entry_condition": "pead_initial_gap8_vol3のdefer_entry(S高翌日繰延最大3営業日)版",
    },
    "uprev_fop10_defer3": {
        "family": "SUE/PEAD",
        "entry_condition": "uprev_fop10のdefer_entry(S高翌日繰延最大3営業日)版",
    },
    "shinchoku_1q40": {
        "family": "SUE/PEAD",
        "entry_condition": "1Q時点で通期進捗率40%以上（1QFinancialStatements）の買い",
    },
    "shinchoku_allq": {
        "family": "SUE/PEAD",
        "entry_condition": "1Q/2Q/3Q各四半期の進捗率閾値(40/65/85%)を跨いだ複合進捗シグナル",
    },
    "pead_x_shinchoku": {
        "family": "SUE/PEAD",
        "entry_condition": "PEAD側(gap8/vol3)×進捗率側(shinchoku_allq)の複合AND条件",
    },
    "pead_gap8_vol3_defer3_HOLDOUT": {
        "family": "SUE/PEAD",
        "entry_condition": "pead_gap8_vol3_defer3のholdout期間(2023-01〜2026-05)再確認",
    },
    "sue_beat": {
        "family": "SUE/PEAD",
        "entry_condition": "営業利益実績が直前予想比+10%以上のビート（2Q/FY限定・SUE型）の買い",
    },
    "sue_x_above200": {
        "family": "SUE/PEAD",
        "entry_condition": "sue_beat母集団に200日線上(above200)フィルタを重ねた絞り込み",
    },
    "sue_x_above200_ul0": {
        "family": "SUE/PEAD",
        "entry_condition": "sue_x_above200相当にul_count_10bd<=2(ストップ高頻発除外)を重ねた絞り込み",
    },
    "sue_x_quiet": {
        "family": "SUE/PEAD",
        "entry_condition": "sue_beat母集団にquiet_ratio>=1.2(静けさ)フィルタを重ねた絞り込み",
    },
    "sue_x_above200_exit_stop8": {
        "family": "SUE/PEAD",
        "entry_condition": "sue_x_above200母集団のexit比較（-8%固定ストップロス版・secondary endpoint）",
    },
    "sue_x_above200_exit_e1": {
        "family": "SUE/PEAD",
        "entry_condition": "sue_x_above200母集団のexit比較（E1シナリオ崩壊型・secondary endpoint）",
    },
    "dividend_uprev": {
        "family": "SUE/PEAD",
        "entry_condition": "配当予想の増額修正（DividendForecastRevision・同一会計年度限定）の買い",
    },
    "uprev_repeater": {
        "family": "SUE/PEAD",
        "entry_condition": "uprev_fop10のうち過去2年以内に2回以上上方修正した常連銘柄のみに絞った買い",
    },
    "margin_expand_yoy": {
        "family": "SUE/PEAD",
        "entry_condition": "単四半期営業利益率が前年同期比+2pt以上改善かつ増収（決算開示イベント）の買い",
    },
    "sales_beat": {
        "family": "SUE/PEAD",
        "entry_condition": "売上実績が直前予想比+5%以上のビート（sue_beatと同型as-of・2Q/FY限定）の買い",
    },
    "guidance_fy_strong": {
        "family": "SUE/PEAD",
        "entry_condition": "FY決算開示で来期売上予想が今期比+10%以上の強気ガイダンスの買い",
    },
    "cfo_margin_improve": {
        "family": "SUE/PEAD",
        "entry_condition": "営業CF/売上高が前年同期比+3pt以上改善かつ増収（2Q/FY開示）の買い",
    },
    "cfo_turnaround": {
        "family": "SUE/PEAD",
        "entry_condition": "営業CF/売上高が前年同期マイナスから当期+2%以上へ転換（黒字転換）の買い",
    },
    "earnings_spillover": {
        "family": "SUE/PEAD",
        "entry_condition": "同業種内でsales_beat銘柄(leader)が出た日、同業種未発表銘柄への波及買い",
    },
    "preearnings_runup": {
        "family": "SUE/PEAD",
        "entry_condition": "次回決算まで推定5営業日以内到達×quiet_ratio>=1.2（決算接近・事前ランアップ）の買い",
    },
    "screen_v1_pead_gap8_vol3_defer3_quiet_ratio_quiet": {
        "family": "SUE/PEAD",
        "entry_condition": "pead_gap8_vol3_defer3母集団にquiet_ratio>=1.2を掛けたスクリーニングバッチv1セル",
    },
    # --- 出来高ショック family（異常出来高・出来高倍率主導のシグナル群） ---
    "volshock_5x": {
        "family": "出来高ショック",
        "entry_condition": "当日出来高が20日平均の5倍以上×陽線×騰落率+2〜8%（初動）の買い",
    },
    "volshock_5x_HOLDOUT_obs": {
        "family": "出来高ショック",
        "entry_condition": "volshock_5xのholdout期間(2023-01〜2026-05)再確認",
    },
    "volshock_x_above200": {
        "family": "出来高ショック",
        "entry_condition": "volshock_5xに200日線上(above200)フィルタを重ねた絞り込み",
    },
    "volshock_x_below200": {
        "family": "出来高ショック",
        "entry_condition": "volshock_5xに200日線下(below200)フィルタを重ねた絞り込み",
    },
    "volshock_x_above200_exit_C1": {
        "family": "出来高ショック",
        "entry_condition": "volshock_x_above200母集団のexit比較（C1: -8%固定ストップ）",
    },
    "volshock_x_above200_exit_C2": {
        "family": "出来高ショック",
        "entry_condition": "volshock_x_above200母集団のexit比較（C2: -10%固定ストップ）",
    },
    "volshock_x_above200_exit_C3": {
        "family": "出来高ショック",
        "entry_condition": "volshock_x_above200母集団のexit比較（C3固定ストップ変種）",
    },
    "volshock_x_above200_exit_E1": {
        "family": "出来高ショック",
        "entry_condition": "volshock_x_above200母集団のexit比較（E1: シナリオ崩壊型）",
    },
    "volshock_x_above200_exit_E2": {
        "family": "出来高ショック",
        "entry_condition": "volshock_x_above200母集団のexit比較（E2: ATRストップ）",
    },
    "volshock_x_above200_exit_E3": {
        "family": "出来高ショック",
        "entry_condition": "volshock_x_above200母集団のexit比較（E3: ベータストップ）",
    },
    "volshock_x_above200_exit_E4": {
        "family": "出来高ショック",
        "entry_condition": "volshock_x_above200母集団のexit比較（E4: 2段階ATRストップ）",
    },
    "volshock_x_above200_micro": {
        "family": "出来高ショック",
        "entry_condition": "volshock_x_above200に時価総額下位30%(micro_cap)増幅フィルタを重ねた絞り込み",
    },
    "volshock_x_above200_quiet": {
        "family": "出来高ショック",
        "entry_condition": "volshock_x_above200にquiet_to_active_volume(静けさ→活性化)増幅フィルタを重ねたチャンピオン構成",
    },
    "volshock_quiet": {
        "family": "出来高ショック",
        "entry_condition": "volshock_5xにabove200フィルタなしでquiet増幅フィルタのみ重ねた絞り込み",
    },
    "va_top1000": {
        "family": "出来高ショック",
        "entry_condition": "volshock_x_above200と同一定義をユニバースTOP1000に拡張した感度検証",
    },
    "vaq_top1000": {
        "family": "出来高ショック",
        "entry_condition": "va_top1000にquiet増幅フィルタを重ねたTOP1000版",
    },
    "vaq_top1000_exit_E1": {
        "family": "出来高ショック",
        "entry_condition": "vaq_top1000母集団のexit比較（E1: シナリオ崩壊型）",
    },
    "va_top1000_exit_E1": {
        "family": "出来高ショック",
        "entry_condition": "va_top1000母集団のexit比較（E1: シナリオ崩壊型）",
    },
    "volshock_x_above200_quiet_ul0": {
        "family": "出来高ショック",
        "entry_condition": "チャンピオン(volshock_x_above200_quiet)にul_count_10bd==0(ストップ高0回)フィルタを重ねた絞り込み",
    },
    "volshock_x_above200_quiet_ul_lt3": {
        "family": "出来高ショック",
        "entry_condition": "チャンピオンにul_count_10bd<=2(ストップ高2回以下)フィルタを重ねた絞り込み",
    },
    "volshock_x_above200_quiet_earnprox20": {
        "family": "出来高ショック",
        "entry_condition": "チャンピオンにearnings_remaining_bd<=20(決算接近推定)フィルタを重ねた絞り込み",
    },
    "volshock_x_above200_ul_lt3": {
        "family": "出来高ショック",
        "entry_condition": "volshock_x_above200(above200無印・広母集団)にul_count_10bd<=2を重ねた広域再確認",
    },
    "updown_vol_ratio_high": {
        "family": "出来高ショック",
        "entry_condition": "20営業日の上昇日出来高合計÷下落日出来高合計が3.0以上に到達した日の買い",
    },
    "turnover_rank_surge": {
        "family": "出来高ショック",
        "entry_condition": "売買代金順位が20営業日前301位以下から当日100位以内へ急上昇（陽線条件付き）の買い",
    },
    # --- 価格リバーサル family（短期反転・残差リバーサル・無条件横断ランキング） ---
    "strev_20d": {
        "family": "価格リバーサル",
        "entry_condition": "直近20営業日リターン下位10%（売られ過ぎ銘柄）のロング",
    },
    "resid_strev_entry": {
        "family": "価格リバーサル",
        "entry_condition": "TOPIX 1ファクター残差ベースの直近1ヶ月反転スコア上位decile新規入り",
    },
    "raw_strev_entry": {
        "family": "価格リバーサル",
        "entry_condition": "素の(-r[t-1])上位decile新規入り（strev_20dの月次イベント化・残差化なし版）",
    },
    "sell_reg_trigger_rebound": {
        "family": "価格リバーサル",
        "entry_condition": "前日比-10%到達（値幅制限トリガー近似）の翌営業日買い",
    },
    "engulf_reversal_day": {
        "family": "価格リバーサル",
        "entry_condition": "安寄り後に陽線で前日終値を上回る包み足（bullish engulfing）×出来高1.5倍の買い",
    },
    "rank_port_v1_composite": {
        "family": "価格リバーサル",
        "entry_condition": "モメンタム(F2)×リバーサル(F8)を等ウェイト合成した連続横断ランキングロングショート型ポートフォリオ",
    },
    "rank_port_v1_f2only": {
        "family": "価格リバーサル",
        "entry_condition": "rank_port_v1のトレンド因子(F2)単独版（合成の対照群）",
    },
    "rank_port_v1_f8only": {
        "family": "価格リバーサル",
        "entry_condition": "rank_port_v1のリバーサル因子(F8=strev)単独版（合成の対照群）",
    },
    # --- ストップ高安 family（S高/S安の値幅制限フラグ主導） ---
    "sh_dip_reentry": {
        "family": "ストップ高安",
        "entry_condition": "ストップ高後3〜10営業日で5〜15%押し目・出来高維持での再上昇買い（翌日追撃は除く）",
    },
    "sh_dip_reentry_exit_E1": {
        "family": "ストップ高安",
        "entry_condition": "sh_dip_reentry母集団のexit比較（E1: シナリオ崩壊型）",
    },
    "ul_fade_standalone": {
        "family": "ストップ高安",
        "entry_condition": "直近10営業日でストップ高(UL)3回以上到達×前日未達（新規到達日）の追随買い",
    },
    "ll_release_rebound": {
        "family": "ストップ高安",
        "entry_condition": "ストップ安(LL)エピソード後、10営業日以内で初めて出来高を伴う陽線が出た日の買い",
    },
    # --- MA/GC family（移動平均線クロス・線上復帰） ---
    "ma200_reclaim": {
        "family": "MA/GC",
        "entry_condition": "前日終値<200日線・当日終値>200日線（200日線奪回クロス）の買い",
    },
    "ma200_reclaim_exit_E1": {
        "family": "MA/GC",
        "entry_condition": "ma200_reclaim母集団のexit比較（E1: シナリオ崩壊型）",
    },
    "golden_cross_2575": {
        "family": "MA/GC",
        "entry_condition": "25日線が75日線を下から上へ抜けるゴールデンクロス（25/75日）の買い",
    },
    # --- ブレイクアウト family（レンジ上放れ・高値更新・連騰・ギャップ持続） ---
    "high52_breakout": {
        "family": "ブレイクアウト",
        "entry_condition": "252日高値更新×出来高20日平均の2倍以上（52週高値ブレイク）の買い",
    },
    "high52_break_vol": {
        "family": "ブレイクアウト",
        "entry_condition": "high52_breakoutの終値基準・251日窓への定義微調整版（出来高倍率条件は同一）",
    },
    "range_breakout": {
        "family": "ブレイクアウト",
        "entry_condition": "60日レンジ幅15%未満（低ボラ収縮）からレンジ上限×1.02上抜け×出来高2倍の買い",
    },
    "range_breakout_exit_E1": {
        "family": "ブレイクアウト",
        "entry_condition": "range_breakout母集団のexit比較（E1: シナリオ崩壊型）",
    },
    "nr7_breakout": {
        "family": "ブレイクアウト",
        "entry_condition": "直近7営業日で最小の日中レンジ（NR7）を付けた翌営業日に前日高値を出来高1.5倍超で上抜けた買い",
    },
    "rs_line_high": {
        "family": "ブレイクアウト",
        "entry_condition": "対TOPIX相対力(RS)ラインが252日新高値かつ株価自体は未新高値（先行ブレイク）の買い",
    },
    "three_up_ignition": {
        "family": "ブレイクアウト",
        "entry_condition": "3営業日連続の陽線×高値切り上げ×終値切り上げ×出来高合計3倍以上（初回性条件付き）の買い",
    },
    "gap_hold_close_strong": {
        "family": "ブレイクアウト",
        "entry_condition": "寄付+3%以上ギャップ×終値が寄付以上を維持×日中レンジ上位20%引け×出来高2倍の買い",
    },
    # --- 業種波及 family（同業種内の主導株→出遅れ株スピルオーバー） ---
    "sector_momentum_laggard": {
        "family": "業種波及",
        "entry_condition": "33業種指数60日リターン上位分位の業種に属し、業種内では自銘柄60日リターンが下位40%（出遅れ）の買い",
    },
    "sector_momentum_laggard_exit_E1": {
        "family": "業種波及",
        "entry_condition": "sector_momentum_laggard母集団のexit比較（E1: シナリオ崩壊型）",
    },
    "sector_sympathy_volshock": {
        "family": "業種波及",
        "entry_condition": "同業種内で他銘柄がvolshock_5x発火した当日、volshock未成立の同業種内出来高中央値以上銘柄への波及買い",
    },
    # --- 需給/信用 family（信用倍率・空売り・売り長・貸借） ---
    "shortcover_turn": {
        "family": "需給/信用",
        "entry_condition": "信用集計指標が閾値到達後に反転（踏み上げ転換）した銘柄の買い",
    },
    "margin_urinaga_trend": {
        "family": "需給/信用",
        "entry_condition": "信用売り長トレンド（25日線・20日高値との複合）に基づく買い",
    },
    "shortcover_x_bear": {
        "family": "需給/信用",
        "entry_condition": "shortcover_turnにbearレジームフィルタを重ねた絞り込み",
    },
    "shortup_lowrise": {
        "family": "需給/信用",
        "entry_condition": "信用売り長ストリーク×直近安値切り上げの複合買い",
    },
    "karauri_fuel": {
        "family": "需給/信用",
        "entry_condition": "空売り残高(調整済み)が4回前スナップショット比+50%以上増加かつ出来高20日平均の2倍以上の買い",
    },
    "margin_pampan": {
        "family": "需給/信用",
        "entry_condition": "信用買い残の消化日数(LongVol/20日平均出来高)が10日以上（カタログ§2-B原義の簡易1条件版）の買い",
    },
    "short_crowd_exit": {
        "family": "需給/信用",
        "entry_condition": "空売り建玉0.5%以上保有の複数機関投資家(アクティブReporter2者以上)が一斉撤退した銘柄の買い",
    },
    "mrgn_upgrade": {
        "family": "需給/信用",
        "entry_condition": "月末マスターで信用区分(Mrgn)が「信用」から「貸借」へ昇格した銘柄の買い",
    },
    # --- イベント family（企業属性の区分変更・資本イベント・大量保有） ---
    "activist_5pct": {
        "family": "イベント",
        "entry_condition": "EDINET大量保有報告書(5%ルール)によるアクティビスト参入検知の買い",
    },
    "mkt_upgrade": {
        "family": "イベント",
        "entry_condition": "月末マスターの市場区分(MarketCode)が上位区分へ昇格した銘柄の買い",
    },
    "scalecat_upgrade": {
        "family": "イベント",
        "entry_condition": "月末マスターの規模区分(ScaleCategory：Core30/Large70/Mid400等)が上位へ昇格した銘柄の買い",
    },
    "split_execution": {
        "family": "イベント",
        "entry_condition": "株式分割の実施日（AdjFactor<=0.5＝1:2以上の分割）を起点にした買い",
    },
    # --- その他 ---
    "resid_momentum_entry": {
        "family": "その他",
        "entry_condition": "TOPIX 1ファクター残差ベースのモメンタムスコア上位decile新規入り（残差モメンタム）",
    },
    "raw_momentum_entry": {
        "family": "その他",
        "entry_condition": "素の12ヶ月モメンタム(σ標準化なし)上位decile新規入り（残差化の対照基準線）",
    },
    "screening_batch_v1": {
        "family": "その他",
        "entry_condition": "スクリーニングバッチ登録メタ行（batch_id=batch_v1・仮説シグネチャではない・n_cells等の集計行）",
    },
    "screening_batch_v2t": {
        "family": "その他",
        "entry_condition": "スクリーニングバッチ登録メタ行（batch_id=batch_v2t・仮説シグネチャではない・F03押し目買い4セルの集計行）",
    },
}

# hoos（holdout observation survival・前向き競走）ステータスによる verdict 上書き
# 出典: docs/stock-algo-kpi-catalog.md:2379-2382, 2397-2398
HOOS_SURVIVED_KPI_NAMES = {
    "volshock_x_above200_quiet",
    "sue_x_above200",
    "sell_reg_trigger_rebound",
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
}
HOOS_REJECTED_KPI_NAMES = {"sh_dip_reentry"}

# 明示的AVOIDANCE_SIGNAL判定（exploratory_conclusion="rejected"より強い回避確定・カタログ実読で確認）
# ul_fade_standalone: catalog:733 fade確認=成立（EV点推定<0 かつ CI上限<0）
# ll_release_rebound: catalog:1881 除外=AVOIDANCE_SIGNAL（EV CI全域負）
AVOID_OVERRIDE_KPI_NAMES = {"ul_fade_standalone", "ll_release_rebound"}

REJECTED_EXPLORATORY_CONCLUSIONS = {"rejected", "degrading"}

FAMILY_KEYWORDS: dict[str, list[str]] = {
    "価格リバーサル": ["リバーサル", "反転", "反発", "逆張り", "押し目", "ランキングポート"],
    "出来高ショック": ["出来高", "volshock", "売買代金", "出来高ショック"],
    "SUE/PEAD": ["決算", "PEAD", "SUE", "上方修正", "進捗率", "ガイダンス", "サプライズ"],
    "業種波及": ["業種", "セクター", "波及", "スピルオーバー"],
    "ブレイクアウト": ["ブレイク", "高値更新", "レンジ", "ギャップ", "連騰"],
    "MA/GC": ["移動平均", "ゴールデンクロス", "GC", "MA", "線奪回"],
    "ストップ高安": ["ストップ高", "ストップ安", "S高", "S安", "UL", "LL"],
    "需給/信用": ["信用", "空売り", "貸借", "踏み上げ", "需給"],
    "イベント": ["大量保有", "分割", "格付け", "区分", "アクティビスト", "IPO", "TOB"],
    "その他": [],
}


def _keywords_for(family: str, extra: list[str]) -> list[str]:
    return sorted(set(FAMILY_KEYWORDS.get(family, []) + extra))


def _period_str(period: dict[str, Any] | None) -> str:
    if not period:
        return "N/A"
    start = period.get("start", "?")
    end = period.get("end", "?")
    return f"{start}〜{end}"


def load_trials(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def determine_verdict(kpi_name: str, rows_for_kpi: list[dict[str, Any]]) -> str:
    """優先順位: hoos_survived(racing) > hoos_rejected(avoid) > 手動AVOID_OVERRIDE(avoid) >
    生verdict=fail(fail) > 生verdict=rejected(rejected) > 生verdict=confirm_fail(rejected) >
    exploratory_conclusion∈{rejected,degrading}(rejected) > pending（既定）。
    """
    if kpi_name in HOOS_SURVIVED_KPI_NAMES:
        return "racing"
    if kpi_name in HOOS_REJECTED_KPI_NAMES:
        return "avoid"
    if kpi_name in AVOID_OVERRIDE_KPI_NAMES:
        return "avoid"

    raw_verdicts = {r.get("verdict") for r in rows_for_kpi}
    if "fail" in raw_verdicts:
        return "fail"
    if "rejected" in raw_verdicts:
        return "rejected"
    if "confirm_fail" in raw_verdicts:
        return "rejected"

    exploratory = {
        r.get("params", {}).get("exploratory_conclusion")
        for r in rows_for_kpi
        if r.get("params", {}).get("exploratory_conclusion")
    }
    if exploratory & REJECTED_EXPLORATORY_CONCLUSIONS:
        return "rejected"

    return "pending"


def build_trial_entries(trials: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """trials.jsonl から kpi_name 単位でdedupしたfingerprintエントリを生成する。"""
    order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in trials:
        kn = row.get("kpi_name")
        if kn is None:
            continue
        if kn not in grouped:
            grouped[kn] = []
            order.append(kn)
        grouped[kn].append(row)

    entries: list[dict[str, Any]] = []
    unknown: list[str] = []
    for kn in order:
        rows = grouped[kn]
        first_row = rows[0]
        last_row = rows[-1]
        curated = KNOWN_TRIALS.get(kn)
        if curated is None:
            unknown.append(kn)
            family = "その他"
            sd = last_row.get("params", {}).get("signal_definition")
            entry_condition = sd if sd else "(未キュレーション・要手動確認)"
        else:
            family = curated["family"]
            entry_condition = curated["entry_condition"]

        verdict = determine_verdict(kn, rows)
        n_values = [r.get("n") for r in rows if r.get("n") is not None]
        entries.append(
            {
                "fp_id": None,  # 採番は呼び出し側でまとめて行う
                "source": "trials",
                "name": kn,
                "family": family,
                "entry_condition": entry_condition,
                "universe": "TOP500",
                "period_tested": _period_str(first_row.get("period")),
                "verdict": verdict,
                "keywords": _keywords_for(family, []),
                "ref": f"trials.jsonl kpi_name={kn} (n_rows={len(rows)}, max_n={max(n_values) if n_values else None})",
            }
        )
    return entries, unknown


# ---------------------------------------------------------------------------
# CURATED_ENTRIES: カタログ由来のキュレーション（回避知見・再提案防止・除外family）
# 出典行番号はスクリプト作成時点(2026-07-18)のdocs/stock-algo-kpi-catalog.mdを実読して起こした。
# ---------------------------------------------------------------------------
CURATED_ENTRIES: list[dict[str, Any]] = [
    # --- source=avoidance: 回避ルール・教訓（複数試行の結果から確定した「触るな」知見） ---
    {
        "source": "avoidance",
        "name": "株式分割は近年(2022-)効かない",
        "family": "イベント",
        "entry_condition": "TDnet表題マッチ「株式分割」の開示イベント買い（比率不問・TOP500）",
        "universe": "TOP500",
        "period_tested": "2016-09〜2026-06（記述測定・事前登録なし・α非消費）",
        "verdict": "avoid",
        "keywords": _keywords_for("イベント", ["株式分割", "分割", "split", "投資単位引下げ"]),
        "ref": "tasks/split_recency_check.md + output/research/split_recency/（2026-08-16 期間分割・事前固定1通り）: "
        "全期間では枠S通過（lift2.73[CI下限1.78]・EV(stop8)+3.41%）だが、**後半202201〜202606 で lift1.82"
        "[CI下限0.78<1.0]・EV(なし)片側下限−0.87%と符号反転**＝判定規約でNO-GO。前半201609〜202112 は"
        "lift4.02[2.30,6.63]と強く、全期間の通過は前半の寄与。**多重比較では消えない**（全期間はBonferroni"
        "調整後もCI下限1.38〜1.46で1.2維持）＝偽陽性でなく『期間で消える』型。市場ベースレートは"
        "前半3.79%→後半4.44%と上昇しているのに到達率は14.8%→7.5%へ半減＝イベント固有の減衰。"
        "発火頻度は年25.3→41.6件と増加（分割が珍しくなくなった可能性・機序は未検証）",
    },
    {
        "source": "avoidance",
        "name": "配当予想の増額修正は近年(2022-)効かない",
        "family": "SUE/PEAD",
        "entry_condition": "TDnet表題マッチ「配当予想の修正」の開示イベント買い（増額幅不問・TOP500）",
        "universe": "TOP500",
        "period_tested": "2016-09〜2026-06（記述測定・事前登録なし・α非消費）",
        "verdict": "avoid",
        "keywords": _keywords_for("SUE/PEAD", ["配当", "増配", "配当予想", "dividend"]),
        "ref": "tasks/split_recency_check.md + output/research/split_recency/（2026-08-16・参考測定）: "
        "全期間では枠F通過（EV(なし)片側下限+0.42%・lift1.28・月19.3件）だが、**後半202201〜202606 で "
        "lift0.90（<1.0）・EV(なし)片側下限−0.25%（<0）＝枠Fの根拠2条件が両方とも崩れる**。"
        "前半はlift1.70[1.22,2.44]・EV下限+0.38%＝全期間の通過は前半の寄与。株式分割と同型の期間減衰。"
        "既存 dividend_uprev（配当予想の増額修正・fins由来）とは別経路だが同じ機序に当たるため近傍",
    },
    {
        "source": "avoidance",
        "name": "200週線の近傍帯(-20〜+20%)は+20%を濃縮しない",
        "family": "MA/GC",
        "entry_condition": "200週SMA（≈4年線）の**近傍帯 dev∈[-20%,+20%)** での買い（@AlphaOwlTrading 2026-08-14 のマンガー帰属ルール）。※両端（<-20% / >=+20%）はベース4.1%を上回るが時期偏在のため別扱い＝下記ref参照",
        "universe": "TOP500",
        "period_tested": "2020-05〜2026-06（74ヶ月・記述測定・事前登録なし・α非消費）",
        "verdict": "avoid",
        "keywords": _keywords_for("MA/GC", ["200週線", "4年線", "SMA200W", "長期移動平均", "クオリティ株"]),
        "ref": "tasks/sma200w_descriptive.md + output/research/sma200w/（2026-08-15 記述測定 n=33,805）: "
        "8帯すべてで+20%到達率は2〜6%＝中央帯（-10〜+20%）は一様に約2%でベース4.1%を下回る。"
        "両端のみ持ち上がるU字（<-20%=6.0%・>=+20%=5.1%）だが、<-20%は月次等重みで5.1%へ低下し"
        "ヒットの38.6%がコロナ後リバウンド3ヶ月に集中＝相場位置の代理変数。帯シェアも2020年→2026年で"
        "<-20%が28.4%→5.3%と激変し、銘柄の性質でなく時期を測っている。§8-6原則A（触媒なしの価格配置）の実証例",
    },
    {
        "source": "avoidance",
        "name": "自社株買い決議は+20%逆方向",
        "family": "自社株買い",
        "entry_condition": "TDnet表題マッチ「自己株式取得に係る事項の決定/市場買付/立会外買付」の開示イベント買い（サイズ不問）",
        "universe": "TOP500",
        "period_tested": "2016-07〜2026-07（記述測定・事前登録なし）",
        "verdict": "avoid",
        "keywords": _keywords_for("自社株買い", ["自己株式", "buyback", "ToSTNeT", "下支え"]),
        "ref": "catalog:128（2026-08-02 記述測定・2026-08-16 基準訂正: TOP500 n=1,148・+20%到達2.5%＝"
        "正ベース4.1%[catalog §6・118ヶ月 n=58,497]の約0.61倍。旧記載「9.5%の1/4」は過熱3ヶ月ローカル値の誤用。"
        "EV+0.96%は中央値+0.72%の下支え型＝急騰と逆方向。lift<1 のため死に筋の結論は基準訂正後も不変。"
        "サイズ≥3%条件は本文数値要のため未検証・output/tdnet/event_profile.md）",
    },
    {
        "source": "avoidance",
        "name": "GC逆指標",
        "family": "MA/GC",
        "entry_condition": "移動平均線クロス／奪回系シグナル全般（ゴールデンクロス25/75日・200日線奪回）の買い",
        "universe": "TOP500",
        "period_tested": "2016-11〜2022-11",
        "verdict": "avoid",
        "keywords": _keywords_for("MA/GC", ["逆指標", "定番テクニカル"]),
        "ref": "catalog:1486-1491（golden_cross_2575 fail + ma200_reclaim fail の2敗を踏まえた確定知見:"
        " 「監視者の多い定番テクニカル転換シグナルは+20%狙いでは逆指標」）",
    },
    {
        "source": "avoidance",
        "name": "低ボラ入口不可",
        "family": "ブレイクアウト",
        "entry_condition": "60日レンジ幅15%未満など低ボラティリティ銘柄のレンジ収縮→上放れ型ブレイク買い",
        "universe": "TOP500",
        "period_tested": "2016-11〜2022-11",
        "verdict": "avoid",
        "keywords": _keywords_for("ブレイクアウト", ["低ボラ", "BBスクイーズ"]),
        "ref": "catalog:73（表: range_breakout fail・lift0.12[0,0.35]・"
        "『低ボラ銘柄は構造的に+20%が出ない』）/ catalog:764",
    },
    {
        "source": "avoidance",
        "name": "S高追随EV-8%",
        "family": "ストップ高安",
        "entry_condition": "ストップ高多発銘柄（直近10営業日UL3回以上到達）への新規追随買い・保有継続",
        "universe": "TOP500",
        "period_tested": "2016-11〜2022-11",
        "verdict": "avoid",
        "keywords": _keywords_for("ストップ高安", ["フェード", "追随買い"]),
        "ref": "catalog:731-735（ul_fade_standalone: EV点推定-8.13% CI[-14.10%,-1.41%]・"
        "事前凍結基準によりfade確認=成立）",
    },
    {
        "source": "avoidance",
        "name": "S安反発EV-5.14%",
        "family": "ストップ高安",
        "entry_condition": "ストップ安張り付き解放後、出来高を伴う初陽線での反発買い",
        "universe": "TOP500",
        "period_tested": "2016-11〜2022-11",
        "verdict": "avoid",
        "keywords": _keywords_for("ストップ高安", ["解放反発", "リバウンド買い"]),
        "ref": "catalog:1784,1881（ll_release_rebound: lift3.10[1.99,4.65]だがEV(なし)-5.14%"
        "[-8.09%,-2.36%]・除外=AVOIDANCE_SIGNAL）",
    },
    {
        "source": "avoidance",
        "name": "§7-AD無条件リバーサル不利",
        "family": "価格リバーサル",
        "entry_condition": "イベント条件を外した連続横断ランキング型トレンド×リバーサル合成ロングショートポートフォリオ",
        "universe": "TOP500",
        "period_tested": "2016-11〜2026-06",
        "verdict": "avoid",
        "keywords": _keywords_for("価格リバーサル", ["連続ランキング", "無条件", "型転換"]),
        "ref": "catalog:2453-2464（rank_port_v1_composite v1棄却・コスト後月次超過-0.19%・NW t=-0.49・"
        "『この期間・TOP500・コスト条件では短期リバーサル買いが一貫して不利』・型転換トラックはv1で一旦停止）",
    },
    {
        "source": "avoidance",
        "name": "増担保解除買い",
        "family": "需給/信用",
        "entry_condition": "増担保規制(TSEMrgnRegCls∈{3,4,5,6})の解除イベント後の買い（信用買残の実減少long_margin_declinedフィルタ込みでも母集団と同一・非弁別）",
        "universe": "TOP500",
        "period_tested": "2016-11〜2022-11（設計段階・EV非集計で見送り）",
        "verdict": "avoid",
        "keywords": _keywords_for("需給/信用", ["増担保", "規制解除", "制度イベント", "TSEMrgnRegCls", "日々公表"]),
        "ref": "catalog §7-AH見送り（naive全解除20bd中央値-10.7%・信用買残の実減少は解除の約9割に該当し左裾を絞れず・機序clean な左裾成分なし・§8-6墓場②実証・trial非消費）",
    },
    # --- source=misokuri: §8-2 突合で落ちた/降格したもの（再提案防止） ---
    {
        "source": "misokuri",
        "name": "アクティビスト5%参入(再訴)",
        "family": "イベント",
        "verdict": "excluded",
        "entry_condition": "大量保有報告書(5%ルール)提出をトリガーにしたロング（activist_5pctの再訴）",
        "universe": "N/A",
        "period_tested": "N/A",
        "keywords": _keywords_for("イベント", ["再訴", "大量保有"]),
        "ref": "catalog:2543（§8-2: activist_5pct §6 fail済み＋EDINET保有割合フィールド不在で§7-P実施不能確定。"
        "再試行は新データ源＋新定義＋期間再定義の三条件でのみ）",
    },
    {
        "source": "misokuri",
        "name": "空売り残高0.5%開示の解消買い戻し",
        "family": "需給/信用",
        "verdict": "excluded",
        "entry_condition": "空売り残高0.5%開示の解消（撤退）を検知した買い戻し",
        "universe": "N/A",
        "period_tested": "N/A",
        "keywords": _keywords_for("需給/信用", ["0.5%開示", "解消買い戻し"]),
        "ref": "catalog:2544（§8-2: short_crowd_exit既試行(pending)と同概念の疑い。"
        "事前登録前に§7の該当周を突合し同一なら変種禁止）",
    },
    {
        "source": "misokuri",
        "name": "株式分割「発表」",
        "family": "イベント",
        "verdict": "excluded",
        "entry_condition": "株式分割の発表日（実施日ではない）を起点にした買い",
        "universe": "N/A",
        "period_tested": "N/A",
        "keywords": _keywords_for("イベント", ["分割発表"]),
        "ref": "catalog:2545（§8-2: split family同一期間変種禁止。§7でsplit_execution rejected済み。"
        "期間再定義後のみ）",
    },
    {
        "source": "misokuri",
        "name": "アナリスト格上げドリフト",
        "family": "その他",
        "verdict": "excluded",
        "entry_condition": "アナリスト格付け/レーティング引き上げ後のドリフト買い",
        "universe": "N/A",
        "period_tested": "N/A",
        "keywords": _keywords_for("その他", ["アナリスト", "格上げ"]),
        "ref": "catalog:2546（§8-2: データ経路が全滅=主要8サイト規約禁止・有償のみ。"
        "効果は小型>大型でTOP500と不整合。hold続行）",
    },
    {
        "source": "misokuri",
        "name": "IPOロックアップ明け",
        "family": "イベント",
        "verdict": "excluded",
        "entry_condition": "IPOロックアップ解除日を起点にしたポジション",
        "universe": "N/A",
        "period_tested": "N/A",
        "keywords": _keywords_for("イベント", ["IPO", "ロックアップ"]),
        "ref": "catalog:2547（§8-2: TOP500対象外。不採用）",
    },
    {
        "source": "misokuri",
        "name": "格付け引き上げ",
        "family": "イベント",
        "verdict": "excluded",
        "entry_condition": "信用格付け機関による格付け引き上げ後の株価反応取り",
        "universe": "N/A",
        "period_tested": "N/A",
        "keywords": _keywords_for("イベント", ["格付け"]),
        "ref": "catalog:2548（§8-2: 株式への波及実証が薄い。不採用）",
    },
    # --- source=misokuri: §7-D 採掘したが見送り（理由の記録・再提案防止） ---
    {
        "source": "misokuri",
        "name": "市場パニック逆張り買い",
        "family": "価格リバーサル",
        "verdict": "pending",
        "entry_condition": "市場全体パニック局面での個別銘柄逆張り買い",
        "universe": "N/A",
        "period_tested": "N/A",
        "keywords": _keywords_for("価格リバーサル", ["市場パニック", "逆張り"]),
        "ref": "catalog:576（§7-D: 棄却済みリバーサル家族の派生。市場条件付けの差分はあるが家族リスク濃厚 → hold）",
    },
    {
        "source": "misokuri",
        "name": "決算ビート×来期ガイダンス常連",
        "family": "SUE/PEAD",
        "verdict": "excluded",
        "entry_condition": "決算ビート×保守的ガイダンス常連銘柄への買い",
        "universe": "N/A",
        "period_tested": "N/A",
        "keywords": _keywords_for("SUE/PEAD", ["ガイダンス常連"]),
        "ref": "catalog:577（§7-D: PEAD家族(holdout棄却)+カタログD群既存の再発見 → 追加せず。"
        "D群『上方修正常連』の優先度裏付けとしてのみ記録）",
    },
    {
        "source": "misokuri",
        "name": "踏み上げ複合(信用倍率×トレンド×出来高)",
        "family": "需給/信用",
        "verdict": "rejected",
        "entry_condition": "信用倍率≤1.2×トレンド×出来高の踏み上げ複合買い",
        "universe": "N/A",
        "period_tested": "N/A",
        "keywords": _keywords_for("需給/信用", ["踏み上げ複合"]),
        "ref": "catalog:578（§7-D: 棄却済み『売り長×トレンド(リフト0.60)』の閾値変更版 → 却下）",
    },
    {
        "source": "misokuri",
        "name": "月足RSI地合い/複合下落底スコア",
        "family": "その他",
        "verdict": "pending",
        "entry_condition": "月足RSI地合いおよび複合下落底スコアに基づく買い",
        "universe": "N/A",
        "period_tested": "N/A",
        "keywords": _keywords_for("その他", ["RSI", "地合い"]),
        "ref": "catalog:579（§7-D: bear地合い確定知見と重複・米国固有指標の縮小版 → hold）",
    },
    {
        "source": "misokuri",
        "name": "海外投資家フロー/自社株買い/指数入替/個別空売り比率(v1再発見)",
        "family": "その他",
        "verdict": "excluded",
        "entry_condition": "海外投資家フロー・自社株買い・指数入替・個別空売り比率の各シグナル",
        "universe": "N/A",
        "period_tested": "N/A",
        "keywords": _keywords_for("その他", ["海外投資家フロー", "自社株買い", "指数入替"]),
        "ref": "catalog:580（§7-D: カタログ§2既存v1の再発見。新規性なし・優先度メモのみ）",
    },
    {
        "source": "misokuri",
        "name": "EDINET保有目的別再分割",
        "family": "イベント",
        "verdict": "rejected",
        "entry_condition": "EDINET大量保有報告書の保有目的別再分割に基づく買い",
        "universe": "N/A",
        "period_tested": "2021-07〜2022-11",
        "keywords": _keywords_for("イベント", ["保有目的", "EDINET"]),
        "ref": "catalog:581（§7-D: n=18の再分割は統計的に無意味 → 却下。activist_5pct fail(n=18)と同一母集団）",
    },
    {
        "source": "misokuri",
        "name": "大型低PBR希少性/小型IPO再エントリー",
        "family": "その他",
        "verdict": "rejected",
        "entry_condition": "大型低PBR希少性銘柄および小型IPO再エントリーに基づく買い",
        "universe": "N/A",
        "period_tested": "N/A",
        "keywords": _keywords_for("その他", ["低PBR", "IPO再エントリー"]),
        "ref": "catalog:582（§7-D: +20%/月との適合度問題・上場前データ不能 → 却下）",
    },
    # --- source=excluded_family: §7-AF 除外family（5件） ---
    {
        "source": "excluded_family",
        "name": "F04 急落・売られすぎからの反発(逆張り)",
        "family": "価格リバーサル",
        "verdict": "excluded",
        "entry_condition": "投げ売り一巡・悪材料鈍化を底サインにしたロング（急落・売られすぎ反発）",
        "universe": "N/A",
        "period_tested": "N/A（名人投稿収穫・§8-5仮説在庫）",
        "keywords": _keywords_for("価格リバーサル", ["F04", "名人投稿", "売られすぎ反発"]),
        "ref": "catalog:2514,2591（§7-AF: 敗北済み価格リバーサルfamily(§7-AD停止裁定・strev系5試行)の"
        "条件付き変種。再訴は期間再定義+追加裁定後のみ。⚠️S安反発EV-5.14%の回避知見と要突合）",
    },
    {
        "source": "excluded_family",
        "name": "F07 主導テーマ・セクター順張り物色",
        "family": "業種波及",
        "verdict": "excluded",
        "entry_condition": "主導テーマ（光/宇宙/メモリ/レアアース/電力等）内の強い銘柄を買う",
        "universe": "N/A",
        "period_tested": "N/A（名人投稿収穫・§8-5仮説在庫）",
        "keywords": _keywords_for("業種波及", ["F07", "名人投稿", "セクター順張り"]),
        "ref": "catalog:2514,2581（§7-AF: 既存fail sector_momentum_laggard(n=2810)等と同一設計の反対バケット"
        "＝近傍再投入。§7-V業種波及family変種禁止にも抵触）",
    },
    {
        "source": "excluded_family",
        "name": "F09 決算・業績サプライズ(PEAD/ストップ高/期待差)",
        "family": "SUE/PEAD",
        "verdict": "excluded",
        "entry_condition": "サプライズ反応とドリフト（決算・業績サプライズ全般）",
        "universe": "N/A",
        "period_tested": "N/A（名人投稿収穫・§8-5仮説在庫）",
        "keywords": _keywords_for("SUE/PEAD", ["F09", "名人投稿"]),
        "ref": "catalog:2514,2587（§7-AF: 既存SUE/PEAD系と近縁＝dedup要。§6拡張ルール5項＝"
        "同一期間への追加バッチ禁止）",
    },
    {
        "source": "excluded_family",
        "name": "F10 コーポレートイベント・カタリスト(TOB/M&A/提携/製品)",
        "family": "イベント",
        "verdict": "excluded",
        "entry_condition": "企業固有イベント（TOB/M&A/業務提携/新製品）への反応買い",
        "universe": "N/A",
        "period_tested": "N/A（名人投稿収穫・§8-5仮説在庫）",
        "keywords": _keywords_for("イベント", ["F10", "名人投稿", "TOB", "M&A"]),
        "ref": "catalog:2514,2592（§7-AF: BL-1/§7-AE（TOB）と統合整理。他はTDnetアドオン判断待ち）",
    },
    {
        "source": "excluded_family",
        "name": "F11 国策・政策テーマ株",
        "family": "その他",
        "verdict": "excluded",
        "entry_condition": "政府支援・国策・規制（AI/半導体支援・防衛・原発・重要鉱物）関連銘柄への買い",
        "universe": "N/A",
        "period_tested": "N/A（名人投稿収穫・§8-5仮説在庫）",
        "keywords": _keywords_for("その他", ["F11", "名人投稿", "国策", "政策テーマ"]),
        "ref": "catalog:2514,2586（§7-AF: データ源なし）",
    },
]


def assign_fp_ids(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for idx, entry in enumerate(entries, start=1):
        entry["fp_id"] = f"FP-{idx:03d}"
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help=f"出力先（既定: {OUTPUT_PATH}）",
    )
    args = parser.parse_args()

    if not TRIALS_JSONL.exists():
        raise SystemExit(f"trials.jsonlが見つかりません: {TRIALS_JSONL}")

    trials = load_trials(TRIALS_JSONL)
    trial_entries, unknown = build_trial_entries(trials)

    all_entries = trial_entries + CURATED_ENTRIES
    all_entries = assign_fp_ids(all_entries)

    catalog_sections = [
        "§7-D 採掘したが見送り(catalog:574-582)",
        "§8-2 突合で落ちた/降格したもの(catalog:2541-2548)",
        "§7-AF 除外family(catalog:2514,2581-2592)",
        "回避知見: GC逆指標(catalog:1486-1491)",
        "回避知見: 低ボラ入口不可(catalog:73,764)",
        "回避知見: S高追随EV-8%(catalog:731-735)",
        "回避知見: S安反発EV-5.14%(catalog:1784,1881)",
        "回避知見: §7-AD無条件リバーサル不利(catalog:2453-2464)",
    ]

    output = {
        "generated_at": None,  # 実行時にUTC ISO8601を設定（下で上書き）
        "sources": {
            "trials_jsonl_lines": len(trials),
            "trials_jsonl_unique_kpi_names": len(trial_entries),
            "catalog_sections": catalog_sections,
        },
        "unknown_kpi_names": unknown,
        "entries": all_entries,
    }

    import datetime

    output["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
        f.write("\n")

    source_counts: dict[str, int] = {}
    family_counts: dict[str, int] = {}
    for e in all_entries:
        source_counts[e["source"]] = source_counts.get(e["source"], 0) + 1
        family_counts[e["family"]] = family_counts.get(e["family"], 0) + 1

    print(f"書き出し完了: {args.output}")
    print(f"entries total: {len(all_entries)}")
    print(f"source別内訳: {source_counts}")
    print(f"family別内訳: {family_counts}")
    print(f"UNKNOWN kpi_names ({len(unknown)}): {unknown}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
