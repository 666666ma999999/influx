"""presign_forward_eval の単体テスト（固定 fixture・通信なし・台帳は tempdir）.

事前登録 tasks/shortage_presign_forward.md §C/§D/§E の規則が再現することを確かめる:
①flea 状態系列と onset ②lookback 全不明→onset_unknown と lookback_partial ③pu 週次/月次の成立
④判定表 confirm_after/hit/pending/miss/unobserved/unscorable ⑤confirm_after の優先（排他）
⑥複数計器 OR の優先順位 ⑦instruments_override ⑧冪等 ⑨比較率 ⑩collect_interval 外 ⑪score_eligible=false
⑫延長区間の wiki 発火（区間は採点器が判定）⑬多重起動ロック ⑭合格線の浮動小数境界 ⑮confirm_after+pending→pending
⑯pu_weekly/pu_monthly の source・id_field 不一致は ValueError
"""
import contextlib
import fcntl
import io
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import presign_forward_eval as pfe  # noqa: E402

D0 = date(2026, 9, 1)  # fixture の flea 系列の初日（9/1 から値がある）


def d(s):
    return date.fromisoformat(s)


def iso(x):
    return x.isoformat()


def values(n, bumps=(), base=100.0, high=130.0):
    """n 日分の x。bumps= [(開始 ISO 日, 終了 ISO 日・含む)] の期間だけ high."""
    out = []
    for i in range(n):
        t = D0 + timedelta(days=i)
        v = base
        for lo, hi in bumps:
            if d(lo) <= t <= d(hi):
                v = high
        out.append(v)
    return out


def flea_rows(qid, vals, n_top=100, start=None):
    rows = []
    for i, v in enumerate(vals):
        w = (start or D0) + timedelta(days=i)
        o = w + timedelta(days=1)
        if v is None:
            rows.append({"date": iso(o), "query_id": qid, "window": iso(w), "status": "error", "error": "HTTP 429"})
        else:
            nt = n_top(i) if callable(n_top) else n_top
            rows.append({"date": iso(o), "query_id": qid, "window": iso(w), "status": "ok",
                         "sold_n_top100": nt, "sold_median": v})
    return rows


def make_cfg():
    return {
        "version": 1, "fixed_at": "2026-10-10",
        "state_model": {"ratio": 1.2, "onset_lookback_days": {"daily": 14, "weekly": 14, "monthly": 45}},
        "onset_rule": {
            "flea": {"source": "flea.jsonl", "event_date_field": "window", "observed_date_field": "date",
                     "value_field": "sold_median", "min_n_field": "sold_n_top100", "min_n": 3,
                     "smooth_days": 7, "smooth_min_values": 4, "baseline_window_days": [-63, -8],
                     "baseline_min_values": 14, "cadence": "daily"},
            "flea_list_price": {"ratio": 1.2},
            "pu_weekly": {"source": "pu.jsonl", "id_field": "id", "status": "ok", "event_date_field": "date",
                          "value_field": "value", "baseline_window_days": [-63, -8], "baseline_min_values": 4,
                          "cadence": "weekly"},
            "pu_monthly": {"source": "pu.jsonl", "id_field": "id", "status": "ok", "event_date_field": "date",
                           "value_field": "yoy_pct", "threshold_pct": 20.0, "cadence": "monthly"},
            "pu_cadence": {"toreca-sar": "weekly", "rice-m": "monthly"},
        },
        "list_prices": {},
        "scoring": {
            "horizon_days": 60, "confirm_lookback_days": 28, "grace_days": 14,
            "collect_interval": {"start": "2026-10-13", "end_exclusive": "2026-12-12"},
            "extension": {"decide_on": "2026-12-12", "rule": "N<5 なら 2027-02-10（排他）まで 1 回延長",
                          "max_extensions": 1},
            "verdict_table_per_instrument": ["1", "2", "3", "4", "5"],
            "combine_instruments": "OR", "state_transitions": "pending→final once",
            "comparison_rate": {"method": "tuesdays"},
            "pass_line": {"min_scored": 5, "min_hit_rate": 0.5, "min_margin_over_comparison": 0.2},
        },
        "ledger": "ledger.jsonl", "report": "report.md",
        "sources": {"wiki": "wiki.jsonl", "rss": "rss.jsonl"},
        "source_classes": {"wiki": "Wikipedia", "feed-official": "official", "feed-industry": "industry",
                           "gnews-media": "gnews"},
        "subjects": {
            "switch2": {"flea": ["q-switch2"], "pu": []},
            "pokeca": {"flea": ["q-pokeca"], "pu": ["toreca-sar"]},
            "kome": {"flea": [], "pu": ["rice-m"]},
            "tamagotchi": {"flea": [], "pu": []},
            "dual": {"flea": ["q-a", "q-b"], "pu": []},
            "dual2": {"flea": ["q-a", "q-short"], "pu": []},
        },
    }


def wiki_fire(episode_id, title, subject, known_date, first=True, eligible=True):
    return {"type": "fire", "run_at": known_date + "T09:30:00+09:00", "known_date": known_date, "title": title,
            "subject": subject, "week_start": known_date, "week_end": known_date, "sum": 1000, "base_median8": 400,
            "ratio": 2.5, "episode_id": episode_id, "first_in_episode": first, "score_eligible": eligible,
            "config_version": 1, "config_sha": "abc"}


def rss_hit(episode_id, source_class, subject, known_date, first=True):
    return {"type": "hit", "lane": "presign", "run_at": known_date + "T07:20:00+09:00", "known_date": known_date,
            "source_class": source_class, "source_id": "src", "subject": subject, "matched_term": "x",
            "vocab_family": "official_notice", "article_id": "a", "event_id": "e", "episode_id": episode_id,
            "first_in_episode": first, "title": "t", "link": "http://x", "presign_version": 1}


class Env:
    """tempdir に fixture 一式を書き、run() 用の引数を作る."""

    def __init__(self, flea=None, pu_rows=None, wiki_fires=None, rss_hits=None, wiki_titles=None, cfg=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = cfg or make_cfg()
        (self.root / "config.json").write_text(json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8")
        wiki_cfg = {"version": 1, "judging": {}, "titles": wiki_titles or []}
        (self.root / "wiki_cfg.json").write_text(json.dumps(wiki_cfg, ensure_ascii=False), encoding="utf-8")
        (self.root / "pu_sources.json").write_text(json.dumps({"series": []}), encoding="utf-8")
        rows = []
        for qid, vals in (flea or {}).items():
            if isinstance(vals, tuple):  # (開始日, 値の列) で 9/1 以外の起点を指定できる
                rows += flea_rows(qid, vals[1], start=vals[0])
            else:
                rows += flea_rows(qid, vals)
        self._write("flea.jsonl", rows)
        self._write("pu.jsonl", pu_rows or [])
        if wiki_fires is not None:
            self._write("wiki.jsonl", wiki_fires)
        if rss_hits is not None:
            self._write("rss.jsonl", rss_hits)

    def _write(self, name, rows):
        with (self.root / name).open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    def argv(self, as_of, dry_run=False):
        argv = ["--as-of", as_of, "--config", str(self.root / "config.json"),
                "--wiki-config", str(self.root / "wiki_cfg.json"), "--pu-sources", str(self.root / "pu_sources.json"),
                "--flea", str(self.root / "flea.jsonl"), "--pu", str(self.root / "pu.jsonl"),
                "--wiki-ledger", str(self.root / "wiki.jsonl"), "--rss-ledger", str(self.root / "rss.jsonl"),
                "--ledger", str(self.root / "ledger.jsonl"), "--report", str(self.root / "report.md"), "--quiet"]
        if dry_run:
            argv.append("--dry-run")
        return argv

    def args(self, as_of, dry_run=False):
        return pfe.build_parser().parse_args(self.argv(as_of, dry_run))

    def run(self, as_of, dry_run=False):
        return pfe.run(self.args(as_of, dry_run))

    def ledger_rows(self):
        return pfe.read_jsonl(self.root / "ledger.jsonl")

    def scorer(self, as_of):
        return pfe.Scorer(self.cfg, json.loads((self.root / "wiki_cfg.json").read_text()), {"series": []},
                          pfe.read_jsonl(self.root / "flea.jsonl"), pfe.read_jsonl(self.root / "pu.jsonl"), d(as_of))

    def close(self):
        self.tmp.cleanup()


# 系列の設計図（全て 9/1 起点・160 日）
N = 160
FLAT = values(N)
# 10/20〜10/30 を 130 → 成立= 10/23〜11/2（7 日中 4 日以上が 130）・onset 10/23
BUMP_OCT = values(N, bumps=[("2026-10-20", "2026-10-30")])
# 10/30〜11/5 を 130 → 成立= 11/2〜11/8・onset 11/2（K=10/13 から lead 20）
BUMP_NOV = values(N, bumps=[("2026-10-30", "2026-11-05")])
# 9/30〜10/6（onset 10/3= K−10）と 10/30〜11/5（onset 11/2= K+20）の 2 山
TWO_BUMPS = values(N, bumps=[("2026-09-30", "2026-10-06"), ("2026-10-30", "2026-11-05")])
SHORT = values(50)  # 9/1〜10/20 で行が止まる＝(10/13, 12/12] の判定可能日 7 日


class FleaStateTest(unittest.TestCase):
    def test_01_baseline_then_rise_gives_onset(self):
        # 9/1〜9/30 = 100・10/1 以降 130。基準 14 値が揃う 9/22 から判定可能・10/4 で m の中央値が 130 → onset
        vals = values(60, bumps=[("2026-10-01", "2026-10-30")])

        def n_top(i):  # 10/10〜10/13 は成約 2 件（min_n 未満）→ x 不明 → m が 3 値しかなく 10/13 は不明
            t = D0 + timedelta(days=i)
            return 2 if d("2026-10-10") <= t <= d("2026-10-13") else 100

        rows = flea_rows("q", vals, n_top=n_top)
        cfg = make_cfg()
        s = pfe.build_flea_series(rows, "q", d("2027-01-01"), cfg["onset_rule"]["flea"], 1.2)
        s.finalize(14)
        self.assertEqual(s.states[d("2026-09-21")], pfe.UNK)   # 基準 13 値
        self.assertEqual(s.states[d("2026-09-22")], pfe.OFF)   # 基準 14 値
        self.assertEqual(s.states[d("2026-10-03")], pfe.OFF)   # 130 が 3 日分
        self.assertEqual(s.states[d("2026-10-04")], pfe.ON)    # 130 が 4 日分 ≥ 1.2×100
        self.assertEqual(s.onsets, [d("2026-10-04")])
        self.assertEqual(s.onset_unknown, [])
        self.assertEqual(s.states[d("2026-10-13")], pfe.UNK)   # min_n 未満の 4 日で m が作れない
        self.assertEqual(s.observed[d("2026-10-04")], d("2026-10-05"))  # 観測可能日= date（翌日）
        self.assertEqual(s.states[d("2026-10-05")], pfe.ON)    # 成立が続く＝同一 ①（onset は増えない）

    def test_01b_first_rows_without_window_are_skipped(self):
        rows = flea_rows("q", values(30))
        rows.append({"date": "2026-08-31", "query_id": "q", "status": "ok", "sold_n_top100": 100, "sold_median": 999})
        s = pfe.build_flea_series(rows, "q", d("2027-01-01"), make_cfg()["onset_rule"]["flea"], 1.2)
        self.assertEqual(len(s.states), 30)

    def test_02_all_unknown_lookback_gives_onset_unknown_and_lookback_partial(self):
        # 9/1〜9/14 = 100・9/15 以降 130 → 初判定可能日 9/22 が既に成立・直前 14 日は全て不明
        vals = values(60, bumps=[("2026-09-15", "2026-10-30")])
        env = Env(flea={"q-switch2": vals})
        try:
            sc = env.scorer("2026-09-30")
            s = sc.series("flea:q-switch2")
            self.assertEqual(s.states[d("2026-09-22")], pfe.ON)
            self.assertEqual(s.onsets, [])
            self.assertEqual(s.onset_unknown, [d("2026-09-22")])
            res = sc.score(["flea:q-switch2"], d("2026-09-21"))
            iv = res["instrument_verdicts"]["flea:q-switch2"]
            self.assertTrue(iv["lookback_partial"])
            self.assertTrue(res["lookback_partial"])
            # 規則 2b: 窓内の最初の成立の開始が onset_unknown → confirm_after（hit/miss の分母外）
            self.assertEqual(res["verdict"], "confirm_after")
            self.assertEqual(iv["rule"], "2b")
            self.assertTrue(iv["onset_unknown"])
            self.assertTrue(res["onset_unknown"])
            self.assertEqual(res["onset_date"], "2026-09-22")
            self.assertEqual(iv["onset_unknown_in_window"], ["2026-09-22"])
        finally:
            env.close()


class PuStateTest(unittest.TestCase):
    def test_03_weekly_ratio_and_dedupe(self):
        rows = []
        day = d("2026-08-03")
        for _ in range(9):  # 8/3〜9/28 = 100
            rows.append({"date": iso(day), "id": "toreca-sar", "value": 100.0, "status": "ok"})
            day += timedelta(days=7)
        rows.append({"date": "2026-10-05", "id": "toreca-sar", "value": 100.0, "status": "ok"})  # 同一 (id,date)…
        rows.append({"date": "2026-10-05", "id": "toreca-sar", "value": 125.0, "status": "ok"})  # …は最後の行
        rows.append({"date": "2026-10-12", "id": "toreca-sar", "value": 500.0, "status": "stale"})  # ok 以外は読まない
        rule = make_cfg()["onset_rule"]["pu_weekly"]
        s = pfe.build_pu_weekly_series(rows, "toreca-sar", d("2027-01-01"), rule, 1.2).finalize(14)
        self.assertNotIn(d("2026-10-12"), s.states)
        self.assertEqual(s.states[d("2026-08-31")], pfe.UNK)  # 基準 3 観測
        self.assertEqual(s.states[d("2026-09-07")], pfe.OFF)  # 基準 4 観測
        self.assertEqual(s.states[d("2026-09-28")], pfe.OFF)
        self.assertEqual(s.states[d("2026-10-05")], pfe.ON)   # 125 ≥ 1.2×100
        self.assertEqual(s.onsets, [d("2026-10-05")])

    def test_03b_monthly_yoy_and_null(self):
        rows = [
            {"date": "2026-08-30", "id": "rice-m", "src_date": "2026-07", "value": 1.0, "yoy_pct": 10.0, "status": "ok"},
            {"date": "2026-09-14", "id": "rice-m", "src_date": "2026-08", "value": 1.0, "yoy_pct": 25.0, "status": "ok"},
            {"date": "2026-09-28", "id": "rice-m", "src_date": "2026-08", "value": 1.0, "yoy_pct": 25.0, "status": "ok"},
            {"date": "2026-10-05", "id": "rice-m", "src_date": "2026-09", "value": 1.0, "yoy_pct": None, "status": "ok"},
        ]
        rule = make_cfg()["onset_rule"]["pu_monthly"]
        s = pfe.build_pu_monthly_series(rows, "rice-m", d("2027-01-01"), rule).finalize(45)
        self.assertEqual(s.states, {d("2026-08-30"): pfe.OFF, d("2026-09-14"): pfe.ON, d("2026-10-05"): pfe.UNK})
        self.assertNotIn(d("2026-09-28"), s.states)  # 同一 (id, src_date) は最初に観測した行
        self.assertEqual(s.onsets, [d("2026-09-14")])
        # Scorer 経由で cadence=monthly が選ばれる
        env = Env(pu_rows=rows)
        try:
            self.assertEqual(env.scorer("2027-01-01").series("pu:rice-m").cadence, "monthly")
        finally:
            env.close()


class VerdictTableTest(unittest.TestCase):
    K = d("2026-10-13")

    def setUp(self):
        self.env = Env(flea={"q-hit": BUMP_NOV, "q-flat": FLAT, "q-short": SHORT, "q-excl": TWO_BUMPS})

    def tearDown(self):
        self.env.close()

    def judge(self, key, as_of, K=None):
        sc = self.env.scorer(as_of)
        return sc.score([key], K or self.K)

    def test_04a_confirm_after_when_ongoing_at_K(self):
        # onset 11/2・K= 11/5（成立中）→ 判定 1 前半
        res = self.judge("flea:q-hit", "2026-11-20", K=d("2026-11-05"))
        self.assertEqual(res["verdict"], "confirm_after")
        self.assertEqual(res["instrument_verdicts"]["flea:q-hit"]["rule"], "1a")
        self.assertEqual(res["onset_date"], "2026-11-02")
        self.assertTrue(res["final"])

    def test_04a2_confirm_after_when_onset_within_28_days_before_K(self):
        # onset 11/2・成立 11/8 まで・K= 11/12（既に不成立だが onset が K−10）→ 判定 1 後半
        res = self.judge("flea:q-hit", "2026-11-20", K=d("2026-11-12"))
        self.assertEqual(res["verdict"], "confirm_after")
        self.assertEqual(res["instrument_verdicts"]["flea:q-hit"]["rule"], "1b")

    def test_04b_hit_lead_20(self):
        res = self.judge("flea:q-hit", "2026-11-05")  # onset 11/2 の行は 11/3 に観測済み
        self.assertEqual(res["verdict"], "hit")
        self.assertEqual(res["onset_date"], "2026-11-02")
        self.assertEqual(res["lead_days"], 20)
        self.assertEqual(res["detect_lag_days"], 1)
        self.assertEqual(res["instrument"], "flea:q-hit")
        self.assertFalse(res["lookback_partial"])
        self.assertTrue(res["final"])

    def test_04c_pending_before_horizon(self):
        res = self.judge("flea:q-hit", "2026-10-20")  # onset はまだ観測されていない・as_of < K+60
        self.assertEqual(res["verdict"], "pending")
        self.assertEqual(res["instrument_verdicts"]["flea:q-hit"]["rule"], "3")
        self.assertFalse(res["final"])

    def test_04d_miss_with_enough_observations(self):
        res = self.judge("flea:q-flat", "2026-12-13")  # K+61・(K, K+60] に判定可能 60 日
        self.assertEqual(res["verdict"], "miss")
        self.assertEqual(res["instrument_verdicts"]["flea:q-flat"]["judgeable_in_window"], 60)

    def test_04e_unobserved_after_grace(self):
        res = self.judge("flea:q-short", "2026-12-25")  # K+73 → 判定可能 7 日 < 42・猶予内 → pending
        self.assertEqual(res["verdict"], "pending")
        self.assertEqual(res["instrument_verdicts"]["flea:q-short"]["rule"], "5")
        res = self.judge("flea:q-short", "2026-12-26")  # K+74 → unobserved
        self.assertEqual(res["verdict"], "unobserved")

    def test_04f_unscorable_without_instrument(self):
        env = Env(flea={"q-flat": FLAT}, wiki_fires=[], rss_hits=[rss_hit("e1", "gnews-media", "tamagotchi", "2026-10-14")])
        try:
            res = env.run("2026-12-20", dry_run=True)
            self.assertEqual(len(res["episodes"]), 1)
            self.assertEqual(res["episodes"][0]["verdict"], "unscorable")
            self.assertTrue(res["episodes"][0]["final"])
            self.assertEqual(res["sources"]["gnews-media"]["unscorable"], 1)
        finally:
            env.close()

    def test_05_confirm_after_beats_later_hit(self):
        # onset 10/3（K−10）と 11/2（K+20）→ 判定 1 が先に当たり confirm_after（排他）
        res = self.judge("flea:q-excl", "2026-12-20")
        self.assertEqual(res["verdict"], "confirm_after")
        self.assertEqual(res["instrument_verdicts"]["flea:q-excl"]["rule"], "1b")
        self.assertEqual(res["onset_date"], "2026-10-03")


def values_from(start, n, bumps=(), base=100.0, high=130.0):
    """start 起点の n 日分の x（values() の起点可変版）."""
    out = []
    for i in range(n):
        t = start + timedelta(days=i)
        v = base
        for lo, hi in bumps:
            if d(lo) <= t <= d(hi):
                v = high
        out.append(v)
    return out


class Rule2bTest(unittest.TestCase):
    """規則 2b（v1 明確化 2026-10-10）: 窓内の最初の成立の開始が onset_unknown → confirm_after（hit/miss の分母外）.

    実データと同じ形= flea の行が 9/23 から・基準 14 値が [t−63, t−8] に揃う初判定可能日は 10/14・初便の発火 K= 10/13。
    """
    S = d("2026-09-23")
    K = "2026-10-13"

    def test_2b_a_already_on_at_first_judgeable_day_is_confirm_after(self):
        # 9/23〜10/6 = 100・10/7 以降 130 → 10/14 の初判定が既に成立・直前 14 日は全て不明 → onset_unknown
        vals = values_from(self.S, 120, bumps=[("2026-10-07", "2027-01-31")])
        env = Env(flea={"q-switch2": (self.S, vals)}, rss_hits=[],
                  wiki_fires=[wiki_fire("w1", "Nintendo Switch 2", "switch2", self.K)])
        try:
            res = env.run("2026-10-20", dry_run=True)
            s = res["series_summaries"][0]
            self.assertEqual(s["first_judgeable"], "2026-10-14")
            self.assertEqual(s["onset_unknown"], ["2026-10-14"])
            self.assertEqual(s["onsets"], [])
            e = res["episodes"][0]
            self.assertEqual(e["verdict"], "confirm_after")
            self.assertTrue(e["final"])
            self.assertTrue(e["onset_unknown"])
            self.assertTrue(e["lookback_partial"])
            self.assertEqual(e["onset_date"], "2026-10-14")
            iv = e["instrument_verdicts"]["flea:q-switch2"]
            self.assertEqual(iv["rule"], "2b")
            self.assertTrue(iv["onset_unknown"])
            self.assertTrue(iv["lookback_partial"])
            w = res["sources"]["wiki"]
            self.assertEqual((w["hit"], w["miss"], w["confirm_after"], w["confirm_after_onset_unknown"]), (0, 0, 1, 1))
            self.assertIsNone(w["hit_rate"])
        finally:
            env.close()

    def test_2b_b_off_at_first_judgeable_then_on_is_hit(self):
        # 10/14 は不成立（100）・10/17 以降 130 → 10/20 に成立（直前に不成立あり＝開始日あり）→ hit・lead 7
        vals = values_from(self.S, 120, bumps=[("2026-10-17", "2027-01-31")])
        env = Env(flea={"q-switch2": (self.S, vals)}, rss_hits=[],
                  wiki_fires=[wiki_fire("w1", "Nintendo Switch 2", "switch2", self.K)])
        try:
            res = env.run("2026-10-21", dry_run=True)  # 10/20 の行は 10/21 に観測
            s = res["series_summaries"][0]
            self.assertEqual(s["onsets"], ["2026-10-20"])
            self.assertEqual(s["onset_unknown"], [])
            e = res["episodes"][0]
            self.assertEqual(e["verdict"], "hit")
            self.assertEqual(e["lead_days"], 7)
            self.assertEqual(e["detect_lag_days"], 1)
            self.assertFalse(e["onset_unknown"])
            self.assertTrue(e["lookback_partial"])  # K 以前は全て不明＝規則 1 は飛ばす
            self.assertEqual(e["instrument_verdicts"]["flea:q-switch2"]["rule"], "2")
            self.assertEqual((res["sources"]["wiki"]["hit"], res["sources"]["wiki"]["miss"]), (1, 0))
        finally:
            env.close()

    def test_2b_c_comparison_pseudo_fire_goes_through_same_rule(self):
        # 疑似発火 T= 10/13 も同じ関数＝発火の verdict と一致し、比較率の分母にも入らない
        vals = values_from(self.S, 120, bumps=[("2026-10-07", "2027-01-31")])
        env = Env(flea={"q-switch2": (self.S, vals)}, rss_hits=[],
                  wiki_fires=[wiki_fire("w1", "Nintendo Switch 2", "switch2", self.K)])
        try:
            res = env.run("2026-10-20", dry_run=True)
            cr = dict(res["subject_comparisons"])["switch2"]
            self.assertEqual(cr["attempted"], 2)  # 10/13・10/20（as_of 10/20 まで）
            self.assertEqual(cr["verdicts"]["2026-10-13"], res["episodes"][0]["verdict"])
            self.assertEqual(cr["verdicts"]["2026-10-13"], "confirm_after")
            self.assertEqual(cr["verdicts"]["2026-10-20"], "confirm_after")  # 10/20 は成立中＝規則 1 前半
            self.assertEqual((cr["hit"], cr["miss"]), (0, 0))
            self.assertIsNone(cr["rate"])
        finally:
            env.close()

    def test_2b_d_first_establishment_decides_even_if_datable_onset_follows(self):
        # 10/14 から onset_unknown の成立（〜10/20 頃）→ 不成立 → 11/13 に開始日ありの onset。窓内の「最初の成立」は前者
        vals = values_from(self.S, 120, bumps=[("2026-10-07", "2026-10-18"), ("2026-11-10", "2026-11-25")])
        env = Env(flea={"q-switch2": (self.S, vals)})
        try:
            sc = env.scorer("2026-12-20")
            s = sc.series("flea:q-switch2")
            self.assertEqual(s.onset_unknown, [d("2026-10-14")])
            self.assertEqual(s.onsets, [d("2026-11-13")])
            res = sc.score(["flea:q-switch2"], d(self.K))
            self.assertEqual(res["verdict"], "confirm_after")
            self.assertTrue(res["onset_unknown"])
            self.assertEqual(res["instrument_verdicts"]["flea:q-switch2"]["rule"], "2b")
            # 疑似発火 11/3（最初の成立の開始が 11/13 の onset）→ hit・lead 10
            res2 = sc.score(["flea:q-switch2"], d("2026-11-03"))
            self.assertEqual((res2["verdict"], res2["lead_days"]), ("hit", 10))
        finally:
            env.close()


class CombineTest(unittest.TestCase):
    def test_06_or_priority(self):
        env = Env(flea={"q-a": FLAT, "q-b": BUMP_NOV, "q-short": SHORT})
        try:
            sc = env.scorer("2026-12-13")
            res = sc.score(["flea:q-a", "flea:q-b"], d("2026-10-13"))  # miss + hit → hit
            self.assertEqual(res["verdict"], "hit")
            self.assertEqual(res["instrument"], "flea:q-b")
            self.assertEqual(res["instrument_verdicts"]["flea:q-a"]["verdict"], "miss")
            res = sc.score(["flea:q-a", "flea:q-short"], d("2026-10-13"))  # miss + pending → pending
            self.assertEqual(res["verdict"], "pending")
            self.assertFalse(res["final"])
            self.assertEqual(res["instrument_verdicts"]["flea:q-short"]["verdict"], "pending")
        finally:
            env.close()

    def test_06c_confirm_after_plus_pending_stays_pending(self):
        # 束ね規則（v1 明確化）: hit が無く pending が 1 つでもあれば pending（確定しない・台帳に書かない）
        base = {"instrument": "x", "lead_days": None, "detect_lag_days": None, "onset_unknown": False, "lookback_partial": False}
        iv = {"flea:a": {**base, "verdict": "confirm_after", "onset_date": "2026-10-23", "instrument": "flea:a"},
              "flea:b": {**base, "verdict": "pending", "onset_date": None, "instrument": "flea:b"}}
        res = pfe.combine_verdicts(iv)
        self.assertEqual((res["verdict"], res["final"], res["onset_date"]), ("pending", False, None))
        # 計器経由でも同じ: K= 10/25 で q-a は成立中（規則 1a）・q-short は行が止まり K+74 前（規則 5 → pending）
        env = Env(flea={"q-a": BUMP_OCT, "q-short": SHORT})
        try:
            res = env.scorer("2026-12-28").score(["flea:q-a", "flea:q-short"], d("2026-10-25"))
            self.assertEqual(res["instrument_verdicts"]["flea:q-a"]["verdict"], "confirm_after")
            self.assertEqual(res["instrument_verdicts"]["flea:q-short"]["verdict"], "pending")
            self.assertEqual(res["verdict"], "pending")
            self.assertFalse(res["final"])
        finally:
            env.close()

    def test_06b_hit_takes_earliest_onset(self):
        env = Env(flea={"q-a": BUMP_NOV, "q-b": BUMP_OCT})
        try:
            res = env.scorer("2026-12-13").score(["flea:q-a", "flea:q-b"], d("2026-10-13"))
            self.assertEqual(res["verdict"], "hit")
            self.assertEqual(res["onset_date"], "2026-10-23")
            self.assertEqual(res["instrument"], "flea:q-b")
            self.assertEqual(res["lead_days"], 10)
        finally:
            env.close()


class OverrideTest(unittest.TestCase):
    def test_07_instruments_override_wins_over_subject(self):
        titles = [{"title": "T-plain", "subject": "switch2"},
                  {"title": "T-ov", "subject": "switch2", "instruments_override": {"flea": ["q-hit"], "pu": []}}]
        env = Env(flea={"q-switch2": FLAT, "q-hit": BUMP_NOV}, wiki_titles=titles,
                  wiki_fires=[wiki_fire("ep-plain", "T-plain", "switch2", "2026-10-13"),
                              wiki_fire("ep-ov", "T-ov", "switch2", "2026-10-13")], rss_hits=[])
        try:
            res = env.run("2026-12-13", dry_run=True)
            by = {e["episode_id"]: e for e in res["episodes"]}
            self.assertEqual(by["ep-plain"]["verdict"], "miss")
            self.assertEqual(by["ep-plain"]["instruments"], ["flea:q-switch2"])
            self.assertEqual(by["ep-ov"]["verdict"], "hit")
            self.assertEqual(by["ep-ov"]["instruments"], ["flea:q-hit"])
            self.assertEqual(by["ep-ov"]["instruments_from"], "instruments_override")
        finally:
            env.close()


class LedgerTest(unittest.TestCase):
    def test_08_idempotent_ledger(self):
        env = Env(flea={"q-switch2": BUMP_OCT, "q-pokeca": FLAT},
                  wiki_fires=[wiki_fire("w1", "Nintendo Switch 2", "switch2", "2026-10-13"),
                              wiki_fire("w2", "Nintendo Switch 2", "switch2", "2026-12-08")],  # 12/8 は as_of で pending
                  rss_hits=[rss_hit("r1", "gnews-media", "pokeca", "2026-10-14")])
        try:
            # as_of 12/28= r1（10/14）の K+74 を過ぎている＝pu 計器（行なし）が unobserved になり flea の miss が立つ
            # （12/27 以前だと pu が pending（規則 5）で OR 優先により episode も pending のまま）
            res1 = env.run("2026-12-28")
            rows1 = env.ledger_rows()
            self.assertEqual(sum(1 for r in rows1 if r["type"] == "preregistration"), 1)
            scores1 = [r for r in rows1 if r["type"] == "score"]
            self.assertEqual({r["episode_id"] for r in scores1}, {"w1", "r1"})  # w2 は pending → 書かない
            self.assertTrue(all(r["final"] for r in scores1))
            self.assertEqual({r["episode_id"]: r["verdict"] for r in scores1}, {"w1": "hit", "r1": "miss"})
            self.assertEqual(res1["written"]["scores"], 2)
            self.assertTrue(res1["written"]["preregistration"])
            comps1 = [r for r in rows1 if r["type"] == "comparison"]
            self.assertTrue(comps1)
            self.assertTrue(Path(res1["written"]["report"]).exists())
            # 2 回目: 何も増えない
            res2 = env.run("2026-12-28")
            rows2 = env.ledger_rows()
            self.assertEqual(len(rows2), len(rows1))
            self.assertEqual(res2["written"], {"preregistration": False, "scores": 0, "comparisons": 0,
                                               "report": res1["written"]["report"]})
            self.assertTrue(all(e["from_ledger"] for e in res2["episodes"] if e["episode_id"] in ("w1", "r1")))
            # 3 回目（後日）: w2 が確定して 1 行だけ増える・確定済は再採点されない
            res3 = env.run("2027-02-20")
            rows3 = env.ledger_rows()
            self.assertEqual(sum(1 for r in rows3 if r["type"] == "score"), 3)
            self.assertEqual(res3["written"]["scores"], 1)
            self.assertEqual(sum(1 for r in rows3 if r["type"] == "preregistration"), 1)
            keys = [(r["source_class"], r["episode_id"], r["spec_version"]) for r in rows3 if r["type"] == "score"]
            self.assertEqual(len(keys), len(set(keys)))
        finally:
            env.close()

    def test_08c_lock_held_elsewhere_skips_and_writes_nothing(self):
        env = Env(flea={"q-switch2": BUMP_OCT}, wiki_fires=[], rss_hits=[rss_hit("r1", "gnews-media", "switch2", "2026-10-13")])
        holder = (env.root / ".eval.lock").open("w")
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 別の採点が握っている状態
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                res = env.run("2026-12-28")
                rc = pfe.main(env.argv("2026-12-28"))
            self.assertTrue(res.get("skipped"))
            self.assertEqual(rc, 0)
            self.assertIn("別の採点が実行中のためスキップ", buf.getvalue())
            self.assertFalse((env.root / "ledger.jsonl").exists())
            self.assertFalse((env.root / "report.md").exists())
            # dry-run はロックを取らない＝握られていても報告は出る
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(env.run("2026-12-28", dry_run=True).get("skipped"))
            holder.close()  # 解放後は通常どおり書ける
            holder = None
            res2 = env.run("2026-12-28")
            self.assertFalse(res2.get("skipped"))
            self.assertTrue((env.root / "ledger.jsonl").exists())
            self.assertTrue((env.root / "report.md").exists())
        finally:
            if holder is not None:
                holder.close()
            env.close()

    def test_08b_dry_run_writes_nothing(self):
        env = Env(flea={"q-switch2": BUMP_OCT}, wiki_fires=[wiki_fire("w1", "Nintendo Switch 2", "switch2", "2026-10-13")],
                  rss_hits=[])
        try:
            res = env.run("2026-12-20", dry_run=True)
            self.assertEqual(res["episodes"][0]["verdict"], "hit")
            self.assertFalse((env.root / "ledger.jsonl").exists())
            self.assertFalse((env.root / "report.md").exists())
            self.assertIn("dry-run", res["report"])
        finally:
            env.close()


class ComparisonTest(unittest.TestCase):
    def test_09_comparison_rate_matches_hand_calculation(self):
        # switch2: onset 10/23 → 火曜 10/13・10/20 = hit／10/27・11/3・11/10・11/17 = confirm_after／11/24・12/1・12/8 = miss
        #   → 2/(2+3) = 0.4。pokeca: flea 平坦 + pu 行なし → 9 本とも miss → 0.0
        # wiki 発火 5 本（N=5 で延長なし＝火曜 9 本）= switch2 ×3（10/13 hit・10/20 hit・11/24 miss）・pokeca ×2（miss ×2）
        #   → 源の比較率= (0.4×3 + 0.0×2)/5 = 0.24
        env = Env(flea={"q-switch2": BUMP_OCT, "q-pokeca": FLAT},
                  wiki_fires=[wiki_fire("w1", "Nintendo Switch 2", "switch2", "2026-10-13"),
                              wiki_fire("w2", "Nintendo Switch 2", "switch2", "2026-10-20"),
                              wiki_fire("w3", "Nintendo Switch 2", "switch2", "2026-11-24"),
                              wiki_fire("w4", "ポケモンカードゲーム", "pokeca", "2026-10-13"),
                              wiki_fire("w5", "ポケモンカードゲーム", "pokeca", "2026-10-20")], rss_hits=[])
        try:
            # as_of 2/21= 最後の火曜 12/8 の K+74（2/20）を過ぎている＝pu 計器（行なし）が unobserved で確定し
            # pokeca の 9 本が全て miss になる（2/10 だと 12/1・12/8 の pu が規則 5 の pending で episode も pending）
            res = env.run("2027-02-21", dry_run=True)
            self.assertFalse(res["intervals"]["wiki"]["extended"])
            comp = dict(res["subject_comparisons"])
            sw = comp["switch2"]
            self.assertEqual(sw["attempted"], 9)
            self.assertEqual(sw["tuesdays"][0], "2026-10-13")
            self.assertEqual(sw["tuesdays"][-1], "2026-12-08")
            self.assertEqual((sw["hit"], sw["miss"], sw["confirm_after"]), (2, 3, 4))
            self.assertAlmostEqual(sw["rate"], 0.4)
            self.assertEqual(sw["verdicts"]["2026-10-20"], "hit")
            self.assertEqual(sw["verdicts"]["2026-10-27"], "confirm_after")
            self.assertEqual(sw["verdicts"]["2026-11-24"], "miss")
            pk = comp["pokeca"]
            self.assertEqual((pk["hit"], pk["miss"]), (0, 9))
            self.assertEqual(pk["rate"], 0.0)
            self.assertIsNone(comp["tamagotchi"]["rate"])
            w = res["sources"]["wiki"]
            self.assertEqual((w["N"], w["hit"], w["miss"], w["scored"]), (5, 2, 3, 5))
            self.assertAlmostEqual(w["comparison"], 0.24)
            self.assertAlmostEqual(w["hit_rate"], 0.4)
            self.assertEqual(w["distinct_onsets"], 1)
            self.assertEqual(w["subjects"], 2)
            self.assertEqual(w["lead_median"], 6.5)  # lead 10（10/13）と 3（10/20）の中央値
            self.assertEqual(pfe.pass_line(w, env.cfg["scoring"]["pass_line"]), "不合格")  # 的中率 0.4 < 0.5
            self.assertIn("| wiki | 5 | 2 | 3 |", res["report"])
        finally:
            env.close()

    def test_09b_wilson(self):
        lo, hi = pfe.wilson(3, 5)
        self.assertAlmostEqual(lo, 0.2307, places=3)
        self.assertAlmostEqual(hi, 0.8824, places=3)
        self.assertIsNone(pfe.wilson(0, 0))

    def test_09c_pass_line(self):
        pl = make_cfg()["scoring"]["pass_line"]
        base = {"scored": 6, "hit_rate": 4 / 6, "comparison": 0.3}
        self.assertTrue(pfe.pass_line(base, pl).startswith("合格"))
        self.assertEqual(pfe.pass_line({**base, "comparison": 0.5}, pl), "不合格")
        self.assertTrue(pfe.pass_line({**base, "scored": 4}, pl).startswith("判定不能"))

    def test_09e_pass_line_float_boundary(self):
        # 7/10 − 1/2 = 0.19999999999999996 を「20 pt 未満」と誤判定しない（Codex 実装レビュー ③）
        pl = make_cfg()["scoring"]["pass_line"]
        self.assertTrue(pfe.pass_line({"scored": 10, "hit_rate": 7 / 10, "comparison": 1 / 2}, pl).startswith("合格"))
        self.assertTrue(pfe.pass_line({"scored": 10, "hit_rate": 5 / 10, "comparison": 3 / 10}, pl).startswith("合格"))
        self.assertEqual(pfe.pass_line({"scored": 10, "hit_rate": 6 / 10, "comparison": 1 / 2}, pl), "不合格")

    def test_09d_no_tuesday_before_interval(self):
        env = Env(flea={"q-switch2": FLAT}, wiki_fires=[], rss_hits=[])
        try:
            res = env.run("2026-10-10", dry_run=True)
            self.assertEqual(dict(res["subject_comparisons"])["switch2"]["attempted"], 0)
            self.assertIn("0 本", res["report"])
        finally:
            env.close()


class PopulationTest(unittest.TestCase):
    def test_10_collect_interval_is_half_open(self):
        env = Env(flea={"q-switch2": FLAT}, wiki_fires=[], rss_hits=[
            rss_hit("before", "gnews-media", "switch2", "2026-10-12"),
            rss_hit("start", "gnews-media", "switch2", "2026-10-13"),
            rss_hit("last", "gnews-media", "switch2", "2026-12-11"),
            rss_hit("end", "gnews-media", "switch2", "2026-12-12"),
        ])
        try:
            # 延長判定日（12/12）より前の as_of= 基準区間 [10/13, 12/12) のまま
            res = env.run("2026-12-11", dry_run=True)
            self.assertEqual({e["episode_id"] for e in res["episodes"]}, {"start", "last"})
            self.assertEqual(res["tally"]["gnews-media"]["rows"], 4)
            self.assertEqual(res["sources"]["gnews-media"]["N"], 2)
            # 12/12 以降は N=2 < 5 で延長され 12/12 の発火も母集団に入る（仕様どおり）
            res2 = env.run("2027-03-01", dry_run=True)
            self.assertTrue(res2["intervals"]["gnews-media"]["extended"])
            self.assertEqual({e["episode_id"] for e in res2["episodes"]}, {"start", "last", "end"})
        finally:
            env.close()

    def test_10b_known_date_after_as_of_is_not_yet_in_population(self):
        env = Env(flea={"q-switch2": FLAT}, wiki_fires=[], rss_hits=[rss_hit("late", "gnews-media", "switch2", "2026-11-10")])
        try:
            self.assertEqual(env.run("2026-11-09", dry_run=True)["episodes"], [])
            self.assertEqual(len(env.run("2026-11-10", dry_run=True)["episodes"]), 1)
        finally:
            env.close()

    def test_11_wiki_requires_first_in_episode_and_score_eligible(self):
        env = Env(flea={"q-switch2": FLAT}, rss_hits=[], wiki_fires=[
            wiki_fire("ok", "Nintendo Switch 2", "switch2", "2026-10-13"),
            wiki_fire("not-eligible", "Nintendo Switch 2", "switch2", "2026-10-13", eligible=False),
            wiki_fire("not-first", "Nintendo Switch 2", "switch2", "2026-10-20", first=False),
            {**wiki_fire("obs", "Nintendo Switch 2", "switch2", "2026-10-13"), "type": "week_obs", "fired": True},
        ])
        try:
            res = env.run("2027-03-01", dry_run=True)
            self.assertEqual([e["episode_id"] for e in res["episodes"]], ["ok"])
            t = res["tally"]["wiki"]
            self.assertEqual((t["rows"], t["first_in_episode"], t["score_eligible"], t["deduped"]), (3, 2, 1, 1))
        finally:
            env.close()

    def test_11b_rss_requires_first_in_episode(self):
        env = Env(flea={"q-switch2": FLAT}, wiki_fires=[], rss_hits=[
            rss_hit("a", "feed-official", "switch2", "2026-10-13"),
            rss_hit("a", "feed-official", "switch2", "2026-10-15", first=False),
            {**rss_hit("neg", "feed-official", "switch2", "2026-10-13"), "type": "negated"},
        ])
        try:
            res = env.run("2027-03-01", dry_run=True)
            self.assertEqual([e["episode_id"] for e in res["episodes"]], ["a"])
            self.assertEqual(res["sources"]["feed-official"]["N"], 1)
        finally:
            env.close()


class ExtensionTest(unittest.TestCase):
    def test_12_extension_only_for_sources_under_five(self):
        fires = [rss_hit(f"g{i}", "gnews-media", "switch2", "2026-10-14") for i in range(5)]
        fires.append(rss_hit("f1", "feed-official", "switch2", "2026-10-14"))
        fires.append(rss_hit("f-ext", "feed-official", "switch2", "2027-01-20"))  # 延長区間内
        fires.append(rss_hit("g-ext", "gnews-media", "switch2", "2027-01-20"))    # 延長なし → 母集団外
        env = Env(flea={"q-switch2": FLAT}, wiki_fires=[], rss_hits=fires)
        try:
            res = env.run("2027-03-01", dry_run=True)
            iv = res["intervals"]
            self.assertFalse(iv["gnews-media"]["extended"])
            self.assertTrue(iv["feed-official"]["extended"])
            self.assertEqual(pfe.iso(iv["feed-official"]["end"]), "2027-02-10")
            ids = {e["episode_id"] for e in res["episodes"]}
            self.assertIn("f-ext", ids)
            self.assertNotIn("g-ext", ids)
            # 延長判定日より前は未判定
            res2 = env.run("2026-12-11", dry_run=True)
            self.assertFalse(res2["intervals"]["feed-official"]["decided"])
        finally:
            env.close()


    def test_13_wiki_fire_in_extension_counted_only_when_extended(self):
        # wiki 行の score_eligible は鮮度だけ＝12/15 の発火が母集団に入るかは採点器の区間判定（延長の有無）で決まる
        late = wiki_fire("w-late", "Nintendo Switch 2", "switch2", "2026-12-15")
        few = [wiki_fire("w1", "Nintendo Switch 2", "switch2", "2026-10-14"), late]
        env = Env(flea={"q-switch2": FLAT}, rss_hits=[], wiki_fires=few)
        try:
            res = env.run("2027-03-01", dry_run=True)
            self.assertTrue(res["intervals"]["wiki"]["extended"])  # N=1 < 5
            self.assertIn("w-late", {e["episode_id"] for e in res["episodes"]})
            res2 = env.run("2026-12-11", dry_run=True)  # 延長判定日の前＝基準区間のまま（12/15 は未来でもある）
            self.assertNotIn("w-late", {e["episode_id"] for e in res2["episodes"]})
        finally:
            env.close()
        many = [wiki_fire(f"w{i}", "Nintendo Switch 2", "switch2", "2026-10-14") for i in range(5)] + [late]
        env = Env(flea={"q-switch2": FLAT}, rss_hits=[], wiki_fires=many)
        try:
            res = env.run("2027-03-01", dry_run=True)
            self.assertFalse(res["intervals"]["wiki"]["extended"])  # N=5 → 延長なし → 12/15 は母集団外
            self.assertNotIn("w-late", {e["episode_id"] for e in res["episodes"]})
            self.assertEqual(res["sources"]["wiki"]["N"], 5)
        finally:
            env.close()


class ConfigContractTest(unittest.TestCase):
    def test_16_pu_id_field_or_source_mismatch_raises(self):
        cfg = make_cfg()
        cfg["onset_rule"]["pu_monthly"]["id_field"] = "series_id"
        env = Env(flea={"q-switch2": FLAT}, wiki_fires=[], rss_hits=[], cfg=cfg)
        try:
            with self.assertRaises(ValueError):
                env.scorer("2026-12-13")
            with self.assertRaises(ValueError):
                env.run("2026-12-13", dry_run=True)
        finally:
            env.close()
        cfg = make_cfg()
        cfg["onset_rule"]["pu_monthly"]["source"] = "pu_monthly.jsonl"
        env = Env(flea={"q-switch2": FLAT}, wiki_fires=[], rss_hits=[], cfg=cfg)
        try:
            with self.assertRaises(ValueError):
                env.run("2026-12-13", dry_run=True)
        finally:
            env.close()
        # 正当な config（同じ source・同じ id_field）は通る＝偽陽性なし
        env = Env(flea={"q-switch2": FLAT}, wiki_fires=[], rss_hits=[])
        try:
            self.assertIn("未実行", env.run("2026-12-13", dry_run=True)["report"])
        finally:
            env.close()


class HelperTest(unittest.TestCase):
    def test_tuesdays(self):
        ts = pfe.tuesdays_between(d("2026-10-13"), d("2026-12-12"))
        self.assertEqual(len(ts), 9)
        self.assertEqual(ts[0], d("2026-10-13"))
        self.assertEqual(ts[-1], d("2026-12-08"))
        self.assertTrue(all(t.weekday() == 1 for t in ts))
        self.assertEqual(pfe.tuesdays_between(d("2026-10-14"), d("2026-10-21"))[0], d("2026-10-20"))

    def test_ge_tolerates_float_rounding(self):
        self.assertTrue(pfe.ge(120.0, 1.2 * 100.0))
        self.assertFalse(pfe.ge(119.9, 1.2 * 100.0))


if __name__ == "__main__":
    unittest.main()
