#!/usr/bin/env python3
"""§7-AE-v2 第40周改: TOB候補スコアv2（EDINET documents_all母集団・BL-1・v1単位バグ修正+レビュー指摘8項目の修復）。

docs/stock-algo-kpi-catalog.md の §7-AE-v2（❄️凍結 2026-07-18・Codex敵対レビューR4 GO
［NO-GO×3→GO］・凍結本文SHA-256=cb3dbc835188b10be962a48a21275bc461642424ee90fb13b15d0a4872cb3851・
コミット99eee9e）を機械適用する。決定③「発明再開は新独立データ到着時のみ」の初の正規適用
（EDINET documents_all = 109試行で未使用の新データ源）。**正式verdictを持たない参考観測(i)+
前向き観察(ii)の複合**であり、陣別alphaを消費しない（台帳には2行=v1invalidated+v2・
team lead側が監査後に追記する。本スクリプトは trials.jsonl に一切書き込まない）。

**v1無効化（2026-07-18・Codex実装レビュー裁定）**: PBR=(AdjC×ShOutFY)÷Eq は単位不整合
（AdjCは分割遡及調整済み・ShOutFYは生株数）。verdict=invalidated
（invalidation_reason=specification_unit_mismatch）として台帳記録済み。本ファイルはv2実装。

スコア凍結（単一式・グリッド探索なし・v1から変更なし部分はそのまま継承）:
    TOB候補スコアv2 = 2特徴の等ウェイト順位和（TOP500 universe内・月初第1営業日に算出・
    両特徴が揃わない銘柄は対象外・上位50銘柄=スコア群）:
    (a) PBR = (月初前営業日C[生値] × 直近開示ShOutFY[生株数]) ÷ 直近開示Eq【低いほど上位】
    (b) ネットキャッシュ比率 = CashEq ÷ (C[生値]×ShOutFY)【高いほど上位】
    rank方向=望ましいほど小さい順位番号・順位和最小が最上位 / 同値=平均順位 /
    50位境界の同値=同率全員含める / 情報cutoff=月初第1営業日の前営業日終了時点
    （fins は DiscDate < 月初第1営業日のみ・価格は前営業日終値）。
    **20営業日フォワードリターンの計算は調整株価Canonical（mbr.compute_forward_return_for_code）
    のまま変更なし**（スコア算出とリターン算出は別関数・別目的）。

**v1からのv2変更点（凍結対象・catalog §7-AE-v2本文2519-2528行）**:
    1. スコア式の単位整合（生値C使用）— v1エラッタ修正時に実装済み・v2でも継承
    2. masterのas-of規則明文化: **master_date_used == 前営業日 でなければFATAL**
       （黙ってさらに古いmasterに遡らない・compute_snapshot_score内で検証）
    3. analysis_group=4値（score_primary/score_repeat_ref_only/control_primary/
       control_repeat_ref_only）。主解析対照群=銘柄ごと全期間で初めてeligible∧rank51位以下に
       なった月のみ（control_primary）。両群所属は選出順序を問わず許容・重複銘柄数を診断報告。
       ラベル行=first_selection_flag/label_window_start/raw_label_end_bday/
       effective_label_end/observed_bdays。部分打切り=観測可能日数内未発生は未発生として分母に
       含める＋「完全63bd観測可能スナップショットのみ」の感度分析。
    4. 対照群にも同一規則でTOBラベル付与 → score_primary vs control_primary の率・差・比を報告。
    5. secCode解決の名寄せPIT復元（主解析=機械的一意一致のみ）: 正規化=NFKC→英字大文字化→
       空白除去→先頭・末尾の法人格語反復除去→記号除去（この順序固定）。masterは初回submitDate
       以前で最新の月次J-Quants master（Code+CoName）。正規化後の完全一致が一意な場合のみ採用・
       複数候補=ambiguous（不採用）・前方一致は使わない。人手復元は主解析に使わず感度分析専用
       （本バッチでは実施せず空テンプレートのみ用意）。92対象会社全件の
       resolved/unresolved/ambiguousと根拠を監査CSVに記録。未解決が残る場合、TOB濃縮の数字は
       「選択バイアスにより方向不明」と表記（過小評価と断定しない）。
    6. (ii)前向き観察の証跡=daily chainとは独立の月次run-log
       data/monitoring/tob_forward/run_log.jsonl（恒久規則: genesis prev_hash=64個の"0"・
       canonical JSON=UTF-8キー昇順separators(",",":")row_hash除外・
       row_hash=SHA256(canonical_json)・append前に末尾連結検証(不一致FATAL)・flock+fsync・
       出力CSVはtmp→原子rename→hash記録・同月成功行が既存で全hash一致=no-op/不一致=FATAL・
       失敗実行もstatus=failedで記録）。**daily証跡チェーン(KPI_DEPENDENCY_TABLE)には配線しない**
       （2026-07-18 Batch D安全性検証で実測確認済み・下記run_forward手前のコメント参照）。
    7. plist: 起動日Day1〜10・WorkingDirectory設定・出力先絶対パス引数明示
    8. 頻度報告: 「632件」は再現可能な走査条件が保存されていない概数（参考注記のみ）。
       正式値は分析窓2021-07-07〜2022-11-30の実走査=307件から始まる段階表（入力manifest hash付き）。

**親子上場特徴はv1から除外**（PIT親会社持分の一次資料が不成立=§7-Pで保有割合フィールド
不在を実証済み。現在値の過去遡及は禁止。PIT資料確立後に新規試行）。

**実データで確定した除外判別ロジック（data/edinet/documents_all/ 実サンプル確認・
2026-07-18・v1から変更なし）**:
    - docTypeCode 220/230（自己株券買付状況報告書・その訂正）は subjectEdinetCode を
      持たない別書類体系であり、そもそも TOB_DOC_TYPE_CODES（240/250/260/270/280）には
      含まれない＝自動的に除外される。
    - 自己TOB（発行者自身による公開買付＝自社株の公開買付）は docTypeCode=240 の中に混在する
      （edinetCode（提出者）== subjectEdinetCode（対象会社）で識別可能・実データ130件確認）。
      MBOはSPC（提出者）が対象会社と異なるEDINETコードを持つため除外されない（MBOを含む、
      という凍結仕様の要求どおり）。

Canonical Module再利用（再実装禁止・§7-AE-v2前提）:
    - カレンダー・営業日・bars読み込み・TOP500ユニバース構築・20営業日フォワードリターン・
      summarize() 集計 = scripts/measure_base_rate.py をそのまま再利用
    - fins数値パース = scripts/kpi_uprev_signals._parse_numeric を再利用
    - fins日次読み込み = scripts/kpi_pead_signals.load_fins_day を再利用
    - EDINET documents_all 生データ読み込みパス = scripts/edinet_fetch.ALL_DOCS_DATA_ROOT
    - ファイルhash・コードツリーhash = scripts/kpi_run_evidence.compute_file_hash /
      compute_code_tree_hash を再利用（**純粋ユーティリティ関数のみ**・
      append_run_log()/KPI_DEPENDENCY_TABLEには一切触れない=daily証跡チェーンとは非結合）
    - secCode解決はv1のEDINET単一スナップショット参照（kpi_activist_signals.load_edinet_code_master）
      を廃止し、v2はPIT名寄せ（本ファイル内で新規実装・下記参照）に置き換える

Usage:
    python3 scripts/kpi_tob_candidate_score.py --snapshot 2022-06
    python3 scripts/kpi_tob_candidate_score.py --range 2021-08:2022-11
    python3 scripts/kpi_tob_candidate_score.py --forward   # 当月・月初第1営業日以外は即exit 0
"""
from __future__ import annotations

import argparse
import bisect
import fcntl
import functools
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: read_json_gz/now_jst/カレンダー系を再利用)
import measure_base_rate as mbr  # noqa: E402  (Canonical Module: カレンダー・bars・ユニバース・20bdリターン・summarize)
import kpi_pead_signals as kps  # noqa: E402  (Canonical Module: load_fins_day)
import kpi_uprev_signals as kus  # noqa: E402  (Canonical Module: _parse_numeric・FINS_HISTORY_START_BD)
import edinet_fetch  # noqa: E402  (Canonical Module: ALL_DOCS_DATA_ROOT・ALL_DOCS_DEFAULT_START)
import kpi_run_evidence as run_evidence  # noqa: E402  (Canonical Module: 純粋util=compute_file_hash/compute_code_tree_hashのみ再利用。
                                          # append_run_log()やKPI_DEPENDENCY_TABLEは一切呼ばない=daily証跡チェーン非結合)

# --- §7-AE-v2 凍結パラメータ（以後変更禁止） ----------------------------------------
UNIVERSE_WINDOW = 21          # 既存TOP500定義と同一（base_rate/§7-AD共通の売買代金トレーリング窓）
UNIVERSE_TOP_N = 500
PORT_TOP_N = 50                # 上位50銘柄=スコア群

# 公開買付関連書類（届出書・訂正届出書・撤回届出書・報告書・訂正報告書）。実データで確認済み
# （220/230=自己株券買付状況報告書系は元々含まれず別体系のため、この集合に入れない）。
TOB_DOC_TYPE_CODES = {"240", "250", "260", "270", "280"}

FORWARD_LABEL_WINDOW_BD = 63   # 副次TOBラベル: 各snapから63営業日以内
LABEL_CUTOFF_DATE = "20221130"  # 2023年以降のEDINET書類はラベルにも一切使わない（凍結・global cutoff）

REFERENCE_START_MONTH = "2021-08"
REFERENCE_END_MONTH = "2022-11"
EDINET_START_BD = edinet_fetch.ALL_DOCS_DEFAULT_START  # "20210707"（EDINET実データ先頭日）

FINS_FIELDS = ("ShOutFY", "Eq", "CashEq")

ANALYSIS_GROUPS = ("score_primary", "score_repeat_ref_only", "control_primary", "control_repeat_ref_only")

# 名寄せ正規化（§7-AE-v2凍結・処理順固定: NFKC→大文字化→空白除去→法人格語反復除去→記号除去）
_CORPORATE_FORM_WORDS = ("株式会社", "合同会社", "有限会社", "(株)")
# 記号「・．，－ー()&」のNFKC後形。．，－はNFKC(step1)で.,-に変換済みのため、この時点(step5)の
# 文字集合で除去する（処理順が固定＝NFKCが先に走るため、pre-NFKC全角形は本ステップに到達しない）。
_SYMBOL_CHARS_TO_STRIP = "・.,-ー()&"

_CANONICAL_DATE_RE = re.compile(r"^\d{8}$")

FORWARD_RUNLOG_GENESIS_HASH = "0" * 64


# --- masterスナップショットのas-of解決（2026-07-18 team lead裁定・bisect方式） -----------------
#
# data/jquants/master/ は月末営業日にしか取得されない設計（jq_fetch.py:607）。§7-AE本文は
# ユニバースを「月初第1営業日」に構築するが、これは build_universe() 内部のProdCat分類
# （内国株券フィルタ）に使う master が月初当日には存在しないことを意味する。
#
# 裁定（team lead 2026-07-18・凍結仕様の解釈として正当化・コード上に明記）:
#   §7-AE凍結本文の情報cutoff=「月初第1営業日の前営業日終了時点」（fins DiscDate<月初日・
#   価格=前営業日終値）。月末最終営業日は定義上、翌月初第1営業日の直前営業日である。
#   よってProdCat分類に直近の（=snapshot日より前で最新の）masterスナップショットを使う
#   ことは、cutoff規約への正確な準拠であり、月初日当日のmasterを新規取得するよりむしろ
#   整合的である。ハードコードした「前月末」ではなく実在ファイル集合からのbisect解決に
#   することで、欠落月があっても機械的に正しく遡れる（堅牢化・2026-07-18裁定）。
#
# §7-AE-v2凍結項目2（2026-07-18）: 上記解決結果は必ず prev_bday（月初第1営業日の直前営業日）と
# 一致しなければならない（黙ってさらに古いmasterに遡らない）。一致しない場合はFATAL
# （compute_snapshot_score内で検証・下記参照）。


def available_master_dates() -> list[str]:
    """data/jquants/master/ に実在するYYYYMMDD日付を昇順で返す（revファイル等の亜種は除外）。"""
    root = jq_fetch.DATA_ROOT / "master"
    return sorted(
        p.name.removesuffix(".json.gz") for p in root.glob("*.json.gz")
        if _CANONICAL_DATE_RE.match(p.name.removesuffix(".json.gz"))
    )


def resolve_master_asof_date(snapshot_bday: str, available: list[str]) -> str:
    """snapshot_bday より前（strict <）で最新の既存masterスナップショット日をbisectで返す。

    look-ahead防止のため strict < を使う（snapshot_bday当日のmasterが将来的に存在する
    ようになっても、それを使わない）。§7-AE-v2凍結項目2: 呼び出し側(compute_snapshot_score)が
    戻り値==prev_bdayを検証しFATALガードする（本関数自体はstrict<のbisect解決のみを担う）。
    """
    pos = bisect.bisect_left(available, snapshot_bday)
    if pos == 0:
        raise SystemExit(
            f"FATAL: {snapshot_bday} より前のmasterスナップショットが1件も見つかりません"
            f"（最古の既存master={available[0] if available else 'なし'}）。"
        )
    return available[pos - 1]


def resolve_master_asof_date_inclusive(target_date: str, available: list[str]) -> Optional[str]:
    """target_date以前（on-or-before・inclusive）で最新の既存masterスナップショット日を返す。

    §7-AE-v2凍結項目5「初回submitDate以前で最新の月次J-Quants master」の名寄せ専用関数。
    resolve_master_asof_date()（strict <・look-ahead防止用・ユニバース構築専用）とは
    意味が異なる別関数（"以前"=inclusiveという凍結字義どおり・母集団除外の「以前」用法とも
    整合）。該当なしはNone（呼び出し側でno_master_availableとして扱う）。
    """
    pos = bisect.bisect_right(available, target_date)
    if pos == 0:
        return None
    return available[pos - 1]


# --- fins as-of シリーズ構築（3フィールドまとめて1回のスキャンで構築・strict "<" cutoff） -------


def build_fins_feature_series(
    fe_start: str, fe_end: str, all_bdays: list[str]
) -> dict[str, dict[str, tuple[list[int], list[float]]]]:
    """[fe_start, fe_end] の全fins開示から ShOutFY/Eq/CashEq のas-ofシリーズを構築する。

    kpi_rank_portfolio._build_asof_series と同型のパターンだが、対象が事前生成イベント
    DataFrame ではなく生fins開示（disclosed_dateキー=DiscDate、値=ShOutFY/Eq/CashEqそのもの）
    のため専用に実装する（3フィールドを1回のfinsスキャンでまとめて構築し、日次gzファイルの
    再読み込みを避ける）。同一DiscDateが複数レコードある場合は後勝ち（最新値で上書き）。

    Returns:
        field(ShOutFY/Eq/CashEq) -> code -> (昇順DiscDate[int]リスト, 対応値リスト)
    """
    raw: dict[str, dict[str, dict[int, float]]] = {f: defaultdict(dict) for f in FINS_FIELDS}
    scan_days = [d for d in all_bdays if fe_start <= d <= fe_end]
    for d in scan_days:
        for rec in kps.load_fins_day(d):
            code = rec.get("Code")
            disc_date = rec.get("DiscDate")
            if not code or not disc_date:
                continue
            di = int(str(disc_date).replace("-", ""))
            for field in FINS_FIELDS:
                val = kus._parse_numeric(rec.get(field))
                if val is not None:
                    raw[field][code][di] = val
    out: dict[str, dict[str, tuple[list[int], list[float]]]] = {}
    for field, code_map in raw.items():
        field_out = {}
        for code, dmap in code_map.items():
            dates = sorted(dmap)
            field_out[code] = (dates, [dmap[dt] for dt in dates])
        out[field] = field_out
    return out


def _asof_strict_before(series: dict, code: str, d: str) -> Optional[float]:
    """disclosed_date < d（strict）の直近開示値を返す（§7-AE凍結cutoff: DiscDate<月初第1営業日）。

    kpi_rank_portfolio._asof_value は bisect_right で "<=" 判定（§7-AD凍結値）だが、§7-AE は
    本文で明示的に strict "<" を要求するため bisect_left を使う専用実装（cutoff規約が異なる
    別試行のため、既存の "<=" 実装を流用すると凍結仕様に反する）。
    """
    entry = series.get(code)
    if entry is None:
        return None
    dates, vals = entry
    di = int(d)
    pos = bisect.bisect_left(dates, di)  # dates[:pos] は全て di 未満
    if pos == 0:
        return None
    return vals[pos - 1]


# --- EDINET TOB書類読み込み（v1から変更なし） -------------------------------------


def load_edinet_documents_all(start_bd: str, end_bd: str) -> list[dict]:
    """[start_bd, end_bd] の EDINET documents_all 生データを日付昇順で結合して返す。

    欠損営業日があれば明確なFATALで停止する（edinet_fetch.py --dataset documents_all で
    先に取得すること）。<date>.revN.json.gz（退避ファイル）は正本ではないため読まない。
    """
    calendar_days = mbr.load_calendar_days()
    all_bdays = mbr.all_business_days(calendar_days)
    business_days = [d for d in all_bdays if start_bd <= d <= end_bd]
    rows: list[dict] = []
    missing: list[str] = []
    for d in business_days:
        path = edinet_fetch.ALL_DOCS_DATA_ROOT / f"{d}.json.gz"
        if not path.exists():
            missing.append(d)
            continue
        obj = jq_fetch.read_json_gz(path)
        rows.extend(obj.get("results", []))
    if missing:
        raise SystemExit(
            f"FATAL: EDINET documents_all キャッシュが {len(missing)}/{len(business_days)} 日分"
            f"見つかりません（例: {missing[:3]}）。\n"
            f"先に `python3 scripts/edinet_fetch.py --dataset documents_all "
            f"--start {start_bd} --end {end_bd}` を実行してください。"
        )
    return rows


def compute_edinet_manifest_hash(start_bd: str, end_bd: str, all_bdays: list[str]) -> dict:
    """[start_bd, end_bd]で実際に読み込んだEDINET documents_allファイル群のmanifest hashを返す
    （§7-AE-v2凍結項目8「段階表を入力manifest hash付きで報告」）。

    kpi_run_evidence.compute_file_hash（純粋関数・daily証跡チェーンとは無関係）を再利用する。
    """
    business_days = [d for d in all_bdays if start_bd <= d <= end_bd]
    parts = []
    for d in business_days:
        path = edinet_fetch.ALL_DOCS_DATA_ROOT / f"{d}.json.gz"
        file_hash = run_evidence.compute_file_hash(path)
        parts.append(f"{d}:{file_hash or 'MISSING'}")
    manifest_hash = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return {"manifest_hash": manifest_hash, "n_files": len(business_days), "start": start_bd, "end": end_bd}


def _build_file_manifest_hash(paths_by_date: dict[str, Path]) -> dict[str, Any]:
    """日付でソートした「日付:SHA256」canonical列からmanifest hashを構築する。

    2026-07-18 Codex実装後レビューブロッカー5修正: forward run-log(run_forward)の入力hashが
    実際に読み込んだ全ファイルを網羅していなかった（bars=前営業日1件のみ・fins=hash未計上等）。
    本関数はrun_evidence.compute_file_hash（純粋関数）を再利用し、bars/fins/pit_master等の
    複数ファイルにまたがる入力をmanifest_hash+n_files+start+endの形で一様に記録する。
    欠損ファイルはhash値'MISSING'として文字列化する（compute_edinet_manifest_hashと同型）。
    """
    dates_sorted = sorted(paths_by_date)
    parts = [f"{d}:{run_evidence.compute_file_hash(paths_by_date[d]) or 'MISSING'}" for d in dates_sorted]
    manifest_hash = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return {
        "manifest_hash": manifest_hash, "n_files": len(dates_sorted),
        "start": dates_sorted[0] if dates_sorted else None,
        "end": dates_sorted[-1] if dates_sorted else None,
    }


def compute_bars_manifest_hash(win_days: list[str]) -> dict[str, Any]:
    """build_universe/compute_snapshot_scoreが実際に読み込んだ全bars日次ファイルのmanifest
    （§7-AE-v2凍結項目6ブロッカー5・トレーリング窓UNIVERSE_WINDOW営業日分＝prev_bdayを含む）。"""
    paths = {d: jq_fetch.DATA_ROOT / "bars" / f"{d}.json.gz" for d in win_days}
    return _build_file_manifest_hash(paths)


def compute_fins_manifest_hash(scan_days: list[str]) -> dict[str, Any]:
    """build_fins_feature_seriesが実際に読み込んだ全fins日次ファイルのmanifest
    （§7-AE-v2凍結項目6ブロッカー5・kps.FINS_DIRを参照＝Canonical Module再利用）。"""
    paths = {d: kps.FINS_DIR / f"{d}.json.gz" for d in scan_days}
    return _build_file_manifest_hash(paths)


def load_edinet_code_master_registry(path: Path) -> dict[str, dict[str, str]]:
    """EDINETコードリスト(EdinetcodeDlInfo.csv)から EDINETコード -> {name, listing_status} を作る。

    v1では証券コード列を直接引いて secCode 解決していたが（TOB成立→上場廃止で証券コード欄が
    空になり系統的に取りこぼす欠陥が判明・2026-07-18）、v2では**提出者名**列を使うPIT名寄せに
    置き換える（build_tob_deal_table参照）。提出者名は非上場化後も空欄化しないことを実データで
    確認済み（2026-07-18・非上場1266件中1266件で提出者名あり=100%）。listing_statusは監査CSVの
    参考列としてのみ保持する。
    """
    if not path.exists():
        raise SystemExit(
            f"FATAL: EDINETコードマスタが見つかりません: {path}\n"
            f"先に `python3 scripts/edinet_fetch.py --fetch-code-master` を実行してください。"
        )
    import csv
    with path.open("r", encoding="utf-8", newline="") as f:
        lines = f.readlines()
    registry: dict[str, dict[str, str]] = {}
    for row in csv.DictReader(lines[1:]):
        edinet_code = (row.get("ＥＤＩＮＥＴコード") or "").strip()
        if not edinet_code:
            continue
        registry[edinet_code] = {
            "name": (row.get("提出者名") or "").strip(),
            "listing_status": (row.get("上場区分") or "").strip() or "(空欄)",
        }
    return registry


# --- secCode解決の名寄せPIT復元（§7-AE-v2凍結項目5・新規） -------------------------


def normalize_company_name(raw: str) -> str:
    """§7-AE-v2凍結の会社名正規化（処理順固定・一言一句）:
    NFKC → 英字大文字化 → 空白（全半角）除去 → 先頭・末尾の法人格語[株式会社/合同会社/
    有限会社/(株)]を反復除去 → 記号「・．，－ー()&」除去（法人格語除去が記号除去より先）。

    記号除去の対象文字は凍結本文記載の8文字（U+30FB・U+FF0E・U+FF0C・U+FF0D・U+30FC・
    U+0028・U+0029・U+0026）だが、記号除去はNFKC(step1)の後に実行されるため、この時点では
    ．（U+FF0E）／，（U+FF0C）／－（U+FF0D）は既にNFKCで .／,／- （半角）へ変換済みである。
    したがって本関数は「NFKC後の文字空間」でこれらの記号を除去する（=処理順固定という
    凍結指示をそのまま実行した帰結。pre-NFKC全角形をこの段で探しても処理順上決して出現せず
    無意味な除去になるため、意図（記号カテゴリの除去）を正しく実現する解釈を採用）。
    """
    if not raw:
        return ""
    s = unicodedata.normalize("NFKC", raw)
    s = s.upper()
    s = re.sub(r"\s+", "", s)
    changed = True
    while changed:
        changed = False
        for word in _CORPORATE_FORM_WORDS:
            if s.startswith(word):
                s = s[len(word):]
                changed = True
            if s.endswith(word):
                s = s[:-len(word)] if len(word) else s
                changed = True
    for ch in _SYMBOL_CHARS_TO_STRIP:
        s = s.replace(ch, "")
    return s


@functools.lru_cache(maxsize=64)
def build_master_name_index(master_date: str) -> dict[str, tuple[str, ...]]:
    """指定master日のCode+CoNameから 正規化名 -> (Code,...) のインデックスを作る（曖昧判定用）。

    同一master snapshot内で複数Codeが同一正規化名になるケース（ambiguous判定用）を含めて
    全件保持する。mbr.load_master_day自体がlru_cache済み（measure_base_rate.py）なので、
    本関数の追加cacheは正規化・インデックス構築コストのみを削減する。
    """
    master = mbr.load_master_day(master_date)
    index: dict[str, list[str]] = defaultdict(list)
    for code, rec in master.items():
        name = rec.get("CoName")
        if not name:
            continue
        norm = normalize_company_name(name)
        if norm:
            index[norm].append(code)
    return {k: tuple(v) for k, v in index.items()}


def resolve_subject_via_name_pit(
    first_submit_date: str, raw_name: str, master_dates: list[str],
) -> dict[str, Any]:
    """secCode解決の名寄せPIT復元（§7-AE-v2凍結・主解析=機械的一意一致のみ）。

    「初回submitDate以前で最新の月次J-Quants master」に対して正規化後の完全一致を探す。
    一意一致のみ採用（複数候補=ambiguous・不採用。前方一致は使わない）。

    Returns:
        {"status": "resolved"|"unresolved"|"ambiguous"|"no_master_available",
         "resolved_code": Optional[str], "normalized_name": str,
         "master_date_used": Optional[str], "n_candidates": int, "raw_name": str}
    """
    normalized = normalize_company_name(raw_name)
    master_date = resolve_master_asof_date_inclusive(first_submit_date, master_dates)
    if master_date is None:
        return {
            "status": "no_master_available", "resolved_code": None,
            "normalized_name": normalized, "master_date_used": None,
            "n_candidates": 0, "raw_name": raw_name,
        }
    index = build_master_name_index(master_date)
    candidates = index.get(normalized, ())
    if len(candidates) == 1:
        return {
            "status": "resolved", "resolved_code": candidates[0],
            "normalized_name": normalized, "master_date_used": master_date,
            "n_candidates": 1, "raw_name": raw_name,
        }
    if len(candidates) == 0:
        return {
            "status": "unresolved", "resolved_code": None,
            "normalized_name": normalized, "master_date_used": master_date,
            "n_candidates": 0, "raw_name": raw_name,
        }
    return {
        "status": "ambiguous", "resolved_code": None,
        "normalized_name": normalized, "master_date_used": master_date,
        "n_candidates": len(candidates), "raw_name": raw_name,
    }


def _resolution_basis_text(res: dict[str, Any]) -> str:
    if res["status"] == "resolved":
        return (
            f"正規化名'{res['normalized_name']}'がmaster({res['master_date_used']})で"
            f"一意一致(Code={res['resolved_code']})"
        )
    if res["status"] == "ambiguous":
        return (
            f"正規化名'{res['normalized_name']}'がmaster({res['master_date_used']})で"
            f"{res['n_candidates']}件に一致(ambiguous・不採用)"
        )
    if res["status"] == "no_master_available":
        return "first_submit_date以前のmasterスナップショットが1件も存在しない"
    return f"正規化名'{res['normalized_name']}'がmaster({res['master_date_used']})で一致0件(unresolved)"


MANUAL_CONFIRMATION_TEMPLATE_COLUMNS = (
    "subject_edinet_code", "raw_name", "confirmer", "source_document_hash",
    "confirmed_at", "basis_text", "resolved_code",
)


def write_manual_confirmation_sensitivity_template(path: Path) -> None:
    """人手復元センシティビティ分析用の**空テンプレート**を書く（§7-AE-v2凍結項目5）。

    主解析には使わない（機械的一意一致のみが主解析）。本バッチでは人手復元を一切実施しない
    （ヘッダー行のみ・データ行0件）。将来、一次資料で証券コードが明記された場合のみ、
    確認者にスコア順位/top50在籍/ラベル/リターンを見せないブラインド手順で追記する運用。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(columns=list(MANUAL_CONFIRMATION_TEMPLATE_COLUMNS)).to_csv(path, index=False)


# --- EDINET TOB案件統合（§7-AE-v2: secCode解決を名寄せPIT復元に置き換え） -----------


def build_tob_deal_table(
    records: list[dict], edinet_registry: dict[str, dict[str, str]], master_dates: list[str],
) -> tuple[dict[str, dict], dict, list[dict]]:
    """subjectEdinetCode単位でTOB案件を1件に統合する（§7-AE-v2凍結の案件束ねロジック）。

    除外規則（v1から変更なし・一言一句）:
        - docTypeCode が TOB_DOC_TYPE_CODES（240/250/260/270/280）でないレコードは対象外。
        - subjectEdinetCode が無い（null）レコードは対象外。
        - edinetCode（提出者）== subjectEdinetCode（対象会社）は自己TOBとして除外
          （MBOはSPCが別法人のため除外されない）。

    同一subjectEdinetCodeに残った書類群は、初回submitDateTime（昇順で最小）を1案件の
    確定日として統合する。**v2の変更点**: secCode解決を、EDINET単一スナップショットの
    証券コード列参照（v1・上場廃止で欠落する欠陥あり）から、PIT名寄せ（正規化会社名の
    「初回submitDate以前で最新のJ-Quants master」に対する機械的一意一致）に置き換える。

    Returns:
        (deals, diag, resolution_audit)。
        deals: subjectEdinetCode -> {code, first_submit_date(YYYYMMDD), n_docs, doc_ids,
               normalized_name, master_date_used}（resolved のみ含む）。
        resolution_audit: distinct_subject_edinet_codes件全件（自己TOB/subject無し除外後）の
               resolved/unresolved/ambiguous/no_master_available と根拠を記録した監査行リスト。
    """
    diag = {
        "raw_tob_type_docs": 0, "excluded_no_subject": 0, "excluded_self_tender": 0,
        "distinct_subject_edinet_codes": 0,
        "resolved_deals": 0, "unresolved_n": 0, "ambiguous_n": 0, "no_master_available_n": 0,
    }
    by_subject: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if str(r.get("docTypeCode")) not in TOB_DOC_TYPE_CODES:
            continue
        diag["raw_tob_type_docs"] += 1
        subject = r.get("subjectEdinetCode")
        if not subject:
            diag["excluded_no_subject"] += 1
            continue
        if r.get("edinetCode") and r.get("edinetCode") == subject:
            diag["excluded_self_tender"] += 1
            continue
        by_subject[subject].append(r)

    diag["distinct_subject_edinet_codes"] = len(by_subject)
    deals: dict[str, dict] = {}
    resolution_audit: list[dict] = []
    for subject, docs in sorted(by_subject.items()):
        docs_sorted = sorted(docs, key=lambda r: r.get("submitDateTime") or "")
        first = docs_sorted[0]
        submit_dt = first.get("submitDateTime") or ""
        if not submit_dt or len(submit_dt) < 10:
            continue
        first_submit_date = submit_dt[:10].replace("-", "")
        entry = edinet_registry.get(subject, {})
        raw_name = entry.get("name") or ""
        listing_status = entry.get("listing_status") or ""
        res = resolve_subject_via_name_pit(first_submit_date, raw_name, master_dates)

        resolution_audit.append({
            "subject_edinet_code": subject, "raw_name": raw_name,
            "normalized_name": res["normalized_name"], "listing_status": listing_status,
            "first_submit_date": first_submit_date, "n_docs": len(docs),
            "master_date_used": res["master_date_used"], "n_candidates": res["n_candidates"],
            "resolution_status": res["status"], "resolved_code": res["resolved_code"],
            "basis": _resolution_basis_text(res),
        })

        if res["status"] == "resolved":
            diag["resolved_deals"] += 1
            deals[subject] = {
                "code": res["resolved_code"], "first_submit_date": first_submit_date,
                "n_docs": len(docs), "doc_ids": [d.get("docID") for d in docs_sorted],
                "normalized_name": res["normalized_name"], "master_date_used": res["master_date_used"],
            }
        elif res["status"] == "ambiguous":
            diag["ambiguous_n"] += 1
        elif res["status"] == "no_master_available":
            diag["no_master_available_n"] += 1
        else:
            diag["unresolved_n"] += 1

    return deals, diag, resolution_audit


def deals_by_code_index(deals: dict[str, dict]) -> dict[str, str]:
    """code -> 最も早い first_submit_date（同一証券コードに複数subjectEdinetCodeが解決した
    稀なケースは最古の日付を採用）。母集団除外・ラベル判定の両方でこのインデックスを使う。"""
    idx: dict[str, str] = {}
    for d in deals.values():
        code = d["code"]
        if code not in idx or d["first_submit_date"] < idx[code]:
            idx[code] = d["first_submit_date"]
    return idx


# --- スコア計算（凍結・単一式） ---------------------------------------------------


def compute_snapshot_score(
    month_start_bday: str,
    bday_index: dict[str, int],
    all_bdays: list[str],
    fins_series: dict,
    exclude_codes: set[str],
    master_dates: list[str],
) -> tuple[pd.DataFrame, dict]:
    """1スナップショット（月初第1営業日）のTOB候補スコアを計算する（§7-AE-v2凍結の単一式）。

    ProdCat分類（TOP500ユニバースのuniverse内国株券フィルタ）は month_start_bday より前で
    最新の既存masterスナップショットをas-of解決して使う。**§7-AE-v2凍結項目2**:
    解決結果は必ず prev_bday と一致しなければならずFATALで検証する（黙ってさらに古い
    masterに遡らない）。売買代金トレーリング窓・価格・fins as-ofはすべて month_start_bday
    （または前営業日）そのものを使い、この点は一切変更しない。

    Returns:
        (df, diag)。df列: code, price_prev_raw, shoutfy, eq, casheq, market_cap, pbr,
        netcash_ratio, eligible, rank_pbr, rank_netcash, score, top50_flag。
        eligible=False の行は rank/score が NaN（両特徴が揃わない銘柄は対象外）。
        price_prev_raw は無調整の生値C。ShOutFY（fins開示の生株式数）と単位を揃えるため、
        時価総額計算にはAdjC(調整済み)ではなくC(生値)を使う。フォワードリターンは別関数
        (mbr.compute_forward_return_for_code)がAdjCを使うため、こちらは変更しない。
    """
    idx = bday_index[month_start_bday]
    prev_bday = all_bdays[idx - 1]

    master_date = resolve_master_asof_date(month_start_bday, master_dates)
    if master_date != prev_bday:
        raise SystemExit(
            f"FATAL: master_date_used({master_date}) != prev_bday({prev_bday})"
            f"（§7-AE-v2凍結項目2: 黙ってさらに古いmasterに遡らない。月初第1営業日={month_start_bday}）。"
        )
    selected, ustats = mbr.build_universe(
        month_start_bday, bday_index, all_bdays, UNIVERSE_WINDOW, UNIVERSE_TOP_N,
        master_date=master_date,
    )
    uni_codes = [c for c, _tv in selected]
    excluded_from_universe = [c for c in uni_codes if c in exclude_codes]
    uni_codes = [c for c in uni_codes if c not in exclude_codes]

    bars_prev = mbr.load_bars_day(prev_bday)

    rows = []
    for code in uni_codes:
        # 生値C（無調整）を使う。ShOutFYが生株式数のため単位を揃える（v1エラッタ修正・v2で継承）。
        price_raw = bars_prev.get(code, {}).get("C")
        shoutfy = _asof_strict_before(fins_series["ShOutFY"], code, month_start_bday)
        eq = _asof_strict_before(fins_series["Eq"], code, month_start_bday)
        casheq = _asof_strict_before(fins_series["CashEq"], code, month_start_bday)

        market_cap = None
        if price_raw and price_raw > 0 and shoutfy is not None and shoutfy > 0:
            market_cap = price_raw * shoutfy

        pbr = None
        if market_cap is not None and eq is not None and eq > 0:
            pbr = market_cap / eq

        netcash = None
        if casheq is not None and market_cap is not None and market_cap > 0:
            netcash = casheq / market_cap

        eligible = pbr is not None and netcash is not None
        rows.append({
            "code": code, "price_prev_raw": price_raw, "shoutfy": shoutfy, "eq": eq, "casheq": casheq,
            "market_cap": market_cap, "pbr": pbr, "netcash_ratio": netcash, "eligible": eligible,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(columns=[
            "code", "price_prev_raw", "shoutfy", "eq", "casheq", "market_cap", "pbr",
            "netcash_ratio", "eligible", "rank_pbr", "rank_netcash", "score", "top50_flag",
        ])
        diag = {
            "month_start_bday": month_start_bday, "prev_bday": prev_bday, "master_date_used": master_date,
            "universe_n": len(selected), "excluded_from_universe_n": len(excluded_from_universe),
            "eligible_n": 0, "top50_n": 0, "universe_stats": ustats,
        }
        return df, diag

    df["rank_pbr"] = pd.NA
    df["rank_netcash"] = pd.NA
    df["score"] = pd.NA
    df["top50_flag"] = False

    elig_mask = df["eligible"]
    n_elig = int(elig_mask.sum())
    if n_elig > 0:
        elig_idx = df.index[elig_mask]
        # rank方向=望ましいほど小さい順位番号・同値=平均順位（凍結・pandas method='average'）
        rank_pbr = df.loc[elig_idx, "pbr"].rank(method="average", ascending=True)
        rank_netcash = df.loc[elig_idx, "netcash_ratio"].rank(method="average", ascending=False)
        score = rank_pbr + rank_netcash
        df.loc[elig_idx, "rank_pbr"] = rank_pbr
        df.loc[elig_idx, "rank_netcash"] = rank_netcash
        df.loc[elig_idx, "score"] = score

        elig_scores = score.sort_values()
        if len(elig_scores) >= PORT_TOP_N:
            cutoff_score = elig_scores.iloc[PORT_TOP_N - 1]
        else:
            cutoff_score = elig_scores.iloc[-1]
        # 50位境界の同値=同率全員含める（凍結・dense boundary。50件を超えることがある）
        df.loc[elig_idx, "top50_flag"] = score <= cutoff_score

    diag = {
        "month_start_bday": month_start_bday, "prev_bday": prev_bday, "master_date_used": master_date,
        "universe_n": len(selected), "excluded_from_universe_n": len(excluded_from_universe),
        "eligible_n": n_elig, "top50_n": int(df["top50_flag"].sum()), "universe_stats": ustats,
    }
    return df, diag


def write_snapshot_csv(path: Path, df: pd.DataFrame, month_start_bday: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out.insert(0, "snapshot_month_start_bday", month_start_bday)
    out.sort_values(["score", "code"], na_position="last").to_csv(path, index=False)


def stage_forward_csv(path: Path, df: pd.DataFrame, month_start_bday: str) -> tuple[Path, str]:
    """出力CSVをstage(.stage)ファイルへ書き、そのsha256を返す（**最終pathへはまだ置かない**）。

    2026-07-18 Codex実装後レビューブロッカー4修正: 旧atomic_write_csvは冪等判定より前に
    tmp→原子renameで最終CSVを書き換えていたため、（決定論的パイプラインが崩れた場合などに）
    冪等判定でFATAL/no-opになっても既に最終ファイルの中身が変わってしまう余地があった。
    修正後は「stageに書く→hash計算→append_forward_run_log()がlock下で冪等判定した上で
    昇格(stage.replace(final))するか、stageを削除して最終ファイルに一切触れないか」を
    append_forward_run_log側に委ねる（下記参照）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out.insert(0, "snapshot_month_start_bday", month_start_bday)
    out = out.sort_values(["score", "code"], na_position="last")
    stage = path.with_name(path.name + ".stage")
    out.to_csv(stage, index=False)
    stage_hash = hashlib.sha256(stage.read_bytes()).hexdigest()
    return stage, stage_hash


# --- (i) 歴史参考観測: フォワードリターン・TOBラベル ------------------------------


def _validate_analysis_group(group: str) -> None:
    """analysis_groupが凍結4値以外ならFATAL（§7-AE-v2凍結項目3・Codex実装後レビューブロッカー1
    修正: ラベル行生成(_build_label_row)とフォワードリターン行生成(_forward_return_row)の
    共通入口で拒否する。未知値が黙ってCSVに紛れ込むのを防ぐ）。
    """
    if group not in ANALYSIS_GROUPS:
        raise ValueError(
            f"invalid analysis_group={group!r}（許容値={ANALYSIS_GROUPS}・"
            f"§7-AE-v2凍結項目3: 4値以外は許容しない）"
        )


def _forward_return_row(code: str, group: str, month_start_bday: str, bday_index: dict, all_bdays: list) -> Optional[dict]:
    _validate_analysis_group(group)
    result = mbr.compute_forward_return_for_code(code, month_start_bday, bday_index, all_bdays)
    if result is None:
        return None
    row = {"code": code, "group": group, "snapshot": month_start_bday[:6], **result}
    return row


def _build_label_row(
    snap: str, code: str, group: str, first_selection_flag: bool, deal_idx: dict[str, str],
    bday_index: dict[str, int], effective_label_end: str, raw_label_end_bday: str, censored: bool,
) -> dict[str, Any]:
    """1(snapshot,code)組のTOBラベル行を作る（§7-AE-v2凍結項目3/4: score/control両群に同一規則
    で適用。観測可能日数内で未発生=未発生として分母に含める＝観測不能な期間は単に含めない）。
    """
    _validate_analysis_group(group)
    first_submit = deal_idx.get(code)
    labeled = bool(first_submit and snap < first_submit <= effective_label_end)
    snap_idx = bday_index[snap]
    effective_idx = bday_index[effective_label_end]
    observed_bdays = effective_idx - snap_idx
    return {
        "snapshot": snap, "code": code, "analysis_group": group,
        "first_selection_flag": first_selection_flag, "tob_labeled": labeled,
        "first_submit_date": first_submit, "label_window_start": snap,
        "raw_label_end_bday": raw_label_end_bday, "effective_label_end": effective_label_end,
        "observed_bdays": observed_bdays, "censored": censored,
    }


def run_reference_observation(output_dir: Path) -> None:
    """(i) 歴史参考観測（2021-08〜2022-11・16スナップショット・§6正式verdictなし・§7-AE-v2）。

    trials.jsonl への書き込みは一切行わない（team lead監査後に台帳追記する運用のため）。
    """
    calendar_days = mbr.load_calendar_days()
    all_bdays = mbr.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    snapshots = mbr.month_starts_in_range(calendar_days, REFERENCE_START_MONTH, REFERENCE_END_MONTH)
    if len(snapshots) < 2:
        raise SystemExit(f"FATAL: 参照期間の月初営業日が不足しています（{len(snapshots)}件）")
    print(f"[ref-obs] {len(snapshots)}スナップショット: {snapshots[0]} 〜 {snapshots[-1]}", flush=True)

    # fins as-of シリーズは参照期間の最終スナップショットまでを一括構築。
    fe_start = kus.FINS_HISTORY_START_BD
    fe_end = snapshots[-1]
    print(f"[ref-obs] fins as-of シリーズ構築中 {fe_start}..{fe_end} ...", flush=True)
    fins_series = build_fins_feature_series(fe_start, fe_end, all_bdays)
    for f in FINS_FIELDS:
        print(f"[ref-obs] fins[{f}]: {len(fins_series[f])} codes", flush=True)

    # masterのas-of解決カバレッジ確認（16スナップショット全てで直前masterが実在することを
    # 先に確認・欠けがあればその月だけ報告して停止）。build_tob_deal_tableの名寄せにも使うため
    # ここで先に構築する（v1はcompute_snapshot_score呼び出し直前でのみ使っていた）。
    master_dates = available_master_dates()
    print(
        f"[ref-obs] 利用可能masterスナップショット: {len(master_dates)}件"
        f"（{master_dates[0]}〜{master_dates[-1]}）", flush=True,
    )
    coverage_missing = []
    for snap in snapshots:
        try:
            resolve_master_asof_date(snap, master_dates)
        except SystemExit:
            coverage_missing.append(snap)
    if coverage_missing:
        raise SystemExit(
            f"FATAL: 以下{len(coverage_missing)}スナップショットはmasterのas-of解決ができません"
            f"（これより前のmasterが1件も存在しない）: {coverage_missing}"
        )
    print(f"[ref-obs] 全{len(snapshots)}スナップショットでmaster as-of解決を確認済み", flush=True)

    # EDINET TOB案件テーブルは EDINET実データ先頭日(20210707) 〜 ラベルcutoff(20221130) を一括構築
    # （母集団除外にも副次ラベルにもこの同一テーブルを使う。2023年以降の書類はラベルにも一切
    # 使わない=凍結仕様の要求どおり、走査範囲自体をcutoffで打ち切る）。
    print(f"[ref-obs] EDINET documents_all 読み込み中 {EDINET_START_BD}..{LABEL_CUTOFF_DATE} ...", flush=True)
    edinet_records = load_edinet_documents_all(EDINET_START_BD, LABEL_CUTOFF_DATE)
    manifest = compute_edinet_manifest_hash(EDINET_START_BD, LABEL_CUTOFF_DATE, all_bdays)
    edinet_registry = load_edinet_code_master_registry(edinet_fetch.EDINET_CODE_MASTER_PATH)
    deals, deal_diag, resolution_audit = build_tob_deal_table(edinet_records, edinet_registry, master_dates)
    deal_idx = deals_by_code_index(deals)
    print(
        f"[ref-obs] EDINET: raw_docs={len(edinet_records)} tob_type_docs={deal_diag['raw_tob_type_docs']} "
        f"self_tender除外={deal_diag['excluded_self_tender']} subject無し除外={deal_diag['excluded_no_subject']} "
        f"distinct={deal_diag['distinct_subject_edinet_codes']} → 機械一意解決={deal_diag['resolved_deals']}"
        f"（unresolved={deal_diag['unresolved_n']}・ambiguous={deal_diag['ambiguous_n']}・"
        f"no_master={deal_diag['no_master_available_n']}）manifest_hash={manifest['manifest_hash'][:16]}...",
        flush=True,
    )

    snapshot_dir = output_dir / "snapshots"
    all_score_rows = []
    forward_rows = []
    label_rows = []
    censor_diag_rows = []
    control_composition_rows = []  # ブロッカー2: control_primary初回選出の内訳診断
    seen_score_codes: set[str] = set()    # 主解析スコア群=銘柄ごと初回top50選出月のみ
    seen_control_codes: set[str] = set()  # 主解析対照群=銘柄ごと全期間で初めてeligible∧rank51+の月のみ
    ever_universe_codes: set[str] = set()   # 過去いずれかのsnapshotでuniverse(TOP500・TOB除外後)に在籍した銘柄
    ever_eligible_codes: set[str] = set()   # 過去いずれかのsnapshotでeligible(PBR/netcash両方算出可)だった銘柄

    for snap in snapshots:
        # 母集団除外: スコア基準日"以前"（on-or-before）にTOB関連書類が存在する対象会社を除外
        exclude_codes = {c for c, d in deal_idx.items() if d <= snap}
        df, diag = compute_snapshot_score(snap, bday_index, all_bdays, fins_series, exclude_codes, master_dates)
        write_snapshot_csv(snapshot_dir / f"score_{snap[:6]}.csv", df, snap)
        all_score_rows.append({
            **{k: (json.dumps(v, ensure_ascii=False) if k == "universe_stats" else v) for k, v in diag.items()},
            "excluded_prior_tob_n": len(exclude_codes),
        })
        print(
            f"[ref-obs] {snap}: master={diag['master_date_used']} universe={diag['universe_n']} "
            f"母集団除外(既存TOB)={len(exclude_codes)} "
            f"eligible={diag['eligible_n']} top50={diag['top50_n']}", flush=True,
        )

        top_codes = df.loc[df["top50_flag"], "code"].tolist()
        control_codes = df.loc[df["eligible"] & ~df["top50_flag"], "code"].tolist()

        # 63営業日ラベル窓（2022-09以降は2022-11-30で右側打切り）
        snap_idx = bday_index[snap]
        raw_label_end_idx = snap_idx + FORWARD_LABEL_WINDOW_BD
        raw_label_end_bday = all_bdays[raw_label_end_idx] if raw_label_end_idx < len(all_bdays) else all_bdays[-1]
        effective_label_end = min(raw_label_end_bday, LABEL_CUTOFF_DATE)
        censored = effective_label_end < raw_label_end_bday
        censored_bdays = 0
        if censored:
            censored_bdays = sum(1 for d in all_bdays[snap_idx + 1: raw_label_end_idx + 1] if d > effective_label_end)
        censor_diag_rows.append({
            "snapshot": snap, "raw_label_end_bday": raw_label_end_bday,
            "effective_label_end": effective_label_end, "censored": censored,
            "censored_bdays": censored_bdays, "window_bdays": FORWARD_LABEL_WINDOW_BD,
            "censored_fraction": censored_bdays / FORWARD_LABEL_WINDOW_BD,
        })

        # §7-AE-v2凍結項目3/4: score/control両群にanalysis_group4値+同一規則ラベルを付与
        # （両群の所属は選出順序を問わず許容・重複銘柄はseen_*_codesの交差で診断報告する）。
        for code in top_codes:
            first_sel = code not in seen_score_codes
            group = "score_primary" if first_sel else "score_repeat_ref_only"
            seen_score_codes.add(code)
            label_rows.append(_build_label_row(
                snap, code, group, first_sel, deal_idx, bday_index,
                effective_label_end, raw_label_end_bday, censored,
            ))
            row = _forward_return_row(code, group, snap, bday_index, all_bdays)
            if row is not None:
                forward_rows.append(row)
        for code in control_codes:
            first_sel = code not in seen_control_codes
            if first_sel:
                # ブロッカー2: control_primary初回選出の内訳診断（ever_*は本snapshot処理前の
                # 状態=過去snapshotまでの累積。3分類は排他的かつ網羅的なはず＝seen_control_codes
                # の更新規則上、「過去に一度でもeligible∧rank51+だった」なら既にseen済みで
                # first_sel=Falseになるため、それ以外の3経路のみがfirst_sel=Trueを生む）。
                if code in seen_score_codes:
                    composition_reason = "prior_score"          # score→control遷移
                elif code not in ever_universe_codes:
                    composition_reason = "prior_universe_absent"  # TOP500新規流入（過去に在籍なし）
                elif code not in ever_eligible_codes:
                    composition_reason = "prior_eligible_false"   # 過去在籍したが特徴未利用可能だった
                else:
                    composition_reason = "other_unclassified"     # 想定外（診断用の安全網・0件が期待値）
                control_composition_rows.append({
                    "snapshot": snap, "code": code, "composition_reason": composition_reason,
                })
            group = "control_primary" if first_sel else "control_repeat_ref_only"
            seen_control_codes.add(code)
            label_rows.append(_build_label_row(
                snap, code, group, first_sel, deal_idx, bday_index,
                effective_label_end, raw_label_end_bday, censored,
            ))
            row = _forward_return_row(code, group, snap, bday_index, all_bdays)
            if row is not None:
                forward_rows.append(row)

        # ever_*は「このsnapshotより前」の状態を意味するため、本snapshot分の更新は
        # 上記の判定が終わった後（次のsnapshotの判定に使う）に行う。
        ever_universe_codes.update(df["code"].tolist())
        ever_eligible_codes.update(df.loc[df["eligible"], "code"].tolist())

    dual_membership_n = len(seen_score_codes & seen_control_codes)
    control_composition_df = pd.DataFrame(control_composition_rows)

    fwd_df = pd.DataFrame(forward_rows)
    label_df = pd.DataFrame(label_rows)
    censor_df = pd.DataFrame(censor_diag_rows)
    audit_df = pd.DataFrame(resolution_audit)

    output_dir.mkdir(parents=True, exist_ok=True)
    fwd_df.to_csv(output_dir / "forward_returns_all_snapshots.csv", index=False)
    label_df.to_csv(output_dir / "tob_labels.csv", index=False)
    censor_df.to_csv(output_dir / "censoring_diagnostics.csv", index=False)
    pd.DataFrame(all_score_rows).to_csv(output_dir / "snapshot_diagnostics.csv", index=False)
    audit_df.to_csv(output_dir / "secCode_resolution_audit_v2.csv", index=False)
    control_composition_df.to_csv(output_dir / "control_primary_composition_diagnostics.csv", index=False)
    write_manual_confirmation_sensitivity_template(
        output_dir / "manual_confirmation_sensitivity_template.csv"
    )

    write_reference_report(
        output_dir / "reference_observation_report.md",
        snapshots, fwd_df, label_df, censor_df, deal_diag, deals, edinet_records,
        manifest, dual_membership_n, control_composition_df,
    )
    print(f"[ref-obs] 完了。レポート: {output_dir / 'reference_observation_report.md'}", flush=True)


def _label_rate_stats(label_df: pd.DataFrame, group: str) -> dict[str, Any]:
    """analysis_group==group かつ first_selection_flag==True（主解析）の行でTOBラベル率を計算する。"""
    sub = label_df[(label_df["analysis_group"] == group) & (label_df["first_selection_flag"])]
    n = len(sub)
    n_labeled = int(sub["tob_labeled"].sum()) if n else 0
    rate = n_labeled / n if n else 0.0
    return {"n": n, "n_labeled": n_labeled, "rate": rate}


def write_reference_report(
    path: Path, snapshots: list[str], fwd_df: pd.DataFrame, label_df: pd.DataFrame,
    censor_df: pd.DataFrame, deal_diag: dict, deals: dict, edinet_records: list[dict],
    manifest: dict, dual_membership_n: int, control_composition_df: pd.DataFrame,
) -> None:
    lines = [
        "# §7-AE-v2 (i) 歴史参考観測 — TOB候補スコアv2",
        "",
        f"生成日時: {jq_fetch.now_jst().isoformat()}",
        f"期間: {REFERENCE_START_MONTH} 〜 {REFERENCE_END_MONTH}（{len(snapshots)}スナップショット・月初第1営業日）",
        "",
        "> **§6正式verdictなし**（月数不足=structurally_capped_n・§7-C浮動株proxyと同型の参考観測）。",
        "> trials.jsonl へは本スクリプトから一切書き込んでいない（team lead監査後に台帳2行"
        "=v1 invalidated + v2 reference_observation を追記）。",
        "> **v1無効化**: PBR=(AdjC×ShOutFY)÷Eq の単位不整合により verdict=invalidated"
        "（invalidation_reason=specification_unit_mismatch）。本レポートはv2（生値C使用・"
        "master as-of FATAL検証・analysis_group4値・PIT名寄せ）の結果。",
        "",
        "## 頻度報告（書類件数 ≠ 案件数・§7-AE-v2凍結項目8: 再現可能な段階表）",
        "",
        "> 「632件」は凍結前の非公式概数で走査条件・期間・重複排除規則が保存されておらず、"
        "正式な段階表の母数には使わない（参考注記のみ）。以下が再現可能な正式値"
        f"（入力manifest_hash={manifest['manifest_hash']}・"
        f"走査ファイル数={manifest['n_files']}・{manifest['start']}〜{manifest['end']}）。",
        "",
        f"- EDINET documents_all 走査範囲: {EDINET_START_BD} 〜 {LABEL_CUTOFF_DATE}（総レコード{len(edinet_records)}件）",
        f"- 公開買付関連書類（docTypeCode 240/250/260/270/280）: **{deal_diag['raw_tob_type_docs']}件**",
        f"  - うち subjectEdinetCode 無し（対象外）: {deal_diag['excluded_no_subject']}件",
        f"  - うち 自己TOB（edinetCode==subjectEdinetCode・除外）: {deal_diag['excluded_self_tender']}件",
        f"  - 残る distinct subjectEdinetCode数（監査対象92相当）: {deal_diag['distinct_subject_edinet_codes']}",
        f"- PIT名寄せで**機械一意解決 = {deal_diag['resolved_deals']}件**",
        f"  - unresolved（正規化名の一致0件）= {deal_diag['unresolved_n']}件",
        f"  - ambiguous（正規化名が複数Codeに一致・不採用）= {deal_diag['ambiguous_n']}件",
        f"  - no_master_available（submitDate以前のmasterが1件も無い）= {deal_diag['no_master_available_n']}件",
        f"- **書類{deal_diag['raw_tob_type_docs']}件 ≠ 案件{deal_diag['resolved_deals']}件**"
        f"（複数書類が同一案件に束ねられるため。訂正届出書・撤回届出書・報告書等は同一案件の"
        f"後続書類として統合され、新規イベントとしては数えない）。",
        f"- 全{deal_diag['distinct_subject_edinet_codes']}対象会社の resolved/unresolved/ambiguous"
        "と根拠は `secCode_resolution_audit_v2.csv` に記録（正規化名・使用master日・候補数・"
        "判定理由を1行ずつ）。人手復元センシティビティ分析は"
        "`manual_confirmation_sensitivity_template.csv`（空テンプレート・本バッチでは未実施）。",
        "",
    ]
    n_unresolved_total = deal_diag["unresolved_n"] + deal_diag["ambiguous_n"] + deal_diag["no_master_available_n"]
    if n_unresolved_total > 0:
        lines += [
            f"> ⚠️ **未解決{n_unresolved_total}件が残るため、下記「副次アウトカム: TOBラベル濃縮」の"
            "数字は選択バイアスにより方向不明と表記する**（§7-AE-v2凍結項目5: 過小評価と断定しない）。"
            "PIT名寄せでも解決できない残差は、真の名称変更・表記揺れ・masterに存在しない銘柄"
            "（未上場化前後の別法人扱い等）が原因と推測されるが、方向（過大/過小のいずれに"
            "偏るか）を機械的に断定する根拠はない。", "",
        ]

    lines += [
        "## 主要アウトカム: 20営業日フォワードリターン（T+1寄付起点・コスト0.3%込・§0標準）",
        "",
        "**主解析（疑似反復排除・銘柄ごと初回選出のみ・score_primary vs control_primaryの"
        "対称比較=§7-AE-v2凍結項目3/4）**:",
        "",
    ]
    header = (
        "| 群 | N | P(+20%終値) | P(+30%) | 中央値 | 平均 | EV(コスト込) | タッチ率(-8/-10/-15) |\n"
        "|---|---|---|---|---|---|---|---|"
    )
    lines.append(header)

    def _row(label: str, df: pd.DataFrame) -> str:
        s = mbr.summarize(df)
        if s["n"] == 0:
            return f"| {label} | 0 | - | - | - | - | - | - |"
        return (
            f"| {label} | {s['n']} | {s['p20']:.1%} | {s['p30']:.1%} | {s['median_ret']:.1%} | "
            f"{s['mean_ret']:.1%} | {s['ev_none']:.1%} | {s['touch8']:.1%}/{s['touch10']:.1%}/{s['touch15']:.1%} |"
        )

    if not fwd_df.empty:
        score_primary = fwd_df[fwd_df["group"] == "score_primary"]
        control_primary = fwd_df[fwd_df["group"] == "control_primary"]
        lines.append(_row("スコア群(score_primary)", score_primary))
        lines.append(_row("対照群(control_primary)", control_primary))
        lines += ["", "**参考併記（全月次系列・検定には不使用・疑似反復あり）**:", "", header]
        all_score = fwd_df[fwd_df["group"].isin(["score_primary", "score_repeat_ref_only"])]
        all_control = fwd_df[fwd_df["group"].isin(["control_primary", "control_repeat_ref_only"])]
        lines.append(_row("スコア群(全月次・参考)", all_score))
        lines.append(_row("対照群(全月次・参考)", all_control))
        lines += [
            "", f"- 両群重複銘柄数（score系とcontrol系の両方に選出履歴がある銘柄・順序不問）: "
            f"**{dual_membership_n}件**",
        ]
    else:
        lines.append("| (シグナルなし) | 0 | - | - | - | - | - | - |")

    # ブロッカー2（2026-07-18 Codex実装後レビュー）: control_primaryは同期月マッチした対照では
    # なく初回観測コホート間の記述的比較である旨を、月別件数と内訳診断で具体的に示す。
    lines += [
        "", "### score_primary / control_primary の月別件数（構成注記・§7-AE-v2凍結項目3補足）",
        "",
        "> ⚠️ **control_primaryは同期月マッチした対照群ではなく、初回観測コホート間の記述的比較**"
        "である。後月のcontrol_primaryにはTOP500新規流入・特徴利用可能化（fins開示到達）・"
        "score→control遷移が混在する（下表の内訳参照。高モメンタム偏重等の方向性を断定する"
        "根拠はない・単なる構成の記述）。",
        "",
    ]
    if not label_df.empty:
        score_by_month = (
            label_df[(label_df["analysis_group"] == "score_primary") & (label_df["first_selection_flag"])]
            .groupby("snapshot").size()
        )
        control_by_month = (
            label_df[(label_df["analysis_group"] == "control_primary") & (label_df["first_selection_flag"])]
            .groupby("snapshot").size()
        )
        lines.append("| snapshot | score_primary件数 | control_primary件数 |")
        lines.append("|---|---|---|")
        for snap in snapshots:
            lines.append(f"| {snap[:6]} | {int(score_by_month.get(snap, 0))} | {int(control_by_month.get(snap, 0))} |")
        score_total_n = int(score_by_month.sum())
        control_total_n = int(control_by_month.sum())
        first_month = snapshots[0]
        score_first_n = int(score_by_month.get(first_month, 0))
        control_first_n = int(control_by_month.get(first_month, 0))
        lines.append(f"| **合計** | **{score_total_n}** | **{control_total_n}** |")
        lines += [
            "",
            f"- score_primary: {first_month[:6]}集中 {score_first_n}/{score_total_n}"
            f"（{score_first_n / score_total_n:.1%}）" if score_total_n else "- score_primary: N=0",
            f"- control_primary: {first_month[:6]}集中 {control_first_n}/{control_total_n}"
            f"（{control_first_n / control_total_n:.1%}）" if control_total_n else "- control_primary: N=0",
            "",
        ]

        later_composition = control_composition_df[control_composition_df["snapshot"] != first_month]
        later_n = len(later_composition)
        lines.append(
            f"**control_primary 後月（{first_month[:6]}を除く）{later_n}件の内訳"
            "（排他的分類・`control_primary_composition_diagnostics.csv`参照）**:"
        )
        lines.append("")
        lines.append("| 理由 | 件数 | 割合 |")
        lines.append("|---|---|---|")
        reason_labels = {
            "prior_score": "score→control遷移（過去にscore群選出歴あり）",
            "prior_universe_absent": "TOP500新規流入（過去にuniverse在籍なし）",
            "prior_eligible_false": "特徴利用可能化（過去にuniverse在籍したがeligible=False）",
            "other_unclassified": "分類不能（想定外・要調査）",
        }
        reason_counts = later_composition["composition_reason"].value_counts() if later_n else pd.Series(dtype=int)
        for reason_key, reason_label in reason_labels.items():
            n = int(reason_counts.get(reason_key, 0))
            pct = f"{n / later_n:.1%}" if later_n else "-"
            lines.append(f"| {reason_label} | {n} | {pct} |")
        if int(reason_counts.get("other_unclassified", 0)) > 0:
            lines.append(
                "> ⚠️ other_unclassifiedが1件以上あります（3分類で説明しきれない経路が存在する"
                "ことを意味する・要調査）。"
            )
    else:
        lines.append("(シグナルなし)")

    lines += [
        "", "## 副次アウトカム: TOBラベル濃縮（63営業日以内・2022-11-30打切り・score_primary vs control_primary）",
        "",
    ]
    if not label_df.empty:
        score_stats = _label_rate_stats(label_df, "score_primary")
        control_stats = _label_rate_stats(label_df, "control_primary")
        diff = score_stats["rate"] - control_stats["rate"]
        ratio = (score_stats["rate"] / control_stats["rate"]) if control_stats["rate"] > 0 else float("nan")
        lines += [
            f"- スコア群(score_primary): N={score_stats['n']} ラベル成立={score_stats['n_labeled']}件"
            f"（{score_stats['rate']:.2%}）",
            f"- 対照群(control_primary): N={control_stats['n']} ラベル成立={control_stats['n_labeled']}件"
            f"（{control_stats['rate']:.2%}）",
            f"- **差(score-control) = {diff:+.2%}pt / 比(score/control) = "
            f"{'計算不能(control=0)' if control_stats['rate'] == 0 else f'{ratio:.2f}倍'}**",
            "",
        ]
        n_censored_snaps = int(censor_df["censored"].sum())
        lines.append(
            f"- 右側打切り対象スナップショット: {n_censored_snaps}/{len(censor_df)}件"
            f"（63bd窓が2022-11-30を超える月。個別打切り率は censoring_diagnostics.csv 参照）"
        )
        if n_censored_snaps > 0:
            censored_snaps = censor_df[censor_df["censored"]]["snapshot"].tolist()
            avg_censor_frac = censor_df[censor_df["censored"]]["censored_fraction"].mean()
            lines.append(
                f"  - 打切りスナップショット: {[s[:6] for s in censored_snaps]}"
                f"・平均打切り率={avg_censor_frac:.1%}（63bd窓のうちこの割合が観測不能。"
                "「観測可能日数内で未発生=未発生」として上記率の分母に含めている）"
            )

        # 感度分析: 完全63bd観測可能なスナップショットのみ（§7-AE-v2凍結項目3の事前指定感度分析）
        uncensored_snaps = set(censor_df[~censor_df["censored"]]["snapshot"])
        label_df_full = label_df[label_df["snapshot"].isin(uncensored_snaps)]
        score_full = _label_rate_stats(label_df_full, "score_primary")
        control_full = _label_rate_stats(label_df_full, "control_primary")
        lines += [
            "", "**感度分析（完全63bd観測可能なスナップショットのみ・打切りスナップショット除外・事前指定）**:",
            f"- スコア群: N={score_full['n']} ラベル成立={score_full['n_labeled']}件（{score_full['rate']:.2%}）",
            f"- 対照群: N={control_full['n']} ラベル成立={control_full['n_labeled']}件（{control_full['rate']:.2%}）",
        ]
    else:
        lines.append("- スコア群シグナルなし（ラベル計測不能）")

    lines += [
        "", "## 実装ノート・既知の制約",
        "- **【v1→v2 単位整合修正・継承】PBR算出の時価総額は無調整の生値C×ShOutFY（生株式数）"
        "を使用**（凍結本文の字義=AdjC[調整済み株価]×ShOutFYは単位混在で、後年の株式分割があった"
        "銘柄では時価総額が分割比率分だけ歪む。実例: 58010古河電工・2022-05-31、生値C=2155.0円/"
        "調整済みAdjC=215.5円[後年1/10分割の遡及調整]、旧v1実装ではPBR=0.0485・netcash=4.44と"
        "誤算出され分割銘柄だけが不当にランキング上位を占拠していた。修正後は58010のPBR=0.4849・"
        "netcash=0.4441で手計算と一致。**20営業日フォワードリターンは元々AdjC使用のmbr側関数の"
        "ままで変更していない**（スコア算出とリターン算出は別関数・別目的）。",
        "- **【v2新規】masterのas-of規則を明文化しFATAL検証**: ProdCat分類は"
        "「snapshot日より前で最新のmaster」を使用し、**master_date_used == 前営業日 でなければ"
        "compute_snapshot_scoreがFATALで停止する**（黙ってさらに古いmasterに遡らない）。"
        "各スナップショットで実際に使ったmaster日は snapshot_diagnostics.csv の"
        "`master_date_used`列で確認できる（全16件で前営業日と一致・FATAL未発生）。",
        "- **【v2新規】analysis_group4値**: score_primary/score_repeat_ref_only/control_primary/"
        "control_repeat_ref_only。主解析対照群=銘柄ごと全期間で初めてeligible∧rank51位以下に"
        "なった月のみ（v1は全月次控除群を無差別に使っておりscoreとcontrolで疑似反復除去の"
        "扱いが非対称だった）。両群所属は選出順序を問わず許容（上記「両群重複銘柄数」参照）。",
        "- **【v2新規】secCode解決を名寄せPIT復元に置き換え**: v1はEDINET単一スナップショットの"
        "証券コード列を参照しており、TOB成立→上場廃止した対象会社ほど証券コード欄が空になり"
        "系統的に取りこぼす欠陥があった（2026-07-18実データで発見・非上場1266件全件で証券コード"
        "欄が空である一方、提出者名は1266件全件で残存することを確認）。v2は正規化会社名を"
        "「初回submitDate以前で最新のJ-Quants master」に対して機械的に一意一致させる方式に"
        "置き換え、92対象会社全件の判定根拠を監査CSVに記録する。",
        "- 母集団除外・ラベル判定は同一のTOB案件統合テーブル（subjectEdinetCode→初回submitDateTime）を使用。",
        "- **【診断値の定義】`snapshot_diagnostics.csv`の`excluded_from_universe_n`列**: "
        "売買代金トレーリング窓+ProdCat分類によるTOP500候補のうち、既存TOB案件テーブルとの"
        "突合で母集団から除外された銘柄数（=当該snapshotのdf行数に現れない銘柄）。標準出力に"
        "印字される「母集団除外(既存TOB)」（exclude_codes全体の件数＝過去分含む既知TOB対象会社"
        "全銘柄数）とは異なる、**より狭い数字**（TOP500候補に実際に入っていた銘柄のみを数える）"
        "であることに注意（2026-07-18 Codex実装後レビューMINOR: 両者の混同防止のため明記）。",
        "- EDINET実データは2021-07-07以降のみ有効（ローリング窓の実測境界）。それ以前に発生した"
        "TOB案件は母集団除外の対象にできない（データ制約・理論上の残存リスクとして明記）。",
        "- 初回書類submitDateTime基準でラベルの時刻を確定しているが、TDnet先行公表により実際の"
        "市場認知はEDINET提出より早いことが多い（時刻精度の限界）。",
        "- (ii)前向き観察の月次run-log(`data/monitoring/tob_forward/run_log.jsonl`)はdaily証跡"
        "チェーン(KPI_DEPENDENCY_TABLE)には配線していない（2026-07-18 Batch D安全性検証で"
        "配線した場合に本番daily証跡が壊れることを実測確認済み・run_forward手前のコード"
        "コメント参照）。証跡実装完了までforwardは「準備済み・未稼働」"
        "（台帳にforward_status=prepared_not_activeと明記予定）。",
        "- スコア式は§7-AE-v2本文の凍結値をそのまま機械適用（グリッド探索・重み推定なし）。",
        "- 全統計量は記述的指標であり確証的p値ではない（§6正式verdictなしの参考観測）。",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- (ii) 前向き観察の月次run-log（§7-AE-v2凍結項目6・新規） -----------------------


def _canonical_json(d: dict[str, Any]) -> str:
    """§7-AE-v2凍結: UTF-8・キー昇順・separators(",",":")のcanonical JSON文字列を返す
    （row_hashフィールドは呼び出し側で除外してから渡すこと）。"""
    return json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row_hash(row_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(row_without_hash).encode("utf-8")).hexdigest()


def _forward_runlog_read_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            rows.append(json.loads(ln))
    return rows


def _forward_runlog_verify_chain(rows: list[dict[str, Any]]) -> str:
    """全行のchain連結・自己hash整合を検証し、次に追記すべきprev_hashを返す（§7-AE-v2凍結項目6・
    2026-07-18 Codex実装後レビューブロッカー3修正）。

    修正前は最終行のみ検証していたため、末尾より前の行が改ざんされてもtail自身のhashが
    整合していれば検知できなかった（例: 1行目のstatus/hashを書き換え、2行目以降のprev_hash
    連結だけ辻褄を合わせても、tail-onlyチェックは通過してしまう）。月次カデンスで行数が
    少ないため全chain再検証のコストは無視できる（凍結文言の意図＝改ざん検知を確実にする
    ための強化・凍結ルール自体の変更ではない）。

    行が0件ならgenesis。1件以上あれば各行について
        (a) 自己hash整合: canonical JSON(row_hash除外)から再計算したhashが保存row_hashと一致
        (b) 連結整合: 0行目はprev_hash==genesis、i行目(i>=1)はprev_hash==rows[i-1]のrow_hash
    をこの順に検証し、いずれか1件でも不一致ならFATAL（改ざん検知）。
    """
    prev_expected = FORWARD_RUNLOG_GENESIS_HASH
    for i, row in enumerate(rows):
        stored_hash = row.get("row_hash")
        recomputed = _row_hash({k: v for k, v in row.items() if k != "row_hash"})
        if stored_hash != recomputed:
            raise SystemExit(
                f"FATAL: data/monitoring/tob_forward/run_log.jsonl {i}行目のrow_hashが再計算値と"
                f"不一致です（改ざん検知・§7-AE-v2凍結項目6）。保存値={stored_hash} 再計算値={recomputed}"
            )
        if row.get("prev_hash") != prev_expected:
            raise SystemExit(
                f"FATAL: data/monitoring/tob_forward/run_log.jsonl {i}行目のprev_hashがchain期待値"
                f"と不一致です（改ざん検知・chain切断・§7-AE-v2凍結項目6）。"
                f"期待値={prev_expected} 実際値={row.get('prev_hash')}"
            )
        prev_expected = stored_hash
    return prev_expected


def _forward_runlog_find_success_row(rows: list[dict[str, Any]], target_month: str) -> Optional[dict[str, Any]]:
    for row in rows:
        if row.get("target_month") == target_month and row.get("status") == "success":
            return row
    return None


def append_forward_run_log(
    run_log_path: Path, row_fields: dict[str, Any],
    stage_csv: Optional[tuple[Path, Path]] = None,
) -> dict[str, Any]:
    """月次run-log(data/monitoring/tob_forward/run_log.jsonl)へ1行を原子的に追記する
    （§7-AE-v2凍結項目6の恒久規則）。

    規則: genesis prev_hash=64個の"0" / canonical JSON=UTF-8キー昇順separators(",",":") /
    row_hash=SHA256(canonical_json・row_hash自体は除く) / append前に全chain連結を検証し
    不一致はFATAL（_forward_runlog_verify_chain参照）/ flock排他+flush+fsync / 同月成功行が
    既存かつ入力・スクリプト・出力hash全一致=no-op、1つでも不一致なら上書きせずFATAL /
    失敗実行もstatus=failedで記録（呼び出し側の責務）。

    Args:
        stage_csv: (stage_path, final_path) のペア。指定時は**冪等判定が確定してから初めて**
            stage_path.replace(final_path) で最終CSVへ昇格する（2026-07-18 Codex実装後
            レビューブロッカー4修正: 冪等判定より前に最終ファイルを書き換えない）。
            no-op（既存成功行と全hash一致）またはFATAL（不一致）の場合はstage_pathを削除し、
            final_pathには一切触れない。status!="success"（failed行等）では常にNone。

    row_fields には target_month・status・その他記録フィールドを含める
    （prev_hash/row_hashは本関数が付与する）。

    Returns:
        実際に書き込んだ（またはno-opで既存採用した）行のdict。
    """
    run_log_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = run_log_path.parent / f".{run_log_path.name}.lock"
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            rows = _forward_runlog_read_lines(run_log_path)
            prev_hash = _forward_runlog_verify_chain(rows)

            if row_fields.get("status") == "success":
                existing = _forward_runlog_find_success_row(rows, row_fields["target_month"])
                if existing is not None:
                    same = (
                        existing.get("input_hashes") == row_fields.get("input_hashes")
                        and existing.get("script_hash") == row_fields.get("script_hash")
                        and existing.get("output_csv_hash") == row_fields.get("output_csv_hash")
                    )
                    if stage_csv is not None and stage_csv[0].exists():
                        stage_csv[0].unlink()  # 昇格しない＝最終CSVには一切触れない
                    if same:
                        print(
                            f"[forward-runlog] {row_fields['target_month']} は既存成功行と"
                            f"全hash一致のためno-op（最終CSV未変更）。", flush=True,
                        )
                        return existing
                    raise SystemExit(
                        f"FATAL: {row_fields['target_month']} の既存成功行とhashが不一致です"
                        f"（上書きしない・最終CSV未変更・§7-AE-v2凍結項目6の冪等ガード）。"
                        f"既存input_hashes={existing.get('input_hashes')} / "
                        f"今回input_hashes={row_fields.get('input_hashes')}"
                    )
                if stage_csv is not None:
                    stage_path, final_path = stage_csv
                    stage_path.replace(final_path)  # 冪等判定確定後に初めて最終CSVへ昇格

            new_row = dict(row_fields)
            new_row["prev_hash"] = prev_hash
            new_row["row_hash"] = _row_hash(new_row)

            line = _canonical_json(new_row)
            with open(run_log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            return new_row
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# --- (ii) 前向き観察: 当月スナップショットのみ記録 --------------------------------
#
# 【2026-07-18 team lead裁定・Batch D証跡チェーン安全性検証の結論・v2でも継続】
# scripts/kpi_run_evidence.py の KPI_DEPENDENCY_TABLE には**配線しない**（file-only記録+
# 独立の月次run-logに留める。上記append_forward_run_log参照）。
#
# 根拠（実測dry-run。data/monitoring/run_log.jsonl・kpi_run_evidence.pyは一切変更していない）:
#   1. 直近本番run_log.jsonl最終行のinputsキー実測 = {bars, fins, master, shortsale, topix}
#      （daily_screen.pyが07:30実行時に実際に組み立てる集合。EDINET系は含まれない）
#   2. KPI_DEPENDENCY_TABLEに tob_candidate_score 行を追加する場合、本スコアが実際に依存する
#      EDINET documents_all を inputs タプルに正直に含めないと「黙って欠落させない」という
#      assert_inputs_cover_dependency_table() の設計原則(A-6/A-7)に反する。
#   3. inputsに "edinet_documents_all" を正直に加えて ALL_DEPENDENCY_TABLE_INPUTS を再計算し、
#      本番run_logの実inputsキー集合と比較したところ missing=['edinet_documents_all'] となり、
#      assert_inputs_cover_dependency_table() は AssertionError を送出する（実行して確認済み）。
#      → 配線した状態で次回07:32本番runが走ると、daily_screen.py側のinputs組み立てが未修正な限り
#        毎日の証跡チェーンそのものがFAILする（本番影響・不可逆に近いリスク）。
#   4. 回避には daily_screen.py 側のinputs組み立てに "edinet_documents_all"（null+null_reason）を
#      恒常的に追加する必要があるが、これは本番07:30/07:32パイプラインそのものの改修であり
#      本スクリプトのスコープ外・別途レビューが必要な変更（フォローアップとして提案・未実施）。
#   5. 結論: daily KPI_DEPENDENCY_TABLEには配線せず、§7-AE-v2凍結項目6の独立月次run-log
#      （data/monitoring/tob_forward/run_log.jsonl）のみで証跡化する
#      （trials.jsonl・daily run_log.jsonlのいずれにも書き込まない）。


def run_forward(output_dir: Path, monitoring_dir: Path) -> int:
    """当月の月初第1営業日に一致する場合のみスコアを計算して記録する（非競走観察枠・正式判定なし）。

    launchd から毎月1〜10日に起動される想定。当日が月初第1営業日でなければ何もせず0で終了する
    （凍結仕様「毎月初のスコア上位50を証跡下で記録」の「毎月初」判定をここで自己完結させる）。

    **証跡チェーン非配線**（2026-07-18裁定・上記コメント参照）: kpi_run_evidence.append_run_log()
    は一切呼ばない。代わりに§7-AE-v2凍結項目6の独立月次run-log
    （data/monitoring/tob_forward/run_log.jsonl）へ成功/失敗いずれもstatus付きで記録する。
    """
    calendar_days = mbr.load_calendar_days()
    all_bdays = mbr.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    today_bd = jq_fetch.now_jst().strftime("%Y%m%d")
    this_month = today_bd[:6]

    starts = mbr.month_starts_in_range(calendar_days, f"{this_month[:4]}-{this_month[4:6]}", f"{this_month[:4]}-{this_month[4:6]}")
    if not starts or starts[0] != today_bd:
        expected = starts[0] if starts else "不明"
        print(f"[forward] 本日({today_bd})は当月の月初第1営業日({expected})ではありません。何もせず終了します。", flush=True)
        return 0

    month_start_bday = today_bd
    run_started_at = jq_fetch.now_jst().isoformat()
    run_log_path = monitoring_dir / "run_log.jsonl"

    try:
        idx = bday_index.get(month_start_bday)
        if idx is None or idx == 0:
            raise SystemExit(f"FATAL: {month_start_bday} が営業日カレンダー上で解決できません。")
        prev_bday = all_bdays[idx - 1]

        fe_start = kus.FINS_HISTORY_START_BD
        print(f"[forward] fins as-of シリーズ構築中 {fe_start}..{prev_bday} ...", flush=True)
        fins_series = build_fins_feature_series(fe_start, month_start_bday, all_bdays)

        edinet_end = min(prev_bday, jq_fetch.now_jst().strftime("%Y%m%d"))
        print(f"[forward] EDINET documents_all 読み込み中 {EDINET_START_BD}..{edinet_end} ...", flush=True)
        edinet_records = load_edinet_documents_all(EDINET_START_BD, edinet_end)
        edinet_registry = load_edinet_code_master_registry(edinet_fetch.EDINET_CODE_MASTER_PATH)
        master_dates = available_master_dates()
        deals, deal_diag, audit = build_tob_deal_table(edinet_records, edinet_registry, master_dates)
        deal_idx = deals_by_code_index(deals)
        exclude_codes = {c for c, d in deal_idx.items() if d <= month_start_bday}

        df, diag = compute_snapshot_score(
            month_start_bday, bday_index, all_bdays, fins_series, exclude_codes, master_dates
        )
        # master_date_used == prev_bday は compute_snapshot_score 内部でFATAL検証済み。

        out_path = monitoring_dir / f"score_{month_start_bday[:6]}.csv"
        # ブロッカー4修正: まずstageに書くのみ（最終pathへはappend_forward_run_logがlock下で
        # 冪等判定した後にしか昇格しない）。
        stage_path, stage_hash = stage_forward_csv(out_path, df, month_start_bday)
        # (i)側 output_dir にも参考複製を残す（証跡run-logの対象外・単なる利便性コピー）。
        write_snapshot_csv(output_dir / "snapshots" / f"score_{month_start_bday[:6]}.csv", df, month_start_bday)

        # ブロッカー5修正: 実際に読み込んだ全入力ファイルを網羅する（旧実装はbars=前営業日1件・
        # fins=hash未計上・PIT名寄せ用master群も未計上だった）。
        win_days = all_bdays[idx - UNIVERSE_WINDOW + 1: idx + 1]  # build_universeが実際に読むbars範囲
        fins_scan_days = [d for d in all_bdays if fe_start <= d <= month_start_bday]  # build_fins_feature_seriesと同一範囲
        pit_master_dates = sorted({r["master_date_used"] for r in audit if r.get("master_date_used")})
        edinet_documents_manifest = compute_edinet_manifest_hash(EDINET_START_BD, edinet_end, all_bdays)

        input_hashes = {
            "calendar_hash": run_evidence.compute_file_hash(jq_fetch.DATA_ROOT / "calendar.json.gz"),
            "bars_manifest": compute_bars_manifest_hash(win_days),
            "fins_manifest": compute_fins_manifest_hash(fins_scan_days),
            "score_master_hash": run_evidence.compute_file_hash(
                jq_fetch.DATA_ROOT / "master" / f"{diag['master_date_used']}.json.gz"
            ),
            "pit_master_manifest": _build_file_manifest_hash(
                {d: jq_fetch.DATA_ROOT / "master" / f"{d}.json.gz" for d in pit_master_dates}
            ),
            "edinet_documents_manifest": edinet_documents_manifest,
            "edinet_code_master_hash": run_evidence.compute_file_hash(edinet_fetch.EDINET_CODE_MASTER_PATH),
        }
        script_hash = run_evidence.compute_code_tree_hash()["value"]

        row = {
            "target_month": month_start_bday[:6], "target_snapshot_bday": month_start_bday,
            "run_started_at": run_started_at, "run_finished_at": jq_fetch.now_jst().isoformat(),
            "status": "success", "master_date_used": diag["master_date_used"],
            "cutoff_date": edinet_end, "input_hashes": input_hashes, "script_hash": script_hash,
            "output_csv_path": str(out_path), "output_csv_hash": stage_hash,
            "row_count": int(len(df)), "top50_n": int(diag["top50_n"]), "error_message": None,
        }
        recorded = append_forward_run_log(run_log_path, row, stage_csv=(stage_path, out_path))

        print(
            f"[forward] {month_start_bday}: master={diag['master_date_used']} universe={diag['universe_n']} "
            f"母集団除外(既存TOB)={len(exclude_codes)} eligible={diag['eligible_n']} top50={diag['top50_n']} "
            f"row_hash={recorded.get('row_hash', '')[:16]}... → {out_path}", flush=True,
        )
        return 0
    except (SystemExit, Exception) as e:
        # SystemExit は BaseException 直系（Exceptionを継承しない）ため、本ファイル内で多用する
        # `raise SystemExit(...)` によるFATALをここで確実に捕捉するには両方を明示的に列挙する
        # 必要がある（2026-07-18 Codex実装後レビューMINOR: 成功/失敗いずれの経路でも
        # 同一のfailure_row形状・同一のappend_forward_run_log呼び出しで記録することで統一する）。
        try:
            failure_row = {
                "target_month": month_start_bday[:6], "target_snapshot_bday": month_start_bday,
                "run_started_at": run_started_at, "run_finished_at": jq_fetch.now_jst().isoformat(),
                "status": "failed", "master_date_used": None, "cutoff_date": None,
                "input_hashes": None, "script_hash": run_evidence.compute_code_tree_hash()["value"],
                "output_csv_path": None, "output_csv_hash": None, "row_count": None, "top50_n": None,
                "error_message": str(e),
            }
            append_forward_run_log(run_log_path, failure_row)  # stage_csv=None固定（失敗runはCSVを書かない）
        except Exception as log_err:
            print(f"WARN: 失敗行の記録自体にも失敗しました: {log_err}", file=sys.stderr, flush=True)
        raise


# --- 単月スナップショット（--snapshot用の最小経路） -------------------------------


def run_single_snapshot(month: str, output_dir: Path) -> int:
    calendar_days = mbr.load_calendar_days()
    all_bdays = mbr.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}
    starts = mbr.month_starts_in_range(calendar_days, month, month)
    if not starts:
        raise SystemExit(f"FATAL: {month} の月初第1営業日が見つかりません。")
    month_start_bday = starts[0]

    fe_start = kus.FINS_HISTORY_START_BD
    fins_series = build_fins_feature_series(fe_start, month_start_bday, all_bdays)

    edinet_end = min(month_start_bday, jq_fetch.now_jst().strftime("%Y%m%d"))
    edinet_records = load_edinet_documents_all(EDINET_START_BD, edinet_end)
    edinet_registry = load_edinet_code_master_registry(edinet_fetch.EDINET_CODE_MASTER_PATH)
    master_dates = available_master_dates()
    deals, _deal_diag, _audit = build_tob_deal_table(edinet_records, edinet_registry, master_dates)
    deal_idx = deals_by_code_index(deals)
    exclude_codes = {c for c, d in deal_idx.items() if d <= month_start_bday}

    df, diag = compute_snapshot_score(
        month_start_bday, bday_index, all_bdays, fins_series, exclude_codes, master_dates
    )
    out_path = output_dir / "snapshots" / f"score_{month_start_bday[:6]}.csv"
    write_snapshot_csv(out_path, df, month_start_bday)
    print(
        f"[snapshot] {month_start_bday}: master={diag['master_date_used']} universe={diag['universe_n']} "
        f"母集団除外(既存TOB)={len(exclude_codes)} eligible={diag['eligible_n']} top50={diag['top50_n']} "
        f"→ {out_path}", flush=True,
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="§7-AE-v2 TOB候補スコアv2（EDINET documents_all母集団）")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--snapshot", metavar="YYYY-MM", help="単月スナップショットのみ計算")
    group.add_argument("--range", metavar="YYYY-MM:YYYY-MM", help="(i)歴史参考観測を実行（既定は凍結期間）")
    group.add_argument("--forward", action="store_true", help="(ii)前向き観察: 当月が月初第1営業日ならスコアを記録")
    ap.add_argument("--output-dir", default="output/kpi/tob_candidate_v2")
    ap.add_argument("--monitoring-dir", default="data/monitoring/tob_forward")
    args = ap.parse_args()

    output_dir = Path(args.output_dir)

    if args.snapshot:
        return run_single_snapshot(args.snapshot, output_dir)

    if args.range:
        if args.range.strip() != f"{REFERENCE_START_MONTH}:{REFERENCE_END_MONTH}":
            print(
                f"WARN: --range は凍結期間 {REFERENCE_START_MONTH}:{REFERENCE_END_MONTH} 固定です"
                f"（指定値 {args.range} は無視し、凍結期間で実行します。凍結仕様の事後変更禁止）。",
                file=sys.stderr,
            )
        run_reference_observation(output_dir)
        return 0

    if args.forward:
        return run_forward(output_dir, Path(args.monitoring_dir))

    return 1


if __name__ == "__main__":
    sys.exit(main())
