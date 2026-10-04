#!/usr/bin/env python3
"""開示表題の供給側レーン（P-INF-29 案 1・オーナー裁定 A 2026-10-04）。

目的: 毎朝 07:15 に保存される TDnet 開示インデックス（`data/tdnet/index/<YYYY>/<a>_<b>.json.gz`）の
**表題だけ**から、企業が自分で言う「供給側の品薄」＝増産・生産能力増強・新工場・大口受注・価格改定の
開示を銘柄コード付きで台帳 `data/tdnet/supply_lane.jsonl` に記録する。判断には使わない（記録だけ・
前向き採点は docs/price-watch-universe.md §16o-3 追記10 の事前登録どおり事象 30 件で 1 回）。

数え方（母集団）:
- index は週次チャンクと日次チャンクが重なり同じ開示が最大 3 回出る（2026-07 以降 167 ファイル
  103,930 行・実測）＝ **Tdnet.id で重複除去してから** pubdate で絞る。
- 語グループは RULE_VERSION で凍結。品薄・供給不足・受注残・フル稼働・逼迫は表題では 0 件（本文にしか
  出ない）ので今回は表題だけ。
- 「受注速報」の月次定期は order に入るが periodic=true を立てる（表には出す・採点母集団から外す）。

stdlib のみ（pandas 不要）＝ daily_screen がホストの /usr/bin/python3 から呼ぶ。

使い方:
  python3 scripts/tdnet_supply_lane.py --days 3            # 既定（今日から 3 日前までの pubdate）
  python3 scripts/tdnet_supply_lane.py --since 2026-09-01 --until 2026-09-30
  python3 scripts/tdnet_supply_lane.py --days 30 --dry-run  # 書かずに件数だけ
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import unicodedata
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = Path("/app") if Path("/app/scripts").exists() else SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from tdnet_index_lib import TDNET_INDEX_DIR, iter_tdnet_records, normalize_code  # noqa: E402  (index 読込・id 重複除去・5桁→4桁は発見器②と共有)
from x_shortage_map import load_center_pin  # noqa: E402  (TOP1000 台帳 code→行 の読み方を 1 箇所化・再実装しない)

RULE_VERSION = "v0-title"
INDEX_DIR = Path(TDNET_INDEX_DIR)
LEDGER_PATH = ROOT / "data/tdnet/supply_lane.jsonl"

# 語グループ（凍結・評価順を固定: 先に当たったグループを採る）。NFKC 正規化後の表題に当てる。
GROUP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("capacity", re.compile(r"生産能力増強|増産|新工場|生産体制の?強化|能力増強")),
    ("order", re.compile(r"大口受注|受注(獲得|決定|のお知らせ)|受注速報")),
    ("price", re.compile(r"価格改定|値上げ")),
)
# v1 の追加グループ（P-INF-30 オーナー裁定 A 2026-10-04・docs §16o-3 追記12）。v0 の 3 グループに当たらなかった
# 表題だけに当てる＝v0 の事象は 1 件も変わらない。行の rule_version を v1-title にして v0 と別に採点する。
RULE_VERSION_V1 = "v1-title"
V1_GROUP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("capex", re.compile(r"設備投資|増設|生産拠点|工場の建設|量産")),
)
GROUP_LABEL = {"capacity": "増産", "order": "受注", "price": "価格", "capex": "設備"}
# 除外（ETF・投信の基準価額、訂正・経過の事務開示）
EXCLUDE_RE = re.compile(r"ETF|基準価額|投資信託|訂正|経過")
# 月次の定期開示（order だが periodic=true・表には出す・採点母集団から外す）
PERIODIC_RE = re.compile(r"受注速報")


def norm_title(title: str) -> str:
    """表題の照合用正規化（NFKC・空白除去）。台帳には元の表題を残す。"""
    return "".join(unicodedata.normalize("NFKC", title or "").split())


def classify(title: str) -> tuple[str | None, bool]:
    """表題 → (group, periodic)。除外語を含む・どのグループにも当たらない時は (None, False)。"""
    t = norm_title(title)
    if not t or EXCLUDE_RE.search(t):
        return None, False
    # 月次定期（受注速報）は他グループの語が同居しても order・periodic=true を優先する
    # （「新工場の稼働状況および9月度受注速報」が capacity に化けて採点母集団に混入する・Codex CONFIRMED-1）
    if PERIODIC_RE.search(t):
        return "order", True
    for group, pat in GROUP_PATTERNS + V1_GROUP_PATTERNS:   # v0 を先に評価（v1 は v0 の取りこぼしだけ）
        if pat.search(t):
            return group, False
    return None, False


def rule_version_of(group: str) -> str:
    """グループ → 語の版（v1 で足したグループだけ v1-title・残りは凍結の v0-title）。"""
    return RULE_VERSION_V1 if group in {g for g, _ in V1_GROUP_PATTERNS} else RULE_VERSION


def load_index(index_dir: Path = INDEX_DIR, since: dt.date = dt.date.min) -> dict[str, dict]:
    """終了日が since 以降の index ファイルを読み Tdnet.id で重複除去して返す（id → Tdnet 行）。

    読み込みと重複除去は tdnet_index_lib.iter_tdnet_records（発見器②と共有）。id の無い行は捨てる
    （台帳の冪等鍵が作れないため）。
    """
    rows: dict[str, dict] = {}
    for t in iter_tdnet_records(str(index_dir), since):
        tid = str(t.get("id") or "")
        if tid and tid not in rows:
            rows[tid] = t
    return rows


def pub_date(pubdate: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(pubdate)[:10])
    except ValueError:
        return None


def scan(rows: dict[str, dict], since: dt.date, until: dt.date,
         center_pin: dict[str, dict], recorded_at: str) -> list[dict]:
    """重複除去済み index 行から期間内の候補を抽出（台帳 1 行の形で返す・pubdate 昇順）。"""
    out: list[dict] = []
    for tid, t in rows.items():
        d = pub_date(t.get("pubdate", ""))
        if d is None or not (since <= d <= until):
            continue
        code = str(t.get("company_code") or "").strip()
        if not code:
            continue
        group, periodic = classify(t.get("title", ""))
        if group is None:
            continue
        code4 = normalize_code(code)
        out.append({
            "id": tid,
            "pubdate": t.get("pubdate"),
            "code": code,
            "code4": code4,
            "name": t.get("company_name") or "",
            "title": t.get("title") or "",
            "group": group,
            "periodic": periodic,
            "in_top1000": code4 in center_pin,
            "rule_version": rule_version_of(group),
            "recorded_at": recorded_at,
        })
    out.sort(key=lambda r: (r["pubdate"] or "", r["id"]))
    return out


def load_ledger(path: Path = LEDGER_PATH) -> list[dict]:
    """台帳を全行返す（無ければ空）。壊れた行は飛ばさず例外にする（表示側で ⚠️ にするため）。"""
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def append_ledger(candidates: list[dict], path: Path = LEDGER_PATH) -> tuple[int, int]:
    """id で冪等 append。(新規件数, 既存件数) を返す。"""
    existing_ids = {r.get("id") for r in load_ledger(path)}
    new_rows = [r for r in candidates if r["id"] not in existing_ids]
    # 0 件でも台帳ファイルは作る（無いと表示側が「生成失敗」と判定する・Codex CONFIRMED-3）
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in new_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(new_rows), len(candidates) - len(new_rows)


def run(since: dt.date, until: dt.date, *, dry_run: bool = False,
        index_dir: Path | None = None, ledger_path: Path | None = None,
        center_pin: dict[str, dict] | None = None) -> dict:
    index_dir = Path(index_dir) if index_dir is not None else INDEX_DIR      # 呼び出し時に解決（テストで差し替え可）
    ledger_path = Path(ledger_path) if ledger_path is not None else LEDGER_PATH
    if not index_dir.is_dir():
        raise FileNotFoundError(f"index ディレクトリが無い: {index_dir}")
    rows = load_index(index_dir, since)
    # 母集団= 重複除去後に期間内（since ≤ pubdate ≤ until）へ絞った表題数（期間外は数えない・Codex CONFIRMED-5）
    scanned = sum(1 for t in rows.values()
                  if (d := pub_date(t.get("pubdate", ""))) is not None and since <= d <= until)
    if scanned == 0:
        # ファイルがあっても収録行が全部期間外なら「候補 0 件の正常終了」にしない（取得ジョブ停止の検知・
        # Codex 2 周目 CONFIRMED）。連休で窓が全休場の朝（年末年始・GW）はこの ⚠️ が出る＝既知・docs 追記10
        raise FileNotFoundError(
            f"index に {since}〜{until} の開示が 1 件も無い（取得ジョブ停止? 連休なら既知）: {index_dir}")
    cp = center_pin if center_pin is not None else load_center_pin()
    recorded_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    cands = scan(rows, since, until, cp, recorded_at)
    periodic = sum(1 for r in cands if r["periodic"])
    top = sum(1 for r in cands if r["in_top1000"])
    if dry_run:
        existing_ids = {r.get("id") for r in load_ledger(ledger_path)}
        new = sum(1 for r in cands if r["id"] not in existing_ids)
        old = len(cands) - new
    else:
        new, old = append_ledger(cands, ledger_path)
    print(
        f"[tdnet_supply] 範囲 {since}〜{until} / 候補 {len(cands)} 件（重複除去後 {scanned} 件の表題を走査）"
        f"/ 新規 {new} 件 / 既存 {old} 件 / periodic {periodic} 件 / TOP1000内 {top} 件"
        + ("（dry-run・書込なし）" if dry_run else "")
    )
    return {"scanned": scanned, "candidates": len(cands), "new": new, "existing": old,
            "periodic": periodic, "in_top1000": top, "rows": cands}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=3,
                    help="今日から N 日前までの pubdate を対象（既定 3＝月曜実行でも金曜分を拾う・冪等なので重なりは無害）")
    ap.add_argument("--since", help="YYYY-MM-DD（--days より優先）")
    ap.add_argument("--until", help="YYYY-MM-DD（既定 今日）")
    ap.add_argument("--dry-run", action="store_true", help="台帳に書かず件数だけ表示")
    a = ap.parse_args(argv)
    today = dt.date.today()
    until = dt.date.fromisoformat(a.until) if a.until else today
    since = dt.date.fromisoformat(a.since) if a.since else until - dt.timedelta(days=a.days)
    if since > until:
        print(f"ERROR: since {since} > until {until}", file=sys.stderr)
        return 2
    try:
        run(since, until, dry_run=a.dry_run)
    except (OSError, ValueError) as exc:
        # 入力欠損・壊れた index は「候補 0 件の正常終了」にしない（daily_screen の表示ビルド状況に ⚠️ が残る）
        print(f"[tdnet_supply] ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
