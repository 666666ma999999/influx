"""X 品薄系の検索語の置き場の回帰テスト（P-INF-22・オーナー裁定 2026-10-03 Q1=a）。

裁定: 主題を含まない汎用語（品不足・入手困難 等）は固定クエリ configs/x_price_watch.json に足さず、
発見器 scripts/price_watch_discover.py の DISCOVER_QUERIES（入口語）にだけ足す。固定クエリは凍結
（scripts/price_watch_collect.py docstring「クエリ(id/q/min_faves)は凍結。編集せず新idで追加する」）。

守らせる 2 点:
1. 固定クエリ 55 本の (id, q, min_faves) が 2026-10-03 時点の指紋と一致する（凍結の確認）。
   新 id を正しく「追加」した時はこのテストが落ちる＝指紋と件数を意図して更新する（無言の変更を防ぐ）。
2. 入口語は初期 3 語＋2026-10-03 追加 5 語を含み、3 文字以上の入口語はすべて STOPLIST に入っている
   （検索語自体が候補トークンとして候補キューに混ざらない）。

発見器本体は playwright 系を import するため、ここでは本文を ast で読む（ホスト直実行でも動く）。
実行: python3 tests/test_price_watch_discover_vocab.py
"""
from __future__ import annotations

import ast
import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/x_price_watch.json"
DISCOVER = ROOT / "scripts/price_watch_discover.py"

FROZEN_COUNT = 55
FROZEN_FINGERPRINT = "8ae26b974f2c9acb5e9be42075d0b4e1f123905b82090c900e43e7a61b7b1001"
ORIGINAL_ENTRY_WORDS = {"値上げ", "値上がり", "品薄"}
ADDED_2026_10_03 = {"品不足", "入手困難", "供給不足", "逼迫", "調達難"}


def _fingerprint(queries: list[dict]) -> str:
    rows = sorted((q["id"], q["q"], q["min_faves"]) for q in queries)
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def _module_constants(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
            if name in {"DISCOVER_QUERIES", "STOPLIST"}:
                out[name] = ast.literal_eval(node.value)
    return out


class FixedQueriesFrozen(unittest.TestCase):
    def test_count_and_fingerprint(self):
        queries = json.loads(CONFIG.read_text(encoding="utf-8"))["queries"]
        self.assertEqual(len(queries), FROZEN_COUNT, "固定クエリの本数が変わった（追加なら指紋と件数を意図して更新）")
        self.assertEqual(_fingerprint(queries), FROZEN_FINGERPRINT, "固定クエリの id/q/min_faves が変わった（凍結違反か意図した追加）")

    def test_generic_entry_words_not_added_as_standalone_fixed_queries(self):
        queries = json.loads(CONFIG.read_text(encoding="utf-8"))["queries"]
        standalone = {q["q"].strip() for q in queries}
        leaked = ADDED_2026_10_03 & standalone
        self.assertEqual(leaked, set(), f"汎用の入口語が固定クエリに単体で入っている: {leaked}")


class EntryWordsInDiscoverer(unittest.TestCase):
    def setUp(self):
        self.consts = _module_constants(DISCOVER)

    def test_entry_words_present(self):
        words = set(self.consts["DISCOVER_QUERIES"])
        self.assertTrue(ORIGINAL_ENTRY_WORDS <= words, "初期 3 語が欠けた")
        self.assertTrue(ADDED_2026_10_03 <= words, f"2026-10-03 追加語が欠けた: {ADDED_2026_10_03 - words}")

    def test_entry_words_are_stoplisted(self):
        stop = set(self.consts["STOPLIST"])
        missing = {w for w in self.consts["DISCOVER_QUERIES"] if len(w) >= 3 and w not in stop}
        self.assertEqual(missing, set(), f"3 文字以上の入口語が STOPLIST に無い（検索語自体が候補化する）: {missing}")


if __name__ == "__main__":
    unittest.main()
