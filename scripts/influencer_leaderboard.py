#!/usr/bin/env python3
"""インフルエンサー別・中間勝率リーダーボード（記述的中間測定）。

**位置づけ（厳守）**: これは記述的な中間測定であり、`data/kpi_trials/*` の正式台帳には
不算入。§2-G（凍結リスト・前向き）の10月正式判定を変更・代替するものではない。
このスクリプトの結果で売買判断はしないこと。

目的: 「勝率が高いインフルエンサーの買い方をもっと調べて」に対する実測の第一歩として、
複数のデータ源（収集方法・バイアス特性が異なる）から (アカウント, 言及日, 銘柄コード) の
三つ組みを抽出し、`scripts/measure_base_rate.py` の Canonical なフォワードリターン計算
（T+1営業日始値エントリー・20営業日後終値イグジット・往復コスト0.3%控除・-8%/-10%損切り
シミュレーション）をそのまま再利用してアカウント別に集計する。

## データ源スキーマの実読結果（設計との差分・不明点は推測せず記録）

指示された4データ源のうち、`output/research/discovery_*.json` は実読の結果
(username/display_name/evidence/sample_posts/investment_focus/score/confidence) の
**インフルエンサー"候補"アカウントのメタデータのみ**であり、個々の投稿の (言及日, 銘柄コード)
は一切含まれない（sample_posts は要約テキストで銘柄コードの構造化フィールドなし）。
同じ `output/research/` ディレクトリ・同じ2026-03〜04 Grokパイプラインの後段成果物である
`output/research/signals.jsonl`（694件、username/ticker/posted_at/direction が構造化済み）
が (account, mention_date, code) を実際に含む唯一のファイルだったため、near_realtime
ソースの実データとして signals.jsonl を採用した（discovery_*.json 自体は0件・理由を
extraction_summary に明記する）。

`output/research/_frozen34_candidates.json` も同様に username+score のみのロースターで
言及データを含まない（0件・理由明記）。`output/research/candidates_masters_20260717.json`
も5名人アカウントのロースターのみ（実際の投稿は data/masters_harvest_20260717/candidates_slim.json
側にあり、そちらを retrospective ソースの実データとして使用）。

## 銘柄コード抽出規則（会社名の曖昧マッチはしない）

- `output/research/signals.jsonl`: 既に構造化済みの `ticker`（例 "7203.T"）を
  `scripts/winrate_score.ticker_to_jquants_code`（Canonical・日本株4桁+.T形式のみ5桁Code化、
  非対応ティッカーはNone）でそのまま変換。direction=="SHORT" は「買い方」測定の趣旨に
  合わないため除外（件数報告）。
- `output/merged_all.json` / `data/masters_harvest_20260717/candidates_slim.json`:
  本文中の **括弧付き銘柄コード**（例「任天堂（7974）」「ミーク(332A)」）のみを
  `CODE_PAREN_RE` で抽出する（会社名辞書によるファジーマッチは一切行わない）。
  括弧なしの裸の4桁数字は年号・数量等との誤判定リスクが高いため対象外（除外理由に含めず、
  そもそも「コードなし」として扱う）。

## フォワードリターン計算

signal_date（T）= 言及日が営業日ならその日、非営業日なら翌営業日（`bisect_left` で解決。
`scripts/winrate_score.evaluate_signal` と同じ規約）。その T を
`measure_base_rate.compute_forward_return_for_code` にそのまま渡す
（内部で T+1営業日始値エントリー・20営業日後終値イグジットを計算）。

データ終端（bars キャッシュの最新日）を超えて20営業日目の株価が必要なレコードは
censored として除外する（`measure_base_rate` の FATAL を避けるため、呼び出し前に
判定する）。

Usage:
    python3 scripts/influencer_leaderboard.py
"""
from __future__ import annotations

import bisect
import gzip
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import jq_fetch  # noqa: E402  (Canonical Module: DATA_ROOT)
import measure_base_rate  # noqa: E402  (Canonical Module: calendar/bars読み込み・フォワードリターン計算・summarize)
import winrate_score  # noqa: E402  (Canonical Module: ticker_to_jquants_code)

OUTPUT_DIR = PROJECT_ROOT / "output" / "influencer_leaderboard"
SIGNALS_PATH = PROJECT_ROOT / "output" / "research" / "signals.jsonl"
MERGED_ALL_PATH = PROJECT_ROOT / "output" / "merged_all.json"
MASTERS_CANDIDATES_PATH = PROJECT_ROOT / "data" / "masters_harvest_20260717" / "candidates_slim.json"
MASTERS_ROSTER_PATH = PROJECT_ROOT / "output" / "research" / "candidates_masters_20260717.json"
FROZEN34_PATH = PROJECT_ROOT / "output" / "research" / "_frozen34_candidates.json"
DISCOVERY_GLOB = "discovery_*.json"
UNIVERSE_W21_PATH = PROJECT_ROOT / "output" / "base_rate" / "universes_w21.csv.gz"

MIN_N_FOR_TABLE = 5
CAUTION_N = 20  # これ未満は「偶然の可能性大」の注記対象

CODE_PAREN_RE = re.compile(r"[（(]\s*([0-9][0-9A-Za-z]{3})\s*[）)]")

DISCLAIMER = (
    "> **中間・記述的測定。データ源ごとにバイアスが異なる（下記ラベル）。"
    "正式判定は§2-G（凍結リスト・前向き）が10月に行う。この結果で売買判断はしない。**"
)


# --- 銘柄コード抽出 -------------------------------------------------------------


def extract_paren_codes(text: str) -> list[str]:
    """本文中の括弧付き銘柄コードを抽出し、J-Quants bars の5桁Code形式で重複排除して返す。"""
    if not text:
        return []
    codes = []
    seen = set()
    for m in CODE_PAREN_RE.finditer(text):
        raw = m.group(1).upper()
        code5 = raw + "0"
        if code5 not in seen:
            seen.add(code5)
            codes.append(code5)
    return codes


# --- ソース別ローダー（各: (account_lower, account_display, mention_date, code, source_bias, note)を返す） ---


def load_source_near_realtime() -> tuple[list[dict], dict]:
    """output/research/signals.jsonl から (account, mention_date, code) を抽出する（label=near_realtime）。

    discovery_*.json 自体には(account,date,code)が無いため使用しない（モジュール docstring 参照）。
    """
    diag = {"raw_records": 0, "short_excluded": 0, "unresolvable_ticker": 0, "extracted": 0}
    rows = []
    if not SIGNALS_PATH.exists():
        return rows, diag
    with open(SIGNALS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            sig = json.loads(line)
            diag["raw_records"] += 1
            posted_at = sig.get("posted_at", "")
            mention_date = posted_at[:10] if posted_at else ""
            username = sig.get("username", "")
            if not mention_date or not username:
                continue
            if sig.get("direction") == "SHORT":
                diag["short_excluded"] += 1
                continue
            code = winrate_score.ticker_to_jquants_code(sig.get("ticker", ""))
            if code is None:
                diag["unresolvable_ticker"] += 1
                continue
            rows.append({
                "account": username.lower(),
                "account_display": username,
                "mention_date": mention_date,
                "code": code,
                "source_bias": "near_realtime",
                "source_file": "output/research/signals.jsonl",
            })
            diag["extracted"] += 1
    return rows, diag


def load_source_realtime() -> tuple[list[dict], dict]:
    """output/merged_all.json（445ツイート）から括弧付きコードを抽出する（label=realtime）。"""
    diag = {"raw_records": 0, "no_code": 0, "extracted": 0}
    rows = []
    if not MERGED_ALL_PATH.exists():
        return rows, diag
    with open(MERGED_ALL_PATH, encoding="utf-8") as f:
        tweets = json.load(f)
    for t in tweets:
        diag["raw_records"] += 1
        posted_at = t.get("posted_at", "")
        mention_date = posted_at[:10] if posted_at else ""
        username = t.get("username", "")
        if not mention_date or not username:
            continue
        codes = extract_paren_codes(t.get("text", ""))
        if not codes:
            diag["no_code"] += 1
            continue
        for code in codes:
            rows.append({
                "account": username.lower(),
                "account_display": username,
                "mention_date": mention_date,
                "code": code,
                "source_bias": "realtime",
                "source_file": "output/merged_all.json",
            })
            diag["extracted"] += 1
    return rows, diag


def load_source_retrospective() -> tuple[list[dict], dict]:
    """data/masters_harvest_20260717/candidates_slim.json（名人5人・558件）から括弧付きコードを抽出する
    （label=retrospective・削除ツイート欠落バイアスあり）。

    output/research/candidates_masters_20260717.json は5名人のロースター（username一覧）のみで
    投稿本文を含まないため、実データ源としては使わず突合確認にのみ用いる。
    """
    diag = {"raw_records": 0, "no_code": 0, "extracted": 0, "roster_cross_check": None}
    rows = []
    if not MASTERS_CANDIDATES_PATH.exists():
        return rows, diag
    with open(MASTERS_CANDIDATES_PATH, encoding="utf-8") as f:
        recs = json.load(f)
    for r in recs:
        diag["raw_records"] += 1
        mention_date = r.get("date", "")
        username = r.get("user", "")
        if not mention_date or not username:
            continue
        text = " ".join(str(r.get(k, "")) for k in ("claim", "sketch", "text_head"))
        codes = extract_paren_codes(text)
        if not codes:
            diag["no_code"] += 1
            continue
        for code in codes:
            rows.append({
                "account": username.lower(),
                "account_display": username,
                "mention_date": mention_date,
                "code": code,
                "source_bias": "retrospective",
                "source_file": "data/masters_harvest_20260717/candidates_slim.json",
            })
            diag["extracted"] += 1

    if MASTERS_ROSTER_PATH.exists():
        with open(MASTERS_ROSTER_PATH, encoding="utf-8") as f:
            roster = json.load(f)
        roster_users = {c["username"] for c in roster.get("candidates", [])}
        harvest_users = {r["user"] for r in recs}
        diag["roster_cross_check"] = {
            "roster_usernames": sorted(roster_users),
            "harvest_usernames": sorted(harvest_users),
            "match": roster_users == harvest_users,
        }
    return rows, diag


def load_source_frozen34_and_discovery_notes() -> dict:
    """_frozen34_candidates.json と discovery_*.json はロースター/候補メタデータのみで
    (account,date,code) を含まないため、抽出0件の理由を記録する（サイレント除外禁止）。
    """
    notes = {}
    if FROZEN34_PATH.exists():
        with open(FROZEN34_PATH, encoding="utf-8") as f:
            d = json.load(f)
        notes["_frozen34_candidates.json"] = {
            "n_candidates": len(d.get("candidates", [])),
            "reason": "username+scoreのみのアカウントロースター。投稿本文・言及日を含まないため抽出0件。",
        }
    discovery_files = sorted((PROJECT_ROOT / "output" / "research").glob(DISCOVERY_GLOB))
    total_candidates = 0
    for fp in discovery_files:
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
        total_candidates += len(d.get("candidates", []))
    notes["discovery_*.json"] = {
        "n_files": len(discovery_files),
        "n_candidates_total": total_candidates,
        "reason": (
            "インフルエンサー候補アカウントの発見メタデータ(username/evidence/sample_posts等)のみで、"
            "個々の投稿の(言及日,銘柄コード)を含まない。実データは同一パイプライン後段の"
            "output/research/signals.jsonl を near_realtime ソースとして採用した。"
        ),
    }
    return notes


# --- 銘柄コード → J-Quants日付・カレンダー解決 ------------------------------------


def resolve_signal_bday(mention_date: str, all_bdays: list[str]) -> Optional[str]:
    """言及日を営業日カレンダーに解決する（営業日ならその日、非営業日なら翌営業日）。

    scripts/winrate_score.evaluate_signal と同じ bisect_left 規約。
    """
    compact = mention_date.replace("-", "")
    idx = bisect.bisect_left(all_bdays, compact)
    if idx >= len(all_bdays):
        return None
    return all_bdays[idx]


def find_latest_bars_date() -> str:
    """bars キャッシュに実在する最新日付（YYYYMMDD）を返す（censored 判定のデータ終端）。"""
    bars_dir = jq_fetch.DATA_ROOT / "bars"
    dates = sorted(p.stem.replace(".json", "") for p in bars_dir.glob("*.json.gz"))
    if not dates:
        raise SystemExit(f"FATAL: bars キャッシュが見つかりません: {bars_dir}")
    return dates[-1]


def prev_year_month(date_str: str) -> str:
    """"YYYY-MM-DD" の暦月の1つ前の月を "YYYYMM" 形式で返す（TOP500ユニバース参照用）。

    月末営業日Tより前の完結済みユニバースだけを参照することで look-ahead を避ける
    （当月のユニバースは月末にならないと確定しないため）。
    """
    y, m = int(date_str[:4]), int(date_str[5:7])
    if m == 1:
        return f"{y - 1}12"
    return f"{y}{m - 1:02d}"


# --- 評価パイプライン ------------------------------------------------------------


def evaluate_rows(rows: list[dict], all_bdays: list[str], bday_index: dict, data_end: str,
                   universe_by_month: dict) -> tuple[list[dict], Counter]:
    """抽出済み(account,mention_date,code)行を measure_base_rate の Canonical 関数で評価する。

    Returns:
        (kept_rows, exclusion_counter)
    """
    kept = []
    excl = Counter()

    for row in rows:
        t_date = resolve_signal_bday(row["mention_date"], all_bdays)
        if t_date is None:
            excl["calendar_out_of_range"] += 1
            continue

        t_idx = bday_index[t_date]
        entry_idx = t_idx + 1
        exit_idx = entry_idx + measure_base_rate.FORWARD_WINDOW_BD
        if exit_idx >= len(all_bdays):
            excl["calendar_out_of_range"] += 1
            continue
        exit_target_day = all_bdays[exit_idx]
        if exit_target_day > data_end:
            excl["censored"] += 1
            continue

        result = measure_base_rate.compute_forward_return_for_code(row["code"], t_date, bday_index, all_bdays)
        if result is None:
            excl["entry_missing"] += 1
            continue

        pym = prev_year_month(row["mention_date"])
        universe_codes = universe_by_month.get(pym)
        if universe_codes is None:
            in_universe = None  # 対象月データなし（判定不可）
        else:
            in_universe = row["code"] in universe_codes

        merged = dict(row)
        merged.update(result)
        merged["signal_date"] = t_date
        merged["universe_month_ref"] = pym
        merged["in_universe_top500"] = in_universe
        kept.append(merged)

    return kept, excl


# --- 集計・出力 ------------------------------------------------------------------


def _fmt_pct(x) -> str:
    return f"{x:.1%}" if x is not None and not pd.isna(x) else "-"


def _fmt_ret(x) -> str:
    return f"{x:+.2%}" if x is not None and not pd.isna(x) else "-"


def build_account_table(df: pd.DataFrame) -> list[dict]:
    """アカウント別に summarize() を適用し、n>=MIN_N_FOR_TABLE のみ行として返す。"""
    table_rows = []
    for account, g in df.groupby("account"):
        stats = measure_base_rate.summarize(g)
        if stats["n"] < MIN_N_FOR_TABLE:
            continue
        bias_counts = g["source_bias"].value_counts().to_dict()
        n_univ_known = g["in_universe_top500"].notna().sum()
        n_univ_in = (g["in_universe_top500"] == True).sum()  # noqa: E712
        table_rows.append({
            "account": account,
            "account_display": g["account_display"].iloc[0],
            "n": stats["n"],
            "p20": stats["p20"],
            "p30": stats["p30"],
            "ev_none": stats["ev_none"],
            "ev_stop8": stats["ev_stop8"],
            "mean_ret": stats["mean_ret"],
            "median_ret": stats["median_ret"],
            "period_min": g["mention_date"].min(),
            "period_max": g["mention_date"].max(),
            "n_near_realtime": bias_counts.get("near_realtime", 0),
            "n_realtime": bias_counts.get("realtime", 0),
            "n_retrospective": bias_counts.get("retrospective", 0),
            "top500_pct": (n_univ_in / n_univ_known) if n_univ_known else None,
        })
    table_rows.sort(key=lambda r: (-(r["p20"] if r["p20"] is not None else -1), -r["n"]))
    return table_rows


def build_insufficient_list(all_extracted_by_account: dict, df: pd.DataFrame) -> list[dict]:
    """n<MIN_N_FOR_TABLE のアカウントを名前+件数のみで返す（評価済みn / 抽出済み生n の両方）。"""
    evaluated_n = df.groupby("account").size().to_dict()
    out = []
    for account, raw_n in all_extracted_by_account.items():
        n_eval = evaluated_n.get(account, 0)
        if n_eval >= MIN_N_FOR_TABLE:
            continue
        out.append({"account": account, "n_evaluated": n_eval, "n_extracted_raw": raw_n})
    out.sort(key=lambda r: -r["n_extracted_raw"])
    return out


def render_report(
    table_rows: list[dict],
    insufficient: list[dict],
    overall_stats: dict,
    universe_stats: dict,
    source_diags: dict,
    exclusion_counter: Counter,
    dedup_removed: int,
    total_raw_extracted: int,
    total_kept: int,
    data_end: str,
    no_code_total: int,
    diag_nr_unresolvable: int,
    diag_nr_short: int,
) -> str:
    lines = [
        "# インフルエンサー別・中間勝率リーダーボード",
        "",
        DISCLAIMER,
        "",
        f"生成基準: bars キャッシュ最新日 = {data_end}（この日までの20営業日後株価が揃うシグナルのみ評価対象）",
        "",
        "## データ源とバイアスラベル",
        "",
        "| ラベル | ファイル | 説明 | バイアス特性 |",
        "|---|---|---|---|",
        "| near_realtime | output/research/signals.jsonl | 2026-03〜04 Grokリサーチパイプライン抽出済みシグナル（694件・structured ticker） | 収集時点で発見できたアカウント・投稿に限られる（Grok検索の網羅性に依存） |",
        "| realtime | output/merged_all.json | Playwright収集445ツイート・24アカウント（本文から括弧付きコードのみ抽出） | ほぼリアルタイム収集だが括弧付き明記コードのみ抽出のため大半除外（下記参照） |",
        "| retrospective | data/masters_harvest_20260717/candidates_slim.json | 名人5アカウントの1年分をまとめて2026-07-17に遡及収集 | 削除ツイート欠落バイアス・成功した予測ほど残りやすい生存者バイアスの疑い |",
        "",
        "**方法論の注記（実データ読み取りに基づく調整）**:",
        "",
        "- `output/research/discovery_*.json` はインフルエンサー候補アカウントの発見メタデータのみで"
        "(言及日,銘柄コード)を含まないため、near_realtimeソースの実データとしては同一パイプライン後段の"
        "signals.jsonl を採用した（discovery自体は0件・下記抽出内訳に理由明記）。",
        "- `output/research/_frozen34_candidates.json` はusername+scoreのみのロースターで言及データなし（0件）。",
        "- `output/research/candidates_masters_20260717.json` は5名人のロースターのみ（突合確認にのみ使用、"
        "実データはcandidates_slim.json側）。",
        "- signals.jsonlはdirection(LONG/SHORT)を持つが、本測定は「言及銘柄が実際に上がったか」の記述的測定"
        "であり方向的中率ではないため、SHORT方向のシグナルは「買い方」測定の趣旨に合わず除外した"
        "（件数は下記）。realtime/retrospectiveソースはdirection情報がなく、抽出できた銘柄言及は"
        "無条件でロング方向の関心表明とみなしている（この非対称性はバイアスとして明記する）。",
        "- signal_date(T) = 言及日が営業日ならその日、非営業日なら翌営業日。"
        "measure_base_rate.compute_forward_return_for_code をそのまま再利用（T+1営業日始値エントリー・"
        "20営業日後終値イグジット・往復コスト0.3%控除・-8%/-10%損切りシミュレーション）。",
        "- TOP500判定は言及日が属する暦月の**前月**のuniverses_w21ユニバース（look-ahead回避）。"
        "ユニバースデータのカバー範囲外（2026-06以降の言及等）は「判定不可」として集計から除外。",
        "",
        "## 抽出・除外内訳（4データ源中2ソースは構造上0件・理由は下記ソース別内訳参照）",
        "",
        f"- 抽出できた言及総数（重複排除前・3ソース合計。discovery/_frozen34は0件）: {total_raw_extracted}件",
        f"- クロスソース重複排除（同一 account × code × mention_date）: {dedup_removed}件",
        f"- 評価完了（kept・下記リーダーボード/全体プールの母数）: {total_kept}件",
        "",
        "**除外内訳サマリ（コード無し / censored / universe外）**:",
        "",
        "| 区分 | 件数 | 説明 |",
        "|---|---|---|",
        f"| コード無し（regex不一致・realtime+retrospective） | {no_code_total} | "
        "括弧付き明記コードが本文中に見つからず抽出不能（会社名ファジーマッチはしない方針のため対象外）|",
        f"| 銘柄コード対象外（near_realtimeのticker構造化済みだがJ-Quants非対応=米国株等） | {diag_nr_unresolvable} | "
        "signals.jsonlのticker形式が日本株4桁+.T形式でない（米国株/ETF/指数/暗号資産等） |",
        f"| SHORT方向除外（near_realtimeのみ） | {diag_nr_short} | 「買い方」測定の趣旨に合わないため除外 |",
        f"| censored（bars最新日={data_end}までに20営業日後株価が揃わない） | {exclusion_counter.get('censored', 0)} | "
        "データ終端に近い言及（評価対象から除外） |",
        f"| entry_missing（エントリー日始値が取得不能・売買停止/コード不存在等） | {exclusion_counter.get('entry_missing', 0)} | "
        "compute_forward_return_for_codeがNoneを返したケース |",
        f"| calendar_out_of_range | {exclusion_counter.get('calendar_out_of_range', 0)} | カレンダー範囲外（通常発生しない想定） |",
        "",
        "**universe外（TOP500）**: 除外はしていない。評価は継続し、下記「TOP500 in/out」セクションに"
        "参考区分として分けて集計している（全体プール・アカウント別表にはTOP500内外を問わず全評価済みレコードを含む）。",
        "",
        "| 除外理由（内部ラベル） | 件数 |",
        "|---|---|",
    ]
    for reason, count in exclusion_counter.most_common():
        lines.append(f"| {reason} | {count} |")
    lines += ["", "### ソース別内訳", ""]
    for name, diag in source_diags.items():
        lines.append(f"**{name}**")
        for k, v in diag.items():
            if k == "roster_cross_check" and v is not None:
                lines.append(f"- roster_cross_check.match: {v['match']}")
            elif v is not None:
                lines.append(f"- {k}: {v}")
        lines.append("")

    lines += [
        "## アカウント別リーダーボード（P(+20%到達)降順）",
        "",
        f"**注意: n<{CAUTION_N} は偶然の可能性が大きい（統計的信頼性が低い）。n>={MIN_N_FOR_TABLE} のみ本表に掲載。**",
        "",
        "凡例: 近=near_realtime件数 / 即=realtime件数 / 遡=retrospective件数（アカウントごとの評価済みnの内訳）",
        "",
        "| アカウント | n | P(+20%到達) | P(+30%) | EV(コスト後) | EV(-8%損切り) | 平均ret | 中央値ret | "
        "近/即/遡 | TOP500内比率 | 期間 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    if not table_rows:
        lines.append("| (n>=5のアカウントなし) | - | - | - | - | - | - | - | - | - | - |")
    for r in table_rows:
        caution = "（注意）" if r["n"] < CAUTION_N else ""
        lines.append(
            f"| @{r['account_display']}{caution} | {r['n']} | {_fmt_pct(r['p20'])} | {_fmt_pct(r['p30'])} | "
            f"{_fmt_ret(r['ev_none'])} | {_fmt_ret(r['ev_stop8'])} | {_fmt_ret(r['mean_ret'])} | "
            f"{_fmt_ret(r['median_ret'])} | {r['n_near_realtime']}/{r['n_realtime']}/{r['n_retrospective']} | "
            f"{_fmt_pct(r['top500_pct'])} | {r['period_min']}〜{r['period_max']} |"
        )

    lines += ["", "## データ不足（n<5・名前と件数のみ）", "", "| アカウント | 評価済みn | 抽出済み生n |", "|---|---|---|"]
    if not insufficient:
        lines.append("| (該当なし) | - | - |")
    for r in insufficient:
        lines.append(f"| @{r['account']} | {r['n_evaluated']} | {r['n_extracted_raw']} |")

    lines += [
        "",
        "## 全体プール",
        "",
        f"| 区分 | n | P(+20%到達) | P(+30%) | EV(コスト後) | EV(-8%損切り) | 平均ret |",
        "|---|---|---|---|---|---|---|",
        f"| 全体 | {overall_stats['n']} | {_fmt_pct(overall_stats.get('p20'))} | "
        f"{_fmt_pct(overall_stats.get('p30'))} | {_fmt_ret(overall_stats.get('ev_none'))} | "
        f"{_fmt_ret(overall_stats.get('ev_stop8'))} | {_fmt_ret(overall_stats.get('mean_ret'))} |",
        "",
        "## TOP500 in/out（言及月の前月ユニバースで判定）",
        "",
        "| 区分 | n | P(+20%到達) | P(+30%) | EV(コスト後) | EV(-8%損切り) | 平均ret |",
        "|---|---|---|---|---|---|---|",
    ]
    for label, stats in universe_stats.items():
        lines.append(
            f"| {label} | {stats['n']} | {_fmt_pct(stats.get('p20'))} | {_fmt_pct(stats.get('p30'))} | "
            f"{_fmt_ret(stats.get('ev_none'))} | {_fmt_ret(stats.get('ev_stop8'))} | "
            f"{_fmt_ret(stats.get('mean_ret'))} |"
        )

    lines += [
        "",
        "## 限界・既知のバイアス（追加）",
        "",
        "- realtime(merged_all.json)ソースは括弧付き明記コードのみを対象にしたため抽出率が低く"
        "（445件中コード検出は少数）、当該アカウントの実際の発言頻度を大きく過小代表している可能性が高い。",
        "- near_realtime(signals.jsonl)とrealtime/retrospectiveの間で、同一アカウント・同時期"
        "（例: 2026年2月）の投稿が別々の収集経路で重複収集されている可能性がある。"
        "exact match（account×code×mention_date）の重複のみ機械的に排除しており、"
        "抽出コードが一部だけ異なる場合の重複は残存しうる。",
        "- retrospectiveソース（名人5アカウントの1年分）は削除済みツイートが欠落するため、"
        "外れた予測ほど削除されて残らない生存者バイアスの可能性がある（spec上も既知の論点）。",
        "- direction(LONG/SHORT)情報を持つのはnear_realtimeソースのみで、他2ソースは方向不明の"
        "「言及＝関心表明」として扱っている。的中率ではなく「言及銘柄の事後の値動き」の記述的測定である。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    rows_nr, diag_nr = load_source_near_realtime()
    rows_rt, diag_rt = load_source_realtime()
    rows_rs, diag_rs = load_source_retrospective()
    roster_notes = load_source_frozen34_and_discovery_notes()

    all_rows = rows_nr + rows_rt + rows_rs
    total_raw_extracted = len(all_rows)

    # クロスソース重複排除（account_lower, code, mention_date が完全一致するもの。先勝ち）
    seen_triples = set()
    deduped = []
    dedup_removed = 0
    for row in all_rows:
        key = (row["account"], row["code"], row["mention_date"])
        if key in seen_triples:
            dedup_removed += 1
            continue
        seen_triples.add(key)
        deduped.append(row)

    all_extracted_by_account: dict = Counter(r["account"] for r in deduped)

    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    data_end = find_latest_bars_date()

    universe_df = pd.read_csv(UNIVERSE_W21_PATH, dtype={"code": str, "month": str})
    universe_by_month = {m: set(g["code"]) for m, g in universe_df.groupby("month")}

    kept, exclusion_counter = evaluate_rows(deduped, all_bdays, bday_index, data_end, universe_by_month)
    total_kept = len(kept)

    df = pd.DataFrame(kept)

    if len(df) > 0:
        table_rows = build_account_table(df)
        overall_stats = measure_base_rate.summarize(df)
        universe_stats = {
            "TOP500内": measure_base_rate.summarize(df[df["in_universe_top500"] == True]),  # noqa: E712
            "TOP500外": measure_base_rate.summarize(df[df["in_universe_top500"] == False]),  # noqa: E712
            "判定不可（対象月データなし）": measure_base_rate.summarize(df[df["in_universe_top500"].isna()]),
        }
    else:
        table_rows = []
        overall_stats = {"n": 0}
        universe_stats = {
            "TOP500内": {"n": 0}, "TOP500外": {"n": 0}, "判定不可（対象月データなし）": {"n": 0},
        }

    insufficient = build_insufficient_list(all_extracted_by_account, df)

    source_diags = {
        "output/research/signals.jsonl (near_realtime)": diag_nr,
        "output/merged_all.json (realtime)": diag_rt,
        "data/masters_harvest_20260717/candidates_slim.json (retrospective)": diag_rs,
        **roster_notes,
    }

    no_code_total = diag_rt.get("no_code", 0) + diag_rs.get("no_code", 0)
    report_md = render_report(
        table_rows, insufficient, overall_stats, universe_stats, source_diags,
        exclusion_counter, dedup_removed, total_raw_extracted, total_kept, data_end,
        no_code_total, diag_nr.get("unresolvable_ticker", 0), diag_nr.get("short_excluded", 0),
    )

    report_path = OUTPUT_DIR / "report.md"
    report_path.write_text(report_md, encoding="utf-8")

    leaderboard_csv_path = OUTPUT_DIR / "leaderboard.csv"
    pd.DataFrame(table_rows).to_csv(leaderboard_csv_path, index=False)

    detail_csv_path = OUTPUT_DIR / "mentions_detail.csv.gz"
    detail_cols = [
        "account", "account_display", "source_bias", "code", "mention_date", "signal_date",
        "entry_date", "exit_date", "entry_price", "ret", "mfe", "mae", "delisted_flag",
        "ret_stop8", "ret_stop10", "in_universe_top500", "universe_month_ref",
    ]
    if len(df) > 0:
        df[detail_cols].to_csv(detail_csv_path, index=False, compression="gzip")
    else:
        pd.DataFrame(columns=detail_cols).to_csv(detail_csv_path, index=False, compression="gzip")

    print(f"抽出（重複排除前）: {total_raw_extracted}件 / 重複排除: {dedup_removed}件 / 評価完了: {total_kept}件")
    print(f"アカウント別表(n>=5): {len(table_rows)}件 / データ不足: {len(insufficient)}件")
    print(f"出力: {report_path}")
    print(f"出力: {leaderboard_csv_path}")
    print(f"出力: {detail_csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
