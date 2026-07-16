#!/usr/bin/env python3
"""前向き検証クロックの死活監視SLA（レビュー合意事項 P8/BG/R7）。

経営計画/運用監視用。合否判定には一切使用しない。

**本スクリプトは粗い死活監視であり、KPI別coverage証明（§6完全性プロトコルの第一証跡）ではない。**
株価予測アルゴのペーパートレード観察クロック（scripts/daily_screen.py が毎営業日走らせる
一連のパイプライン）が生きているかを日次で死活監視する。東証営業日のみ以下3点をチェックし、
1件でもFAILならmacOS通知（osascript）を出してexit code 1で終了する:
    (1) data/paper_trades/state.json が当日更新されているか（last_run_at優先。JSON破損・
        last_run_at値不正は即FAIL。mtimeフォールバックは「正常にパースできるJSONだが
        last_run_atキーが無い」場合のみ）
    (2) output/paper_today.md 冒頭の「実行時刻: 」日付が当日か
    (3) data/jsf/archive_log.jsonl を直近営業日のrun_id単位で集約し、zandaka/shina/meigara/
        seigenichiranの4データセット全てについて status=saved|skipped_dup（成功系）・
        data_dateの妥当性・保存ファイルの実在（data/jsf/配下）を検証
非営業日（土日・日本の祝日・東証年末年始休場）は3チェックをスキップし、SKIPとして記録するのみ。
祝日定数（HOLIDAYS_JP）に収録の無い年（2028年以降）は土日判定のみでの継続を行わず、
status=config_errorとしてFAILする。

結果は data/monitoring/sla_log.jsonl に1実行1行でappendする。これは運用監視ログであり、
合否判定台帳ではない（data/kpi_trials/trials.jsonl 等の正式な検証台帳とは別物・書き込み厳禁）。

スコープ外: 欠測が「12完全暦月」の有効性判定に与える影響のルール策定は本スクリプトの範囲外
（監視・記録のみ行う。ルール自体は別途team-leadが起草しCodexレビューへ諮る）。

Python標準ライブラリのみで動作する（pip install不要・Docker不要）。ホストのlaunchdから
`python3 scripts/kpi_clock_sla.py` を直接実行する前提（config/launchd/com.influx.kpi-clock-sla.plist。
launchctl load は本スクリプト作成時点では未実施・ユーザー確認事項）。

Usage:
    python3 scripts/kpi_clock_sla.py             # 通常実行（営業日ならチェック→ログ追記→FAIL時通知）
    python3 scripts/kpi_clock_sla.py --dry-run    # 確認モード（通知・ログ書き込みなし）
"""
from __future__ import annotations

import argparse
import datetime
import json
import glob
import gzip
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent))
import jsf_daily_archive  # noqa: E402  (Canonical Module: now_jst()/JST/LOG_PATHを再利用。標準ライブラリのみで完結する既存実装のため流用)

PROJECT_ROOT = Path(__file__).parent.parent
STATE_PATH = PROJECT_ROOT / "data" / "paper_trades" / "state.json"
PAPER_TODAY_PATH = PROJECT_ROOT / "output" / "paper_today.md"
SLA_LOG_DIR = PROJECT_ROOT / "data" / "monitoring"
SLA_LOG_PATH = SLA_LOG_DIR / "sla_log.jsonl"

# §6付記II A-2（docs/stock-algo-kpi-catalog.md:440-441）: run_log_hashchain.txt の別媒体保存
# （vaultミラー）。正本は scripts/kpi_run_evidence.py が data/monitoring/ へ追記する
# RUN_LOG_HASHCHAIN_PATH と同一パス規約（本スクリプトはstdlib専用制約のためimportせずPathを複製）。
RUN_LOG_HASHCHAIN_PATH = PROJECT_ROOT / "data" / "monitoring" / "run_log_hashchain.txt"
VAULT_HASHCHAIN_MIRROR_PATH = (
    Path.home() / "Documents" / "Obsidian Vault" / "02_Ai" / "influx" / "influx-runlog-hashchain.md"
)
VAULT_HASHCHAIN_MIRROR_HEADER = """---
project: influx
type: log
folder: "02_Ai/influx/"
categories:
  - "[[influx_ope]]"
tags:
  - project/influx
  - type/log
---
> [!note] 🤖 自動追記ミラー（scripts/kpi_clock_sla.py 実行毎に新規行のみ追記・§6付記II A-2 別媒体保存）
> 正本: `Desktop/biz/influx/data/monitoring/run_log_hashchain.txt`（scripts/kpi_run_evidence.py が追記）。
> 以下は正本と同一形式（各行1個のsha256ハッシュ・出現順）。このファイルは追記専用（過去行の
> 削除・編集は次回実行では復元されません）。

"""
_HASHCHAIN_LINE_RE = re.compile(r"^[0-9a-f]{64}$")

# ペーパートレード勝敗台帳のvault表示ビュー（2026-07-16 ユーザー指示「vault mdで管理して」）。
# 正本は data/paper_trades/ledger.jsonl（機械台帳・単一書き込み者=daily_screen.py）で、
# 本ビューは読み取り専用の人間向けレンダリング（Canonical Module原則: 台帳の二重管理はしない。
# ledger.jsonl→md の全文再生成・固定名上書き）。
LEDGER_PATH = PROJECT_ROOT / "data" / "paper_trades" / "ledger.jsonl"
MASTER_DIR = PROJECT_ROOT / "data" / "jquants" / "master"
VAULT_PAPER_LEDGER_PATH = (
    Path.home() / "Documents" / "Obsidian Vault" / "02_Ai" / "influx" / "influx-paper-ledger.md"
)
# 系統名の日本語ラベル（表示専用・正本は config/paper_watchlist.json の kpi_name）
KPI_LABELS = {
    "volshock_x_above200_quiet": "チャンピオン（出来高ショック×静→動）",
    "volshock_x_above200": "出来高ショック×200日線（対照）",
    "volshock_5x": "出来高ショック素（対照）",
    "shortcover_x_bear": "ショートカバー×bear（対照）",
    "sue_x_above200": "SUE×200日線",
    "sue_beat": "SUE素（対照）",
    "sales_beat": "売上ビート",
    "guidance_fy_strong": "ガイダンス強気",
    "cfo_margin_improve": "CFOマージン改善",
    "margin_expand_yoy": "マージン改善",
    "earnings_spillover": "決算読み替え",
    "sell_reg_trigger_rebound": "規制トリガー反発",
    "turnover_rank_surge": "ランク急上昇",
    "raw_strev_entry": "素リバーサル",
    "gap_hold_close_strong": "引け強",
    "engulf_reversal_day": "切り返し",
    "three_up_ignition": "三連陽線",
    "pead_gap8_vol3": "PEAD（参照・棄却済み）",
    "sh_dip_reentry": "S高押し目（休眠）",
}
EXIT_LABELS = {"stop_loss": "損切り", "time_exit": "期日(20営業日)", "delisted": "上場廃止"}

# output/paper_today.md 冒頭日付の抽出パターン。scripts/daily_screen.py の
# REPORT_TIMESTAMP_RE（daily_screen.py:105 "実行時刻: (\d{4}-\d{2}-\d{2})"）と同一定義。
# daily_screen.py本体はKPIスクリプト群・J-Quants等の重量依存を持つためimportはせず、
# 正規表現のみを複製する（本スクリプトはstdlibのみで動作する制約があるため。Canonical
# Moduleはdaily_screen.py側であり、パターンを変更する場合は両方を同時に直すこと）。
REPORT_TIMESTAMP_RE = re.compile(r"実行時刻: (\d{4}-\d{2}-\d{2})")

# --- 東証営業日判定用の祝日定数 -------------------------------------------------
#
# 国民の祝日（2026年）: 内閣府「国民の祝日について」（国民の祝日に関する法律 昭和23年法律
# 第178号）の規定（固定日 + ハッピーマンデー制度 + 春分/秋分の日）に基づき算出。
# 春分の日(3/20)・秋分の日(9/23)は国立天文台「暦要項」の official 発表値（2025年2月官報
# 告示分）。
# 振替休日: 5/3(憲法記念日,日曜)→5/4・5/5も祝日のため順延し5/6が振替休日（同法第3条2項）。
# 国民の休日: 9/21(敬老の日,月)と9/23(秋分の日,水)に挟まれた9/22(火)が祝日扱いの休日となる
# （同法第3条3項）。
HOLIDAYS_JP_2026: dict[str, str] = {
    "2026-01-01": "元日",
    "2026-01-12": "成人の日",
    "2026-02-11": "建国記念の日",
    "2026-02-23": "天皇誕生日",
    "2026-03-20": "春分の日",
    "2026-04-29": "昭和の日",
    "2026-05-03": "憲法記念日",
    "2026-05-04": "みどりの日",
    "2026-05-05": "こどもの日",
    "2026-05-06": "振替休日（5/3憲法記念日の振替）",
    "2026-07-20": "海の日",
    "2026-08-11": "山の日",
    "2026-09-21": "敬老の日",
    "2026-09-22": "国民の休日（敬老の日と秋分の日に挟まれた平日）",
    "2026-09-23": "秋分の日",
    "2026-10-12": "スポーツの日",
    "2026-11-03": "文化の日",
    "2026-11-23": "勤労感謝の日",
}

# 国民の祝日（2027年）: 算出方法は2026年分と同一（固定日+ハッピーマンデー+春分/秋分）。
# 春分の日(3/21)・秋分の日(9/23)は国立天文台「暦要項」の想定値（2026年2月官報告示ベース）。
# 本スクリプト作成時点(2026-07-16)ではオンライン照合を行っていないため、2027年2月の最終
# 官報告示との突合を運用上のTODOとする（本SLAは合否判定に使わない運用監視のため実害は
# 限定的。ズレが判明した場合は本定数を直接修正すること）。
# 振替休日: 3/21(春分の日,日曜)→3/22(月,他の祝日と重複なし)が振替休日。
HOLIDAYS_JP_2027: dict[str, str] = {
    "2027-01-01": "元日",
    "2027-01-11": "成人の日",
    "2027-02-11": "建国記念の日",
    "2027-02-23": "天皇誕生日",
    "2027-03-21": "春分の日",
    "2027-03-22": "振替休日（3/21春分の日の振替）",
    "2027-04-29": "昭和の日",
    "2027-05-03": "憲法記念日",
    "2027-05-04": "みどりの日",
    "2027-05-05": "こどもの日",
    "2027-07-19": "海の日",
    "2027-08-11": "山の日",
    "2027-09-20": "敬老の日",
    "2027-09-23": "秋分の日",
    "2027-10-11": "スポーツの日",
    "2027-11-03": "文化の日",
    "2027-11-23": "勤労感謝の日",
}

# 東証の年末年始非営業日（祝日ではないが取引所規則で休場。国民の祝日とは別出典）。
# 出典: 日本取引所グループ(JPX)の年間営業日カレンダー（大納会/大発会の運用に基づき
# 毎年12/31・1/2・1/3を休場日とする慣行）。1/1は元日として上記祝日リストに別掲済み。
TSE_YEAR_END_NEWYEAR_CLOSURES: set[str] = {
    "2026-01-02", "2026-01-03", "2026-12-31",
    "2027-01-02", "2027-01-03", "2027-12-31",
}

HOLIDAYS_JP: dict[str, str] = {**HOLIDAYS_JP_2026, **HOLIDAYS_JP_2027}
NON_TRADING_DATES: set[str] = set(HOLIDAYS_JP) | TSE_YEAR_END_NEWYEAR_CLOSURES

# HOLIDAYS_JP/TSE_YEAR_END_NEWYEAR_CLOSURESが収録している年（this範囲外はconfig_errorでFAIL）
SUPPORTED_HOLIDAY_YEARS = (2026, 2027)


class CheckResult(NamedTuple):
    """1チェックの結果（P8の3チェック共通の戻り値型）。"""

    name: str
    ok: bool
    detail: str


class UnsupportedHolidayYearError(Exception):
    """HOLIDAYS_JPが収録していない年の祝日判定を求められた場合に送出する。

    本SLAは合否判定に使わない運用監視だが、祝日判定ができないまま「土日判定のみで営業日扱い」
    を継続すると、実際は祝日で本来非稼働の日をFAIL扱いにする/しないの判定を無根拠に行うことに
    なる。従って収録範囲外の年は判定不能として即座にconfig_errorでFAILし、運用者に定数更新を
    促す（Codexレビュー指摘・2026-07-16。旧版は土日判定のみで継続していた）。
    """

    def __init__(self, year: int):
        self.year = year
        super().__init__(f"HOLIDAYS_JPが{year}年分を収録していません")


def is_tokyo_business_day(date: datetime.date) -> bool:
    """東証営業日判定（土日 + 日本の祝日 + 年末年始休場を除外）。

    HOLIDAYS_JP / TSE_YEAR_END_NEWYEAR_CLOSURES は2026-2027年分のみ収録（出典は各定数
    直上のコメント参照）。収録範囲外の年は祝日判定が不能なため UnsupportedHolidayYearError を
    送出する（呼び出し側でconfig_errorとして扱いFAILする。年またぎ前に本定数の更新が必要）。

    Raises:
        UnsupportedHolidayYearError: date.year が SUPPORTED_HOLIDAY_YEARS に含まれない場合。
    """
    if date.weekday() >= 5:  # Sat=5, Sun=6
        return False
    if date.year not in SUPPORTED_HOLIDAY_YEARS:
        raise UnsupportedHolidayYearError(date.year)
    return date.isoformat() not in NON_TRADING_DATES


def previous_business_day(date: datetime.date) -> datetime.date:
    """dateより前で直近の東証営業日を返す（dateそのものは含めない）。"""
    d = date - datetime.timedelta(days=1)
    while not is_tokyo_business_day(d):
        d -= datetime.timedelta(days=1)
    return d


def check_state_json(today: datetime.date) -> CheckResult:
    """data/paper_trades/state.json が当日中に更新されているかを確認する（P8チェック1）。

    「当日更新」の判定は内部フィールドを優先する: state.json の last_run_at
    （scripts/daily_screen.py が実行の都度 jq_fetch.now_jst().isoformat() で書き込む
    実際の実行時刻）を最優先で使う。last_screened_date は走査対象のT-1営業日を表す別概念
    のフィールド（daily_screen.pyは「前営業日終値までのデータで判定」する設計のため常に
    当日より前の日付になり得る）であり、「更新されたか」の判定には使わない。

    mtimeフォールバックは「正常にパースできるJSONだが last_run_at キーが無い」場合のみに
    限定する。JSON自体が破損している場合、および last_run_at キーはあるが値が不正
    （パース不能）な場合は、いずれもファイルが壊れている/書き込みが異常終了した可能性を
    示す実質的なシグナルであるため、mtimeで誤魔化さず即FAILとする
    （Codexレビュー指摘・2026-07-16。旧版はいずれのケースもmtimeにフォールバックしており、
    破損検知として機能していなかった）。
    """
    if not STATE_PATH.exists():
        return CheckResult("state_json_today", False, f"{STATE_PATH} が存在しません")

    try:
        raw = STATE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult("state_json_today", False, f"読み込み失敗: {exc}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return CheckResult(
            "state_json_today", False, f"JSON破損のため即FAIL（mtimeフォールバックなし）: {exc}"
        )
    if not isinstance(data, dict):
        return CheckResult(
            "state_json_today", False,
            f"JSONのトップレベルがオブジェクトではありません（型={type(data).__name__}）",
        )

    if "last_run_at" not in data:
        # 正常パース済みJSONだがlast_run_atキーが無い場合のみmtimeフォールバックを許可する。
        mtime = STATE_PATH.stat().st_mtime
        updated_date = datetime.datetime.fromtimestamp(mtime, tz=jsf_daily_archive.JST).date()
        source = "mtime(last_run_atキー無し)"
    else:
        last_run_at = data.get("last_run_at")
        try:
            updated_date = datetime.datetime.fromisoformat(last_run_at).date()
            source = "last_run_at"
        except (TypeError, ValueError) as exc:
            return CheckResult(
                "state_json_today", False,
                f"last_run_at値が不正のため即FAIL（mtimeフォールバックなし）: {last_run_at!r} ({exc})",
            )

    ok = updated_date == today
    detail = f"{source}={updated_date.isoformat()} / 当日={today.isoformat()}"
    return CheckResult("state_json_today", ok, detail)


def check_paper_today_md(today: datetime.date) -> CheckResult:
    """output/paper_today.md 冒頭の「実行時刻: 」日付が当日かを確認する（P8チェック2）。"""
    if not PAPER_TODAY_PATH.exists():
        return CheckResult("paper_today_md_date", False, f"{PAPER_TODAY_PATH} が存在しません")

    body = PAPER_TODAY_PATH.read_text(encoding="utf-8")
    match = REPORT_TIMESTAMP_RE.search(body)
    if not match:
        return CheckResult("paper_today_md_date", False, "「実行時刻: 」行が見つかりません")

    report_date = match.group(1)
    ok = report_date == today.isoformat()
    return CheckResult(
        "paper_today_md_date", ok, f"実行時刻={report_date} / 当日={today.isoformat()}"
    )


# archive_log.jsonl の実スキーマ（jsf_daily_archive.py: archive_dataset()/save_dataset()）で
# 成功系とみなすstatus語彙。run_start はデータセット単位のレコードではないため対象外
# （dataset フィールドが None）。
ARCHIVE_LOG_SUCCESS_STATUSES = {"saved", "skipped_dup"}


def _next_business_day(today: datetime.date) -> datetime.date:
    """東証の翌営業日を返す（祝日定数収録外の年は UnsupportedHolidayYearError を伝播）。"""
    d = today + datetime.timedelta(days=1)
    while not is_tokyo_business_day(d):
        d += datetime.timedelta(days=1)
    return d


def _valid_data_date(value: object, today: datetime.date, dataset: str = "") -> bool:
    """archive_log.jsonl の data_date フィールド（YYYYMMDD文字列想定）の妥当性を検証する。

    8桁数字であること・実在する暦日であることを確認する。上限はデータセット別:
    meigara（品貸料）は制度上 data_date=翌営業日（品貸日）が正常のため翌営業日まで許容
    （実測: 2026-07-15実行→20260716 / 2026-07-16実行→20260717。archive_log.jsonl 実データで確認）。
    その他のデータセットは当日まで。
    """
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        return False
    try:
        d = datetime.date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return False
    upper = _next_business_day(today) if dataset == "meigara" else today
    return d <= upper


def check_archive_log(today: datetime.date) -> CheckResult:
    """data/jsf/archive_log.jsonl を直近run_id単位で集約し、必要データセット群の成功を確認する
    （P8チェック3）。

    LOG_PATH / DATASETS / DATA_ROOT / latest_dataset_file は jsf_daily_archive.py
    （Canonical Module）のものを再利用する。旧版は最終行のtsのみを見ていたが、archiveは
    1回の実行(run_id)でzandaka/shina/meigara/seigenichiranの4データセットをまとめて処理する
    ため、最終行だけでは他データセットの欠測・error状態を見逃す。本チェックは直近run_id内の
    全データセットレコードを集約し、以下をすべて満たさない場合FAILとする:
        (1) 対象4データセット全てにレコードが存在する（欠測なし）
        (2) 各レコードのstatusが成功系(saved/skipped_dup)である（errorはFAIL）
        (3) 各レコードのdata_dateが妥当（8桁日付・未来日でない）
        (4) statusがsavedの場合はrecord["file"]が実在する。skipped_dupの場合はfile列がNone
            （既存内容と重複のため未保存）になる実装のため、latest_dataset_file()で当該
            データセットディレクトリに既存ファイルが1件以上あることを確認する
    直近run_idの日付が許容下限（前営業日）を下回る場合は「run自体が古い」としてFAILする
    （archiveが12:30/19:30 JST実行・本SLAは08:45 JST実行のため、直近runは通常前営業日の
    19:30分になる）。
    """
    log_path = jsf_daily_archive.LOG_PATH
    if not log_path.exists():
        return CheckResult("archive_log_recent", False, f"{log_path} が存在しません")

    records: list[dict] = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue  # 破損行は無視して次行へ（監視が本処理を壊さない原則）

    dataset_records = [r for r in records if r.get("dataset") is not None]
    if not dataset_records:
        return CheckResult("archive_log_recent", False, f"{log_path} にdatasetレコードが0件です")

    runs: dict[str, list[dict]] = {}
    for r in dataset_records:
        runs.setdefault(r.get("run_id"), []).append(r)

    def _run_last_ts(rid: str) -> str:
        return max(r.get("ts", "") for r in runs[rid])

    latest_rid = max(runs, key=_run_last_ts)
    try:
        latest_date = max(
            datetime.datetime.fromisoformat(r["ts"]).date() for r in runs[latest_rid]
        )
    except (KeyError, ValueError) as exc:
        return CheckResult(
            "archive_log_recent", False, f"直近run_id={latest_rid}のts解析に失敗: {exc}"
        )

    threshold = previous_business_day(today)
    if latest_date < threshold:
        return CheckResult(
            "archive_log_recent", False,
            f"直近run_id={latest_rid}の日付={latest_date.isoformat()} が"
            f"許容下限(前営業日)={threshold.isoformat()}を下回っています",
        )

    by_dataset = {r["dataset"]: r for r in runs[latest_rid]}
    expected_datasets = sorted(jsf_daily_archive.DATASETS)
    missing: list[str] = []
    failed: list[str] = []
    for ds in expected_datasets:
        record = by_dataset.get(ds)
        if record is None:
            missing.append(ds)
            continue
        status = record.get("status")
        if status not in ARCHIVE_LOG_SUCCESS_STATUSES:
            failed.append(f"{ds}:status={status}")
            continue
        if not _valid_data_date(record.get("data_date"), today, dataset=ds):
            failed.append(f"{ds}:data_date不正({record.get('data_date')!r})")
            continue
        if status == "saved":
            file_field = record.get("file")
            if not file_field or not (PROJECT_ROOT / file_field).exists():
                failed.append(f"{ds}:保存ファイル不在({file_field})")
        else:  # skipped_dup: file列はNoneが仕様。既存ファイルの実在で代替確認する。
            dataset_dir = jsf_daily_archive.DATA_ROOT / ds
            if jsf_daily_archive.latest_dataset_file(dataset_dir) is None:
                failed.append(f"{ds}:skipped_dupだが既存保存ファイルなし({dataset_dir})")

    ok = not missing and not failed
    detail = (
        f"run_id={latest_rid} 日付={latest_date.isoformat()} 許容下限(前営業日)={threshold.isoformat()} "
        f"欠測データセット={missing or 'なし'} 失敗={failed or 'なし'}"
    )
    return CheckResult("archive_log_recent", ok, detail)


def _applescript_escape(text: str) -> str:
    """AppleScript文字列リテラル内で安全な形にエスケープする。"""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def notify_mac(title: str, message: str) -> None:
    """macOS通知センターへ通知を表示する（osascript経由）。

    通知自体の失敗はSLA監視の主目的（FAIL検知とexit code）を妨げないよう、
    例外は握りつぶさずWARNログのみ出して処理を継続する。OSError（osascript不在等）に加え、
    timeout=10発火時のsubprocess.TimeoutExpiredも同様に捕捉する（Codexレビュー指摘・
    2026-07-16。旧版はTimeoutExpiredを未捕捉のまま本処理全体をクラッシュさせていた）。
    """
    script = (
        f'display notification "{_applescript_escape(message)}" '
        f'with title "{_applescript_escape(title)}" sound name "Basso"'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"WARN: osascript通知に失敗: {exc}", file=sys.stderr)


def _read_mirrored_hash_lines(vault_path: Path) -> list[str]:
    """vault_path内の既存hash行を出現順のリストで返す（frontmatter/noteブロックは対象外）。"""
    if not vault_path.exists():
        return []
    body = vault_path.read_text(encoding="utf-8")
    return [ln.strip() for ln in body.splitlines() if _HASHCHAIN_LINE_RE.match(ln.strip())]


def _atomic_write_text(path: Path, content: str) -> None:
    """tmp書き込み+os.replace（Path.replace）による原子的置換（既存atomic writeイディオムに揃える）。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def mirror_hashchain_to_vault() -> None:
    """run_log_hashchain.txt の新規行のみをvaultへ追記ミラーする（§6付記II A-2 別媒体保存）。

    scripts/kpi_run_evidence.py（Dockerコンテナ内・daily_screen.py経由）がrun_log_hashchain.txtへ
    追記するが、vaultはホストの~/Documents配下でありコンテナからは書けないため、ホストで動く
    本SLAスクリプトの実行毎にミラーを行う。

    自己修復（修正4・Codexレビュー指摘2026-07-16）: 旧実装は「既にミラー済みの行数」を
    vault側の実ハッシュ行数から数えるだけで、途中行の削除・改変・並べ替え（行数が同じまま
    内容だけ壊れるケース）を検知できなかった。本実装は vault側の実hash行列が正本
    （run_log_hashchain.txt）の**先頭からの完全一致prefix**になっているかを検証し、
    一致する場合のみ続きの新規行を追記する。不一致（欠落・改変・並べ替えのいずれか）が
    検知された場合は、ヘッダ込み全文を正本から原子的に再生成する（警告print）。

    通知・SLA判定（本スクリプトの主目的）には一切影響しない表示専用ミラーのため、失敗しても
    例外を外へ伝播させず、WARN表示のみに縮退する（daily_screen.py sync_vault_mirror と同型の
    既存フォールバック方針）。
    """
    try:
        if not RUN_LOG_HASHCHAIN_PATH.exists():
            return
        source_lines = [
            ln.strip() for ln in RUN_LOG_HASHCHAIN_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        if not source_lines:
            return

        mirrored_lines = _read_mirrored_hash_lines(VAULT_HASHCHAIN_MIRROR_PATH)
        VAULT_HASHCHAIN_MIRROR_PATH.parent.mkdir(parents=True, exist_ok=True)

        if mirrored_lines == source_lines[: len(mirrored_lines)]:
            # vault側は正本のprefixとして健全 → 続きだけ追記（従来どおりの高速経路）
            new_lines = source_lines[len(mirrored_lines):]
            if not new_lines:
                return
            if not VAULT_HASHCHAIN_MIRROR_PATH.exists():
                _atomic_write_text(VAULT_HASHCHAIN_MIRROR_PATH, VAULT_HASHCHAIN_MIRROR_HEADER)
            with open(VAULT_HASHCHAIN_MIRROR_PATH, "a", encoding="utf-8") as f:
                for ln in new_lines:
                    f.write(ln + "\n")
            print(f"vault hashchainミラー: 新規{len(new_lines)}行追記（{VAULT_HASHCHAIN_MIRROR_PATH}）")
        else:
            # 不一致（途中行の削除・改変・並べ替えのいずれか）→ ヘッダ込み全文を原子的に再生成
            print(
                f"WARN: vault hashchainミラーが正本のprefixと不一致（途中行の削除・改変・並べ替えの"
                f"可能性。mirrored={len(mirrored_lines)}行 / source={len(source_lines)}行）。"
                f"ヘッダ込み全文を原子的に再生成します。",
                file=sys.stderr,
            )
            content = VAULT_HASHCHAIN_MIRROR_HEADER + "".join(ln + "\n" for ln in source_lines)
            _atomic_write_text(VAULT_HASHCHAIN_MIRROR_PATH, content)
            print(f"vault hashchainミラー: 全文再生成完了（{len(source_lines)}行・{VAULT_HASHCHAIN_MIRROR_PATH}）")
    except OSError as exc:
        print(f"WARN: vault hashchainミラー書込に失敗（SLA監視本体には影響なし・TCC権限を確認）: {exc}", file=sys.stderr)


def _load_master_names() -> dict:
    """data/jquants/master/ の最新月次スナップショットから 銘柄コード→社名 を引く（stdlibのみ）。

    master不在・読込失敗時は空dictを返し、呼び出し側はコード表示に縮退する（表示専用のため）。
    """
    try:
        paths = sorted(glob.glob(str(MASTER_DIR / "*.json.gz")))
        if not paths:
            return {}
        with gzip.open(paths[-1], "rt", encoding="utf-8") as f:
            data = json.load(f).get("data", [])
        return {r.get("Code"): r.get("CoName", "") for r in data}
    except (OSError, ValueError):
        return {}


def render_paper_ledger_to_vault() -> None:
    """ペーパートレード勝敗台帳（ledger.jsonl）をvaultのmdビューへ全文再生成する。

    正本は data/paper_trades/ledger.jsonl（本関数は読み取りのみ・合否判定に不使用）。
    固定名・上書き更新（influx-kpi-cockpit.md と同運用）。失敗しても例外を外へ伝播させず
    WARN縮退（mirror_hashchain_to_vault と同型のフォールバック方針）。
    """
    try:
        if not LEDGER_PATH.exists():
            return
        rows = [json.loads(ln) for ln in LEDGER_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
        names = _load_master_names()
        now = jsf_daily_archive.now_jst().strftime("%Y-%m-%d %H:%M")

        def label(kpi: str) -> str:
            return KPI_LABELS.get(kpi, kpi)

        def coname(code: str) -> str:
            return names.get(code) or code

        def fmt_date(d: str) -> str:
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d and len(d) == 8 else (d or "—")

        closed = sorted((r for r in rows if r.get("status") == "closed"),
                        key=lambda r: (r.get("exit_date") or "", r.get("signal_date") or ""), reverse=True)
        open_pos = sorted((r for r in rows if r.get("status") == "open"),
                          key=lambda r: r.get("signal_date") or "", reverse=True)
        pending = sorted((r for r in rows if r.get("status") == "pending_entry"),
                         key=lambda r: r.get("signal_date") or "", reverse=True)

        rets = [r.get("ret_net") for r in closed if isinstance(r.get("ret_net"), (int, float))]
        wins = sum(1 for x in rets if x > 0)
        total = f"{sum(rets) * 100:+.1f}%" if rets else "—"
        avg = f"{(sum(rets) / len(rets)) * 100:+.1f}%" if rets else "—"

        lines = [
            "---",
            "project: influx",
            "type: progress",
            'folder: "02_Ai/influx/"',
            "categories:",
            '  - "[[influx_ope]]"',
            "tags:",
            "  - project/influx",
            "  - type/progress",
            "---",
            "",
            "# ペーパートレード勝敗台帳（自動生成ビュー）",
            "",
            f"> [!note] 🤖 毎朝08:45自動更新（scripts/kpi_clock_sla.py）。最終更新: {now}",
            "> 正本: repo `data/paper_trades/ledger.jsonl`（機械台帳）。このページは読み取り専用ビューで、"
            "**合否判定には使いません**。きょうの新規候補は [[influx_paper_today]]、全体像は [[influx-kpi-cockpit]]。",
            "",
            "## 確定成績サマリ",
            "",
            f"- 決着済み: **{len(closed)}試合 {wins}勝{len(closed) - wins}敗** / 平均損益 {avg} / 合計 {total}（コスト込み）",
            f"- 保有中: {len(open_pos)}件 / エントリー待ち: {len(pending)}件（台帳 計{len(rows)}行）",
            "- 勝敗は各取引の20営業日後に確定。統計判定は confirmed n≥30（中間）/ 正式は §6付記II の固定窓",
            "",
            "## 決着済み（新しい順）",
            "",
            "| シグナル日 | 銘柄 | 系統 | 出口 | 損益(net) |",
            "|---|---|---|---|---|",
        ]
        for r in closed:
            ret = r.get("ret_net")
            ret_s = f"{ret * 100:+.1f}%" if isinstance(ret, (int, float)) else "—"
            lines.append(
                f"| {fmt_date(r.get('signal_date', ''))} | {coname(r.get('code', ''))} | {label(r.get('kpi_name', ''))} "
                f"| {EXIT_LABELS.get(r.get('exit_reason', ''), r.get('exit_reason', '—'))} | {ret_s} |"
            )
        lines += ["", "## 保有中", "", "| シグナル日 | 銘柄 | 系統 | 買値 |", "|---|---|---|---|"]
        for r in open_pos:
            ep = r.get("entry_price")
            lines.append(
                f"| {fmt_date(r.get('signal_date', ''))} | {coname(r.get('code', ''))} "
                f"| {label(r.get('kpi_name', ''))} | {ep if ep is not None else '—'} |"
            )
        lines += ["", "## エントリー待ち（翌営業日寄付で約定予定）", "",
                  "| シグナル日 | 銘柄 | 系統 |", "|---|---|---|"]
        for r in pending:
            lines.append(
                f"| {fmt_date(r.get('signal_date', ''))} | {coname(r.get('code', ''))} | {label(r.get('kpi_name', ''))} |"
            )
        lines.append("")
        VAULT_PAPER_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        VAULT_PAPER_LEDGER_PATH.write_text("\n".join(lines), encoding="utf-8")
        print(f"vault勝敗台帳ビュー更新: 決着{len(closed)}/保有{len(open_pos)}/待ち{len(pending)}（{VAULT_PAPER_LEDGER_PATH}）")
    except OSError as exc:
        print(f"WARN: vault勝敗台帳ビュー書込に失敗（SLA監視本体には影響なし）: {exc}", file=sys.stderr)


def _append_log(record: dict) -> None:
    """data/monitoring/sla_log.jsonl へ1レコード追記する。

    これは運用監視ログであり、合否判定台帳ではない（data/kpi_trials/trials.jsonl 等の
    正式な検証台帳とは別物）。
    """
    SLA_LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(SLA_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _handle_unsupported_year(
    exc: UnsupportedHolidayYearError, run_id: str, ts: str, today: datetime.date, dry_run: bool
) -> int:
    """UnsupportedHolidayYearError発生時の共通処理（status=config_errorでFAIL）。

    is_tokyo_business_day(today)自体、またはprevious_business_day経由で過去年に遡って
    祝日判定できなかった場合（check_archive_log内）のいずれで発生しても同一の扱いとする。
    """
    print(
        f"FATAL: {exc}。scripts/kpi_clock_sla.py の祝日定数を{exc.year}年分について"
        "追加更新してください（土日判定のみでの継続は行いません）。",
        file=sys.stderr,
    )
    record = {
        "run_id": run_id,
        "ts": ts,
        "date": today.isoformat(),
        "business_day": None,
        "status": "config_error",
        "checks": [],
        "detail": f"HOLIDAYS_JPが{exc.year}年分を収録していないため祝日判定不能（土日判定のみでの継続を停止）",
    }
    if dry_run:
        print("[dry-run] ログ書き込みなし")
    else:
        _append_log(record)
    return 1


def main() -> int:
    """3チェックを実行し、data/monitoring/sla_log.jsonlへ記録、FAIL時はmacOS通知+exit 1。"""
    parser = argparse.ArgumentParser(
        description=(
            "前向き検証クロックの死活監視SLA（P8/BG/R7）。経営計画/運用監視用で合否判定には"
            "使わない。粗い死活監視であり、KPI別coverage証明（§6完全性プロトコルの第一証跡）"
            "ではない。"
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="通知・ログ書き込みを行わず、チェック結果の表示のみ行う確認モード",
    )
    parser.add_argument(
        "--render-ledger-only",
        action="store_true",
        help="SLAチェック・ログ書き込みを行わず、vaultの勝敗台帳ビューだけを再生成する",
    )
    args = parser.parse_args()

    if args.render_ledger_only:
        render_paper_ledger_to_vault()
        return 0

    print(
        "[NOTE] 本SLAは粗い死活監視であり、KPI別coverage証明（§6完全性プロトコルの第一証跡）"
        "ではない。"
    )

    today = jsf_daily_archive.now_jst().date()
    run_id = uuid.uuid4().hex
    ts = jsf_daily_archive.now_jst().isoformat()

    try:
        business_day = is_tokyo_business_day(today)
    except UnsupportedHolidayYearError as exc:
        return _handle_unsupported_year(exc, run_id, ts, today, args.dry_run)

    if not business_day:
        print(f"SKIP: {today.isoformat()} は東証非営業日のためチェックをスキップします")
        record = {
            "run_id": run_id,
            "ts": ts,
            "date": today.isoformat(),
            "business_day": False,
            "status": "SKIP",
            "checks": [],
            "detail": "東証非営業日（土日/日本の祝日/年末年始休場）のためチェックをスキップ",
        }
        if args.dry_run:
            print("[dry-run] ログ書き込みなし")
        else:
            _append_log(record)
            mirror_hashchain_to_vault()
            render_paper_ledger_to_vault()
        return 0

    try:
        checks = [
            check_state_json(today),
            check_paper_today_md(today),
            check_archive_log(today),
        ]
    except UnsupportedHolidayYearError as exc:
        return _handle_unsupported_year(exc, run_id, ts, today, args.dry_run)
    overall_ok = all(c.ok for c in checks)

    print(f"=== 前向き検証クロック 死活監視SLA（{today.isoformat()}・営業日）===")
    for c in checks:
        mark = "OK" if c.ok else "FAIL"
        print(f"[{mark}] {c.name}: {c.detail}")
    print(f"総合判定: {'OK' if overall_ok else 'FAIL'}")

    record = {
        "run_id": run_id,
        "ts": ts,
        "date": today.isoformat(),
        "business_day": True,
        "status": "OK" if overall_ok else "FAIL",
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
    }

    if args.dry_run:
        print("[dry-run] 通知・ログ書き込みなし")
        return 0 if overall_ok else 1

    _append_log(record)
    mirror_hashchain_to_vault()
    render_paper_ledger_to_vault()

    if not overall_ok:
        failed_names = ", ".join(c.name for c in checks if not c.ok)
        notify_mac(
            "influx: 前向き検証クロックSLA違反",
            f"{today.isoformat()} チェック失敗: {failed_names}",
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
