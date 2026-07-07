#!/usr/bin/env python3
"""出来高ショック実務版 シグナル生成器（カタログ§2-C #4・v0リスト#4・初期構成）。

docs/stock-algo-kpi-catalog.md の §2-C「出来高ショック」実務版を実装する。
`data/jquants/bars/YYYYMMDD.json.gz`（四本値・出来高）のみで完結する（fins不使用）。

定義（グリッドサーチはしない・1構成のみ）:
    - 各営業日D・各銘柄について判定:
        (1) Va(D) >= 直近20回の有効Va観測値の平均 × 5倍（volume shock）
            ※「直近20営業日」の厳密な暦固定窓ではなく「直近20回の有効出来高観測値」を採用した
              実務版の定義（売買停止等の欠測日を挟んでも自然にスキップされ、演算量も抑えられる。
              PEAD側 compute_max20/va_ratio の暦固定窓とは定義が異なる点に留意）
        (2) 陽線: AdjC(D) > AdjO(D)
        (3) 前日終値比 +2%〜+8%（AdjC(D)/AdjC(D-1)-1。まだ大きく噴いていない初動のみ採用）
    - シグナル確定日 = D（Dそのものの四本値で判定。Dの出来高・終値を使うためlook-aheadではないが、
      「Dの引け後に確定するシグナル」である点はPEAD等の反応日Gと同じ性質）
    - Va(D)自体・当日の陽線判定・前日比判定は全てD時点までのデータのみ使用（D+1以降は一切参照しない）

このKPIはユニバース新規流入（新規上場・急上昇銘柄の直近ランクイン等）と重なりやすいため
（カタログ§6手順5）、判定対象（ユニバース内）シグナルの membership(new/existing) 内訳を
scripts/measure_base_rate.py が既に出力済みの `output/base_rate/universes_w{window}.csv.gz`
と突合して算出し、report.mdに追記する（Canonical Module原則・membership判定ロジックの
再実装はしない。既存出力を読むだけ）。

フォワードリターン計算・ユニバース判定・ブートストラップCI等は scripts/kpi_event_study.py の
run_event_study() に委譲する。第5周より --defer-entry を既定Trueとする（team-lead方針）。

第11周: 個別銘柄の200日線位置（BKLZ需給フラグ・カタログ§2-A）をvolshockの上に重ねる複合検証用に、
各シグナルへ dev200 = (AdjC(D) - SMA200(D)) / SMA200(D) を付与する（SMA200はD自身を含む直近200回の
有効AdjC観測値の平均。measure_base_rate.build_regime_series の「当日を含む直近200件平均」という
既存のTOPIX SMA200と同じ「D含む」PIT規約に合わせた・こちらは個別銘柄なので別途計算する）。
--filter-ma200 above|below を指定すると、そのdev200の符号でシグナルを絞り込む（未指定時は
従来どおり全シグナルを出力し、dev200列はNaNを含み得るのみでシグナル件数・判定には一切影響しない
＝既存のvolshock_5xベースラインとの比較可能性を壊さない）。200日分の有効履歴が無い銘柄・時期は
insufficient_ma200_history としてスキップする（データ開始2016-07のため実質2017年5月頃以降が対象）。

Usage:
    python3 scripts/kpi_volshock_signals.py --start 2016-11 --end 2022-11
    python3 scripts/kpi_volshock_signals.py --start 2016-11 --end 2022-11 --skip-harness
    python3 scripts/kpi_volshock_signals.py --start 2016-11 --end 2022-11 --filter-ma200 above \\
        --kpi-name volshock_x_above200
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: DATA_ROOT を再利用。第11周dev200のウォームアップ計算用)
import kpi_event_study  # noqa: E402  (Canonical Module: run_event_study を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: IN_SAMPLE_START/END・MONTH_RE を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・bars読み込みを再利用)

KPI_NAME = "volshock_5x"

VOL_MULTIPLIER_DEFAULT = 5.0
VOL_HISTORY_WINDOW = 20  # 直近何回の有効Va観測値を平均するか
DAY_RET_MIN_DEFAULT = 0.02
DAY_RET_MAX_DEFAULT = 0.08
# 元は20日窓(VOL_HISTORY_WINDOW)向けの安全マージンとして40だったが、第11周のdev200(200日窓)を
# 満たすため260に拡大した(200日窓が20日窓を包含するため、この拡大はva_hist側の判定結果を変えない
# ＝deque(maxlen=20)は直近20件のみ保持するので、それより前のウォームアップをどれだけ延ばしても
# idx_start到達時点の中身は変わらない。既存のvolshock_5xベースラインとの比較可能性は壊さない)。
WARMUP_BDAYS = 260

MA200_WINDOW = 200  # dev200用SMA200のウィンドウ（D自身を含む直近200回の有効AdjC観測値。第11周）
MA200_FILTER_CHOICES = ("above", "below")


def _earliest_bars_date() -> str:
    """data/jquants/bars/ に実在する最古の営業日(YYYYMMDD)を返す(ハードコードしない)。

    kpi_high52_signals.py の同名関数と同じ実装(252/200日といった長期ウィンドウを持つ
    シグナル生成器が共通で必要とする単純な事前チェックのため、両スクリプトに意図的に複製している。
    measure_base_rate.py は§6準拠のため変更禁止で、そちらに追加関数を足すのは避けた)。
    """
    bars_dir = jq_fetch.DATA_ROOT / "bars"
    dates = sorted(p.name[:8] for p in bars_dir.glob("*.json.gz"))
    if not dates:
        raise SystemExit(f"FATAL: bars キャッシュが1件も見つかりません: {bars_dir}")
    return dates[0]


# --- シグナル生成 ---------------------------------------------------------------


def generate_volshock_signals(
    start_bd: str,
    end_bd: str,
    vol_multiplier: float = VOL_MULTIPLIER_DEFAULT,
    day_ret_min: float = DAY_RET_MIN_DEFAULT,
    day_ret_max: float = DAY_RET_MAX_DEFAULT,
    filter_ma200: Optional[str] = None,
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd]（YYYYMMDD営業日）内の全銘柄・全営業日から出来高ショックシグナルを生成する。

    1営業日1パスの逐次スキャンで実装する（各銘柄のVa履歴をdeque(maxlen=20)で保持し、
    20営業日分のbarsファイルを毎日読み直すことを避ける＝計算量O(全日数×全銘柄)に抑える）。

    Args:
        filter_ma200: "above"/"below"/None（既定）。指定時はdev200の符号（above: dev200>0,
            below: dev200<0）でシグナルを絞り込む。200日分の有効履歴が無くdev200が計算できない
            シグナルはフィルタ指定時のみ除外する（Noneの場合は従来どおり全件を出力し、
            dev200/sma200列にNaNを含み得るのみ＝既存のvolshock_5xベースラインとの比較可能性を壊さない）。

    Returns:
        (signals_df, diag)。signals_df の列: signal_date, code, va, va_avg20, day_ret, adjc, adjo,
        sma200, dev200。diag はフィルタ段階別の件数内訳。
    """
    if filter_ma200 is not None and filter_ma200 not in MA200_FILTER_CHOICES:
        raise SystemExit(f"FATAL: filter_ma200 は {MA200_FILTER_CHOICES} のみ対応です（指定値: {filter_ma200}）")
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    idx_start = bday_index[start_bd]
    idx_end = bday_index[end_bd]
    idx_earliest_bars = bday_index[_earliest_bars_date()]
    warmup_idx = max(idx_earliest_bars, idx_start - WARMUP_BDAYS)
    scan_days = all_bdays[warmup_idx : idx_end + 1]

    va_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=VOL_HISTORY_WINDOW))
    adjc200_hist: dict[str, deque] = defaultdict(lambda: deque(maxlen=MA200_WINDOW))
    prev_bars: Optional[dict] = None

    diag = {
        "business_days_scanned": 0,  # イベント期間内（ウォームアップ除く）の営業日数
        "code_day_observations": 0,
        "insufficient_volume_history": 0,
        "volume_shock_5x": 0,
        "not_green_candle": 0,
        "no_prev_close": 0,
        "day_ret_out_of_range": 0,
        "signals_volshock5x": 0,
        "insufficient_ma200_history": 0,  # 200日分の有効AdjC観測値が無くdev200計算不能（signals_volshock5xの内数）
        "filtered_by_ma200": 0,  # --filter-ma200指定時、dev200の符号が不一致で除外（signals_volshock5xの内数）
    }

    rows: list[dict] = []

    for d in scan_days:
        bars_d = measure_base_rate.load_bars_day(d)
        in_event_window = start_bd <= d <= end_bd
        if in_event_window:
            diag["business_days_scanned"] += 1

        for code, rec in bars_d.items():
            va_d = rec.get("Va")
            adjc_d = rec.get("AdjC")

            # dev200用の200日履歴はD自身を含む規約（build_regime_seriesのSMA200と同じ「当日を
            # 含む直近N件平均」）のため、当日の判定より先に更新する（va_histとは順序が逆）。
            hist200 = adjc200_hist[code]
            if adjc_d is not None:
                hist200.append(adjc_d)

            if in_event_window:
                diag["code_day_observations"] += 1
                hist = va_hist.get(code)
                if hist is not None and len(hist) == VOL_HISTORY_WINDOW:
                    va_avg = sum(hist) / len(hist)
                    if va_d and va_avg > 0 and va_d >= va_avg * vol_multiplier:
                        diag["volume_shock_5x"] += 1
                        adjc = rec.get("AdjC")
                        adjo = rec.get("AdjO")
                        if adjc is not None and adjo is not None and adjc > adjo:
                            prev_rec = (prev_bars or {}).get(code)
                            prev_close = prev_rec.get("AdjC") if prev_rec else None
                            if prev_close:
                                day_ret = adjc / prev_close - 1
                                if day_ret_min <= day_ret <= day_ret_max:
                                    if len(hist200) == MA200_WINDOW:
                                        sma200 = sum(hist200) / len(hist200)
                                        dev200 = (adjc - sma200) / sma200 if sma200 else None
                                    else:
                                        sma200 = None
                                        dev200 = None
                                        diag["insufficient_ma200_history"] += 1

                                    if filter_ma200 is not None:
                                        if dev200 is None:
                                            pass  # insufficient_ma200_historyに計上済み・行は追加しない
                                        elif filter_ma200 == "above" and dev200 <= 0:
                                            diag["filtered_by_ma200"] += 1
                                        elif filter_ma200 == "below" and dev200 >= 0:
                                            diag["filtered_by_ma200"] += 1
                                        else:
                                            diag["signals_volshock5x"] += 1
                                            rows.append(
                                                {
                                                    "signal_date": d,
                                                    "code": code,
                                                    "va": va_d,
                                                    "va_avg20": va_avg,
                                                    "day_ret": day_ret,
                                                    "adjc": adjc,
                                                    "adjo": adjo,
                                                    "sma200": sma200,
                                                    "dev200": dev200,
                                                }
                                            )
                                    else:
                                        # フィルタ未指定＝従来どおり全件出力（既存ベースラインとの
                                        # 比較可能性を壊さない。dev200/sma200はNaNを含み得る）
                                        diag["signals_volshock5x"] += 1
                                        rows.append(
                                            {
                                                "signal_date": d,
                                                "code": code,
                                                "va": va_d,
                                                "va_avg20": va_avg,
                                                "day_ret": day_ret,
                                                "adjc": adjc,
                                                "adjo": adjo,
                                                "sma200": sma200,
                                                "dev200": dev200,
                                            }
                                        )
                                else:
                                    diag["day_ret_out_of_range"] += 1
                            else:
                                diag["no_prev_close"] += 1
                        else:
                            diag["not_green_candle"] += 1
                else:
                    diag["insufficient_volume_history"] += 1

            # 履歴更新は当日の判定より後（＝当日のVaは当日自身の判定には使うが、次回以降の
            # 「直近20回」の一員としてのみ蓄積する。翌日以降の判定にのみ影響しlook-aheadではない）
            if va_d:
                va_hist[code].append(va_d)

        prev_bars = bars_d

    return pd.DataFrame(rows), diag


# --- membership(new/existing)内訳（既存のuniverses_w{window}.csv.gzを突合するのみ） -----


def compute_membership_breakdown(returns_path: Path, base_rate_dir: Path, universe_window: int) -> dict:
    """判定対象（in_universe）シグナルのmembership(new/existing)内訳を算出する。

    scripts/measure_base_rate.py が既に出力済みの universes_w{window}.csv.gz
    （month, code, rank, turnover_sum, membership 列）と突合するだけで、membership判定
    ロジック自体は一切再実装しない（Canonical Module原則）。
    """
    universes_path = base_rate_dir / f"universes_w{universe_window}.csv.gz"
    universes_df = pd.read_csv(universes_path, dtype={"month": str, "code": str})
    lookup = {(m, c): mem for m, c, mem in zip(universes_df["month"], universes_df["code"], universes_df["membership"])}

    returns_df = pd.read_csv(
        returns_path, dtype={"code": str, "universe_month_used": str}
    )
    in_universe_df = returns_df[returns_df["in_universe"] == True]  # noqa: E712 (CSV読込のbool比較)

    memberships = [
        lookup.get((row.universe_month_used, row.code), "unknown") for row in in_universe_df.itertuples()
    ]
    counts: dict[str, int] = defaultdict(int)
    for m in memberships:
        counts[m] += 1
    return dict(counts)


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="出来高ショック実務版 シグナル生成器（カタログ§2-C #4）+ KPI検証ハーネス実行"
    )
    parser.add_argument(
        "--start", default=kpi_pead_signals.IN_SAMPLE_START,
        help=f"検索開始月 YYYY-MM（既定 {kpi_pead_signals.IN_SAMPLE_START}）",
    )
    parser.add_argument(
        "--end", default=kpi_pead_signals.IN_SAMPLE_END,
        help=f"検索終了月 YYYY-MM（既定 {kpi_pead_signals.IN_SAMPLE_END}）",
    )
    parser.add_argument("--vol-multiplier", type=float, default=VOL_MULTIPLIER_DEFAULT)
    parser.add_argument("--day-ret-min", type=float, default=DAY_RET_MIN_DEFAULT)
    parser.add_argument("--day-ret-max", type=float, default=DAY_RET_MAX_DEFAULT)
    parser.add_argument("--kpi-name", default=KPI_NAME)
    parser.add_argument("--output-dir", default="output/kpi", help="ハーネス出力先ルート（kpi-name配下に生成）")
    parser.add_argument("--base-rate-dir", default=str(kpi_event_study.DEFAULT_BASE_RATE_DIR))
    parser.add_argument("--universe-window", type=int, default=21, choices=[21, 126])
    parser.add_argument(
        "--skip-harness", action="store_true", help="シグナル生成のみ行いハーネス実行をスキップ（デバッグ用）"
    )
    parser.add_argument(
        "--no-defer-entry", action="store_true",
        help="第5周より--defer-entryが既定Trueのため、従来のT+1固定挙動に戻したい場合のみ指定",
    )
    parser.add_argument(
        "--filter-ma200", choices=list(MA200_FILTER_CHOICES), default=None,
        help="dev200(個別銘柄の200日線乖離)の符号でシグナルを絞り込む(第11周・BKLZ需給フラグ検証用)",
    )
    args = parser.parse_args()

    if not kpi_pead_signals.MONTH_RE.match(args.start) or not kpi_pead_signals.MONTH_RE.match(args.end):
        raise SystemExit("FATAL: --start/--end は YYYY-MM 形式で指定してください")
    if args.end > kpi_pead_signals.IN_SAMPLE_END:
        print(
            f"WARN: --end={args.end} は§6で凍結したholdout期間(2023年以降)に抵触します。"
            f"in-sample評価は{kpi_pead_signals.IN_SAMPLE_END}までに限定してください"
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

    signals_df, diag = generate_volshock_signals(
        start_bd, end_bd, args.vol_multiplier, args.day_ret_min, args.day_ret_max,
        filter_ma200=args.filter_ma200,
    )

    output_root = Path(args.output_dir)
    kpi_dir = output_root / args.kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_path = kpi_dir / "signals_raw.csv"
    signals_df.to_csv(signals_path, index=False)

    print(
        f"シグナル生成完了: {len(signals_df)}件"
        f"（vol_multiplier={args.vol_multiplier}x, day_ret={args.day_ret_min:.0%}〜{args.day_ret_max:.0%}）"
    )
    print(
        f"営業日走査={diag['business_days_scanned']}日 / 銘柄日観測={diag['code_day_observations']} "
        f"(出来高履歴不足={diag['insufficient_volume_history']}, 出来高5倍超={diag['volume_shock_5x']}, "
        f"陽線でない除外={diag['not_green_candle']}, 前日終値なし={diag['no_prev_close']}, "
        f"前日比レンジ外={diag['day_ret_out_of_range']}, シグナル成立={diag['signals_volshock5x']})"
    )
    print(
        f"dev200: 200日履歴不足で計算不能={diag['insufficient_ma200_history']}"
        + (f" / --filter-ma200={args.filter_ma200}で符号不一致除外={diag['filtered_by_ma200']}" if args.filter_ma200 else "")
    )
    print(f"出力: {signals_path}")

    if args.skip_harness:
        return 0
    if signals_df.empty:
        print("WARN: シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    defer_entry = not args.no_defer_entry
    params = {
        "vol_multiplier": args.vol_multiplier,
        "vol_history_window": VOL_HISTORY_WINDOW,
        "day_ret_min": args.day_ret_min,
        "day_ret_max": args.day_ret_max,
        "defer_entry": defer_entry,
        "filter_ma200": args.filter_ma200,
        "ma200_window": MA200_WINDOW,
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

    membership_counts = compute_membership_breakdown(
        result["returns_path"], Path(args.base_rate_dir), args.universe_window
    )
    membership_line = " / ".join(f"{k}={v}" for k, v in sorted(membership_counts.items()))
    print(f"membership内訳（判定対象={result['n']}件）: {membership_line}")

    with open(result["report_path"], "a", encoding="utf-8") as f:
        f.write("\n## ユニバースmembership内訳（第5周・§6手順5注記）\n")
        f.write(
            f"- 判定対象（in_universe）シグナル{result['n']}件のmembership内訳: {membership_line}"
            f"（`output/base_rate/universes_w{args.universe_window}.csv.gz`と突合。"
            f"membership判定ロジック自体はmeasure_base_rate.py側のCanonical Moduleをそのまま参照）\n"
        )
        if args.filter_ma200:
            f.write(
                f"\n## dev200(200日線位置)フィルタ内訳（第11周・BKLZ需給フラグ検証）\n"
                f"- --filter-ma200={args.filter_ma200}\n"
                f"- 200日履歴不足でdev200計算不能(生シグナルからスキップ): {diag['insufficient_ma200_history']}件\n"
                f"- フィルタ条件(dev200符号)不一致で除外: {diag['filtered_by_ma200']}件\n"
                f"- 比較用ベースライン(volshock_5x無印・フィルタなし): n=292 lift=2.34[1.39,3.43] "
                f"EV(なし)+0.57% EV(-8%損切り)-0.75%（過去実行分。本レポートのn/lift/EVと直接比較すること）\n"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
