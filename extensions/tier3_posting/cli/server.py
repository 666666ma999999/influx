#!/usr/bin/env python3
"""X Post Review管理画面用APIサーバー。

PostStoreを直接操作するREST APIを提供し、review.htmlからfetch()で呼び出す。
"""
import argparse
import base64
import glob as globmod
import hashlib
import json
import os
import re
import shutil
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from ..x_poster.post_store import PostStore
from ..account_routing import resolve_account, get_account_list, ACCOUNTS
from ..services.view_model import enrich_draft_for_ui, build_meta, build_cli_commands
from ..services.draft_service import build_draft
from ..services.style_prompt_builder import build_rewrite_prompt
from ..services.rewrite_store import record_rewrite

UI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")


def encode_images_base64(drafts: list, allowed_dirs: list = None) -> list:
    """ドラフトの画像ファイルをBase64エンコードしてインライン化。

    build_review_page.py の encode_images_base64 と同じロジック。

    Args:
        drafts: ドラフトのリスト
        allowed_dirs: 許可ディレクトリのリスト（指定時はパス検証を実施）

    Returns:
        images_base64 フィールドを追加したドラフトのリスト
    """
    for draft in drafts:
        images = draft.get("images", [])
        images_base64 = []
        for img in images:
            img_path = img.get("path", "")
            # パス検証: 許可ディレクトリ配下かチェック
            if allowed_dirs:
                real_path = os.path.realpath(img_path)
                if not any(real_path.startswith(os.path.realpath(d)) for d in allowed_dirs):
                    continue
            if os.path.exists(img_path):
                try:
                    with open(img_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("utf-8")
                    ext = os.path.splitext(img_path)[1].lower()
                    mime = {
                        "png": "image/png",
                        "jpg": "image/jpeg",
                        "jpeg": "image/jpeg",
                    }.get(ext.lstrip("."), "image/png")
                    images_base64.append({
                        "data": f"data:{mime};base64,{b64}",
                        "type": img.get("type", ""),
                        "description": img.get("description", ""),
                    })
                except Exception as e:
                    print(f"  画像エンコードエラー ({img_path}): {e}")
        if images_base64:
            draft["images_base64"] = images_base64
    return drafts


_ARTICLE_BODY_MD_IMG = re.compile(
    r"""!\[([^\]]*)\]\(\s*([^\s)]+)(\s+["'][^"']*["'])?\s*\)"""
)
_ARTICLE_BODY_HTML_IMG = re.compile(
    r"""<img\b([^>]*?)\bsrc=(['"])([^'"]+)\2""", re.IGNORECASE
)
_ARTICLE_BODY_ABSOLUTE_URL = re.compile(r"^(?:[a-z][a-z0-9+\-.]*:|/|#)", re.IGNORECASE)


def _rewrite_relative_image_url(url: str) -> str:
    """相対パスの画像 URL のみ /api/images/<basename> に変換。絶対 URL（scheme:, /, #）はそのまま。"""
    url = url.strip()
    if _ARTICLE_BODY_ABSOLUTE_URL.match(url):
        return url
    basename = os.path.basename(url)
    if not basename:
        return url
    return f"/api/images/{basename}"


def normalize_article_body_image_urls(draft: dict) -> None:
    """make_article 由来の article_body 内の相対パス画像参照を canonical URL に変換する。

    保存データは変更せず、API 返却用 draft の `metadata.article_body` のみを書き換える。
    make_article の markdown は `![alt](../images/foo.png)` 形式の相対パスを含み、
    review UI でレンダリングすると `http://host/images/foo.png` に解決されて 404 になる。
    `/api/images/<basename>` に書き換えることで既存 `_serve_image` 経路から配信可能になる。
    """
    if draft.get("template_type") != "make_article":
        return
    metadata = draft.get("metadata") or {}
    raw = metadata.get("article_body")
    if not raw:
        return

    def repl_md(match: "re.Match[str]") -> str:
        alt = match.group(1)
        new_url = _rewrite_relative_image_url(match.group(2))
        title = match.group(3) or ""
        return f"![{alt}]({new_url}{title})"

    def repl_html(match: "re.Match[str]") -> str:
        attrs = match.group(1)
        quote = match.group(2)
        new_url = _rewrite_relative_image_url(match.group(3))
        return f"<img{attrs}src={quote}{new_url}{quote}"

    normalized = _ARTICLE_BODY_MD_IMG.sub(repl_md, raw)
    normalized = _ARTICLE_BODY_HTML_IMG.sub(repl_html, normalized)
    if normalized != raw:
        metadata["article_body"] = normalized
        draft["metadata"] = metadata


class ReviewHandler(SimpleHTTPRequestHandler):
    """review.html配信 + PostStore操作REST APIハンドラ。"""

    store: PostStore = None  # クラス変数として設定
    base_dir: str = ""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._serve_html()
        elif path == "/api/drafts":
            self._api_get_drafts()
        elif path == "/api/meta":
            meta = build_meta()
            meta["cli_commands"] = build_cli_commands(self.store.get_all_with_history())
            self._json_response(meta)
        elif path == "/api/accounts":
            self._json_response(get_account_list())
        elif path == "/api/bookmarks":
            self._api_get_bookmarks()
        elif path == "/api/images":
            self._api_list_images()
        elif path.startswith("/api/screenshots/"):
            filename = path.split("/api/screenshots/", 1)[1]
            self._serve_screenshot(filename)
        elif path.startswith("/api/images/"):
            filename = path.split("/api/images/", 1)[1]
            self._serve_image(filename)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()

        # /api/drafts/create (new post)
        if path == "/api/drafts/create":
            self._api_create_draft(body)
        # /api/drafts/bulk-status
        elif path == "/api/drafts/bulk-status":
            self._api_bulk_status(body)
        # /api/drafts/compact
        elif path == "/api/drafts/compact":
            self._api_compact()
        # /api/drafts/<id>/status
        elif re.match(r"^/api/drafts/[a-f0-9]+/status$", path):
            news_id = path.split("/")[3]
            self._api_update_status(news_id, body)
        # /api/drafts/<id>/edit
        elif re.match(r"^/api/drafts/[a-f0-9]+/edit$", path):
            news_id = path.split("/")[3]
            self._api_edit_draft(news_id, body)
        # /api/drafts/<id>/rewrite/accept (must be before /rewrite)
        elif re.match(r"^/api/drafts/[a-f0-9]+/rewrite/accept$", path):
            news_id = path.split("/")[3]
            self._api_accept_rewrite(news_id, body)
        # /api/drafts/<id>/rewrite
        elif re.match(r"^/api/drafts/[a-f0-9]+/rewrite$", path):
            news_id = path.split("/")[3]
            self._api_rewrite_draft(news_id, body)
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        """CORSプリフライト対応（ローカル開発用）。"""
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    # ─── Internal helpers ─────────────────────────────────

    def _read_body(self) -> dict:
        """リクエストボディをJSONとして読み込む。"""
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def _json_response(self, data, status=200):
        """JSON レスポンスを返す。"""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self):
        """CORS ヘッダーを追加する。"""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    # ─── GET endpoints ────────────────────────────────────

    def _serve_html(self):
        """GET / — review.html を配信する。"""
        html_path = os.path.join(UI_DIR, "review.html")
        if not os.path.exists(html_path):
            self.send_error(404, f"review.html not found: {html_path}")
            return

        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()

        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _api_get_drafts(self):
        """GET /api/drafts — 全ドラフト取得（履歴・インプレッション付き）。"""
        try:
            drafts = self.store.get_all_with_history()

            # 画像のBase64エンコード（output/配下のみ許可）
            drafts = encode_images_base64(drafts, allowed_dirs=["output/"])

            for draft in drafts:
                # screenshot_paths をファイル名のみに変換
                paths = draft.get("screenshot_paths", [])
                draft["screenshot_paths"] = [os.path.basename(p) for p in paths]
                # article_body 内の相対パス画像 URL を canonical 化
                normalize_article_body_image_urls(draft)
                # UI用ビューモデル変換（account_id, char_remaining, actions等）
                enrich_draft_for_ui(draft)

            self._json_response(drafts)
        except Exception as e:
            self._json_response({"ok": False, "error": str(e)}, status=500)

    def _api_get_bookmarks(self):
        """GET /api/bookmarks — ブックマークJSONL読み込み（plan.md M1 T1.3 でパス修正）。"""
        try:
            from pathlib import Path
            from ..services.style_prompt_builder import _read_jsonl

            # 正規パス: data/writing_style/bookmarks/normalized.jsonl（教師データ169件）
            # cli/server.py → tier3_posting/ → extensions/ → プロジェクトルート (parents[3])
            project_root = Path(__file__).resolve().parents[3]
            primary = project_root / "data" / "writing_style" / "bookmarks" / "normalized.jsonl"
            if primary.exists():
                self._json_response(_read_jsonl(primary))
                return

            # フォールバック: 旧 output/ 配下（収集後の整形前データ）
            output_dir = Path(self.base_dir).parent  # output/
            for fallback in ("bookmarks.jsonl", "bookmarks_test.jsonl"):
                candidate = output_dir / fallback
                if candidate.exists():
                    self._json_response(_read_jsonl(candidate))
                    return

            self._json_response([])
        except Exception as e:
            self._json_response({"ok": False, "error": str(e)}, status=500)

    def _serve_screenshot(self, filename: str):
        """GET /api/screenshots/<filename> — スクリーンショットPNG配信。"""
        # パストラバーサル対策
        if ".." in filename or "/" in filename or "\\" in filename:
            self.send_error(400, "Invalid filename")
            return

        filepath = os.path.join(self.base_dir, filename)
        if not os.path.exists(filepath):
            self.send_error(404, f"Screenshot not found: {filename}")
            return

        try:
            with open(filepath, "rb") as f:
                data = f.read()

            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, f"Error reading screenshot: {e}")

    # ─── POST endpoints ───────────────────────────────────

    def _api_update_status(self, news_id: str, body: dict):
        """POST /api/drafts/<news_id>/status — ステータス更新。"""
        try:
            status = body.get("status")
            if not status:
                self._json_response(
                    {"ok": False, "error": "status is required"}, status=400
                )
                return

            extra = {}
            if body.get("reason"):
                extra["archived_reason"] = body["reason"]
            if body.get("scheduled_at"):
                extra["scheduled_at"] = body["scheduled_at"]
            if body.get("account_id"):
                extra["account_id"] = body["account_id"]

            ok = self.store.update_draft_status(news_id, status, **extra)

            if ok:
                self._json_response({"ok": True})
            else:
                self._json_response(
                    {"ok": False, "error": f"news_id not found: {news_id}"},
                    status=404,
                )
        except Exception as e:
            self._json_response({"ok": False, "error": str(e)}, status=500)

    def _find_draft(self, news_id: str) -> dict:
        """news_idでドラフトを検索する。見つからない場合はNone。"""
        for d in self.store.load_drafts():
            if d.get("news_id") == news_id:
                return d
        return None

    def _api_edit_draft(self, news_id: str, body: dict):
        """POST /api/drafts/<news_id>/edit — ドラフト編集。"""
        try:
            current_draft = self._find_draft(news_id)

            if current_draft is None:
                self._json_response(
                    {"ok": False, "error": f"news_id not found: {news_id}"},
                    status=404,
                )
                return

            current_status = current_draft.get("status", "draft")

            # rejected の場合は draft に戻す（review.html と同じ挙動）
            if current_status == "rejected":
                current_status = "draft"

            # 編集フィールドを extra として渡す
            fields = {}
            for key in ("title", "body", "hashtags", "scheduled_at", "account_id", "correction_instructions"):
                if key in body:
                    fields[key] = body[key]

            ok = self.store.update_draft_status(news_id, current_status, **fields)
            if ok:
                self._json_response({"ok": True})
            else:
                self._json_response(
                    {"ok": False, "error": f"update failed: {news_id}"},
                    status=500,
                )
        except Exception as e:
            self._json_response({"ok": False, "error": str(e)}, status=500)

    def _api_bulk_status(self, body: dict):
        """POST /api/drafts/bulk-status — 一括ステータス更新。"""
        try:
            news_ids = body.get("news_ids", [])
            status = body.get("status")
            if not status or not news_ids:
                self._json_response(
                    {"ok": False, "error": "news_ids and status are required"},
                    status=400,
                )
                return

            extra = {}
            reason = body.get("reason")
            if reason:
                extra["archived_reason"] = reason

            count = self.store.bulk_update_status(news_ids, status, **extra)
            self._json_response({"ok": True, "count": count})
        except Exception as e:
            self._json_response({"ok": False, "error": str(e)}, status=500)

    def _api_compact(self):
        """POST /api/drafts/compact — drafts.jsonl 圧縮。"""
        try:
            if not os.path.exists(self.store.drafts_path):
                self._json_response(
                    {"ok": False, "error": "drafts.jsonl not found"}, status=404
                )
                return

            # 圧縮前の行数
            with open(self.store.drafts_path, "r", encoding="utf-8") as f:
                before_lines = sum(1 for line in f if line.strip())

            # マージ済みの最終状態を取得
            drafts = self.store.load_drafts()

            # バックアップ
            bak_path = self.store.drafts_path + ".bak"
            shutil.copy2(self.store.drafts_path, bak_path)

            # 新しいJSONLに書き直し
            with open(self.store.drafts_path, "w", encoding="utf-8") as f:
                for d in drafts:
                    d.pop("_update", None)
                    f.write(json.dumps(d, ensure_ascii=False) + "\n")

            after_lines = len(drafts)

            # index.json 再生成
            self.store._index = None
            self.store._load_index()
            self.store._save_index()

            self._json_response({
                "ok": True,
                "before": before_lines,
                "after": after_lines,
            })
        except Exception as e:
            self._json_response({"ok": False, "error": str(e)}, status=500)

    def _api_create_draft(self, body: dict):
        """POST /api/drafts/create — 新規ドラフト作成。"""
        try:
            title = body.get("title", "").strip()
            text = body.get("body", "")
            if not text:
                self._json_response(
                    {"ok": False, "error": "body is required"}, status=400
                )
                return

            # 画像パスバリデーション（output/配下のみ許可）
            validated_images = None
            if body.get("images"):
                validated_images = []
                for img in body["images"]:
                    img_path = img.get("path", "")
                    real_path = os.path.realpath(img_path)
                    if real_path.startswith(os.path.realpath("output/")):
                        validated_images.append(img)

            # draft_service でスキーマ統一
            draft = build_draft(
                body=text,
                title=title,
                hashtags=body.get("hashtags", []),
                template_type=body.get("template_type", "manual"),
                scheduled_at=body.get("scheduled_at"),
                images=validated_images,
            )

            # account_id（手動指定 or 自動ルーティング）
            draft["account_id"] = body.get("account_id") or resolve_account(draft)

            ok = self.store.add_draft(draft)
            if ok:
                self._json_response({"ok": True, "news_id": draft["news_id"]})
            else:
                self._json_response(
                    {"ok": False, "error": "duplicate news_id"}, status=409
                )
        except Exception as e:
            self._json_response({"ok": False, "error": str(e)}, status=500)

    def _api_list_images(self):
        """GET /api/images — 投稿用画像の一覧を返す。"""
        try:
            output_dir = os.path.dirname(self.base_dir)  # output/
            image_dirs = [
                output_dir,  # output/*.png
                os.path.join(self.base_dir, "images"),  # output/posting/images/
            ]
            images = []
            for d in image_dirs:
                if not os.path.isdir(d):
                    continue
                for ext in ("*.png", "*.jpg", "*.jpeg"):
                    for path in globmod.glob(os.path.join(d, ext)):
                        basename = os.path.basename(path)
                        # debug/ や schedule_error は除外
                        if "debug" in path or "schedule_error" in basename or "schedule_dryrun" in basename:
                            continue
                        # Base64 サムネイル生成
                        try:
                            with open(path, "rb") as f:
                                b64 = base64.b64encode(f.read()).decode("utf-8")
                            rel = os.path.relpath(path, output_dir)
                            images.append({
                                "path": path,
                                "name": basename,
                                "rel_path": rel,
                                "data": f"data:image/png;base64,{b64}",
                            })
                        except Exception:
                            continue

            self._json_response(images)
        except Exception as e:
            self._json_response({"ok": False, "error": str(e)}, status=500)

    def _serve_image(self, filename: str):
        """GET /api/images/<filename> — 画像ファイル配信。"""
        if ".." in filename or "/" in filename or "\\" in filename:
            self.send_error(400, "Invalid filename")
            return
        # output/ 配下を検索
        output_dir = os.path.dirname(self.base_dir)
        candidates = [
            os.path.join(output_dir, filename),
            os.path.join(self.base_dir, "images", filename),
        ]
        filepath = None
        for c in candidates:
            if os.path.exists(c):
                filepath = c
                break
        if not filepath:
            self.send_error(404, f"Image not found: {filename}")
            return
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            ext = os.path.splitext(filename)[1].lower()
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(ext, "image/png")
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_error(500, f"Error: {e}")

    @staticmethod
    def _call_llm(prompt: str) -> str:
        """claude CLIを呼び出してテキストを返す（APIキー不要）。"""
        import subprocess

        try:
            result = subprocess.run(
                ["claude", "-p", prompt],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                print(f"WARNING: claude CLI failed (rc={result.returncode}): {result.stderr[:200]}")
                return ""
            return result.stdout.strip()
        except FileNotFoundError:
            print("WARNING: claude CLI not found in PATH")
            return ""
        except subprocess.TimeoutExpired:
            print("WARNING: claude CLI timed out (120s)")
            return ""
        except Exception as e:
            print(f"WARNING: claude CLI call failed: {e}")
            return ""

    def _api_rewrite_draft(self, news_id: str, body: dict):
        """POST /api/drafts/<id>/rewrite — LLMでドラフト本文をリライト。"""
        try:
            instruction = body.get("instruction", "").strip()
            if not instruction:
                self._json_response(
                    {"ok": False, "error": "instruction is required"}, status=400
                )
                return

            draft = self._find_draft(news_id)
            if draft is None:
                self._json_response(
                    {"ok": False, "error": f"news_id not found: {news_id}"},
                    status=404,
                )
                return

            original_body = draft.get("body", "")
            account_id = draft.get("account_id", "")

            # プロンプト構築 + LLM呼び出し
            prompt = build_rewrite_prompt(original_body, instruction, account_id)
            rewritten = self._call_llm(prompt)

            if not rewritten:
                self._json_response(
                    {"ok": False, "error": "LLM呼び出しに失敗しました（APIキー未設定または通信エラー）"},
                    status=500,
                )
                return

            self._json_response({
                "ok": True,
                "body": rewritten,
                "original_body": original_body,
            })
        except Exception as e:
            self._json_response({"ok": False, "error": str(e)}, status=500)

    def _api_accept_rewrite(self, news_id: str, body: dict):
        """POST /api/drafts/<id>/rewrite/accept — 承認されたリライトペアを学習データとして保存。"""
        try:
            instruction = body.get("instruction", "").strip()
            original_body = body.get("original_body", "").strip()
            rewritten_body = body.get("rewritten_body", "").strip()

            if not instruction or not rewritten_body:
                self._json_response(
                    {"ok": False, "error": "instruction and rewritten_body are required"},
                    status=400,
                )
                return

            draft = self._find_draft(news_id)
            if draft is None:
                self._json_response(
                    {"ok": False, "error": f"news_id not found: {news_id}"},
                    status=404,
                )
                return

            account_id = draft.get("account_id") or resolve_account(draft) or "unknown"
            draft_metadata = {
                "template_type": draft.get("template_type", ""),
                "char_len_before": len(original_body),
                "char_len_after": len(rewritten_body),
            }

            rewrite_id = record_rewrite(
                account_id=account_id,
                news_id=news_id,
                instruction=instruction,
                original_body=original_body,
                rewritten_body=rewritten_body,
                draft_metadata=draft_metadata,
            )

            self._json_response({"ok": True, "rewrite_id": rewrite_id})
        except Exception as e:
            self._json_response({"ok": False, "error": str(e)}, status=500)


def main():
    parser = argparse.ArgumentParser(description="X Post Review APIサーバー")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data-dir", default="output/posting")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    store = PostStore(base_dir=args.data_dir)
    ReviewHandler.store = store
    ReviewHandler.base_dir = args.data_dir

    server = HTTPServer((args.host, args.port), ReviewHandler)
    print(f"Review server: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
