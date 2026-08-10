#!/usr/bin/env python3
"""せどりトレンド定点観測 — 週次ダイジェスト生成（観測専用・銘柄非提示）。

設計正本: tasks/sedori_keyword_review.md §7（2026-08-09 新設）。
- 収集は launchd 週次ランナー（sedori_trend_run.sh）が docker 経由で実行済みの前提。
  本スクリプトは data/sedori_trend/texts/ の直近7日分を読み、週次ダイジェストMDを生成する
- 商品名の抽出はヒューリスティック（【】・「」内・カタカナ連・型番パターン）＝人が読む台帳の下書き
- 供給側反応（再販・増産・受注生産）は独立セクションに強調（たまごっち型の見逃し防止＝本レーンの存在理由）
- 台帳・アラート・受益カード・αには一切触れない
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEXTS_DIR = REPO / "data" / "sedori_trend" / "texts"
DIGEST_DIR = REPO / "data" / "sedori_trend" / "digests"

SUPPLY_RE = re.compile(r"再販|増産|受注生産|再入荷|再生産")
# 商品名らしきものの抽出: 【】内・「」内・カタカナ4文字以上の連なり・OP-XX/RTX等の型番
# 各要素は (正規表現, 型番として正規化するか)。型番は \b を使わない（日本語文中では
# 「op-01カートン」のように前後が \w 扱いになり境界が成立せず取り逃す）代わりに、
# 大文字化＋空白除去で表記ゆれを1つの名前に合算する（OP-01 と op-01 を別名で数えない）。
NAME_PATTERNS = [
    (re.compile(r"【([^】]{2,25})】"), False),
    (re.compile(r"「([^」]{2,25})」"), False),
    (re.compile(r"([ァ-ヴー]{4,}(?:ex|EX)?)"), False),
    # 型番の接尾辞は ASCII 英字のみ（\w* だと「rtx4090入荷」の日本語まで型番に飲み込む）
    (re.compile(r"(OP-\d{2}|RTX\s?\d{4}[A-Za-z]*|DDR\d|PS5|Switch\s?2)", re.I), True),
]
STOP_NAMES = {"プレミア", "プレゼント", "キャンペーン", "フォロー", "リポスト", "オンライン",
              "ショップ", "サイト", "アカウント", "タイムライン", "メルカリ", "ヤフオク",
              "アマゾン", "ポイント", "クーポン", "リツイート", "フォロワー"}


def today_utc() -> dt.date:
    """基準日（UTC）。texts/ のファイル名も収集ランナーも UTC 日付なので合わせる。"""
    return dt.datetime.now(dt.timezone.utc).date()


def load_recent(days: int = 7) -> list[dict]:
    """収集ランナーと同じ窓（UTC前日から遡って days 日分）のユニーク投稿を返す。

    ランナー sedori_trend_run.sh は UTC の前日〜7日前を収集する。窓を today-6〜today に
    すると当日（ファイル不在）を含む代わりに最古の収集日を読み落とすため、前日を終端にする。
    """
    end = today_utc() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days - 1)
    rows, seen = [], set()
    if not TEXTS_DIR.exists():
        return rows
    for p in sorted(TEXTS_DIR.glob("*.jsonl")):
        try:
            day = dt.date.fromisoformat(p.stem)
        except ValueError:
            continue
        if day < start or day > end:
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = d.get("status_id") or d.get("text", "")[:140]
            if key and key not in seen:
                seen.add(key)
                rows.append(d)
    return rows


def extract_names(text: str) -> set[str]:
    names = set()
    for pat, as_model in NAME_PATTERNS:
        for m in pat.findall(text):
            m = re.sub(r"\s+", "", m).upper() if as_model else m.strip()
            if 2 <= len(m) <= 25 and m not in STOP_NAMES:
                names.add(m)
    return names


def build_digest(rows: list[dict]) -> str:
    week = today_utc().isocalendar()
    counter: collections.Counter[str] = collections.Counter()
    supply_posts = []
    for r in rows:
        t = r.get("text", "")
        counter.update(extract_names(t))
        if SUPPLY_RE.search(t):
            supply_posts.append(r)
    top = [(n, c) for n, c in counter.most_common(40) if c >= 2]
    lines = [
        f"# せどりトレンド週次ダイジェスト {week[0]}-W{week[1]:02d}",
        "",
        f"観測専用（銘柄非提示・判定なし）。直近7日・ユニーク{len(rows)}投稿・"
        f"収集クエリ4本（configs/sedori_trend.json）。",
        "",
        "## 🏭 供給側反応（再販・増産・受注生産）— たまごっち型の候補",
        "",
    ]
    if supply_posts:
        if len(supply_posts) > 20:
            lines.append(f"（全{len(supply_posts)}件中20件表示）")
        for r in supply_posts[:20]:
            d = (r.get("posted_at") or "")[:10]
            lines.append(f"- [{d}] {r.get('text', '')[:120].replace(chr(10), ' ')}")
    else:
        lines.append("- 今週は該当なし")
    lines += ["", "## 📈 頻出の商品名らしき語（出現2回以上・ヒューリスティック抽出）", ""]
    if top:
        for n, c in top:
            lines.append(f"- {n} ×{c}")
    else:
        lines.append("- 抽出なし")
    lines += ["", "> 注: 本ダイジェストは投資判断・銘柄推奨ではない（本文転載に企業名・製品名が",
              "> 現れることがあるが、受益判定・推奨の意味を一切持たない）。供給側反応に上場メーカーが",
              "> 現れた場合のみ、§0b の関門（決算実読）を通した上で別途検討する。", ""]
    return "\n".join(lines)


def main() -> int:
    rows = load_recent()
    supply_n = sum(1 for r in rows if SUPPLY_RE.search(r.get("text", "")))
    digest = build_digest(rows)
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    week = today_utc().isocalendar()
    out = DIGEST_DIR / f"digest_{week[0]}-W{week[1]:02d}.md"
    out.write_text(digest, encoding="utf-8")
    print(f"digest -> {out.relative_to(REPO)}（投稿{len(rows)}件）")
    print(f"SUPPLY_COUNT={supply_n}")  # 機械可読行（run.sh が完全一致で読む・文言変更禁止）
    return 0


if __name__ == "__main__":
    sys.exit(main())
