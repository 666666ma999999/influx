#!/usr/bin/env python3
"""Chrome プロファイルから X(Twitter) Cookie を抽出して influx 暗号化形式で保存。

Usage:
    python scripts/import_chrome_cookies.py --chrome-profile "Profile 2" --account kabuki666999
    python scripts/import_chrome_cookies.py --chrome-profile "Default" --account maaaki

macOS 専用（Chrome v10 暗号, Safe Storage Keychain）。初回実行時に Keychain 承認ダイアログが出る。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def get_chrome_password() -> bytes:
    """macOS Keychain から Chrome Safe Storage パスワードを取得。"""
    result = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage", "-a", "Chrome"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Keychain 取得失敗: {result.stderr.strip()}")
    return result.stdout.strip().encode()


def derive_key(password: bytes) -> bytes:
    """PBKDF2-HMAC-SHA1(password, 'saltysalt', 1003, 16) で AES-128 キー導出（stdlib のみ）。"""
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)


def decrypt_v10(encrypted: bytes, key_hex: str, strip_host_prefix: bool) -> str:
    """Chrome v10 形式の暗号化値を openssl 経由で復号（AES-128-CBC、IV=space*16）。
    Chrome 128+ は SHA256(host) の 32 バイトプレフィックスを plaintext 先頭に付けるので、
    新しい Chrome で書き込まれた Cookie は strip_host_prefix=True で先頭 32 バイトを剥がす。"""
    if not encrypted.startswith(b"v10"):
        raise ValueError(f"unexpected prefix: {encrypted[:3]!r}")
    ciphertext = encrypted[3:]
    iv_hex = "20" * 16
    result = subprocess.run(
        ["openssl", "enc", "-aes-128-cbc", "-d", "-K", key_hex, "-iv", iv_hex],
        input=ciphertext,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"openssl decrypt failed: {result.stderr.decode(errors='replace')}")
    plaintext = result.stdout
    if strip_host_prefix and len(plaintext) >= 32:
        plaintext = plaintext[32:]
    return plaintext.decode("utf-8", errors="replace")


def save_cookies_json(cookies: list, cookie_file: Path) -> None:
    """Fernet 暗号化は cryptography 依存のため、Docker 側で再暗号化する前提で平文 JSON 保存。
    cookie_crypto.load_cookies_encrypted は復号失敗時に平文 JSON へフォールバックするため互換。
    race condition 回避のため O_CREAT|O_EXCL パーミッション 0o600 で開く。"""
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    # 既存ファイルを消してから 0o600 モードで作成
    try:
        cookie_file.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(str(cookie_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)


def chrome_time_to_unix(chrome_us: int) -> float:
    """Chrome の expires_utc（1601-01-01 基点マイクロ秒）を Unix 秒へ変換。永続/削除時は -1。"""
    if chrome_us == 0:
        return -1
    # 1601-01-01 → 1970-01-01 = 11644473600 秒
    return chrome_us / 1_000_000 - 11_644_473_600


_SAMESITE_MAP = {0: "None", 1: "Lax", 2: "Strict"}


def samesite_chrome_to_playwright(value: int) -> str:
    """Chrome の samesite（0=None, 1=Lax, 2=Strict, -1=Unspecified）→ Playwright 形式。
    -1 は仕様上 Lax 相当（RFC 6265bis デフォルト）。未知値は警告出力して Lax フォールバック。"""
    if value in _SAMESITE_MAP:
        return _SAMESITE_MAP[value]
    if value != -1:
        print(f"WARN: unknown samesite value {value}, falling back to Lax", file=sys.stderr)
    return "Lax"


def _host_matches(host_key: str, domains: list[str]) -> bool:
    """host_key が指定ドメインのルートまたはサブドメインに一致するか。
    host_key はリーディングドットを含むことがある（'.x.com' や '.www.x.com'）。"""
    h = host_key.lstrip(".").lower()
    return any(h == d or h.endswith("." + d) for d in domains)


def extract_cookies(chrome_profile: str, domains: list[str], strip_host_prefix: bool = True) -> list[dict]:
    """Chrome プロファイルから指定ドメインの Cookie を抽出して Playwright 形式で返す。"""
    profile_dir = Path.home() / "Library/Application Support/Google/Chrome" / chrome_profile
    src_db = profile_dir / "Cookies"
    if not src_db.exists():
        raise FileNotFoundError(f"Cookies DB not found: {src_db}")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    shutil.copy(src_db, tmp_path)

    password = get_chrome_password()
    key_hex = derive_key(password).hex()

    con = sqlite3.connect(tmp_path)
    rows = con.execute(
        "SELECT host_key, name, encrypted_value, path, expires_utc, is_secure, is_httponly, samesite "
        "FROM cookies"
    ).fetchall()
    con.close()
    tmp_path.unlink(missing_ok=True)

    cookies = []
    for host_key, name, enc, path, expires_utc, is_secure, is_httponly, samesite in rows:
        if not _host_matches(host_key, domains):
            continue
        try:
            value = decrypt_v10(enc, key_hex, strip_host_prefix) if enc else ""
        except Exception as e:
            print(f"WARN: decrypt failed for {name}@{host_key}: {e}", file=sys.stderr)
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": host_key,
                "path": path,
                "expires": chrome_time_to_unix(expires_utc),
                "httpOnly": bool(is_httponly),
                "secure": bool(is_secure),
                "sameSite": samesite_chrome_to_playwright(samesite),
            }
        )
    return cookies


def main() -> int:
    parser = argparse.ArgumentParser(description="Chrome から X Cookie を抽出して暗号化保存")
    parser.add_argument("--chrome-profile", required=True, help='Chrome プロファイル名（"Default", "Profile 2" 等）')
    parser.add_argument("--account", required=True, help="influx 側アカウント ID（x_profiles/<account>/cookies.json に保存）")
    parser.add_argument("--domains", default="twitter.com,x.com", help="抽出対象ドメイン（カンマ区切り、ルート＋サブドメイン完全一致）")
    args = parser.parse_args()

    domains = [d.strip().lower() for d in args.domains.split(",") if d.strip()]
    cookies = extract_cookies(args.chrome_profile, domains)
    if not cookies:
        print(f"[{args.chrome_profile}] 対象 Cookie なし（ホスト一致なし）", file=sys.stderr)
        return 1

    auth_names = {"auth_token", "ct0", "twid", "kdt"}
    found_auth = sorted({c["name"] for c in cookies if c["name"] in auth_names})
    if not found_auth:
        print(f"WARN: auth 系 Cookie なし（ログイン状態ではない可能性）: names={sorted({c['name'] for c in cookies})}", file=sys.stderr)

    output = PROJECT_ROOT / "x_profiles" / args.account / "cookies.json"
    save_cookies_json(cookies, output)

    print(f"[{args.chrome_profile}] → {output}")
    print(f"Cookie 数: {len(cookies)}")
    print(f"auth 系: {found_auth}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
