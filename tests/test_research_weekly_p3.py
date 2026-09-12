"""無人週次収集と取り込みの契約（実収集・LLM呼び出しは行わない）。"""
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import research_weekly, winrate_ingest

ROOT = Path(__file__).resolve().parents[1]


class TestWeeklyCollection(unittest.TestCase):
    def test_frozen_research_accounts_exactly_34(self):
        frozen = research_weekly._load_frozen_list(str(ROOT / research_weekly.FROZEN_LIST_PATH))
        candidates = research_weekly._to_candidates(frozen)
        self.assertEqual(len(candidates), 34)
        self.assertEqual({c['username'] for c in candidates}, set(frozen['research_accounts']))

    def test_contrarian_intersects_research_accounts(self):
        names = [f'u{i}' for i in range(34)]
        frozen = {'research_accounts': names, 'collection_groups': {
            'inverse': {'is_contrarian': True, 'accounts': [
                {'username': 'u0'}, {'username': 'outside'}]},
            'normal': {'accounts': [{'username': 'u1'}]},
        }}
        self.assertEqual(research_weekly._contrarian_usernames(frozen), {'u0'})

    def test_research_accounts_must_be_34_unique_strings(self):
        names = [f'u{i}' for i in range(34)]
        for bad in (names[:33], names + ['u34'], names[:33] + ['u0'], names[:33] + [7], 'notalist'):
            with self.subTest(bad=str(bad)[:40]):
                with self.assertRaises(ValueError):
                    research_weekly._to_candidates({'research_accounts': bad})
        self.assertEqual(len(research_weekly._to_candidates({'research_accounts': [' ' + n for n in names]})), 34)

    def test_summary_counts_missing_and_empty_accounts_without_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            posted = directory / 'tweets_posted.json'
            empty = directory / 'tweets_empty.json'
            posted.write_text('[{"text":"one"},{"text":"two"}]')
            empty.write_text('[]')
            # A stale file must not hide a failed/zero-post collection this week.
            (directory / 'tweets_missing.json').write_text('[{"text":"stale"}]')
            log = directory / 'weekly_log.md'
            output = io.StringIO()
            with patch.object(research_weekly, 'RESEARCH_DIR', tmp), \
                 patch.object(research_weekly, 'WEEKLY_LOG_PATH', str(log)), \
                 patch.object(research_weekly, 'ensure_research_dir'), \
                 patch.object(research_weekly, '_load_frozen_list', return_value={
                     'research_accounts': ['posted', 'empty', 'missing']}), \
                 patch.object(research_weekly, 'EXPECTED_RESEARCH_ACCOUNTS', 3), \
                 patch.object(research_weekly, 'phase_collect', return_value=[str(posted), str(empty)]), \
                 patch('scripts.research_influencers.phase_evaluate', side_effect=AssertionError('legacy evaluation called')), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(research_weekly.main(), 0)
            summary = next(line for line in output.getvalue().splitlines() if line.startswith('RW_SUMMARY '))
            self.assertEqual(json.loads(summary.removeprefix('RW_SUMMARY ')), {
                'accounts': 3, 'collected_files': 2, 'zero_post_accounts': 2, 'total_tweets': 2})
            self.assertIn('抽出はラッパー側', log.read_text())
            self.assertEqual(len(log.read_text().splitlines()), 1)


class TestIngestExitCode(unittest.TestCase):
    def run_ingest(self, records):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / 'input.json'
            input_path.write_text(json.dumps(records))
            with patch.object(sys, 'argv', ['winrate_ingest.py', '--input', str(input_path),
                                           '--research-dir', tmp, '--dry-run']), \
                 contextlib.redirect_stdout(io.StringIO()):
                return winrate_ingest.main()

    def test_empty_input_is_success(self):
        self.assertEqual(self.run_ingest([]), 0)

    def test_all_rejected_is_exit_two(self):
        self.assertEqual(self.run_ingest([{}, 'invalid']), 2)

    def test_mark_processed_appends_worklist_urls_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            worklist = Path(tmp) / 'worklist.json'
            worklist.write_text(json.dumps({'worklist': [
                {'tweet_url': 'https://x.com/a/status/1'}, {'tweet_url': 'https://x.com/a/status/2'}, {'text': 'no url'}]}))
            self.assertEqual(winrate_ingest.mark_processed(str(worklist), tmp), 2)
            self.assertEqual(winrate_ingest.mark_processed(str(worklist), tmp), 0)
            lines = (Path(tmp) / winrate_ingest.PROCESSED_LEDGER_FILENAME).read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(json.loads(lines[0])['batch'].startswith('research-weekly-'))

    def test_worklist_file_error_is_nonzero(self):
        from scripts import winrate_worklist
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'tweets_broken.json').write_text('{not json')
            with patch.object(sys, 'argv', ['winrate_worklist.py', '--tweets-glob', str(Path(tmp) / 'tweets_*.json'),
                                           '--research-dir', tmp, '--output', str(Path(tmp) / 'out.json')]), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(winrate_worklist.main(), 1)

    def test_partial_rejection_and_duplicates_are_success(self):
        signal = {
            'tweet_url': 'https://x.com/example/status/123', 'username': 'example',
            'posted_at': '2026-09-12T00:00:00+00:00', 'ticker': '7203.T',
            'direction': 'LONG', 'confidence': 0.9, 'matched_text': 'buy',
            'reasoning': 'test', 'extraction_model': 'sonnet/prompt-v2',
            'extracted_at': '2026-09-12T01:00:00+00:00',
        }
        self.assertEqual(self.run_ingest([signal, signal, {}]), 0)


class TestWeeklyWrapper(unittest.TestCase):
    """本物の Docker/Claude/通知/採点を起動せず、シェル全体を実行する。"""

    def run_wrapper(self, scenario="success", **overrides):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            output = root / "output/research"
            output.mkdir(parents=True)
            prompt = root / "docs/prompts/influencer_signal_extraction_v2.md"
            prompt.parent.mkdir(parents=True)
            prompt.write_text("FIXED PROMPT CONTENT")
            mock = bin_dir / "mock"
            mock.write_text("#!" + sys.executable + "\n" + r'''
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
scenario = os.environ["SCENARIO"]
with open("calls.log", "a") as f:
    f.write(name + " " + " ".join(args) + "\n")
if name == "docker":
    if "research_weekly.py" in " ".join(args):
        if scenario == "collect_fail": sys.exit(9)
        if scenario == "cookie": print("ログイン画面にリダイレクト（Cookie 失効の可能性）")
        if scenario != "no_summary":
            print('RW_SUMMARY ' + json.dumps(dict(accounts=34, collected_files=18,
                  zero_post_accounts=17 if scenario == "threshold" else 16, total_tweets=42)))
elif name == "claude":
    assert "ANTHROPIC_API_KEY" not in os.environ, "claude -p に API 鍵が渡ると購読でなく API 課金になる"
    assert "FIXED PROMPT CONTENT" in sys.stdin.read()
    # 契約ごとに検証（引数順や無害なオプション追加で壊れないように）
    assert args[0] == "-p"
    assert args[args.index("--output-format") + 1] == "text"
    assert args[args.index("--model") + 1] == os.environ.get("CLAUDE_MODEL", "sonnet")
    assert args[args.index("--tools") + 1] == ""
    banned = set(args[args.index("--disallowedTools") + 1:])
    assert {"Write", "Edit", "Bash", "Agent", "WebFetch", "WebSearch"} <= banned, banned
    if scenario == "claude_fail": sys.exit(7)
    print("broken [ []" if scenario == "bad_json" else "[]" if scenario == "empty_signals" else
          json.dumps([{"ticker": "7203.T"}, {"extraction_model": "kept/prompt-v2", "extracted_at": "kept"}]))
elif name == "python3":
    if args[0] == "scripts/winrate_worklist.py":
        if scenario == "work_fail": sys.exit(3)
        pathlib.Path("output/research/extraction_worklist.json").write_text(
            json.dumps({"worklist": [] if scenario == "empty_work" else [{"text": "buy"}]}))
    elif args[0] == "scripts/winrate_ingest.py":
        if scenario == "ingest_fail": sys.exit(2)
        print("取り込み成功（新規）: 0" if scenario == "empty_signals" else "取り込み成功（新規）: 1")
    elif args[0] == "scripts/winrate_score.py":
        if scenario == "score_fail": sys.exit(6)
    else:
        os.execv(sys.executable, [sys.executable] + args)
''')
            mock.chmod(0o755)
            for name in ("docker", "claude", "python3", "osascript"):
                (bin_dir / name).symlink_to(mock)
            wrapper = (ROOT / "scripts/research_weekly_launchd.sh").read_text()
            wrapper = wrapper.replace(
                'PROJECT_ROOT="/Users/masaaki_nagasawa/Desktop/biz/influx"',
                'PROJECT_ROOT="' + tmp + '"')
            env = dict(os.environ, SCENARIO=scenario, ANTHROPIC_API_KEY="test",
                       XAI_API_KEY="test", COOKIE_ENCRYPTION_KEY="test",
                       CLAUDE_BIN=str(bin_dir / "claude"), CLAUDE_MODEL="sonnet",
                       ZERO_POST_THRESHOLD="17", PATH=str(bin_dir) + ":/usr/bin:/bin")
            env.update(overrides)
            if scenario == "missing_claude":
                env["CLAUDE_BIN"] = str(bin_dir / "absent")
            if scenario == "missing_identity":
                env.pop("USER", None)
                env.pop("LOGNAME", None)
            result = subprocess.run(["/bin/bash"], input=wrapper, text=True,
                                    capture_output=True, cwd=root, env=env, timeout=15)
            calls = (root / "calls.log").read_text()
            records = [json.loads(p.read_text()) for p in output.glob("extraction_result_*.json")]
            return result, calls, records

    def test_success_order_metadata_and_notification(self):
        result, calls, records = self.run_wrapper(CLAUDE_MODEL="custom")
        self.assertEqual(result.returncode, 0, result.stderr)
        stages = ["research_weekly.py", "winrate_worklist.py", "claude -p", "winrate_ingest.py", "winrate_score.py"]
        self.assertEqual(sorted(stages, key=calls.index), stages)
        self.assertIn("週次完了: 収集18/抽出2/取込1", calls)
        self.assertEqual(records[0][0]["extraction_model"], "custom/prompt-v2")
        self.assertIn("T", records[0][0]["extracted_at"])
        # モデルが偽の証跡を返してもラッパー値で上書きされる（S2/S4）
        self.assertEqual(records[0][1]["extraction_model"], "custom/prompt-v2")
        self.assertNotEqual(records[0][1]["extracted_at"], "kept")

    def test_fail_closed_stages(self):
        cases = [("collect_fail", 4, "winrate_worklist.py"), ("cookie", 4, "winrate_worklist.py"),
                 ("no_summary", 4, "winrate_worklist.py"), ("threshold", 4, "winrate_worklist.py"),
                 ("work_fail", 3, "claude -p"), ("missing_claude", 5, "winrate_ingest.py"),
                 ("claude_fail", 5, "winrate_ingest.py"), ("bad_json", 5, "winrate_ingest.py"),
                 ("ingest_fail", 2, "winrate_score.py"), ("score_fail", 6, "週次完了")]
        for scenario, rc, forbidden in cases:
            with self.subTest(scenario=scenario):
                result, calls, _ = self.run_wrapper(scenario)
                self.assertEqual(result.returncode, rc, result.stderr)
                self.assertNotIn(forbidden, calls)
                self.assertIn("osascript", calls)
                self.assertIn("docker compose -f docker-compose.vnc.yml down", calls)

    def test_empty_worklist_exits_with_success_notification_without_extraction(self):
        result, calls, _ = self.run_wrapper("empty_work")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("claude -p", calls)
        self.assertIn("週次完了: 収集18/抽出0/取込0", calls)

    def test_missing_user_and_logname_are_synthesized(self):
        result, calls, _ = self.run_wrapper("missing_identity")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claude -p", calls)

    def test_empty_signals_continue_to_score(self):
        result, calls, records = self.run_wrapper("empty_signals")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(records, [[]])
        self.assertIn("winrate_score.py", calls)
        self.assertIn("週次完了: 収集18/抽出0/取込0", calls)

    def test_threshold_override(self):
        result, _, _ = self.run_wrapper("threshold", ZERO_POST_THRESHOLD="18")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
