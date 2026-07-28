"""東京製鐵「国内鉄スクラップ購入価格」PDFパーサ（標準ライブラリのみ: re + zlib）.

同社サイト https://www.tokyosteel.co.jp/scrapprice/ が公表する改定ごとの PDF を機械解析し、
price_universe_check.py と同じ契約 {value, day_pct, weekly_pct, monthly_pct, src_date, layout}
を返す。

設計の芯（位置固定を使わない）:
  - 行 = 品名（日本語）を PDF 埋め込みフォントの /ToUnicode CMap で復号し、**文字列でアンカー**
  - 列 = 工場名（日本語）を同じく復号し、数値セルの右端から復元した等間隔グリッドへ**幾何割当**
  - 採った表は 6 つの構造チェック（_validate）を全通過した時だけ返す。1 つでも落ちたら
    None を返して黙って誤値を出さない（influx 台帳の既知の罠＝田中貴金属の位置ズレ事故の回避）

参考: 東京製鐵の等級名に "H2" は存在しない。市場の指標品種 H2 に対応する同社等級は「特級」
（日刊鉄鋼新聞の市況表記が「鉄スクラップ(特級(H2))」）。TARGET_GRADE で切替可能。
"""
from __future__ import annotations

import re
import unicodedata
import zlib
from datetime import date, datetime

LAYOUT = "tokyosteel_pdf_v1"
LIST_URL = "https://www.tokyosteel.co.jp/scrapprice/"
BASE_URL = "https://www.tokyosteel.co.jp/assets/docs/scrapprice/price/{year}/{stamp}.pdf"

# 指標品種。東京製鐵の等級名で指定する（市場の H2 = 同社「特級」）
TARGET_GRADE = "特級"
# 代表工場（理由は返却 dict の representative に同梱）
TARGET_FACTORY = "田原工場"

# 価格表の等級は品位の高い順に並ぶ。単調性チェック（行ズレ検知）に使う代表系列
GRADE_ORDER = ["特Ａ", "特級", "一級", "二級", "級外"]
# 妥当性レンジ（円/t）
VALUE_MIN, VALUE_MAX = 10_000.0, 200_000.0
PRICE_STEP = 100.0  # 建値の最小刻み。実測 423 セル中 5 件が 100 円刻み（2025.10.10 の
                    # 「特Ａ」43,700 等）。500 にすると正当な表を parse_fail にする


# --------------------------------------------------------------------------- #
# PDF 低レベル: オブジェクト / ストリーム / ObjStm
# --------------------------------------------------------------------------- #
def _inflate(dic: bytes, raw: bytes) -> bytes | None:
    if b"/FlateDecode" not in dic:
        return raw
    try:
        return zlib.decompress(raw)
    except zlib.error:
        try:  # 末尾ゴミ混入に耐える
            return zlib.decompressobj().decompress(raw)
        except zlib.error:
            return None


def _scan_objects(data: bytes) -> dict[int, tuple[bytes, bytes | None]]:
    """トップレベル `N 0 obj … endobj` を辞書とストリームに分解する。"""
    objs: dict[int, tuple[bytes, bytes | None]] = {}
    for m in re.finditer(rb"(?<![0-9])(\d+)\s+\d+\s+obj\b", data):
        num = int(m.group(1))
        end = data.find(b"endobj", m.end())
        if end < 0:
            continue
        body = data[m.end():end]
        sm = re.search(rb"stream\r?\n", body)
        if sm:
            dic = body[:sm.start()]
            se = body.find(b"endstream", sm.end())
            objs[num] = (dic, _inflate(dic, body[sm.end():se if se >= 0 else len(body)]))
        else:
            objs[num] = (body, None)
    return objs


def _expand_objstm(objs: dict[int, tuple[bytes, bytes | None]]) -> None:
    """圧縮オブジェクトストリーム(/ObjStm)の中身を objs へ展開する（PDF1.5+ 対策）。"""
    for dic, stream in list(objs.values()):
        if b"/ObjStm" not in dic or stream is None:
            continue
        n = re.search(rb"/N\s+(\d+)", dic)
        first = re.search(rb"/First\s+(\d+)", dic)
        if not n or not first:
            continue
        n, first = int(n.group(1)), int(first.group(1))
        head = stream[:first].split()
        for i in range(n):
            try:
                num, off = int(head[2 * i]), int(head[2 * i + 1])
            except (IndexError, ValueError):
                break
            nxt = int(head[2 * i + 3]) + first if 2 * i + 3 < len(head) else len(stream)
            if num not in objs:  # トップレベル定義を優先（増分更新の上書き対策）
                objs[num] = (stream[first + off:nxt], None)


def _ref(dic: bytes, key: bytes) -> int | None:
    m = re.search(key + rb"\s+(\d+)\s+\d+\s+R", dic)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# フォント: ToUnicode CMap と字幅
# --------------------------------------------------------------------------- #
def _parse_cmap(stream: bytes) -> dict[int, str]:
    txt = stream.decode("latin-1", "replace")
    mp: dict[int, str] = {}
    for blk in re.findall(r"beginbfchar(.*?)endbfchar", txt, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            mp[int(src, 16)] = "".join(
                chr(int(dst[i:i + 4], 16)) for i in range(0, len(dst) - 3, 4))
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", txt, re.S):
        for lo, hi, dst in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            lo_i, hi_i, d = int(lo, 16), int(hi, 16), int(dst, 16)
            if hi_i - lo_i > 0xFFFF:
                continue
            for k in range(lo_i, hi_i + 1):
                mp[k] = chr(d + k - lo_i)
    return mp


def _parse_w_array(body: bytes) -> dict[int, float]:
    """CIDFont の /W 配列 `[ c [w…] cfirst clast w ]` を展開する。"""
    m = re.search(rb"/W\s*\[(.*)\]", body, re.S)
    if not m:
        return {}
    toks = re.findall(rb"\[|\]|-?[\d.]+", m.group(1))
    widths: dict[int, float] = {}
    i, cur = 0, None
    while i < len(toks):
        t = toks[i]
        if t == b"[":
            j, k = i + 1, 0
            while j < len(toks) and toks[j] != b"]":
                if cur is not None:
                    widths[int(cur) + k] = float(toks[j])
                j += 1
                k += 1
            i, cur = j + 1, None
            continue
        if t == b"]":
            i += 1
            continue
        if cur is None:
            cur = float(t)
            i += 1
            continue
        if i + 1 < len(toks) and toks[i + 1] not in (b"[", b"]"):
            lo, hi, w = int(cur), int(float(t)), float(toks[i + 1])
            if 0 <= hi - lo <= 65535:
                for c in range(lo, hi + 1):
                    widths[c] = w
            i, cur = i + 2, None
        else:
            cur = float(t)
            i += 1
    return widths


class _Font:
    """1 フォント分のデコーダ（2byte CID or 1byte simple）。"""

    def __init__(self, objs, num):
        body = objs.get(num, (b"", None))[0]
        self.two_byte = b"/Type0" in body
        self.cmap: dict[int, str] = {}
        tu = _ref(body, b"/ToUnicode")
        if tu is not None and objs.get(tu, (b"", None))[1]:
            self.cmap = _parse_cmap(objs[tu][1])
        self.widths: dict[int, float] = {}
        self.default_w = 1000.0 if self.two_byte else 500.0
        if self.two_byte:
            desc = re.search(rb"/DescendantFonts\s*\[?\s*(\d+)\s+\d+\s+R", body)
            if desc:
                dbody = objs.get(int(desc.group(1)), (b"", None))[0]
                dw = re.search(rb"/DW\s+([\d.]+)", dbody)
                if dw:
                    self.default_w = float(dw.group(1))
                wref = _ref(dbody, b"/W")
                if wref is not None:
                    self.widths = _parse_w_array(b"/W " + objs.get(wref, (b"", None))[0])
                else:
                    self.widths = _parse_w_array(dbody)
        else:
            fc = re.search(rb"/FirstChar\s+(\d+)", body)
            wref = _ref(body, b"/Widths")
            warr = objs.get(wref, (b"", None))[0] if wref is not None else b""
            if not warr:
                inline = re.search(rb"/Widths\s*\[(.*?)\]", body, re.S)
                warr = inline.group(1) if inline else b""
            nums = [float(x) for x in re.findall(rb"-?[\d.]+", warr)]
            if fc and nums:
                self.widths = {int(fc.group(1)) + i: w for i, w in enumerate(nums)}

    def decode(self, raw: bytes) -> tuple[str, float]:
        """バイト列 → (テキスト, 幅/1000em)。未マップ CID は U+FFFD。"""
        text, width = [], 0.0
        codes = ([raw[i] * 256 + raw[i + 1] for i in range(0, len(raw) - 1, 2)]
                 if self.two_byte else list(raw))
        for c in codes:
            text.append(self.cmap.get(c, "�" if self.two_byte else chr(c)))
            width += self.widths.get(c, self.default_w)
        return "".join(text), width / 1000.0


# --------------------------------------------------------------------------- #
# コンテンツストリーム: 座標つきテキスト抽出
# --------------------------------------------------------------------------- #
_TOKEN = re.compile(
    rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>|\[|\]|-?[\d.]+|/[^\s/\[\]<>()]+|[A-Za-z'\"*]+",
    re.S)


def _unescape(s: bytes) -> bytes:
    out, i = bytearray(), 0
    table = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
    while i < len(s):
        if s[i] == 0x5C and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in table:
                out.append(table[nxt])
                i += 2
            elif 0x30 <= nxt <= 0x37:
                j, oct_ = i + 1, b""
                while j < len(s) and len(oct_) < 3 and 0x30 <= s[j] <= 0x37:
                    oct_ += s[j:j + 1]
                    j += 1
                out.append(int(oct_, 8) & 0xFF)
                i = j
            else:
                out.append(nxt)
                i += 2
        else:
            out.append(s[i])
            i += 1
    return bytes(out)


def _mul(m: list[float], n: list[float]) -> list[float]:
    return [m[0] * n[0] + m[1] * n[2], m[0] * n[1] + m[1] * n[3],
            m[2] * n[0] + m[3] * n[2], m[2] * n[1] + m[3] * n[3],
            m[4] * n[0] + m[5] * n[2] + n[4], m[4] * n[1] + m[5] * n[3] + n[5]]


def _extract_items(content: bytes, fonts: dict[str, _Font]) -> list[dict] | None:
    """[{text, x0, x1, y, size}] を返す。回転・傾きがあれば None（想定外レイアウト）。"""
    items: list[dict] = []
    ctm: list[float] = [1, 0, 0, 1, 0, 0]
    ctm_stack: list[list[float]] = []
    tm = tlm = [1, 0, 0, 1, 0, 0]
    font: _Font | None = None
    size, leading, hscale = 0.0, 0.0, 1.0
    stack: list[bytes] = []

    def nums(k):
        v = [float(t) for t in stack if re.fullmatch(rb"-?[\d.]+", t)]
        return v[-k:] if len(v) >= k else None

    for tk in _TOKEN.findall(content):
        if tk[:1] in b"(<[]" or re.fullmatch(rb"-?[\d.]+", tk) or tk[:1] == b"/":
            stack.append(tk)
            continue
        op = tk
        if op == b"q":
            ctm_stack.append(ctm[:])
        elif op == b"Q" and ctm_stack:
            ctm = ctm_stack.pop()
        elif op == b"cm":
            v = nums(6)
            if v:
                ctm = _mul(v, ctm)
        elif op == b"BT":
            tm = tlm = [1, 0, 0, 1, 0, 0]
        elif op == b"Tf":
            v = [t for t in stack if t[:1] == b"/"]
            n = nums(1)
            if v:
                font = fonts.get(v[-1][1:].decode("latin-1"))
            if n:
                size = n[0]
        elif op == b"Tz":
            n = nums(1)
            if n:
                hscale = n[0] / 100.0
        elif op == b"TL":
            n = nums(1)
            if n:
                leading = n[0]
        elif op == b"Tm":
            v = nums(6)
            if v:
                tm = tlm = v
        elif op in (b"Td", b"TD"):
            v = nums(2)
            if v:
                if op == b"TD":
                    leading = -v[1]
                tlm = _mul([1, 0, 0, 1, v[0], v[1]], tlm)
                tm = tlm[:]
        elif op == b"T*":
            tlm = _mul([1, 0, 0, 1, 0, -leading], tlm)
            tm = tlm[:]
        elif op in (b"Tj", b"TJ", b"'", b'"'):
            if op in (b"'", b'"'):
                tlm = _mul([1, 0, 0, 1, 0, -leading], tlm)
                tm = tlm[:]
            eff = _mul(tm, ctm)
            if abs(eff[1]) > 1e-6 or abs(eff[2]) > 1e-6:
                return None  # 回転/傾き＝想定外レイアウト。黙って座標を信じない
            scale = size * hscale * eff[0]
            # TJ の要素ごとに開始位置を持たせる。iText で再生成された回（実測 2026-04-16）は
            # 1 行分の数値が 1 個の TJ に 1 文字ずつ詰まっており、まとめて 1 要素として扱うと
            # セル境界が消えて表が壊れる
            runs, pos, in_array = [], 0.0, b"[" in stack
            for t in stack:
                if t[:1] in b"(<":
                    if t[:1] == b"(":
                        txt, w = font.decode(_unescape(t[1:-1])) if font else ("", 0.0)
                    else:
                        h = re.sub(rb"\s", b"", t[1:-1])
                        if len(h) % 2:
                            h += b"0"
                        try:
                            txt, w = (font.decode(bytes.fromhex(h.decode()))
                                      if font else ("", 0.0))
                        except ValueError:
                            txt, w = "", 0.0
                    runs.append([pos, txt, w])
                    pos += w
                elif in_array and re.fullmatch(rb"-?[\d.]+", t):
                    pos -= float(t) / 1000.0  # カーニング（負値で右送り）
            merged: list[list] = []
            for x0, txt, w in runs:  # 0.3em 未満の隙間は同一セルとして再結合
                if merged and x0 - (merged[-1][0] + merged[-1][2]) < 0.3:
                    merged[-1][1] += txt
                    merged[-1][2] = x0 + w - merged[-1][0]
                else:
                    merged.append([x0, txt, w])
            for x0, txt, w in merged:
                if txt.strip():
                    items.append({"text": txt, "x0": eff[4] + x0 * scale,
                                  "x1": eff[4] + (x0 + w) * scale,
                                  "y": eff[5], "size": size * eff[3]})
            stack = []
            continue
        stack = []
    return items


# --------------------------------------------------------------------------- #
# 表の再構成
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    """全角英数・全角ハイフンを畳み、空白(U+3000含む)を除去した比較用キー。"""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\s　]+", "", s)
    return s.replace("－", "-").replace("―", "-").replace("ー", "-")


_NUM = re.compile(r"^-?[\d,]+$")
_CJK = re.compile(r"[぀-ヿ一-鿿Ａ-Ｚ]")


def _cluster_rows(items, tol=2.0):
    rows: list[list[dict]] = []
    for it in sorted(items, key=lambda d: -d["y"]):
        if rows and abs(rows[-1][0]["y"] - it["y"]) <= tol:
            rows[-1].append(it)
        else:
            rows.append([it])
    return [sorted(r, key=lambda d: d["x0"]) for r in rows]


def _build_grid(rows, label_max_x=120.0):
    """数値セルの右端から列の等間隔格子を復元する。格子に乗らなければ None。

    列が丸ごと空の改定がある（実測 2026-05-23: 高松列が空欄で「5月27日」開始の注記のみ）ため、
    観測された右端クラスタが等間隔であることを要求せず、**最小間隔を 1 列ピッチとみなして
    全クラスタが同一格子上に載るか**を検査する。載らなければレイアウト変更として None。
    """
    edges = [it["x1"] for row in rows for it in row
             if _NUM.match(it["text"].strip()) and it["x0"] > label_max_x]
    if len(edges) < 8:
        return None
    cols: list[list[float]] = []
    for e in sorted(edges):
        if cols and e - cols[-1][-1] <= 3.0:
            cols[-1].append(e)
        else:
            cols.append([e])
    centers = [sum(c) / len(c) for c in cols]
    if len(centers) < 3:
        return None
    pitch = min(b - a for a, b in zip(centers, centers[1:]))
    if pitch < 10.0:
        return None
    ks = []
    for c in centers:
        k = round((c - centers[0]) / pitch)
        if abs(c - (centers[0] + k * pitch)) > 1.5:
            return None  # 同一格子に載らない = 右揃え前提/列構成の崩壊
        ks.append(k)
    if len(set(ks)) != len(ks):
        return None
    return [centers[0] + k * pitch for k in range(max(ks) + 1)], pitch


def _header_band(rows, data_top, centers, pitch, col_of_left, max_rows=10):
    """データ帯の直上から上へ走査し、列ヘッダ帯のラベルを列ごとに集める。

    工場名の一覧はハードコードしない（2025→2026 で「高松/サテライト」→「高松鉄鋼/センター」の
    改称を実測。名前リスト方式だと改称のたびに系列ごと落ちる）。代わりに
      - 左端ラベル(x<120)は読み飛ばす（「品名」など表の角セル）
      - 1 列幅に収まり(x1-x0 <= pitch)、かつ列に割り当てられるラベルだけ採る
      - それ以外（住所・E-Mail・表題など列を横断する広い文字列）が現れたら走査を打ち切る
    という幾何条件だけで帯を決める。戻り値は {列index: [上から順のラベル]}。
    """
    above = [r for r in rows if r[0]["y"] > data_top]
    band: dict[int, list[str]] = {}
    for row in reversed(above):  # データ帯に近い行から上へ
        picked = []
        for it in row:
            if it["x0"] < 120:
                continue  # 行見出し列（品名など）
            if it["x1"] - it["x0"] > pitch + 0.5:
                return band  # 列を横断する広い文字列＝ヘッダ帯の外。打ち切り
            ci = col_of_left(it["x0"])
            if ci is None:
                return band
            picked.append((ci, _norm(it["text"])))
        for ci, txt in picked:
            band.setdefault(ci, []).insert(0, txt)  # 上の行ほど前に来る
        max_rows -= 1
        if max_rows <= 0:
            break
    return band


def _parse_page(items) -> dict | None:
    """1 ページ分を {"factories", "table", "meta", "n_cells"} へ。失敗は None。"""
    rows = _cluster_rows(items)
    grid = _build_grid(rows)
    if grid is None:
        return None
    base, pitch = grid
    # 端の列が丸ごと空の改定に備え、格子を左右 2 セルずつ延長した上で最終的な列範囲を決める
    pad = 2
    centers = [base[0] + (k - pad) * pitch for k in range(len(base) + 2 * pad)]

    def col_of_edge(x1):
        best = min(range(len(centers)), key=lambda i: abs(centers[i] - x1))
        return best if abs(centers[best] - x1) <= 3.0 else None

    def col_of_left(x0):
        """ラベル左端がどのセル [右端-pitch, 右端) に入るか（中央揃え前提）。"""
        for i, c in enumerate(centers):
            if c - pitch - 0.5 <= x0 < c:
                return i
        return None

    table: dict[str, dict[int, float]] = {}
    data_row_y: list[float] = []
    value_cols: set[int] = set()
    for row in rows:
        labels = [it for it in row if it["x0"] < 120 and _CJK.search(it["text"])]
        vals = [it for it in row if it["x0"] > 120 and _NUM.match(it["text"].strip())]
        if len(labels) != 1 or not vals:
            continue
        name = _norm(labels[0]["text"])
        if not name or name in table:
            continue
        cells: dict[int, float] = {}
        for it in vals:
            ci = col_of_edge(it["x1"])
            if ci is None or ci in cells:
                return None  # 列に割り当てられない/重複 = 格子前提の崩壊
            cells[ci] = float(it["text"].replace(",", ""))
        table[name] = cells
        value_cols |= set(cells)
        data_row_y.append(row[0]["y"])
    if not table:
        return None

    band = _header_band(rows, max(data_row_y), centers, pitch, col_of_left)
    if not band:
        return None
    lo = min(value_cols | set(band))
    hi = max(value_cols | set(band))
    if any(i not in band for i in range(lo, hi + 1)):
        return None  # ヘッダを持たない列がある = レイアウト変更
    names = {i: "/".join(band[i]) for i in range(lo, hi + 1)}
    if len(set(names.values())) != hi - lo + 1:
        return None  # 列ヘッダが一意でない = 列の取り違えが起きうる

    meta: dict[str, str] = {}
    for it in items:
        t = it["text"]
        m = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日", t)
        if m:
            iso = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            key = "effective_date" if ("適用" in t or "より" in t) else "issued_date"
            meta.setdefault(key, iso)
        m = re.search(r"No\.\s*([\d\-]+)", t)
        if m:
            meta.setdefault("revision_no", m.group(1))
        if "改定幅" in _norm(t) or "対前回" in _norm(t):
            meta["is_diff_page"] = "1"
    named = {g: {names[i]: v for i, v in cells.items()}
             for g, cells in table.items()}
    return {"factories": [names[i] for i in range(lo, hi + 1)],
            "table": named, "meta": meta,
            "n_cells": sum(len(c) for c in table.values())}


# --------------------------------------------------------------------------- #
# 自己検証
# --------------------------------------------------------------------------- #
def _match_factory(cells: dict) -> str | None:
    """合成ヘッダのうち TARGET_FACTORY を含む列を 1 つだけ選ぶ（0 件/複数一致なら None）。"""
    hits = [k for k in cells if _norm(TARGET_FACTORY) in k]
    return hits[0] if len(hits) == 1 else None


def _validate(price: dict, diff: dict | None) -> tuple[bool, list[str]]:
    checks: list[str] = []
    ok = True

    def fail(msg):
        nonlocal ok
        ok = False
        checks.append("NG:" + msg)

    tbl = price["table"]
    # 1) 指標行・代表列の存在（文字列アンカー）
    if _norm(TARGET_GRADE) not in tbl:
        fail(f"grade_missing({TARGET_GRADE})")
    elif _match_factory(tbl[_norm(TARGET_GRADE)]) is None:
        fail(f"factory_missing({TARGET_FACTORY})")
    else:
        checks.append("OK:anchor")
    # 2) 値域と刻み
    vals = [v for cells in tbl.values() for v in cells.values()]
    if not vals or not all(VALUE_MIN <= v <= VALUE_MAX for v in vals):
        fail("value_range")
    elif not all(abs(v / PRICE_STEP - round(v / PRICE_STEP)) < 1e-6 for v in vals):
        fail("value_step")
    else:
        checks.append("OK:range_step")
    # 3) 等級の単調性（行ラベルと値の対応ズレを意味論で検知）
    order = [_norm(g) for g in GRADE_ORDER if _norm(g) in tbl]
    if len(order) >= 3:
        bad = []
        for f in price["factories"]:
            seq = [tbl[g][f] for g in order if f in tbl[g]]
            if any(a < b for a, b in zip(seq, seq[1:])):
                bad.append(f)
        if bad:
            fail("grade_monotonicity(" + ",".join(bad) + ")")
        else:
            checks.append("OK:monotonic")
    else:
        fail("grade_order_unavailable")
    # 4) 適用開始日
    eff = price["meta"].get("effective_date")
    if not eff:
        fail("effective_date_missing")
    else:
        try:
            d = datetime.strptime(eff, "%Y-%m-%d").date()
        except ValueError:
            fail("effective_date_unparsable")
        else:
            if not (date(2000, 1, 1) <= d <= date(date.today().year + 1, 12, 31)):
                fail("effective_date_range")
            else:
                checks.append("OK:date")
    # 5) 改定番号の年（下2桁）と適用日の年の整合
    rev = price["meta"].get("revision_no", "")
    if rev and eff and re.match(r"^\d{2}-\d{3}$", rev):
        if rev[:2] != eff[2:4]:
            fail(f"revision_year_mismatch({rev} vs {eff})")
        else:
            checks.append("OK:revision_no")
    # 6) 前回比ページの形（品名×工場のセル集合）が価格ページと一致すること
    if diff is not None:
        if ({g: set(c) for g, c in tbl.items()}
                != {g: set(c) for g, c in diff["table"].items()}):
            fail("diff_page_shape_mismatch")
        elif not all(abs(v) <= 10_000 and abs(v / PRICE_STEP - round(v / PRICE_STEP)) < 1e-6
                     for c in diff["table"].values() for v in c.values()):
            fail("diff_value_implausible")
        else:
            checks.append("OK:diff_page")
    return ok, checks


# --------------------------------------------------------------------------- #
# 公開 API
# --------------------------------------------------------------------------- #
REPRESENTATIVE_REASON = (
    "田原工場（世界最大級の電炉拠点・価格表の最左列）。2026-02〜07 の全22改定を実測した根拠: "
    "(1)『特級』の建値が22/22回で存在（欠測ゼロ）(2)全13品種での欠測が22/286＝銑ダライ粉のみ "
    "(3)16/22回で全8拠点中の最高値＝他拠点の上限として機能。"
    "ただし改定で最も動きが少ない列でもある（値が動いた回 10/22。宇都宮・東京湾岸は 15/22）。"
    "早期検知を優先するなら TARGET_FACTORY を『宇都宮工場』へ変えるか、返り値の table から"
    "全8拠点を使う。"
)


def parse_tokyosteel_scrap(pdf_bytes: bytes) -> dict | None:
    """東京製鐵のスクラップ購入価格 PDF を解析する。

    Args:
        pdf_bytes: PDF のバイト列。

    Returns:
        価格表 dict、または構造チェックに 1 つでも落ちた場合 None（誤値を返さない）。
        - value: 指標品種 TARGET_GRADE（=H2 相当「特級」）の TARGET_FACTORY 建値（円/t）
        - day_pct/weekly_pct/monthly_pct: 単一 PDF からは算出不能のため常に None
          （fetch_tokyosteel_scrap が過去回 PDF と突き合わせて埋める）
        - src_date: 適用開始日（発行日ではない）
    """
    if not pdf_bytes.startswith(b"%PDF"):
        return None
    objs = _scan_objects(pdf_bytes)
    _expand_objstm(objs)

    pages = []
    for num, (body, _stream) in objs.items():
        flat = body.replace(b" ", b"")
        if b"/Type/Page" not in flat or b"/Type/Pages" in flat:
            continue
        fm = re.search(rb"/Font\s*<<(.*?)>>", body, re.S)
        if fm:
            fdict = fm.group(1)
        else:
            ref = _ref(body, b"/Font")
            fdict = objs.get(ref, (b"", None))[0] if ref is not None else b""
        fonts = {name.decode("latin-1"): _Font(objs, int(fnum))
                 for name, fnum in re.findall(rb"/([^\s/]+)\s+(\d+)\s+\d+\s+R", fdict)}
        cm = re.search(rb"/Contents([^/]*)", body)
        cnums = [int(x) for x in re.findall(rb"(\d+)\s+\d+\s+R", cm.group(1))] if cm else []
        content = b"".join(objs[c][1] for c in cnums if objs.get(c) and objs[c][1])
        if content and fonts:
            pages.append((num, content, fonts))
    if not pages:
        return None

    parsed = []
    for _num, content, fonts in sorted(pages):
        items = _extract_items(content, fonts)
        if not items:
            continue
        page = _parse_page(items)
        if page:
            parsed.append(page)
    if not parsed:
        return None

    price = next((p for p in parsed if not p["meta"].get("is_diff_page")), None)
    diff = next((p for p in parsed if p["meta"].get("is_diff_page")), None)
    if price is None:
        return None

    ok, checks = _validate(price, diff)
    if not ok:
        return None

    grade = _norm(TARGET_GRADE)
    factory = _match_factory(price["table"][grade])
    value = price["table"][grade][factory]
    chg = diff["table"].get(grade, {}).get(factory) if diff else None
    return {
        "value": value,
        "day_pct": None,      # 日次公表ではないため日次変化率は定義しない
        "weekly_pct": None,   # 単一 PDF では算出不能（fetch_… が埋める）
        "monthly_pct": None,
        "src_date": price["meta"]["effective_date"],
        "layout": LAYOUT,
        "unit": "JPY/t",
        "grade": TARGET_GRADE,
        "factory": factory,            # table のキーとして使える合成ヘッダ
        "factory_label": factory.split("/")[0],
        "factory_anchor": TARGET_FACTORY,
        "representative": REPRESENTATIVE_REASON,
        "issued_date": price["meta"].get("issued_date"),
        "revision_no": price["meta"].get("revision_no"),
        "prev_change": chg,
        "prev_change_pct": (round(chg / (value - chg) * 100, 2)
                            if chg is not None and value != chg else None),
        "factories": price["factories"],
        "grades": list(price["table"]),
        "table": price["table"],
        "n_cells": price["n_cells"],
        "checks": checks,
    }


def _fetch(url: str, timeout: int = 30) -> bytes:
    from urllib.request import Request, urlopen
    req = Request(url, headers={"User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310  固定ドメインのみ
        return resp.read()


def list_pdf_urls(html: str | None = None) -> list[tuple[str, str]]:
    """一覧ページから [(適用開始日 'YYYY-MM-DD', 絶対URL)] を新しい順に返す。"""
    if html is None:
        html = _fetch(LIST_URL).decode("utf-8", "replace")
    found = {}
    for y, stamp in re.findall(
            r"scrapprice/price/(\d{4})/(\d{4}\.\d{2}\.\d{2})\.pdf", html):
        found[stamp.replace(".", "-")] = BASE_URL.format(year=y, stamp=stamp)
    return sorted(found.items(), reverse=True)


def latest_pdf_url(html: str | None = None) -> str | None:
    """一覧ページの最新改定日 PDF の URL。見つからなければ None。"""
    urls = list_pdf_urls(html)
    return urls[0][1] if urls else None


def fetch_tokyosteel_scrap(today: str | None = None) -> dict | None:
    """最新 PDF を解析し、過去回 PDF と突き合わせて weekly/monthly を埋める。

    weekly_pct = 7 日前時点で有効だった建値との比、monthly_pct = 30 日前時点との比。
    参照回が見つからない場合は None のまま（推定で埋めない）。
    """
    urls = list_pdf_urls()
    if not urls:
        return None
    cur = parse_tokyosteel_scrap(_fetch(urls[0][1]))
    if cur is None:
        return None
    base = datetime.strptime(today or cur["src_date"], "%Y-%m-%d").date()
    for days, key in ((7, "weekly_pct"), (30, "monthly_pct")):
        target = date.fromordinal(base.toordinal() - days).isoformat()
        ref = next((u for d, u in urls if d <= target), None)
        if ref is None:
            continue
        past = parse_tokyosteel_scrap(_fetch(ref))
        if past and past["value"]:
            cur[key] = round((cur["value"] / past["value"] - 1) * 100, 2)
    return cur
