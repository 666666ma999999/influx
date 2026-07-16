#!/usr/bin/env python3
"""KPI日次実行の証跡基盤（§6付記II A節「証跡基盤」・凍結）。

`docs/stock-algo-kpi-catalog.md:424-521`（2026-07-16 前向き完全性・正式判定運用プロトコル）の
A節（同ファイル432-448行）が要求する第一証跡を実装する:
    - `append_run_log()`: run-summary 1行を `data/monitoring/run_log.jsonl` へ append-only で
      追記する（A-1・カタログ434-439行の必須フィールド）。
    - hash chain: 追記のたびに「前回末尾hash + 今回行」のsha256を
      `data/monitoring/run_log_hashchain.txt` へ1行追記する（A-2・カタログ440-441行）。
    - `KPI_DEPENDENCY_TABLE` + `resolve_kpi_critical_dates()`: KPI→依存入力・critical_dates を
      スクリプト定数で凍結する（A-6/A-7・カタログ453-459行）。

本モジュールは判定・シグナル生成ロジックに一切関与しない（証跡専用・§6付記II Bの完全性判定は
本ログを事後集計して行う別プロセス）。呼び出し元（scripts/daily_screen.py）は本番実行の最後に
`append_run_log()` を1回呼ぶ。vaultミラー（A-2の別媒体保存）は `scripts/kpi_clock_sla.py` 側で
`run_log_hashchain.txt` の新規行のみを追記する（ホスト側で動くのはSLAスクリプトのため）。

2026-07-16 Codexレビュー（NO-GO 4点）修正: (1) `compute_code_tree_hash()` を常にscripts/*.py
内容sha256主体に統一（gitのdirty検知はfilenameのみで内容差分を識別できなかった欠陥を修正・
git情報は参考フィールドに降格） (2) `append_run_log()` を `fcntl.flock` 排他ロック+
run_log/hashchain整合性の自動自己修復でトランザクション化 (3) `assert_inputs_cover_dependency_table()`
+ `describe_topix_snapshot()` を追加し、KPI_DEPENDENCY_TABLE依存の全入力（shortcover_x_bearの
topix含む）がinputsに漏れなく記録されることを機械的に保証 (4) vaultミラー側の自己修復は
`scripts/kpi_clock_sla.py` の `mirror_hashchain_to_vault()` で対応（本ファイルは正本側のみ）。

Usage（呼び出し例。daily_screen.py参照）:
    import kpi_run_evidence as run_evidence
    run_evidence.append_run_log(run_summary_dict)
"""
from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent
DATA_ROOT = PROJECT_ROOT / "data" / "jquants"  # jq_fetch.DATA_ROOTと同一パス規約（循環import回避のため複製せず定数だけ再定義）
MONITORING_DIR = PROJECT_ROOT / "data" / "monitoring"
RUN_LOG_PATH = MONITORING_DIR / "run_log.jsonl"
RUN_LOG_HASHCHAIN_PATH = MONITORING_DIR / "run_log_hashchain.txt"
RUN_LOG_LOCK_PATH = MONITORING_DIR / ".run_log.lock"  # 修正2: 二重起動排他用フロックファイル

GENESIS_HASH = "0" * 64  # hash chain初回行が連結する起点値（前回末尾hashが存在しない場合）


# === §6付記II A-1: run_log.jsonl 必須フィールド（カタログ434-439行） ===========================

REQUIRED_RUN_SUMMARY_FIELDS = (
    "run_id",
    "run_started_at",
    "run_finished_at",
    "mode",
    "target_signal_date",
    "target_entry_date",
    "overall_status",
    "kpi_results",  # KPI別{status, raw_signal_count, universe_pass_count, actionable_count,
                     # ledger_insert_count, duplicate_skip_count, result_set_hash}
    "watchlist_hash",
    "code_tree_hash",
    "inputs",  # 入力別{as_of_date, record_count, file_hash, latest_available_date, null_reason}
    "ledger",  # {before_count, before_hash, after_count, after_hash}
    "business_calendar_version",
    "original_run_id",  # リカバリ時のみ非null
    "sla_judgment",  # data/monitoring/sla_log.jsonl側で事後確定（本行生成時点では未定）
)

REQUIRED_KPI_RESULT_FIELDS = (
    "status",
    "raw_signal_count",
    "universe_pass_count",
    "actionable_count",
    "ledger_insert_count",
    "duplicate_skip_count",
    "result_set_hash",
)


# === §6付記II A-6/A-7: KPI依存関係表 + critical_dates（カタログ453-459行・凍結） ================
#
# 出典（各フィールドの根拠）:
#   - inputs: scripts/daily_screen.py の generate_kpi_signals() 分岐が実際にimportして呼ぶ
#     生成器（kpi_*_signals.py）の読み込みデータソースをgrep実査して機械的に確定
#     （bars/master/fins/shortsale/topixのいずれか。jsf系(zandaka/shina/seigenichiran/meigara)は
#     どのKPI生成器からも参照されておらず対象外＝表示専用監視のみで使用）。
#   - universe(master+bars)は全KPI共通の後置フィルタ（daily_screen.py:1631-1652の
#     UniverseCache.in_universe）のため個別列挙せず全エントリに暗黙付与する
#     （measure_base_rate.build_universe が bars でturnover集計・masterでProdCat"011"判定の
#     両方を必要とする。measure_base_rate.py:144-191参照）。
#   - 正常公開ラグ: bars/fins/shortsaleはdaily_screen.py:24-37 docstring「J-Quants当日データ
#     可用性プローブ」の実測（2026-07-07・同日17:44 JST時点count=0・翌営業日10:05 JST正常取得）。
#     shortsaleはjq_fetch.py:23-24（DiscDate+1営業日で使用可能と明記）でも裏付け。
#     masterはjq_fetch.py:13（月末営業日ごと）・lag概念より critical_date 充足の有無が本質。
#   - 取得締切: daily_screen.py:33 docstring「実行時刻は当面 平日07:30 JST とする」。
#   - formal_judgment/alpha: カタログ§6付記II A-20（507-520行）の正式判定対象列挙表。
#
# critical_dates_rule の値:
#   None                       = KPI固有のcritical dateなし（A-7「その他の日次KPI」）
#   "raw_strev_month_end"      = 各暦月の月末営業日（A-7・欠測=窓失効）。raw_strev_entry専用
# 加えて「月次ユニバース更新に必要な月初第1営業日」は全KPI共通のcritical date（A-7）のため
# critical_dates_rule欄には含めず resolve_kpi_critical_dates() が全KPI一律に付与する。

_BARS_ONLY_INPUTS = ("bars", "master")
_FINS_EVENT_INPUTS = ("bars", "master", "fins")

# (kpi_name, inputs, critical_dates_rule, fins_dependent_daily_miss, formal_judgment, alpha, alpha_basis)
_DEPENDENCY_ROWS: tuple[tuple[str, tuple[str, ...], Optional[str], bool, bool, Optional[float], Optional[str]], ...] = (
    # --- volshock系（bars単独・単日出来高ショック判定） -----------------------------------------
    ("volshock_5x", _BARS_ONLY_INPUTS, None, False, False, None,
     "対象外（A-20・比較対照）"),
    ("volshock_x_above200", _BARS_ONLY_INPUTS, None, False, False, None,
     "対象外（A-20・比較対照）"),
    ("volshock_x_above200_quiet", _BARS_ONLY_INPUTS, None, False, True, 0.025,
     "A-20: チャンピオン（陣別導入前登録）"),
    # --- shortcover（bars+shortsale+topix regime） -------------------------------------------
    ("shortcover_x_bear", ("bars", "master", "shortsale", "topix"), None, False, False, None,
     "対象外（A-20・比較対照）"),
    # --- pead（reference・holdout棄却済み） ---------------------------------------------------
    ("pead_gap8_vol3", _FINS_EVENT_INPUTS, None, True, False, None,
     "対象外（A-20・参照・棄却済み）"),
    # --- SUE系（fins as-of・§7-J） -------------------------------------------------------------
    ("sue_x_above200", _FINS_EVENT_INPUTS, None, True, True, 0.025,
     "A-20: sue_x_above200/sue_beat（§7-J paired判定の構成要素）"),
    ("sue_beat", _FINS_EVENT_INPUTS, None, True, True, 0.025,
     "A-20: sue_x_above200/sue_beat（§7-J paired判定の構成要素）"),
    # --- 第1陣（§7-Q固定ファミリーS=5のうち観察継続4系統） --------------------------------------
    ("sell_reg_trigger_rebound", _BARS_ONLY_INPUTS, None, False, True, 0.005,
     "A-20: 第1陣（0.025/5）"),
    ("turnover_rank_surge", _BARS_ONLY_INPUTS, None, False, True, 0.005,
     "A-20: 第1陣（0.025/5）"),
    ("margin_expand_yoy", _FINS_EVENT_INPUTS, None, True, True, 0.005,
     "A-20: 第1陣（0.025/5）"),
    ("raw_strev_entry", _BARS_ONLY_INPUTS, "raw_strev_month_end", False, True, 0.005,
     "A-20: 第1陣（0.025/5）"),
    # --- 第2陣（§7-T・3系統） ------------------------------------------------------------------
    ("gap_hold_close_strong", _BARS_ONLY_INPUTS, None, False, True, 0.004167,
     "A-20: 第2陣（0.0125/3）"),
    ("engulf_reversal_day", _BARS_ONLY_INPUTS, None, False, True, 0.004167,
     "A-20: 第2陣（0.0125/3）"),
    ("three_up_ignition", _BARS_ONLY_INPUTS, None, False, True, 0.004167,
     "A-20: 第2陣（0.0125/3）"),
    # --- 第3陣（§7-W・1系統） ------------------------------------------------------------------
    ("sales_beat", _FINS_EVENT_INPUTS, None, True, True, 0.00625,
     "A-20: 第3陣（0.05/2^3）"),
    # --- 第4陣（§7-Y・2系統） ------------------------------------------------------------------
    ("guidance_fy_strong", _FINS_EVENT_INPUTS, None, True, True, 0.0015625,
     "A-20: 第4陣（0.05/2^4÷2）"),
    ("cfo_margin_improve", _FINS_EVENT_INPUTS, None, True, True, 0.0015625,
     "A-20: 第4陣（0.05/2^4÷2）"),
    # --- 第5陣（§7-AB・1系統） -----------------------------------------------------------------
    ("earnings_spillover", _FINS_EVENT_INPUTS, None, True, True, 0.0015625,
     "A-20: 第5陣（0.05/2^5）"),
)

KPI_DEPENDENCY_TABLE: dict[str, dict[str, Any]] = {
    row[0]: {
        "inputs": row[1],
        "critical_dates_rule": row[2],
        "fins_dependent_daily_miss": row[3],  # A-7: fins系は「毎営業日均等走査・fins入力の欠測日は当該KPIの欠測日」
        "formal_judgment": row[4],
        "alpha": row[5],
        "alpha_basis": row[6],
    }
    for row in _DEPENDENCY_ROWS
}

# KPI_DEPENDENCY_TABLEに現れる入力名の全集合（shortcover_x_bearが依存するtopixを含む）。
# assert_inputs_cover_dependency_table() の判定基準（修正3）。
ALL_DEPENDENCY_TABLE_INPUTS: frozenset[str] = frozenset(
    name for spec in KPI_DEPENDENCY_TABLE.values() for name in spec["inputs"]
)


def assert_inputs_cover_dependency_table(inputs: dict[str, Any]) -> None:
    """KPI_DEPENDENCY_TABLEに現れる全入力名がrun-summaryのinputsにキーとして記録されている
    ことを保証する（A-1/A-6・修正3・Codexレビュー指摘2026-07-16）。

    取得不能な入力値はdescribe_input_snapshot()/describe_topix_snapshot()がnull+null_reasonで
    「明示的に」埋めるが、「キー自体が存在しないこと」は許さない（黙って欠落させない）。
    呼び出し側（daily_screen.py）のinputs組み立てが漏れた場合（例: 新規KPI登録で依存入力が
    増えたのに反映漏れ）を機械的に検知するアサート。target_signal_date未確定でinputs組み立て
    自体を意図的に省略するケースでは呼び出し側は本関数を呼ばないこと（別の理由での省略のため）。

    Raises:
        AssertionError: KPI_DEPENDENCY_TABLE由来の入力名がinputsに1つでも欠落している場合。
    """
    missing = sorted(ALL_DEPENDENCY_TABLE_INPUTS - inputs.keys())
    if missing:
        raise AssertionError(
            f"inputsにKPI_DEPENDENCY_TABLE依存の入力が欠落しています（黙って欠落させない・修正3）: {missing}"
        )

# 入力種別ごとの公開ラグ・取得締切の凍結値（A-6「正常公開ラグ・取得締切・最終レコード許容日」）。
# marginはどのKPI生成器からも直接使われていない（daily_screen.pyのformat_margin_freshness_linesは
# 表示専用監視）が、依存関係表の完全性のため参考記録する。
INPUT_LAG_SPEC: dict[str, dict[str, Any]] = {
    "bars": {
        "normal_publish_lag_bdays": 1,
        "acquisition_deadline_jst": "07:30",
        "source": "scripts/daily_screen.py:24-37（J-Quants当日データ可用性プローブ実測）+ :33（実行時刻07:30 JST）",
    },
    "fins": {
        "normal_publish_lag_bdays": 1,
        "acquisition_deadline_jst": "07:30",
        "source": "scripts/daily_screen.py:24-37（同日開示分もsnapshot取得はT+1朝に確定）",
    },
    "shortsale": {
        "normal_publish_lag_bdays": 1,
        "acquisition_deadline_jst": "07:30",
        "source": "scripts/jq_fetch.py:23-24（DiscDate+1営業日で使用可能と明記）",
    },
    "master": {
        "normal_publish_lag_bdays": None,
        "acquisition_deadline_jst": "07:30",
        "source": "scripts/jq_fetch.py:13（月末営業日ごと・lagより月初critical_date充足が本質）",
    },
    "topix": {
        "normal_publish_lag_bdays": 1,
        "acquisition_deadline_jst": "07:30",
        "source": "scripts/daily_screen.py:refresh_topix_if_stale（bars同様のJ-Quants日次系列）",
    },
    "margin": {
        "normal_publish_lag_calendar_days": 4,
        "acquisition_deadline_jst": None,
        "source": "scripts/jq_fetch.py:18-22（基準日+4暦日を保守的使用可能日とする）。KPI生成には未使用・監視専用",
    },
}


def is_month_first_bday(date: str, all_bdays: list[str], bday_index: dict[str, int]) -> bool:
    """dateがその暦月の最初の営業日か（A-7「月次ユニバース更新に必要な月初第1営業日=全KPI共通」）。"""
    idx = bday_index[date]
    return idx == 0 or all_bdays[idx - 1][:6] != date[:6]


def is_month_last_bday(date: str, all_bdays: list[str], bday_index: dict[str, int]) -> bool:
    """dateがその暦月の最終営業日か（A-7 raw_strev_entry専用critical date判定と同一規則。
    scripts/daily_screen.py generate_raw_strev_signals の月末判定と同型・循環import回避のため複製）。
    """
    idx = bday_index[date]
    return idx + 1 >= len(all_bdays) or all_bdays[idx + 1][:6] != date[:6]


def resolve_kpi_critical_dates(
    kpi_name: str, target_signal_date: str, all_bdays: list[str], bday_index: dict[str, int],
) -> list[str]:
    """target_signal_date時点でkpi_nameに適用されるcritical_dateラベルの一覧を返す（A-7）。

    空リストなら当日はそのKPIにとってcritical dateではない（=通常欠測許容ルールが適用される）。
    """
    active: list[str] = []
    if is_month_first_bday(target_signal_date, all_bdays, bday_index):
        active.append("universe_month_first_bday")  # 全KPI共通
    rule = KPI_DEPENDENCY_TABLE.get(kpi_name, {}).get("critical_dates_rule")
    if rule == "raw_strev_month_end" and is_month_last_bday(target_signal_date, all_bdays, bday_index):
        active.append("raw_strev_month_end")
    return active


# === 入力スナップショットの証跡計算（A-1「入力別{as-of日, 最終レコード日, 件数, hash}」） ==========


def compute_file_hash(path: Path) -> Optional[str]:
    """ファイルの生バイト列のsha256を返す（存在しない場合None）。"""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def describe_input_snapshot(kind: str, as_of_date: str, file_date: Optional[str] = None) -> dict[str, Any]:
    """入力種別kind（bars/fins/shortsale/master/margin）の1スナップショットの証跡を返す。

    Args:
        kind: DATA_ROOT配下のサブディレクトリ名（bars/fins/shortsale/master/margin）。
        as_of_date: この入力が対応する営業日ラベル（target_signal_dateまたはKPI固有のas-of日）。
        file_date: 実際に参照するファイル日付（YYYYMMDD）。省略時はas_of_dateと同一
            （masterのように「as_of_date以前の直近月末」を別途解決して渡すケースを想定）。

    Returns:
        {as_of_date, file_date, record_count, file_hash, latest_available_date, null_reason}。
        取得不能な項目はnull+null_reasonに理由を記す（A-1「取得不能なフィールドはnull+理由」）。
    """
    resolved_file_date = file_date or as_of_date
    result: dict[str, Any] = {
        "as_of_date": as_of_date,
        "file_date": resolved_file_date,
        "record_count": None,
        "file_hash": None,
        "latest_available_date": None,
        "null_reason": None,
    }
    kind_dir = DATA_ROOT / kind
    if not kind_dir.exists():
        result["null_reason"] = f"{kind_dir} が存在しません"
        return result

    existing = sorted(p.name.removesuffix(".json.gz") for p in kind_dir.glob("*.json.gz"))
    if existing:
        result["latest_available_date"] = existing[-1]

    target_path = kind_dir / f"{resolved_file_date}.json.gz"
    if not target_path.exists():
        reason = f"{resolved_file_date}.json.gz が未取得（cache未取得または将来日）"
        result["null_reason"] = reason
        return result

    try:
        raw_bytes = target_path.read_bytes()
        result["file_hash"] = hashlib.sha256(raw_bytes).hexdigest()
        obj = json.loads(gzip.decompress(raw_bytes))
        result["record_count"] = len(obj.get("data", []))
    except (OSError, EOFError, gzip.BadGzipFile, json.JSONDecodeError) as e:
        result["null_reason"] = f"読込/解析失敗: {e}"
    return result


def describe_topix_snapshot(as_of_date: str) -> dict[str, Any]:
    """topix.json.gz（全期間・単一ファイル。jq_fetch.fetch_topix参照）の入力証跡を返す
    （A-1 inputs.topix・修正3: shortcover_x_bearが依存するtopixの証跡欠落を解消）。

    bars/fins/shortsale/masterは日付別ファイル（describe_input_snapshot対象）だが、topixは
    全期間1ファイル（DATA_ROOT/topix.json.gz・daily_screen.py refresh_topix_if_staleが日次で
    鮮度確保）のため専用の証跡関数とする。file_hash・全件数に加え、ファイル内容中の日付列から
    latest_available_date（ファイル収録済みの最終日）を算出する。

    Args:
        as_of_date: この入力が対応する営業日ラベル（target_signal_date）。

    Returns:
        describe_input_snapshot()と同一キー構成のdict。file_dateは常にNone（全期間単一ファイル
        のため「ファイル日付」という概念がない）。as_of_date以前のレコードが1件もない場合は
        null_reasonにその旨を明示する。
    """
    result: dict[str, Any] = {
        "as_of_date": as_of_date,
        "file_date": None,
        "record_count": None,
        "file_hash": None,
        "latest_available_date": None,
        "null_reason": None,
    }
    path = DATA_ROOT / "topix.json.gz"
    if not path.exists():
        result["null_reason"] = f"{path} が存在しません"
        return result
    try:
        raw_bytes = path.read_bytes()
        result["file_hash"] = hashlib.sha256(raw_bytes).hexdigest()
        obj = json.loads(gzip.decompress(raw_bytes))
        rows = obj.get("data", [])
        result["record_count"] = len(rows)
        dates = sorted(r["Date"].replace("-", "") for r in rows if r.get("Date"))
        result["latest_available_date"] = dates[-1] if dates else None
        if not any(d <= as_of_date for d in dates):
            result["null_reason"] = f"as_of_date={as_of_date}以前のtopixレコードがありません"
    except (OSError, EOFError, gzip.BadGzipFile, json.JSONDecodeError, KeyError) as e:
        result["null_reason"] = f"読込/解析失敗: {e}"
    return result


def find_applicable_master_date(as_of_date: str, master_dir: Optional[Path] = None) -> Optional[str]:
    """as_of_date以前の直近の月末営業日master（実在ファイル）のYYYYMMDDを返す（無ければNone）。

    measure_base_rate.UniverseCache._t_date_for_month と同じ「直近確定済み月末」規約だが、
    本モジュールはdaily_screen.pyへの逆import（循環）を避けるため、実在するmasterファイルの
    一覧から機械的に導出する（カレンダー計算に依存しない・シンプルなファイル名走査）。
    """
    d = master_dir or (DATA_ROOT / "master")
    if not d.exists():
        return None
    candidates = sorted(p.name.removesuffix(".json.gz") for p in d.glob("*.json.gz") if p.name <= f"{as_of_date}.json.gz")
    return candidates[-1] if candidates else None


# === ledger / watchlist / 営業日カレンダー の証跡（A-1） =========================================


def compute_file_hash_and_linecount(path: Path) -> tuple[Optional[str], Optional[int]]:
    """JSONLファイル等の(sha256, 行数)を返す（存在しない場合は(None, 0)）。"""
    if not path.exists():
        return None, 0
    raw = path.read_bytes()
    line_count = sum(1 for ln in raw.splitlines() if ln.strip())
    return hashlib.sha256(raw).hexdigest(), line_count


def compute_watchlist_hash(watchlist_path: Path) -> Optional[str]:
    """config/paper_watchlist.json の生バイト列のsha256（A-1 watchlist_hash）。"""
    return compute_file_hash(watchlist_path)


def compute_business_calendar_version(calendar_path: Optional[Path] = None) -> dict[str, Any]:
    """data/jquants/calendar.json.gz のhash+収録件数を「営業日カレンダーversion」として返す（A-1）。"""
    path = calendar_path or (DATA_ROOT / "calendar.json.gz")
    if not path.exists():
        return {"file_hash": None, "record_count": None, "null_reason": f"{path} が存在しません"}
    try:
        raw = path.read_bytes()
        obj = json.loads(gzip.decompress(raw))
        return {
            "file_hash": hashlib.sha256(raw).hexdigest(),
            "record_count": len(obj.get("data", [])),
            "null_reason": None,
        }
    except (OSError, EOFError, gzip.BadGzipFile, json.JSONDecodeError) as e:
        return {"file_hash": None, "record_count": None, "null_reason": f"読込/解析失敗: {e}"}


def compute_result_set_hash(rows: list[tuple[str, str]]) -> str:
    """KPI1本のシグナル候補集合（(code, signal_date)のリスト）を正規化してsha256を返す（A-1
    result_set_hash）。呼び出し側でソート済みか問わないよう本関数内でソートする。
    """
    normalized = sorted(f"{code}|{signal_date}" for code, signal_date in rows)
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


# === code_tree_hash（A-1・dirty worktree検知込み） ===============================================


def compute_code_tree_hash(project_root: Optional[Path] = None) -> dict[str, Any]:
    """コードツリーの版を一意に識別するhashを返す（A-1 code_tree_hash・dirty worktree検知込み）。

    識別の主体は常に scripts/*.py の内容sha256（sorted相対パス+content連結）とする
    （method="content_sha256"・コンテナ/ホストいずれでも決定的。修正1・Codexレビュー指摘
    2026-07-16）。旧実装はgitが使える環境では `git status --porcelain` の変更ファイル名一覧
    のみをhash化しており、同一ファイル内のコード変更（内容差分）が同一hashになる欠陥があった
    （ファイル名だけ見て内容を見ない＝dirty内容を識別できない）。

    gitが使える環境（.gitがmount/存在）では `git rev-parse HEAD` + dirty検知を参考フィールド
    （git_head/git_dirty/git_dirty_files）として併記するが、値の同一性判定には使わない
    （フィールド名で識別の主体と参考情報を区別する）。本番runner（docker-compose.yml）は
    `.git` をコンテナへmountしていないため、本番実行では常に git_head=None
    （2026-07-16実機確認: コンテナ内 `git rev-parse HEAD` は "fatal: not a git repository" で失敗）。
    """
    root = project_root or PROJECT_ROOT
    script_files = sorted((root / "scripts").glob("*.py"))
    hasher = hashlib.sha256()
    for p in script_files:
        rel = p.relative_to(root).as_posix()
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(p.read_bytes())
        hasher.update(b"\x00")

    result: dict[str, Any] = {
        "method": "content_sha256",
        "value": hasher.hexdigest(),
        "n_files_hashed": len(script_files),
        "git_head": None,
        "git_dirty": None,
        "git_dirty_files": None,
        "git_unavailable_reason": None,
    }
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=10, check=True,
        ).stdout
        dirty_files = sorted(ln[3:] for ln in status.splitlines() if ln.strip())
        result["git_head"] = head
        result["git_dirty"] = bool(dirty_files)
        result["git_dirty_files"] = dirty_files
    except (subprocess.CalledProcessError, FileNotFoundError, OSError, subprocess.TimeoutExpired) as e:
        result["git_unavailable_reason"] = str(e)
    return result


# === run_log.jsonl 追記 + hash chain（A-1/A-2） ================================================


def _read_last_hashchain_hash() -> str:
    if not RUN_LOG_HASHCHAIN_PATH.exists():
        return GENESIS_HASH
    lines = [ln for ln in RUN_LOG_HASHCHAIN_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return GENESIS_HASH
    return lines[-1].split()[0]


def _compute_full_hashchain(run_log_lines: list[str]) -> list[str]:
    """run_log.jsonlの全行からGENESIS_HASH起点でhash chainを先頭から再計算する（修正2）。

    _verify_and_repair_hashchain()の整合性確認・自己修復の両方で使う共通ロジック。
    """
    chain: list[str] = []
    prev = GENESIS_HASH
    for ln in run_log_lines:
        prev = hashlib.sha256((prev + ln).encode("utf-8")).hexdigest()
        chain.append(prev)
    return chain


def _regenerate_hashchain_file(run_log_lines: list[str]) -> None:
    """run_log.jsonl全行から run_log_hashchain.txt を原子的に全文再生成する（自己修復・修正2）。"""
    chain = _compute_full_hashchain(run_log_lines)
    content = "".join(h + "\n" for h in chain)
    tmp = RUN_LOG_HASHCHAIN_PATH.with_name(RUN_LOG_HASHCHAIN_PATH.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(RUN_LOG_HASHCHAIN_PATH)


def _verify_and_repair_hashchain(run_log_lines: list[str]) -> None:
    """run_log全行から再計算したhash chainと run_log_hashchain.txt の現内容を突合する（A-2）。

    トランザクション非一貫性対策（修正2・Codexレビュー指摘2026-07-16）: 旧実装は
    run_log置換 → hashchain追記 の2ステップが非トランザクションで、間で停止すると
    hashchainがrun_logより1行以上少ない状態のまま永久に不一致となり得た。append_run_log()
    冒頭（フロック取得後）で毎回本関数を呼ぶことで、前回実行の中断があっても次回実行時に
    自動復元する。欠落・改変・行数差のいずれであっても「全行一致」でなければ不一致として
    検知し、run_logを正としてhashchainを全文再生成する（警告print）。
    """
    expected = _compute_full_hashchain(run_log_lines)
    actual = (
        [ln.strip() for ln in RUN_LOG_HASHCHAIN_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if RUN_LOG_HASHCHAIN_PATH.exists() else []
    )
    if actual != expected:
        print(
            f"WARN: run_log_hashchain.txtがrun_log.jsonlと不一致（欠落・改変・行数差のいずれか。"
            f"既存={len(actual)}行 / 期待={len(expected)}行）。run_log全{len(run_log_lines)}行から"
            f"全文再生成します。",
            file=sys.stderr,
        )
        _regenerate_hashchain_file(run_log_lines)


def _append_hash_chain(run_log_line: str) -> str:
    """前回末尾hash + 今回行 のsha256を計算し、hashchainファイルへ1行追記する（A-2）。

    tmp書き込み+os.replace（Path.replace）による原子的置換で、途中クラッシュ時も
    ファイルが不完全な状態で残らないようにする（scripts/daily_screen.py save_state と同型の
    既存atomic writeイディオムに合わせる）。

    Returns:
        今回追記したhash値（sha256 16進64文字）。
    """
    prev_hash = _read_last_hashchain_hash()
    combined = (prev_hash + run_log_line).encode("utf-8")
    new_hash = hashlib.sha256(combined).hexdigest()
    existing = RUN_LOG_HASHCHAIN_PATH.read_text(encoding="utf-8") if RUN_LOG_HASHCHAIN_PATH.exists() else ""
    new_content = existing + new_hash + "\n"
    tmp = RUN_LOG_HASHCHAIN_PATH.with_name(RUN_LOG_HASHCHAIN_PATH.name + ".tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(RUN_LOG_HASHCHAIN_PATH)
    return new_hash


def append_run_log(run_summary: dict[str, Any]) -> dict[str, Any]:
    """run_summary（A-1必須フィールドを持つdict）を data/monitoring/run_log.jsonl へ
    append-only で1行追記し、hash chainを更新する。

    欠損フィールドがあれば ValueError で拒否する（呼び出し側の責務として、値自体が取得不能な
    場合はnull+理由で埋めた上で本関数を呼ぶこと。フィールドの「キーが存在しないこと」と
    「値がnullなこと」は区別する＝A-1の完全性を機械的に保証する）。

    トランザクション性（修正2・Codexレビュー指摘2026-07-16）: 本関数全体を
    `data/monitoring/.run_log.lock` へのfcntl.flock排他ロックで囲み、二重起動（同時刻に
    daily_screen.pyが2プロセス走る等）による競合書き込みを防ぐ。ロック取得後、まず
    run_log全行から再計算したhash chainと run_log_hashchain.txt を突合し、前回実行が
    run_log置換後・hashchain追記前で中断していた場合は自己修復（全文再生成）してから
    今回分を追記する（_verify_and_repair_hashchain参照）。

    ファイル書き込みはtmp+os.replace（Path.replace）による原子的置換で行う
    （scripts/daily_screen.py save_state / paper_eval.write_ledger_atomic と同型の既存
    atomic writeイディオムに揃える。JSONL全体を読み直して1行追記する設計は、1実行1行/日の
    増分ペースであれば読み直しコストは無視できる規模に留まる）。

    Args:
        run_summary: REQUIRED_RUN_SUMMARY_FIELDS を全て持つdict。

    Returns:
        実際に書き込んだ行のdict（呼び出し側のログ表示・テスト検証用）。

    Raises:
        ValueError: 必須フィールドが欠落している場合。
    """
    missing = [f for f in REQUIRED_RUN_SUMMARY_FIELDS if f not in run_summary]
    if missing:
        raise ValueError(f"run_summaryに必須フィールドが欠落しています: {missing}")

    MONITORING_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(run_summary, ensure_ascii=False, sort_keys=True)

    with open(RUN_LOG_LOCK_PATH, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)  # 二重起動排他（修正2）
        try:
            existing = RUN_LOG_PATH.read_text(encoding="utf-8") if RUN_LOG_PATH.exists() else ""
            existing_lines = [ln for ln in existing.splitlines() if ln.strip()]
            _verify_and_repair_hashchain(existing_lines)  # 前回中断分の自己修復（修正2）

            new_content = existing + line + "\n"
            tmp = RUN_LOG_PATH.with_name(RUN_LOG_PATH.name + ".tmp")
            tmp.write_text(new_content, encoding="utf-8")
            tmp.replace(RUN_LOG_PATH)

            _append_hash_chain(line)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return run_summary


if __name__ == "__main__":
    # 簡易セルフチェック（本番実行経路ではない）: 依存関係表の整合性のみ確認する。
    n_formal = sum(1 for v in KPI_DEPENDENCY_TABLE.values() if v["formal_judgment"])
    print(f"KPI_DEPENDENCY_TABLE: {len(KPI_DEPENDENCY_TABLE)}系統登録・うち正式判定対象{n_formal}系統")
    for name, spec in KPI_DEPENDENCY_TABLE.items():
        print(f"  {name}: inputs={spec['inputs']} formal={spec['formal_judgment']} alpha={spec['alpha']}")
    sys.exit(0)
