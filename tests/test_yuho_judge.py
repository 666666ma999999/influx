"""yuho_judge の判定ロジック（P-INF-34 工程④・plan T6）の回帰テスト。

対象:
(a) share_pct: 分母 None→None・0→None・143036/181163→79.0（味の素 第148期の実測値）
(b) judge_period: 3 条件 True→需要／cond_c False→需要条件不成立／欠測→未判定／expectation→期待
(c) share_basis: 利益比を優先し、区分利益が None の時だけ売上比。profit_measure を必ず残す
(d) 単一セグメント（segments 空）は share=100・単一セグメント
(e) find_segment_member: 完全一致・部分一致・引けない
(f) build_products: fixture 2 期＋偽の read 行で periods が 2 本・label が期ごとに違う・source.doc_id あり
(g) Codex Critical の回帰: 1=丸め前の値で判定（29.96%→False・粗利率 1e-5 低下→False）／3=同一期・同一品目の複数 KPI を
    落とさない（キー= segment, item, kpi_name・衝突は先頭＋警告）／4=出所（element_id か ir_source）が揃って初めて 済／
    5=単一セグメントは single_segment フラグがある時だけ／8=代走 doc は費用集計から除外し「未測定」／10=consolidated_measure
    を期に出す／share_granularity を期に出す
(h) Codex 2 回目の回帰: 1=品目の label は 需要（区分）／需要条件不成立（区分）・segment_label に区分の判定を残す／
    2=候補行の無い期は 未判定（当期候補なし）／3=説明会資料だけの出所は未完（source_kind・IR 由来のみの件数）／
    4=KPI が数値でない期は 未判定（KPI 非数値）・expectation＋根拠節なら 期待／5=文字列列は strip 済み有効値で数える／
    6=usage_ok が True の doc だけ費用の実測／Warning=負の分母・分子は share=None・basis「赤字のため構成比を出せない」

実行: docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_yuho_judge -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import yuho_judge as yj  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "yuho" / "9999"


def _slice(net_sales=1000, gross=400, op=150, biz=None, seg_sales=600, seg_profit=120,
           profit_measure="jpcrp_cor:OperatingIncome", member="SegA"):
    return {
        "consolidated": {"net_sales": net_sales, "gross_profit": gross, "operating_profit": op, "business_profit": biz},
        "segments": {member: {"label": "A事業", "net_sales": seg_sales, "segment_profit": seg_profit}},
        "profit_measure": profit_measure,
    }


class SharePctTest(unittest.TestCase):
    def test_denominator_none(self):
        self.assertIsNone(yj.share_pct(100, None))
        self.assertIsNone(yj.share_pct(None, 100))
        self.assertIsNone(yj.share_pct(100, 0))

    def test_negative_denominator_or_numerator_is_none(self):
        self.assertIsNone(yj.share_pct(100, -50))     # 連結が赤字
        self.assertIsNone(yj.share_pct(-10, 100))     # 区分が赤字
        self.assertIsNone(yj.share_pct(-10, -50))
        self.assertEqual(yj.share_pct(0, 100), 0.0)   # 区分 0 は 0%
        self.assertTrue(yj._is_negative_pair(-10, 100))
        self.assertTrue(yj._is_negative_pair(10, 0))
        self.assertFalse(yj._is_negative_pair(None, 100))
        self.assertFalse(yj._is_negative_pair(10, None))

    def test_ajinomoto_example(self):
        # 既定は丸めない（判定用）・digits=1 が表示用
        self.assertAlmostEqual(yj.share_pct(143036, 181163), 78.9543, places=3)
        self.assertEqual(yj.share_pct(143036, 181163, digits=1), 79.0)
        self.assertEqual(yj.share_pct(66202, 181163, digits=yj.SHARE_DIGITS), 36.5)
        self.assertAlmostEqual(yj.gross_margin(40004, 100000), 0.40004, places=6)
        self.assertEqual(yj.gross_margin(40004, 100000, digits=4), 0.4)


class UnroundedJudgeTest(unittest.TestCase):
    """Codex Critical 1: 判定は丸め前の値・丸めは表示だけ。"""

    def test_share_2996_of_10000_is_not_30(self):
        cur = _slice(op=10000, seg_profit=2996)
        j = yj.judge_period(None, cur, "SegA")
        self.assertEqual(j["share_pct"], 30.0)      # 表示は 30.0 に丸まる
        self.assertIs(j["cond_a"], False)           # 判定は 29.96 < 30
        self.assertIs(yj.judge_period(None, _slice(op=10000, seg_profit=3000), "SegA")["cond_a"], True)

    def test_gross_margin_tiny_drop_is_false(self):
        prev = _slice(net_sales=100000, gross=40004)   # 0.40004
        cur = _slice(net_sales=100000, gross=40000)    # 0.40000 → 下がっている
        j = yj.judge_period(prev, cur, "SegA")
        self.assertEqual((j["gross_margin"], j["gross_margin_prev"]), (0.4, 0.4))  # 表示は同じ 0.4000
        self.assertIs(j["cond_c"], False)
        self.assertIs(yj.judge_period(cur, prev, "SegA")["cond_c"], True)  # 逆向き（上がる）は True


class JudgePeriodTest(unittest.TestCase):
    def test_all_true_is_demand(self):
        prev = _slice(net_sales=900, gross=350, seg_sales=500, seg_profit=100)   # gm 0.3889
        cur = _slice(net_sales=1000, gross=400, seg_sales=600, seg_profit=120)   # gm 0.40, sales +20%, profit up
        j = yj.judge_period(prev, cur, "SegA")
        self.assertEqual((j["cond_a"], j["cond_b"], j["cond_c"]), (True, True, True))
        self.assertEqual(j["share_pct"], 80.0)
        self.assertEqual(j["share_basis"], "利益比")
        self.assertEqual(j["profit_measure"], "jpcrp_cor:OperatingIncome")
        self.assertEqual(yj.label_of((j["cond_a"], j["cond_b"], j["cond_c"]), False), "需要")

    def test_cond_c_false_is_not_met(self):
        prev = _slice(net_sales=900, gross=400, seg_sales=500, seg_profit=100)   # gm 0.444
        cur = _slice(net_sales=1000, gross=400, seg_sales=600, seg_profit=120)   # gm 0.40 → down
        j = yj.judge_period(prev, cur, "SegA")
        self.assertEqual((j["cond_a"], j["cond_b"], j["cond_c"]), (True, True, False))
        self.assertEqual(yj.label_of((j["cond_a"], j["cond_b"], j["cond_c"]), False), "需要条件不成立")

    def test_cond_b_needs_both_growth_and_profit_up(self):
        prev = _slice(seg_sales=600, seg_profit=120)
        cur_small_growth = _slice(seg_sales=620, seg_profit=130)   # +3.3%
        self.assertFalse(yj.judge_period(prev, cur_small_growth, "SegA")["cond_b"])
        cur_profit_down = _slice(seg_sales=700, seg_profit=110)    # +16% but profit down
        self.assertFalse(yj.judge_period(prev, cur_profit_down, "SegA")["cond_b"])

    def test_missing_gross_profit_is_undecided(self):
        prev = _slice(gross=None)
        cur = _slice(gross=None, seg_sales=700, seg_profit=130)
        j = yj.judge_period(prev, cur, "SegA")
        self.assertIsNone(j["cond_c"])
        self.assertIsNone(j["gross_margin"])
        self.assertEqual(yj.label_of((j["cond_a"], j["cond_b"], j["cond_c"]), False), "未判定")

    def test_no_prev_makes_b_and_c_none(self):
        j = yj.judge_period(None, _slice(), "SegA")
        self.assertTrue(j["cond_a"])
        self.assertIsNone(j["cond_b"])
        self.assertIsNone(j["cond_c"])

    def test_expectation_beats_undecided_and_not_met(self):
        self.assertEqual(yj.label_of((None, None, None), True), "期待")
        self.assertEqual(yj.label_of((True, False, True), True), "期待")
        self.assertEqual(yj.label_of((True, True, True), True), "需要")

    def test_negative_profit_has_no_sales_fallback(self):
        # 区分利益が赤字: 利益比は出せない・売上比に逃がさず share=None・basis は「赤字のため構成比を出せない」
        j = yj.judge_period(_slice(), _slice(seg_profit=-5, seg_sales=900), "SegA")
        self.assertIsNone(j["share_pct"])
        self.assertEqual(j["share_basis"], "赤字のため構成比を出せない")
        self.assertIsNone(j["cond_a"])
        self.assertEqual(yj.label_of((j["cond_a"], j["cond_b"], j["cond_c"]), False), "未判定")
        # 連結営業利益が赤字でも同じ
        j = yj.judge_period(_slice(), _slice(op=-100), "SegA")
        self.assertEqual((j["share_pct"], j["share_basis"], j["cond_a"]), (None, "赤字のため構成比を出せない", None))

    def test_sales_basis_only_when_profit_missing(self):
        cur = _slice(seg_profit=None, seg_sales=400, net_sales=1000)
        j = yj.judge_period(_slice(), cur, "SegA")
        self.assertEqual(j["share_basis"], "売上比")
        self.assertEqual(j["share_pct"], 40.0)
        self.assertEqual(j["profit_measure"], "jpcrp_cor:OperatingIncome")
        self.assertIsNone(j["cond_b"])  # 区分利益が無いので b は欠測

    def test_business_profit_denominator_for_ifrs(self):
        cur = _slice(op=None, biz=181163, seg_profit=143036, profit_measure="jpigp_cor:BusinessProfitIFRS")
        j = yj.judge_period(None, cur, "SegA")
        self.assertEqual(j["share_pct"], 79.0)
        self.assertEqual(j["consolidated_measure"], "business_profit")

    def test_consolidated_profit_none_gives_sales_basis(self):
        cur = _slice(op=None, biz=None, seg_profit=120, seg_sales=600, net_sales=1000)
        j = yj.judge_period(None, cur, "SegA")
        self.assertEqual(j["share_basis"], "売上比")
        self.assertEqual(j["share_pct"], 60.0)

    def test_segment_not_found_share_none(self):
        j = yj.judge_period(_slice(), _slice(), None)
        self.assertIsNone(j["share_pct"])
        self.assertIsNone(j["share_basis"])
        self.assertIsNone(j["cond_a"])

    def test_single_segment_company(self):
        # Codex Critical 5: 単一セグメント扱いは facts.single_segment=True が立っている時だけ
        prev = {"consolidated": {"net_sales": 900, "gross_profit": 300, "operating_profit": 90, "business_profit": None},
                "segments": {}, "profit_measure": None, "single_segment": True}
        cur = {"consolidated": {"net_sales": 1000, "gross_profit": 350, "operating_profit": 100, "business_profit": None},
               "segments": {}, "profit_measure": None, "single_segment": True}
        j = yj.judge_period(prev, cur, None)
        self.assertEqual((j["share_pct"], j["share_basis"]), (100.0, "単一セグメント"))
        self.assertEqual((j["cond_a"], j["cond_b"], j["cond_c"]), (True, True, True))

    def test_empty_segments_without_flag_is_not_single(self):
        # segments が空＝抽出失敗かもしれないので、フラグ無しでは share=None・未判定
        for flag in (None, False, "true"):
            cur = {"consolidated": {"net_sales": 1000, "gross_profit": 350, "operating_profit": 100, "business_profit": None},
                   "segments": {}, "profit_measure": None, "single_segment": flag}
            prev = dict(cur, consolidated={"net_sales": 900, "gross_profit": 300, "operating_profit": 90, "business_profit": None})
            j = yj.judge_period(prev, cur, None)
            self.assertIsNone(j["share_pct"], flag)
            self.assertEqual(j["share_basis"], "セグメント抽出なし")
            self.assertIsNone(j["cond_a"])
            self.assertIsNone(j["cond_b"])
            self.assertIs(j["cond_c"], True)
            self.assertEqual(yj.label_of((j["cond_a"], j["cond_b"], j["cond_c"]), False), "未判定")
        # facts_slice はフラグを通す（yuho_fetch の実出力は None）
        self.assertIsNone(yj.facts_slice({"consolidated": {}, "segments": {}}, "CurrentYear")["single_segment"])
        self.assertIs(yj.facts_slice({"consolidated": {}, "segments": {}, "single_segment": True}, "CurrentYear")["single_segment"], True)


class FindSegmentMemberTest(unittest.TestCase):
    SEGS = {"SeasoningsAndFoodsReportableSegmentsMember": {"label": "調味料・食品"},
            "HealthcareAndOthersReportableSegmentsMember": {"label": "ヘルスケア等"}}

    def test_exact_and_partial(self):
        self.assertEqual(yj.find_segment_member(self.SEGS, "調味料・食品"), "SeasoningsAndFoodsReportableSegmentsMember")
        self.assertEqual(yj.find_segment_member(self.SEGS, "調味料･食品セグメント"), "SeasoningsAndFoodsReportableSegmentsMember")
        self.assertEqual(yj.find_segment_member(self.SEGS, "ヘルスケア"), "HealthcareAndOthersReportableSegmentsMember")

    def test_not_found(self):
        self.assertIsNone(yj.find_segment_member(self.SEGS, "冷凍食品"))
        self.assertIsNone(yj.find_segment_member(self.SEGS, None))
        self.assertIsNone(yj.find_segment_member({}, "調味料・食品"))


class BuildProductsTest(unittest.TestCase):
    def setUp(self):
        self.docs = [json.loads((FIX / f"S100TEST{x}.sections.json").read_text(encoding="utf-8")) for x in "AB"]
        self.row = {"segment": "調味料・食品", "item": "海外調味料", "kpi_name": "海外調味料の販売数量",
                    "kpi_value": 108, "kpi_unit": "%", "kpi_prev_value": 105, "share_scope": "区分",
                    "label_hint": "需要", "quote": "海外調味料の販売数量が前期比8%増", "quote_section": "mdna",
                    "expectation": False}
        self.exp_row = {"segment": "ヘルスケア等", "item": "ABF", "kpi_name": None, "kpi_value": None,
                        "kpi_unit": None, "kpi_prev_value": None, "share_scope": "区分", "label_hint": "期待",
                        "quote": "半導体材料「ABF」を成長事業・重点領域と位置付け", "quote_section": "business_policy",
                        "expectation": True}
        self.reads = {
            "S100TESTA": {"doc_id": "S100TESTA", "model": "m", "prompt_chars": 1909, "rows": [self.row, self.exp_row],
                          "usage": {"input_tokens": 2000, "output_tokens": 300}, "usage_ok": True, "error": None},
            "S100TESTB": {"doc_id": "S100TESTB", "model": "m", "prompt_chars": 1858, "rows": [dict(self.row, kpi_value=105)],
                          "usage": {"input_tokens": 1900, "output_tokens": 250}, "usage_ok": True, "error": None},
        }

    def test_periods_and_labels(self):
        r = yj.build_products(self.docs, self.reads)
        self.assertEqual(r["products_status"], "済")
        self.assertEqual(len(r["products"]), 2)
        p = next(p for p in r["products"] if p["item"] == "海外調味料")
        self.assertEqual([x["period_end"] for x in p["periods"]], ["2025-03-31", "2026-03-31"])
        by = {x["doc_id"]: x for x in p["periods"]}
        a, b = by["S100TESTA"], by["S100TESTB"]
        self.assertEqual((a["share_pct"], a["share_basis"], a["label"]), (79.0, "利益比", "需要（区分）"))
        self.assertEqual((b["share_pct"], b["cond_c"], b["label"]), (78.8, False, "需要条件不成立（区分）"))
        self.assertEqual((a["segment_label"], b["segment_label"]), ("需要", "需要条件不成立"))
        self.assertEqual(a["source"]["source_kind"], "有報")
        self.assertEqual(a["profit_measure"], "jpigp_cor:BusinessProfitIFRS")
        self.assertEqual(a["source"]["doc_id"], "S100TESTA")
        self.assertEqual(a["source"]["element_id"],
                         "jpcrp_cor:ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock")
        self.assertEqual(a["kpi_value"], 108)
        self.assertEqual(b["kpi_value"], 105)

    def test_expectation_row_only_in_latest(self):
        r = yj.build_products(self.docs, self.reads)
        p = next(p for p in r["products"] if p["item"] == "ABF")
        by = {x["doc_id"]: x for x in p["periods"]}
        self.assertEqual(by["S100TESTA"]["label"], "期待")          # ヘルスケア等は売上減で b=False・expectation で期待
        self.assertEqual(by["S100TESTA"]["segment_label"], "期待")
        # 前期は候補行なし → 品目の label は 未判定（当期候補なし）・segment_label には区分の判定（c=False）を参考値で残す
        self.assertEqual(by["S100TESTB"]["label"], "未判定（当期候補なし）")
        self.assertEqual(by["S100TESTB"]["segment_label"], "需要条件不成立")
        self.assertIs(by["S100TESTB"]["cond_c"], False)
        self.assertIsNone(by["S100TESTB"]["kpi_value"])
        self.assertIsNone(by["S100TESTB"]["source"]["source_kind"])
        md = yj.render_summary(r)
        self.assertIn("| segment_label | label |", md)
        self.assertIn("| 需要条件不成立 | 未判定（当期候補なし） |", md)

    def test_kpi_non_numeric_is_undecided_unless_expectation(self):
        # ヘルスケア等（b=False → 区分は 需要条件不成立）で kpi_value が数値に読めない → 未判定（KPI 非数値）
        nan_row = dict(self.exp_row, kpi_value="好調", expectation=False, label_hint="需要")
        reads = {"S100TESTA": dict(self.reads["S100TESTA"], rows=[nan_row]), "S100TESTB": None}
        p = yj.build_products(self.docs, reads)["products"][0]
        a = {x["doc_id"]: x for x in p["periods"]}["S100TESTA"]
        self.assertIsNone(a["kpi_value"])
        self.assertEqual(a["kpi_value_raw"], "好調")
        self.assertEqual((a["segment_label"], a["label"]), ("需要条件不成立", "未判定（KPI 非数値）"))
        self.assertIn("— (raw='好調')", yj.render_summary(yj.build_products(self.docs, reads)))
        # expectation=True で根拠節があれば 期待 が優先（vault §5 の順）
        reads = {"S100TESTA": dict(self.reads["S100TESTA"], rows=[dict(nan_row, expectation=True)]), "S100TESTB": None}
        a = yj.build_products(self.docs, reads)["products"][0]["periods"][-1]
        self.assertEqual((a["segment_label"], a["label"]), ("期待", "期待"))
        # expectation=True でも根拠節（quote_section）が無ければ 期待 にしない
        reads = {"S100TESTA": dict(self.reads["S100TESTA"], rows=[dict(nan_row, expectation=True, quote_section=None)]),
                 "S100TESTB": None}
        a = yj.build_products(self.docs, reads)["products"][0]["periods"][-1]
        self.assertEqual(a["label"], "未判定（KPI 非数値）")
        # 区分で 需要 が成立していても KPI が数値でなければ 需要（区分）にしない（Codex 3 回目 Critical 1・vault §5「KPI が数字で無い→未判定」）
        reads = {"S100TESTA": dict(self.reads["S100TESTA"], rows=[dict(self.row, kpi_value="約108")]), "S100TESTB": None}
        a = yj.build_products(self.docs, reads)["products"][0]["periods"][-1]
        self.assertEqual((a["kpi_value"], a["segment_label"], a["label"]), (None, "需要", "未判定（KPI 非数値）"))

    def test_segment_undecided_gives_reasoned_label(self):
        # 区分名が facts に引けない → share=None・cond_a=None → 区分は 未判定・品目は 未判定（区分数値なし）
        row = dict(self.row, segment="存在しない区分")
        reads = {"S100TESTA": dict(self.reads["S100TESTA"], rows=[row]), "S100TESTB": None}
        a = yj.build_products(self.docs, reads)["products"][0]["periods"][-1]
        self.assertEqual((a["segment_label"], a["label"]), ("未判定", "未判定（区分数値なし）"))
        self.assertEqual(yj.product_label(None, "需要", 1, False), "未判定（当期候補なし）")
        self.assertEqual(yj.product_label({}, "需要", None, False), "未判定（KPI 非数値）")
        self.assertEqual(yj.product_label({}, "需要", None, True), "期待")
        self.assertEqual(yj.product_label({}, "需要", 5, True), "需要（区分）")
        self.assertEqual(yj.product_label({}, "需要条件不成立", 5, True), "期待")
        self.assertEqual(yj.product_label({}, "需要条件不成立", 5, False), "需要条件不成立（区分）")

    def test_expectation_needs_verified_basis(self):
        # 期待は LLM の自己申告だけでは付けない（Codex 3 回目 Critical 2）: 経営方針の節・実在する element_id・本文にある引用が全部要る
        base = dict(self.exp_row)
        def label_for(row):
            reads = {"S100TESTA": dict(self.reads["S100TESTA"], rows=[row]), "S100TESTB": None}
            per = yj.build_products(self.docs, reads)["products"][0]["periods"]
            return {x["doc_id"]: x for x in per}["S100TESTA"]
        ok = label_for(base)
        self.assertEqual((ok["label"], ok["expectation"], ok["expect_basis"]), ("期待", True, "経営方針の本文に品目名つきの引用あり"))
        # 別品目の引用（ABF の文を 海外調味料 に付ける）→ 期待にしない（Codex 4 回目 Critical 1）
        a = label_for(dict(base, item="海外調味料"))
        self.assertEqual((a["label"], a["expectation"]), ("未判定（KPI 非数値）", False))
        self.assertIn("別品目", a["expect_basis"])
        # 記号の違い（ABF® と本文の ABF）は品目名の照合で無視する
        self.assertEqual(label_for(dict(base, item="ABF®"))["label"], "期待")
        # 下線・語の途中の改行は照合で無視する（Codex 5 回目 Warning 1・2）
        docs_u = [dict(self.docs[0], sections=dict(self.docs[0]["sections"],
                       business_policy={"element_id": "x:Policy", "chars": 12, "text": "ABF2を成\n長事業とする"})), self.docs[1]]
        reads_u = {"S100TESTA": dict(self.reads["S100TESTA"], rows=[dict(base, item="ABF_2", quote="ABF2を成\n長事業とする")]),
                   "S100TESTB": None}
        per_u = {x["doc_id"]: x for x in yj.build_products(docs_u, reads_u)["products"][0]["periods"]}["S100TESTA"]
        self.assertEqual((per_u["label"], per_u["expectation"]), ("期待", True))
        # 成長事業を示す語が無い引用 → 期待にしない
        a = label_for(dict(base, quote="半導体材料「ABF」"))
        self.assertEqual(a["label"], "未判定（KPI 非数値）")
        self.assertIn("成長事業を示す語", a["expect_basis"])
        # 経営方針の節が当期の有報に無い（節を None にした doc）→ 期待にしない
        docs = [dict(self.docs[0], sections=dict(self.docs[0]["sections"], business_policy=None)), self.docs[1]]
        reads = {"S100TESTA": dict(self.reads["S100TESTA"], rows=[base]), "S100TESTB": None}
        per = {x["doc_id"]: x for x in yj.build_products(docs, reads)["products"][0]["periods"]}["S100TESTA"]
        self.assertEqual((per["label"], per["expect_basis"]), ("未判定（KPI 非数値）", "経営方針の節が当期の有報に無い"))
        # 根拠節が説明会資料・引用なし → 未判定（KPI 非数値）
        a = label_for(dict(base, quote_section="ir_presentation", quote=None))
        self.assertEqual((a["label"], a["expectation"]), ("未判定（KPI 非数値）", False))
        self.assertIn("経営方針でない", a["expect_basis"])
        # 引用が本文に無い
        a = label_for(dict(base, quote="存在しない引用文"))
        self.assertEqual(a["label"], "未判定（KPI 非数値）")
        self.assertIn("見つからない", a["expect_basis"])
        # 引用が空
        self.assertEqual(label_for(dict(base, quote="  "))["expect_basis"], "引用が空")
        # 旧形式の文字列 "false" は期待にしない・"true" は読める
        self.assertEqual(label_for(dict(base, expectation="false"))["label"], "未判定（KPI 非数値）")
        self.assertEqual(label_for(dict(base, expectation="true"))["label"], "期待")
        # 読めない値（"yes"）は理由を残して期待にしない
        a = label_for(dict(base, expectation="yes"))
        self.assertEqual(a["label"], "未判定（KPI 非数値）")
        self.assertIn("読めない", a["expect_basis"])
        # 数値 KPI の行で区分が 需要 なら期待より 需要（区分）が先（vault §5 の順）
        a = label_for(dict(self.row, expectation=True, quote_section="business_policy",
                           quote="海外調味料の販売数量の拡大を経営上の目標の指標としております"))
        self.assertEqual((a["label"], a["expectation"], a["expect_basis"]),
                         ("需要（区分）", True, "経営方針の本文に品目名つきの引用あり"))

    def test_string_columns_counted_after_strip(self):
        # item が dict → product にしない（警告）・kpi_name が空白だけ → None → 出所未完・segment は strip
        bad_item = dict(self.row, item={"x": 1})
        blank_kpi = dict(self.row, kpi_name="   ", segment=" 調味料・食品 ")
        reads = {"S100TESTA": dict(self.reads["S100TESTA"], rows=[bad_item, blank_kpi]), "S100TESTB": None}
        r = yj.build_products(self.docs, reads)
        self.assertEqual(r["products_total"], 1)
        p = r["products"][0]
        self.assertEqual((p["segment"], p["item"], p["kpi_name"]), ("調味料・食品", "海外調味料", None))
        self.assertFalse(p["source_complete"])
        self.assertEqual(r["products_status"], "未判定")
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("item が無い行を捨てた", r["warnings"][0])
        self.assertEqual(yj._product_key({"item": {"x": 1}, "kpi_name": "k"})[1], "")

    def test_usage_summed_and_summary_has_tokens_line(self):
        r = yj.build_products(self.docs, self.reads)
        self.assertEqual(r["usage"]["input_tokens"], 3900)
        self.assertEqual(r["usage"]["output_tokens"], 550)
        md = yj.render_summary(r, price_in=3.0, price_out=15.0)
        self.assertIn("input_tokens=3900 output_tokens=550", md)
        self.assertIn("入力トークン合計 × 単価_in ＋ 出力トークン合計 × 単価_out", md)
        expected = 3900 / 1e6 * 3.0 + 550 / 1e6 * 15.0  # 0.0117 + 0.00825 = 0.01995
        self.assertIn(f"1 社あたり ${expected:.4f}", md)
        self.assertIn("単価: 未設定", yj.render_summary(r))

    def test_period_keeps_own_kpi_name_and_numeric_value(self):
        # 2025 期は「単価 "106%"」・2026 期は「数量 96」＝キーに kpi_name が入ったので別 product になる（期で揺れる問題は
        # 仕様側で扱う）。"106%" は 106・"1,234" は 1234 にする
        reads = {
            "S100TESTA": dict(self.reads["S100TESTA"], rows=[dict(self.row, kpi_name="数量（前年比）", kpi_value=96)]),
            "S100TESTB": dict(self.reads["S100TESTB"], rows=[dict(self.row, kpi_name="単価", kpi_value="106%", kpi_prev_value="1,234")]),
        }
        r = yj.build_products(self.docs, reads)
        self.assertEqual([p["kpi_name"] for p in r["products"]], ["数量（前年比）", "単価"])  # 直近期の行が先
        qty = {x["doc_id"]: x for x in r["products"][0]["periods"]}
        price = {x["doc_id"]: x for x in r["products"][1]["periods"]}
        self.assertEqual((qty["S100TESTA"]["kpi_name"], qty["S100TESTA"]["kpi_value"]), ("数量（前年比）", 96))
        self.assertIsNone(qty["S100TESTB"]["kpi_name"])  # 2025 期にはこの KPI の行が無い＝穴
        self.assertEqual((price["S100TESTB"]["kpi_name"], price["S100TESTB"]["kpi_value"], price["S100TESTB"]["kpi_prev_value"]),
                         ("単価", 106, 1234))
        self.assertIsNone(price["S100TESTA"]["kpi_value"])
        self.assertEqual(qty["S100TESTA"]["share_scope"], "区分")
        self.assertIsNone(yj._num("n/a"))   # 数値に読めない値は None（2 回目 Critical 4・raw は kpi_value_raw に残す）
        self.assertIsNone(yj._num(None))
        md = yj.render_summary(r)
        self.assertIn("| 単価 | 2025-03-31 |", md)
        self.assertNotIn("106% %", md)

    def test_no_reads_is_undecided(self):
        r = yj.build_products(self.docs, {"S100TESTA": None, "S100TESTB": None})
        self.assertEqual(r["products_status"], "未判定")
        self.assertEqual(r["products"], [])
        self.assertEqual((r["products_total"], r["complete_products"], r["warnings"]), (0, 0, []))
        self.assertEqual((r["usage"]["docs_read"], r["usage"]["docs_measured"], r["usage"]["docs_proxy"]), (0, 0, 0))
        self.assertIn("未測定", yj.render_summary(r, 2.0, 10.0))

    def test_consolidated_measure_and_share_granularity_in_periods(self):
        # Codex Critical 10 ＋ share_granularity
        r = yj.build_products(self.docs, self.reads)
        p = next(p for p in r["products"] if p["item"] == "海外調味料")
        by = {x["doc_id"]: x for x in p["periods"]}
        self.assertEqual(by["S100TESTA"]["consolidated_measure"], "business_profit")
        self.assertEqual(by["S100TESTA"]["share_granularity"], "区分")                  # 行の share_scope="区分"
        reads = {"S100TESTA": dict(self.reads["S100TESTA"], rows=[dict(self.row, share_scope="品目")]),
                 "S100TESTB": None}
        p2 = yj.build_products(self.docs, reads)["products"][0]
        by2 = {x["doc_id"]: x for x in p2["periods"]}
        self.assertEqual(by2["S100TESTA"]["share_granularity"], "区分比を品目に適用（確証に使わない）")
        self.assertEqual(by2["S100TESTB"]["share_granularity"], "区分比を品目に適用（確証に使わない）")  # 行なしでも区分比は出る
        reads = {"S100TESTA": dict(self.reads["S100TESTA"], rows=[dict(self.row, segment="存在しない区分")]), "S100TESTB": None}
        p3 = yj.build_products(self.docs, reads)["products"][0]
        self.assertIsNone(p3["periods"][0]["share_pct"])
        self.assertIsNone(p3["periods"][0]["share_granularity"])  # share が無ければ粒度も無い
        self.assertIsNone(yj._share_granularity({"share_scope": "区分"}, None))


class MultiKpiTest(unittest.TestCase):
    """Codex Critical 3: 同じ期・同じ品目の複数 KPI を落とさない（キー= segment, item, kpi_name）。"""

    def setUp(self):
        self.docs = [json.loads((FIX / f"S100TEST{x}.sections.json").read_text(encoding="utf-8")) for x in "AB"]
        base = {"segment": "調味料・食品", "item": "調味料、栄養・加工食品", "kpi_unit": "%", "kpi_prev_value": None,
                "share_scope": "区分", "label_hint": "需要", "quote_section": "mdna", "expectation": False}
        self.domestic = dict(base, kpi_name="国内数量", kpi_value=96, quote="国内 数量 96%")
        self.overseas = dict(base, kpi_name="海外数量", kpi_value=102, quote="海外 数量 102%")

    def _read(self, rows):
        return {"doc_id": "S100TESTA", "model": "m", "prompt_chars": 100, "rows": rows,
                "usage": {"input_tokens": 100, "output_tokens": 10}, "error": None}

    def test_two_kpis_same_item_give_two_products(self):
        r = yj.build_products(self.docs, {"S100TESTA": self._read([self.domestic, self.overseas]), "S100TESTB": None})
        self.assertEqual(r["products_total"], 2)
        self.assertEqual([p["kpi_name"] for p in r["products"]], ["国内数量", "海外数量"])
        self.assertEqual([p["item"] for p in r["products"]], ["調味料、栄養・加工食品"] * 2)
        vals = {p["kpi_name"]: {x["doc_id"]: x["kpi_value"] for x in p["periods"]} for p in r["products"]}
        self.assertEqual(vals, {"国内数量": {"S100TESTA": 96, "S100TESTB": None},
                                "海外数量": {"S100TESTA": 102, "S100TESTB": None}})
        self.assertEqual(r["warnings"], [])

    def test_same_key_twice_in_one_doc_keeps_first_and_warns(self):
        dup = dict(self.domestic, kpi_value=97, quote="重複した行")
        r = yj.build_products(self.docs, {"S100TESTA": self._read([self.domestic, dup]), "S100TESTB": None})
        self.assertEqual(r["products_total"], 1)
        self.assertEqual(r["products"][0]["periods"][-1]["kpi_value"], 96)  # 先頭の行
        self.assertEqual(len(r["warnings"]), 1)
        self.assertIn("衝突", r["warnings"][0])
        self.assertIn("S100TESTA", r["warnings"][0])
        self.assertIn("国内数量", r["warnings"][0])
        md = yj.render_summary(r)
        self.assertIn("## 警告", md)
        self.assertIn("衝突", md)

    def test_key_normalizes_kpi_name(self):
        same = dict(self.domestic, kpi_name="国内 数量")  # 空白違いは同じ KPI
        r = yj.build_products(self.docs, {"S100TESTA": self._read([self.domestic, same]), "S100TESTB": None})
        self.assertEqual(r["products_total"], 1)
        self.assertEqual(len(r["warnings"]), 1)


class SourceCompletenessTest(unittest.TestCase):
    """Codex Critical 4: 出所（doc_id ＋ element_id）が揃った product が無ければ 未判定。
    2 回目 Critical 3: 説明会資料（ir_source）だけの出所は揃ったと数えない（source_ir_only・ir_only_products で件数だけ出す）。"""

    def setUp(self):
        self.docs = [json.loads((FIX / f"S100TEST{x}.sections.json").read_text(encoding="utf-8")) for x in "AB"]
        self.ir_row = {"segment": "調味料・食品", "item": "海外調味料", "kpi_name": "数量", "kpi_value": 105, "kpi_unit": "%",
                       "kpi_prev_value": None, "share_scope": "品目", "label_hint": "需要",
                       "quote": "海外調味料 数量 105%", "quote_section": "ir_presentation", "expectation": False}

    def _read(self, rows, ir_source="FY2025_presentation.txt"):
        return {"doc_id": "S100TESTA", "model": "m", "prompt_chars": 100, "rows": rows, "ir_source": ir_source,
                "usage": {"input_tokens": 100, "output_tokens": 10}, "error": None}

    def test_ir_only_source_is_incomplete_but_counted(self):
        r = yj.build_products(self.docs, {"S100TESTA": self._read([self.ir_row]), "S100TESTB": None})
        p = r["products"][0]
        per = {x["doc_id"]: x for x in p["periods"]}
        self.assertEqual(per["S100TESTA"]["source"]["ir_source"], "FY2025_presentation.txt")
        self.assertEqual(per["S100TESTA"]["source"]["source_kind"], "説明会資料")
        self.assertIsNone(per["S100TESTA"]["source"]["element_id"])   # 説明会資料に有報の節 ID は無い
        self.assertIsNone(per["S100TESTB"]["source"]["ir_source"])    # 行の無い期には伝搬しない
        self.assertFalse(p["source_complete"])                         # IR 由来だけでは出所未完
        self.assertTrue(p["source_ir_only"])
        self.assertEqual((r["products_status"], r["complete_products"], r["ir_only_products"], r["products_total"]),
                         ("未判定", 0, 1, 1))
        md = yj.render_summary(r)
        self.assertIn("IR 由来のみで出所未完=1 products", md.splitlines()[0])
        self.assertIn("説明会資料: FY2025_presentation.txt", md)

        r2 = yj.build_products(self.docs, {"S100TESTA": self._read([self.ir_row], ir_source=None), "S100TESTB": None})
        self.assertFalse(r2["products"][0]["source_complete"])
        self.assertFalse(r2["products"][0]["source_ir_only"])
        self.assertIsNone(r2["products"][0]["periods"][-1]["source"]["ir_source"])
        self.assertIsNone(r2["products"][0]["periods"][-1]["source"]["source_kind"])
        self.assertEqual((r2["products_status"], r2["complete_products"], r2["ir_only_products"]), ("未判定", 0, 0))

    def test_ir_source_not_propagated_to_yuho_rows(self):
        row = dict(self.ir_row, quote_section="mdna")
        r = yj.build_products(self.docs, {"S100TESTA": self._read([row]), "S100TESTB": None})
        src = r["products"][0]["periods"][-1]["source"]
        self.assertIsNone(src["ir_source"])
        self.assertEqual(src["element_id"], "jpcrp_cor:ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock")
        self.assertEqual(src["source_kind"], "有報")
        self.assertTrue(r["products"][0]["source_complete"])
        self.assertFalse(r["products"][0]["source_ir_only"])
        self.assertEqual(r["ir_only_products"], 0)
        self.assertIn("有報: jpcrp_cor:ManagementAnalysis", yj.render_summary(r))

    def test_missing_section_or_kpi_name_is_incomplete(self):
        # quote_section が null → element_id も ir_source も無い＝未完成
        r = yj.build_products(self.docs, {"S100TESTA": self._read([dict(self.ir_row, quote_section=None)]), "S100TESTB": None})
        self.assertFalse(r["products"][0]["source_complete"])
        self.assertEqual(r["products_status"], "未判定")
        # quote_section が本文の無い節（business_results は fixture で null）→ element_id 無し＝未完成
        r = yj.build_products(self.docs, {"S100TESTA": self._read([dict(self.ir_row, quote_section="business_results")]), "S100TESTB": None})
        self.assertFalse(r["products"][0]["source_complete"])
        # kpi_name が無い行は出所が揃っていても未完成
        r = yj.build_products(self.docs, {"S100TESTA": self._read([dict(self.ir_row, kpi_name=None)]), "S100TESTB": None})
        self.assertFalse(r["products"][0]["source_complete"])
        self.assertEqual(r["products_status"], "未判定")

    def test_all_row_periods_must_be_complete(self):
        # 2026 期は IR 由来で ir_source あり・2025 期は quote_section=null → product としては未完成
        read_b = dict(self._read([dict(self.ir_row, quote_section=None)]), doc_id="S100TESTB")
        r = yj.build_products(self.docs, {"S100TESTA": self._read([self.ir_row]), "S100TESTB": read_b})
        self.assertEqual(r["products_total"], 1)
        self.assertFalse(r["products"][0]["source_complete"])
        self.assertFalse(r["products"][0]["source_ir_only"])   # 2025 期は出所が何も無い＝IR 由来のみでもない
        self.assertEqual(r["products_status"], "未判定")
        # 2026 期= 有報の節・2025 期= 説明会資料 → 揃ってはいないが IR 由来のみ（旧ルールなら完備）に数える
        read_b = dict(self._read([self.ir_row]), doc_id="S100TESTB")
        r = yj.build_products(self.docs, {"S100TESTA": self._read([dict(self.ir_row, quote_section="mdna")]),
                                          "S100TESTB": read_b})
        self.assertFalse(r["products"][0]["source_complete"])
        self.assertTrue(r["products"][0]["source_ir_only"])
        self.assertEqual(r["ir_only_products"], 1)


class CostMeasurementTest(unittest.TestCase):
    """Codex Critical 8: 代走（model に「代走」・input_tokens=0）は費用集計から除外し「未測定」と書く。
    2 回目 Critical 6: 実測に数えるのは usage_ok が True の doc だけ（通信失敗・usage_ok の無い旧 read.json は未測定）。"""

    PROXY_MODEL = "claude-sonnet (Claude Code subagent 代走・API 未使用)"

    def setUp(self):
        self.docs = [json.loads((FIX / f"S100TEST{x}.sections.json").read_text(encoding="utf-8")) for x in "AB"]
        self.row = {"segment": "調味料・食品", "item": "海外調味料", "kpi_name": "数量", "kpi_value": 105, "kpi_unit": "%",
                    "kpi_prev_value": None, "share_scope": "区分", "label_hint": "需要", "quote": "q",
                    "quote_section": "mdna", "expectation": False}

    def _read(self, doc_id, model, inp, out, chars=1000, usage_ok=True, error=None):
        rec = {"doc_id": doc_id, "model": model, "prompt_chars": chars, "rows": [self.row],
               "usage": {"input_tokens": inp, "output_tokens": out}, "error": error}
        if usage_ok is not None:
            rec["usage_ok"] = usage_ok
        return rec

    def test_all_proxy_is_unmeasured(self):
        reads = {"S100TESTA": self._read("S100TESTA", self.PROXY_MODEL, 0, 0),
                 "S100TESTB": self._read("S100TESTB", self.PROXY_MODEL, 0, 0)}
        r = yj.build_products(self.docs, reads)
        u = r["usage"]
        self.assertEqual((u["docs_read"], u["docs_measured"], u["docs_proxy"]), (2, 0, 2))
        self.assertEqual((u["input_tokens"], u["output_tokens"], u["prompt_chars"]), (0, 0, 0))
        self.assertTrue(all(d["proxy"] for d in r["docs"]))
        md = yj.render_summary(r, price_in=2.0, price_out=10.0)
        self.assertIn("## 費用（未測定）", md)
        self.assertIn("API 未実行（代走）＝費用未測定: 2 doc", md)
        self.assertIn("未測定: API を実際に叩いた doc が 0", md)
        self.assertIn("代走=2 usage 未取得=0", md)
        self.assertNotIn("1 社あたり $", md)   # 金額の行は出さない（案内文の「1 社あたりの金額は…実走してから」は別）
        self.assertNotIn("単価:", md)
        self.assertNotIn("文字→トークン係数", md)
        self.assertIn("（API 未実行・代走）", md)

    def test_mixed_counts_only_measured(self):
        reads = {"S100TESTA": self._read("S100TESTA", "claude-sonnet-5", 1000, 100, chars=2000),
                 "S100TESTB": self._read("S100TESTB", self.PROXY_MODEL, 0, 0, chars=999)}
        r = yj.build_products(self.docs, reads)
        u = r["usage"]
        self.assertEqual((u["docs_measured"], u["docs_proxy"]), (1, 1))
        self.assertEqual((u["input_tokens"], u["output_tokens"], u["prompt_chars"]), (1000, 100, 2000))
        md = yj.render_summary(r, price_in=2.0, price_out=10.0)
        self.assertIn("## 費用（実測）", md)
        self.assertIn("実測 doc=1: input_tokens=1000 output_tokens=100 prompt_chars=2000", md)
        self.assertIn("input_tokens ÷ prompt_chars = 0.500", md)
        expected = 1000 / 1e6 * 2.0 + 100 / 1e6 * 10.0
        self.assertIn(f"1 社あたり ${expected:.4f}（実測 1 doc 分）", md)
        self.assertIn("API 未実行（代走）＝費用未測定: 1 doc", md)

    def test_measured_only_when_usage_ok_true(self):
        # usage_ok=True なら 0 トークンでも実測（API が usage を返した事実が根拠）
        reads = {"S100TESTA": self._read("S100TESTA", "claude-sonnet-5", 0, 0, usage_ok=True), "S100TESTB": None}
        u = yj.build_products(self.docs, reads)["usage"]
        self.assertEqual((u["docs_measured"], u["docs_proxy"], u["docs_unmeasured"]), (1, 0, 0))
        # usage_ok が無い旧 read.json → 未測定（通信失敗の $0 を実測に混ぜない）
        reads = {"S100TESTA": self._read("S100TESTA", "claude-sonnet-5", 0, 0, usage_ok=None), "S100TESTB": None}
        r = yj.build_products(self.docs, reads)
        u = r["usage"]
        self.assertEqual((u["docs_read"], u["docs_measured"], u["docs_proxy"], u["docs_unmeasured"]), (1, 0, 0, 1))
        md = yj.render_summary(r, price_in=2.0, price_out=10.0)
        self.assertIn("## 費用（未測定）", md)
        self.assertIn("usage 未取得＝費用未測定: 1 doc", md)
        self.assertNotIn("1 社あたり $", md)
        self.assertFalse(r["docs"][-1]["usage_ok"])
        # usage_ok=False（通信失敗）も未測定
        reads = {"S100TESTA": self._read("S100TESTA", "claude-sonnet-5", 0, 0, usage_ok=False,
                                         error="attempt1: network; attempt2: network"), "S100TESTB": None}
        u = yj.build_products(self.docs, reads)["usage"]
        self.assertEqual((u["docs_measured"], u["docs_unmeasured"], u["docs_error"]), (0, 1, 1))
        # error ありでも usage_ok=True なら実測に入れる（JSON 抽出失敗でも消費は起きている）
        reads = {"S100TESTA": self._read("S100TESTA", "claude-sonnet-5", 700, 30, usage_ok=True,
                                         error="attempt1: no json array; attempt2: no json array"), "S100TESTB": None}
        u = yj.build_products(self.docs, reads)["usage"]
        self.assertEqual((u["docs_measured"], u["docs_error"], u["input_tokens"]), (1, 1, 700))
        self.assertFalse(yj._is_proxy_read(None))
        self.assertTrue(yj._is_proxy_read({"model": "x 代走 y", "usage": {"input_tokens": 0}}))
        self.assertFalse(yj._is_proxy_read({"model": "x 代走 y", "usage": {"input_tokens": 5}}))


if __name__ == "__main__":
    unittest.main()
