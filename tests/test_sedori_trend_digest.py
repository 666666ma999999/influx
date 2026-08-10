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
import re
import subprocess
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

    def test_model_number_negatives(self):
        """負例（Codex 2026-08-10 MEDIUM-1）: 英数字に隣接する断片を型番にしない。

        `\\b` を外しただけだと、より長い識別子・価格・URL・ハッシュの一部から偽の型番を拾う。
        前後の ASCII 英数字を否定することで、日本語隣接（通す）と英数字隣接（落とす）を分ける。
        """
        for text, must_not in [
            ("PS50周年モデル発売", "PS5"),
            ("OP-010は別型番です", "OP-01"),
            ("DDR5000円で落札", "DDR5"),
            ("Switch20周年記念", "SWITCH2"),
            ("RTX40900というSKU", "RTX4090"),
            ("https://x.example/aPS5b", "PS5"),
            ("sha=abcDDR5def", "DDR5"),
        ]:
            with self.subTest(text=text):
                self.assertNotIn(must_not, self.m.extract_names(text))

    def test_japanese_adjacency_still_matches(self):
        """負例対策が正当入力を殺していないこと（偽陽性ガード）。"""
        for text, expected in [
            ("OP-01カートン入荷", "OP-01"),
            ("DDR5メモリが高騰", "DDR5"),
            ("PS5が品薄", "PS5"),
            ("Switch 2の抽選", "SWITCH2"),
        ]:
            with self.subTest(text=text):
                self.assertIn(expected, self.m.extract_names(text))

    def test_stop_substrings_reject_compound_ad_words(self):
        """複合語の宣伝文句を商品名にしない（Codex 2026-08-10 LOW-3）。"""
        self.assertNotIn("オンラインショップ", self.m.extract_names("【オンラインショップ】限定"))
        self.assertNotIn("プレゼントキャンペーン",
                         self.m.extract_names("「プレゼントキャンペーン」実施中"))
        # 正当入力は通る（宣伝語を含まないカタカナ商品名）
        self.assertIn("ロマンスドーン", self.m.extract_names("ロマンスドーンが高騰"))

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

    def test_shape_invalid_records_do_not_crash(self):
        """JSONとして妥当でも形が違う行で週次処理が落ちない（Codex 2026-08-10 LOW-4）。

        ランナーは digest 失敗で非ゼロ終了するため、1行の異常が全停止になっていた。
        """
        (self.texts / "2026-08-07.jsonl").write_text(
            "\n".join([
                "[]",
                "null",
                "42",
                '"文字列だけの行"',
                '{"status_id":"a","text":null}',
                '{"status_id":"b","text":"正常な投稿・再販決定"}',
            ]),
            encoding="utf-8",
        )
        rows = self.m.load_recent()  # 例外を出さないこと自体が検証
        self.assertEqual([r["status_id"] for r in rows], ["b"])
        # 後段（名前抽出・供給側判定）も通ること
        self.assertIsInstance(self.m.build_digest(rows), str)


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
        raw = buf.getvalue()
        lines = raw.splitlines()
        self.assertIn("SUPPLY_COUNT=2", lines, "run.sh が読む機械可読行の書式が変わっている")
        # splitlines() は \r を吸収してしまうため、生出力の行境界でも検証する
        # （CRLF になると run.sh の sed 完全一致が外れる・Codex 2026-08-10 LOW-5）
        self.assertRegex(raw, r"(?m)^SUPPLY_COUNT=2$")
        self.assertNotIn("\r", raw, "行末に CR が混ざると run.sh の sed が読めない")

    def test_supply_count_is_readable_by_the_actual_runner_sed(self):
        """ランナー実物の sed 式でこの出力が読めること（Python 側だけで固めない・LOW-5）。

        run.sh の該当行から sed 式を実際に取り出して適用する。テストが通るのに shell 側の
        契約が壊れている状態を防ぐ。
        """
        run_sh = (REPO / "scripts" / "sedori_trend_run.sh").read_text(encoding="utf-8")
        m = re.search(r"sed -n '([^']*SUPPLY_COUNT[^']*)'", run_sh)
        self.assertIsNotNone(m, "run.sh から SUPPLY_COUNT の sed 式を見つけられない")
        sed_expr = m.group(1)
        (self.texts / "2026-08-05.jsonl").write_text(
            "\n".join([
                json.dumps({"status_id": "a", "text": "再販決定"}, ensure_ascii=False),
                json.dumps({"status_id": "b", "text": "増産を発表"}, ensure_ascii=False),
            ]),
            encoding="utf-8",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.m.main()
        proc = subprocess.run(["sed", "-n", sed_expr], input=buf.getvalue(),
                              capture_output=True, text=True, check=True)
        self.assertEqual(proc.stdout.strip().splitlines()[-1:], ["2"],
                         f"run.sh の sed 式 {sed_expr!r} でこの出力から件数を取り出せない")

    def test_digest_is_named_by_window_end_not_generation_day(self):
        """窓の終端が属する週で名付ける（Codex 2026-08-10 MEDIUM-2）。

        基準日 2026-08-10（月）は ISO W33 だが、窓は 08-03〜08-09 = W32。生成日基準だと
        「W33 のダイジェスト」に W32 のデータが入り、毎週の定期実行で必ず1週ずれる。
        """
        (self.texts / "2026-08-05.jsonl").write_text(
            json.dumps({"status_id": "a", "text": "【ポケカ】再販決定"}, ensure_ascii=False),
            encoding="utf-8",
        )
        with redirect_stdout(io.StringIO()):
            self.m.main()
        self.assertEqual(self.m.week_label(), "2026-W32")
        out = self.m.DIGEST_DIR / "digest_2026-W32.md"
        self.assertTrue(out.exists(), "窓の終端の週で命名されていない（生成日基準に戻っている）")
        self.assertFalse((self.m.DIGEST_DIR / "digest_2026-W33.md").exists(),
                         "生成日基準の名前で余分なファイルが出ている")
        body = out.read_text(encoding="utf-8")
        self.assertIn("観測専用（銘柄非提示・判定なし）", body, "観測専用の但し書きが消えている")
        self.assertIn("2026-W32", body, "見出しの週ラベルが窓と一致していない")
        self.assertIn("収集日ラベル 2026-08-03〜2026-08-09", body, "収集日ラベルの明記が消えている")
        self.assertIn("再販決定", body, "供給側反応が本文に載っていない")


class TestSupplyPostOrdering(unittest.TestCase):
    """供給側反応の20件制限が「新しい順」で切られること（Codex 2026-08-10 LOW-6）。"""

    def setUp(self):
        self.m = load_module()
        self.m.today_utc = lambda: dt.date(2026, 8, 10)

    def test_newest_supply_posts_survive_truncation(self):
        """21件以上ある時、窓の終盤の重要な発表が省略側に落ちない。"""
        rows = [{"status_id": f"old{i}", "text": f"再販のお知らせ{i}",
                 "posted_at": f"2026-08-03T0{i % 10}:00:00Z"} for i in range(20)]
        rows.append({"status_id": "new", "text": "重要な増産を発表",
                     "posted_at": "2026-08-09T12:00:00Z"})
        body = self.m.build_digest(rows)
        self.assertIn("重要な増産を発表", body, "最新の発表が20件制限で切り捨てられている")
        self.assertIn("（全21件中、新しい順に20件表示）", body)
        # 最古の投稿が押し出されていること（新しい順に20件＝21件目が落ちる）
        supply_section = body.split("## 📈")[0]
        self.assertEqual(supply_section.count("\n- ["), 20)

    def test_actual_posted_at_range_is_shown_alongside_collection_labels(self):
        """収集日ラベルと投稿実時刻を別々に出す（X検索の since/until が1日ずれるため）。

        両者を「対象期間」の1語にまとめると、窓外の日付の投稿が並んでいるように読めてしまう。
        """
        rows = [{"status_id": "a", "text": "再販決定", "posted_at": "2026-08-04T01:00:00Z"},
                {"status_id": "b", "text": "増産発表", "posted_at": "2026-08-10T09:00:00Z"}]
        body = self.m.build_digest(rows)
        self.assertIn("収集日ラベル 2026-08-03〜2026-08-09（ファイル名基準）", body)
        self.assertIn("投稿の実時刻レンジ 2026-08-04〜2026-08-10（UTC）", body)

    def test_missing_posted_at_does_not_break_the_range(self):
        """posted_at 欠落だけの入力でも落ちない（レンジは「不明」と出す）。"""
        body = self.m.build_digest([{"status_id": "a", "text": "再販決定"}])
        self.assertIn("投稿の実時刻レンジ 不明（UTC）", body)


if __name__ == "__main__":
    unittest.main()
