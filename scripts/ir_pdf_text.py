#!/usr/bin/env python3
"""決算説明会資料（PDF）をテキスト化する（P-INF-34 工程①の補助・plan T3）。

pypdf でページごとに文字を取り出し、`\\n\\n[page N]\\n` で区切った 1 本の文字列にする。
CLI は同名の `.txt` を PDF の隣に書き `pages=<n> chars=<n>` を出す。`--url` を付けると先に
urllib で 1 回だけ保存する（既にファイルがあれば取りに行かない）。

使い方:
    python scripts/ir_pdf_text.py --pdf data/edinet/yuho/2802/ir/FY2025_presentation.pdf
    python scripts/ir_pdf_text.py --url https://example.com/x.pdf --out data/edinet/yuho/2802/ir/x.pdf
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from pypdf import PdfReader

TIMEOUT_SECONDS = 60
USER_AGENT = "Mozilla/5.0 (compatible; influx-ir-pdf-text/1.0)"


def page_texts(pdf_path: Path) -> list[str]:
    """PDF の各ページの本文（strip 済み）をページ順に返す。文字が取れないページは ""。

    pypdf がフォント不備で例外を投げたページも "" にして先へ進む（ページ単位で握り潰す）。
    """
    reader = PdfReader(str(pdf_path))
    texts: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - pypdf の抽出失敗はページ単位で握り潰して先へ進む
            text = ""
        texts.append(text.strip())
    return texts


def join_pages(texts: list[str]) -> str:
    """ページ本文の list を `[page N]` 見出し付きの 1 本の文字列にする（N は 1 始まり）。"""
    return "\n\n".join(f"[page {i}]\n{text}" for i, text in enumerate(texts, start=1))


def pdf_to_text(pdf_path: Path) -> str:
    """PDF の全ページを `[page N]` 見出し付きで 1 本の文字列にする。

    ページ区切りは `\\n\\n[page N]\\n`（N は 1 始まり）。文字が取れないページは見出しだけ残す。

    Args:
        pdf_path: 読む PDF のパス。

    Returns:
        `[page 1]\\n...\\n\\n[page 2]\\n...` の形の文字列。
    """
    return join_pages(page_texts(pdf_path))


def download_pdf(url: str, out_path: Path) -> Path:
    """URL の PDF を `out_path` に 1 回だけ保存する（既存ファイルがあれば取りに行かない）。

    Raises:
        RuntimeError: HTTP エラー・応答が PDF でない。
    """
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} for {url}") from None
    if body[:5] != b"%PDF-":
        raise RuntimeError(f"not a PDF: {url} (head={body[:20]!r})")
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    tmp_path.write_bytes(body)
    tmp_path.replace(out_path)
    return out_path


def convert(pdf_path: Path) -> tuple[Path, int, int, int]:
    """PDF → 隣の同名 `.txt`。(txt_path, pages, chars, failed_pages) を返す。

    chars は本文の字数（`[page N]` 見出しを除く）・failed_pages は本文が 0 字のページ数。
    全ページ失敗（chars=0）でも .txt は書く（見出しだけ・何が起きたか残すため）。
    """
    texts = page_texts(pdf_path)
    txt_path = pdf_path.with_suffix(".txt")
    txt_path.write_text(join_pages(texts), encoding="utf-8")
    body_chars = sum(len(t) for t in texts)
    failed_pages = sum(1 for t in texts if not t)
    return txt_path, len(texts), body_chars, failed_pages


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="決算説明会 PDF → テキスト（P-INF-34 T3）")
    parser.add_argument("--pdf", type=Path, help="テキスト化する PDF のパス")
    parser.add_argument("--url", help="先に保存する PDF の URL（--out 必須）")
    parser.add_argument("--out", type=Path, help="--url の保存先パス")
    args = parser.parse_args(argv)

    pdf_path: Optional[Path] = args.pdf
    if args.url:
        if not args.out:
            parser.error("--url には --out が要る")
        pdf_path = download_pdf(args.url, args.out)
        print(f"downloaded {pdf_path}")
    if pdf_path is None:
        parser.error("--pdf か --url のどちらかが要る")
    if not pdf_path.exists():
        print(f"not found: {pdf_path}", file=sys.stderr)
        return 1
    txt_path, pages, chars, failed_pages = convert(pdf_path)
    print(f"saved {txt_path} pages={pages} chars={chars} failed_pages={failed_pages}")
    if chars == 0:
        # 全ページ抽出失敗（画像だけの PDF・フォント不備）＝ 工程③に渡せる本文が無いので失敗として返す
        print(f"error: no text extracted from {pdf_path} (pages={pages} chars=0 failed_pages={failed_pages})",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
