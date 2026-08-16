#!/usr/bin/env python3
"""本日の株レコメンド（配信ループ・表示層専用）を生成する。

2026-07-31 敵対的クロスレビュー（Fable別文脈+Codex 両者一致）の処方:
「証拠ループ（凍結・FDR）」と「配信ループ（表示層）」の二層化。本スクリプトは配信側で、
- 入力はすべて凍結済み/生成済みの正本（ledger / recipe_shelf_meta / watchlist）の引用のみ
- 新規の統計推定を行わない（α非消費・trials.jsonl / screening_batches.jsonl に不算入）
- §0付記II 一方向ルール準拠: 金額・枚数・倍率・配分は一切提案しない
- 台帳・state への書込みなし（output/ と vault ミラーのみ）
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import jq_fetch  # noqa: E402  (stdlib-only・DATA_ROOT と read_json_gz を再利用)
from kpi_clock_sla import KPI_SHORT_JA  # noqa: E402  (表示名の正本を再利用)

LEDGER_PATH = REPO / "data/paper_trades/ledger.jsonl"
META_PATH = REPO / "config/recipe_shelf_meta.json"
WATCHLIST_PATH = REPO / "config/paper_watchlist.json"
OUTPUT_PATH = REPO / "output/daily_reco.md"
SUMMARY_PATH = REPO / "output/daily_reco_summary.json"
VAULT_PATH = Path(
    "/Users/masaaki_nagasawa/Documents/Obsidian Vault/02_Ai/influx/influx-daily-reco.md"
)

# 証拠段（凍結分類 recipe_shelf_meta.json kpi_classification.tier の引用）
TIER_RECO = "promising"   # 過去関門を全通過（前向き検証中）→ 有力候補欄
TIER_WATCH = "observing"  # 本番試験走行 → 観察欄
# control / dead / (watchlist status: reference / hoos_rejected) → 使用禁止・参照欄

RECENT_BDAYS = 5      # 有力候補として掲示する鮮度窓（営業日・表示専用の既定値）
NOSTOP_BDAYS = 20     # nostop 満期（catalog 凍結値の引用）

# 営業日概算用の祝日（表示専用の概算・判定には不使用）
HOLIDAYS_APPROX = {"2026-08-11", "2026-09-21", "2026-09-22", "2026-10-12"}


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def code_names() -> dict[str, str]:
    """銘柄コード→社名（daily_screen.code_name と同じ月次masterを読む・表示専用）。"""
    names: dict[str, str] = {}
    try:
        files = sorted((jq_fetch.DATA_ROOT / "master").glob("*.json.gz"))
        if files:
            obj = jq_fetch.read_json_gz(files[-1])
            rows = obj if isinstance(obj, list) else (obj.get("info") or obj.get("data") or [])
            for row in rows:
                if row.get("Code"):
                    names[str(row["Code"])] = row.get("CoName") or ""
    except Exception as exc:  # noqa: BLE001  (表示専用・社名なしで続行)
        print(f"WARN: 社名マスタ読込失敗（コードのみ表示で続行）: {exc}", file=sys.stderr)
    return names


def add_bdays(date: dt.date, n: int) -> dt.date:
    """土日+概算祝日スキップの営業日加算（表示専用の概算）。"""
    current = date
    remaining = n
    while remaining > 0:
        current += dt.timedelta(days=1)
        if current.weekday() < 5 and current.isoformat() not in HOLIDAYS_APPROX:
            remaining -= 1
    return current


def recent_bday_floor(today: dt.date, n: int) -> dt.date:
    current = today
    remaining = n
    while remaining > 0:
        current -= dt.timedelta(days=1)
        if current.weekday() < 5 and current.isoformat() not in HOLIDAYS_APPROX:
            remaining -= 1
    return current


def parse_ymd(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y%m%d").date()


def label(kpi: str) -> str:
    return KPI_SHORT_JA.get(kpi, kpi)


def build() -> tuple[str, dict]:
    meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    tiers = {kpi: item["tier"] for kpi, item in meta["kpi_classification"].items()}
    watchlist = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))["watchlist"]
    wl_by_name = {e["kpi_name"]: e for e in watchlist}
    ledger = load_jsonl(LEDGER_PATH)
    names = code_names()
    today = dt.date.today()
    recent_floor = recent_bday_floor(today, RECENT_BDAYS)

    def banned_reason(kpi: str) -> str | None:
        status = wl_by_name.get(kpi, {}).get("status")
        if status == "reference":
            return "holdout棄却済み（参照のみ）"
        if status == "hoos_rejected":
            return "holdout棄却（hoos_rejected）"
        tier = tiers.get(kpi)
        if tier == "dead":
            return "負け確定（dead）"
        if tier == "control":
            return "対照・参照系統（判断に使わない）"
        return None

    def frozen_ev(kpi: str) -> float | None:
        ins = wl_by_name.get(kpi, {}).get("in_sample", {})
        return ins.get("ev_none")

    # --- 有力候補: promising 系統の発火のうち signal_date が直近 RECENT_BDAYS 営業日内 ---
    reco_rows = [
        r for r in ledger
        if tiers.get(r["kpi_name"]) == TIER_RECO
        and r["status"] in ("pending_entry", "open")
        and parse_ymd(r["signal_date"]) >= recent_floor
    ]
    by_code: dict[str, list[dict]] = defaultdict(list)
    for r in reco_rows:
        by_code[r["code"]].append(r)

    def sort_key(code: str):
        rows = by_code[code]
        evs = [frozen_ev(r["kpi_name"]) for r in rows]
        return (-len({r["kpi_name"] for r in rows}), -max((e for e in evs if e is not None), default=0.0))

    ranked_codes = sorted(by_code, key=sort_key)

    # --- 本日の新規（pending_entry 全量）を証拠段で区分 ---
    pending = [r for r in ledger if r["status"] == "pending_entry"]
    watch_pending = [r for r in pending if tiers.get(r["kpi_name"]) == TIER_WATCH]
    banned_pending = [r for r in pending if banned_reason(r["kpi_name"])]

    # --- 次に何が起きるか ---
    open_entries = [r["entry_date"] for r in ledger if r["status"] == "open" and r.get("entry_date")]
    first_read = add_bdays(parse_ymd(min(open_entries)), NOSTOP_BDAYS).isoformat() if open_entries else "未定"

    lines: list[str] = []
    lines.append("# 本日の株レコメンド（配信ループ・表示専用）")
    lines.append("")
    lines.append("> 毎朝自動更新・手書き禁止。**正式合格0本の間、ここに載る候補はすべて「ペーパー提案」**（前向き検証中・実弾の推奨ではない）。")
    lines.append("> 金額・枚数・倍率・配分は提案しない（catalog §0付記II 一方向ルール）。本ファイルは判定・台帳・αに一切影響しない表示層。")
    lines.append("")
    lines.append("## ✅ 実戦投入可（正式合格）: 0本")
    lines.append("")
    lines.append(f"## 🌟 有力候補（過去関門を全通過した系統の発火・直近{RECENT_BDAYS}営業日）: {len(ranked_codes)}銘柄")
    lines.append("")
    if ranked_codes:
        lines.append("| 順位 | 銘柄 | 社名 | 有力系統 | 系統数 | 参考EV(凍結v2・nostop・系統別) | signal_date | 状態 |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for rank, code in enumerate(ranked_codes, 1):
            rows = sorted(by_code[code], key=lambda r: r["signal_date"], reverse=True)
            kpis = sorted({r["kpi_name"] for r in rows})
            # EV estimand v2（§6付記IV凍結・月等ウェイト）の点推定と片側95%下限を系統別に全列挙。
            # v2未算出の系統は「v2未算出」と明示（旧v1値で埋めない＝欠測の偽装禁止・A2レビュー指摘）
            ev_parts = []
            for k in kpis:
                ev2 = wl_by_name.get(k, {}).get("in_sample", {}).get("estimand_v2") or {}
                if ev2.get("status") == "computed":
                    ev_parts.append(
                        f"{ev2['ev_none_v2'] * 100:+.2f}%[下限{ev2['ev_none_ci1s_low'] * 100:+.2f}%]"
                        f"(n={ev2['n_used_none']})"
                    )
                else:
                    ev_parts.append("v2未算出")
            ev_text = " / ".join(ev_parts) if ev_parts else "—"
            status = "エントリー前" if any(r["status"] == "pending_entry" for r in rows) else "保有中(ペーパー)"
            sig = max(r["signal_date"] for r in rows)
            lines.append(
                f"| {rank} | {code} | {names.get(code, '')} | {'・'.join(label(k) for k in kpis)} | "
                f"{len(kpis)} | {ev_text} | {sig} | {status} |"
            )
        lines.append("")
        lines.append(
            "掲載式: 並べ替え = ①有力(promising)系統数の多い順 → ②同数なら凍結in-sample EV(なし)最大値の高い順。"
            "EVは新規推定ではなく config/paper_watchlist.json 凍結値の引用（前向き未確定・参考値）。"
        )
        lines.append("")
        lines.append(
            "⚠️ EVは estimand v2（月等ウェイト・§6付記IV凍結・catalog正本）。[下限]=片側95%下限（絶対EV・市場超過ではない）。"
            "初回算出時点で下限>0は16系統中 earnings_spillover の1本のみ＝in-sampleの証拠はまだ薄いという正直な表示。"
            "順位は従来どおり①系統数②v1凍結EV（v2未算出系統の欠測バイアスを避けるため順位規則は不変・表示のみv2）。"
            "同一銘柄の複数系統は同一の決算開示に由来しうるため、系統数は独立な証拠の数ではない。"
        )
    else:
        lines.append(f"（直近{RECENT_BDAYS}営業日に有力系統の発火なし）")
    lines.append("")
    lines.append(f"## 🟡 観察のみ・明朝エントリー分（判断に使わない・成績計測中）: {len(watch_pending)}件")
    lines.append("")
    if watch_pending:
        lines.append("| 銘柄 | 社名 | 系統 | signal_date |")
        lines.append("|---|---|---|---|")
        for r in sorted(watch_pending, key=lambda r: (r["code"], r["kpi_name"])):
            lines.append(f"| {r['code']} | {names.get(r['code'], '')} | {label(r['kpi_name'])} | {r['signal_date']} |")
    else:
        lines.append("（なし）")
    lines.append("")
    lines.append(f"## 🚫 使用禁止・参照系統の発火（対照/棄却済み）: {len(banned_pending)}件")
    lines.append("")
    if banned_pending:
        lines.append("| 銘柄 | 社名 | 系統 | 理由 |")
        lines.append("|---|---|---|---|")
        for r in sorted(banned_pending, key=lambda r: (r["code"], r["kpi_name"])):
            lines.append(f"| {r['code']} | {names.get(r['code'], '')} | {label(r['kpi_name'])} | {banned_reason(r['kpi_name'])} |")
    else:
        lines.append("（なし）")
    lines.append("")
    # --- 仕込み型（価格系列レーン）の合流 ---
    # 急騰狙い（上のセクション）とは別ゴール: 商品価格の上昇 → 恩恵が決算に出るまで8〜15週。
    # 計算は build_shikomi_list.build_rows() を再利用（同じ計算を2箇所に書かない）。
    sk_error = None
    try:
        from build_shikomi_list import LOOKBACK_DAYS as _SK_LOOKBACK
        from build_shikomi_list import build_rows as _shikomi_rows
        sk_rows, sk_skipped, sk_latest = _shikomi_rows()
    except Exception as exc:  # noqa: BLE001  表示専用・本体は落とさないが**黙らせない**
        sk_rows, sk_skipped, sk_latest = [], [], None
        sk_error = str(exc)[:120]
    if sk_error:
        # 失敗を「0銘柄」と誤読させない（Codex指摘: 障害が本文を開くまで分からない状態を作らない）。
        # summary 経由で朝の通知にも ⚠️ を伝搬する。
        lines.append(f"## 🌱 仕込み型（価格系列レーン・別ゴール）: ⚠️ **生成失敗**（{sk_error}）")
        lines.append("")
        lines.append("> この節が失敗している間、仕込み型の候補は**0件ではなく不明**です。復旧まで判断に使わないこと。")
        lines.append("")
    else:
        # 0件でも節は必ず出す（統合済みなのか生成漏れなのかを区別できるようにする・Codex指摘3）
        lines.append(f"## 🌱 仕込み型（価格系列レーン・別ゴール）: {len(sk_rows)}銘柄")
        lines.append("")
        lines.append(
            "> 上の「有力候補」が**1ヶ月で+20%の急騰**を狙うのに対し、こちらは**商品価格の上昇 → 恩恵が"
            "決算に出るまで8〜15週**の型。**このレーンも成績は未確定**（初回評価 2026-09〜11）＝実弾の推奨ではない。"
        )
        lines.append("")
        if not sk_rows:
            lines.append(f"（直近{_SK_LOOKBACK}日に価格系列レーンの発火なし。次の週次チェックは月曜08:30）")
            lines.append("")
        lines.append("| 証拠 | 銘柄 | 社名 | 発火した価格 | 発火日 | 騰落 | TOPIX | **超過** |")
        lines.append("|---|---|---|---|---|---:|---:|---:|")
        for fd, sjp, code4, name, tier, chg, mkt, exc, _entry, _ev, stale in sk_rows:
            mark = ("◎確証" if tier == "confirmed" else "△仮") + ("(要再確認)" if stale else "")
            fmt_ = lambda v: f"{v:+.2f}%" if v is not None else "—"
            lines.append(
                f"| {mark} | {code4} | {name} | {sjp} | {fd[4:6]}/{fd[6:]} | "
                f"{fmt_(chg)} | {fmt_(mkt)} | **{fmt_(exc)}** |"
            )
        lines.append("")
        lines.append(
            "掲載式: ①決算実読の証拠が強い順（確証→仮）→ ②対TOPIX超過が小さい順（＝まだ市場が反応していない順）。"
            "仕込み起点＝発火の翌営業日終値→各銘柄の最新終値。**超過が小さい＝仕込み余地が残っている可能性**が"
            "ある一方、**効かないシグナルだった可能性もある**（未検証）。決着は9〜11月の前向き評価。"
        )
        if sk_skipped:
            lines.append("")
            lines.append(
                "掲載しなかった発火銘柄（決算実読で却下 or カード未登録）: "
                + " ／ ".join(f"{c} {n}（{s}）" for c, n, s in sk_skipped)
                + " ＝これらは発火通知には出るが候補には出さない（帰属プロトコル §0b）"
            )
        lines.append("")

    lines.append("## 次に何が起きるか")
    lines.append("")
    lines.append(
        f"- 前向き成績の初回読み取り目安: **{first_read}**"
        f"（最古エントリー {min(open_entries) if open_entries else '—'} の nostop {NOSTOP_BDAYS}営業日満期・土日祝スキップの概算）"
    )
    lines.append("- 正式合格の初回判定: 最短 2027-08（output/recipe_shelf.md 参照）")
    lines.append("")
    lines.append(f"生成時刻: {dt.datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append(
        "データ源: data/paper_trades/ledger.jsonl / config/recipe_shelf_meta.json / config/paper_watchlist.json"
        " ／ 仕込み型= data/price_watch/forward_log.jsonl + configs/price_universe_sources.json + data/jquants"
    )
    lines.append("")

    summary = {
        "reco_count": len(ranked_codes),
        "top": [
            {"code": code, "name": names.get(code, ""), "n_kpis": len({r["kpi_name"] for r in by_code[code]})}
            for code in ranked_codes[:3]
        ],
        "watch_pending": len(watch_pending),
        "banned_pending": len(banned_pending),
        "first_read_date": first_read,
        "shikomi_count": len(sk_rows),
        "shikomi_error": sk_error,
    }
    return "\n".join(lines), summary


def main() -> None:
    content, summary = build()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(content, encoding="utf-8")
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        VAULT_PATH.write_text(content, encoding="utf-8")
        print(f"mirrored {VAULT_PATH}")
    except OSError as exc:
        print(f"WARN: vaultミラー書込失敗（repo正本は生成済み）: {exc}", file=sys.stderr)
    print(f"generated {OUTPUT_PATH.relative_to(REPO)} reco={summary['reco_count']}銘柄")


if __name__ == "__main__":
    main()
