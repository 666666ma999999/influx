#!/usr/bin/env python3
"""前向き検証中/観察中の全系統の「成熟予定日」と「現行αでの検出力」を一覧化するレポート生成。

**経営計画/運用監視用。合否判定には一切使用しない。**

レビュー合意事項P3/BB対応・Codexレビュー指摘反映（2026-07-16）。`config/paper_watchlist.json` の
status="observation" 系統（休眠/棄却系statusは除外。除外理由は本ファイル
EXCLUDED_STATUS_REASONS と出力レポート脚注を参照）について、以下を機械算出する:

    (a) 月間シグナル頻度: 第一候補=output/kpi/<kpi>/returns.csv の in-sample行数÷期間月数
        （ファイル実在時。in_universe==True かつ in_sample.period 期間に限定）。
        無い系統は docs/stock-algo-kpi-catalog.md 記載の頻度値（FREQ_OVERRIDE参照）
    (b) n≥100到達予測日: 各系統の registered_date（watchlist実読）起点・(a)の頻度で外挿
        （前向き検証基盤の稼働開始日は系統ごとに2026-07-07〜2026-07-15と幅があるため、
        一律固定ではなく系統別に正確な起点を使う）
    (c) 12完全暦月到達日: 2027-08-01固定（全系統共通・仕様どおり。FULL_12MONTH_DATE）
    (d) 概算最早判定日† = (b)と(c)の遅い方。bull/bear両レジームカバー要件は全行共通脚注で明記。
        「概算」なのはn100到達予測が頻度の外挿値であり、レジーム条件を未判定のため
    (e) 片側α: docs/stock-algo-kpi-catalog.md の陣別alpha-spending（0.05/2^b を陣内系統数で
        分割）を系統ごとに定数表（ALPHA_TABLE）に固定。該当カタログ行番号はコメント参照。
        陣別alpha-spending導入(2026-07-13)より前に登録された4系統（volshock_5x/
        volshock_x_above200/volshock_x_above200_quiet/shortcover_x_bear）はcatalog上に
        個別のforward-test α明記がないため本表ではα=N/A・power=N/Aとし、別セクション
        「4. 参考: αが未定義の4系統に片側α=0.025を仮定した場合の感度」に隔離する
        （SUEペアはcatalog L1112-1118に明示基準95%CI下限>0があるため本表のままα=0.025を採用）
    (f) 検出力感度表: 真EV∈{+0.5%,+1%,+2%}それぞれで
        power = Φ( EV/(σ/√n_eff) − z_(1-α) ) （正規近似・scipy不使用・math.erfで自作）。
        σの算出列はret列（nostop・正式primary endpoint）を優先し、無い場合のみret_stop8に
        フォールバック（フォールバック発生時はレポートに明示）。n_effと対応するσは2通り
        （次元を一致させるため保守列はσも取引レベルではなく月次平均リターン系列のSDを使う）:
          - 楽観: n_eff=判定時点の予測シグナル数（頻度×経過月数）・σ=σ_trade（取引レベル
            endpoint列の標本標準偏差。各シグナルが独立というσ×√n型の仮定）
          - 保守: n_eff=判定時点の経過月数（月次ブロック相当）・σ=σ_month（月次平均endpoint
            系列の標本標準偏差。取引間の系統内相関を月次集計で粗く補正した次元一致の値）

出力: output/kpi_maturity/report.md（Markdown・仮定節/出典節つき）
      output/kpi_maturity/maturity.csv

Usage:
    docker compose run --rm xstock python scripts/kpi_maturity_power.py
"""
from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

WATCHLIST_PATH = Path("config/paper_watchlist.json")
CATALOG_PATH = Path("docs/stock-algo-kpi-catalog.md")
OUTPUT_KPI_DIR = Path("output/kpi")
REPORT_DIR = Path("output/kpi_maturity")
REPORT_MD_PATH = REPORT_DIR / "report.md"
REPORT_CSV_PATH = REPORT_DIR / "maturity.csv"

# 12完全暦月到達日（仕様固定・全系統共通）:
#   初の完全暦月 = 2026-08 → 12ヶ月目終了 = 2027-07-31 → 判定可能 = 2027-08-01
# n≥100到達予測日の起点は「一律固定」ではなく系統ごとの registered_date（watchlist実読）を
# 使う（Codexレビュー指摘で修正・旧版はORIGIN_DATE=2026-07-07を全系統一律に適用していた）。
FULL_12MONTH_DATE = date(2027, 8, 1)

# グレゴリオ暦の平均月長（365.2425日 / 12ヶ月）。月数⇔日数の変換に一貫して使用する。
AVG_DAYS_PER_MONTH = 365.2425 / 12

# 検出力感度表で評価する真EVのシナリオ（+0.5%・+1%・+2%）
EV_SCENARIOS: tuple[float, ...] = (0.005, 0.01, 0.02)

# 対象とする有効status（"前向き検証中/観察中"）。休眠/棄却系は除外する。
ACTIVE_STATUS = "observation"

# 除外したstatusとその理由（レポート脚注に転記）。
# config/paper_watchlist.json 実読の結果、status語彙は observation(17) / reference(1) /
# hoos_rejected(1) の3種のみ（休眠/dormant系statusは存在しなかった）。
EXCLUDED_STATUS_REASONS: dict[str, str] = {
    "hoos_rejected": (
        "前向きholdout-of-sample観察で棄却判定済み（sh_dip_reentry。純観察の対象外・"
        "以後の意思決定に使わない棄却系status）"
    ),
    "reference": (
        "holdout検証でverdict=REJECTED済み（pead_gap8_vol3）。config記載のroleに"
        "「参照のみ...追跡観察として継続監視するが新候補の意思決定には使わない」と明記されており、"
        "前向き正式判定の対象外（棄却済みを継続監視するだけの参照status）"
    ),
}

# ---------------------------------------------------------------------------
# 系統 → 片側α 定数表（陣別 alpha-spending）
#
# docs/stock-algo-kpi-catalog.md の陣別alpha-spending定義（陣bの予算=0.05/2^b・
# 陣内系統数で分割・幾何級数の総和が0.05以下に収まる設計）:
#   第1陣(5系統) 0.025/5   = α0.005      （catalog L1772・2026-07-13 §7-Q登録）
#   第2陣(3系統) 0.0125/3  ≈ α0.004167   （catalog L1773・2026-07-13 §7-T登録）
#   第3陣(1系統) 0.05/2^3/1 = α0.00625    （catalog L1972・2026-07-14 §7-W登録）
#   第4陣(2系統) 0.05/2^4/2 = α0.0015625  （catalog L2069・2026-07-14 §7-Y登録）
#   第5陣(1系統) 0.05/2^5/1 = α0.0015625  （catalog L2218・2026-07-15 §7-AB登録）
#
# 陣別alpha-spending導入(2026-07-13)より前に登録された6系統
# （volshock_5x / volshock_x_above200 / volshock_x_above200_quiet / shortcover_x_bear /
#  sue_x_above200 / sue_beat）には陣番号が割り当てられていない。catalogはSUEペアについてのみ
# 明示的な前向き正式判定基準「95%CI下限>0」（catalog L1112-1118）を記載しており、これは
# 両側95%CI下限>0 ⇔ 片側α=0.025 相当の検定と等価であるため、SUEペアは本表でα=0.025を採用する。
#
# 一方 volshock/shortcover の4系統（ALPHA_UNDEFINED_SYSTEMS）にはcatalog上に個別の
# forward-test α明記がなく、SUEペアと同水準とみなせる明示的根拠が無いため、本表では
# α=N/A・power=N/Aとし、参考として片側α=0.025を仮定した場合の感度のみ別セクション
# （4. 参考）に分離して表示する（Codexレビュー指摘: 旧版はSUEと同一のPRE_COHORT_ALPHAを
# 未定義4系統にも本表内で直接適用しており、根拠のない仮定が正式値と同列に見える構成だった）。
PRE_COHORT_ALPHA = 0.025
_PRE_COHORT_NOTE_SUE = "catalog L1112-1118: 前向き正式判定基準=95%CI下限>0（⇔片側α=0.025相当）"

# 参考セクション専用の仮定α（PRE_COHORT_ALPHAと同値だが、正式採用ではなく感度分析用であることを
# 変数名で明示する）。
REFERENCE_ALPHA = PRE_COHORT_ALPHA

ALPHA_UNDEFINED_SYSTEMS: tuple[str, ...] = (
    "volshock_5x",
    "volshock_x_above200",
    "volshock_x_above200_quiet",
    "shortcover_x_bear",
)
ALPHA_UNDEFINED_NOTE = (
    "陣別alpha-spending導入(2026-07-13・catalog L1769-1774)より前に登録され、catalog上に"
    "個別のforward-test α明記がない。SUEペア(同時期登録)にはcatalog L1112-1118の明示基準"
    "(95%CI下限>0)があるためα=0.025を採用しているが、本系統には同水準の根拠となる明示基準が"
    "catalog上に見当たらないため、正式なα確定まで本表ではα=N/A・power=N/Aとする"
    "（参考として片側α=0.025を仮定した場合の感度は本レポート§4参照。正式な凍結ルールではない）"
)

ALPHA_TABLE: dict[str, tuple[float, str]] = {
    "sue_x_above200": (PRE_COHORT_ALPHA, _PRE_COHORT_NOTE_SUE),
    "sue_beat": (PRE_COHORT_ALPHA, _PRE_COHORT_NOTE_SUE),
    "sell_reg_trigger_rebound": (0.025 / 5, "第1陣5系統・catalog L1772: 0.025/5=α0.005"),
    "turnover_rank_surge": (0.025 / 5, "第1陣5系統・catalog L1772: 0.025/5=α0.005"),
    "margin_expand_yoy": (0.025 / 5, "第1陣5系統・catalog L1772: 0.025/5=α0.005"),
    "raw_strev_entry": (0.025 / 5, "第1陣5系統・catalog L1772: 0.025/5=α0.005"),
    "gap_hold_close_strong": (0.0125 / 3, "第2陣3系統・catalog L1773: 0.0125/3=α0.004167"),
    "engulf_reversal_day": (0.0125 / 3, "第2陣3系統・catalog L1773: 0.0125/3=α0.004167"),
    "three_up_ignition": (0.0125 / 3, "第2陣3系統・catalog L1773: 0.0125/3=α0.004167"),
    "sales_beat": (0.05 / 2 ** 3 / 1, "第3陣1系統・catalog L1972: 0.05/2^3/1=α0.00625"),
    "guidance_fy_strong": (0.05 / 2 ** 4 / 2, "第4陣2系統・catalog L2069: 0.05/2^4/2=α0.0015625"),
    "cfo_margin_improve": (0.05 / 2 ** 4 / 2, "第4陣2系統・catalog L2069: 0.05/2^4/2=α0.0015625"),
    "earnings_spillover": (0.05 / 2 ** 5 / 1, "第5陣1系統・catalog L2218: 0.05/2^5/1=α0.0015625"),
}

# 月間シグナル頻度の第二候補（output/kpi/<kpi>/returns.csv が存在しない系統のみ・catalog記載値）
FREQ_OVERRIDE: dict[str, tuple[float, str]] = {
    "volshock_x_above200_quiet": (
        1.0,
        "output/kpi/volshock_x_above200_quiet/returns.csv 不在のためcatalog記載値を採用。"
        "catalog L458: 「チャンピオン構成としてペーパートレード観察入り（月約1件のスナイパー型）」",
    ),
    "sue_x_above200": (
        8.1,
        "output/kpi/sue_x_above200/returns.csv 不在のためcatalog記載値を採用。"
        "catalog L1064: 「主候補 sue_x_above200（§7-H H1確定値・月平均約8.1件）」",
    ),
}


@dataclass
class SystemRow:
    """1系統ぶんの成熟予定日・検出力算出結果。"""

    kpi_name: str
    status: str
    registered_date: str
    origin_date: date
    n_insample: Optional[int]
    freq_monthly: float
    freq_provenance: str
    n100_date: date
    full12_date: date
    maturity_date: date
    alpha: Optional[float]
    alpha_provenance: str
    endpoint_col: Optional[str]
    endpoint_fallback: bool
    sigma_trade: Optional[float]
    sigma_month: Optional[float]
    sigma_provenance: str
    n_eff_optimistic: int
    n_eff_conservative: int
    power: dict[tuple[float, str], Optional[float]] = field(default_factory=dict)
    ref_power: dict[tuple[float, str], Optional[float]] = field(default_factory=dict)


def norm_cdf(x: float) -> float:
    """標準正規分布の累積分布関数（scipy不使用・math.erfで自作）。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_ppf(p: float, tol: float = 1e-12) -> float:
    """標準正規分布のパーセント点関数（逆CDF）。norm_cdf に対する二分探索で自作する（scipy不使用）。

    Args:
        p: 累積確率（0<p<1）。
        tol: 二分探索の収束許容幅。

    Returns:
        Φ(z) = p を満たす z。
    """
    lo, hi = -10.0, 10.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2.0


def compute_power(
    ev: float, sigma: Optional[float], n_eff: float, alpha: Optional[float]
) -> Optional[float]:
    """片側z検定の検出力を正規近似で算出する。

    power = Φ( EV/(σ/√n_eff) − z_(1-α) )

    Args:
        ev: 仮定する真のEV（例 0.005 = +0.5%）。
        sigma: リターン列の標本標準偏差。returns.csv不在等でNoneの場合は算出不可。
        n_eff: 有効サンプルサイズ（楽観=予測n・保守=経過月数）。
        alpha: 片側有意水準。未定義（陣別alpha-spending導入前で個別根拠なし）の場合はNone。

    Returns:
        検出力（0〜1）。sigma/alpha が None・0以下、または n_eff<=0 の場合は None（算出不可）。
    """
    if sigma is None or sigma <= 0 or n_eff <= 0 or alpha is None:
        return None
    z_alpha = norm_ppf(1.0 - alpha)
    se = sigma / math.sqrt(n_eff)
    return norm_cdf(ev / se - z_alpha)


def parse_period_months(period: str) -> tuple[int, int, int, int, int]:
    """"2016-11〜2022-11" 形式の period 文字列を (y1, m1, y2, m2, n_months) に変換する。

    Args:
        period: watchlist の in_sample.period フィールド値。

    Returns:
        開始年・開始月・終了年・終了月・期間月数（両端含む）のタプル。
    """
    m = re.match(r"(\d{4})-(\d{2}).*?(\d{4})-(\d{2})", period)
    if not m:
        raise ValueError(f"period文字列を解釈できません: {period!r}")
    y1, m1, y2, m2 = (int(x) for x in m.groups())
    n_months = (y2 - y1) * 12 + (m2 - m1) + 1
    return y1, m1, y2, m2, n_months


def load_returns_frequency_sigma(kpi_name: str, period: str) -> Optional[dict]:
    """output/kpi/<kpi>/returns.csv から in-sample 月間頻度と2種のσを算出する（第一候補）。

    in_universe==True かつ signal_date が in_sample.period 範囲内の行のみを対象とする。
    watchlist記載のnと完全一致することを事前確認済み（volshock_5x等15系統で検証）。

    endpoint列は ret（nostop・正式primary endpoint）を優先し、無い場合のみ ret_stop8 に
    フォールバックする（フォールバック発生はprovenanceに明示）。

    σは2種算出する（n_effの次元と一致させるため）:
        sigma_trade: endpoint列の取引レベル標本標準偏差（楽観列=予測シグナル数に対応）
        sigma_month: 月次平均endpoint値の系列の標本標準偏差（保守列=経過月数に対応）

    Args:
        kpi_name: watchlist の kpi_name。
        period: watchlist in_sample.period（例 "2016-11〜2022-11"）。

    Returns:
        n / months / freq_monthly / sigma_trade / sigma_month / endpoint_col 等を含む dict。
        returns.csv が存在しなければ None（呼び出し側で FREQ_OVERRIDE にフォールバック）。
    """
    path = OUTPUT_KPI_DIR / kpi_name / "returns.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    y1, m1, y2, m2, n_months = parse_period_months(period)
    ym_start, ym_end = y1 * 100 + m1, y2 * 100 + m2
    ym = df["signal_date"] // 100
    mask = (df["in_universe"] == True) & (ym >= ym_start) & (ym <= ym_end)  # noqa: E712
    sub = df[mask].copy()
    sub["_ym"] = ym[mask]
    n = len(sub)
    freq_monthly = n / n_months

    if "ret" in sub.columns:
        endpoint_col = "ret"
        endpoint_fallback = False
    elif "ret_stop8" in sub.columns:
        endpoint_col = "ret_stop8"
        endpoint_fallback = True
    else:
        raise ValueError(f"{path}: ret/ret_stop8列がいずれも存在しません")

    sigma_trade = float(sub[endpoint_col].std(ddof=1))

    monthly_means = sub.groupby("_ym")[endpoint_col].mean()
    n_months_with_data = len(monthly_means)
    if n_months_with_data >= 2:
        sigma_month: Optional[float] = float(monthly_means.std(ddof=1))
    else:
        sigma_month = None

    fallback_note = "[ret列不在のためret_stop8にフォールバック]" if endpoint_fallback else ""
    provenance = (
        f"output/kpi/{kpi_name}/returns.csv（in_universe==True・{period}・"
        f"n={n}・{n_months}ヶ月・endpoint列={endpoint_col}{fallback_note}・"
        f"月次データあり{n_months_with_data}/{n_months}ヶ月）"
    )
    return {
        "n": n,
        "months": n_months,
        "freq_monthly": freq_monthly,
        "sigma_trade": sigma_trade,
        "sigma_month": sigma_month,
        "endpoint_col": endpoint_col,
        "endpoint_fallback": endpoint_fallback,
        "n_months_with_data": n_months_with_data,
        "provenance": provenance,
    }


def compute_maturity_dates(freq_monthly: float, origin_date: date) -> dict:
    """n≥100到達予測日・12完全暦月到達日・概算最早判定日を算出する。

    n≥100到達予測日は各系統の origin_date（=registered_date。watchlist実読）起点で
    頻度から外挿する。12完全暦月到達日は仕様固定（全系統共通・FULL_12MONTH_DATE）。

    Args:
        freq_monthly: 月間シグナル頻度。
        origin_date: n≥100到達予測の外挿起点（系統ごとのregistered_date）。

    Returns:
        n100_date / maturity_date / n_eff_optimistic / n_eff_conservative を含む dict。
    """
    months_to_n100 = 100.0 / freq_monthly
    n100_date = origin_date + timedelta(days=months_to_n100 * AVG_DAYS_PER_MONTH)
    maturity_date = max(n100_date, FULL_12MONTH_DATE)
    elapsed_days = (maturity_date - origin_date).days
    elapsed_months_float = elapsed_days / AVG_DAYS_PER_MONTH
    elapsed_months_int = max(1, math.floor(elapsed_months_float))
    n_eff_optimistic = max(1, round(freq_monthly * elapsed_months_float))
    return {
        "n100_date": n100_date,
        "maturity_date": maturity_date,
        "n_eff_optimistic": n_eff_optimistic,
        "n_eff_conservative": elapsed_months_int,
    }


def verify_catalog_citations() -> list[str]:
    """カタログ引用文言の存在を実行時に軽量検証する（catalogは読み取り専用・書き込みしない）。

    行番号がずれても文言自体が残っていれば警告のみに留める（FATALにしない・
    ノンブロッキングな経営監視用チェック）。

    Returns:
        検証で見つからなかった文言の警告メッセージのリスト（空なら全一致）。
    """
    warnings: list[str] = []
    if not CATALOG_PATH.exists():
        return [f"catalog不在のため引用検証をスキップ: {CATALOG_PATH}"]
    text = CATALOG_PATH.read_text(encoding="utf-8")
    checks = [
        ("月約1件のスナイパー型", 458),
        ("月平均約8.1件", 1064),
        ("第1陣（5系統）: 0.025/5", 1772),
        ("第2陣（3系統）: 0.0125/3", 1773),
        ("第3陣予算 = 0.05/2^3 = 0.00625", 1972),
        ("第4陣予算 = 0.05/2^4 = 0.003125", 2069),
        ("第5陣予算 = 0.05/2^5 = 0.0015625", 2218),
        ("95%CI下限>0", 1114),
    ]
    for phrase, line_no in checks:
        if phrase not in text:
            warnings.append(f"catalog引用文言が見つかりません（想定L{line_no}）: {phrase!r}")
    return warnings


def load_watchlist() -> tuple[list[dict], list[dict]]:
    """config/paper_watchlist.json を読み込み、対象(status=observation)と除外系統を分離する。

    Returns:
        (active_entries, excluded_entries) のタプル。
    """
    data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    entries = data["watchlist"]
    active = [e for e in entries if e["status"] == ACTIVE_STATUS]
    excluded = [e for e in entries if e["status"] != ACTIVE_STATUS]
    return active, excluded


def build_rows(active_entries: list[dict]) -> list[SystemRow]:
    """対象系統ごとに頻度・成熟日・α・検出力を算出し SystemRow のリストを組み立てる。

    Args:
        active_entries: status=observation の watchlist エントリ一覧。

    Returns:
        maturity_date 昇順にソートした SystemRow のリスト。
    """
    rows: list[SystemRow] = []
    for entry in active_entries:
        kpi_name = entry["kpi_name"]
        period = entry["in_sample"]["period"]
        registered_date = entry.get("registered_date", "")
        origin_date = date.fromisoformat(registered_date)

        stats = load_returns_frequency_sigma(kpi_name, period)
        if stats is None:
            freq_monthly, freq_provenance = FREQ_OVERRIDE[kpi_name]
            n_insample = entry["in_sample"].get("n")
            endpoint_col, endpoint_fallback = None, False
            sigma_trade, sigma_month = None, None
            sigma_provenance = "returns.csv不在のため算出不可（検出力はN/A）"
        else:
            freq_monthly = stats["freq_monthly"]
            freq_provenance = stats["provenance"]
            n_insample = stats["n"]
            endpoint_col = stats["endpoint_col"]
            endpoint_fallback = stats["endpoint_fallback"]
            sigma_trade = stats["sigma_trade"]
            sigma_month = stats["sigma_month"]
            sigma_provenance = stats["provenance"]

        dates = compute_maturity_dates(freq_monthly, origin_date)

        if kpi_name in ALPHA_TABLE:
            alpha, alpha_provenance = ALPHA_TABLE[kpi_name]
        elif kpi_name in ALPHA_UNDEFINED_SYSTEMS:
            alpha, alpha_provenance = None, ALPHA_UNDEFINED_NOTE
        else:
            raise KeyError(
                f"{kpi_name}: ALPHA_TABLE/ALPHA_UNDEFINED_SYSTEMSのいずれにも未登録です"
                "（新規系統追加時はどちらかに追記してください）"
            )

        row = SystemRow(
            kpi_name=kpi_name,
            status=entry["status"],
            registered_date=registered_date,
            origin_date=origin_date,
            n_insample=n_insample,
            freq_monthly=freq_monthly,
            freq_provenance=freq_provenance,
            n100_date=dates["n100_date"],
            full12_date=FULL_12MONTH_DATE,
            maturity_date=dates["maturity_date"],
            alpha=alpha,
            alpha_provenance=alpha_provenance,
            endpoint_col=endpoint_col,
            endpoint_fallback=endpoint_fallback,
            sigma_trade=sigma_trade,
            sigma_month=sigma_month,
            sigma_provenance=sigma_provenance,
            n_eff_optimistic=dates["n_eff_optimistic"],
            n_eff_conservative=dates["n_eff_conservative"],
        )
        for ev in EV_SCENARIOS:
            row.power[(ev, "optimistic")] = compute_power(
                ev, sigma_trade, dates["n_eff_optimistic"], alpha
            )
            row.power[(ev, "conservative")] = compute_power(
                ev, sigma_month, dates["n_eff_conservative"], alpha
            )
            if alpha is None:
                row.ref_power[(ev, "optimistic")] = compute_power(
                    ev, sigma_trade, dates["n_eff_optimistic"], REFERENCE_ALPHA
                )
                row.ref_power[(ev, "conservative")] = compute_power(
                    ev, sigma_month, dates["n_eff_conservative"], REFERENCE_ALPHA
                )
        rows.append(row)

    rows.sort(key=lambda r: r.maturity_date)
    return rows


def write_csv(rows: list[SystemRow], path: Path) -> None:
    """maturity.csv を書き出す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "kpi_name",
        "status",
        "registered_date",
        "n_insample",
        "freq_monthly",
        "freq_provenance",
        "n100_predicted_date",
        "full12month_date",
        "maturity_date",
        "alpha_one_sided",
        "alpha_provenance",
        "endpoint_col",
        "endpoint_fallback_to_ret_stop8",
        "sigma_trade",
        "sigma_month",
        "n_eff_optimistic",
        "n_eff_conservative",
    ]
    for ev in EV_SCENARIOS:
        for variant in ("optimistic", "conservative"):
            fieldnames.append(f"power_ev{ev * 100:g}pct_{variant}")
    for ev in EV_SCENARIOS:
        for variant in ("optimistic", "conservative"):
            fieldnames.append(f"ref_alpha0.025_power_ev{ev * 100:g}pct_{variant}")

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            row_dict = {
                "kpi_name": r.kpi_name,
                "status": r.status,
                "registered_date": r.registered_date,
                "n_insample": r.n_insample,
                "freq_monthly": round(r.freq_monthly, 3),
                "freq_provenance": r.freq_provenance,
                "n100_predicted_date": r.n100_date.isoformat(),
                "full12month_date": r.full12_date.isoformat(),
                "maturity_date": r.maturity_date.isoformat(),
                "alpha_one_sided": round(r.alpha, 8) if r.alpha is not None else "N/A",
                "alpha_provenance": r.alpha_provenance,
                "endpoint_col": r.endpoint_col if r.endpoint_col else "N/A",
                "endpoint_fallback_to_ret_stop8": r.endpoint_fallback,
                "sigma_trade": round(r.sigma_trade, 6) if r.sigma_trade is not None else "N/A",
                "sigma_month": round(r.sigma_month, 6) if r.sigma_month is not None else "N/A",
                "n_eff_optimistic": r.n_eff_optimistic,
                "n_eff_conservative": r.n_eff_conservative,
            }
            for ev in EV_SCENARIOS:
                for variant in ("optimistic", "conservative"):
                    v = r.power[(ev, variant)]
                    row_dict[f"power_ev{ev * 100:g}pct_{variant}"] = (
                        round(v, 4) if v is not None else "N/A"
                    )
            for ev in EV_SCENARIOS:
                for variant in ("optimistic", "conservative"):
                    rv = r.ref_power.get((ev, variant))
                    row_dict[f"ref_alpha0.025_power_ev{ev * 100:g}pct_{variant}"] = (
                        round(rv, 4) if rv is not None else "N/A"
                    )
            writer.writerow(row_dict)


def _fmt_power(v: Optional[float]) -> str:
    return f"{v * 100:.1f}%" if v is not None else "N/A"


def write_report_md(
    rows: list[SystemRow], excluded_entries: list[dict], citation_warnings: list[str], path: Path
) -> None:
    """report.md を書き出す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# 前向き検証中/観察中 全系統 成熟予定日・検出力レポート")
    lines.append("")
    lines.append(
        "**経営計画/運用監視用。合否判定には一切使用しない。**"
        "（正式な判定は各系統の凍結ルール[§7-J/Q/T/W/Y/AB等]どおり、"
        "前向きデータでの片側p値検定のみが行う）"
    )
    lines.append("")
    lines.append(f"生成元: `scripts/kpi_maturity_power.py` / 対象: status=`{ACTIVE_STATUS}` の全系統")
    lines.append(f"対象系統数: {len(rows)}件 / 除外系統数: {len(excluded_entries)}件")
    lines.append("")

    lines.append("## 除外系統（休眠/棄却系status）")
    lines.append("")
    lines.append("| kpi_name | status | 除外理由 |")
    lines.append("|---|---|---|")
    for e in excluded_entries:
        reason = EXCLUDED_STATUS_REASONS.get(e["status"], "（理由未定義）")
        lines.append(f"| {e['kpi_name']} | {e['status']} | {reason} |")
    lines.append("")

    lines.append("## 1. 頻度・成熟予定日 一覧（概算最早判定日の早い順）")
    lines.append("")
    lines.append(
        "| kpi_name | registered_date | 月間頻度(件/月) | n(in-sample) | "
        "n≥100到達予測日 | 12完全暦月到達日 | 概算最早判定日† | 片側α |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        alpha_str = f"{r.alpha:.6f}" if r.alpha is not None else "N/A"
        lines.append(
            f"| {r.kpi_name} | {r.registered_date} | {r.freq_monthly:.2f} | "
            f"{r.n_insample if r.n_insample is not None else 'N/A'} | "
            f"{r.n100_date.isoformat()} | {r.full12_date.isoformat()} | "
            f"{r.maturity_date.isoformat()} | {alpha_str} |"
        )
    lines.append("")
    lines.append(
        "† bull/bear両レジームカバー要件あり: 正式判定はn≥100または12完全暦月の遅い方に加え、"
        "前向き観測期間中に強気(bull)・弱気(bear)の両レジームを最低1回ずつ含むことが必要"
        "（catalog L1112-1113等・§7-J/Q/T/W/Y/AB共通の凍結ルール）。本表の日付は頻度外挿のみで"
        "レジーム条件を判定しておらず、実際の判定可能日はレジーム構成次第でさらに後ろ倒しになりうる"
        "（列名を「正式判定可能日」ではなく「概算最早判定日」としているのはこのため）。"
    )
    lines.append("")

    lines.append("## 2. 頻度・αの出典")
    lines.append("")
    lines.append("| kpi_name | 頻度の出典 | αの出典 |")
    lines.append("|---|---|---|")
    for r in rows:
        lines.append(f"| {r.kpi_name} | {r.freq_provenance} | {r.alpha_provenance} |")
    lines.append("")

    lines.append("## 3. 検出力感度表（真EV別・n_eff楽観/保守別）")
    lines.append("")
    lines.append(
        "概算最早判定日時点での検出力 power = Φ( EV/(σ/√n_eff) − z_(1-α) )。"
        "σ_trade（楽観列用）はendpoint列（ret優先・無ければret_stop8にフォールバック）の"
        "取引レベル標本標準偏差、σ_month（保守列用）は月次平均endpoint値系列の標本標準偏差"
        "（in-sample期間内・in_universe==True行。n_effの次元とσの次元を一致させるため2種を"
        "使い分ける）。returns.csv不在の2系統（volshock_x_above200_quiet・sue_x_above200）は"
        "σ算出不可のためN/A。αがN/Aの4系統（volshock_5x/volshock_x_above200/"
        "volshock_x_above200_quiet/shortcover_x_bear）はpowerもN/A（参考感度は§4）。"
    )
    lines.append("")
    header = (
        "| kpi_name | 片側α | σ_trade(楽観列用) | σ_month(保守列用) | n_eff楽観 | n_eff保守 | "
        "power(EV+0.5%)楽観 | power(EV+0.5%)保守 | "
        "power(EV+1%)楽観 | power(EV+1%)保守 | "
        "power(EV+2%)楽観 | power(EV+2%)保守 |"
    )
    lines.append(header)
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        alpha_str = f"{r.alpha:.6f}" if r.alpha is not None else "N/A"
        sigma_trade_str = f"{r.sigma_trade * 100:.2f}%" if r.sigma_trade is not None else "N/A"
        sigma_month_str = f"{r.sigma_month * 100:.2f}%" if r.sigma_month is not None else "N/A"
        cells = [
            _fmt_power(r.power[(ev, variant)])
            for ev in EV_SCENARIOS
            for variant in ("optimistic", "conservative")
        ]
        lines.append(
            f"| {r.kpi_name} | {alpha_str} | {sigma_trade_str} | {sigma_month_str} | "
            f"{r.n_eff_optimistic} | {r.n_eff_conservative} | " + " | ".join(cells) + " |"
        )
    lines.append("")

    lines.append("## 4. 参考: αが未定義の4系統に片側α=0.025を仮定した場合の感度")
    lines.append("")
    lines.append(
        "本表はα=N/Aとした4系統（陣別alpha-spending導入前登録・catalog上に個別のforward-test "
        "α明記なし）について、SUEペアの明示基準（catalog L1112-1118・95%CI下限>0⇔片側α=0.025相当）"
        "と【仮に】同水準と仮定した場合の参考感度である。正式なα確定ではなく、catalog側での"
        "個別基準追記を待つ暫定値。σ・n_effの算出方法は§3と同一。"
    )
    lines.append("")
    lines.append(header)
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r.kpi_name not in ALPHA_UNDEFINED_SYSTEMS:
            continue
        sigma_trade_str = f"{r.sigma_trade * 100:.2f}%" if r.sigma_trade is not None else "N/A"
        sigma_month_str = f"{r.sigma_month * 100:.2f}%" if r.sigma_month is not None else "N/A"
        cells = [
            _fmt_power(r.ref_power.get((ev, variant)))
            for ev in EV_SCENARIOS
            for variant in ("optimistic", "conservative")
        ]
        lines.append(
            f"| {r.kpi_name} | {REFERENCE_ALPHA:.6f}(仮定) | {sigma_trade_str} | "
            f"{sigma_month_str} | {r.n_eff_optimistic} | {r.n_eff_conservative} | "
            + " | ".join(cells)
            + " |"
        )
    lines.append("")

    lines.append("## 仮定・前提条件")
    lines.append("")
    lines.append(
        "1. **n≥100到達予測起点の系統別化**: n≥100到達予測日は各系統の `registered_date` "
        "（`config/paper_watchlist.json` 実読・2026-07-07〜2026-07-15の幅がある）を起点に"
        "頻度で外挿する。旧版は全系統一律 `ORIGIN_DATE=2026-07-07` を使用していたが、"
        "後発登録系統ほど楽観側に寄る（=実際にはこれより遅くなりうる）バイアスがあったため"
        "系統別の実際の起点に修正した（Codexレビュー指摘）。"
    )
    lines.append(
        "2. **12完全暦月到達日の固定**: 初の完全暦月=2026-08 → 12ヶ月目終了=2027-07-31 → "
        "判定可能=2027-08-01を全系統共通で使用（仕様固定・registered_dateの違いに関わらず不変）。"
    )
    lines.append(
        "3. **月⇔日変換**: 月数と暦日数の相互変換にはグレゴリオ暦の平均月長 "
        "365.2425/12 ≈ 30.4368日 を一貫して使用（AVG_DAYS_PER_MONTH）。"
    )
    lines.append(
        "4. **α定数表の適用範囲**: 陣別alpha-spending（catalog L1769-1774・L1972・L2069・L2218）"
        "は2026-07-13以降に登録された11系統（第1〜5陣）に明示定義がある。"
        "SUEペア（sue_x_above200/sue_beat）はcatalog L1112-1118の明示基準（95%CI下限>0）を"
        "片側α=0.025として本表で採用した。それより前に登録された残り4系統"
        "（volshock_5x/volshock_x_above200/volshock_x_above200_quiet/shortcover_x_bear）には"
        "個別のforward-test α明記がなく、SUEペアと同水準とみなせる明示的根拠がcatalog上に"
        "見当たらないため、本表ではα=N/A・power=N/Aとし、片側α=0.025を仮定した場合の参考感度は"
        "§4に分離して表示する（旧版はこの4系統にもSUEと同一のα=0.025を本表内で直接適用しており、"
        "根拠の異なる仮定値が正式値と同列に見える構成だった。Codexレビュー指摘で修正）。"
    )
    lines.append(
        "5. **endpoint列の優先順位**: σの算出列はret列（nostop・正式primary endpoint）を優先し、"
        "無い場合のみret_stop8にフォールバックする（フォールバック発生時は§2の出典欄に明示）。"
        "旧版はret_stop8を優先していたが、前向き判定の正式primary endpointはnostop（ret列）である"
        "ため優先順位を修正した（Codexレビュー指摘）。"
    )
    lines.append(
        "6. **σの次元整合（楽観/保守で異なるσを使用）**: 検出力の正規近似は各シグナルのリターンが"
        "独立同分布であることを仮定するが、実際には同一銘柄の重複イベントや同時期のマクロ要因により"
        "系統内リターンは正の自己相関を持ちうる（月次ブロック・ブートストラップが catalog内で"
        "標準的に採用されている理由）。楽観列（n_eff=予測シグナル数）は取引レベルのσ_trade"
        "（独立性を仮定した上限側シナリオ）を、保守列（n_eff=経過月数=月次ブロック相当）は"
        "月次平均リターン系列のσ_month（相関を月次集計で粗く補正した下限側シナリオ）を用いる"
        "ことで、n_effとσの次元を一致させている（旧版は保守列にもσ_trade=取引レベルσをそのまま"
        "使っており、n_eff=月数に対してσ=1回あたり標準偏差という次元不整合があった。"
        "Codexレビュー指摘で修正）。実際の検出力はこの2値の間、あるいはそれ以下に位置する"
        "可能性がある。"
    )
    lines.append(
        "7. **σの算出期間**: in-sample期間（各系統の`in_sample.period`、多くは"
        "2016-11〜2022-11）のendpoint列（ret優先・無ければret_stop8）から算出した"
        "σ_trade（取引レベル）・σ_month（月次平均系列）を、前向き期間のσの代理値として"
        "使用する（前向き期間のσが同水準である保証はない）。"
    )
    lines.append(
        "8. **returns.csv不在の2系統**: volshock_x_above200_quiet・sue_x_above200は"
        "output/kpi/配下にreturns.csvが存在せず、σが算出できないため検出力はN/A。"
        "頻度のみcatalog記載値で代替した（volshock_x_above200_quietはαも未定義のため"
        "§4参考表でもN/A。sue_x_above200はαは定義済み(0.025)だがσ不在によりpowerはN/A）。"
    )
    lines.append("")

    lines.append("## 出典（ファイル・行番号）")
    lines.append("")
    lines.append(f"- `{WATCHLIST_PATH}`: status語彙・in_sample.period・in_sample.n・registered_date")
    lines.append(f"- `output/kpi/<kpi>/returns.csv`: in_universe==True行の月間頻度・σ_trade・σ_month（15系統）")
    lines.append(
        f"- `{CATALOG_PATH}` L1769-1774: 陣別alpha-spending総則（陣bの予算=0.05/2^b）"
    )
    lines.append(f"- `{CATALOG_PATH}` L1772/L1773/L1972/L2069/L2218: 陣別α確定値")
    lines.append(f"- `{CATALOG_PATH}` L1112-1118: SUEペアの前向き正式判定基準（95%CI下限>0）")
    lines.append(f"- `{CATALOG_PATH}` L458: volshock_x_above200_quietの頻度「月約1件」")
    lines.append(f"- `{CATALOG_PATH}` L1064: sue_x_above200の頻度「月平均約8.1件」")
    if citation_warnings:
        lines.append("")
        lines.append("### 引用文言の実行時検証で見つからなかった項目（要目視確認）")
        for w in citation_warnings:
            lines.append(f"- {w}")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    citation_warnings = verify_catalog_citations()
    for w in citation_warnings:
        print(f"[WARN] {w}")

    active_entries, excluded_entries = load_watchlist()
    print(
        f"対象(status={ACTIVE_STATUS})={len(active_entries)}件 / "
        f"除外={len(excluded_entries)}件: "
        + ", ".join(f"{e['kpi_name']}({e['status']})" for e in excluded_entries)
    )

    rows = build_rows(active_entries)

    write_csv(rows, REPORT_CSV_PATH)
    write_report_md(rows, excluded_entries, citation_warnings, REPORT_MD_PATH)

    print(f"書き出し完了: {REPORT_CSV_PATH}（{len(rows)}行+header）")
    print(f"書き出し完了: {REPORT_MD_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
