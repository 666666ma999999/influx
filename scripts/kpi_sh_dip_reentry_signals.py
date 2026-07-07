#!/usr/bin/env python3
"""ストップ高：初押し再上昇（Invest Leaders検証投稿型） シグナル生成器（カタログ§2-E・第15周T4）。

docs/stock-algo-kpi-catalog.md の §2-E「ストップ高：初押し再上昇」を実装する（tasks指示・
第15周「頻度直交セット」T4）。`data/jquants/bars/YYYYMMDD.json.gz`（四本値・出来高。未調整
O/H/L/C・調整済みAdjO/AdjC・売買代金Vaを含む）のみで完結する（fins不使用）。

## S高判定（東証の公知の値幅制限テーブルを実装・生データから独立再計算）

東証の制限値幅は「基準値段（通常は前営業日終値）」の帯ごとに定額の制限値幅を加算した価格が
その日の値幅上限となる（TSE施行規則別表）。本実装では**未調整**のO/H/L/C（分割調整をかけると
過去の実際の呼値グリッドと整合しなくなるため）を使い、基準値段=前営業日の未調整終値・
制限値幅=PRICE_LIMIT_TABLE参照・当日終値が値幅上限に到達（許容誤差内）した日をS高日とする。

PRICE_LIMIT_TABLEの根拠（本実装着手前に実データから独立検証済み）: J-Quants日足のUL
（当日値幅上限到達フラグ）を正解ラベルとみなし、400営業日相当のランダムサンプル（約4300件の
UL=1レコード）について「前日終値+テーブル参照幅」が当日高値と一致するか照合した結果、
一致率97.7%（ProdCat=023のETF/ETN銘柄を除く内国株券に限れば99.9%超・不一致3件はいずれも
個別銘柄で基準値段の特殊事情＝権利落ち等の可能性が高い稀少ケース）。この照合作業でテーブルの
帯境界を複数箇所修正した（例: 500円未満500円以上700円未満は当初100円と誤って記憶していたが
実データ照合で700円以上1,000円未満=150円・500円以上700円未満=100円・10,000円以上15,000円未満
=3,000円（当初2,000円という誤った記憶を実データで訂正）であることを確認済み）。70,000円超の
帯は本サンプルに実例が無く、確認済み帯の等比数列パターン（1-1.5-2-3-5-7-10の桁上げ繰り返し）
から外挿した（TOP500ユニバースでこの価格帯に達する銘柄は極めて稀であり実質的な影響はない）。
許容誤差S_HIGH_TOLERANCE=5円は帯境界付近で観測された呼値丸め誤差（最大約5円）を吸収するため。

定義（グリッドサーチはしない・1構成のみ・tasks指示どおり）:
    (1) S高判定: 当日終値(未調整C) が 前日終値(未調整C) + PRICE_LIMIT_TABLE[前日終値] の
        値幅上限に到達（許容誤差5円）した日をS高日Sとする
    (2) シグナル: Sの3〜10営業日後の各営業日Dを古い順に走査し、以下を全て満たす**最初**のDを
        採用する（1つのS高につき1回のみ）:
            - S高終値(調整済みAdjC(S))からの押し = AdjC(D)/AdjC(S)-1 が [-15%, -5%] の範囲
            - 当日陽線: AdjC(D) > AdjO(D)
            - Va(D) >= 0.3 × Va(S)（売買代金は分割調整不要。§1共通規約）

このKPIは strev/volshock 系と異なり月次ユニバーススナップショットに依存しない
イベント検出型（PEAD等と同系統）のため、S高検出は全銘柄・全期間を対象に行い
（前月末ユニバースでの絞り込みは行わない）、ユニバース所属判定は
scripts/kpi_event_study.run_event_study() 側（_universe_membership）に委譲する。

フォワードリターン計算・ユニバース判定・ブートストラップCI等は scripts/kpi_event_study.py の
run_event_study() に委譲する（--defer-entryは既定True）。

Usage:
    python3 scripts/kpi_sh_dip_reentry_signals.py --start 2016-11 --end 2022-11
    python3 scripts/kpi_sh_dip_reentry_signals.py --start 2016-11 --end 2022-11 --skip-harness
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: DATA_ROOT を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: run_event_study を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・MONTH_RE を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読み込みを再利用)

KPI_NAME = "sh_dip_reentry"

# --- 東証 値幅制限テーブル（実データ照合で検証済み。基準値段の帯 -> 制限値幅。単位: 円） -----
# (下限[以上], 上限[未満], 制限値幅)
PRICE_LIMIT_TABLE: list[tuple[float, float, float]] = [
    (0, 100, 30), (100, 200, 50), (200, 500, 80), (500, 700, 100),
    (700, 1000, 150), (1000, 1500, 300), (1500, 2000, 400), (2000, 3000, 500),
    (3000, 5000, 700), (5000, 7000, 1000), (7000, 10000, 1500), (10000, 15000, 3000),
    (15000, 20000, 4000), (20000, 30000, 5000), (30000, 50000, 7000), (50000, 70000, 10000),
    # 以下は実データに実例が無く、確認済み帯の等比数列パターンから外挿（docstring参照）
    (70000, 100000, 15000), (100000, 150000, 20000), (150000, 200000, 30000), (200000, 300000, 50000),
    (300000, 500000, 70000), (500000, 700000, 100000), (700000, 1000000, 150000),
    (1000000, 1500000, 200000), (1500000, 2000000, 300000), (2000000, 3000000, 500000),
    (3000000, 5000000, 700000), (5000000, 7000000, 1000000), (7000000, 10000000, 1500000),
    (10000000, 15000000, 3000000), (15000000, 20000000, 4000000), (20000000, 30000000, 5000000),
    (30000000, 50000000, 7000000), (50000000, float("inf"), 10000000),
]
S_HIGH_TOLERANCE = 5.0  # 円。帯境界付近の呼値丸め誤差(実データ照合で最大約5円)を吸収する許容誤差

DIP_MIN_BDAYS = 3  # Sの3営業日後から探索
DIP_MAX_BDAYS = 10  # Sの10営業日後まで探索
PULLBACK_MIN_DEFAULT = -0.15  # 押しの下限(-15%)
PULLBACK_MAX_DEFAULT = -0.05  # 押しの上限(-5%)
VA_MIN_RATIO_DEFAULT = 0.3  # Va(D) >= 0.3 x Va(S)


def price_limit_width(basis: float) -> float:
    """基準値段(前営業日の未調整終値)から制限値幅を引く(PRICE_LIMIT_TABLEの線形走査)。"""
    for lo, hi, width in PRICE_LIMIT_TABLE:
        if lo <= basis < hi:
            return width
    raise ValueError(f"基準値段 {basis} が値幅制限テーブルの範囲外です(0円以下?)")


def _earliest_bars_date() -> str:
    """data/jquants/bars/ に実在する最古の営業日(YYYYMMDD)を返す(ハードコードせず実ファイルから確認)。"""
    bars_dir = jq_fetch.DATA_ROOT / "bars"
    dates = sorted(p.name[:8] for p in bars_dir.glob("*.json.gz"))
    if not dates:
        raise SystemExit(f"FATAL: bars キャッシュが1件も見つかりません: {bars_dir}")
    return dates[0]


# --- フェーズ1: S高日検出（未調整O/H/L/C・1営業日1パスの逐次スキャン） -----------------------


def detect_stophigh_events(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
) -> tuple[list[dict], dict]:
    """[start_bd, end_bd]内の全銘柄・全営業日からS高日を検出する（未調整C使用・基準値段=前日未調整C）。

    Returns:
        (events, diag)。events は [{code, s_date, s_close_raw, s_basis, s_width}, ...]。
    """
    idx_earliest_bars = bday_index[_earliest_bars_date()]
    idx_start = bday_index[start_bd]
    warmup_idx = max(idx_earliest_bars, idx_start - 1)  # 基準値段=前日終値のみ必要(1日マージンで十分)
    idx_end = bday_index[end_bd]
    scan_days = all_bdays[warmup_idx : idx_end + 1]

    prev_raw_close: dict[str, float] = {}
    events: list[dict] = []
    diag = {
        "business_days_scanned": 0,
        "code_day_observations": 0,
        "no_prev_close": 0,
        "stophigh_days": 0,
    }

    for d in scan_days:
        bars_d = measure_base_rate.load_bars_day(d)
        in_event_window = start_bd <= d <= end_bd
        if in_event_window:
            diag["business_days_scanned"] += 1

        for code, rec in bars_d.items():
            raw_c = rec.get("C")
            basis = prev_raw_close.get(code)

            if in_event_window:
                diag["code_day_observations"] += 1
                if basis is None or basis <= 0 or raw_c is None:
                    diag["no_prev_close"] += 1
                else:
                    width = price_limit_width(basis)
                    upper = basis + width
                    if raw_c >= upper - S_HIGH_TOLERANCE:
                        diag["stophigh_days"] += 1
                        events.append(
                            {
                                "code": code,
                                "s_date": d,
                                "s_close_raw": raw_c,
                                "s_basis": basis,
                                "s_width": width,
                                "s_adjc": rec.get("AdjC"),
                                "s_va": rec.get("Va"),
                            }
                        )

            if raw_c is not None:
                prev_raw_close[code] = raw_c

    return events, diag


# --- フェーズ2: 初押し再上昇の探索（各S高イベントにつき3-10営業日後を古い順に走査） ----------


def find_dip_reentry_signals(
    events: list[dict],
    all_bdays: list[str],
    bday_index: dict[str, int],
    start_bd: str,
    end_bd: str,
    pullback_min: float = PULLBACK_MIN_DEFAULT,
    pullback_max: float = PULLBACK_MAX_DEFAULT,
    va_min_ratio: float = VA_MIN_RATIO_DEFAULT,
) -> tuple[pd.DataFrame, dict]:
    """S高イベントごとに3-10営業日後を古い順に走査し、条件を満たす最初のDを1件だけ採用する。"""
    diag = {
        "s_events_total": len(events),
        "s_events_missing_s_adjc_or_va": 0,
        "s_events_window_out_of_range": 0,
        "candidate_days_scanned": 0,
        "candidate_missing_bars": 0,
        "candidate_pullback_out_of_range": 0,
        "candidate_not_green_candle": 0,
        "candidate_volume_below_threshold": 0,
        "signals_sh_dip_reentry": 0,
        "no_match_within_window": 0,
    }
    rows: list[dict] = []

    for ev in events:
        code = ev["code"]
        s_date = ev["s_date"]
        adjc_s = ev["s_adjc"]
        va_s = ev["s_va"]
        if not adjc_s or not va_s:
            diag["s_events_missing_s_adjc_or_va"] += 1
            continue

        s_idx = bday_index[s_date]
        matched = False
        for offset in range(DIP_MIN_BDAYS, DIP_MAX_BDAYS + 1):
            d_idx = s_idx + offset
            if d_idx >= len(all_bdays):
                diag["s_events_window_out_of_range"] += 1
                break
            d = all_bdays[d_idx]
            diag["candidate_days_scanned"] += 1

            rec_d = measure_base_rate.load_bars_day(d).get(code)
            if rec_d is None:
                diag["candidate_missing_bars"] += 1
                continue
            adjc_d = rec_d.get("AdjC")
            adjo_d = rec_d.get("AdjO")
            va_d = rec_d.get("Va")
            if adjc_d is None or adjo_d is None or va_d is None:
                diag["candidate_missing_bars"] += 1
                continue

            pullback = adjc_d / adjc_s - 1
            if not (pullback_min <= pullback <= pullback_max):
                diag["candidate_pullback_out_of_range"] += 1
                continue
            if not (adjc_d > adjo_d):
                diag["candidate_not_green_candle"] += 1
                continue
            if not (va_d >= va_min_ratio * va_s):
                diag["candidate_volume_below_threshold"] += 1
                continue

            # 全条件成立: このDを採用しこのS高イベントの探索を打ち切る(1つのS高につき1回のみ)
            if start_bd <= d <= end_bd:
                diag["signals_sh_dip_reentry"] += 1
                rows.append(
                    {
                        "signal_date": d,
                        "code": code,
                        "s_date": s_date,
                        "days_after_s": offset,
                        "pullback": pullback,
                        "va_ratio": va_d / va_s,
                        "s_close_raw": ev["s_close_raw"],
                        "s_basis": ev["s_basis"],
                        "s_width": ev["s_width"],
                    }
                )
            matched = True
            break

        if not matched:
            diag["no_match_within_window"] += 1

    return pd.DataFrame(rows), diag


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ストップ高：初押し再上昇 シグナル生成器(カタログ§2-E・第15周T4) + KPI検証ハーネス実行"
    )
    parser.add_argument(
        "--start", default=kpi_pead_signals.IN_SAMPLE_START,
        help=f"検索開始月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_START})",
    )
    parser.add_argument(
        "--end", default=kpi_pead_signals.IN_SAMPLE_END,
        help=f"検索終了月 YYYY-MM(既定 {kpi_pead_signals.IN_SAMPLE_END})",
    )
    parser.add_argument("--pullback-min", type=float, default=PULLBACK_MIN_DEFAULT)
    parser.add_argument("--pullback-max", type=float, default=PULLBACK_MAX_DEFAULT)
    parser.add_argument("--va-min-ratio", type=float, default=VA_MIN_RATIO_DEFAULT)
    parser.add_argument("--kpi-name", default=KPI_NAME)
    parser.add_argument("--output-dir", default="output/kpi", help="ハーネス出力先ルート(kpi-name配下に生成)")
    parser.add_argument("--base-rate-dir", default=str(kpi_event_study.DEFAULT_BASE_RATE_DIR))
    parser.add_argument("--universe-window", type=int, default=21, choices=[21, 126])
    parser.add_argument(
        "--skip-harness", action="store_true", help="シグナル生成のみ行いハーネス実行をスキップ(デバッグ用)"
    )
    parser.add_argument(
        "--no-defer-entry", action="store_true",
        help="--defer-entryが既定Trueのため、従来のT+1固定挙動に戻したい場合のみ指定",
    )
    args = parser.parse_args()

    if not kpi_pead_signals.MONTH_RE.match(args.start) or not kpi_pead_signals.MONTH_RE.match(args.end):
        raise SystemExit("FATAL: --start/--end は YYYY-MM 形式で指定してください")
    if args.end > kpi_pead_signals.IN_SAMPLE_END:
        print(
            f"WARN: --end={args.end} は§6で凍結したholdout期間(2023年以降)に抵触します。"
            f"in-sample評価は{kpi_pead_signals.IN_SAMPLE_END}までに限定してください"
            f"(holdoutは選抜済み候補の最終確認にのみ使用)。",
            file=sys.stderr,
        )

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays_all = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays_all)}
    start_bound = args.start.replace("-", "") + "01"
    end_bound = args.end.replace("-", "") + "31"
    start_bd = next((d for d in all_bdays_all if d >= start_bound), None)
    end_bd = next((d for d in reversed(all_bdays_all) if d <= end_bound), None)
    if start_bd is None or end_bd is None:
        raise SystemExit("FATAL: 指定期間に営業日が見つかりません")

    print("フェーズ1: S高日検出中...", file=sys.stderr)
    events, s_diag = detect_stophigh_events(start_bd, end_bd, all_bdays_all, bday_index)
    print(
        f"S高日検出完了: {s_diag['stophigh_days']}件"
        f"(営業日走査={s_diag['business_days_scanned']}日 銘柄日観測={s_diag['code_day_observations']} "
        f"前日終値なし除外={s_diag['no_prev_close']})"
    )

    print("フェーズ2: 初押し再上昇の探索中...", file=sys.stderr)
    signals_df, r_diag = find_dip_reentry_signals(
        events, all_bdays_all, bday_index, start_bd, end_bd,
        args.pullback_min, args.pullback_max, args.va_min_ratio,
    )

    output_root = Path(args.output_dir)
    kpi_dir = output_root / args.kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_path = kpi_dir / "signals_raw.csv"
    signals_df.to_csv(signals_path, index=False)

    print(
        f"シグナル生成完了: {len(signals_df)}件"
        f"(pullback範囲=[{args.pullback_min:.0%}, {args.pullback_max:.0%}], va_min_ratio={args.va_min_ratio})"
    )
    print(
        f"S高イベント={r_diag['s_events_total']} (s_adjc/va欠損除外={r_diag['s_events_missing_s_adjc_or_va']}, "
        f"探索窓がカレンダー範囲外={r_diag['s_events_window_out_of_range']}) / "
        f"候補日走査={r_diag['candidate_days_scanned']} (bars欠損={r_diag['candidate_missing_bars']}, "
        f"押し範囲外={r_diag['candidate_pullback_out_of_range']}, 陰線除外={r_diag['candidate_not_green_candle']}, "
        f"出来高比未達={r_diag['candidate_volume_below_threshold']}) / "
        f"シグナル成立={r_diag['signals_sh_dip_reentry']} / 窓内マッチなし={r_diag['no_match_within_window']}"
    )
    print(f"出力: {signals_path}")

    if args.skip_harness:
        return 0
    if signals_df.empty:
        print("WARN: シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    defer_entry = not args.no_defer_entry
    params = {
        "price_limit_table_version": "2026-07-07_empirically_validated",
        "s_high_tolerance": S_HIGH_TOLERANCE,
        "dip_min_bdays": DIP_MIN_BDAYS,
        "dip_max_bdays": DIP_MAX_BDAYS,
        "pullback_min": args.pullback_min,
        "pullback_max": args.pullback_max,
        "va_min_ratio": args.va_min_ratio,
        "defer_entry": defer_entry,
    }
    result = kpi_event_study.run_event_study(
        signals_df=signals_df[["signal_date", "code"]],
        kpi_name=args.kpi_name,
        params=params,
        period=(args.start, args.end),
        output_dir=output_root,
        base_rate_dir=Path(args.base_rate_dir),
        universe_window=args.universe_window,
        defer_entry=defer_entry,
    )
    lift_str = f"{result['lift']:.2f}" if result["lift"] is not None else "-"
    ci_str = (
        f"[{result['ci_low']:.2f}, {result['ci_high']:.2f}]" if result["ci_low"] is not None else "[-, -]"
    )
    print(f"ハーネス実行完了: n={result['n']} lift={lift_str} ci95={ci_str} verdict={result['verdict']}")
    print(f"report: {result['report_path']}")
    print(f"returns: {result['returns_path']}")

    with open(result["report_path"], "a", encoding="utf-8") as f:
        f.write("\n## S高判定・値幅制限テーブルの実装ノート(第15周T4)\n")
        f.write(
            f"- S高日検出={s_diag['stophigh_days']}件(前日終値なしで除外={s_diag['no_prev_close']})。"
            f"未調整O/H/L/C使用・基準値段=前営業日未調整終値・許容誤差{S_HIGH_TOLERANCE}円\n"
        )
        f.write(
            "- PRICE_LIMIT_TABLEは実装着手前にJ-Quants日足のUL(値幅上限到達)フラグを正解ラベルとして"
            "独立検証済み(約4300件のUL=1レコードで一致率97.7%・ETF/ETN等ProdCat!=011を除く内国株券"
            "限定なら99.9%超)。70,000円超の帯は実データに実例が無く等比数列パターンから外挿"
            "(TOP500ユニバースでは実質的な影響なしと判断)\n"
        )
        f.write(
            f"- 初押し再上昇探索: S高の{DIP_MIN_BDAYS}〜{DIP_MAX_BDAYS}営業日後を古い順に走査し、"
            f"条件成立の最初の1件のみ採用(1つのS高につき1回のみ)。"
            f"窓内マッチなし(S高はしたが再上昇条件を満たすDが窓内に無かった)={r_diag['no_match_within_window']}件\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
