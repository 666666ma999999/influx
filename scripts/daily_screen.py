#!/usr/bin/env python3
"""ペーパートレード観察リスト 日次スクリーニング（毎営業日の自動シグナル判定・記録）。

config/paper_watchlist.json の観察対象KPI（volshock_5x/shortcover_x_bear/pead_gap8_vol3）を
「前営業日(T)終値までのデータ」で判定し、新規シグナルを data/paper_trades/ledger.jsonl に
2段階記録する: (1)シグナル検出時=entry_price未定・status=pending_entry (2)T+1（+defer_entry猶予）
のAdjOが確定した時点でentry_priceを確定=status=open。実際の状態遷移は scripts/paper_eval.py に
委譲する（Canonical Module原則・本スクリプトは重複実装しない）。

シグナル判定ロジックは全て既存KPIスクリプトの生成関数をそのまま import 再利用する
（再実装しない。config/paper_watchlist.jsonの_comment参照）:
    - volshock_5x       -> kpi_volshock_signals.generate_volshock_signals()
    - shortcover_x_bear -> kpi_shortcover_signals.generate_shortcover_signals()
                           + measure_base_rate.build_regime_series() でbear日のみ抽出
    - pead_gap8_vol3    -> kpi_pead_signals.generate_pead_signals()
volshock系（volshock_5x/volshock_x_above200/volshock_x_above200_quiet）は params に
"quiet_min" があれば、候補（当日は数件程度なので軽量）に第13周のquiet_ratio
（kpi_volshock_v2_amplifiers.compute_quiet_ratio・§7-B v2-2）を計算し閾値でさらに絞り込む
（汎用実装: kpi_nameをハードコード分岐せず、paramsにquiet_minがあるvolshock系エントリ全てに適用）。
シグナルは §0確定の対象ユニバース（月次売買代金TOP500・window=21・measure_base_rate.build_universe
を再利用）に属するものだけをledgerに記録する（in-sample/holdoutの検証スコープと整合させるため。
TOP500外は観察対象から除外し件数のみ報告する）。

=== J-Quants当日データ可用性プローブ（2026-07-07実施・実データ） ===
既存キャッシュを調査した結果、2026-07-06分(bars/fins/shortsale)を「同日05:57 JST」「同日10:41 JST」
「同日17:44 JST」の3時点で取得試行した過去ログが残っており、いずれも count=0（未公表）だった
（jq_fetch.py の fetch_snapshot_for_date は既存ファイルの中身を見ずスキップするため、この空
レスポンスが恒久キャッシュとして固着していた＝本スクリプトの ensure_recent_data() が起動時に
空キャッシュを検知して強制再取得するのはこのバグの回避策）。翌営業日 2026-07-07 10:05 JST に
再取得したところ bars=4437件/fins=15件/shortsale=1115件と正常取得できた。
結論: 同日夜間（17時台）でもshortsale公表が間に合わない実績を確認したため、本スクリプトは
「前営業日(T)終値までのデータで判定し、想定エントリーはT+1寄付」という設計（=T+1寄付ルールと
整合する「翌朝実行」構成）を採用する。実行時刻は当面 平日07:30 JST とするが、07:30時点でT
（前日）のデータが確実に揃っているかは本プローブでは未検証（検証できたのは翌営業日10:05時点の
みで、9:00の市場寄り付き後）。エントリー価格自体はT+1寄付が確定した後の“次回実行”で埋める
2段階記録のため、当日朝の実行タイミングの正確性は本質的な問題にならない（=間に合わなくても
翌日以降のリトライで自然に追いつく設計。ensure_recent_data() のキャッチアップ機構を参照）。

Usage:
    python3 scripts/daily_screen.py                 # 通常実行（データ更新+シグナル判定+ledger追記+レポート生成）
    python3 scripts/daily_screen.py --dry-run        # 今あるキャッシュのみでシグナル判定→件数表示。データ取得・ledger書込みなし
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import jq_fetch  # noqa: E402  (Canonical Module: DATA_ROOT / read_json_gz / now_jst / get_api_key / fetch_topix を再利用)
import kpi_pead_signals  # noqa: E402  (Canonical Module: generate_pead_signals を再利用)
import kpi_shortcover_signals  # noqa: E402  (Canonical Module: generate_shortcover_signals を再利用)
import kpi_volshock_signals  # noqa: E402  (Canonical Module: generate_volshock_signals を再利用)
import kpi_volshock_v2_amplifiers  # noqa: E402  (Canonical Module: compute_quiet_ratio を再利用・第13周A)
import measure_base_rate  # noqa: E402  (Canonical Module: カレンダー・regime・universe構築を再利用)
import paper_eval  # noqa: E402  (Canonical Module: ledger I/O・状態更新・scoreboard集計を再利用)

PROJECT_ROOT = Path(__file__).parent.parent
WATCHLIST_PATH = PROJECT_ROOT / "config" / "paper_watchlist.json"
STATE_PATH = PROJECT_ROOT / "data" / "paper_trades" / "state.json"
TODAY_REPORT_PATH = PROJECT_ROOT / "output" / "paper_today.md"

# Vault ミラー（ユーザーは vault 側しかドキュメントを読まないため、レポート本体を毎回転写する。
# 2026-07-09 ユーザー指示。正本は TODAY_REPORT_PATH（repo）・vault 側は読み取り専用ミラー）
VAULT_MIRROR_PATH = Path.home() / "Documents" / "Obsidian Vault" / "02_Ai" / "influx" / "influx_paper_today.md"
VAULT_MIRROR_FRONTMATTER = """---
project: influx
type: report
folder: "02_Ai/influx/"
categories:
  - "[[influx_ope]]"
last_updated: {today}
tags:
  - project/influx
  - type/report
---
> [!note] 🤖 自動生成ミラー（毎朝07:30・手編集禁止）
> 正本: `Desktop/biz/influx/output/paper_today.md`（scripts/daily_screen.py が上書き）。
> このノートへの追記・編集は次回実行で消えます。

"""


REPORT_TIMESTAMP_RE = re.compile(r"実行時刻: (\d{4}-\d{2}-\d{2})")


def _extract_report_date(body: str) -> str:
    """レポート本文の「実行時刻: ISO8601」行から日付部分を取り出す（監査V-7）。

    早期リターン日（走査済み・データ未公表）は新しいレポートが生成されず本文の実行時刻が
    前回実行のまま据え置かれるため、ミラー転写時に毎回 now_jst() の「今日」をfrontmatterへ
    スタンプすると、本文（前回実行日）とミラーのlast_updated（今日）が1日ずれる不整合が
    生じていた。本文の実際の実行時刻から日付を取り出して使うことでこれを解消する。
    パース失敗時は表示専用のためジョブを落とさず now_jst() の日付にフォールバックする。
    """
    m = REPORT_TIMESTAMP_RE.search(body)
    return m.group(1) if m else jq_fetch.now_jst().date().isoformat()


def sync_vault_mirror() -> None:
    """repo の当日レポートを vault へ転写する（読み取り専用ミラー）。

    launchd 実行時に ~/Documents が TCC で書込不可の可能性があるため、失敗しても
    朝ジョブ本体（スクリーニング・ledger・state）を落とさず WARN のみ出す。
    早期リターン日（走査済み・週末）でも直近レポートを再転写するため、main() の
    全ての正常経路から呼ばれる。
    """
    try:
        if not TODAY_REPORT_PATH.exists():
            return
        body = TODAY_REPORT_PATH.read_text(encoding="utf-8")
        front = VAULT_MIRROR_FRONTMATTER.format(today=_extract_report_date(body))
        VAULT_MIRROR_PATH.write_text(front + body, encoding="utf-8")
        print(f"vaultミラー更新: {VAULT_MIRROR_PATH}")
    except BaseException as e:  # noqa: BLE001  (表示系の失敗で朝ジョブを落とさない)
        print(f"WARN: vaultミラー書込に失敗（朝ジョブ本体には影響なし・TCC権限を確認）: {e}", file=sys.stderr)

UNIVERSE_WINDOW = 21  # scripts/measure_base_rate.py 既定・v0系KPI検証と同一
UNIVERSE_TOP_N = 500  # カタログ§0「月次売買代金TOP500」

PEAD_LOOKBACK_BDAYS = 2  # 反応日Gが対象日に来る開示は前営業日にも起こりうるための走査開始オフセット

VOLSHOCK_FAMILY_KPI_NAMES = ("volshock_5x", "volshock_x_above200", "volshock_x_above200_quiet")

# 次点候補（team-lead 2026-07-07指示・「新規シグナルなしの日が続き市場で何が起きているか見えない」
# への対応）: 判定基準（config/paper_watchlist.jsonのparams）そのものは一切変更せず、
# 「惜しかった」候補を参考表示するためだけの近傍バンド下限。上限は各entryの実際の閾値
# （vol_multiplier=5.0・dev200>0・quiet_min）をそのまま使う。表示専用でledger/state/scoreboardには
# 一切影響しない。
NEAR_MISS_VOL_MULTIPLIER_FLOOR = 4.0  # 出来高4.0〜5.0倍（5倍未達）
NEAR_MISS_DEV200_FLOOR = -0.02  # dev200が-2%〜0%（200日線のわずか下）
NEAR_MISS_QUIET_FLOOR = 1.0  # quiet_ratio 1.0〜1.2（未達）
MAX_NEXT_CANDIDATES_ROWS = 10  # 次点候補セクション全体（カテゴリ1+2合計）の表示上限行数

# 回収R1+R2（2026-07-08 team-lead指示・「レコメンドゼロ」根本診断への対応）:
# 稼働可視化・チャンピオン基準は config/paper_watchlist.json の該当エントリを Canonical Module として
# 参照する（avg_monthly_signals等の数値は本ファイルに複製しない）。ギャップ分布(中央値/最大)のみ、
# in-sample期間の生データから独立に導出した記述統計であり config には存在しないため以下に定数化する。
CHAMPION_KPI_NAME = "volshock_x_above200_quiet"  # チャンピオン構成（tasks/stock_algo_kpi_loop.md 第13周確定）
RECENT_SIGNALS_KPI_NAMES = (CHAMPION_KPI_NAME, "volshock_x_above200")  # 60bdバックフィル対象（team-lead指定の2KPI）
RECENT_SIGNALS_LOOKBACK_BDAYS = 60
RECENT_SIGNALS_CACHE_PATH = PROJECT_ROOT / "data" / "paper_trades" / "recent_signals_cache.json"
BDAYS_PER_MONTH_APPROX = 21  # 営業日/月の換算規約（measure_base_rate.UNIVERSE_WINDOWと同一の慣行値）

# チャンピオン(CHAMPION_KPI_NAME)のin-sampleシグナル間隔の記述統計（2026-07-08実測・再現手順:
# output/kpi/volshock_v2_amplifiers/signals_features_volshock_x_above200.csv の quiet_bucket=="quiet"
# 行から distinct signal_date を昇順抽出→隣接日の営業日差分(n=69日付間隔・68ギャップ)。
# median=13, max=85 と確定・team-lead診断の数値と一致確認済み）。
CHAMPION_GAP_MEDIAN_BDAYS = 13
CHAMPION_GAP_MAX_BDAYS = 85
# ゼロ日数警戒閾値（tasks/stock_algo_kpi_loop.md 2026-07-08「実測分布ベース」で確定した値をそのまま採用）。
GAP_WARN_LIGHT_BDAYS = 40  # 超過で軽度注意
GAP_WARN_INVESTIGATE_BDAYS = 60  # 超過で要調査
GAP_WARN_ANOMALY_BDAYS = 85  # 超過で異常（=歴史分布の最大ギャップと同値）

# UL10表示タグ（第17周所見の運用反映・2026-07-09 team-lead指示）:
# catalog §7-F・output/kpi/ul_fade_standalone/report.md で確定した「直近10営業日にストップ高
# (bars UL=='1') 3回以上に達した銘柄の20bd期待値=-8.13%（CI上限-1.41%）」を、チャンピオン
# (CHAMPION_KPI_NAME)該当銘柄・次点候補の表示行にのみ注意喚起として併記する（表示専用・
# シグナル発火条件・スクリーニングロジックは一切変更しない）。
# 定義は kpi_volshock_v3_ul_earnings.compute_ul_count_10bd と同一（シグナル日Dを含む直近
# UL10_WINDOW_BDAYS営業日[D-9,D]のUL=='1'日数）だが、同モジュールは kpi_event_study 等の
# 検証専用依存を持つ一回性の分析スクリプトのため、毎朝の本番スクリーニングからimportして
# 結合させない（研究/本番分離・docs/research-workflow.md）。代わりに本ファイルが既に
# compute_quiet_ratio等と同じ経路で使っている measure_base_rate.load_bars_day（bars読込の
# Canonical Module・functools.lru_cache済み）を直接再利用して同一ロジックをここに定義する。
UL10_WINDOW_BDAYS = 10  # D-9..D（kpi_volshock_v3_ul_earnings.UL_WINDOW_BDAYSと同値）
UL10_FADE_WARN_MIN = 3  # 第17周確定: 3回以上でEV-8.13%（catalog §7-F）
UL10_FADE_WARN_TEXT = "⚠️祭り終盤(in-sample EV-8.13%)"


def compute_ul_count_10bd(code: str, signal_bd: str, bday_index: dict[str, int], all_bdays: list[str]) -> Optional[int]:
    """シグナル日Dを含む直近UL10_WINDOW_BDAYS営業日[D-9,D]のストップ高(UL=='1')回数を数える。

    ウォームアップ不足（データ開始日付近）はNone（呼び出し側はUL10=?で明示・サイレント省略しない）。
    """
    idx = bday_index[signal_bd]
    if idx - (UL10_WINDOW_BDAYS - 1) < 0:
        return None
    window_days = all_bdays[idx - (UL10_WINDOW_BDAYS - 1) : idx + 1]
    count = 0
    for d in window_days:
        rec = measure_base_rate.load_bars_day(d).get(code)
        if rec is not None and rec.get("UL") == "1":
            count += 1
    return count


UL10_FOOTNOTE = (
    "UL10=直近10営業日（signal_dateを含む）のストップ高日数。⚠️の根拠はTOP500全体の"
    "in-sample実測（第17周 catalog §7-F・UL10>=3群の20bd EV-8.13%）であり、参考情報。"
    "UL10=?はbars欠損等で計算不能（安全の意味ではない）。"
)


def format_ul10_tag(code: str, signal_date: str, bday_index: dict[str, int], all_bdays: list[str]) -> str:
    """UL10表示タグ文字列を返す（bars欠損・ウォームアップ不足でNoneならUL10=?）。

    表示専用タグの失敗が朝ジョブ全体（シグナル判定・ledger・state更新）を落とさないよう、
    ここで例外を完全に隔離する（bday_index未登録日・barsキャッシュ欠損・破損JSONは
    measure_base_rate側でKeyError/SystemExit等になりうる）。
    """
    try:
        count = compute_ul_count_10bd(code, signal_date, bday_index, all_bdays)
    except BaseException:  # noqa: BLE001  (SystemExit含む・表示専用のため全て?へ縮退)
        return "UL10=?"
    if count is None:
        return "UL10=?"
    tag = f"UL10={count}回"
    if count >= UL10_FADE_WARN_MIN:
        tag += f" {UL10_FADE_WARN_TEXT}"
    return tag


# --- 稼働監視（Cookie鮮度・margin鮮度・jsfアーカイブ欠損。監査O-5/O-6/O-7・表示専用） --------------
# 3項目とも判定ロジック（シグナル発火条件）には一切関与しない。取得・チェック失敗時も
# 朝ジョブ本体（スクリーニング・ledger・state更新）を落とさずWARN表示に縮退する。

X_PROFILES_DIR = PROJECT_ROOT / "x_profiles"
COOKIE_AGE_WARN_DAYS = 14  # 監査O-5: 実測80日のアカウントを「21日」と誤表示していた事例の再発防止
MARGIN_STALE_WARN_DAYS = 90  # 監査O-6
JSF_GAP_CHECK_SCRIPT = Path(__file__).parent / "check_jsf_gaps.py"  # 監査O-7


def format_cookie_age_lines() -> list[str]:
    """x_profiles/*/cookies.json の鮮度をmtimeから表示する（監査O-5・表示専用）。"""
    if not X_PROFILES_DIR.exists():
        return []
    lines = []
    now_ts = jq_fetch.now_jst().timestamp()
    for account_dir in sorted(p for p in X_PROFILES_DIR.iterdir() if p.is_dir()):
        cookie_path = account_dir / "cookies.json"
        if not cookie_path.exists():
            continue
        try:
            age_days = int((now_ts - cookie_path.stat().st_mtime) // 86400)
        except OSError as e:
            lines.append(f"- Cookie鮮度(@{account_dir.name}): 確認失敗（{e}）")
            continue
        tag = f"⚠️{age_days}日経過（{COOKIE_AGE_WARN_DAYS}日超・再取得検討）" if age_days > COOKIE_AGE_WARN_DAYS else f"{age_days}日経過"
        lines.append(f"- Cookie鮮度(@{account_dir.name}): {tag}")
    return lines


def format_margin_freshness_lines(margin_dir: Path) -> list[str]:
    """data/jquants/margin/ 最新ファイルの日付鮮度を表示する（監査O-6・表示専用・本番判定には未使用）。"""
    if not margin_dir.exists():
        return ["- margin鮮度: data/jquants/margin/ が存在しません"]
    files = sorted(margin_dir.glob("*.json.gz"))
    if not files:
        return ["- margin鮮度: データなし（data/jquants/margin/未取得）"]
    latest_date_str = files[-1].name.split(".")[0]
    try:
        latest_date = datetime.datetime.strptime(latest_date_str, "%Y%m%d").date()
    except ValueError:
        return [f"- margin鮮度: 確認失敗（ファイル名から日付を解釈できません: {files[-1].name}）"]
    age_days = (jq_fetch.now_jst().date() - latest_date).days
    tag = f"⚠️最新={latest_date_str}（{age_days}日前・{MARGIN_STALE_WARN_DAYS}日超）" if age_days > MARGIN_STALE_WARN_DAYS else f"最新={latest_date_str}（{age_days}日前）"
    return [f"- margin鮮度: {tag}"]


def format_jsf_gap_check_lines() -> list[str]:
    """scripts/check_jsf_gaps.py を呼び出し、結果1行を稼働状況に表示する（監査O-7・表示専用）。

    サブプロセス自体の失敗（スクリプト例外・タイムアウト等）も朝ジョブ本体を落とさず
    WARN表示に縮退する（check=Trueにしない）。
    """
    try:
        result = subprocess.run(
            [sys.executable, str(JSF_GAP_CHECK_SCRIPT)],
            capture_output=True, text=True, timeout=30,
        )
        stdout_lines = [ln for ln in result.stdout.strip().splitlines() if ln]
        summary = stdout_lines[-1] if stdout_lines else "(出力なし)"
        return [f"- jsf日次アーカイブ欠損検知: {summary}"]
    except Exception as e:  # noqa: BLE001  (監視系の失敗で朝ジョブ本体を落とさない)
        return [f"- jsf日次アーカイブ欠損検知: 確認失敗（{e}）"]


# --- watchlist設定読み込み -----------------------------------------------------------


def load_watchlist() -> list[dict]:
    obj = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    return obj["watchlist"]


# --- データ鮮度確保（jq_fetch.py 呼び出し + 既知の空キャッシュ固着バグの回避） -----------------


def clear_empty_cache_files(kind: str, dates: list[str]) -> int:
    """指定日付群のうち「ファイルは存在するがdata配列が空」のキャッシュを削除し強制再取得対象にする。

    jq_fetch.fetch_snapshot_for_date はファイル存在のみでスキップ判定するため、市場閉場前後の
    際どいタイミングで空レスポンスがキャッシュされると恒久的に再取得されない
    （本ファイルdocstring「J-Quants当日データ可用性プローブ」参照・2026-07-07実データで確認）。
    fins は開示が無い日に正当に空となるため対象外（jq_fetch.py print_status と同じ扱い）。
    """
    if kind == "fins":
        return 0
    removed = 0
    kind_dir = jq_fetch.DATA_ROOT / kind
    for d in dates:
        path = kind_dir / f"{d}.json.gz"
        if not path.exists():
            continue
        try:
            obj = jq_fetch.read_json_gz(path)
        except (OSError, EOFError):
            path.unlink(missing_ok=True)
            removed += 1
            continue
        if not obj.get("data"):
            path.unlink()
            removed += 1
    return removed


def refresh_recent_data(start_bd: str, end_bd: str, all_bdays: list[str]) -> None:
    """[start_bd, end_bd] 区間の bars/fins/shortsale を jq_fetch.py 経由で最新化する（冪等）。"""
    target_days = [d for d in all_bdays if start_bd <= d <= end_bd]
    for kind in ("bars", "fins", "shortsale"):
        removed = clear_empty_cache_files(kind, target_days)
        if removed:
            print(f"[refresh] {kind}: 空キャッシュ{removed}件を削除し再取得対象にしました", file=sys.stderr)
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / "jq_fetch.py"),
             "--only", kind, "--start", start_bd, "--end", end_bd],
            check=True,
        )


def refresh_topix_if_stale(target_bd: str) -> None:
    """topix.json.gz は jq_fetch.fetch_topix() が「存在すれば全期間スキップ」する設計のため
    日次では自動更新されない（2026-07-07実データで確認: bars/fins/shortsaleは20260706まで
    取得済みだったのにtopixは20260703止まりだった）。target_bd時点のレジーム判定
    （TOPIX SMA200）に必要な分だけ、古ければ削除して jq_fetch.fetch_topix() を再実行する
    （jq_fetch.py 本体は変更せず、呼び出し側で鮮度を確保するだけの最小介入）。
    """
    path = jq_fetch.DATA_ROOT / "topix.json.gz"
    if path.exists():
        obj = jq_fetch.read_json_gz(path)
        dates = [r["Date"].replace("-", "") for r in obj.get("data", [])]
        if dates and max(dates) >= target_bd:
            return
        print(
            f"[refresh] topix.json.gz が古い(最新={max(dates) if dates else 'なし'} < 必要={target_bd})。再取得します",
            file=sys.stderr,
        )
        path.unlink()
    api_key = jq_fetch.get_api_key()
    run_id = uuid.uuid4().hex
    jq_fetch.fetch_topix(api_key, run_id)


def refresh_master_if_stale(target_bd: str, calendar_days: list[tuple[str, str]]) -> None:
    """data/jquants/master/（月次ユニバース構築の材料・月末営業日ごとに1ファイル）を最新化する。

    === CRITICAL時限爆弾（2026-07-08「レコメンドゼロ」根本診断で発見） ===
    master は jq_fetch.py --only master を明示的に呼ばない限り自動更新されない。本スクリプトは
    bars/fins/shortsale（refresh_recent_data）とtopix（refresh_topix_if_stale）は日次で鮮度確保
    するが、master にはこの手当てが無かった。UniverseCache.universe_for_month() は必要な月末営業日
    のmasterが無いと measure_base_rate.build_universe() のSystemExitを握りつぶし「ユニバース外」
    扱い（=新規シグナル0件と区別不能）にする設計のため、月が変わって新しい月末営業日のmasterが
    未取得のまま放置されると、シグナル判定ロジック自体は健全でも全シグナルが静かに消える
    （2026-08-03以降、2026-07-31分masterが無ければ発火する想定だった障害）。

    target_bd（走査末尾の営業日）までに必要な月末営業日を
    jq_fetch.month_end_business_days_in_range で列挙し、未取得ファイルがあれば
    jq_fetch.py --only master を呼んで補充する（jq_fetch.fetch_snapshot_for_date は既存ファイルを
    無条件スキップするため、範囲を全履歴始点からtarget_bdまでにしても実際にAPIを叩くのは不足分のみ
    ・冪等かつ軽量。jq_fetch.py本体・build_universe()は一切変更しない）。
    """
    expected = jq_fetch.month_end_business_days_in_range(calendar_days, jq_fetch.DEFAULT_START, target_bd)
    missing = [d for d in expected if not (jq_fetch.DATA_ROOT / "master" / f"{d}.json.gz").exists()]
    if not missing:
        return
    print(
        f"[refresh] master: 不足{len(missing)}件（{missing[0]}〜{missing[-1]}）を検知。取得します",
        file=sys.stderr,
    )
    subprocess.run(
        [sys.executable, str(Path(__file__).parent / "jq_fetch.py"),
         "--only", "master", "--start", jq_fetch.DEFAULT_START, "--end", target_bd],
        check=True,
    )


# --- 走査対象レンジの決定（キャッチアップ込み） --------------------------------------------


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def compute_scan_range(state: dict, all_bdays: list[str]) -> tuple[Optional[str], Optional[str]]:
    """未スクリーニングの営業日範囲 [start, end] を返す（無ければ (None, None)）。

    T = 「今日より前」の最新営業日（本スクリプトの設計上、前営業日終値までしか使わない・
    docstring「J-Quants当日データ可用性プローブ」参照）。state.last_screened_date が無ければ
    初回実行としてT一日のみを対象にする（過去に遡った一括生成はしない）。
    """
    today_str = jq_fetch.now_jst().strftime("%Y%m%d")
    completed = [d for d in all_bdays if d < today_str]
    if not completed:
        return None, None
    t_max = completed[-1]
    last_screened = state.get("last_screened_date")
    if last_screened is None:
        return t_max, t_max
    candidates = [d for d in completed if d > last_screened]
    if not candidates:
        return None, None
    return candidates[0], t_max


def _bars_available(d: str) -> bool:
    path = jq_fetch.DATA_ROOT / "bars" / f"{d}.json.gz"
    if not path.exists():
        return False
    obj = jq_fetch.read_json_gz(path)
    return bool(obj.get("data"))


def _bars_shortsale_available(d: str) -> bool:
    if not _bars_available(d):
        return False
    path = jq_fetch.DATA_ROOT / "shortsale" / f"{d}.json.gz"
    if not path.exists():
        return False
    obj = jq_fetch.read_json_gz(path)
    return bool(obj.get("data"))


def pead_effective_end(end_bd: str, bday_index: dict[str, int], all_bdays: list[str]) -> str:
    """PEADは開示時刻>=15:00の反応日がend_bdの翌営業日に押し出されうる
    （kpi_pead_signals.reaction_day）。その翌営業日のbarsがまだ存在しない場合
    （=まだ発生していない営業日）、end_bd当日の開示分は今回の走査から除外し
    end_bdを1営業日前倒しする（該当開示は翌営業日のbarsが揃った次回実行で
    自然に拾われる。measure_base_rate.load_bars_dayのFATAL停止を避けるための
    呼び出し側ガード。kpi_pead_signals.py 自体は変更しない）。
    """
    idx = bday_index[end_bd]
    if idx + 1 < len(all_bdays) and not _bars_available(all_bdays[idx + 1]):
        return all_bdays[idx - 1] if idx > 0 else end_bd
    return end_bd


def latest_available_bday_overall(all_bdays: list[str], max_lookback: int = 40) -> Optional[str]:
    """カレンダー全体の末尾から遡り、bars/shortsaleが揃っている直近の営業日を返す（--dry-run用）。

    catch-up走査用の latest_fully_available_bday とは異なり「連続性」は要求しない
    （どこか1日でも最新の有効キャッシュが見つかればそれで良い・過去に遡って履歴全体を
    先頭から舐めると2016年より前の日付でbarsが存在せず即座にNoneになってしまうため分離した）。
    """
    today_str = jq_fetch.now_jst().strftime("%Y%m%d")
    past_or_today = [d for d in all_bdays if d <= today_str]
    for d in reversed(past_or_today[-max_lookback:]):
        if _bars_shortsale_available(d):
            return d
    return None


def latest_fully_available_bday(candidate_days: list[str]) -> Optional[str]:
    """candidate_days（昇順・通常は数日〜十数日程度の狭いキャッチアップ区間）のうち
    bars/shortsale が両方非空で揃っている最後の日を返す（fins は開示が無い日に正当に空に
    なりうるため判定対象に含めない）。未公表で連続性が途切れた時点で打ち切る（間の空白日を
    残したまま先の日だけ処理すると§0のPIT前提が壊れるため）。
    """
    result: Optional[str] = None
    for d in candidate_days:
        if not _bars_shortsale_available(d):
            break
        result = d
    return result


# --- ユニバース判定（TOP500・§0確定スコープとの整合） ----------------------------------------


class UniverseCache:
    """signal_dateが属する月(YYYY-MM)ごとの直近確定済み月末TOP500ユニバースをメモ化する。

    measure_base_rate.build_universe() をそのまま再利用する（Canonical Module）。判定規約は
    kpi_event_study._universe_membership と同じ「signal_monthより厳密に前の直近月末」。

    rank_for() は次点候補「ユニバース外の完全通過者」表示（2026-07-07team-lead指示）専用の
    追加機能。同じbuild_universe()をtop_n=n_intersect（=その月の全候補数）で再度呼び出すだけで
    全銘柄ランキングを取得する（judgment ロジック自体は一切変更しない・top_n引数を実際の候補数
    ぴったりに指定するのでbuild_universe内蔵のWARN「ユニバースが{top_n}件未満」も誘発しない）。
    universe_for_month()の在籍判定（TOP500・in_universe()の戻り値）には一切影響しない別キャッシュ。
    """

    def __init__(self, calendar_days: list[tuple[str, str]], bday_index: dict[str, int], all_bdays: list[str]):
        self._calendar_days = calendar_days
        self._bday_index = bday_index
        self._all_bdays = all_bdays
        self._cache: dict[str, Optional[set]] = {}
        self._stats_cache: dict[str, dict] = {}
        self._full_rank_cache: dict[str, Optional[dict[str, int]]] = {}
        self._failure_reasons: dict[str, str] = {}  # month -> 理由（R1「⚠️見える化」・0件と区別可能にする）

    def _t_date_for_month(self, month: str) -> Optional[str]:
        month_ends = measure_base_rate.month_ends_in_range(self._calendar_days, "2016-08", month)
        prior_ends = [d for d in month_ends if d[:6] < month.replace("-", "")]
        return prior_ends[-1] if prior_ends else None

    def universe_for_month(self, month: str) -> Optional[set]:
        if month in self._cache:
            return self._cache[month]
        t_date = self._t_date_for_month(month)
        if t_date is None:
            self._cache[month] = None
            self._failure_reasons[month] = "対象月の前月末営業日が特定できません（データ開始日に近すぎる可能性）"
            return None
        try:
            selected, stats = measure_base_rate.build_universe(
                t_date, self._bday_index, self._all_bdays, UNIVERSE_WINDOW, UNIVERSE_TOP_N
            )
        except SystemExit as e:
            print(f"WARN: {month}のユニバース構築に失敗（{e}）。この月のシグナルはユニバース判定不能扱い", file=sys.stderr)
            self._cache[month] = None
            self._failure_reasons[month] = f"t_date={t_date}のユニバース構築失敗: {e}"
            return None
        codes = {code for code, _ in selected}
        self._cache[month] = codes
        self._stats_cache[month] = stats
        return codes

    def in_universe(self, code: str, signal_date: str) -> bool:
        month = f"{signal_date[:4]}-{signal_date[4:6]}"
        universe = self.universe_for_month(month)
        if universe is None:
            return False
        return code in universe

    def failed_months(self) -> dict[str, str]:
        """これまでに universe_for_month() がユニバース構築に失敗した月とその理由を返す
        （R1「⚠️見える化」用・表示専用。判定結果自体（in_universeがFalseを返す挙動）には影響しない）。
        """
        return dict(self._failure_reasons)

    def rank_for(self, code: str, signal_date: str) -> Optional[int]:
        """全銘柄中の月間売買代金順位（1始まり）。次点候補「ユニバース外の完全通過者」表示専用
        （§0確定のTOP500在籍判定・in_universe()には一切影響しない表示専用の追加計算）。
        """
        month = f"{signal_date[:4]}-{signal_date[4:6]}"
        if month not in self._cache:
            self.universe_for_month(month)
        if self._cache.get(month) is None:
            return None
        if month not in self._full_rank_cache:
            t_date = self._t_date_for_month(month)
            n_intersect = self._stats_cache[month]["n_intersect"]
            try:
                selected_full, _stats = measure_base_rate.build_universe(
                    t_date, self._bday_index, self._all_bdays, UNIVERSE_WINDOW, n_intersect
                )
            except SystemExit:
                self._full_rank_cache[month] = None
                return None
            self._full_rank_cache[month] = {c: i for i, (c, _tv) in enumerate(selected_full, start=1)}
        ranks = self._full_rank_cache[month]
        return ranks.get(code) if ranks else None


# --- KPI別シグナル生成ディスパッチ ------------------------------------------------------------


def generate_kpi_signals(
    entry: dict, start_bd: str, end_bd: str, regime_by_day: dict[str, str],
    bday_index: dict[str, int], all_bdays: list[str],
) -> pd.DataFrame:
    kpi_name = entry["kpi_name"]
    params = entry["params"]

    if kpi_name in VOLSHOCK_FAMILY_KPI_NAMES:
        # 第11周: volshock_x_above200 は kpi_volshock_signals.py 実装済みの filter_ma200
        # (dev200符号フィルタ) をそのまま再利用する (Canonical Module原則・判定ロジックの
        # 再実装はしない)。日次判定でも generate_volshock_signals 内部の SMA200 は
        # 「D自身を含む直近200営業日終値平均」= PIT安全（同関数のdocstring参照）。
        df, _diag = kpi_volshock_signals.generate_volshock_signals(
            start_bd, end_bd,
            vol_multiplier=params["vol_multiplier"],
            day_ret_min=params["day_ret_min"],
            day_ret_max=params["day_ret_max"],
            filter_ma200=params.get("filter_ma200"),
        )
        if df.empty:
            return df[["signal_date", "code"]]
        quiet_min = params.get("quiet_min")
        if quiet_min is not None:
            # 第13周A v2-2: quiet_ratio（kpi_volshock_v2_amplifiers.compute_quiet_ratio）
            # で閾値以上の「静→動」候補のみ残す。kpi_name分岐に依らずparamsにquiet_minが
            # あれば適用する汎用実装（当日候補は数件程度なので銘柄毎の計算コストは軽量）。
            df = df[df.apply(
                lambda row: (
                    (q := kpi_volshock_v2_amplifiers.compute_quiet_ratio(
                        row["code"], row["signal_date"], bday_index, all_bdays,
                    )) is not None and q >= quiet_min
                ),
                axis=1,
            )]
        return df[["signal_date", "code"]] if not df.empty else df

    if kpi_name == "shortcover_x_bear":
        raw_df, _diag = kpi_shortcover_signals.generate_shortcover_signals(
            start_bd, end_bd,
            aggregate_threshold=params["aggregate_threshold"],
            exit_threshold=params["exit_threshold"],
        )
        if raw_df.empty:
            return raw_df[["signal_date", "code"]] if "signal_date" in raw_df.columns else raw_df
        filtered = raw_df[raw_df["signal_date"].map(regime_by_day).eq(entry["regime_filter"])]
        return filtered[["signal_date", "code"]].reset_index(drop=True)

    if kpi_name == "pead_gap8_vol3":
        idx_start = bday_index[start_bd]
        scan_start_idx = max(0, idx_start - PEAD_LOOKBACK_BDAYS)
        scan_start = all_bdays[scan_start_idx]
        # 開示走査の終端は「反応日(=開示日+1)のbarsが存在する範囲」に縮めておく（pead_effective_end）。
        # 出力側のsignal_date上限は縮めない元のend_bd（=既にbars確定済み）のままでよい。
        scan_end = pead_effective_end(end_bd, bday_index, all_bdays)
        df, _diag = kpi_pead_signals.generate_pead_signals(
            scan_start, scan_end,
            gap_threshold=params["gap_threshold"],
            vol_multiplier=params["vol_multiplier"],
            vol_window=params["vol_window"],
        )
        if df.empty:
            return df[["signal_date", "code"]] if "signal_date" in df.columns else df
        filtered = df[(df["signal_date"] >= start_bd) & (df["signal_date"] <= end_bd)]
        return filtered[["signal_date", "code"]].reset_index(drop=True)

    raise SystemExit(f"FATAL: 未知のkpi_name={kpi_name}（config/paper_watchlist.json確認）")


# --- 次点候補「1条件だけ惜しい銘柄」検出（volshock系限定・判定基準は変更しない） -------------------


def find_volshock_near_misses(
    entry: dict, start_bd: str, end_bd: str, bday_index: dict[str, int], all_bdays: list[str],
    universe_cache: UniverseCache,
) -> list[dict]:
    """volshock系KPIの「1条件だけ惜しい」次点候補（カテゴリ2・TOP500内限定）を検出する。

    generate_volshock_signals()自体・compute_quiet_ratio()自体は一切変更しない（判定基準は不変）。
    vol_multiplierを近傍下限(NEAR_MISS_VOL_MULTIPLIER_FLOOR=4.0倍)まで緩め、filter_ma200=None
    （符号を問わずdev200を取得するため）で呼び出すことで「実際の合格条件の厳密なスーパーセット」を
    取得する（これは既存関数が公開済みの引数を正規に使うだけであり、シグナル判定ロジックの
    再実装ではない）。そのスーパーセットの中から、対象3条件（出来高倍率・dev200・quiet_ratio。
    entryのparamsに存在するものだけが判定対象＝volshock_5xはdev200/quiet_ratio判定なし）のうち
    ちょうど1つだけが近傍未達バンドに入り、残り全てが実際の閾値をそのまま満たす行だけを
    daily_screen.py側で独立に分類する（＝完全合格でも2条件以上の未達でもない候補のみ）。
    """
    params = entry["params"]
    has_ma200 = params.get("filter_ma200") == "above"
    has_quiet = params.get("quiet_min") is not None

    df, _diag = kpi_volshock_signals.generate_volshock_signals(
        start_bd, end_bd,
        vol_multiplier=NEAR_MISS_VOL_MULTIPLIER_FLOOR,
        day_ret_min=params["day_ret_min"],
        day_ret_max=params["day_ret_max"],
        filter_ma200=None,
    )
    if df.empty:
        return []

    real_vol_multiplier = params["vol_multiplier"]
    quiet_min = params.get("quiet_min")
    results: list[dict] = []

    for row in df.itertuples(index=False):
        if not universe_cache.in_universe(row.code, row.signal_date):
            continue  # カテゴリ2はTOP500内限定（team-lead仕様）

        va_ratio = row.va / row.va_avg20
        vol_class = (
            "ok" if va_ratio >= real_vol_multiplier
            else "near" if NEAR_MISS_VOL_MULTIPLIER_FLOOR <= va_ratio < real_vol_multiplier
            else "fail"
        )

        if has_ma200:
            if pd.isna(row.dev200):
                continue  # 200日履歴不足で判定不能（既存シグナル生成のinsufficient_ma200_historyと同じ扱い）
            dev200 = float(row.dev200)
            ma200_class = (
                "ok" if dev200 > 0
                else "near" if NEAR_MISS_DEV200_FLOOR <= dev200 < 0
                else "fail"
            )
        else:
            ma200_class = "ok"

        if has_quiet:
            quiet_ratio = kpi_volshock_v2_amplifiers.compute_quiet_ratio(
                row.code, row.signal_date, bday_index, all_bdays,
            )
            if quiet_ratio is None:
                continue
            quiet_class = (
                "ok" if quiet_ratio >= quiet_min
                else "near" if NEAR_MISS_QUIET_FLOOR <= quiet_ratio < quiet_min
                else "fail"
            )
        else:
            quiet_class = "ok"

        classes = [vol_class, ma200_class, quiet_class]
        if classes.count("near") != 1 or "fail" in classes:
            continue  # 「ちょうど1条件だけ惜しい」以外（完全合格・2条件以上未達）は対象外

        if vol_class == "near":
            reason = f"出来高{va_ratio:.2f}倍(5倍未達)"
        elif ma200_class == "near":
            reason = f"dev200={dev200:+.2%}(200日線のわずか下)"
        else:
            reason = f"quiet_ratio={quiet_ratio:.2f}(1.2未達)"

        results.append(
            {
                "kpi_name": entry["kpi_name"],
                "code": row.code,
                "signal_date": row.signal_date,
                "reason": reason,
                "ul10": format_ul10_tag(row.code, row.signal_date, bday_index, all_bdays),
            }
        )
    return results


# --- 次点候補セクションのMarkdown整形（カテゴリ1+2） -----------------------------------------


def format_next_candidates_section(out_of_universe: list[dict], near_miss: list[dict]) -> list[str]:
    """次点候補（判定基準は満たしていないが参考表示する惜しい銘柄）セクションのMarkdown行を生成する。

    表示専用（ledger/state/累計成績には一切影響しない・呼び出し元は本関数の戻り値を
    write_today_report()に渡す、または--dry-run時はそのままprintするだけ）。
    合計最大MAX_NEXT_CANDIDATES_ROWS行に収め、カテゴリ1（ユニバース外の完全通過者）を優先表示する。
    """
    lines = ["## 次点候補（判定基準は満たしていません・参考表示のみ）", "", "### ユニバース外の完全通過者", ""]

    remaining = MAX_NEXT_CANDIDATES_ROWS
    if out_of_universe:
        shown = sorted(out_of_universe, key=lambda r: (r["signal_date"], r["kpi_name"], r["code"]))[:remaining]
        lines.append("| KPI | 銘柄コード | signal_date | 月間売買代金ランク | UL10 |")
        lines.append("|---|---|---|---|---|")
        for r in shown:
            rank_str = str(r["rank"]) if r["rank"] is not None else "-"
            lines.append(f"| {r['kpi_name']} | {r['code']} | {r['signal_date']} | {rank_str} | {r['ul10']} |")
        if len(out_of_universe) > len(shown):
            lines.append(f"（他 {len(out_of_universe) - len(shown)} 件省略）")
        remaining -= len(shown)
    else:
        lines.append("（該当候補なし）")

    lines += ["", "### 1条件だけ惜しい銘柄（TOP500内限定）", ""]
    if not near_miss:
        lines.append("（該当候補なし）")
    elif remaining <= 0:
        lines.append(f"（{len(near_miss)}件該当・上記カテゴリで表示上限に達したため省略）")
    else:
        shown2 = sorted(near_miss, key=lambda r: (r["signal_date"], r["kpi_name"], r["code"]))[:remaining]
        lines.append("| KPI | 銘柄コード | signal_date | 惜しい条件 | UL10 |")
        lines.append("|---|---|---|---|---|")
        for r in shown2:
            lines.append(f"| {r['kpi_name']} | {r['code']} | {r['signal_date']} | {r['reason']} | {r['ul10']} |")
        if len(near_miss) > len(shown2):
            lines.append(f"（他 {len(near_miss) - len(shown2)} 件省略）")

    lines.append("")
    lines.append(UL10_FOOTNOTE)
    return lines


# --- 運用可視化（R1ユニバース失敗警告 + R2稼働コンテキスト/60bdバックフィル） -----------------------
# 2026-07-08 team-lead指示「回収R1+R2」: (1)ユニバース構築失敗を0件シグナルと区別可能にする
# (2)稼働日数・チャンピオン歴史的期待値・シグナル間隔ギャップの常時可視化、で構成する。
# いずれも表示専用でありシグナル判定ロジック・ledger記録内容には一切影響しない。


def format_universe_failure_section(failures: dict[str, str]) -> list[str]:
    """UniverseCache.failed_months()の内容をpaper_today.md冒頭に出す警告セクションを整形する。

    failuresが空なら空リストを返す（=通常時はセクション自体を出さない）。非空の場合のみ
    「シグナル判定が無効」だったことを明示し、通常の「新規シグナルなし」と区別できるようにする
    （R1 §CRITICAL時限爆弾の是正・refresh_master_if_stale()導入後も、未知の原因でユニバース構築が
    失敗した場合の保険として引き続き機能する）。
    """
    if not failures:
        return []
    lines = [
        "## ⚠️ ユニバース構築失敗（シグナル判定は無効）",
        "",
        "以下の月はユニバース（TOP500選抜）構築に失敗しました。該当月の候補は判定不能のため"
        "「ユニバース外」として扱われ、通常の「新規シグナルなし」とは区別が必要です"
        "（0件表示だけでは本当に市場で何も起きなかったのか、判定自体が壊れていたのか分かりません）。",
        "",
    ]
    for month in sorted(failures):
        lines.append(f"- **{month}**: {failures[month]}")
    lines.append("")
    return lines


def format_operational_context_lines(
    first_screened_date: str, end_bd: str, bday_index: dict[str, int],
    champion_avg_monthly_signals: float, records: list[dict], recent_signals: list[dict],
) -> list[str]:
    """稼働日数・チャンピオンの歴史的期待発生数・直近シグナルからの経過を常時表示する見出し行を作る
    （R2「レコメンドゼロ」が探索停止によるものか運用未熟によるものか常時判別できるようにする）。

    経過日数は ledger（本番記録）と recent_signals（60bdバックフィル・R2 §2）の両方から
    CHAMPION_KPI_NAME のsignal_dateを集め最新のものを採用する（ledgerは稼働開始日以降しか
    持たないため、バックフィルが「その前から実は何件出ていたか」を補う設計・team-lead方針）。
    """
    n_days = bday_index[end_bd] - bday_index[first_screened_date] + 1
    expected_champion = n_days / BDAYS_PER_MONTH_APPROX * champion_avg_monthly_signals

    candidates = [r["signal_date"] for r in records if r["kpi_name"] == CHAMPION_KPI_NAME]
    candidates += [r["signal_date"] for r in recent_signals if r["kpi_name"] == CHAMPION_KPI_NAME]
    if candidates:
        last_date = max(candidates)
        gap = bday_index[end_bd] - bday_index[last_date]
        gap_str = f"{gap}営業日"
        if gap > GAP_WARN_ANOMALY_BDAYS:
            warn = "異常"
        elif gap > GAP_WARN_INVESTIGATE_BDAYS:
            warn = "要調査"
        elif gap > GAP_WARN_LIGHT_BDAYS:
            warn = "軽度注意"
        else:
            warn = "正常範囲"
    else:
        gap_str = f"不明（直近{RECENT_SIGNALS_LOOKBACK_BDAYS}営業日のバックフィル内に該当なし・ledgerにも記録なし）"
        warn = "要調査"

    return [
        "## 稼働状況",
        "",
        f"稼働 {n_days}営業日目 / この期間のチャンピオン（{CHAMPION_KPI_NAME}）歴史的期待発生数 "
        f"≈{expected_champion:.1f}件（月{champion_avg_monthly_signals:.1f}件ベース） / "
        f"直近シグナルからの経過 = {gap_str}"
        f"（歴史分布: 中央値{CHAMPION_GAP_MEDIAN_BDAYS}営業日・最大{CHAMPION_GAP_MAX_BDAYS}営業日） "
        f"/ 警戒レベル: {warn}",
        "",
    ]


def load_recent_signals_cache() -> dict:
    if not RECENT_SIGNALS_CACHE_PATH.exists():
        return {"computed_through": None, "hits": []}
    return json.loads(RECENT_SIGNALS_CACHE_PATH.read_text(encoding="utf-8"))


def save_recent_signals_cache(cache: dict) -> None:
    RECENT_SIGNALS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = RECENT_SIGNALS_CACHE_PATH.with_name(RECENT_SIGNALS_CACHE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(RECENT_SIGNALS_CACHE_PATH)


def update_recent_signals_cache(
    end_bd: str, watchlist_by_name: dict[str, dict], bday_index: dict[str, int], all_bdays: list[str],
    regime_by_day: dict[str, str], universe_cache: UniverseCache,
) -> list[dict]:
    """直近RECENT_SIGNALS_LOOKBACK_BDAYS営業日のチャンピオン+比較対照KPIヒットをキャッシュする
    （R2 §2「バックフィル参考・ledger非記録」）。

    generate_kpi_signals()・UniverseCache.in_universe()をそのまま再利用し判定ロジックは一切
    再実装しない（Canonical Module原則）。初回はフルスキャン、以降はcomputed_through翌営業日〜
    end_bdだけを追加スキャンし、ローリング窓（60営業日）外に出た古いヒットを切り捨てて日次
    インクリメンタルに更新する（毎日60日分を再計算する重さを避ける設計・team-lead推奨方針）。
    """
    end_idx = bday_index[end_bd]
    window_start = all_bdays[max(0, end_idx - RECENT_SIGNALS_LOOKBACK_BDAYS + 1)]

    cache = load_recent_signals_cache()
    computed_through = cache.get("computed_through")
    hits: list[dict] = cache.get("hits", [])

    if computed_through is None:
        scan_start: Optional[str] = window_start
    elif computed_through >= end_bd:
        scan_start = None  # 既に最新まで計算済み（再走査不要・ローリング窓のトリムのみ行う）
    else:
        idx = bday_index.get(computed_through)
        scan_start = all_bdays[idx + 1] if idx is not None else window_start

    if scan_start is not None:
        for kpi_name in RECENT_SIGNALS_KPI_NAMES:
            entry = watchlist_by_name.get(kpi_name)
            if entry is None:
                continue
            df = generate_kpi_signals(entry, scan_start, end_bd, regime_by_day, bday_index, all_bdays)
            for row in df.itertuples(index=False):
                if not universe_cache.in_universe(row.code, row.signal_date):
                    continue
                hits.append({"kpi_name": kpi_name, "code": row.code, "signal_date": row.signal_date})

    hits = [h for h in hits if h["signal_date"] >= window_start]
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict] = []
    for h in hits:
        key = (h["kpi_name"], h["code"], h["signal_date"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(h)

    save_recent_signals_cache({"computed_through": end_bd, "hits": deduped})
    return deduped


def format_recent_signals_section(recent_signals: list[dict]) -> list[str]:
    lines = [
        f"## 直近{RECENT_SIGNALS_LOOKBACK_BDAYS}営業日のシグナル履歴（バックフィル参考・ledger非記録）",
        "",
        f"対象KPI: {', '.join(RECENT_SIGNALS_KPI_NAMES)}（チャンピオン+比較対照。TOP500ユニバース内のみ）",
        "",
    ]
    if not recent_signals:
        lines += ["（該当なし）", ""]
        return lines
    lines.append("| KPI | 銘柄コード | signal_date |")
    lines.append("|---|---|---|")
    for r in sorted(recent_signals, key=lambda r: (r["signal_date"], r["kpi_name"], r["code"])):
        lines.append(f"| {r['kpi_name']} | {r['code']} | {r['signal_date']} |")
    lines.append("")
    return lines


# --- ledgerへの新規シグナル追記 --------------------------------------------------------------


def append_new_signals(
    records: list[dict], entry: dict, signals_df: pd.DataFrame, bday_index: dict[str, int],
    all_bdays: list[str], run_id: str,
) -> int:
    kpi_name = entry["kpi_name"]
    existing_keys = paper_eval.ledger_keys(records)
    added = 0
    for row in signals_df.itertuples(index=False):
        key = (kpi_name, row.code, row.signal_date)
        if key in existing_keys:
            continue
        idx = bday_index[row.signal_date]
        if idx + 1 >= len(all_bdays):
            continue  # カレンダー終端（実運用では起こらない想定）
        planned_entry_date = all_bdays[idx + 1]
        records.append(
            {
                "run_id": run_id,
                "kpi_name": kpi_name,
                "code": row.code,
                "signal_date": row.signal_date,
                "detected_at": jq_fetch.now_jst().isoformat(),
                "status": "pending_entry",
                "planned_entry_date": planned_entry_date,
                "defer_entry": entry["defer_entry"],
                "max_defer_bdays": entry["max_defer_bdays"] if entry["defer_entry"] else 0,
                "entry_date": None,
                "entry_price": None,
                "defer_bdays_used": None,
                "exit_date": None,
                "exit_price": None,
                "exit_reason": None,
                "ret_gross": None,
                "ret_net": None,
                "ret_stop8": None,  # 第12周: ret_net(既存stop8/20bd満期)のエイリアス。closed確定時にscripts/paper_eval.pyが設定
                "exit_date_nostop": None,
                "exit_price_nostop": None,
                "exit_reason_nostop": None,
                "ret_nostop": None,  # 第12周並走記録: 損切りなし・20bd満期（カタログ§7-A C3相当）
                "exit_date_e1": None,
                "exit_price_e1": None,
                "exit_reason_e1": None,
                "ret_e1": None,  # 第12周並走記録: シナリオ崩壊(200日線割れ)exit（カタログ§7-A E1）
                "mfe": None,
                "mae": None,
                "updated_at": jq_fetch.now_jst().isoformat(),
            }
        )
        existing_keys.add(key)
        added += 1
    return added


# --- レポート生成 --------------------------------------------------------------------------


def write_today_report(
    new_signal_records: list[dict], records: list[dict], scoreboard: dict[str, dict],
    scan_start: Optional[str], scan_end: Optional[str], out_of_universe_counts: dict[str, int],
    next_candidates_lines: list[str], universe_failure_lines: list[str],
    operational_context_lines: list[str], recent_signals_lines: list[str],
    bday_index: dict[str, int], all_bdays: list[str],
) -> None:
    lines = [
        "# ペーパートレード 当日スクリーニングレポート（scripts/daily_screen.py 自動生成）",
        "",
        *universe_failure_lines,
        *operational_context_lines,
        f"実行時刻: {jq_fetch.now_jst().isoformat()}",
        f"走査区間: {scan_start or '-'} 〜 {scan_end or '-'}（前営業日終値までのデータで判定・想定エントリーはT+1寄付）",
        "",
        "## 新規シグナル",
        "",
    ]
    if new_signal_records:
        # UL10列: 全KPI行で表示する。根拠の第17周T1(ul_fade_standalone)はチャンピオン限定
        # ではなくTOP500全体の実測（catalog §7-F）のため、証拠の適用範囲と表示範囲を一致
        # させる（"-"を安全と誤読させない・Codexレビュー④反映）。
        lines.append("| KPI | 銘柄コード | signal_date | 想定エントリー日 | UL10 |")
        lines.append("|---|---|---|---|---|")
        for r in new_signal_records:
            ul10_str = format_ul10_tag(r["code"], r["signal_date"], bday_index, all_bdays)
            lines.append(
                f"| {r['kpi_name']} | {r['code']} | {r['signal_date']} | {r['planned_entry_date']} | {ul10_str} |"
            )
        lines.append("")
        lines.append(UL10_FOOTNOTE)
    else:
        lines.append("（本走査区間で新規シグナルなし）")
    if out_of_universe_counts:
        lines.append("")
        lines.append(
            "TOP500ユニバース外のため除外した候補件数: "
            + " / ".join(f"{k}={v}" for k, v in sorted(out_of_universe_counts.items()))
        )

    lines += ["", *recent_signals_lines]
    lines += ["", *next_candidates_lines]

    lines += ["", "## 保有中ポジション（pending_entry / open）", ""]
    active = [r for r in records if r["status"] in ("pending_entry", "open")]
    if active:
        lines.append("| KPI | 銘柄コード | signal_date | status | entry_date | entry_price |")
        lines.append("|---|---|---|---|---|---|")
        for r in sorted(active, key=lambda r: (r["kpi_name"], r["signal_date"])):
            lines.append(
                f"| {r['kpi_name']} | {r['code']} | {r['signal_date']} | {r['status']} | "
                f"{r['entry_date'] or '-'} | {r['entry_price'] or '-'} |"
            )
    else:
        lines.append("（保有中ポジションなし）")

    lines += ["", "## 累計成績（確定済みのみ）", ""]
    lines.append("| KPI | 稼働中(pending/open) | entry_missing | 確定n | P(+20%) | EV(net) |")
    lines.append("|---|---|---|---|---|---|")
    for kpi in sorted(scoreboard):
        s = scoreboard[kpi]
        p20 = f"{s['p_reach20']:.1%}" if s["p_reach20"] is not None else "-"
        ev = f"{s['ev_net']:+.2%}" if s["ev_net"] is not None else "-"
        lines.append(
            f"| {kpi} | {s['n_pending_entry']}/{s['n_open']} | {s['n_entry_missing']} | "
            f"{s['n_closed']} | {p20} | {ev} |"
        )
    lines.append("")
    lines.append("詳細は `output/paper_scoreboard.md`（scripts/paper_eval.py）を参照。")

    TODAY_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    TODAY_REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- メイン処理 -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="ペーパートレード観察リスト 日次スクリーニング")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="データ取得・ledger書込みを行わず、今あるキャッシュのみでシグナル件数を表示する",
    )
    args = parser.parse_args()

    watchlist = [e for e in load_watchlist() if e["status"] in ("observation", "reference")]
    calendar_days = measure_base_rate.load_calendar_days()
    all_bdays = measure_base_rate.all_business_days(calendar_days)
    bday_index = {d: i for i, d in enumerate(all_bdays)}

    if args.dry_run:
        end_bd = latest_available_bday_overall(all_bdays)
        if end_bd is None:
            print("FATAL: bars/shortsaleの有効キャッシュが1件も見つかりません", file=sys.stderr)
            return 1
        start_bd = end_bd
        print(f"[dry-run] 今あるキャッシュの最新営業日 {end_bd} のみで判定します（データ取得は行いません）")
    else:
        state = load_state()
        start_bd, end_bd = compute_scan_range(state, all_bdays)
        if start_bd is None:
            print("走査すべき新規営業日がありません（既に最新まで処理済み）")
            sync_vault_mirror()
            return 0
        refresh_recent_data(start_bd, end_bd, all_bdays)
        refresh_topix_if_stale(end_bd)
        refresh_master_if_stale(end_bd, calendar_days)
        available_end = latest_fully_available_bday([d for d in all_bdays if start_bd <= d <= end_bd])
        if available_end is None:
            print(f"WARN: {start_bd}〜{end_bd} のデータがまだ公表されていません。次回実行で再試行します", file=sys.stderr)
            sync_vault_mirror()
            return 0
        end_bd = available_end
        print(f"走査区間: {start_bd} 〜 {end_bd}")
        # 稼働開始日（R2「稼働N営業日目」計測の起点）。新設フィールドのため既存state.jsonとの
        # 後方互換フォールバックを3段で持つ: 明示保存済み > 既存last_screened_date(=既に稼働実績あり
        # の証跡) > 今回のstart_bd(=真の初回実行)。
        first_screened_date = state.get("first_screened_date") or state.get("last_screened_date") or start_bd

    topix_close = measure_base_rate.load_topix_series()
    regime_by_day = measure_base_rate.build_regime_series(topix_close)
    universe_cache = UniverseCache(calendar_days, bday_index, all_bdays)

    run_id = uuid.uuid4().hex
    records = paper_eval.read_ledger()
    new_signal_records: list[dict] = []
    out_of_universe_counts: dict[str, int] = {}
    next_candidates_out_of_universe: list[dict] = []  # 次点候補カテゴリ1（表示専用・ledger非経由）
    next_candidates_near_miss: list[dict] = []  # 次点候補カテゴリ2（表示専用・ledger非経由）

    for entry in watchlist:
        signals_df = generate_kpi_signals(entry, start_bd, end_bd, regime_by_day, bday_index, all_bdays)
        n_raw = len(signals_df)
        if not signals_df.empty:
            in_univ_mask = signals_df.apply(
                lambda r: universe_cache.in_universe(r["code"], r["signal_date"]), axis=1
            )
            out_of_universe_rows = signals_df[~in_univ_mask]
            out_count = len(out_of_universe_rows)
            if out_count:
                out_of_universe_counts[entry["kpi_name"]] = out_count
                for row in out_of_universe_rows.itertuples(index=False):
                    next_candidates_out_of_universe.append(
                        {
                            "kpi_name": entry["kpi_name"],
                            "code": row.code,
                            "signal_date": row.signal_date,
                            "rank": universe_cache.rank_for(row.code, row.signal_date),
                            "ul10": format_ul10_tag(row.code, row.signal_date, bday_index, all_bdays),
                        }
                    )
            signals_df = signals_df[in_univ_mask].reset_index(drop=True)
        print(f"[{entry['kpi_name']}] 候補{n_raw}件 -> TOP500内{len(signals_df)}件")

        if entry["kpi_name"] in VOLSHOCK_FAMILY_KPI_NAMES:
            next_candidates_near_miss.extend(
                find_volshock_near_misses(entry, start_bd, end_bd, bday_index, all_bdays, universe_cache)
            )

        if args.dry_run:
            continue

        before = len(records)
        added = append_new_signals(records, entry, signals_df, bday_index, all_bdays, run_id)
        new_signal_records.extend(records[before : before + added])

    next_candidates_lines = format_next_candidates_section(next_candidates_out_of_universe, next_candidates_near_miss)
    universe_failures = universe_cache.failed_months()
    universe_failure_lines = format_universe_failure_section(universe_failures)

    if args.dry_run:
        if universe_failure_lines:
            print("\n".join(universe_failure_lines))
        print("\n".join(next_candidates_lines))
        print("[dry-run] ledger書込み・レポート生成は行いません")
        return 0

    records, fill_diag, open_diag, parallel_diag = paper_eval.run_update(records)
    paper_eval.write_ledger_atomic(records)
    print(
        f"新規シグナル追加: {len(new_signal_records)}件 / entry確定: {fill_diag['filled']}件 / "
        f"ポジションクローズ: {open_diag['closed_stop_loss'] + open_diag['closed_time_exit'] + open_diag['closed_delisted']}件 / "
        f"並走exit解決(nostop/e1): {parallel_diag['resolved_nostop']}/{parallel_diag['resolved_e1']}件"
    )

    scoreboard = paper_eval.compute_scoreboard(records)
    paper_eval.write_scoreboard_md(scoreboard, paper_eval.SCOREBOARD_PATH)

    watchlist_by_name = {e["kpi_name"]: e for e in watchlist}
    recent_signals = update_recent_signals_cache(
        end_bd, watchlist_by_name, bday_index, all_bdays, regime_by_day, universe_cache,
    )
    recent_signals_lines = format_recent_signals_section(recent_signals)
    champion_avg_monthly_signals = watchlist_by_name[CHAMPION_KPI_NAME]["in_sample"]["avg_monthly_signals"]
    operational_context_lines = format_operational_context_lines(
        first_screened_date, end_bd, bday_index, champion_avg_monthly_signals, records, recent_signals,
    )
    # 監査O-5/O-6/O-7: Cookie鮮度・margin鮮度・jsfアーカイブ欠損検知を同じ稼働状況セクションに追記
    # （3項目とも表示専用・シグナル判定ロジックには一切影響しない）
    operational_context_lines += format_cookie_age_lines()
    operational_context_lines += format_margin_freshness_lines(jq_fetch.DATA_ROOT / "margin")
    operational_context_lines += format_jsf_gap_check_lines()
    operational_context_lines.append("")

    write_today_report(
        new_signal_records, records, scoreboard, start_bd, end_bd, out_of_universe_counts,
        next_candidates_lines, universe_failure_lines, operational_context_lines, recent_signals_lines,
        bday_index, all_bdays,
    )
    print(f"レポート出力: {TODAY_REPORT_PATH}")

    save_state({
        "last_screened_date": end_bd,
        "last_run_at": jq_fetch.now_jst().isoformat(),
        "last_run_id": run_id,
        "first_screened_date": first_screened_date,
    })
    # vault転写はstate保存(=ジョブ完了境界)の後に置く: vault側I/O詰まりや強制終了で
    # last_screened_dateが失われ翌日に二重走査になるのを防ぐ(Codexレビュー⑤反映)
    sync_vault_mirror()
    return 0


if __name__ == "__main__":
    sys.exit(main())
