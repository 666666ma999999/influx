"""
Cookie暗号化ユーティリティ
Fernet対称暗号化によるcookies.json保護
"""
import json
import os
import hashlib
import base64
from pathlib import Path

try:
    from cryptography.fernet import Fernet
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False


def _get_encryption_key() -> bytes:
    """
    暗号化キーを取得。
    環境変数 COOKIE_ENCRYPTION_KEY が設定されていれば使用。
    未設定の場合はマシン固有キーを生成。
    """
    env_key = os.environ.get("COOKIE_ENCRYPTION_KEY")
    if env_key:
        key_bytes = hashlib.sha256(env_key.encode()).digest()
        return base64.urlsafe_b64encode(key_bytes)

    # フォールバック: マシン固有のキー生成
    import socket
    machine_id = f"{os.getlogin()}@{socket.gethostname()}"
    key_bytes = hashlib.sha256(machine_id.encode()).digest()
    return base64.urlsafe_b64encode(key_bytes)


def save_cookies_encrypted(cookies: list, cookie_file: Path):
    """Cookieを暗号化して保存"""
    cookie_file = Path(cookie_file)

    if not HAS_CRYPTOGRAPHY:
        # cryptography未インストール時はプレーンテキスト保存（パーミッションは設定）
        with open(cookie_file, "w") as f:
            json.dump(cookies, f, indent=2)
        os.chmod(cookie_file, 0o600)
        return

    fernet = Fernet(_get_encryption_key())
    plaintext = json.dumps(cookies).encode("utf-8")
    encrypted = fernet.encrypt(plaintext)

    with open(cookie_file, "wb") as f:
        f.write(encrypted)
    os.chmod(cookie_file, 0o600)


def load_cookies_encrypted(cookie_file: Path) -> list:
    """暗号化されたCookieを復号して読み込む"""
    cookie_file = Path(cookie_file)

    if not cookie_file.exists():
        return []

    if not HAS_CRYPTOGRAPHY:
        # cryptography未インストール時はプレーンテキスト読み込み
        with open(cookie_file, "r") as f:
            return json.load(f)

    fernet = Fernet(_get_encryption_key())
    with open(cookie_file, "rb") as f:
        data = f.read()

    try:
        plaintext = fernet.decrypt(data)
        return json.loads(plaintext.decode("utf-8"))
    except Exception:
        # 復号失敗時: プレーンテキストとして読み込みを試みる（移行期対応）
        try:
            with open(cookie_file, "r") as f:
                return json.load(f)
        except Exception:
            return []
