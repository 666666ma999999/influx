"""sedori_trend_digest.py の回帰テスト（2026-08-10 の修理3点を固定する）。

せどりトレンド定点観測レーン（設計正本: tasks/sedori_keyword_review.md §7）の週次ダイジェスト
生成器を検証する。stdlib のみ（host 柵で実行可・Docker 不要）。

固定する3点（いずれも 2026-08-10 に実データで再現・修理済み）:
  1. 型番の語境界: `\\b` を使うと「op-01カートン」のように前後が日本語だと \\w 扱いで境界が
     成立せず取り逃す。さらに大文字小文字が別名として数えられ、掲載条件（2回以上）に届かず消える
  2. 型番の接尾辞: `\\w*` は「rtx4090入荷」の日本語まで型番に飲み込む（英字のみに限定する）
  3. 集計窓: 収集ランナー（sedori_trend_run.sh）は UTC 前日〜7日前を収集するのに対し、集計が
     当日〜6日前だと最古の収集日を丸ごと読み落とす（実測 637→712投稿 = +75 の取りこぼし）

併せて、ランナーが完全一致で読む機械可読行 `SUPPLY_COUNT=<n>` の書式が壊れていないことを確認する。

実行:
    python3 -m unittest tests.test_sedori_trend_digest -v
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "sedori_trend_digest.py"


def load_module():
    """scripts/ は package ではないのでファイルパスから直接 import する。"""
    spec = importlib.util.spec_from_file_location("sedori_trend_digest", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestExtractNames(unittest.TestCase):
    """修理1・2: 型番抽出の語境界と接尾辞。"""

    def setUp(self):
        self.m = load_module()

    def test_model_number_found_inside_japanese(self):
        """日本語に挟まれた型番を拾う（`\\b` 復活の防止）。"""
        for text, expected in [
            ("PS5が品薄で転売価格が上昇", "PS5"),
            ("Switch 2 の抽選受付が開始", "SWITCH2"),
            ("ロマンスドーンop-01カートン¥535,000", "OP-01"),
            ("DDR5メモリが高騰", "DDR5"),
        ]:
            with self.subTest(text=text):
                self.assertIn(expected, self.m.extract_names(text))

    def test_model_number_case_is_merged(self):
        """大文字小文字の表記ゆれを1つの名前に合算する（掲載条件2回以上に届かせる）。"""
        names_lower = self.m.extract_names("op-01が入荷")
        names_upper = self.m.extract_names("OP-01 BOX 買取")
        self.assertEqual(names_lower & {"OP-01"}, {"OP-01"})
        self.assertEqual(names_upper & {"OP-01"}, {"OP-01"})
        # 別名として分裂していないこと（小文字キーが残っていない）
        self.assertNotIn("op-01", names_lower)

    def test_model_suffix_does_not_swallow_japanese(self):
        """接尾辞は ASCII 英字のみ（`\\w*` 復活の防止）。"""
        self.assertIn("RTX4090", self.m.extract_names("rtx4090入荷しました"))
        self.assertNotIn("RTX4090入荷", self.m.extract_names("rtx4090入荷しました"))

    def test_legitimate_model_suffix_is_kept(self):
        """正当入力の通過確認: 英字接尾辞（Ti 等）は型番の一部として残る。"""
        self.assertIn("RTX4090TI", self.m.extract_names("RTX 4090Ti の在庫あり"))

    def test_japanese_name_patterns_are_not_uppercased(self):
        """【】「」・カタカナ連の抽出は正規化しない（型番だけが大文字化対象）。"""
        self.assertIn("ポケカ", self.m.extract_names("【ポケカ】新弾の予約"))
        self.assertIn("黒煙の支配者", self.m.extract_names("「黒煙の支配者」が高騰"))

    def test_stop_names_are_excluded(self):
        """STOP_NAMES は従来どおり除外される（既存挙動の回帰確認）。"""
        self.assertNotIn("キャンペーン", self.m.extract_names("【キャンペーン】実施中"))


class TestLoadRecentWindow(unittest.TestCase):
    """修理3: 集計窓が収集ランナーの窓（UTC前日から7日分）と一致すること。"""

    def setUp(self):
        self.m = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.texts = Path(self.tmp.name) / "texts"
        self.texts.mkdir()
        self.m.TEXTS_DIR = self.texts
        # 基準日を固定（UTC 2026-08-10）→ 期待窓 = 2026-08-03〜2026-08-09
        self.m.today_utc = lambda: dt.date(2026, 8, 10)

    def write_day(self, day: str, n: int):
        p = self.texts / f"{day}.jsonl"
        p.write_text(
            "\n".join(
                json.dumps({"status_id": f"{day}-{i}", "text": f"{day} 投稿{i}"}, ensure_ascii=False)
                for i in range(n)
            ),
            encoding="utf-8",
        )

    def test_window_matches_runner_range(self):
        """8/02（窓外・古い）と 8/10（窓外・当日）を除き、8/03〜8/09 の7日を読む。"""
        for day in ["2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05",
                    "2026-08-06", "2026-08-07", "2026-08-08", "2026-08-09", "2026-08-10"]:
            self.write_day(day, 2)
        rows = self.m.load_recent()
        days = sorted({r["status_id"].rsplit("-", 1)[0] for r in rows})
        self.assertEqual(days[0], "2026-08-03", "収集された最古日を読み落としている")
        self.assertEqual(days[-1], "2026-08-09", "当日（収集対象外）を含めている")
        self.assertEqual(len(days), 7)
        self.assertEqual(len(rows), 14)

    def test_oldest_collected_day_is_not_dropped(self):
        """修理前の窓（当日〜6日前）だと 8/03 が落ちた — その退行を直接検出する。"""
        self.write_day("2026-08-03", 5)
        self.write_day("2026-08-09", 5)
        rows = self.m.load_recent()
        self.assertEqual(len(rows), 10)

    def test_duplicate_status_id_is_deduped(self):
        """既存挙動の回帰確認: 同一 status_id は重複排除される（冪等再収集の前提）。"""
        self.write_day("2026-08-05", 3)
        (self.texts / "2026-08-06.jsonl").write_text(
            json.dumps({"status_id": "2026-08-05-0", "text": "重複"}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.assertEqual(len(self.m.load_recent()), 3)

    def test_malformed_filename_and_line_are_skipped(self):
        """既存挙動の回帰確認: 日付でないファイル名・壊れた行は黙って飛ばす。"""
        (self.texts / "notadate.jsonl").write_text('{"status_id":"x","text":"y"}', encoding="utf-8")
        (self.texts / "2026-08-07.jsonl").write_text('{壊れたJSON\n{"status_id":"ok","text":"z"}',
                                                     encoding="utf-8")
        rows = self.m.load_recent()
        self.assertEqual([r["status_id"] for r in rows], ["ok"])


class TestMachineReadableContract(unittest.TestCase):
    """ランナー sedori_trend_run.sh が完全一致で読む行の書式を固定する。"""

    def setUp(self):
        self.m = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.texts = base / "texts"
        self.texts.mkdir()
        self.m.TEXTS_DIR = self.texts
        self.m.DIGEST_DIR = base / "digests"
        # main() は出力パスを REPO 相対で表示するため、リポジトリ根の想定も tmp に寄せる
        self.m.REPO = base
        self.m.today_utc = lambda: dt.date(2026, 8, 10)

    def test_supply_count_line_format(self):
        """`SUPPLY_COUNT=<n>` が行頭完全一致で出る（run.sh の sed 条件と同じ形）。"""
        (self.texts / "2026-08-05.jsonl").write_text(
            "\n".join([
                json.dumps({"status_id": "a", "text": "再販決定のお知らせ"}, ensure_ascii=False),
                json.dumps({"status_id": "b", "text": "増産すると発表"}, ensure_ascii=False),
                json.dumps({"status_id": "c", "text": "ただの雑談"}, ensure_ascii=False),
            ]),
            encoding="utf-8",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.m.main()
        self.assertEqual(rc, 0)
        lines = buf.getvalue().splitlines()
        self.assertIn("SUPPLY_COUNT=2", lines, "run.sh が読む機械可読行の書式が変わっている")

    def test_digest_file_is_written_with_week_label(self):
        """週ラベル付きのダイジェストが UTC 基準週で書き出される。"""
        (self.texts / "2026-08-05.jsonl").write_text(
            json.dumps({"status_id": "a", "text": "【ポケカ】再販決定"}, ensure_ascii=False),
            encoding="utf-8",
        )
        with redirect_stdout(io.StringIO()):
            self.m.main()
        week = dt.date(2026, 8, 10).isocalendar()
        out = self.m.DIGEST_DIR / f"digest_{week[0]}-W{week[1]:02d}.md"
        self.assertTrue(out.exists(), f"{out.name} が生成されていない")
        body = out.read_text(encoding="utf-8")
        self.assertIn("観測専用（銘柄非提示・判定なし）", body, "観測専用の但し書きが消えている")
        self.assertIn("再販決定", body, "供給側反応が本文に載っていない")


if __name__ == "__main__":
    unittest.main()
