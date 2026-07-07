#!/usr/bin/env python3
"""アクティビスト大量保有出現シグナル生成器（カタログ§2-E #11・kpi-name: activist_5pct）。

docs/stock-algo-kpi-catalog.md の §2-E「アクティビスト大量保有出現」を実装する。
`data/edinet/YYYYMMDD.json.gz`（scripts/edinet_fetch.py 取得分・docTypeCode 350/360限定の
メタデータ）を読み込み、提出者名(filerName)を `data/activist_dictionary.json` と
時点管理付きで照合し、大量保有報告書(350=新規)の提出日を反応日=翌営業日でシグナル化する。

実装済み（メタデータのみで判定可能な部分）:
    - docTypeCode=350（大量保有報告書=新規の大量保有報告）の辞書マッチ提出
    - 辞書の時点管理（known_since_year <= シグナル発生年のエントリのみ使用・後知恵防止）
    - 対象銘柄コードは EDINET `secCode` フィールドを使用（5桁化して J-Quants Code と直接比較）

未実装（実データ未確認のため意図的に見送り・§実装ノート参照）:
    - docTypeCode=360（変更報告書）の増減方向判定 = 「買い増し」のみへの絞り込み
      （書類本体CSV/XBRLの取得・解析が必要。--include-change-reports で増減不問のまま
      オプトインは可能だが、既定では除外する）
    - 「重要提案行為等」記載による強度2倍フラグ（同じく書類本体が必要・intensity列は
      常に1.0で出力し、TODOとして diag に記録する）
    - APIキー未入手のため `data/edinet/` は空。本スクリプトは実データ到達後に実行する
      （それまでは py_compile と `--dry-run-smoke` によるロジック検証のみ可能）。

フォワードリターン計算・ユニバース判定・ブートストラップCI等は scripts/kpi_event_study.py の
run_event_study() に委譲する（Canonical Module原則・再実装しない）。

Usage:
    python3 scripts/kpi_activist_signals.py --start 2016-11 --end 2022-11
    python3 scripts/kpi_activist_signals.py --start 2016-11 --end 2022-11 --skip-harness
    python3 scripts/kpi_activist_signals.py --dry-run-smoke   # 合成フィクスチャでロジック検証のみ
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import edinet_fetch  # noqa: E402  (Canonical Module: DATA_ROOT/read_json_gz系を再利用)
import jq_fetch  # noqa: E402  (Canonical Module: read_json_gz/カレンダー系を再利用)
import kpi_event_study  # noqa: E402  (Canonical Module: run_event_study を再利用)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー読み込みを再利用)

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# §0確認事項5・§6標本分割で凍結: in-sampleは2016-11〜2022-11。holdout(2023以降)はロック中。
IN_SAMPLE_START = "2016-11"
IN_SAMPLE_END = "2022-11"

KPI_NAME = "activist_5pct"

DEFAULT_DICTIONARY_PATH = Path("data/activist_dictionary.json")

# 法人格・ファンド形態を表すサフィックス（正規化時に除去して部分一致の頑健性を上げる）。
# 大文字化した英字表記にも対応するため大文字トークンも含める。
CORP_SUFFIX_TOKENS = [
    "株式会社", "合同会社", "有限会社", "合資会社", "合名会社",
    "PTE LTD", "PTE. LTD.", "LLC", "LP", "L.P.", "LTD", "LTD.", "INC", "INC.",
    "CO., LTD.", "CO.,LTD.", "CORPORATION", "CORP", "GK",
]


# --- 辞書読み込み・正規化マッチング -------------------------------------------------


def normalize_name(raw: str) -> str:
    """filerName / name_patterns を突き合わせ可能な形へ正規化する。

    Unicode NFKC（全角英数・カナ変種を統一）-> 大文字化 -> 法人格サフィックス除去
    -> 空白/中黒/カンマ除去、の順で処理する。
    """
    if not raw:
        return ""
    s = unicodedata.normalize("NFKC", raw).upper()
    for token in CORP_SUFFIX_TOKENS:
        s = s.replace(token.upper(), "")
    for ch in (" ", "　", "・", ",", ".", "-", "(", ")", "（", "）"):
        s = s.replace(ch, "")
    return s.strip()


def load_activist_dictionary(path: Path) -> list[dict]:
    """activist_dictionary.json を読み込み entries リストを返す（正規化済みpatternsを付与）。"""
    if not path.exists():
        raise SystemExit(f"FATAL: アクティビスト辞書が見つかりません: {path}")
    obj = json.loads(path.read_text(encoding="utf-8"))
    entries = obj["entries"]
    for e in entries:
        e["_normalized_patterns"] = [normalize_name(p) for p in e["name_patterns"]]
    return entries


def match_activist(filer_name: str, entries: list[dict], as_of_year: int) -> Optional[dict]:
    """filerName を辞書と時点管理付きで照合する。

    Args:
        as_of_year: シグナル発生年（提出日の年）。この年以前に公知になっていた
            （known_since_year <= as_of_year）エントリのみを照合対象にする
            （後知恵防止・カタログ§2-E「辞書は年次スナップショットで時点管理」の実装）。

    Returns:
        マッチしたエントリ（辞書そのもの・_normalized_patterns込み）。無ければ None。
        複数マッチした場合は辞書内で最初に一致したものを返す（同一提出者が複数エントリに
        またがることは想定していないため実務上は問題にならない）。
    """
    norm_filer = normalize_name(filer_name)
    if not norm_filer:
        return None
    for entry in entries:
        if entry["known_since_year"] > as_of_year:
            continue
        for pattern in entry["_normalized_patterns"]:
            if pattern and (pattern in norm_filer or norm_filer in pattern):
                return entry
    return None


def normalize_seccode(raw: Optional[str]) -> Optional[str]:
    """EDINET secCode を J-Quants Code 形式（5桁文字列）へ正規化する。

    4桁なら末尾に"0"を付与（J-Quants側の普通株式コード慣行）。5桁ならそのまま。
    それ以外（空・非数字等）は None を返す（対象コード特定不能として除外）。
    """
    if not raw or not str(raw).strip():
        return None
    s = str(raw).strip()
    if s.isdigit() and len(s) == 4:
        return s + "0"
    if s.isdigit() and len(s) == 5:
        return s
    return None


# --- EDINET キャッシュ読み込み ------------------------------------------------------


def load_edinet_records(start_bd: str, end_bd: str) -> tuple[list[dict], dict]:
    """[start_bd, end_bd]（YYYYMMDD営業日）の data/edinet/*.json.gz を読み込み、
    (submission_date, record) のリストとして返す（docTypeCode 350/360 は edinet_fetch.py
    側で既に絞り込み済み）。

    Returns:
        (rows, diag)。rows は各要素 {"submission_date": ..., **record} の dict。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    business_days = [d for d in all_bdays if start_bd <= d <= end_bd]

    diag = {"business_days_scanned": len(business_days), "files_missing": 0, "total_records": 0}
    rows: list[dict] = []
    missing_dates: list[str] = []
    for d in business_days:
        path = edinet_fetch.DATA_ROOT / f"{d}.json.gz"
        if not path.exists():
            diag["files_missing"] += 1
            missing_dates.append(d)
            continue
        obj = jq_fetch.read_json_gz(path)
        for rec in obj.get("results", []):
            diag["total_records"] += 1
            rows.append({"submission_date": d, **rec})

    if diag["files_missing"] > 0:
        raise SystemExit(
            f"FATAL: EDINETキャッシュが {diag['files_missing']}/{len(business_days)} 日分見つかりません"
            f"（例: {missing_dates[:3]}）。\n"
            f"先に `python3 scripts/edinet_fetch.py --start {start_bd} --end {end_bd}` "
            f"を実行してください（EDINET_API_KEY設定が前提）。"
        )
    return rows, diag


# --- シグナル生成 ---------------------------------------------------------------


def generate_activist_signals(
    start_bd: str,
    end_bd: str,
    dictionary_path: Path = DEFAULT_DICTIONARY_PATH,
    include_change_reports: bool = False,
) -> tuple[pd.DataFrame, dict]:
    """[start_bd, end_bd] 内でアクティビスト大量保有出現シグナルを生成する。

    Returns:
        (signals_df, diag)。signals_df 列: signal_date, code, filer_name, matched_entity_id,
        doc_type_code, submission_date, doc_id, intensity（常時1.0・強度2倍フラグは未実装）。
    """
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    entries = load_activist_dictionary(dictionary_path)
    records, load_diag = load_edinet_records(start_bd, end_bd)

    diag = {
        **load_diag,
        "matched_records": 0,
        "matched_350_new": 0,
        "matched_360_change_excluded": 0,
        "matched_360_change_included": 0,
        "no_seccode": 0,
        "calendar_end_skipped": 0,
        "signals_before_range_filter": 0,
    }

    rows: list[dict] = []
    for rec in records:
        filer_name = rec.get("filerName") or ""
        submission_date = rec["submission_date"]
        as_of_year = int(submission_date[:4])

        entry = match_activist(filer_name, entries, as_of_year)
        if entry is None:
            continue
        diag["matched_records"] += 1

        doc_type_code = str(rec.get("docTypeCode"))
        if doc_type_code == "360":
            if not include_change_reports:
                diag["matched_360_change_excluded"] += 1
                continue
            diag["matched_360_change_included"] += 1
        elif doc_type_code == "350":
            diag["matched_350_new"] += 1
        else:
            continue  # edinet_fetch.py側で既に350/360限定のはずだが念のため防御

        code = normalize_seccode(rec.get("secCode"))
        if code is None:
            diag["no_seccode"] += 1
            continue

        idx = bday_index[submission_date]
        if idx + 1 >= len(all_bdays):
            diag["calendar_end_skipped"] += 1
            continue
        signal_date = all_bdays[idx + 1]

        rows.append({
            "signal_date": signal_date,
            "code": code,
            "filer_name": filer_name,
            "matched_entity_id": entry["id"],
            "doc_type_code": doc_type_code,
            "submission_date": submission_date,
            "doc_id": rec.get("docID"),
            "intensity": 1.0,  # 重要提案行為等2倍フラグは未実装（書類本体取得が必要・実装ノート参照）
        })

    signals_df = pd.DataFrame(rows)
    diag["signals_before_range_filter"] = int(len(signals_df))
    if not signals_df.empty:
        signals_df = signals_df[
            (signals_df["signal_date"] >= start_bd) & (signals_df["signal_date"] <= end_bd)
        ].reset_index(drop=True)
    diag["signals_in_range"] = int(len(signals_df))
    return signals_df, diag


# --- ドライラン・スモークテスト（実EDINETデータなしでロジック検証） -------------------


def _dry_run_smoke() -> int:
    """合成フィクスチャで generate_activist_signals() 相当のロジックをオフラインで検証する。

    APIキー未入手で `data/edinet/` が空の間、実データに依存せず正誤判定できる範囲
    （辞書照合・時点管理・secCode正規化・反応日計算）を確認する。edinet_fetch.DATA_ROOT を
    一時ディレクトリに差し替えて実行するため、リポジトリの実データには一切触れない。
    """
    import shutil
    import tempfile

    print("[dry-run-smoke] 合成フィクスチャでロジック検証を開始...")
    tmp_dir = Path(tempfile.mkdtemp(prefix="edinet_smoke_"))
    original_data_root = edinet_fetch.DATA_ROOT
    try:
        edinet_fetch.DATA_ROOT = tmp_dir

        calendar_days = measure_base_rate.load_calendar_days()
        all_bdays = measure_base_rate.all_business_days(calendar_days)
        # 2018年の実在営業日を2つ選ぶ（辞書エントリのknown_since_yearを跨ぐ年で検証するため）
        sample_days = [d for d in all_bdays if "20180101" <= d <= "20181231"][:3]
        if len(sample_days) < 3:
            print("[dry-run-smoke] FAIL: サンプル営業日が3件確保できません（カレンダーキャッシュ不足）")
            return 1
        d1, d2, d3 = sample_days

        fixtures = {
            d1: {
                "date": d1, "total_count": 3, "filtered_count": 2,
                "results": [
                    {  # 辞書マッチ・350(新規)・secCode 4桁 -> 5桁化されるはず
                        "docID": "S100TEST1", "filerName": "エフィッシモ・キャピタル・マネジメント",
                        "docTypeCode": "350", "secCode": "7203",
                    },
                    {  # 辞書マッチしない提出者（ノイズ・除外されるはず）
                        "docID": "S100TEST2", "filerName": "無関係投資顧問株式会社",
                        "docTypeCode": "350", "secCode": "9984",
                    },
                ],
            },
            d2: {
                "date": d2, "total_count": 1, "filtered_count": 1,
                "results": [
                    {  # 辞書マッチ・360(変更) -> 既定(include_change_reports=False)では除外されるはず
                        "docID": "S100TEST3", "filerName": "オアシス・マネジメント",
                        "docTypeCode": "360", "secCode": "6758",
                    },
                ],
            },
            d3: {
                "date": d3, "total_count": 1, "filtered_count": 1,
                "results": [
                    {  # secCode欠損 -> code特定不能で除外されるはず
                        "docID": "S100TEST4", "filerName": "エフィッシモ・キャピタル・マネジメント",
                        "docTypeCode": "350", "secCode": None,
                    },
                ],
            },
        }
        for d, obj in fixtures.items():
            jq_fetch.write_json_gz(tmp_dir / f"{d}.json.gz", obj)

        signals_df, diag = generate_activist_signals(d1, d3, include_change_reports=False)

        checks = []
        checks.append(("シグナル件数=1（350マッチのみ採用）", len(signals_df) == 1))
        if len(signals_df) == 1:
            row = signals_df.iloc[0]
            checks.append(("code='72030'（4桁secCode 7203 -> 5桁化）", row["code"] == "72030"))
            checks.append(("matched_entity_id='effissimo'", row["matched_entity_id"] == "effissimo"))
            idx_d1 = all_bdays.index(d1)
            expected_signal_date = all_bdays[idx_d1 + 1]
            checks.append((f"signal_date=submission({d1})の翌営業日={expected_signal_date}",
                            row["signal_date"] == expected_signal_date))
        checks.append(("diag: matched_350_new=2（secCode欠損の1件も350マッチとしては計上）", diag["matched_350_new"] == 2))
        checks.append(("diag: matched_360_change_excluded=1", diag["matched_360_change_excluded"] == 1))
        checks.append(("diag: no_seccode=1", diag["no_seccode"] == 1))
        checks.append(("diag: matched_records=3（無関係投資顧問は不一致で含まれない）", diag["matched_records"] == 3))

        all_ok = True
        for label, ok in checks:
            print(f"  [{'OK' if ok else 'NG'}] {label}")
            all_ok = all_ok and ok

        # include_change_reports=True 側も検証（360がオプトインで拾えること）
        signals_df2, diag2 = generate_activist_signals(d1, d3, include_change_reports=True)
        check2 = len(signals_df2) == 2 and diag2["matched_360_change_included"] == 1
        print(f"  [{'OK' if check2 else 'NG'}] include_change_reports=True で360も採用され件数=2")
        all_ok = all_ok and check2

        print("[dry-run-smoke] " + ("全チェック PASS" if all_ok else "一部チェック FAIL"))
        return 0 if all_ok else 1
    finally:
        edinet_fetch.DATA_ROOT = original_data_root
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --- メイン処理 -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="アクティビスト大量保有出現シグナル生成器 + KPI検証ハーネス実行"
    )
    parser.add_argument("--start", default=IN_SAMPLE_START, help=f"検索開始月 YYYY-MM（既定 {IN_SAMPLE_START}）")
    parser.add_argument("--end", default=IN_SAMPLE_END, help=f"検索終了月 YYYY-MM（既定 {IN_SAMPLE_END}）")
    parser.add_argument("--dictionary-path", default=str(DEFAULT_DICTIONARY_PATH))
    parser.add_argument(
        "--include-change-reports", action="store_true",
        help="docTypeCode=360(変更報告書)も増減不問で採用する（既定は除外。増加のみへの絞り込みは"
             "書類本体解析が必要なため未実装。オプトイン時は「買い増しに限定していない」ことに注意）",
    )
    parser.add_argument("--kpi-name", default=KPI_NAME)
    parser.add_argument("--output-dir", default="output/kpi", help="ハーネス出力先ルート（kpi-name配下に生成）")
    parser.add_argument(
        "--skip-harness", action="store_true", help="シグナル生成のみ行いハーネス実行をスキップ（デバッグ用）"
    )
    parser.add_argument(
        "--defer-entry", action="store_true",
        help="T+1にAdjOが無い(S高等)場合、最大3営業日までエントリーを繰り延べる（第4周・§6手順6実装）",
    )
    parser.add_argument(
        "--dry-run-smoke", action="store_true",
        help="合成フィクスチャでロジックのみ検証して終了（実EDINETデータ不要・APIキー不要）",
    )
    args = parser.parse_args()

    if args.dry_run_smoke:
        return _dry_run_smoke()

    if not MONTH_RE.match(args.start) or not MONTH_RE.match(args.end):
        raise SystemExit("FATAL: --start/--end は YYYY-MM 形式で指定してください")
    if args.end > IN_SAMPLE_END:
        print(
            f"WARN: --end={args.end} は§6で凍結したholdout期間(2023年以降)に抵触します。"
            f"in-sample評価は{IN_SAMPLE_END}までに限定してください。",
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

    signals_df, diag = generate_activist_signals(
        start_bd, end_bd, Path(args.dictionary_path), args.include_change_reports
    )

    output_root = Path(args.output_dir)
    kpi_dir = output_root / args.kpi_name
    kpi_dir.mkdir(parents=True, exist_ok=True)
    signals_path = kpi_dir / "signals_raw.csv"
    signals_df.to_csv(signals_path, index=False)

    print(f"シグナル生成完了: {len(signals_df)}件")
    print(
        f"内訳: EDINETレコード総数={diag['total_records']} 辞書マッチ={diag['matched_records']} "
        f"(350新規採用={diag['matched_350_new']} / 360除外={diag['matched_360_change_excluded']} / "
        f"360採用={diag['matched_360_change_included']}) secCode欠損除外={diag['no_seccode']}"
    )
    print(f"出力: {signals_path}")

    if args.skip_harness:
        return 0
    if signals_df.empty:
        print("WARN: シグナルが0件のためハーネス実行をスキップします", file=sys.stderr)
        return 0

    params = {
        "dictionary_version": json.loads(Path(args.dictionary_path).read_text(encoding="utf-8"))["version"],
        "include_change_reports": args.include_change_reports,
        "intensity_multiplier_implemented": False,
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
