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
# 各要素は (正規表現, 型番として正規化するか)。
# 型番の境界は `\b` ではなく「前後に ASCII 英数字が来ない」で表す。`\b` は日本語文中では
# 前後が \w 扱いで境界が成立せず「op-01カートン」を取り逃す一方、境界を単に外すと
# 「PS50周年」「DDR5000円」「OP-010」やURL断片から偽の型番を拾ってしまう（Codex 2026-08-10 MEDIUM-1）。
# マッチは大文字化＋空白除去で正規化し、表記ゆれを1つの名前に合算する（OP-01 と op-01 を別名で数えない）。
# 接尾辞は ASCII 英字のみ（\w* だと「rtx4090入荷」の日本語まで型番に飲み込む）。
MODEL_RE = re.compile(
    r"(?<![A-Za-z0-9])(OP-\d{2}|RTX\s?\d{4}[A-Za-z]*|DDR\d|PS5|Switch\s?2)(?![A-Za-z0-9])",
    re.I,
)
NAME_PATTERNS = [
    (re.compile(r"【([^】]{2,25})】"), False),
    (re.compile(r"「([^」]{2,25})」"), False),
    (re.compile(r"([ァ-ヴー]{4,}(?:ex|EX)?)"), False),
    (MODEL_RE, True),
]
STOP_NAMES = {"プレミア", "プレゼント", "キャンペーン", "フォロー", "リポスト", "オンライン",
              "ショップ", "サイト", "アカウント", "タイムライン", "メルカリ", "ヤフオク",
              "アマゾン", "ポイント", "クーポン", "リツイート", "フォロワー"}
# 完全一致の STOP_NAMES では「オンラインショップ」「プレゼントキャンペーン」のような複合語が
# 商品名として通ってしまうため、宣伝語を含む候補も落とす（Codex 2026-08-10 LOW-3）。
STOP_SUBSTRINGS = ("キャンペーン", "プレゼント", "フォロー", "リポスト", "リツイート",
                   "オンラインショップ", "クーポン")


def today_utc() -> dt.date:
    """基準日（UTC）。texts/ のファイル名も収集ランナーも UTC 日付なので合わせる。"""
    return dt.datetime.now(dt.timezone.utc).date()


def window(days: int = 7) -> tuple[dt.date, dt.date]:
    """集計窓 (start, end) を返す唯一の定義。

    ランナー sedori_trend_run.sh は UTC の前日〜7日前を収集する。窓を today-6〜today に
    すると当日（ファイル不在）を含む代わりに最古の収集日を読み落とすため、前日を終端にする。
    週ラベル・ファイル名もこの窓の終端から決める（読込・表示・命名で窓の定義を二重に持たない）。
    """
    end = today_utc() - dt.timedelta(days=1)
    return end - dt.timedelta(days=days - 1), end


def load_recent(days: int = 7) -> list[dict]:
    """収集ランナーと同じ窓（UTC前日から遡って days 日分）のユニーク投稿を返す。"""
    start, end = window(days)
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
            # JSON として妥当でも形が違う行（[] / null / text が文字列でない）で週次処理を
            # 落とさない。ランナーは digest 失敗で非ゼロ終了するため1行の異常が全停止になる
            # （Codex 2026-08-10 LOW-4）。
            if not isinstance(d, dict) or not isinstance(d.get("text", ""), str):
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
            if not 2 <= len(m) <= 25 or m in STOP_NAMES:
                continue
            if any(s in m for s in STOP_SUBSTRINGS):
                continue
            names.add(m)
    return names


def week_label(days: int = 7) -> str:
    """ダイジェストの週ラベル。窓の終端が属する ISO 週で名付ける唯一の定義。

    生成日基準にすると、月曜09:00の定期実行で「中身は前週なのにファイル名は当週」になる
    （Codex 2026-08-10 MEDIUM-2）。年またぎでは年もずれるため、必ず窓の終端で決める。
    """
    iso = window(days)[1].isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def build_digest(rows: list[dict]) -> str:
    start, end = window()
    counter: collections.Counter[str] = collections.Counter()
    supply_posts = []
    for r in rows:
        t = r.get("text", "")
        counter.update(extract_names(t))
        if SUPPLY_RE.search(t):
            supply_posts.append(r)
    # 新しい順に並べてから20件へ切る。古い順のままだと、窓の終盤に出た重要な増産発表が
    # 省略側に落ちて見逃し防止という目的と逆になる（Codex 2026-08-10 LOW-6）。
    supply_posts.sort(key=lambda r: r.get("posted_at") or "", reverse=True)
    top = [(n, c) for n, c in counter.most_common(40) if c >= 2]
    # 収集日ラベル（ファイル名）と投稿の実時刻は一致しない。X 検索の since/until 窓が
    # 収集日と1日程度ずれるため、実測レンジを併記して「対象期間」を誤読させない。
    stamps = sorted(s[:10] for s in (r.get("posted_at") or "" for r in rows) if s)
    actual = f"{stamps[0]}〜{stamps[-1]}" if stamps else "不明"
    lines = [
        f"# せどりトレンド週次ダイジェスト {week_label()}",
        "",
        f"観測専用（銘柄非提示・判定なし）。**収集日ラベル {start.isoformat()}〜{end.isoformat()}"
        f"（ファイル名基準）／投稿の実時刻レンジ {actual}（UTC）**・"
        f"ユニーク{len(rows)}投稿・収集クエリ4本（configs/sedori_trend.json）。",
        "",
        "## 🏭 供給側反応（再販・増産・受注生産）— たまごっち型の候補",
        "",
    ]
    if supply_posts:
        if len(supply_posts) > 20:
            lines.append(f"（全{len(supply_posts)}件中、新しい順に20件表示）")
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
    out = DIGEST_DIR / f"digest_{week_label()}.md"
    out.write_text(digest, encoding="utf-8")
    print(f"digest -> {out.relative_to(REPO)}（投稿{len(rows)}件）")
    print(f"SUPPLY_COUNT={supply_n}")  # 機械可読行（run.sh が完全一致で読む・文言変更禁止）
    return 0


if __name__ == "__main__":
    sys.exit(main())
