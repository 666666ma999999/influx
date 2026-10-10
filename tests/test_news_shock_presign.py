"""品薄予兆レーン presign（P-INF-43 b・2026-10-10 事前登録）の回帰テスト。ネット不要（HTTP はモック）。

正本: tasks/shortage_presign_forward.md §事前登録 B・E／configs/news_shock.json の presign ブロック。
守ること: 固定反例 4 件が非発火／negated・既定経路（--lane 省略）に presign の関数が一切呼ばれない・
既存 --selftest が引き続き全通過。
実行: docker compose run --rm -v "$PWD/tests:/app/tests" xstock python -m unittest tests.test_news_shock_presign -v
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import news_shock_collect as ns  # noqa: E402

PCFG = ns.presign_config()
BY = {q["subject"]: q for q in PCFG["queries"]}
RUN_AT = "2026-10-13T00:30:00+00:00"


def _item(title: str, link: str, pub: str = "Tue, 13 Oct 2026 08:30:00 +0900", desc: str = "") -> dict:
    return {"title": title, "link": link, "desc": desc, "pubdate": pub}


@contextlib.contextmanager
def _quiet():
    with mock.patch("sys.stdout", new=io.StringIO()), mock.patch("sys.stderr", new=io.StringIO()):
        yield


def _run(tmp: Path, gnews: dict | None = None, feeds: dict | None = None, date: str = "2026-10-13",
         dry_run: bool = False):
    """run_presign をモック HTTP で回し (rc, 台帳の全行, notify mock) を返す。

    gnews= {subject: items}（他主題は 0 件）／feeds= {feed_id: items | Exception}（他 feed は 0 件）。
    """
    gnews, feeds = gnews or {}, feeds or {}
    ledger = tmp / "presign_log.jsonl"

    def fake_gnews(qe, pcfg):
        v = gnews.get(qe["subject"], [])
        if isinstance(v, Exception):
            raise v
        return list(v)

    def fake_feed(fd, pcfg):
        v = feeds.get(fd["id"], [])
        if isinstance(v, Exception):
            raise v
        return list(v)

    with mock.patch.object(ns, "PRESIGN_LEDGER", ledger), \
            mock.patch.object(ns, "fetch_presign_gnews", side_effect=fake_gnews), \
            mock.patch.object(ns, "fetch_feed", side_effect=fake_feed), \
            mock.patch.object(ns, "notify") as notify, _quiet():
        rc = ns.run_presign(dry_run=dry_run, date_override=date)
    rows = ([json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
            if ledger.exists() else [])
    return rc, rows, notify


# ---------------------------------------------------------------- ① 固定反例 4 件 ② 正例 2 件
class FixedCounterexamples(unittest.TestCase):
    def test_japan_airlines_is_not_pan(self):
        self.assertIsNone(ns.presign_judge("ジャパン航空が運賃の価格改定を発表", BY["pan"], PCFG, "gnews-media"))

    def test_tamagotchi_is_not_keiran(self):
        self.assertIsNone(ns.presign_judge("たまごっちの抽選販売", BY["keiran"], PCFG, "gnews-media"))

    def test_pan_miokuri_is_negated(self):
        got = ns.presign_judge("パンの価格改定を見送り", BY["pan"], PCFG, "gnews-media")
        self.assertEqual(got["type"], "negated")
        self.assertEqual(got["matched_vocab"], "価格改定")

    def test_dram_to_fall_is_negated(self):
        got = ns.presign_judge("DRAM contract price forecast to fall 10% QoQ", BY["dram"], PCFG, "gnews-media")
        self.assertEqual(got["type"], "negated")
        self.assertEqual(got["vocab_family"], "industry_forecast")


class Positives(unittest.TestCase):
    def test_nanya_dram_both_families(self):
        got = ns.presign_judge("ナンヤテクノロジー、DRAM契約価格を最大20%引き上げへ 供給不足深刻化",
                               BY["dram"], PCFG, "gnews-media")
        self.assertEqual(got["type"], "hit")
        self.assertEqual(got["matched_term"], "DRAM")
        self.assertEqual(got["families_all"], ["official_notice", "industry_forecast"])
        self.assertEqual(got["vocab_family"], "official_notice")       # gnews は official 優先
        self.assertIn("供給不足", got["matched_vocab_all"])
        self.assertIn("契約価格", got["matched_vocab_all"])

    def test_hbm_outlook_feed_industry(self):
        got = ns.presign_judge("HBM Supply Constraints Persist, 2027 Price Outlook Revised Upward",
                               BY["dram"], PCFG, "feed-industry")
        self.assertEqual(got["type"], "hit")
        self.assertEqual(got["matched_term"], "HBM")
        self.assertEqual(got["vocab_family"], "industry_forecast")
        self.assertIn("industry_forecast", got["families_all"])

    def test_exclude_is_per_query(self):
        got = ns.presign_judge("たまごっちの抽選販売", BY["tamagotchi"], PCFG, "gnews-media")
        self.assertEqual((got["type"], got["vocab_family"]), ("hit", "official_notice"))

    def test_direction_required_without_direction_is_silent(self):
        self.assertIsNone(ns.presign_judge("DRAM contract price forecast flat QoQ", BY["dram"], PCFG, "gnews-media"))

    def test_ascii_boundary_allows_cjk_adjacency_but_not_ascii_substring(self):
        self.assertTrue(ns._presign_token_in("ps5の抽選販売", "PS5"))
        self.assertFalse(ns._presign_token_in("david copperfield show", "copper"))
        self.assertFalse(ns._presign_token_in("golden week", "gold"))

    def test_coverage_rule_japanese_vocab_in_search_phrases(self):
        jp = [v for words in PCFG["vocab_families"].values() for v in words if not v.isascii()]
        self.assertEqual([v for v in jp if v not in PCFG["search_or_phrases"]], [])


# ---------------------------------------------------------------- ③ Atom / RSS 解析
ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>METI</title>
  <link rel="self" href="https://www.meti.go.jp/ml_index_release_atom.xml"/>
  <entry>
    <title>半導体の供給不足に関する注意喚起</title>
    <link rel="alternate" href="https://www.meti.go.jp/press/2026/10/x.html"/>
    <id>tag:meti.go.jp,2026:x</id>
    <updated>2026-10-13T10:00:00+09:00</updated>
    <summary type="html">&lt;p&gt;本文です&lt;/p&gt;</summary>
  </entry>
  <entry>
    <title>二件目</title>
    <link href="https://www.meti.go.jp/press/2026/10/y.html"/>
    <published>2026-10-12T23:00:00Z</published>
    <updated>2026-10-13T01:00:00Z</updated>
    <content type="html">内容</content>
  </entry>
</feed>"""

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>鶏鳴新聞</title><link>https://www.keimei.ne.jp/</link>
<item><title>鶏卵相場 上昇見通し</title><link>https://www.keimei.ne.jp/article/1.html</link>
<description><![CDATA[<b>卵価</b>が上昇]]></description>
<pubDate>Tue, 13 Oct 2026 08:30:00 +0900</pubDate></item>
</channel></rss>"""


class FeedParsing(unittest.TestCase):
    def test_atom_entry_link_href_updated_summary(self):
        items = ns.parse_feed_items(ATOM.encode("utf-8"))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "半導体の供給不足に関する注意喚起")
        self.assertEqual(items[0]["link"], "https://www.meti.go.jp/press/2026/10/x.html")
        self.assertEqual(items[0]["desc"], "本文です")
        self.assertEqual(items[0]["pubdate"], "2026-10-13T10:00:00+09:00")
        # rel 省略の link は alternate・published を updated より優先・content も desc に
        self.assertEqual(items[1]["link"], "https://www.meti.go.jp/press/2026/10/y.html")
        self.assertEqual(items[1]["pubdate"], "2026-10-12T23:00:00Z")
        self.assertEqual(items[1]["desc"], "内容")

    def test_leading_blank_line_or_bom_before_xml_declaration(self):
        # 鶏鳴新聞 feed の実測: 1 行目が空行（XML 宣言が 2 行目）。BOM つきも同様に読める
        self.assertEqual(len(ns.parse_feed_items(b"\n" + RSS.encode("utf-8"))), 1)
        self.assertEqual(len(ns.parse_feed_items(b"\xef\xbb\xbf" + RSS.encode("utf-8"))), 1)

    def test_rss_item_cdata_description_pubdate(self):
        items = ns.parse_feed_items(RSS.encode("utf-8"))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["link"], "https://www.keimei.ne.jp/article/1.html")
        self.assertEqual(items[0]["desc"], "卵価 が上昇")
        self.assertEqual(items[0]["pubdate"], "Tue, 13 Oct 2026 08:30:00 +0900")

    def test_feed_subjects_restrict_judgement(self):
        # keimei（subjects=[keiran]）の記事は keiran でだけ判定され、feed-industry の族付与になる
        with tempfile.TemporaryDirectory() as d:
            rc, rows, _ = _run(Path(d), feeds={"keimei-shimbun": ns.parse_feed_items(RSS.encode("utf-8"))})
        hits = [r for r in rows if r["type"] == "hit"]
        self.assertEqual(rc, 0)
        self.assertEqual([(h["source_id"], h["subject"], h["source_class"], h["vocab_family"]) for h in hits],
                         [("keimei-shimbun", "keiran", "feed-industry", "industry_forecast")])


# ---------------------------------------------------------------- ④ pubdate の UTC 日付化
class PubdateUtc(unittest.TestCase):
    def test_rfc822_crosses_date_in_utc(self):
        self.assertEqual(ns.parse_pubdate_utc("Tue, 13 Oct 2026 08:30:00 +0900", RUN_AT), ("2026-10-12", True))

    def test_iso8601_with_offset_and_z(self):
        self.assertEqual(ns.parse_pubdate_utc("2026-10-13T08:30:00+09:00", RUN_AT), ("2026-10-12", True))
        self.assertEqual(ns.parse_pubdate_utc("2026-10-13T08:30:00Z", RUN_AT), ("2026-10-13", True))

    def test_unparseable_falls_back_to_run_date_and_flag(self):
        self.assertEqual(ns.parse_pubdate_utc("いつか", RUN_AT), ("2026-10-13", False))
        self.assertEqual(ns.parse_pubdate_utc("", RUN_AT), ("2026-10-13", False))

    def test_event_id_uses_utc_date_and_source_class(self):
        a = ns.presign_event_id("dram", "official_notice", "2026-10-12", "gnews-media")
        b = ns.presign_event_id("dram", "official_notice", "2026-10-12", "gnews-media")
        c = ns.presign_event_id("dram", "official_notice", "2026-10-12", "feed-official")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(a), 16)


# ---------------------------------------------------------------- ⑤ 重複排除と utm 除去
class Dedupe(unittest.TestCase):
    def test_normalize_link_drops_utm_and_fragment_only(self):
        self.assertEqual(ns.normalize_link("https://n/a?utm_source=t&id=1&utm_medium=m#top"), "https://n/a?id=1")
        self.assertEqual(ns.normalize_link("https://n/a"), "https://n/a")
        self.assertEqual(ns.presign_article_id("gnews:dram", ns.normalize_link("https://n/a?id=1&utm_x=1")),
                         ns.presign_article_id("gnews:dram", "https://n/a?id=1"))
        self.assertNotEqual(ns.presign_article_id("gnews:dram", "https://n/a"),
                            ns.presign_article_id("trendforce-semiconductors", "https://n/a"))

    def test_same_source_and_link_is_written_once_across_runs(self):
        title = "DRAM 値上げを通知 供給不足で"
        with tempfile.TemporaryDirectory() as d:
            rc1, rows1, n1 = _run(Path(d), gnews={"dram": [_item(title, "https://n/a?id=1")]})
            rc2, rows2, n2 = _run(Path(d), gnews={"dram": [_item(title, "https://n/a?id=1&utm_source=x#frag")]},
                                  date="2026-10-14")
        self.assertEqual((rc1, rc2), (0, 0))
        hits1 = [r for r in rows1 if r["type"] == "hit"]
        hits2 = [r for r in rows2 if r["type"] == "hit"]
        self.assertEqual(len(hits1), 1)
        self.assertEqual(len(hits2), 1, "2 便目は同一 (source_id, link_norm) を書かない")
        self.assertEqual(hits1[0]["known_date"], "2026-10-13", "known_date は最初の便の日付のまま")
        self.assertEqual([r["type"] for r in rows2[-2:]], ["run_summary", "run_summary"])
        self.assertEqual(rows2[-1]["hits"], 0)
        self.assertEqual(n1.call_count, 1)
        self.assertEqual(n2.call_count, 0)

    def test_feed_article_hitting_two_subjects_writes_two_rows_once(self):
        # 裁定 b（2026-10-10）: 鍵は (source_id, subject, link_norm)。feed 1 記事 × 主題 2（dram と nand-ssd）で
        # 2 行（article_id は同じ値）・同じ便の再実行では増えない
        title = "DRAM and NAND Flash contract prices forecast to rise further on AI server demand"
        items = [_item(title, "https://www.trendforce.com/presscenter/news/1.html")]
        with tempfile.TemporaryDirectory() as d:
            rc1, rows1, _ = _run(Path(d), feeds={"trendforce-semiconductors": items})
            rc2, rows2, _ = _run(Path(d), feeds={"trendforce-semiconductors": items})
        hits1 = [r for r in rows1 if r["type"] == "hit"]
        hits2 = [r for r in rows2 if r["type"] == "hit"]
        self.assertEqual((rc1, rc2), (0, 0))
        self.assertEqual(sorted(h["subject"] for h in hits1), ["dram", "nand-ssd"])
        self.assertEqual(len({h["article_id"] for h in hits1}), 1, "同一記事なので article_id は同じ")
        self.assertEqual({h["source_id"] for h in hits1}, {"trendforce-semiconductors"})
        self.assertEqual(len(hits2), 2, "再実行では hit 行が増えない（台帳全体で 2 行のまま・run_summary だけ増える）")
        self.assertEqual(rows2[-1]["hits"], 0)
        self.assertEqual([r["type"] for r in rows2[-2:]], ["run_summary", "run_summary"])

    def test_different_source_same_link_is_separate_row(self):
        title = "DRAM supply shortage prompts price increase notice"
        with tempfile.TemporaryDirectory() as d:
            _, rows, _ = _run(Path(d), gnews={"dram": [_item(title, "https://n/same")]},
                              feeds={"samsung-newsroom": [_item(title, "https://n/same")]})
        hits = [r for r in rows if r["type"] == "hit"]
        self.assertEqual(sorted(h["source_id"] for h in hits), ["gnews:dram", "samsung-newsroom"])
        self.assertNotEqual(hits[0]["article_id"], hits[1]["article_id"])

    def test_hit_row_has_all_schema_keys_and_snippet(self):
        want = {"type", "lane", "run_at", "known_date", "source_class", "source_id", "subject", "matched_term",
                "matched_vocab", "matched_vocab_all", "vocab_family", "families_all", "article_id", "event_id",
                "dup_event", "episode_id", "first_in_episode", "title", "desc_snippet", "link", "pubdate",
                "pubdate_utc_date", "pubdate_parsed", "presign_version", "config_sha"}
        with tempfile.TemporaryDirectory() as d:
            _, rows, _ = _run(Path(d), gnews={"dram": [_item("DRAM 値上げ", "https://n/1", desc="x" * 500)]})
        hit = [r for r in rows if r["type"] == "hit"][0]
        self.assertEqual(set(hit), want)
        self.assertEqual(len(hit["desc_snippet"]), 300)
        self.assertEqual(hit["lane"], "presign")
        self.assertEqual(hit["presign_version"], PCFG["version"])
        self.assertEqual(hit["pubdate_utc_date"], "2026-10-12")
        self.assertTrue(hit["pubdate_parsed"])
        summ = rows[-1]
        self.assertEqual(summ["type"], "run_summary")
        self.assertEqual((summ["queries_planned"], summ["queries_ok"], summ["feeds_planned"], summ["feeds_ok"]),
                         (len(PCFG["queries"]), len(PCFG["queries"]), len(PCFG["feeds"]), len(PCFG["feeds"])))
        self.assertEqual(summ["items"]["gnews:dram"], 1)


# ---------------------------------------------------------------- ⑥ エピソード 7 日・非連鎖
class Episode(unittest.TestCase):
    def test_day0_day6_same_day7_new(self):
        eps: dict = {}
        e0 = ns.presign_episode(eps, "gnews-media", "dram", "official_notice", "2026-10-13")
        e6 = ns.presign_episode(eps, "gnews-media", "dram", "official_notice", "2026-10-19")
        e7 = ns.presign_episode(eps, "gnews-media", "dram", "official_notice", "2026-10-20")
        e13 = ns.presign_episode(eps, "gnews-media", "dram", "official_notice", "2026-10-26")
        self.assertEqual(e0, ("gnews-media:dram:official_notice:2026-10-13", True))
        self.assertEqual(e6, ("gnews-media:dram:official_notice:2026-10-13", False))
        self.assertEqual(e7, ("gnews-media:dram:official_notice:2026-10-20", True), "day7 は新・day6 からは連鎖しない")
        self.assertEqual(e13, ("gnews-media:dram:official_notice:2026-10-20", False))

    def test_episode_key_is_source_class_subject_family(self):
        eps: dict = {}
        ns.presign_episode(eps, "gnews-media", "dram", "official_notice", "2026-10-13")
        self.assertTrue(ns.presign_episode(eps, "feed-official", "dram", "official_notice", "2026-10-13")[1])
        self.assertTrue(ns.presign_episode(eps, "gnews-media", "dram", "industry_forecast", "2026-10-13")[1])
        self.assertTrue(ns.presign_episode(eps, "gnews-media", "nand-ssd", "official_notice", "2026-10-13")[1])

    def test_episode_restored_from_ledger_and_notify_only_first(self):
        t = "DRAM 値上げを発表 供給不足"
        with tempfile.TemporaryDirectory() as d:
            _, r1, n1 = _run(Path(d), gnews={"dram": [_item(t, "https://n/1")]}, date="2026-10-13")
            _, r2, n2 = _run(Path(d), gnews={"dram": [_item(t, "https://n/2")]}, date="2026-10-19")
            _, r3, n3 = _run(Path(d), gnews={"dram": [_item(t, "https://n/3")]}, date="2026-10-20")
        h1, h2, h3 = ([r for r in rows if r["type"] == "hit"][-1] for rows in (r1, r2, r3))
        self.assertEqual((h1["episode_id"], h1["first_in_episode"]), ("gnews-media:dram:official_notice:2026-10-13", True))
        self.assertEqual((h2["episode_id"], h2["first_in_episode"]), ("gnews-media:dram:official_notice:2026-10-13", False))
        self.assertEqual((h3["episode_id"], h3["first_in_episode"]), ("gnews-media:dram:official_notice:2026-10-20", True))
        self.assertEqual((n1.call_count, n2.call_count, n3.call_count), (1, 0, 1))
        self.assertEqual([r3[-1]["new_episodes"], r2[-1]["new_episodes"]], [1, 0])


class NegatedAndErrors(unittest.TestCase):
    def test_negated_row_written_without_episode_or_notify(self):
        with tempfile.TemporaryDirectory() as d:
            rc, rows, notify = _run(Path(d), gnews={"pan": [_item("パンの価格改定を見送り", "https://n/p")]})
        neg = [r for r in rows if r["type"] == "negated"]
        self.assertEqual(rc, 0)
        self.assertEqual(len(neg), 1)
        self.assertIsNone(neg[0]["episode_id"])
        self.assertFalse(neg[0]["first_in_episode"])
        self.assertEqual(notify.call_count, 0)
        self.assertEqual((rows[-1]["hits"], rows[-1]["negated"]), (0, 1))

    def test_dry_run_writes_nothing_and_never_notifies(self):
        with tempfile.TemporaryDirectory() as d:
            rc, rows, notify = _run(Path(d), gnews={"dram": [_item("DRAM 値上げ", "https://n/1")]}, dry_run=True)
            self.assertFalse((Path(d) / "presign_log.jsonl").exists())
        self.assertEqual((rc, rows, notify.call_count), (0, [], 0))

    def test_partial_failure_gate_one_third(self):
        with tempfile.TemporaryDirectory() as d:
            rc_ok, rows_ok, _ = _run(Path(d), feeds={"meti-release": RuntimeError("HTTP 503")})
        with tempfile.TemporaryDirectory() as d:
            rc_ng, rows_ng, _ = _run(Path(d), gnews={q: RuntimeError("timeout") for q in BY})
        self.assertEqual(rc_ok, 0)
        self.assertEqual(rows_ok[-1]["feeds_ok"], len(PCFG["feeds"]) - 1)
        self.assertEqual([r["source_id"] for r in rows_ok if r["type"] == "error"], ["meti-release"])
        self.assertEqual(rc_ng, 1, "全 query 失敗（1/3 超）は rc=1")
        self.assertEqual(rows_ng[-1]["queries_ok"], 0)

    def test_bad_date_is_rejected(self):
        with _quiet():
            self.assertEqual(ns.run_presign(dry_run=True, date_override="2026/10/13"), 2)


# ---------------------------------------------------------------- 収集開始日ゲート（2026-10-10 team-lead 裁定）
class CollectStartGate(unittest.TestCase):
    START = json.loads((ROOT / "configs/presign_forward.json").read_text(encoding="utf-8")
                       )["scoring"]["collect_interval"]["start"]

    def test_config_start_is_2026_10_13(self):
        self.assertEqual(self.START, "2026-10-13")
        self.assertEqual(ns.presign_collect_start().isoformat(), "2026-10-13")

    def test_before_start_skips_without_fetch_or_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "presign_log.jsonl"
            with mock.patch.object(ns, "PRESIGN_LEDGER", ledger), \
                    mock.patch.object(ns, "fetch_presign_gnews") as fg, \
                    mock.patch.object(ns, "fetch_feed") as ff, \
                    mock.patch.object(ns, "notify") as nt, \
                    mock.patch("sys.stdout", new=io.StringIO()) as out:
                rc = ns.run_presign(dry_run=False, date_override="2026-10-12")
            self.assertEqual(rc, 0)
            self.assertIn("収集区間前のためスキップ", out.getvalue())
            self.assertFalse(ledger.exists(), "台帳に何も書かない")
            self.assertFalse((Path(d) / ".presign.lock").exists(), "ロックも取らない（ゲートはロックの前）")
            for m in (fg, ff, nt):
                m.assert_not_called()

    def test_first_day_of_interval_runs(self):
        with tempfile.TemporaryDirectory() as d:
            rc, rows, _ = _run(Path(d), gnews={"dram": [_item("DRAM 値上げ 供給不足", "https://n/1")]},
                               date="2026-10-13")
        self.assertEqual(rc, 0)
        self.assertEqual([r["type"] for r in rows], ["hit", "run_summary"])
        self.assertEqual(rows[0]["known_date"], "2026-10-13")

    def test_dry_run_before_start_still_runs(self):
        with tempfile.TemporaryDirectory() as d:
            rc, rows, notify = _run(Path(d), gnews={"dram": [_item("DRAM 値上げ", "https://n/1")]},
                                    date="2026-10-10", dry_run=True)
            self.assertFalse((Path(d) / "presign_log.jsonl").exists())
        self.assertEqual((rc, rows, notify.call_count), (0, [], 0))

    def test_missing_config_or_key_is_nonzero_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d) / "presign_log.jsonl"
            no_key = Path(d) / "fwd_no_key.json"
            no_key.write_text(json.dumps({"scoring": {"collect_interval": {"end_exclusive": "2026-12-12"}}}),
                              encoding="utf-8")
            for cfg_path in (Path(d) / "missing.json", no_key):
                with mock.patch.object(ns, "PRESIGN_LEDGER", ledger), \
                        mock.patch.object(ns, "PRESIGN_FORWARD_CONFIG", cfg_path), \
                        mock.patch.object(ns, "fetch_presign_gnews") as fg, \
                        mock.patch.object(ns, "fetch_feed") as ff, _quiet():
                    rc = ns.run_presign(dry_run=False, date_override="2026-10-13")
                self.assertEqual(rc, 1, f"{cfg_path.name}: 設定が読めない時は非ゼロ")
                self.assertFalse(ledger.exists())
                fg.assert_not_called()
                ff.assert_not_called()


# ---------------------------------------------------------------- ⑦ 既定経路（--lane 省略）不変
class DefaultLaneUnchanged(unittest.TestCase):
    def test_main_without_lane_never_touches_presign(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with mock.patch.object(sys, "argv", ["news_shock_collect.py"]), \
                    mock.patch.object(ns, "LEDGER", tmp / "news_log.jsonl"), \
                    mock.patch.object(ns, "PRESIGN_LEDGER", tmp / "presign_log.jsonl"), \
                    mock.patch.object(ns, "fetch_rss", return_value=[]) as fr, \
                    mock.patch.object(ns, "run_presign") as rp, \
                    mock.patch.object(ns, "_selftest_presign") as sp, \
                    mock.patch.object(ns, "fetch_presign_gnews") as fg, \
                    mock.patch.object(ns, "fetch_feed") as ff, \
                    mock.patch.object(ns, "notify") as nt, _quiet():
                rc = ns.main()
            self.assertEqual(rc, 0)
            self.assertEqual(fr.call_count, len(ns.load_config()["queries"]), "shock のクエリ数ぶん fetch_rss")
            for m in (rp, sp, fg, ff, nt):
                m.assert_not_called()
            self.assertFalse((tmp / "presign_log.jsonl").exists(), "presign 台帳は作られない")
            lines = [json.loads(l) for l in (tmp / "news_log.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([l["type"] for l in lines], ["run_summary"])
            self.assertNotIn("lane", lines[0], "shock の run_summary 行の形は従来どおり（lane キーなし）")

    def test_main_with_lane_presign_never_touches_shock(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with mock.patch.object(sys, "argv", ["x", "--lane", "presign", "--dry-run", "--date", "2026-10-13"]), \
                    mock.patch.object(ns, "LEDGER", tmp / "news_log.jsonl"), \
                    mock.patch.object(ns, "fetch_rss") as fr, \
                    mock.patch.object(ns, "run_presign", return_value=0) as rp, _quiet():
                rc = ns.main()
            self.assertEqual(rc, 0)
            rp.assert_called_once_with(dry_run=True, date_override="2026-10-13")
            fr.assert_not_called()
            self.assertFalse((tmp / "news_log.jsonl").exists(), "shock 台帳には触らない")

    def test_lane_presign_selftest_dispatch(self):
        with mock.patch.object(sys, "argv", ["x", "--lane", "presign", "--selftest"]), \
                mock.patch.object(ns, "_selftest_presign", return_value=0) as sp, \
                mock.patch.object(ns, "_selftest") as s0, _quiet():
            self.assertEqual(ns.main(), 0)
        sp.assert_called_once()
        s0.assert_not_called()


# ---------------------------------------------------------------- ⑧ 既存 --selftest が引き続き全通過
class ExistingSelftestStillPasses(unittest.TestCase):
    def test_shock_selftest_rc0(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/news_shock_collect.py"), "--selftest"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-800:] + r.stderr[-800:])
        self.assertIn("OK: 全通過", r.stdout)

    def test_presign_selftest_rc0(self):
        r = subprocess.run([sys.executable, str(ROOT / "scripts/news_shock_collect.py"), "--lane", "presign",
                            "--selftest"], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-800:] + r.stderr[-800:])
        self.assertIn("OK: presign 全通過", r.stdout)


if __name__ == "__main__":
    unittest.main()
