#!/usr/bin/env python3
"""scripts/lib/xstock_vnc.sh（Docker 待機・コンテナ復旧の共通部品）の回帰テスト。

なぜ Python から shell を叩くか:
    この repo の検証は `python3 -m unittest discover -s tests` の1本しか無い。
    シェル用のテストランナーを新設すると「誰も回さないテスト」が増えるだけなので、
    既存のゲートにそのまま乗せる（2026-08-29・Codex レビューの回帰テスト提案への回答）。

Docker は起動しない:
    docker / osascript / open / sleep を PATH 先頭の stub に差し替えて挙動を固定する。
    stub の呼び出しは $CALL_LOG に追記され、テストはそのログを検査する。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib" / "xstock_vnc.sh"

# docker stub: 第1引数ごとに振る舞いを環境変数で切り替える
#   FAKE_PS_NAMES   … `docker ps --format` が返す行（改行区切り）
#   FAKE_INFO_RC    … `docker info` の終了コード
#   FAKE_EXEC_RC    … `docker exec … test -S …` の終了コード（DISPLAY 準備の可否）
DOCKER_STUB = """#!/bin/bash
echo "docker $*" >> "$CALL_LOG"
case "$1" in
  info) exit "${FAKE_INFO_RC:-0}" ;;
  ps)   printf '%s\\n' ${FAKE_PS_NAMES:-} ; exit 0 ;;
  exec) exit "${FAKE_EXEC_RC:-0}" ;;
  *)    exit 0 ;;
esac
"""

PASSTHRU_STUB = """#!/bin/bash
echo "{name} $*" >> "$CALL_LOG"
exit 0
"""


class XstockVncLibTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self._tmp.name) / "bin"
        self.bin.mkdir()
        self.call_log = Path(self._tmp.name) / "calls.log"
        self.call_log.touch()

        (self.bin / "docker").write_text(DOCKER_STUB, encoding="utf-8")
        for name in ("osascript", "open", "sleep"):
            (self.bin / name).write_text(PASSTHRU_STUB.format(name=name), encoding="utf-8")
        for f in self.bin.iterdir():
            f.chmod(0o755)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_sh(self, body: str, **env: str) -> subprocess.CompletedProcess:
        """lib を source した bash スニペットを stub 化された PATH で実行する。"""
        script = f'set -u\ncd "{REPO}"\n. "{LIB}"\n{body}\n'
        full_env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "CALL_LOG": str(self.call_log),
        }
        full_env.update(env)
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=full_env, timeout=60
        )

    def calls(self) -> str:
        return self.call_log.read_text(encoding="utf-8")

    # --- 設定の優先順位（2026-08-29 Codex 指摘: source 後の無条件代入で環境上書きが消えていた） ---

    def test_wait_precedence(self) -> None:
        """環境の XSTOCK_* > runner の MAX_WAIT_SEC/INTERVAL_SEC > lib 既定 の順で効く。"""
        # runner が source 前に行う代入をそのまま再現する
        preamble = (
            "MAX_WAIT_SEC=${MAX_WAIT_SEC:-300}\n"
            "INTERVAL_SEC=${INTERVAL_SEC:-30}\n"
            "XSTOCK_DAEMON_WAIT=${XSTOCK_DAEMON_WAIT:-$MAX_WAIT_SEC}\n"
            "XSTOCK_DAEMON_INTERVAL=${XSTOCK_DAEMON_INTERVAL:-$INTERVAL_SEC}\n"
        )
        cases = [
            ({}, "300 30"),
            ({"MAX_WAIT_SEC": "77"}, "77 30"),
            ({"XSTOCK_DAEMON_WAIT": "60"}, "60 30"),
            ({"MAX_WAIT_SEC": "77", "XSTOCK_DAEMON_WAIT": "60"}, "60 30"),
            ({"INTERVAL_SEC": "5"}, "300 5"),
        ]
        for env, expected in cases:
            with self.subTest(env=env):
                script = f'set -u\ncd "{REPO}"\n{preamble}. "{LIB}"\necho "$XSTOCK_DAEMON_WAIT $XSTOCK_DAEMON_INTERVAL"\n'
                full_env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                            "CALL_LOG": str(self.call_log), **env}
                res = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                     env=full_env, timeout=60)
                self.assertEqual(res.stdout.strip(), expected, res.stderr)

    # --- 待機値の検証（不正値で無限ループ・八進エラーにしない） ---

    def test_validate_waits_rejects_bad_values(self) -> None:
        bad = [
            ("XSTOCK_DAEMON_INTERVAL", "0"),      # 待機カウンタが増えず無限ループ
            ("XSTOCK_DAEMON_INTERVAL", "09"),     # 先頭ゼロ＝算術展開で八進エラー
            ("XSTOCK_DAEMON_WAIT", "08"),
            ("XSTOCK_DAEMON_WAIT", "abc"),
            ("XSTOCK_DAEMON_WAIT", "99999"),      # 上限超え
            ("XSTOCK_DISPLAY_WAIT", "abc"),       # 比較が毎回失敗して待機が終わらない
            ("XSTOCK_WARMUP", "-1"),
        ]
        for name, value in bad:
            with self.subTest(var=name, value=value):
                res = self.run_sh("xstock_validate_waits", **{name: value})
                self.assertEqual(res.returncode, 1, f"{name}={value} が受理された: {res.stdout}")
                self.assertIn("ERROR:", res.stderr)

    def test_validate_waits_accepts_defaults(self) -> None:
        self.assertEqual(self.run_sh("xstock_validate_waits").returncode, 0)

    # --- daemon 待機 ---

    def test_wait_daemon_ok_when_daemon_up(self) -> None:
        res = self.run_sh("xstock_wait_daemon", FAKE_INFO_RC="0")
        self.assertEqual(res.returncode, 0)
        self.assertNotIn("docker compose", self.calls())

    def test_wait_daemon_times_out_and_notifies(self) -> None:
        res = self.run_sh(
            "xstock_wait_daemon",
            FAKE_INFO_RC="1", XSTOCK_DAEMON_WAIT="2", XSTOCK_DAEMON_INTERVAL="1",
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("osascript", self.calls())  # 失敗は必ず通知する

    # --- ensure_ready ---

    def test_ensure_ready_returns_early_when_container_ready(self) -> None:
        """既に動いていて DISPLAY も準備済みなら、compose を叩かずすぐ返る。"""
        res = self.run_sh(
            'xstock_ensure_ready; echo "STARTED=$XSTOCK_STARTED"',
            FAKE_PS_NAMES="xstock-vnc", FAKE_EXEC_RC="0",
            XSTOCK_COMPOSE_CMD="echo COMPOSE_CALLED",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("STARTED=0", res.stdout)
        self.assertNotIn("COMPOSE_CALLED", res.stdout)

    def test_ensure_ready_fails_without_recreating_when_display_stuck(self) -> None:
        """稼働中なのに DISPLAY が来ない時に compose をやり直さない。

        作り直すと同じコンテナを使う別ジョブを巻き込んで落とすため（2026-08-29 Codex 2巡目）。
        """
        res = self.run_sh(
            "xstock_ensure_ready",
            FAKE_PS_NAMES="xstock-vnc", FAKE_EXEC_RC="1",
            XSTOCK_DISPLAY_WAIT="2", XSTOCK_COMPOSE_CMD="echo COMPOSE_CALLED",
        )
        self.assertEqual(res.returncode, 1)
        self.assertNotIn("COMPOSE_CALLED", res.stdout)
        self.assertIn("osascript", self.calls())

    def test_ensure_ready_gives_up_when_container_never_appears(self) -> None:
        res = self.run_sh(
            "xstock_ensure_ready",
            FAKE_PS_NAMES="", XSTOCK_UP_WAIT="2",
            XSTOCK_COMPOSE_CMD="true", XSTOCK_WARMUP="0",
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("osascript", self.calls())

    # --- 通知 ---

    def test_notify_strips_applescript_breaking_chars(self) -> None:
        """タイトル・本文の " と \\ を落とす（素で渡すと構文エラーで黙って消える）。"""
        res = self.run_sh(
            'xstock_notify "本文に \\" と \\\\ を含む"',
            XSTOCK_NOTIFY_TITLE='題"名\\',
        )
        self.assertEqual(res.returncode, 0)
        line = [l for l in self.calls().splitlines() if l.startswith("osascript")][0]
        self.assertNotIn('\\', line)
        self.assertEqual(line.count('"'), 4)  # 本文とタイトルを囲む2組だけ


class XpriceWatchRunnerTest(unittest.TestCase):
    """xprice_watch_run.sh の失敗通知（stderr だけで終わらせない）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self._tmp.name) / "bin"
        self.bin.mkdir()
        self.call_log = Path(self._tmp.name) / "calls.log"
        self.call_log.touch()
        (self.bin / "osascript").write_text(PASSTHRU_STUB.format(name="osascript"), encoding="utf-8")
        (self.bin / "sleep").write_text(PASSTHRU_STUB.format(name="sleep"), encoding="utf-8")
        for f in self.bin.iterdir():
            f.chmod(0o755)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_collect_failure_notifies(self) -> None:
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "CALL_LOG": str(self.call_log),
            "XSTOCK_SKIP_ENSURE": "1",   # Docker には触らない
            "COLLECT_CMD": "false",      # 収集を必ず失敗させる
            "ALERT_CMD": "echo stub-alert",
            "ALERTS_FILE": str(Path(self._tmp.name) / "none.jsonl"),
        }
        res = subprocess.run(
            ["bash", str(REPO / "scripts" / "xprice_watch_run.sh")],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("収集が2回失敗", res.stderr)
        self.assertIn("osascript", self.call_log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
