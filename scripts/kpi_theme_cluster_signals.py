#!/usr/bin/env python3
"""テーマ初動 v0（theme_cluster_v0）の生成器（tasks/theme_cluster_v0.md・P-INF-28 案A）。

`configs/x_shortage_map.json` の subject ごとに、受益銘柄（tier≠rejected・code あり・3 社以上の
subject のみ）のうち **同日の対 TOPIX 超過が +3.0pt 以上** の銘柄が「3 社以上 かつ 受益社数の 30% 以上」
出た日を「テーマ初動日」とし、その日を signal_date として **受益銘柄全部** を行にする
（翌営業日始値でのエントリーは daily_screen の既存枠組み defer_entry=true が担う）。

定義は `_tmp_ab_backtest2.py`（疑似前向き 2026-01〜10-01: +20 日 +2.82pt・勝率 48/83）をそのまま移植:
- 日次超過 = (AdjC/前日 AdjC − 1)×100 − (TOPIX C/前日 C − 1)×100 ≥ EXCESS_PT_MIN
- 分割日（AdjFactor≠1）の銘柄はその日の判定（分子）から外す（分母= 受益社数は変えない）
- 同一 subject の再発火は直近発火から REFIRE_SUPPRESS_BDAYS 営業日以内なら出さない
  （走査開始前の発火も見られるよう、連鎖の起点 REFIRE_CHAIN_ANCHOR_BD から内部で走査する）
- bars・TOPIX が欠ける日は判定しない（黙って 0 にせず diag に件数を残す）

FROZEN_START（2026-10-05）より前の signal_date は台帳に入れない。この生成器自体は過去日も再現できる
（成功基準の 2026-09-18 dram 8/12・2026-10-01 dram 11/12 の検証用）ので、凍結の適用は
呼び出し側 daily_screen の分岐で行う（本モジュールの定数を唯一の正本として参照する）。

検証（Docker）:
    docker compose run --rm xstock python -c "
import sys; sys.path.insert(0,'/app/scripts')
import measure_base_rate as mbr, kpi_theme_cluster_signals as t
bd=mbr.all_business_days(mbr.load_calendar_days()); bi={d:i for i,d in enumerate(bd)}
df,diag=t.generate_theme_cluster_signals('20260918','20261001',bd,bi)
print(df[df.subject=='dram'].drop_duplicates(['signal_date','subject'])); print(diag)"
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jq_fetch  # noqa: E402  (Canonical Module: DATA_ROOT / read_json_gz を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: load_bars_day を再利用)
import x_shortage_map  # noqa: E402  (Canonical Module: subject/受益カードの正本ローダを再利用)

KPI_NAME = "theme_cluster_v0"
FROZEN_START = "20261005"       # 実装翌営業日。これより前の signal_date は台帳に入れない（v0 凍結）
EXCESS_PT_MIN = 3.0             # 日次対 TOPIX 超過の閾値（pt）
MIN_HITS = 3                    # 超過銘柄の最小社数
MIN_HIT_RATIO = 0.30            # 超過銘柄 / 受益社数 の最小比率
REFIRE_SUPPRESS_BDAYS = 5       # 同一 subject の再発火抑止（営業日）
MIN_BENEFICIARIES = 3           # 母集団に入る subject の最小受益社数
# 再発火抑止の連鎖（発火→5 営業日抑止→次の発火→…）は「どこから数え始めたか」で結果が変わる。
# start_bd の 5 営業日前からだけ走査すると、本来は抑止されていたはずの日が「直近発火」になって当日を
# 落とす（実測 2026-10-04: 10-01 単日走査で dram/nand-ssd/cpu-logic の初動が 0 件になった）。
# 連鎖の起点を疑似前向きと同じ 2026 年初（_tmp_ab_backtest2.py の「2026-01 以降」）に固定し、
# どの呼び出し側（daily_screen・build_daily_reco）でも同じ発火列になるようにする（全走査でも約 3 秒）。
REFIRE_CHAIN_ANCHOR_BD = "20260105"

SIGNAL_COLUMNS = ["signal_date", "code", "subject", "n_hits", "n_ben"]


def load_topix_close() -> dict[str, float]:
    """TOPIX 終値を YYYYMMDD -> C で返す（jq_fetch.DATA_ROOT/topix.json.gz・C 欠損行は除く）。"""
    obj = jq_fetch.read_json_gz(jq_fetch.DATA_ROOT / "topix.json.gz")
    return {
        r["Date"].replace("-", ""): float(r["C"])
        for r in obj["data"]
        if r.get("Date") and r.get("C")
    }


def eligible_subjects(m: dict) -> list[tuple[dict, list[str]]]:
    """母集団= 受益銘柄（tier≠rejected・code あり）が MIN_BENEFICIARIES 社以上の subject。

    Returns:
        [(subject, [5桁コード...])]。5 桁化= TOP1000 台帳の 4 桁 code + "0"（bars の Code と同じ）。
        同一 subject 内の重複コードは 1 つに畳む（分母の二重計上を防ぐ）。
    """
    out: list[tuple[dict, list[str]]] = []
    for s in m["subjects"]:
        codes: list[str] = []
        for c in s.get("beneficiaries", []):
            if c.get("tier") == "rejected" or not c.get("code"):
                continue
            k = str(c["code"]) + "0"
            if k not in codes:
                codes.append(k)
        if len(codes) >= MIN_BENEFICIARIES:
            out.append((s, codes))
    return out


def _bars_or_none(d: str, cache: dict[str, Optional[dict]]) -> Optional[dict]:
    """bars が無い日は None（load_bars_day は SystemExit するので存在確認を先に行う）。

    ファイルはあるが中身が空（`data: []`・取得失敗の空キャッシュ）も欠落として None に正規化する
    （空 dict のままだと「判定した・0件」に化けて黙って 0 になる・Codex 2026-10-04 指摘）。
    """
    if d not in cache:
        path = jq_fetch.DATA_ROOT / "bars" / f"{d}.json.gz"
        cache[d] = (measure_base_rate.load_bars_day(d) or None) if path.exists() else None
    return cache[d]


def generate_theme_cluster_signals(
    start_bd: str,
    end_bd: str,
    all_bdays: list[str],
    bday_index: dict[str, int],
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd] のテーマ初動日に受益銘柄全部を行として返す。

    Args:
        start_bd: 出力する signal_date の下限（YYYYMMDD）。内部走査は REFIRE_SUPPRESS_BDAYS 営業日前から。
        end_bd: 出力する signal_date の上限（YYYYMMDD・この日の bars/TOPIX まで要る）。
        all_bdays: 取引所暦の営業日リスト（昇順）。
        bday_index: 営業日 -> index。

    Returns:
        (df, diag)。df の columns = SIGNAL_COLUMNS（0 件でも列を持つ）。
        同一銘柄が複数 subject に属す場合は subject ごとに行が出る（台帳側は (kpi,code,signal_date) で 1 行）。
        diag: 母集団・走査日数・欠落で判定しなかった日数・発火数・走査開始前の発火数など。
    """
    m = x_shortage_map.load()
    topix = load_topix_close()
    subjects = eligible_subjects(m)
    idx_start = bday_index[start_bd]
    idx_end = bday_index[end_bd]
    # 走査は連鎖の起点（REFIRE_CHAIN_ANCHOR_BD・暦に無ければ暦の先頭）から。start_bd がそれより前なら
    # start_bd の REFIRE_SUPPRESS_BDAYS 営業日前から（i-1 を参照するので 1 以上）。
    anchor_idx = bday_index.get(REFIRE_CHAIN_ANCHOR_BD, 1)
    scan_from = anchor_idx if idx_start >= anchor_idx else max(1, idx_start - REFIRE_SUPPRESS_BDAYS)

    diag = {
        "subjects_eligible": len(subjects),
        "subjects_total": len(m["subjects"]),
        "business_days_scanned": 0,            # 判定できた日数（走査開始前の抑止用の日も含む）
        "days_skipped_topix_missing": 0,
        "days_skipped_bars_missing": 0,
        "split_day_exclusions": 0,             # AdjFactor≠1 で分子から外した銘柄×日
        "fires_total": 0,                      # 発火した subject×日（抑止後・走査開始前も含む）
        "fires_before_start": 0,               # start_bd より前の発火（抑止の参照にだけ使う）
        "fires_refire_suppressed": 0,          # 条件は満たしたが直近発火から 5 営業日以内で出さなかった数
        f"signals_{KPI_NAME}": 0,              # 出力行数（受益銘柄×発火）
    }
    bars_cache: dict[str, Optional[dict]] = {}
    judged_days: list[tuple[int, str, str, float]] = []  # (i, d, p, topix_ret)
    for i in range(scan_from, idx_end + 1):
        d, p = all_bdays[i], all_bdays[i - 1]
        if d not in topix or p not in topix:
            diag["days_skipped_topix_missing"] += 1
            continue
        if _bars_or_none(d, bars_cache) is None or _bars_or_none(p, bars_cache) is None:
            diag["days_skipped_bars_missing"] += 1
            continue
        judged_days.append((i, d, p, (topix[d] / topix[p] - 1) * 100))
    diag["business_days_scanned"] = len(judged_days)

    rows: list[dict] = []
    for s, codes in subjects:
        last_fire: Optional[int] = None
        for i, d, p, topix_ret in judged_days:
            bars_d, bars_p = bars_cache[d], bars_cache[p]
            hits = 0
            for k in codes:
                b, bp = bars_d.get(k), bars_p.get(k)
                if not b or not bp or not b.get("AdjC") or not bp.get("AdjC"):
                    continue
                if float(b.get("AdjFactor") or 1) != 1:
                    diag["split_day_exclusions"] += 1
                    continue
                if (b["AdjC"] / bp["AdjC"] - 1) * 100 - topix_ret >= EXCESS_PT_MIN:
                    hits += 1
            if hits < MIN_HITS or hits < MIN_HIT_RATIO * len(codes):
                continue
            if last_fire is not None and i - last_fire <= REFIRE_SUPPRESS_BDAYS:
                diag["fires_refire_suppressed"] += 1
                continue
            last_fire = i
            diag["fires_total"] += 1
            if i < idx_start:
                diag["fires_before_start"] += 1
                continue
            for k in codes:
                rows.append({"signal_date": d, "code": k, "subject": s["id"], "n_hits": hits, "n_ben": len(codes)})

    diag[f"signals_{KPI_NAME}"] = len(rows)
    df = pd.DataFrame(rows, columns=SIGNAL_COLUMNS)
    return df, diag


if __name__ == "__main__":
    bd = measure_base_rate.all_business_days(measure_base_rate.load_calendar_days())
    bi = {d: i for i, d in enumerate(bd)}
    args = sys.argv[1:]
    s, e = (args + [bd[-1], bd[-1]])[:2] if args else (bd[-1], bd[-1])
    df_, diag_ = generate_theme_cluster_signals(s, e, bd, bi)
    print(df_.drop_duplicates(["signal_date", "subject"])[["signal_date", "subject", "n_hits", "n_ben"]].to_string(index=False))
    print(diag_)
