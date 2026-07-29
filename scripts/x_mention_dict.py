"""X投稿の本文から TOP1000 銘柄への言及を抽出する（辞書＋境界規則＋最長一致）.

なぜ要るか（2026-07-29 の実測）:
X監視は「その言葉が何件投稿されたか」しか数えていなかった。2024-04-02（ラピダスへの
5,900億円追加支援の発表日）の「ラピダス」投稿数は対照日と同じ38件で **±0%** だったが、
本文にはこれが含まれていた:

    「ラピダスに5900億円追加支援 この関係で北海道電力の株価が爆上がりしているのかな」

＝ **件数では鳴らないが、中身には出ていた**。本モジュールはその「中身」を機械で拾う。

誤検出対策（実測21,933件のツイートで検証・詳細は docs/price-watch-universe.md §16c）:
1. **境界規則** — カタカナ社名の前後がカタカナなら別語（フジ←フジクラ を殺す）。
   英数社名も同様（SMC←SMCC）。これだけで誤ヒット 13,435→11,170 に減り、
   「エン」(4849・565件がエンジニア等)「フジ」(8278・574件がフジクラ等)が全消滅した。
2. **最長一致優先** — 「ソフトバンクグループ」を「ソフトバンク」で二重計上しない。
3. **2文字以下の社名は不採用** — 一般語との衝突が避けられない。
4. **裸の4桁コードは不採用** — 日付・価格・年号と衝突するため。`(9509)` 形式のみ拾う。

使い方:
    python3 scripts/x_mention_dict.py            # 辞書の統計と自己テスト
    python3 scripts/x_mention_dict.py --scan <jsonlのtextフィールド>  # 実本文で誤検出を点検
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

APP = Path("/app") if Path("/app/scripts").exists() else Path(__file__).resolve().parent.parent
CENTER_PIN = APP / "data/center_pin/center_pin.jsonl"

MIN_LEN = 3          # 2文字以下の社名は一般語と衝突するため採用しない
KATAKANA = r"ァ-ヶー"
LATIN = r"A-Za-z0-9"

# 末尾のこれらは付いていても外れていても同一企業とみなす（表記ゆれの吸収）
SUFFIX_RE = re.compile(
    r"(ホールディングス|ホールディング|グループ本社|グループホールディングス|グループ|株式会社|HD)$")

# 境界規則を通しても一般語として残るもの。除外理由を必ず添える（後から見直せるように）
#
# 重要: この辞書が読むのは**消費者の投稿**（品薄・値上がり・転売の話題）であって、
# 株クラスタの投稿ではない。株の投稿だけを見て「衝突しない」と判断すると必ず外す。
# 実例: 2026-07-28 の実収集で「プレミア価格を覚悟してましたが…」がプレミアグループ(7199)に
# 誤マッチした。株クラスタ21,933件のコーパスでは同じ誤りが1件も出ていなかった。
MANUAL_EXCLUDE: dict[str, str] = {
    "ディスコ": "Discord/ディスコ(音楽)と衝突。6146は『DISCO』表記が主なので実害は小さいが要注意",
}

# 「接尾辞を落とした短縮形」だけを止める（正式名とコード表記 `(7199)` は引き続き有効）。
# 一般語と同じ綴りになってしまうものが対象。
SHORT_FORM_BLOCK: dict[str, str] = {
    "プレミア": "『プレミア価格』『プレミア感』と衝突（7199 プレミアグループ・実測で誤検出）",
    "ストライク": "野球・ボウリング用語と衝突（6196 ストライク）",
    "トモニ": "『共に』のカタカナ表記と衝突（8600 トモニホールディングス）",
    "アサヒ": "朝日新聞・アサヒ飲料など同名多数と衝突（2502 アサヒグループホールディングス）",
    "ヤマダ": "人名『山田』と衝突（9831 ヤマダホールディングス）",
    "ストーリー": "一般語（該当社があれば正式名で拾う）",
}

# 4桁コードは裸だと日付・価格と衝突するため、括弧などで囲まれた形だけ拾う
CODE_RE = re.compile(r"[（(\[【]\s*(\d{4}|\d{3}[A-Z])\s*[)）\]】]")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s).replace(" ", "").replace("　", "")


def _variants(name: str) -> set[str]:
    """社名の表記ゆれ（接尾辞あり/なし）。MIN_LEN 未満は捨てる。"""
    n = _norm(name)
    out = {n}
    stripped = SUFFIX_RE.sub("", n)
    if stripped and stripped not in SHORT_FORM_BLOCK:
        out.add(stripped)
    return {v for v in out if len(v) >= MIN_LEN and v not in MANUAL_EXCLUDE}


def _boundary(v: str) -> tuple[str, str]:
    """同じ文字種が続いていたら別語とみなすための先読み/後読み。"""
    if re.fullmatch(f"[{KATAKANA}]+", v):
        return f"(?<![{KATAKANA}])", f"(?![{KATAKANA}])"
    if re.fullmatch(f"[{LATIN}]+", v):
        return f"(?<![{LATIN}])", f"(?![{LATIN}])"
    return "", ""


def build_dict(path: Path = CENTER_PIN) -> dict[str, tuple[str, str]]:
    """表記 -> (code, 正式名)。TOP1000台帳が唯一の供給源（関門B: 台帳外は出さない）。"""
    table: dict[str, tuple[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        for v in _variants(r["name"]):
            # 同じ表記が複数社に割り当たったら、曖昧なので両方採用しない
            if v in table and table[v][0] != r["code"]:
                table[v] = ("__AMBIGUOUS__", "")
                continue
            table.setdefault(v, (r["code"], r["name"]))
    return {k: v for k, v in table.items() if v[0] != "__AMBIGUOUS__"}


def build_matcher(table: dict[str, tuple[str, str]]) -> list[tuple[re.Pattern, str, str, str]]:
    """最長一致優先で並べたパターン列。長い表記から先に当てて二重計上を防ぐ。"""
    out = []
    for v in sorted(table, key=len, reverse=True):
        pre, post = _boundary(v)
        out.append((re.compile(pre + re.escape(v) + post), v, *table[v]))
    return out


def find_mentions(text: str, matcher, codes: set[str] | None = None) -> list[dict]:
    """1投稿から言及銘柄を返す。同一銘柄は1件に畳む（連呼で件数が膨らまないように）。"""
    t = _norm(text)
    consumed = [False] * len(t)   # 最長一致で使った位置は短い表記に使わせない
    found: dict[str, dict] = {}
    for pat, variant, code, name in matcher:
        for m in pat.finditer(t):
            if any(consumed[m.start():m.end()]):
                continue
            for i in range(m.start(), m.end()):
                consumed[i] = True
            found.setdefault(code, {"code": code, "name": name, "matched": variant, "by": "name"})
    if codes:
        for m in CODE_RE.finditer(t):
            c = m.group(1)          # 台帳の code は4桁（例 9509 / 285A）
            if c in codes:
                found.setdefault(c, {"code": c, "name": "", "matched": m.group(0), "by": "code"})
    return list(found.values())


def _selftest(matcher, codes) -> int:
    """実測で確認済みの事例を固定テストにする。"""
    cases = [
        ("ラピダスに5900億円追加支援 この関係で北海道電力の株価が爆上がりしているのかな",
         {"9509"}, "2024-04-02 の実投稿。本命ケース"),
        ("データセンター建設で電力消費が増えるらしいです。北海道電力の件もありますし、"
         "今後はデータセンター建設を見ながら電力株を買うのも「あり」かもしれませんね",
         {"9509"}, "2024-04-10 の実投稿"),
        ("エンジニア募集中です", set(), "『エン』(4849)の誤検出が起きないこと"),
        ("フジクラが決算で急騰", {"5803"}, "『フジ』(8278)でなく『フジクラ』(5803)に付くこと"),
        ("ソフトバンクグループの決算", {"9984"}, "短い『ソフトバンク』(9434)に二重計上されないこと"),
        ("メモリ品薄でキオクシアが上昇", {"285A"}, "英数字混じりコードの銘柄"),
        ("プレミア価格を覚悟してましたが定価で購入出来てラッキーでした", set(),
         "2026-07-28 の実収集で出た誤検出。『プレミア』(7199短縮形)に付かないこと"),
        ("プレミアグループ(7199)が上昇", {"7199"},
         "正式名とコード表記なら 7199 を拾えること（短縮形だけを止めている）"),
    ]
    ng = 0
    for text, want, why in cases:
        got = {m["code"] for m in find_mentions(text, matcher, codes)}
        mark = "OK " if got == want else "NG "
        if got != want:
            ng += 1
        print(f"  {mark} {why}\n       期待={sorted(want)} 実際={sorted(got)}")
    return ng


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scan", type=Path, help="本文コーパス(JSON配列 or 1行1テキスト)で誤検出点検")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    table = build_dict()
    matcher = build_matcher(table)
    codes = {c for c, _ in table.values()}
    print(f"=== 銘柄名辞書 ===")
    print(f"表記 {len(table)} 種 / 銘柄 {len(codes)} 社（TOP1000台帳 977社が供給源）")
    print(f"除外: {MIN_LEN}文字未満の社名・曖昧表記・手動除外 {len(MANUAL_EXCLUDE)} 件")
    print("\n=== 自己テスト ===")
    ng = _selftest(matcher, codes)

    if args.scan:
        raw = args.scan.read_text(encoding="utf-8", errors="ignore")
        try:
            texts = json.loads(raw)
        except json.JSONDecodeError:
            texts = [l for l in raw.splitlines() if l.strip()]
        from collections import Counter
        hits = Counter()
        label = {}
        for t in texts:
            for m in find_mentions(str(t), matcher, codes):
                hits[m["code"]] += 1
                if m["name"]:
                    label[m["code"]] = m["name"]
        print(f"\n=== 実本文 {len(texts):,} 件のスキャン ===")
        print(f"言及のあった銘柄 {len(hits)} 社 / 延べ {sum(hits.values()):,} 件")
        for code, c in hits.most_common(args.top):
            print(f"  {c:>6}  {code} {label.get(code, '(コード表記のみ)')}")

    if ng:
        print(f"\nNG: 自己テスト {ng} 件失敗")
        return 1
    print("\nOK: 自己テスト全通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
