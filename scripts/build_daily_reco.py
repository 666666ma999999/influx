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
