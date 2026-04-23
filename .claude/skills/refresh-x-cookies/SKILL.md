---
name: refresh-x-cookies
description: influxプロジェクトのX(Twitter)Cookieを macOS Chrome プロファイルから抽出して暗号化保存する。X の bot 検知強化により VNC Playwright 経路は廃止（2026-04-21）。Chrome に X ログイン済みなら 1 コマンドで完結。
trigger_keywords: Cookie更新, Cookie再取得, refresh cookies, x_profiles, COOKIE_ENCRYPTION_KEY, influx cookie
---

# X Cookie 再取得（Chrome Cookie 抽出経路、唯一の確実手段）

影響アカウント: `@kabuki666999`, `@maaaki`

**禁止**: VNC Playwright 経由のログイン（X の bot 検知で「次へ」後にフォームクリア発生、2026-04-21 検証で確認）。対応スクリプト `refresh_cookies_vnc.py` / `setup_profile.py` / `setup_from_chrome.py` は削除済み。

---

## 前提条件

1. 普段使い macOS Chrome で対象 X アカウントにログイン済み（x.com/home がホームタイムラインを表示）
2. `security find-generic-password -s "Chrome Safe Storage"` で Keychain エントリあり
3. Homebrew openssl 利用可（`/opt/homebrew/bin/openssl`、`openssl version` で動作確認）
4. Chrome 128+ 環境（SHA256(host) プレフィックスで暗号化されている）

---

## STEP 1: Chrome プロファイルの特定

```bash
# プロファイル一覧（ユーザー名 / Google メールから推定）
python3 -c "
import json
d=json.load(open('/Users/masaaki_nagasawa/Library/Application Support/Google/Chrome/Local State'))['profile']['info_cache']
[print(f'{k:15s} {v.get(\"name\",\"?\"):20s} {v.get(\"user_name\",\"?\")}') for k,v in d.items()]"

# 各プロファイルの X 認証 Cookie 有無（Chrome 起動中でも読める）
for p in "Default" "Profile 1" "Profile 2" "Profile 3"; do
  cp "/Users/masaaki_nagasawa/Library/Application Support/Google/Chrome/$p/Cookies" /tmp/ck.db 2>/dev/null || continue
  python3 -c "
import sqlite3
con=sqlite3.connect('/tmp/ck.db')
r=con.execute(\"SELECT name FROM cookies WHERE (host_key LIKE '%.twitter.com' OR host_key LIKE '%.x.com' OR host_key='twitter.com' OR host_key='x.com') AND name IN ('auth_token','ct0','twid','kdt')\").fetchall()
print(f'[$p] auth: {sorted(set(x[0] for x in r))}')"
done
```

`auth_token, ct0, kdt, twid` 4 種揃っているプロファイルが抽出対象。

---

## STEP 2: 抽出 + 保存

```bash
cd /Users/masaaki_nagasawa/Desktop/biz/influx
python3 scripts/import_chrome_cookies.py --chrome-profile "Profile 2" --account kabuki666999
python3 scripts/import_chrome_cookies.py --chrome-profile "Default"   --account maaaki
```

初回実行時に **macOS が Keychain 承認ダイアログ**を出す（Chrome Safe Storage へのアクセス許可）。以降は許可済みで自動。

---

## STEP 3: 検証

```bash
# ファイル状態
ls -la /Users/masaaki_nagasawa/Desktop/biz/influx/x_profiles/*/cookies.json

# Docker 側で認証テスト
docker exec xstock-vnc python -c "
import sys, time
sys.path.insert(0, '/app')
from collector.cookie_crypto import load_cookies_encrypted
from pathlib import Path
from playwright.sync_api import sync_playwright

for acc in ('kabuki666999', 'maaaki'):
    cookies = load_cookies_encrypted(Path(f'/app/x_profiles/{acc}/cookies.json'))
    norm = []
    for c in cookies:
        cc = dict(c)
        if cc.get('expires') is None or cc['expires'] < 0:
            cc.pop('expires', None)
        if cc.get('sameSite') not in ('Strict', 'Lax', 'None'):
            cc['sameSite'] = 'Lax'
        norm.append(cc)
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={'width':1280,'height':900}, locale='ja-JP')
        ctx.add_cookies(norm)
        page = ctx.new_page()
        page.goto('https://x.com/home', wait_until='domcontentloaded', timeout=30000)
        time.sleep(3)
        try:
            page.wait_for_selector('[data-testid=\"AppTabBar_Home_Link\"]', timeout=10000)
            print(f'{acc}: LOGIN OK')
        except Exception as e:
            print(f'{acc}: FAIL -> {str(e)[:80]}')
        b.close()
"
```

`@kabuki666999: LOGIN OK` と `@maaaki: LOGIN OK` の両方が出れば完了。

---

## トラブルシュート

| 症状 | 原因 | 対処 |
|---|---|---|
| `Keychain 取得失敗` | macOS の承認ダイアログでキャンセル | 再実行して「常に許可」選択 |
| `auth 系 Cookie なし` 警告 | Chrome プロファイルが X 未ログイン | Chrome でそのプロファイルを開き X にログインしてから再実行 |
| Docker 側検証で `FAIL` | Cookie 復号が失敗、または X セッション自体が無効 | (1) 暗号化形式確認 `file x_profiles/*/cookies.json` (2) Chrome 側で x.com/home が表示されるか確認 |
| `Cookies DB not found` | Chrome プロファイル名の誤り | Local State の `info_cache` キーを再確認 |

---

## 重要な技術ポイント

- **Chrome 128+ の SHA256(host) プレフィックス**: 暗号値の先頭 32 バイトがハッシュ（ドメイン入れ替え検知）。`import_chrome_cookies.py` は strip 済み
- **厳密ドメインフィルタ**: LIKE `%x.com` は `dropbox.com` 等にも誤マッチ。`import_chrome_cookies.py` は host_key の完全ドメイン一致+サブドメイン一致で厳密化済み
- **cryptography 依存なし**: stdlib `hashlib.pbkdf2_hmac` + `openssl` subprocess で復号。Docker-only ルール準拠
- **保存形式**: 平文 JSON（Docker 側 `cookie_crypto.load_cookies_encrypted` が Fernet 復号失敗時に平文 JSON フォールバック）
- **openssl `-K` のセキュリティ注意**: AES キー 16 バイトが一時的にプロセステーブルに載る。共有 Mac では注意

---

## 成功実績

- 2026-04-21: 両アカウント（Profile 2 → kabuki666999 17 cookies / Default → maaaki 19 cookies）で取得成功、Docker 側 x.com/home 認証 OK

## 関連ファイル

- `scripts/import_chrome_cookies.py` — Canonical 抽出スクリプト
- `collector/cookie_crypto.py` — `load_cookies_encrypted` / `save_cookies_encrypted`
- `x_profiles/<account>/cookies.json` — 保存先
- `tasks/lessons.md` — 過去の Cookie 関連学び
